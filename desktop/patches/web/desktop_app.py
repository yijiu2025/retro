# -*- coding: utf-8 -*-
"""桌面编排入口 —— 被 `python39\\python.exe _internal\\desktop_app.py` 拉起。

职责（按顺序）：
  1. 装配 sys.path 与环境变量（数据目录、端口、桌面标记）
  2. 起 Flask（API + 前端静态托管）在**后台线程**
  3. 模型服务**懒加载**：不预起；有模型时 app 的自动启动逻辑会后台拉起，
     没有模型时等用户在「模型设置」页下载后点「加载」
  4. 开 pywebview 原生窗口（Edge WebView2，不走浏览器）
  5. 窗口关闭 → 停模型服务 → 退出

🔴 为什么 Flask 放线程、窗口留主线程：pywebview 的 GUI 消息循环必须在
   创建它的线程跑；而 app.run() 会阻塞。两者对调会直接卡死。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

# --------------------------------------------------------------------------
# 路径装配（全部相对本文件：_internal/desktop_app.py → 包根）
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))            # _internal
_PKG = os.path.dirname(_HERE)                                   # 包根

for _p in (
    os.path.join(_PKG, "python39", "Lib", "site-packages"),    # 依赖（含 CPU torch）
    _HERE,                                                      # web/ bootstrap/ scripts/
    os.path.join(_HERE, "web"),
    os.path.join(_HERE, "core"),
    os.path.join(_HERE, "scripts"),
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 上面的插入顺序会把 site-packages 顶到最前 —— 但 web/ 必须能覆盖同名的
# 开发中模块，且 core/ 要低于已安装版本（与 app.py 的口径一致）。重排一次：
sys.path[:] = [p for p in sys.path if p not in (_HERE, os.path.join(_HERE, "web"),
                                                os.path.join(_HERE, "core"),
                                                os.path.join(_HERE, "scripts"))]
sys.path[0:0] = [os.path.join(_HERE, "web"), _HERE,
                 os.path.join(_HERE, "core"), os.path.join(_HERE, "scripts")]

import config_store  # noqa: E402

config_store.ensure_dirs()

# --------------------------------------------------------------------------
# 环境变量默认值（真实环境变量优先，不覆盖用户显式设置）
# --------------------------------------------------------------------------
_ENV_DEFAULTS = {
    "RETRO_DESKTOP": "1",
    "RETRO_MODELS_DIR": config_store.models_dir(),
    "RETRO_MODEL_STATE_FILE": os.path.join(config_store.DATA_DIR, "model-service", "state.json"),
    "RETRO_OUTPUT_DIR": os.path.join(config_store.DATA_DIR, "outputs"),
    "RETRO_RULE_WORKERS": str(config_store.rule_workers()),
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)


def _free_port() -> int:
    """让 OS 挑一个空闲端口（避开 8000/8001 被别的程序占用的老问题）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


API_PORT = int(os.environ.get("RETRO_PORT") or 0) or _free_port()
MODEL_PORT = int(os.environ.get("RETRO_MODEL_PORT") or 0) or _free_port()
os.environ["RETRO_PORT"] = str(API_PORT)
os.environ["RETRO_MODEL_PORT"] = str(MODEL_PORT)

_LOG = os.path.join(config_store.DATA_DIR, "logs", "desktop.log")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _wait_api(timeout: float = 30.0) -> bool:
    import urllib.request

    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/models", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def main() -> None:
    _log(f"包根: {_PKG}")
    _log(f"API 端口: {API_PORT}  模型服务端口: {MODEL_PORT}")
    _log(f"模型目录: {config_store.models_dir()}  计算模式: {config_store.compute_mode()}")

    # app.py 以模块导入（它自带 sys.path 修正与 core/ 兜底）
    import app as api_app  # noqa: E402

    api_app._install_exit_hooks()

    # Flask 在后台线程跑；app.run 阻塞，绝不能占主线程（窗口要消息循环）
    t = threading.Thread(
        target=lambda: api_app.app.run(host="127.0.0.1", port=API_PORT,
                                       debug=False, threaded=True),
        name="flask", daemon=True,
    )
    t.start()
    if not _wait_api():
        _log("API 未在 30s 内就绪 —— 仍然尝试开窗（页面会显示连接失败）")

    # 有模型就让 app 的后台自动启动把模型服务拉起来；没有就跳过，
    # 等用户在「模型设置」页下载后点加载（_autostart_model_service 内部已做此判断）。
    api_app._autostart_model_service()

    headless = os.environ.get("RETRO_DESKTOP_HEADLESS") == "1"
    if headless:
        _log("RETRO_DESKTOP_HEADLESS=1 —— 不起窗口，仅跑服务（冒烟测试用）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    import webview  # noqa: E402  延迟导入：headless 模式不该被它拖累

    window = webview.create_window(
        "RetroWeb · 逆合成工作台",
        f"http://127.0.0.1:{API_PORT}",
        width=1480, height=920, min_size=(1024, 680),
    )
    _log("窗口已创建，进入 GUI 消息循环")
    webview.start()  # 阻塞直到用户关窗

    _log("窗口关闭，正在停止模型服务……")
    try:
        api_app._model_service.shutdown_if_owned()
    except Exception as e:  # noqa: BLE001
        _log(f"停模型服务失败（无碍）：{e}")
    _log("退出")


if __name__ == "__main__":
    main()
