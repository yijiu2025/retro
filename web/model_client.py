# -*- coding: utf-8 -*-
"""模型服务客户端 —— API 进程访问「常驻模型服务」的唯一入口。

分工（详见 web/model_server.py 顶部注释）：

    web/app.py          API 服务  :8000   秒起；本模块让它**不加载任何模型**
    web/model_server.py 模型服务  :8001   常驻；真正加载模型并推理

本模块做四件事：

    1. **探测**：模型服务活着吗？在加载吗？就绪吗？
       - HTTP `GET /status` 能通  → 实时状态，权威
       - 不通 → 读 state 文件（加载期间模型服务还没起 HTTP，只能靠它）
    2. **拉起**：不在跑就 `subprocess.Popen` 起一份（**独立控制台窗口**，日志可见）
    3. **复用**：API 重启后发现模型服务还活着 → 直接复用 —— 模型不重载
    4. **转接**：`predict()` / `search()` 把请求 HTTP 转发过去

🔴 为什么用 `subprocess.Popen` 而不是 `multiprocessing.Process`：
   模型服务内部要 spawn 规则子进程，而 Windows 的 spawn 在**多线程进程**里会死锁。
   `Popen` 走 CreateProcess，不经过 multiprocessing 的 re-import 机制，
   在有多线程的 API 进程里调用是安全的。

🔴 为什么用「新控制台窗口」而不是隐藏窗口：
   模型加载要 40~90 秒，用户需要看得见它在动（否则只能看到前端一个转圈）。
   新控制台还顺带把 Ctrl+C 隔离了 —— 关 API 窗口不会误杀模型服务，这正是
   「重启 API 不重载模型」能成立的前提。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)  # 仓库根

#: 判定「state 文件是否新鲜」的窗口：超过它就当模型服务已经不在加载了。
#: 首次加载可能十几分钟（冷盘 + 解压），给足余量，宁可多报一会儿 loading。
STALE_AFTER = float(os.environ.get("RETRO_MODEL_STALE_AFTER", "3600"))


def pid_alive(pid: int) -> bool:
    """进程是否还活着。

    🔴 为什么非要按 pid 判：`loading` 是**写在磁盘上**的，进程崩了文件还在。
       只看文件新鲜度的话（窗口 1 小时），模型服务崩溃后 API 会整整一小时都
       报「正在加载」，用户就一直等一个永远不会就绪的东西 —— 这是最坏的那种
       假状态：看起来在动，其实早死了。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False

# 🔴 本机回环地址必须**绕过系统代理**。
#    开发机上经常挂着全局代理（企业代理 / Clash / 抓包工具），会设 HTTP_PROXY。
#    urllib 默认尊重它，于是连 127.0.0.1:8001 的请求被发给代理 → 代理答 502，
#    而报错看起来像「模型服务没起来 / 连不上」，查半天查不到代理头上。
#    用空 ProxyHandler 强制直连；本服务只跟 127.0.0.1 说话，不需要任何代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(method: str, url: str, body=None, timeout: float = 0.6):
    """发一个 JSON 请求。失败抛异常（调用方决定怎么兜）。"""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _http_detail(e: urllib.error.HTTPError) -> str:
    """从 HTTPError 的 body 里抠出后端写的人话（没有就退回状态码说明）。"""
    try:
        return (json.loads(e.read().decode("utf-8")) or {}).get("error") or ""
    except Exception:  # noqa: BLE001
        return ""


class ModelServiceClient:
    """常驻模型服务的客户端。"""

    def __init__(self, port: int | None = None, state_file: str | None = None) -> None:
        self.port = int(port or os.environ.get("RETRO_MODEL_PORT", "8001"))
        self.base = f"http://127.0.0.1:{self.port}"
        self.state_file = state_file or os.environ.get("RETRO_MODEL_STATE_FILE") or os.path.join(
            PARENT, ".tmp-model-service", f"state-{self.port}.json"
        )
        self.server_script = os.path.join(HERE, "model_server.py")
        self._proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------
    # 状态探测
    # ------------------------------------------------------------------
    def _from_state_file(self) -> dict:
        """HTTP 不通时的回退：读模型服务写的 state 文件。

        加载期间模型服务还没起 HTTP（它必须先单线程加载完），
        所以「正在加载」这个状态只能从文件里读出来。
        """
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return {"state": "stopped", "source": "none"}

        age = time.time() - float(st.get("updated_at") or 0)
        fresh = age < STALE_AFTER
        st["source"] = "file"
        st["age"] = round(age, 1)
        pid = st.get("pid")
        if pid and not pid_alive(int(pid)):
            # 进程没了 —— 文件是残骸。别把它当成「还在加载」。
            st["state"] = "stopped"
            st["died"] = True
        elif not fresh:
            # 文件太旧：那份进程多半也早没了（崩溃后文件会留在磁盘上）
            st["state"] = "stopped"
            st["stale"] = True
        return st

    def status(self) -> dict:
        """模型服务当前状态。HTTP 优先，文件回退。"""
        try:
            st = _http_json("GET", f"{self.base}/status", timeout=0.6)
            st["source"] = "http"
            st["running"] = True
            return st
        except Exception:  # noqa: BLE001
            st = self._from_state_file()
            st["running"] = False
            return st

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _spawn(self, model_key: str) -> subprocess.Popen:
        """起一份模型服务进程（独立控制台窗口）。"""
        env = dict(os.environ)
        env["RETRO_MODEL_PORT"] = str(self.port)
        env["RETRO_MODEL_STATE_FILE"] = self.state_file
        if model_key:
            env["RETRO_PRELOAD"] = model_key
        else:
            env["RETRO_PRELOAD"] = "none"

        kwargs: dict = {"cwd": PARENT, "env": env, "close_fds": True}
        if os.name == "nt":
            # 新控制台：日志看得见，且 Ctrl+C / 关窗口不会波及 API 进程
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        return subprocess.Popen([sys.executable, self.server_script], **kwargs)

    def stop(self, timeout: float = 20.0) -> dict:
        """请模型服务优雅退出（顺带回收规则子进程）。"""
        try:
            _http_json("POST", f"{self.base}/shutdown", body={}, timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        # 等它把端口放掉（HTTP 不通 且 state 文件不再更新 ⇒ 已退）
        end = time.time() + timeout
        while time.time() < end:
            if not self.status().get("running"):
                return {"ok": True, "stopped": True}
            time.sleep(0.5)
        # 兜底：真的不退就直接杀
        try:
            _http_json("POST", f"{self.base}/shutdown", body={}, timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "stopped": True, "forced": True}

    def ensure(self, model_key: str = "", wait: float = 0.0) -> dict:
        """确保模型服务在跑；必要时拉起。返回当前状态。

        wait>0 时最多等这么久，等到 HTTP 能应答就提前返回（不是等模型加载完）。
        """
        st = self.status()
        if st.get("running"):
            return st

        # 文件说它正在加载（HTTP 还没起）⇒ 已经在起了，别再拉一份
        if st.get("state") == "loading" and st.get("source") == "file":
            return st

        self._proc = self._spawn(model_key)
        end = time.time() + wait
        while time.time() < end:
            st = self.status()
            if st.get("running") or st.get("state") == "loading":
                return st
            time.sleep(0.3)
        return self.status()

    def load(self, model_key: str, force: bool = False) -> dict:
        """把模型服务切到指定模型。

        🔴 「换模型」不是热切换，而是**重启模型服务**：加载模型必须在进程的
           单线程阶段完成（Windows spawn 死锁），而服务一起 HTTP 就有线程了。
           所以顺序是：① 若已加载该模型 → 直接返回；② 否则停掉旧进程，拉起新的。

        force=True：**即使已经在跑同一个模型也重新拉起**。
           🔴 这是 /api/model-service/restart 必须的语义。模型服务的代码一改
           （比如修 run_retro_search 的预算 bug）、或者 checkpoint 文件换了，
           复用旧进程就等于「改了没生效」—— 而这个 no-op 从返回体上完全看不出来
           （照样是 ok:true / already:true），排查时极容易被误读成「已经重启过了」。
        """
        st = self.status()
        cur = st.get("model") or ""
        if not force and st.get("running") and cur == model_key and st.get("state") in ("ready", "loading"):
            return {"ok": True, "already": True, **st}

        was_running = bool(st.get("running")) or st.get("state") == "loading"
        if was_running:
            self.stop()

        # 上一次的 state 文件会干扰「是否在加载」的判断，先清掉
        try:
            os.remove(self.state_file)
        except OSError:
            pass

        self._proc = self._spawn(model_key)
        # 等它写下 loading（说明进程真的起来了），最多 15 秒
        end = time.time() + 15.0
        while time.time() < end:
            st = self.status()
            if st.get("running") or st.get("state") == "loading":
                return {"ok": True, "restarted": True, **st}
            time.sleep(0.3)
        return {"ok": True, "restarted": True, **self.status()}

    def shutdown_if_owned(self) -> None:
        """API 退出时的收尾。默认**不动**模型服务（常驻复用的前提）。

        想让它跟着 API 一起退，设 RETRO_MODEL_STOP_WITH_API=1。
        """
        if os.environ.get("RETRO_MODEL_STOP_WITH_API", "").strip() in ("1", "true", "yes"):
            self.stop()

    # ------------------------------------------------------------------
    # 转接（推理 / 搜索）
    # ------------------------------------------------------------------
    def _post_json(self, path: str, body: dict, timeout: float) -> dict:
        """POST 一个 JSON，返回解析好的 dict。

        🔴 必须把 HTTPError 和 URLError **分开**：HTTPError 是服务**应答了**、
        只是状态码非 2xx（比如「模型未就绪」的 409），body 里带着人话；
        URLError 才是真的连不上。混在一起报「模型服务不可达」，就会把
        「模型还没加载完」说成「连不上」，用户于是去查端口、查进程，全查错方向。
        """
        try:
            out = _http_json("POST", f"{self.base}{path}", body=body, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(_http_detail(e)) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"模型服务不可达（{self.base}）—— 它可能没在运行：{e}"
            ) from e
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or "模型服务返回失败")
        return out

    # ------------------------------------------------------------------
    # 正向模型（「可行性过滤」的判据）
    # ------------------------------------------------------------------
    def load_forward(self, key: str = "", timeout: float = 600.0) -> dict:
        """请模型服务加载正向模型。**立刻返回受理结果**，进度看 /api/models。

        🔴 与 load() 关键区别：正向模型**不需要重启进程**（它构造时不 spawn 规则
           子进程 ⇒ 可以在 HTTP 起来之后热挂）。所以这里没有 stop/start 那套，
           也不存在"重启等于改了没生效"的坑。
        """
        try:
            out = _http_json(
                "POST", f"{self.base}/forward/load", body={"forward": key or ""}, timeout=timeout
            )
        except urllib.error.HTTPError as e:
            raise RuntimeError(_http_detail(e)) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"模型服务不可达（{self.base}）—— 它可能没在运行：{e}"
            ) from e
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or "加载正向模型失败")
        return out

    def unload_forward(self, timeout: float = 30.0) -> dict:
        """卸载正向模型（省内存）。"""
        try:
            out = _http_json("POST", f"{self.base}/forward/unload", body={}, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(_http_detail(e)) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"模型服务不可达（{self.base}）—— 它可能没在运行：{e}"
            ) from e
        return out

    def predict(self, smiles: str, num_results: int, timeout: float = 900.0) -> list:
        """单步预测。返回 [{reaction, probability}, ...]。"""
        out = self._post_json(
            "/predict", {"smiles": smiles, "num_results": num_results}, timeout
        )
        return out.get("results") or []

    def search(self, params: dict, timeout: float = 1800.0) -> dict:
        """多步搜索。返回 run_retro_search 的那个 dict。"""
        out = self._post_json("/search", params, timeout)
        return out.get("search") or {}

    # ------------------------------------------------------------------
    # 状态矩阵（给 /api/models 用）
    # ------------------------------------------------------------------
    def catalog(self, static_models: list, forward_static: list | None = None) -> dict:
        """把静态清单 + 服务实时状态合成一张状态矩阵。

        每个模型的状态取值：
            ready        已加载，可立即推理
            loading      正在加载（服务还没起 HTTP，靠 state 文件读到）
            failed       加载失败（error 给原因）
            idle         文件在，但没加载 —— 点一下就能后启动
            unavailable  模型文件缺失 —— 先放好文件

        forward_static：正向模型（可行性过滤判据）的静态清单，形状同 static_models。
        它**不是**搜索主模型，所以不混进 `models`（混进去前端就会把它渲染成一格
        可选的"预测模型"，选中它跑逆合成必然失败）；单独放在 `forward` 字段。
        """
        st = self.status()
        svc_state = str(st.get("state") or "stopped")
        cur = str(st.get("model") or "")
        if not st.get("running"):
            # HTTP 不通时，state 是从文件读来的，需要按「能不能用」重新归类：
            if svc_state == "failed":
                pass
            elif svc_state in ("loading", "ready"):
                # 🔴 含 ready：那一刻模型刚加载完、写完 state 文件，但 HTTP 还没
                #    开始监听（几百毫秒的窗口）。对外它**还不可用** —— 归到
                #    loading 而不是 stopped，免得前端闪一下「已停止」再变「就绪」。
                svc_state = "loading"
            else:
                svc_state = "stopped"

        rows = []
        for m in static_models:
            key = m["key"]
            if not m["exists"]:
                state = "unavailable"
            elif key == cur and svc_state == "loading":
                state = "loading"
            elif key == cur and svc_state == "ready":
                state = "ready"
            elif key == cur and svc_state == "failed":
                state = "failed"
            else:
                state = "idle"
            rows.append({**m, "state": state, "is_current": key == cur})

        forward = None
        if forward_static:
            fm = dict(forward_static[0])
            fsvc = st.get("forward") or {}
            f_own = str(fsvc.get("state") or "idle")
            f_key = str(fsvc.get("key") or "")
            if not fm["exists"]:
                f_state = "unavailable"
            elif not st.get("running") or (f_key and f_key != fm["key"]):
                # 🔴 服务没在跑（HTTP 不通）时内存里那份正向模型**用不上** ——
                #    搜索要经 HTTP 转接，这里如实报 idle，别给一个"已就绪"的假象。
                #    报的是另一个正向模型（换过）时同理。
                f_state = "idle"
            else:
                f_state = f_own if f_own in ("loading", "ready", "failed") else "idle"
            fm.update(
                {
                    "state": f_state,
                    "error": str(fsvc.get("error") or ""),
                    "load_seconds": fsvc.get("load_seconds"),
                }
            )
            forward = fm

        # 🔴 HTTP 还没起的那段窗口里，状态是从 **state 文件**读的，而文件里
        #    没有 `elapsed`（那是 ModelHost 现算的）。前端要显示「加载中（已 12s）」，
        #    这个秒数在这里补出来 —— 否则加载早期那几十秒界面上只有一句"加载中"，
        #    看不出到底是在走还是卡住了，恰恰是最需要它有数的那一段。
        elapsed = st.get("elapsed")
        if elapsed is None and svc_state == "loading":
            started = float(st.get("started_at") or 0)
            if started:
                elapsed = round(time.time() - started, 1)

        return {
            "ok": True,
            "service": {
                "running": bool(st.get("running")),
                "state": svc_state,
                "pid": st.get("pid"),
                "port": self.port,
                "phase": st.get("phase") or "",
                "error": st.get("error") or "",
                "elapsed": elapsed,
                "load_seconds": st.get("load_seconds"),
                "source": st.get("source"),
            },
            "current": cur,
            "models": rows,
            "forward": forward,
        }
