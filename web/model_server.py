# -*- coding: utf-8 -*-
"""RetroChimera 常驻模型服务 —— 独立进程，只干一件事：把模型加载好并接待推理请求。

为什么要有这个服务
------------------
模型加载慢（首次 40~90 秒起，冷盘更久），而 API 服务要改代码、要重启、要秒起。
把两者放进同一个进程 ⇒ 每次重启 API 都连带重载模型，改一行代码等一分钟。

现在拆成：

    web/app.py          →  API 服务（:8000），秒起；模型状态与推理一律**HTTP 转接**
    web/model_server.py →  模型服务（:8001），常驻；由 API 拉起或复用

API 重启时若探测到本服务还活着，就直接复用 —— 模型**不重载**。
前端永远只跟 API 说话，不直连本服务（8001 只绑定 127.0.0.1）。

🔴 为什么「换模型 = 重启本进程」而不是热切换
--------------------------------------------
`RetroChimeraModel` 构造时会 `spawn` 规则应用子进程。Windows 的 spawn 在
**多线程进程**里会死锁（子进程 re-import 主模块时卡住）—— 这是本项目最贵的坑。
所以要加载模型，进程必须正处在**单线程阶段**。

本服务正是这么做的（启动顺序严格如下）：

    1. 进程起来，仍单线程  →  写 state 文件（loading，让 API 能报告进度）
    2. 加载模型（spawn 规则子进程，此刻安全）
    3. 写 state 文件（ready）
    4. 起 Flask（这之后才有请求线程；此时再也不需要 spawn 了）

因此「加载第二个模型」只能靠**重启本进程**完成：API 先 `POST /shutdown`
让本进程退出（顺带 clean 回收规则子进程），再用 `subprocess.Popen` 起一份新的。
本次设计一次只持有一个模型 —— 实测每个模型连同规则子进程约 18 GB，
两个并存有打满页面文件的风险（见 bootstrap/limit_workers.py）。

用法
----
    python web/model_server.py                    # 默认 8001，加载默认模型
    set RETRO_PRELOAD=uspto50k && python web/model_server.py
    set RETRO_MODEL_PORT=8011 && python web/model_server.py

通常不用手动跑 —— API 服务（`启动后端API.bat`）会按需拉起并复用本服务。
"""
from __future__ import annotations

# --- 让 `from bootstrap import ...` 可用（本文件在 web/ 下） -----------------
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)  # 仓库根
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 🔴 这两个必须在任何第三方 import 之前：envfix 修 DLL 搜索路径（_ssl 报错），
#    limit_workers 把规则子进程数从 16 压到 4（否则 70GB 打满页面文件）。
from bootstrap import envfix  # noqa: F401, E402
from bootstrap import limit_workers  # noqa: F401, E402

import atexit  # noqa: E402
import json  # noqa: E402
import signal  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

from flask import Flask, jsonify, request  # noqa: E402

import model_runtime  # noqa: E402  纯模块：无 Flask 副作用，可安全单线程导入

# --------------------------------------------------------------------------
# 运行参数
# --------------------------------------------------------------------------
PORT = int(os.environ.get("RETRO_MODEL_PORT", "8001"))
HOST = "127.0.0.1"

#: state 文件 —— 加载期间本服务**还没起 HTTP**，API 靠这个文件知道「在加载什么」。
#: 按端口分文件：同一台机器跑两个实例（如做并行验证）时互不干扰。
STATE_FILE = os.environ.get("RETRO_MODEL_STATE_FILE") or os.path.join(
    _PARENT, ".tmp-model-service", f"state-{PORT}.json"
)

host = model_runtime.ModelHost()
_started_at = time.time()


def _write_state(**extra) -> None:
    """原子写 state 文件（tmp + os.replace），避免 API 读到半截 JSON。"""
    payload = {
        "pid": os.getpid(),
        "port": PORT,
        "state": host.state,
        "model": host.model_key,
        "phase": host.phase,
        "error": host.error,
        "load_seconds": host.load_seconds,
        "started_at": _started_at,
        "updated_at": time.time(),
    }
    payload.update(extra)
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:  # noqa: BLE001
        print(f"[model] 写状态文件失败（无碍，只是 API 看不到进度）：{e}", flush=True)


def _remove_state() -> None:
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 退出清理：回收规则子进程（否则关窗口时它们变孤儿并刷满屏 BrokenPipeError）
# --------------------------------------------------------------------------
_CTRL_HANDLER = None


def _bye(exit_code: int = 0) -> None:
    """卸载模型（关规则子进程）后退出。幂等。"""
    try:
        if host.state == "ready":
            closed = model_runtime.shutdown_rule_servers(host._model)  # noqa: SLF001
            if closed:
                print(f"[model] 已关闭 {closed} 组规则子进程", flush=True)
        host._model = None  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    finally:
        _remove_state()
        os._exit(exit_code)  # noqa: SLF001


def _install_exit_hooks() -> None:
    atexit.register(lambda: _remove_state())

    def _sigint(signum, frame):  # noqa: ARG001
        print("\n[model] 收到 Ctrl+C，正在回收规则子进程……", flush=True)
        _bye(130)

    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:  # noqa: BLE001
        pass

    # Windows：点窗口 × 时用 SetConsoleCtrlHandler 接管，先收子进程再退出
    try:
        import ctypes
        from ctypes import wintypes

        HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _console_handler(ctrl_type):  # noqa: ARG001
            _bye(0)
            return False

        global _CTRL_HANDLER
        _CTRL_HANDLER = HANDLER_ROUTINE(_console_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# HTTP 接口（只有 API 服务会来调它；绑定 127.0.0.1，不对外）
# --------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "retrochimera-model", "port": PORT})


@app.route("/status")
def status():
    """模型服务实时状态。API 的 /api/models 会把它合并进状态矩阵。"""
    st = host.status()
    st.update(
        {
            "ok": True,
            "pid": os.getpid(),
            "port": PORT,
            "uptime": round(time.time() - _started_at, 1),
        }
    )
    return jsonify(st)


def _infer_error(e: Exception):
    """把推理异常翻译成合适的 HTTP 响应。

    🔴 「模型未就绪 / 正在加载」是**可预期的业务状态**，不是服务器故障：
       从前一律回 500，调用方只能看到「模型服务返回 500」，而真正的原因
       （还在加载 / 没加载）埋在日志里。现在未就绪回 409 + 人话，
       调用方可以把它原样呈现给用户。
    """
    if host.state != "ready":
        return jsonify({"ok": False, "error": str(e), "state": host.state}), 409
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/predict", methods=["POST"])
def predict():
    """单步逆合成预测。body: {smiles, num_results}"""
    data = request.get_json(force=True, silent=True) or {}
    smiles = str(data.get("smiles") or "").strip()
    if not smiles:
        return jsonify({"ok": False, "error": "缺少 smiles"}), 400
    try:
        num_results = max(1, min(int(data.get("num_results", 5)), 20))
    except (TypeError, ValueError):
        num_results = 5
    try:
        return jsonify({"ok": True, "results": host.predict(smiles, num_results)})
    except Exception as e:  # noqa: BLE001
        if host.state == "ready":
            traceback.print_exc()  # 真出错了才打栈；未就绪是可预期状态，不打
        return _infer_error(e)


@app.route("/search", methods=["POST"])
def search():
    """多步路线搜索。body 直接就是 run_retro_search 的 kwargs。"""
    data = request.get_json(force=True, silent=True) or {}
    smiles = str(data.get("smiles") or "").strip()
    if not smiles:
        return jsonify({"ok": False, "error": "缺少 smiles"}), 400
    allowed = (
        "smiles", "max_routes", "max_calls", "time_limit", "max_depth",
        "max_heavy_atoms", "value_fn", "purchasable", "required_materials",
    )
    params = {k: v for k, v in data.items() if k in allowed}
    try:
        return jsonify({"ok": True, "search": host.search(params)})
    except Exception as e:  # noqa: BLE001
        if host.state == "ready":
            traceback.print_exc()
        return _infer_error(e)


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """优雅退出（供 API 换模型时调用：先停本服务，再由 API 拉起新的）。

    响应先返回（让调用方拿到 200），真正的卸载+退出放在短延迟的后台线程里，
    因为它要等规则子进程 close 完 —— 那可能要几秒。
    """

    def _later():
        time.sleep(0.25)
        print("[model] 收到 shutdown 请求，正在退出……", flush=True)
        _bye(0)

    threading.Thread(target=_later, daemon=True).start()
    return jsonify({"ok": True, "bye": True})


# --------------------------------------------------------------------------
# 启动：**顺序至关重要**（单线程加载 → 再起 Flask）
# --------------------------------------------------------------------------
def main() -> None:
    print("=" * 68, flush=True)
    print("  RetroChimera 模型服务（常驻）", flush=True)
    print("=" * 68, flush=True)
    print(f"  监听 127.0.0.1:{PORT}   （仅本机；前端不直连它，只经 API 服务）", flush=True)

    _install_exit_hooks()

    # 🔴 统一走 default_model_key()：它会把 `RETRO_PRELOAD=none/0/false/off`
    #    这类「显式要求不预加载」的写法解析成空串。从前这里直接用
    #    os.environ.get()，"none" 会被当成模型名传下去 → 「未知模型: none」。
    key = model_runtime.default_model_key()
    if len(sys.argv) > 1 and sys.argv[1].strip():
        key = sys.argv[1].strip()

    if not key:
        host.state = "idle"
        _write_state(note="未指定预加载模型")
        print("[model] 未指定预加载模型（RETRO_PRELOAD=none）—— 只提供状态查询。", flush=True)
    else:
        host.state = "loading"
        host.phase = "准备加载"
        _write_state(note="即将开始加载")
        print(f"[model] 开始加载「{key}」（首次约 40~90 秒）……", flush=True)

        def _on_phase(name: str, text: str) -> None:
            print(f"[model]   · {text}", flush=True)
            _write_state()

        try:
            host.load(key, on_phase=_on_phase)
            _write_state()
            print(
                f"[model] ✓ 模型「{key}」已就绪，用时 {host.load_seconds}s"
                "（规则子进程已起）",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            host.state = "failed"
            host.error = f"{type(e).__name__}: {e}"
            _write_state(traceback=traceback.format_exc())
            print(f"[model] ✕ 加载失败：{host.error}", flush=True)
            traceback.print_exc()

    print(f"  状态接口: http://127.0.0.1:{PORT}/status", flush=True)
    print("=" * 68, flush=True)

    # 走到这里才起 Flask —— 此时加载已完成，spawn 不再发生，多线程安全。
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    from multiprocessing import freeze_support

    # 🔴 必须：模型内部 spawn 规则子进程时，子进程会 re-import 本文件
    #    （作为 __mp_main__）。freeze_support + 下面的 __main__ 守卫一起，
    #    保证子进程不会又去加载一遍模型、又去起一个 Flask。
    freeze_support()
    main()
