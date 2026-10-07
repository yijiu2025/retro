# -*- coding: utf-8 -*-
"""RetroChimera 本地网页版 —— 逆合成预测 + 完整合成路径搜索

启动（在仓库根执行，用相对路径 —— 换盘符 / 换目录都不用改）:
    python web/app.py
    或双击仓库根目录的 启动后端API.bat

浏览器打开 http://127.0.0.1:8000   ← 只返回端点清单 JSON，不托管前端页面
"""
import os
import re
import sys

# --- 修正 DLL 搜索路径（必须在任何第三方 import 之前执行） -----------------
# Windows 上只有 conda 环境「已激活」时 <env>\Library\bin 才在 PATH 中；
# 直接双击 bat 或用绝对路径调用 python.exe 时 _ssl.pyd 找不到依赖的 OpenSSL DLL，
# 报 "DLL load failed while importing _ssl"。这里显式预加载解决。
_ENV_ROOT = os.path.dirname(os.path.abspath(sys.executable))
_BIN_DIR = os.path.join(_ENV_ROOT, "Library", "bin")
if os.path.isdir(_BIN_DIR):
    if _BIN_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _BIN_DIR + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(_BIN_DIR)
        except OSError:
            pass
    try:
        import ctypes

        for _name in ("libcrypto-1_1-x64.dll", "libssl-1_1-x64.dll"):
            _p = os.path.join(_BIN_DIR, _name)
            if os.path.exists(_p):
                ctypes.CDLL(_p)
    except Exception:  # noqa: BLE001
        pass

import threading
import time
import traceback
import queue
import concurrent.futures

from flask import Flask, Response, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(HERE)  # 仓库根

# --- 上游源码（core/）挂载兜底 ---------------------------------------------
# retrochimera 源码随本仓一起分发在 <仓库根>/core/。正常情况已装好
# （`pip install -e core`，见 README 3.2）；这里再兜一层，避免换机器时
# 忘记装却只报一句 "ModuleNotFoundError" 看不出缘由。
# 放在 sys.path **末尾** —— 优先级低于任何已安装版本，不会覆盖显式安装。
_CORE_DIR = os.path.join(_PARENT, "core")
if os.path.isdir(_CORE_DIR) and _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

# --- 限制规则应用子进程数（必须在导入 retrochimera / syntheseus 之前） -------
# 实现见 bootstrap/limit_workers.py：把 syntheseus 的 cpu_count() 固定为 4
# （可用环境变量 RETRO_RULE_WORKERS 覆盖）。
# 不限制的话 RuleApplicationServer 按 os.cpu_count()（本机 16）开子进程，
# 每个子进程加载完整规则库约 4.4 GB → 合计约 70 GB，会把页面文件打满并报
# "WinError 1455 页面文件太小，无法完成操作"。
try:
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    from bootstrap import limit_workers  # noqa: F401
except Exception as _e:  # noqa: BLE001
    print(f"[app] 注意：未能限制规则子进程数（{_e}）")

# --- 模型目录：**默认按相对位置推导，可用环境变量覆盖** --------------------
# 🔴 曾经写死 `F:\retrochimera\models\...` ⇒ 换台机器/换盘符就直接崩，
#    而且报错是"找不到模型文件"，看不出是路径问题。
#    现在的解析顺序（先到先用）：
#      ① 环境变量 RETRO_MODELS_DIR      （整个模型根目录，推荐：换机只改这一处）
#      ② 各模型单独的 RETRO_MODEL_<KEY> （只想挪某一个时用）
#      ③ <仓库根>/models/<key>           （默认：web/ 的上一层）
_MODELS_ROOT = os.environ.get("RETRO_MODELS_DIR") or os.path.join(
    os.path.dirname(HERE), "models"
)


def _model_path(env_key: str, dirname: str) -> str:
    """单个模型的目录：环境变量优先，否则 <模型根>/<dirname>。"""
    return os.environ.get(env_key) or os.path.join(_MODELS_ROOT, dirname)


MODELS = {
    "pistachio": {
        "path": _model_path("RETRO_MODEL_PISTACHIO", "retrochimera_pistachio"),
        "label": "Pistachio（主推，性能最强）",
    },
    "uspto50k": {
        "path": _model_path("RETRO_MODEL_USPTO50K", "retrochimera_uspto50k"),
        "label": "USPTO-50K（基准模型，较弱）",
    },
}

app = Flask(__name__, static_folder=None)
_lock = threading.Lock()
_loaded: dict = {}

# --- CORS：仅当配置了 RETRO_CORS_ORIGINS 时才启用 -------------------------
# 🔴 dev 走 vite proxy（同源），不需要 CORS；但**换机部署时前端往往在另一个域名**，
#    没有 CORS 的话浏览器会拦掉所有请求，而报错只说 "Failed to fetch"，
#    根本看不出是 CORS —— 这是部署时最容易浪费时间的坑。
#    配置示例：RETRO_CORS_ORIGINS=https://retro.example.com,http://localhost:5177
_CORS_ORIGINS = [
    o.strip()
    for o in (os.environ.get("RETRO_CORS_ORIGINS") or "").split(",")
    if o.strip()
]
_CORS_ALLOW_ALL = "*" in _CORS_ORIGINS


@app.after_request
def _apply_cors(resp):
    """给所有响应补 CORS 头（未配置则完全不动，行为与从前一致）。"""
    if not _CORS_ORIGINS:
        return resp
    origin = request.headers.get("Origin") or ""
    if _CORS_ALLOW_ALL:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in _CORS_ORIGINS:
        # 回显具体 origin（而不是 *），这样才允许带凭据的请求
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@app.route("/api/<path:_sub>", methods=["OPTIONS"])
@app.route("/api", methods=["OPTIONS"])
def _cors_preflight(_sub: str = ""):
    """预检请求：after_request 已经补好头，这里返回 204 即可。"""
    return ("", 204)

# --- 独立模型 worker 进程（单线程，彻底绕开 Windows spawn 死锁） ---
# 🔴 为什么必须另起一个进程：
#    RetroChimera 用 multiprocessing 起规则子进程，而 Windows 的 spawn 在
#    「已有其他线程存活的进程」里会死锁（子进程 re-import 主模块时卡死）。
#    Flask 一跑起来就有请求线程，所以**绝不能在本进程里 spawn**。
#    解决办法：模型放在一个**独立、单线程**的 worker 进程里（它自己启动时
#    单线程 spawn 规则子进程，安全），Flask 只通过队列把请求递过去、拿结果回来。
_REQ_Q = None
_RESP_Q = None
_WORKER_READY = threading.Event()
_WORKER_FAILED = False

# --- 任务中心：把长任务变成可查询、可取消、带阶段与日志的 job -----------------
# 详见 web/jobhub.py。此处只 import 并绑定模型缓存引用；runner 需要 get_model 与
# validate_target_smiles，这两个在文件后面才定义 ⇒ 真正装配放到文件末尾的
# _setup_jobhub()（由 /api/jobs 首次请求时触发，也在启动时预调用一次）。
try:
    import jobhub

    jobhub.bind_loaded_cache(_loaded)
except Exception as _e:  # noqa: BLE001
    jobhub = None  # type: ignore[assignment]
    print(f"[app] 注意：任务中心未启用（{_e}），/api/predict 仍可用但无进度", flush=True)

_PREDICT_RUNNER = None

# --- 大模型工艺分析（投料顺序 / 当量 / 条件 / 副产物 / LCMS / NMR） -------------
# 走独立的 OpenAI 兼容客户端，与模型推理链路完全解耦：不占主线程 pump、不 spawn 子进程。
try:
    import llm

    print(f"[app] 大模型分析：{'已配置' if llm.is_configured() else '未配置（.env 里填 RETRO_LLM_*）'}", flush=True)
except Exception as _e:  # noqa: BLE001
    llm = None  # type: ignore[assignment]
    print(f"[app] 注意：大模型分析模块未启用（{_e}）", flush=True)


def _setup_jobhub():
    """装配预测 runner（幂等）。"""
    global _PREDICT_RUNNER
    if jobhub is None:
        return None
    if _PREDICT_RUNNER is None:
        _PREDICT_RUNNER = jobhub.make_predict_runner(
            validate_target_smiles=validate_target_smiles,
            predict_fn=predict_remote,
            model_keys=set(MODELS.keys()),
        )
    return _PREDICT_RUNNER


def get_model(key: str):
    """按需加载并缓存模型。"""
    if key in _loaded:
        return _loaded[key]
    with _lock:
        if key in _loaded:
            return _loaded[key]
        from retrochimera import RetroChimeraModel

        info = MODELS.get(key)
        if info is None:
            raise ValueError(f"未知模型: {key}")
        model = RetroChimeraModel(model_dir=info["path"])
        _loaded[key] = model
        return model


# --------------------------------------------------------------------------
# 独立模型 worker 进程：只做一件事 —— 加载模型 + 在队列上处理推理请求。
# 它自身是「单线程进程」，启动时 spawn 规则子进程是安全的；推理时复用已起的
# 子进程，不再 spawn。Flask 主进程通过下面两个队列与它通信，永不自己 spawn。
# --------------------------------------------------------------------------
def model_worker_main(req_q, resp_q, model_key: str) -> None:
    """worker 进程的入口（在独立进程的主线程里运行）。

    🔴 注意：本函数由 multiprocessing 在子进程里调用，子进程会先 re-import
    本模块（app.py），顶部的 DLL 修复会再次执行，因此子进程能正常 import ssl。
    """
    import traceback as _tb

    try:
        from retrochimera import RetroChimeraModel
        from syntheseus import Molecule
    except Exception as e:  # noqa: BLE001
        resp_q.put({"type": "ready", "error": f"{type(e).__name__}: {e}"})
        return

    info = MODELS.get(model_key)
    if info is None:
        resp_q.put({"type": "ready", "error": f"未知模型: {model_key}"})
        return

    try:
        print(f"[worker] 加载模型「{model_key}」……", flush=True)
        model = RetroChimeraModel(model_dir=info["path"])
        print(f"[worker] ✓ 模型「{model_key}」已加载，规则子进程就绪", flush=True)
    except Exception as e:  # noqa: BLE001
        resp_q.put(
            {
                "type": "ready",
                "error": f"模型加载失败: {type(e).__name__}: {e}",
                "traceback": _tb.format_exc(),
            }
        )
        return

    # 加载成功：先回一个 ready，再进入请求循环
    resp_q.put({"type": "ready"})

    while True:
        req = req_q.get()
        if req is None:
            break
        try:
            preds = model([Molecule(req["smiles"])], num_results=req["num_results"])
            # 🔴 str(p) 是**反应 SMILES**（reactants>>product），不是纯前体；
            #    反应物侧的拆解在 jobhub 的 runner 里做（reaction -> precursor_list）。
            out = [
                {"reaction": str(p), "probability": float(p.metadata["probability"])}
                for p in preds[0]
            ]
            resp_q.put({"type": "result", "id": req["id"], "ok": True, "results": out})
        except Exception as e:  # noqa: BLE001
            resp_q.put(
                {
                    "type": "result",
                    "id": req["id"],
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": _tb.format_exc(),
                }
            )


def predict_remote(smiles: str, model_key: str, num_results: int) -> list:
    """推理桥：把请求丢进队列，阻塞等 worker 进程的结果回来。

    返回 worker 给的纯数据列表：[{precursors, probability}, ...]。
    本函数运行在 Flask 主线程的 job pump 里，只做 IPC，绝不 spawn 子进程。
    """
    global _WORKER_FAILED
    if _WORKER_FAILED or _REQ_Q is None or _RESP_Q is None:
        raise RuntimeError("模型 worker 进程未就绪（可能启动失败或已崩溃）")
    import uuid

    rid = uuid.uuid4().hex
    _REQ_Q.put({"id": rid, "smiles": smiles, "model": model_key, "num_results": num_results})
    while True:
        try:
            resp = _RESP_Q.get(timeout=float(os.environ.get("RETRO_WORKER_TIMEOUT", "900")))
        except Exception:  # noqa: BLE001
            raise RuntimeError("模型 worker 进程无响应（可能已崩溃）")
        if resp.get("type") == "ready":
            # 理论上 ready 已被 main() 消费；此处兜底处理
            if resp.get("error"):
                _WORKER_FAILED = True
                raise RuntimeError("模型 worker 启动失败：" + resp["error"])
            _WORKER_READY.set()
            continue
        if resp.get("id") == rid:
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "模型推理失败"))
            return resp["results"]


# --------------------------------------------------------------------------
# 退出清理：安静地关掉规则应用子进程
# --------------------------------------------------------------------------
# 背景：RetroChimera 的 RuleApplicationServer 会 spawn 若干子进程（每个约 4.4 GB），
# 官方在 __init__ 里注册了 weakref.finalize 做清理，但 **Ctrl+C 或直接关窗口时
# finalize 来不及执行** → 子进程变成孤儿、并在控制台刷出满屏
# "BrokenPipeError: [WinError 109] 管道已结束" 的 traceback（看着像崩溃，其实无害）。
# 这里显式注册退出钩子，主动调用 server.close()，让退出过程安静、不留残留进程。
def _iter_rule_servers(obj, seen=None, depth=0):
    """在模型对象里找出所有 RuleApplicationServer 实例。

    实测结构（retrochimera 1.3.0）：
        RetroChimeraModel
          └─ _models: list[BackwardReactionModel]
               ├─ TemplateClassificationModel._server = RuleApplicationServer
               └─ TemplateLocalizationModel._server   = RuleApplicationServer
    所以必须同时遍历「属性」和「容器元素」，否则一个都找不到。
    """
    if seen is None:
        seen = set()
    if obj is None or depth > 7 or id(obj) in seen:
        return
    seen.add(id(obj))

    # 命中目标
    if type(obj).__name__ == "RuleApplicationServer":
        yield obj
        return

    # 容器：list / tuple / set / dict value
    if isinstance(obj, dict):
        for v in list(obj.values()):
            yield from _iter_rule_servers(v, seen, depth + 1)
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for v in list(obj):
            yield from _iter_rule_servers(v, seen, depth + 1)
        return

    # 只下潜到自定义对象，避免遍历 torch/numpy 的大对象图
    mod = type(obj).__module__ or ""
    if mod.startswith(("torch", "numpy", "rdkit", "syntheseus")):
        return

    # 1) 已知属性名
    for attr in ("_server", "_rule_model", "_reaction_model", "model", "_model", "_models"):
        try:
            child = getattr(obj, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if child is not None:
            yield from _iter_rule_servers(child, seen, depth + 1)

    # 2) 兜底：按属性名模糊扫描（属性名单可能随版本变化）
    try:
        attrs = vars(obj)
    except Exception:  # noqa: BLE001
        attrs = {}
    for name, child in list(attrs.items()):
        if name.startswith("__") or child is None:
            continue
        low = name.lower()
        if ("server" in low or "rule" in low) and name not in (
            "_server", "_rule_model", "_reaction_model", "model", "_model", "_models",
        ):
            yield from _iter_rule_servers(child, seen, depth + 1)


def _shutdown_workers():
    """退出时关闭所有规则子进程（幂等、绝不抛异常）。"""
    closed = 0
    for model in list(_loaded.values()):
        for server in _iter_rule_servers(model):
            try:
                if getattr(server, "_running", False):
                    server.close()
                    closed += 1
            except Exception:  # noqa: BLE001
                pass
    if closed:
        print(f"[app] 已关闭 {closed} 组规则子进程，正常退出", flush=True)


def _stop_worker(worker):
    """优雅关闭模型 worker 进程（非 daemon，需显式回收）。

    - 先往请求队列丢一个 None，让 worker 的 `while True` 循环 break，
      触发 RuleApplicationServer 的 finalize 自动杀掉规则子进程；
    - join 等待（最多 30s，留给正在跑的推理收尾）；
    - 仍未退出（如推理卡死）则 terminate 强杀。
    """
    if worker is None:
        return
    try:
        if _REQ_Q is not None:
            _REQ_Q.put(None)
    except Exception:  # noqa: BLE001
        pass
    try:
        worker.join(timeout=30)
    except Exception:  # noqa: BLE001
        pass
    if worker.is_alive():
        try:
            worker.terminate()
        except Exception:  # noqa: BLE001
            pass


def _install_exit_hooks():
    import atexit
    import signal

    atexit.register(_shutdown_workers)

    # Ctrl+C：先清子进程，再让默认 KeyboardInterrupt 流程走完
    def _sigint(signum, frame):
        _shutdown_workers()
        # 恢复默认处理，第二次 Ctrl+C 可强制退出
        try:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        except Exception:  # noqa: BLE001
            pass
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:  # noqa: BLE001
        pass

    # Windows：直接点窗口右上角 × 关闭时，用 SetConsoleCtrlHandler 接管，
    # 先回收规则子进程再退出（否则子进程会变孤儿并刷满屏 BrokenPipeError）。
    try:
        import ctypes
        from ctypes import wintypes

        HANDLER_ROUTINE = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.DWORD
        )

        def _console_handler(ctrl_type):
            # 0=CTRL_C, 1=CTRL_BREAK, 2=CTRL_CLOSE, 5=CTRL_LOGOFF, 6=CTRL_SHUTDOWN
            try:
                _shutdown_workers()
            except Exception:  # noqa: BLE001
                pass
            return False  # 返回 False → 继续执行默认处理，进程正常退出

        # 必须保存引用，否则回调会被 GC 回收导致崩溃
        global _CTRL_HANDLER
        _CTRL_HANDLER = HANDLER_ROUTINE(_console_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 库存策略：决定搜索何时终止（哪些分子视为「可购买的起始物料」）
# --------------------------------------------------------------------------
_canon_cache: dict = {}


def canon_smiles(smi):
    """把 SMILES 规范化成 RDKit canonical 形式；无法解析返回 None。

    为什么必须规范化：搜索过程中模型返回的分子写法，和用户手工输入/从
    ChemDraw 复制的写法经常不同（原子顺序、等价分支的书写顺序都可能不一样），
    直接做字符串比较会导致「指定的起始物料」几乎永远匹配不上，白名单形同虚设。
    """
    if not smi:
        return None
    if smi in _canon_cache:
        return _canon_cache[smi]
    try:
        from rdkit import Chem

        mol = Chem.MolFromSmiles(smi)
        result = Chem.MolToSmiles(mol) if mol is not None else None
    except Exception:  # noqa: BLE001
        result = None
    _canon_cache[smi] = result
    return result


def make_inventory(max_heavy_atoms=10, purchasable=None):
    """构造分子库存判定器。

    max_heavy_atoms: 重原子数不超过该值的分子视为「可购买」。
    purchasable:     额外指定的可购买分子 SMILES 列表。命中即视为可购买
                     （搜索**不再往下拆它**），与上面的重原子规则是「或」关系。
                     比较用 canonical SMILES，因此写法不同也能正确匹配。
    """
    from rdkit import Chem
    from syntheseus.search.mol_inventory import BaseMolInventory

    extra = {c for c in (canon_smiles(s) for s in (purchasable or [])) if c}

    class HeuristicInventory(BaseMolInventory):
        def is_purchasable(self, mol) -> bool:
            if extra:
                canon = canon_smiles(mol.smiles)
                if canon is not None and canon in extra:
                    return True
            try:
                m = Chem.MolFromSmiles(mol.smiles)
                return m is not None and m.GetNumHeavyAtoms() <= max_heavy_atoms
            except Exception:  # noqa: BLE001
                return False

    return HeuristicInventory()


def make_value_function(kind, inventory, max_heavy_atoms):
    """构造 Retro* 的 value function：估计「从该分子出发还需要几步」。

    - "constant"   : 恒为 0。这是 syntheseus 的最简默认值，效果等于关闭启发式
                     引导，搜索退化为纯粹按反应概率（-logP）做 best-first。
    - "complexity" : 按重原子数估计剩余步数——分子越大，通常越需要多拆几步。
                     搜索因此会优先处理「已经接近可购买」的分支，在预算有限时
                     更容易把某一条分支一路走通，而不是把预算平摊在所有分支上。

    该值是「代价估计」，越大表示越不值得优先扩展。启发式以 1 步为下界
    （任何不可购买的分子至少还要 1 步），不会低估，因此不会牺牲找到可行路线的能力。
    """
    from syntheseus.search.node_evaluation.common import ConstantNodeEvaluator

    if kind != "complexity":
        return ConstantNodeEvaluator(0.0)

    from rdkit import Chem
    from syntheseus.search.graph.and_or import OrNode
    from syntheseus.search.node_evaluation.base import NoCacheNodeEvaluator

    atoms_per_step = 5.0  # 每多 5 个重原子，估计多需要 1 步

    class ComplexityValueFunction(NoCacheNodeEvaluator[OrNode]):
        def _evaluate_nodes(self, nodes, graph=None):
            values = []
            for node in nodes:
                mol = getattr(node, "mol", None)
                if mol is None:
                    values.append(0.0)
                    continue
                try:
                    if inventory.is_purchasable(mol):
                        values.append(0.0)
                        continue
                    rd_mol = Chem.MolFromSmiles(mol.smiles)
                    n_atoms = (
                        rd_mol.GetNumHeavyAtoms() if rd_mol is not None else max_heavy_atoms
                    )
                except Exception:  # noqa: BLE001
                    values.append(1.0)
                    continue
                values.append(1.0 + max(0, n_atoms - max_heavy_atoms) / atoms_per_step)
            return values

    return ComplexityValueFunction()


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
# 🔴 本服务**只提供 API，不托管任何前端页面**（前端在 retroweb 仓库）。
#    访问根路径给一份自描述，方便浏览器直接打开时知道这是哪儿、有哪些接口。
_API_INDEX = {
    "service": "RetroChimera API",
    "hint": "本服务只提供 API。前端界面在 retroweb 仓库，由它来调用这些接口。",
    "endpoints": [
        {"method": "GET", "path": "/api/models", "desc": "可用模型 + 是否已加载"},
        {"method": "POST", "path": "/api/load", "desc": "预热：提前把模型加载进内存"},
        {"method": "POST", "path": "/api/predict", "desc": "单步逆合成预测"},
        {"method": "POST", "path": "/api/search", "desc": "多步路线搜索（脚本/调试用）"},
        {"method": "POST", "path": "/api/parse", "desc": "结构文本 → 规范结构 + 分子式/分子量"},
        {"method": "POST", "path": "/api/depict", "desc": "SMILES → 带 2D 坐标的 molfile"},
        {"method": "GET", "path": "/api/jobs", "desc": "任务列表"},
        {"method": "POST", "path": "/api/jobs", "desc": "提交长任务"},
        {"method": "GET", "path": "/api/jobs/<job_id>", "desc": "查任务状态 / 取结果"},
        {"method": "POST", "path": "/api/jobs/<job_id>/cancel", "desc": "取消任务"},
        {"method": "GET", "path": "/api/llm/status", "desc": "大模型配置状态（?probe=1 真实验证）"},
        {"method": "GET", "path": "/api/llm/models", "desc": "端点配置下可见的模型（可指定配置 / 草稿）"},
        {"method": "GET", "path": "/api/llm/settings", "desc": "端点配置列表 + 当前配置（key 脱敏）"},
        {"method": "POST", "path": "/api/llm/settings", "desc": "保存端点配置（多套绑定，立即生效）"},
        {"method": "POST", "path": "/api/llm/settings/active", "desc": "切换当前使用的端点配置（无需保存）"},
        {"method": "POST", "path": "/api/llm/keys/reset", "desc": "重试失效 Key（可按配置清）"},
        {"method": "POST", "path": "/api/llm/analyze", "desc": "工艺分析（一次性返回）"},
        {"method": "POST", "path": "/api/llm/analyze/stream", "desc": "工艺分析（SSE 流式）"},
        {"method": "POST", "path": "/api/llm/conditions/stream", "desc": "候选反应条件枚举（SSE；先选条件再深入分析）"},
        {"method": "POST", "path": "/api/llm/followup/stream", "desc": "工艺分析追问（SSE，携带上下文自由回答）"},
    ],
}


@app.route("/")
def index():
    return jsonify(_API_INDEX)


@app.route("/api")
def api_index():
    return jsonify(_API_INDEX)


@app.route("/api/models")
def api_models():
    return jsonify(
        {
            "ok": True,
            "models": [
                {"key": k, "label": v["label"], "loaded": _WORKER_READY.is_set()}
                for k, v in MODELS.items()
            ],
        }
    )


# --------------------------------------------------------------------------
# 结构输入解析：CDXML / MOL / SDF / SMILES  ->  SMILES
# 交给 RDKit 处理（环境内已装 2023.09.6，原生支持 MolsFromCDXML）
# --------------------------------------------------------------------------
def _clean_smiles_text(txt: str) -> str:
    """从纯文本里取第一行非空、看起来像 SMILES 的内容。

    允许逗号出现——因为 ChemDraw 导出的 [F,Cl,Br,I] 这类通用取代基
    正需要被识别出来才能给出有针对性的诊断。
    只排除明显是中文说明/日志的内容。
    """
    smile_re = re.compile(r"^[A-Za-z0-9@+\-\[\]\(\)=#$:/\\%.*,;]+$")
    for line in txt.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if len(s) > 2000:
            return ""
        if smile_re.match(s):
            return s
        return ""
    return ""


def parse_structure(text: str):
    """返回 (ok, smiles, kind, n_atoms, error)。"""
    t = (text or "").strip()
    if not t:
        return False, "", "", 0, "内容为空"

    from rdkit import Chem

    # 1) CDXML —— ChemDraw 直接 Ctrl+C 复制出来的就是它
    if ("<CDXML" in t or "<cdxml" in t) or ("<n " in t and "<b " in t):
        # ChemDraw 的 CDXML 偶尔缺 BondLength，RDKit 会报
        # "No such node (<xmlattr>.BondLength)"，这里补一个默认值
        fixed = t
        if "BondLength" not in fixed:
            fixed = fixed.replace(
                "<CDXML", '<CDXML BondLength="14.4"', 1
            ) if "<CDXML" in fixed else fixed.replace(
                "<cdxml", '<cdxml bondLength="14.4"', 1
            )
        try:
            mols = list(Chem.MolsFromCDXML(fixed))
        except Exception as e:  # noqa: BLE001
            return False, "", "CDXML", 0, f"CDXML 解析失败：{e}"
        mols = [m for m in mols if m is not None and m.GetNumAtoms() > 0]
        if not mols:
            return False, "", "CDXML", 0, "CDXML 里没有可识别的原子"
        smis = [Chem.MolToSmiles(m) for m in mols]
        smis = [s for s in smis if s]
        if not smis:
            return False, "", "CDXML", 0, "CDXML 解析出了原子但无法成键"
        return True, ".".join(smis), "CDXML", sum(m.GetNumAtoms() for m in mols), ""

    # 2) MOL / SDF
    if "V2000" in t or "V3000" in t:
        try:
            mol = Chem.MolFromMolBlock(t, sanitize=True, removeHs=False)
            if mol is None:
                # 可能是多分子 SDF，逐块试
                blocks = t.split("$$$$")
                got = []
                for blk in blocks:
                    if not blk.strip():
                        continue
                    mm = Chem.MolFromMolBlock(blk, sanitize=True, removeHs=False)
                    if mm is not None:
                        got.append(Chem.MolToSmiles(mm))
                if got:
                    return True, ".".join(got), "MOL/SDF", 0, ""
                return False, "", "MOL/SDF", 0, "MOL/SDF 解析失败（RDKit 返回空）"
            return True, Chem.MolToSmiles(mol), "MOL/SDF", mol.GetNumAtoms(), ""
        except Exception as e:  # noqa: BLE001
            return False, "", "MOL/SDF", 0, f"MOL/SDF 解析失败：{e}"

    # 3) 纯 SMILES
    cand = _clean_smiles_text(t)
    if cand:
        # 反应式 SMILES（含 >）也接受
        if ">" in cand:
            parts = [p for p in cand.split(">")]
            good = []
            for p in parts:
                if not p:
                    continue
                for one in p.split("."):
                    if Chem.MolFromSmiles(one) is not None:
                        good.append(one)
            if good:
                return True, ".".join(good), "SMILES(反应式)", 0, ""
        mol = Chem.MolFromSmiles(cand)
        if mol is not None:
            # 带通配原子（*）的结构是 R 基团/通用结构，不能做逆合成
            if any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()):
                return (
                    False,
                    "",
                    "SMILES",
                    0,
                    "结构里含通配原子 *（R 基团/连接点），逆合成预测需要完整的确定分子。",
                )
            return True, Chem.MolToSmiles(mol), "SMILES", mol.GetNumAtoms(), ""
        return False, "", "SMILES", 0, "RDKit 无法解析这串 SMILES"

    return False, "", "", 0, "无法识别的内容格式"


def diagnose_smiles_failure(s: str):
    """给定一段解析失败的 SMILES，给出人话诊断 + 可能的修复候选。

    返回 dict: {reasons: [str], suggestions: [{label, smiles}], is_markush: bool}
    """
    from rdkit import Chem

    reasons = []
    is_markush = False

    # ① 通用取代基占位：[F,Cl,Br,I] / [*] / [R] 等 —— ChemDraw 的 R 基团
    combo = re.findall(r"\[([A-Za-z]{1,2}(?:,[A-Za-z]{1,2})+)\]", s)
    if combo:
        is_markush = True
        uniq = sorted(set(",".join(c.split(",")) for c in combo))
        for c in uniq:
            reasons.append(
                f"结构里有通用取代基「{c}」（ChemDraw 里写成 R 基团/多选一原子标签）。"
                f"SMILES 不允许方括号里出现逗号，RDKit 会把整串当成一个原子名而报错。"
            )

    # ② 星号占位
    if "[*" in s or "*" in s:
        is_markush = True
        reasons.append("结构里含 * 或 [*] 通配原子（通常是 R 基团或连接点）。")

    # ③ R# 标签（ChemDraw 常见 R1/R2）
    if re.search(r"\[R\d*\]|R\d(?![a-z])", s):
        is_markush = True
        reasons.append("结构里含 R1/R2 之类的可变基团标签。")

    # ④ 电荷/价位明显不对的提示（交给 RDKit 原始信息，这里只补充通用建议）
    if not reasons:
        reasons.append("RDKit 无法解析这串 SMILES，语法或化学价态有问题。")

    # ── 生成修复候选：把每个占位基团逐一替换成具体原子 ──
    suggestions = []
    if combo:
        # 取第一个出现的组合标签，生成 4 个候选（F / Cl / Br / I 或组合里的选项）
        first = combo[0]
        options = [x.strip() for x in first.split(",") if x.strip()]
        for opt in options[:4]:
            cand = re.sub(r"\[" + re.escape(first) + r"\]", opt, s)
            mol = None
            try:
                mol = Chem.MolFromSmiles(cand)
            except Exception:  # noqa: BLE001
                mol = None
            suggestions.append(
                {
                    "label": f"把所有「{first}」替换为 {opt}",
                    "smiles": cand,
                    "valid": mol is not None,
                    "canonical": Chem.MolToSmiles(mol) if mol is not None else "",
                }
            )

    # ── 也提供「把所有占位全部删掉/换成 H」的候选 ──
    if is_markush:
        stripped = s
        stripped = re.sub(r"\[[A-Za-z]{1,2}(?:,[A-Za-z]{1,2})+\]", "", stripped)
        stripped = re.sub(r"\[\*[^\]]*\]", "", stripped)
        stripped = re.sub(r"\[R\d*\]", "", stripped)
        try:
            m2 = Chem.MolFromSmiles(stripped)
            suggestions.append(
                {
                    "label": "删掉所有通用取代基（保留骨架）",
                    "smiles": stripped,
                    "valid": m2 is not None,
                    "canonical": Chem.MolToSmiles(m2) if m2 is not None else "",
                }
            )
        except Exception:  # noqa: BLE001
            pass

    return {"reasons": reasons, "suggestions": suggestions, "is_markush": is_markush}


@app.route("/api/parse", methods=["POST"])
def api_parse():
    """把用户粘贴/上传的结构（CDXML、MOL、SMILES）解析为规范 SMILES。

    请求体：{"text": "...", "kind": "auto|cdxml|mol|smiles"}
    响应：  {ok, smiles, kind, atoms, canonical_smiles, formula, mw, error}
    """
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"ok": False, "error": "没有收到内容"}), 400

    try:
        ok, smi, kind, natoms, err = parse_structure(text)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    if not ok:
        # 失败时给出人话诊断 + 可一键替换的候选结构
        payload = {"ok": False, "kind": kind, "error": err}
        try:
            cand_txt = _clean_smiles_text(text)
            if cand_txt:
                payload.update(diagnose_smiles_failure(cand_txt))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return jsonify(payload), 200

    # 附加一点信息，方便前端展示
    formula, mw = "", None
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors

        mol = Chem.MolFromSmiles(smi)
        if mol is not None:
            formula = rdMolDescriptors.CalcMolFormula(mol)
            mw = round(Descriptors.MolWt(mol), 2)
    except Exception:  # noqa: BLE001
        pass

    return jsonify(
        {
            "ok": True,
            "smiles": smi,
            "kind": kind,
            "atoms": natoms,
            "formula": formula,
            "mw": mw,
            "error": "",
        }
    )


@app.route("/api/depict", methods=["POST"])
def api_depict():
    """画板用：SMILES → 带 2D 坐标的 MOL(V2000) 文本。

    前端的结构式画板只负责「谁跟谁连」，坐标与芳香性交给 RDKit：
    画完的图以 molfile 形式回传 /api/parse 判真伪，载入已有结构时反过来
    从这里拿一份带坐标的 molfile 填进画板。

    请求体：{"smiles": "..."}
    响应：  {ok, molfile, smiles, error}
    """
    data = request.get_json(force=True, silent=True) or {}
    smi = data.get("smiles", "")
    if not isinstance(smi, str) or not smi.strip():
        return jsonify({"ok": False, "error": "没有收到 smiles"}), 400

    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem

        mol = Chem.MolFromSmiles(smi.strip())
        if mol is None:
            return jsonify({"ok": False, "error": "无法解析该 SMILES"}), 200
        # kekulize=False：保留芳香键（bond type 4），画板上画成芳香圈才好看
        AllChem.Compute2DCoords(mol)
        return jsonify(
            {
                "ok": True,
                "molfile": Chem.MolToMolBlock(mol, kekulize=False),
                "smiles": Chem.MolToSmiles(mol),
            }
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/load", methods=["POST"])
def api_load():
    """查询/触发模型就绪状态。

    🔴 模型现在跑在独立的 worker 进程里（见 model_worker_main），由 main() 启动时
    自动加载，Flask 本进程不再直接加载模型。这里只回报 worker 是否就绪。
    """
    return jsonify(
        {
            "ok": _WORKER_READY.is_set() and not _WORKER_FAILED,
            "loaded": _WORKER_READY.is_set(),
            "failed": _WORKER_FAILED,
        }
    )


# --------------------------------------------------------------------------
# 大模型工艺分析（llm.py）
#   给定一步反应（产物 + 前体 + 概率 + 路线上下文），输出结构化工艺分析：
#   投料顺序 / 当量 / 反应条件 / 预期结果与收率 / 可能的副产物 / LCMS / NMR。
#
#   🔴 与模型推理链路完全隔离：不经过 jobhub、不占主线程 pump、不 spawn 子进程，
#      所以「大模型在分析」不会阻塞「单步预测」的轮询。
# --------------------------------------------------------------------------
def _llm_common_payload(data: dict) -> dict:
    """从请求体里取出这一步反应上下文（产物 / 前体 / 概率 / 模型 / 路线上下文 / 勾选模块）。

    🔴 单独抽出来是为了让「深入分析」与「候选条件枚举」共用同一套解析 ——
       两条链路的上下文必须完全一致，否则枚举出的条件与后面分析的条件对不上。
    """
    precursors = data.get("precursors") or []
    if isinstance(precursors, str):
        precursors = [p for p in precursors.split(".") if p]
    # 前端勾选的分析模块（白名单过滤，未传 = 全量）
    modules = data.get("modules") or []
    if isinstance(modules, str):
        modules = [m for m in modules.split(",") if m]
    modules = [str(m).strip() for m in modules if str(m).strip()]
    return {
        "product": str(data.get("product") or "").strip(),
        "precursors": [str(p).strip() for p in precursors if str(p).strip()],
        "probability": data.get("probability"),
        "model": str(data.get("model") or ""),
        "context": data.get("context") or {},
        "continue_from": str(data.get("continue_from") or "").strip(),
        "modules": modules,
    }


def _llm_payload():
    """深入分析请求体：在公共上下文上补「用户选定的候选条件方案」。

    🔴 chosen_condition 必须**白名单 + 截断**后转发：它是前端从枚举结果里原样带回的对象，
       直接透传等于让请求体成为 prompt 的任意注入口（长度也不可控）。
    """
    data = request.get_json(force=True, silent=True) or {}
    req = _llm_common_payload(data)
    chosen = data.get("chosen_condition")
    chosen_out = None
    if isinstance(chosen, dict) and chosen:
        chosen_out = {}
        for key in (
            "id",
            "name",
            "strategy",
            "solvent",
            "temperature",
            "time",
            "atmosphere",
            "catalyst",
            "equiv",
            "concentration",
            "workup",
            "risk",
            "best_for",
        ):
            val = chosen.get(key)
            if val is not None and str(val).strip():
                chosen_out[key] = str(val).strip()[:400]
        for key in ("pros", "cons"):
            val = chosen.get(key)
            if isinstance(val, list):
                items = [str(x).strip()[:300] for x in val if str(x).strip()]
                if items:
                    chosen_out[key] = items[:8]
        if not chosen_out:
            chosen_out = None
    req["chosen_condition"] = chosen_out
    return req


def _llm_conditions_payload():
    """候选条件枚举请求体：公共上下文 + 想要几套方案（2~6，默认 4）。"""
    data = request.get_json(force=True, silent=True) or {}
    req = _llm_common_payload(data)
    try:
        count = int(data.get("count"))
    except (TypeError, ValueError):
        count = 4
    req["count"] = max(2, min(6, count))
    return req


def _llm_followup_payload():
    """追问请求体：这一步反应上下文 + 已有分析 + 历史问答 + 本次问题。

    🔴 追问要「携带上下文」，所以除了产物/前体/路线，还要把**上一次抽出的结构化分析**
       与**历史问答**一起带上 —— 否则模型每次都在答新问题，接不上前文。
    """
    data = request.get_json(force=True, silent=True) or {}
    precursors = data.get("precursors") or []
    if isinstance(precursors, str):
        precursors = [p for p in precursors.split(".") if p]
    history = []
    for m in data.get("history") or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content[:4000]})
    analysis = data.get("analysis")
    return {
        "product": str(data.get("product") or "").strip(),
        "precursors": [str(p).strip() for p in precursors if str(p).strip()],
        "probability": data.get("probability"),
        "model": str(data.get("model") or ""),
        "context": data.get("context") or {},
        "analysis": analysis if isinstance(analysis, dict) else None,
        "history": history,
        "question": str(data.get("question") or "").strip(),
    }


@app.route("/api/llm/status")
def api_llm_status():
    """大模型配置状态（脱敏，不回传 key）。前端据此决定「AI 分析」按钮是否可用。

    `?probe=1` 会真发一次最小请求，验证 **key + 模型** 是否可用（默认不做，避免每次挂载都打网络）。
    """
    if llm is None:
        return jsonify({"ok": True, "configured": False, "hint": "后端未启用大模型模块（llm.py 缺失或导入失败）"})
    st = llm.public_status()
    st["ok"] = True
    if (request.args.get("probe") or "").strip() in ("1", "true", "yes"):
        st["probe"] = llm.probe()
    return jsonify(st)


@app.route("/api/llm/models", methods=["GET", "POST"])
def api_llm_models():
    """列出这套端点配置的 key 在 /v1/models 里能看到的模型 id（仅列出，不代表套餐内可用）。

    默认用当前激活的配置；也可以指定：
      · `{profile_id: "<已保存的 id>"}` —— 拉那一套的；
      · `{profile: {id?, base_url, api_keys}}` —— **还没保存的草稿**：新建端点时要能先看看
        「这个地址 + 这个 key」下有哪些模型可选，否则只能盲填模型名。
    两者的 api_keys 元素含 `*` = 按脱敏串还原成真实 key（只在该配置自己的池里匹配）。
    """
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块", "models": []})
    sel: dict = {}
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        draft = data.get("profile") if isinstance(data.get("profile"), dict) else None
        if draft:
            sel = dict(draft)
        pid = str(data.get("profile_id") or "").strip()
        if pid:
            sel["id"] = pid
    else:
        pid = (request.args.get("profile_id") or "").strip()
        if pid:
            sel = {"id": pid}

    out = llm.list_models(sel or None)
    cur = str(sel.get("model") or "")
    if not cur and sel.get("id"):
        saved = next((p for p in llm.current_profiles()["profiles"] if p["id"] == sel["id"]), None)
        cur = str((saved or {}).get("model") or "")
    out["current"] = cur or llm.config().get("model")
    return jsonify(out)


@app.route("/api/llm/settings")
def api_llm_settings():
    """设置面板的当前值（key 脱敏）：接口地址 / 模型 / token 上限 / key 池状态。"""
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"})
    return jsonify(llm.get_settings())


@app.route("/api/llm/settings", methods=["POST"])
def api_llm_settings_save():
    """保存端点配置 → web/llm_settings.json（优先级高于 .env，立即生效），复位 key 轮换。

    提交 `{profiles: [...], active_profile: "<id>"}` 整包 —— 新建 / 修改 / 删除都走它。
    每套里的 `api_keys` 元素含 `*` = 按脱敏串保留**该套**原 key；明文 = 新增。
    返回保存后的设置 + 最新状态，前端一次拿全、不用再打 /llm/status。
    """
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"})
    payload = request.get_json(force=True, silent=True) or {}
    out = llm.save_settings(payload)
    out["status"] = llm.public_status()
    return jsonify(out)


@app.route("/api/llm/settings/active", methods=["POST"])
def api_llm_settings_active():
    """切换当前使用的端点配置（只改 active_profile，立即生效，不用点保存）。

    接口地址 / 模型 / Key 是**一套绑定**的：换供应商 = 换这一套。挨个字段手动改，
    漏一处就得到「看着配好了、一调就 401/404」的状态。
    """
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"})
    data = request.get_json(force=True, silent=True) or {}
    pid = str(data.get("id") or data.get("profile_id") or "").strip()
    out = llm.activate_profile(pid)
    out["status"] = llm.public_status()
    return jsonify(out)


@app.route("/api/llm/keys/reset", methods=["POST"])
def api_llm_keys_reset():
    """清空 key 的失效/冷却标记（「重试失效 Key」按钮）。

    带 `{profile_id}` 只清那一套（按钮作用于面板上当前编辑的配置）；不带 = 全清。
    """
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"})
    data = request.get_json(force=True, silent=True) or {}
    llm.reset_key_state(str(data.get("profile_id") or "").strip())
    return jsonify(llm.get_settings())


@app.route("/api/llm/analyze", methods=["POST"])
def api_llm_analyze():
    """非流式：一次拿到结构化分析（前端流被阻断时的兜底）。"""
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"}), 500
    req = _llm_payload()
    if not req["product"] and not req["precursors"]:
        return jsonify({"ok": False, "error": "缺少产物或前体信息"}), 400
    try:
        out = llm.analyze(req)
        return jsonify({"ok": True, **out})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 200


@app.route("/api/llm/analyze/stream", methods=["POST"])
def api_llm_analyze_stream():
    """流式（SSE）：LLM 逐 token 转发，结束时补一条 `done`（含抽好的 JSON + RDKit 质量表）。

    🔴 为什么用 POST + fetch 而不是 EventSource：EventSource 只能 GET、不能带 body，
        而这里必须把「产物 + 前体 + 路线上下文」当 body 传。前端用 fetch 读流。
    """
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"}), 500
    req = _llm_payload()
    if not req["product"] and not req["precursors"]:
        return jsonify({"ok": False, "error": "缺少产物或前体信息"}), 400

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",   # 反向代理别缓冲
        "Connection": "keep-alive",
    }
    return Response(
        llm.stream_analyze(req),
        mimetype="text/event-stream",
        headers=headers,
    )


@app.route("/api/llm/conditions/stream", methods=["POST"])
def api_llm_conditions_stream():
    """候选反应条件枚举（SSE）：先列出「这一步可以怎么做」的多套条件，再由用户挑一套深入分析。

    🔴 与 /api/llm/analyze/stream 的上下文**完全一致**（同一份 build_messages 正文），
       差别只在 system 提示与输出结构 —— 这条只要「条件方案」数组，不要机理 / 后处理细节。
       选定某套后，前端把它作为 `chosen_condition` 回传给 analyze/stream ⇒ 深入分析会锁定它。
    """
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"}), 500
    req = _llm_conditions_payload()
    if not req["product"] and not req["precursors"]:
        return jsonify({"ok": False, "error": "缺少产物或前体信息"}), 400

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(
        llm.stream_conditions(req),
        mimetype="text/event-stream",
        headers=headers,
    )


@app.route("/api/llm/followup/stream", methods=["POST"])
def api_llm_followup_stream():
    """工艺分析「追问」（SSE）：带着这一步反应的上下文与已有分析，回答用户的其他问题。

    🔴 与 /api/llm/analyze/stream 的关键差别：那条要求模型吐**结构化 JSON**（response_format=json_object），
       这条要的是**自由文本**回答 ⇒ 后端必须显式关掉 json 模式（见 llm.stream_followup）。
       否则模型会把答案塞进 JSON 字符串里，读起来全是转义符。
    """
    if llm is None:
        return jsonify({"ok": False, "error": "后端未启用大模型模块"}), 500
    req = _llm_followup_payload()
    if not req["question"]:
        return jsonify({"ok": False, "error": "缺少追问内容"}), 400

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(
        llm.stream_followup(req),
        mimetype="text/event-stream",
        headers=headers,
    )


def validate_target_smiles(smiles: str):
    """校验作为预测目标的目标分子 SMILES。

    返回 (ok, canonical_or_empty, err_payload_or_None)。
    err_payload 直接可 jsonify，包含人话原因与修复候选。
    """
    from rdkit import Chem

    s = (smiles or "").strip()
    if not s:
        return False, "", {"ok": False, "error": "请输入目标分子的结构或 SMILES"}

    mol = None
    try:
        mol = Chem.MolFromSmiles(s)
    except Exception as e:  # noqa: BLE001
        return False, "", {"ok": False, "error": f"分子校验出错：{e}"}

    if mol is None:
        payload = {"ok": False, "error": "这个 SMILES 无法被识别为合法分子"}
        try:
            payload.update(diagnose_smiles_failure(s))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return False, "", payload

    # 通配原子（R 基团）不能作为预测目标
    if any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()):
        payload = {
            "ok": False,
            "error": "结构里含通配原子 *（R 基团/连接点），逆合成预测需要完整的确定分子。",
        }
        try:
            payload.update(diagnose_smiles_failure(s))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return False, "", payload

    # 芳香性检查：能写出 SMILES 但无法 kekulize，说明该环的键级/杂原子画错了。
    # RDKit 在这种情况下会打印 "Can't kekulize mol"，但 MolFromSmiles 仍返回对象，
    # 后续模型内部再解析时就会失败 —— 提前拦截并给出可操作提示。
    try:
        Chem.Kekulize(Chem.Mol(mol), clearAromaticFlags=True)
    except Exception:  # noqa: BLE001
        payload = {
            "ok": False,
            "error": (
                "这个结构的芳香环无法确定键级（kekulize 失败），"
                "通常是环上的杂原子或双键画错了，模型无法处理。"
            ),
            "reasons": [
                "环上出现了 RDKit 无法分配单/双键的芳香体系",
                "常见原因：杂原子（N/O/S）价态不对，或该成双键的地方画成了芳香键",
                "如果是从 ChemDraw 粘贴：请检查苯环/杂环上的电荷、氢原子和双键位置",
            ],
            "suggestions": [],
            "is_markush": False,
        }
        return False, "", payload

    return True, Chem.MolToSmiles(mol), None


@app.route("/api/jobs", methods=["POST"])
def api_jobs_create():
    """创建一个后台预测任务，立即返回 job_id（不阻塞）。

    请求体：{"smiles": "...", "model": "pistachio", "num_results": 5}
    响应：  {"ok": true, "job_id": "..."}
    """
    if jobhub is None or _setup_jobhub() is None:
        return jsonify({"ok": False, "error": "任务中心不可用"}), 500
    data = request.get_json(force=True, silent=True) or {}
    if not str(data.get("smiles") or "").strip():
        return jsonify({"ok": False, "error": "请输入目标分子的结构或 SMILES"}), 400
    params = {
        "smiles": str(data.get("smiles")).strip(),
        "model": data.get("model", "pistachio"),
        "num_results": int(data.get("num_results", 5)),
    }
    job = jobhub.submit("predict", params, _PREDICT_RUNNER)
    return jsonify({"ok": True, "job_id": job.id})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_jobs_get(job_id):
    """查询任务快照。

    ?since=<seq> 只返回 seq 大于该值的日志行（增量拉取，避免重复传输）。
    """
    if jobhub is None:
        return jsonify({"ok": False, "error": "任务中心不可用"}), 500
    job = jobhub.get_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "任务不存在或已过期"}), 404
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    snap = job.snapshot(since=since)
    snap["ok"] = True
    return jsonify(snap)


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_jobs_cancel(job_id):
    """请求取消。⚠️ 只在阶段边界生效 —— Python 无法中断正在进行的模型加载/推理。"""
    if jobhub is None:
        return jsonify({"ok": False, "error": "任务中心不可用"}), 500
    ok = jobhub.cancel(job_id)
    return jsonify({"ok": ok, "message": "已请求取消" if ok else "任务已结束，无需取消"})


@app.route("/api/_threads")
def api_debug_threads():
    """🔴 临时诊断：导出所有线程栈，定位 GET /api/jobs 卡死原因（不碰 job._lock）。"""
    import sys as _sys
    import traceback as _tb

    frames = _sys._current_frames()
    out = {}
    for tid, frame in frames.items():
        try:
            out[str(tid)] = [_l.strip() for _l in _tb.format_stack(frame)[-14:]]
        except Exception:  # noqa: BLE001
            out[str(tid)] = ["<unavailable>"]
    return jsonify({"ok": True, "threads": out})


@app.route("/api/predict", methods=["POST"])
def api_predict():
    """单步逆合成预测（同步，兼容旧调用）。

    🔴 新前端请用 /api/jobs：这个接口会一直挂起到算完，模型首次加载可达十几分钟，
    前端只能干等看不到进度。此处内部复用 job，保证结果与异步路径完全一致。
    """
    if jobhub is not None and _setup_jobhub() is not None:
        data = request.get_json(force=True, silent=True) or {}
        smiles = (data.get("smiles") or "").strip()
        if not smiles:
            return jsonify({"ok": False, "error": "请输入目标分子的结构或 SMILES"}), 400
        params = {
            "smiles": smiles,
            "model": data.get("model", "pistachio"),
            "num_results": int(data.get("num_results", 5)),
        }
        job = jobhub.submit("predict", params, _PREDICT_RUNNER)
        jobhub.wait(job.id, timeout=float(os.environ.get("RETRO_SYNC_TIMEOUT", "1800")))
        if job.state == "succeeded":
            return jsonify(job.result)
        if job.state == "cancelled":
            return jsonify({"ok": False, "error": "任务已取消"}), 499
        payload = dict(job.error_payload or {"ok": False, "error": job.error or "预测失败"})
        return jsonify(payload), 400 if job.error_payload else 500

    # --- 兜底：任务中心不可用时的老路径 ---
    data = request.get_json(force=True, silent=True) or {}
    smiles = (data.get("smiles") or "").strip()
    model_key = data.get("model", "pistachio")
    num_results = int(data.get("num_results", 5))

    if not smiles:
        return jsonify({"ok": False, "error": "请输入目标分子的结构或 SMILES"}), 400
    num_results = max(1, min(num_results, 20))

    ok_smi, smiles, err = validate_target_smiles(smiles)
    if not ok_smi:
        return jsonify(err), 400

    try:
        from syntheseus import Molecule

        t0 = time.time()
        model = get_model(model_key)
        load_s = time.time() - t0

        t1 = time.time()
        preds = model([Molecule(smiles)], num_results=num_results)
        infer_s = time.time() - t1

        results = []
        for i, p in enumerate(preds[0], 1):
            prob = float(p.metadata["probability"])
            smi = str(p)
            results.append(
                {
                    "rank": i,
                    "precursors": smi,
                    "precursor_list": smi.split("."),
                    "probability": prob,
                    "num_molecules": len(smi.split(".")),
                    "level": "high" if prob > 0.5 else ("mid" if prob > 0.1 else "low"),
                }
            )

        return jsonify(
            {
                "ok": True,
                "product": smiles,
                "model": model_key,
                "results": results,
                "load_seconds": round(load_s, 1),
                "infer_seconds": round(infer_s, 1),
            }
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/search", methods=["POST"])
def api_search():
    """多步合成路径搜索：返回到达起始物料的完整合成路线。"""
    data = request.get_json(force=True, silent=True) or {}
    smiles = (data.get("smiles") or "").strip()
    model_key = data.get("model", "pistachio")
    max_routes = int(data.get("max_routes", 5))
    max_calls = int(data.get("max_calls", 60))
    time_limit = float(data.get("time_limit", 120))
    max_depth = int(data.get("max_depth", 3))
    max_heavy_atoms = int(data.get("max_heavy_atoms", 10))
    value_fn = str(data.get("value_fn") or "complexity").strip().lower()
    raw_purchasable = data.get("purchasable") or []
    raw_required = data.get("required_materials") or []

    if not smiles:
        return jsonify({"ok": False, "error": "请输入目标分子的 SMILES"}), 400

    # 参数保护
    max_routes = max(1, min(max_routes, 20))
    max_calls = max(5, min(max_calls, 400))
    time_limit = max(10.0, min(time_limit, 600.0))
    max_depth = max(1, min(max_depth, 8))
    max_heavy_atoms = max(1, min(max_heavy_atoms, 40))
    if value_fn not in ("constant", "complexity"):
        value_fn = "complexity"

    def _norm_mol_list(items):
        """规范化用户给的分子列表。支持字符串（逗号/空格/分号分隔）或数组。

        返回值统一为 canonical SMILES，保证后续与搜索里的分子做可靠比较。
        """
        if isinstance(items, str):
            items = re.split(r"[\s,;]+", items)
        good, bad = [], []
        for item in items or []:
            text = str(item).strip()
            if not text:
                continue
            canon = canon_smiles(text)
            if canon is None:
                bad.append(text)
            elif canon not in good:
                good.append(canon)
        return good, bad

    purchasable, bad_purchasable = _norm_mol_list(raw_purchasable)
    required, bad_required = _norm_mol_list(raw_required)
    if bad_purchasable or bad_required:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "指定的分子无法解析为合法结构："
                    + "、".join(bad_purchasable + bad_required),
                }
            ),
            400,
        )

    ok_smi, smiles, err = validate_target_smiles(smiles)
    if not ok_smi:
        return jsonify(err), 400

    try:
        from syntheseus import Molecule
        from syntheseus.search.algorithms.best_first.retro_star import RetroStarSearch
        from syntheseus.search.analysis.route_extraction import iter_routes_time_order
        from syntheseus.search.graph.and_or import AndNode, OrNode
        from syntheseus.search.node_evaluation.common import ReactionModelLogProbCost

        t0 = time.time()
        model = get_model(model_key)
        load_s = time.time() - t0

        inventory = make_inventory(max_heavy_atoms, purchasable)

        t1 = time.time()
        searcher = RetroStarSearch(
            reaction_model=model,
            mol_inventory=inventory,
            value_function=make_value_function(value_fn, inventory, max_heavy_atoms),
            and_node_cost_fn=ReactionModelLogProbCost(),
            limit_reaction_model_calls=max_calls,
            time_limit_s=time_limit,
            max_expansion_depth=max_depth,
        )
        graph, _ = searcher.run_from_mol(Molecule(smiles))
        search_s = time.time() - t1

        # ---- 提取路径 ----
        or_nodes = [n for n in graph.nodes() if isinstance(n, OrNode)]
        and_nodes = [n for n in graph.nodes() if isinstance(n, AndNode)]

        routes = []
        for nodes in iter_routes_time_order(graph, max_routes=max_routes):
            steps = []
            for n in nodes:
                if not isinstance(n, AndNode):
                    continue
                rxn = n.reaction
                try:
                    prob = rxn.metadata.get("probability")
                except Exception:  # noqa: BLE001
                    prob = None
                reactants = sorted(m.smiles for m in rxn.reactants)
                steps.append(
                    {
                        "product": rxn.product.smiles,
                        "reactants": reactants,
                        "reactants_joined": ".".join(reactants),
                        "probability": float(prob) if prob is not None else None,
                    }
                )
            if steps:
                probs = [s["probability"] for s in steps if s["probability"] is not None]
                # 路线总概率 = 各步概率之积（近似独立假设）
                total = 1.0
                for p in probs:
                    total *= p
                routes.append(
                    {
                        "steps": steps,
                        "num_steps": len(steps),
                        "total_probability": total if probs else None,
                        "material_count": sum(
                            1
                            for s in steps
                            for r in s["reactants"]
                            if not any(r == t["product"] for t in steps)
                        ),
                    }
                )

        # ---- 「路线必须包含指定原料」过滤 ----
        # 说明：这是搜索**完成后**的筛选，它无法改变搜索本身探索了哪些分支。
        # 如果搜索过程中根本没走到含该原料的分支，这里会把所有路线都过滤掉，
        # 此时应当配合「指定起始物料」白名单一起用。
        filtered_out = 0
        if required:
            required_set = set(required)
            kept = []
            for rt in routes:
                present = set()
                for step in rt["steps"]:
                    present.add(step["product"])
                    present.update(step["reactants"])
                present_canon = {c for c in (canon_smiles(x) for x in present) if c}
                if required_set.issubset(present_canon):
                    kept.append(rt)
            filtered_out = len(routes) - len(kept)
            routes = kept

        # 按总概率降序
        routes.sort(key=lambda r: (r["total_probability"] is None, -(r["total_probability"] or 0)))

        # 路线里出现的所有分子（用于标注起始物料）
        all_products = {s["product"] for r in routes for s in r["steps"]}
        starting_materials = set()
        for r in routes:
            for s in r["steps"]:
                for react in s["reactants"]:
                    if react not in all_products:
                        starting_materials.add(react)

        return jsonify(
            {
                "ok": True,
                "target": smiles,
                "model": model_key,
                "routes": routes,
                "starting_materials": sorted(starting_materials),
                "purchasable": sorted(purchasable),
                "required_materials": sorted(required),
                "filtered_out": filtered_out,
                "value_fn": value_fn,
                "stats": {
                    "or_nodes": len(or_nodes),
                    "and_nodes": len(and_nodes),
                    "expansions": len(and_nodes),
                    "max_heavy_atoms": max_heavy_atoms,
                },
                "load_seconds": round(load_s, 1),
                "search_seconds": round(search_s, 1),
            }
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


def _preload_model_in_main_thread() -> None:  # pragma: no cover - 已废弃
    """⚠️ 已废弃，保留仅为兼容旧调用点。

    历史：曾用「主线程预加载 + 热身推理」来绕开 Windows spawn 死锁。实测证明这不够 ——
    spawn 在**多线程进程**里同样会死锁（Flask 一跑起来就有 serve 线程）。
    现在模型整体搬进独立 worker 进程（见 `model_worker_main` / `predict_remote`），
    Flask 本进程永不 spawn，因此这个函数不再需要做任何事。
    真正的预加载发生在 `main()` 里启动 worker 进程的那一段。
    """
    return


class _MainThreadExecutor(concurrent.futures.Executor):
    """🔴 把任务调度回**主线程**执行的执行器（解决 Windows spawn 死锁）。

    用法：
        - Flask 在独立后台线程里 `app.run()`，照常接请求、照常把 job 提交进来；
        - 真正跑预测（会 spawn 规则子进程）的 `_run` 被放进队列，
          由**主线程**的 `pump()` 逐个取出执行 —— 所以 spawn 永远发生在主线程，
          不会触发「非主线程 spawn 死锁」。
        - 主线程同一时刻只跑一个 job（等价于 max_workers=1），与原来串行语义一致；
          GET /api/jobs 由 Flask 请求线程并发响应（只读 job 状态，不阻塞）。

    这样即便 `RETRO_PRELOAD=none` 关闭了启动预加载，首个 job 也会在主线程里
    加载模型（spawn 安全），而不是在 Flask 工作线程里卡死。
    """

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._shutdown_flag = False

    def submit(self, fn, *args, **kwargs):  # type: ignore[override]
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fut, fn, args, kwargs))
        return fut

    def pump(self) -> None:
        """主线程永远在这个循环里：取出任务、在主线程执行、回填结果。"""
        while True:
            item = self._q.get()
            if item is None:
                break
            fut, fn, args, kwargs = item
            try:
                fut.set_result(fn(*args, **kwargs))
            except Exception as e:  # noqa: BLE001
                fut.set_exception(e)

    def shutdown(self, wait: bool = False, cancel_futures: bool = False) -> None:  # type: ignore[override]
        self._q.put(None)


def main():
    global _REQ_Q, _RESP_Q, _WORKER_FAILED
    import multiprocessing

    port = int(os.environ.get("RETRO_PORT", "8000"))
    host = "127.0.0.1"
    print("=" * 68, flush=True)
    print("  RetroChimera 本地网页版 —— 逆合成预测 + 合成路径搜索", flush=True)
    print("=" * 68, flush=True)

    # 🔴 把任务的真正执行（会 spawn 规则子进程）调度回主线程，避免 Windows 死锁
    if jobhub is not None:
        _mt_executor = _MainThreadExecutor()
        jobhub.set_executor(_mt_executor)
    else:
        _mt_executor = None

    # 🔴 启动独立的单线程模型 worker 进程（在其中 spawn 规则子进程，安全）。
    #    Flask 本进程永不 spawn，彻底绕开「多线程进程里 spawn 死锁」。
    model_key = str(os.environ.get("RETRO_PRELOAD", "pistachio"))
    if model_key.lower() in ("", "none", "0", "false"):
        model_key = "pistachio"
    _REQ_Q = multiprocessing.Queue()
    _RESP_Q = multiprocessing.Queue()
    # 🔴 必须是**非 daemon** 进程：worker 内部要 spawn 规则子进程，而 daemon 进程
    #    在 Python 里禁止有子进程（会抛 AssertionError: daemonic processes are not
    #    allowed to have children）。worker 由「仍是单线程」的主进程 spawn，安全；
    #    退出时由 _stop_worker() 显式回收（而非依赖 daemon 自动随父退出）。
    worker = multiprocessing.Process(
        target=model_worker_main, args=(_REQ_Q, _RESP_Q, model_key)
    )
    print(f"[app] 正在启动独立模型进程（加载「{model_key}」，首次约需 40~90 秒）……", flush=True)
    worker.start()

    # 阻塞等 worker 回 ready（worker 在单线程里加载模型 + spawn，安全）
    try:
        msg = _RESP_Q.get(timeout=float(os.environ.get("RETRO_WORKER_TIMEOUT", "900")))
    except Exception:  # noqa: BLE001
        msg = {}
    if msg.get("type") == "ready":
        if msg.get("error"):
            _WORKER_FAILED = True
            print(f"[app] ✕ 模型进程启动失败：{msg['error']}", flush=True)
            if msg.get("traceback"):
                print(msg["traceback"], flush=True)
        else:
            _WORKER_READY.set()
            print("[app] ✓ 模型进程已就绪，可立即接受预测。", flush=True)
    else:
        print("[app] ✕ 模型进程返回了意外消息，预测将不可用。", flush=True)
        _WORKER_FAILED = True

    # 🔴 主线程要跑预测任务（validate/predict），会用到大库 rdkit；
    #    在正式接请求之前预热一次，避免首个 job 在请求线程里临时 import rdkit
    #    拖慢甚至饿死 GET /api/jobs 轮询（rdkit import 在首次调用时约需数秒）。
    try:
        import rdkit  # noqa: F401

        print("[app] ✓ rdkit 已预热（主线程预测路径不再临时 import）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[app] 注意：rdkit 预热失败（{e}），首个 job 会临时 import", flush=True)

    print(f"  请在浏览器打开:  http://{host}:{port}", flush=True)
    print("  按 Ctrl+C 停止服务（会自动回收规则子进程，退出时的红色", flush=True)
    print("  BrokenPipeError traceback 属于正常现象，不影响任何功能）", flush=True)
    print("=" * 68, flush=True)

    # Flask 在独立后台线程里跑，主线程专门 pump 任务队列（预测永远在主线程经队列转发）
    flask_thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, threaded=True),
        name="flask-serve",
        daemon=True,
    )
    flask_thread.start()

    if _mt_executor is not None:
        try:
            _mt_executor.pump()  # 主线程永远在这里：逐个执行预测任务
        except KeyboardInterrupt:
            pass
        finally:
            _stop_worker(worker)
            _shutdown_workers()
            print("[app] 服务已停止（子进程已回收）", flush=True)
    else:
        # 任务中心不可用：主线程空转，等 Flask 线程结束
        try:
            flask_thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            _stop_worker(worker)
            _shutdown_workers()


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    _install_exit_hooks()
    main()
