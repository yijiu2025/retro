# -*- coding: utf-8 -*-
"""RetroChimera 本地网页版 —— 逆合成预测 + 完整合成路径搜索

启动（在仓库根执行，用相对路径 —— 换盘符 / 换目录都不用改）:
    python web/app.py
    或双击仓库根目录的 启动后端API.bat

浏览器打开 http://127.0.0.1:8000   ← 只返回端点清单 JSON，不托管前端页面
"""
# 与 llm.py / jobhub.py 一致：推迟注解求值，允许 Python 3.9 下使用 `X | None` 语法
from __future__ import annotations

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

import json
import threading
import time
import traceback
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

# --- 规则子进程数限制：**已移到模型服务进程** -------------------------------
# `bootstrap.limit_workers` 的实现就是 `import syntheseus` 然后打补丁，而它必须
# 早于 retrochimera/syntheseus 导入。从前放在这里，等于**逼着 API 进程也把
# syntheseus + torch 整个拖进来**（几十秒的启动成本），可 API 现在根本不跑模型。
# 真正 spawn 规则子进程的只有模型服务进程，需要限制的是它 —— 见
# web/model_server.py 顶部，那边 import 得更早。
# 🔴 不要为了「保险」把这段加回来：一加 API 启动就慢几十秒，而且没有任何收益。

# --- 模型表 / 纯算子：**单一来源在 model_runtime** -------------------------
# 🔴 这里只 import，不再自己定义一份。模型服务进程（model_server.py）也要用
#    同一份 MODELS 与同一套搜索逻辑 —— 两份定义一定会分叉，然后就是
#    「API 说有这个模型、模型服务说不认识」这类查半天的怪事。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from model_client import ModelServiceClient  # noqa: E402
from model_runtime import (  # noqa: E402
    DEFAULT_SEARCH_ALGORITHM,
    MODELS,
    SEARCH_ALGORITHMS,
    canon_smiles,
    forward_models_catalog,
    make_inventory,
    make_value_function,
    models_catalog,
    run_retro_search,
)

#: 常驻模型服务的客户端（API 进程访问模型的**唯一**入口）。
#: 模型不在本进程里 —— 本进程既不加载、也不 spawn 规则子进程。
_model_service = ModelServiceClient()

app = Flask(__name__, static_folder=None)

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

# --- 模型跑在独立常驻服务里（见 web/model_server.py） -----------------------
# 本进程（API）**不再加载模型、不再 spawn 规则子进程**，只做 HTTP 转接。
# 好处有三条，都是实打实省时间的：
#   1. 启动不再等模型 —— 从前这里阻塞等 ready（40~90 秒起，冷盘更久），
#      期间连 /api/parse、/api/depict、AI 分析这些跟模型无关的接口都连不上，
#      前端全屏报「网络错误」，看不出是在等模型；
#   2. 改 API 代码重启 ⇒ 模型服务还活着 ⇒ 直接复用，**模型不重载**；
#   3. 本进程永不 import torch/syntheseus ⇒ 起得快，也不可能踩 spawn 死锁。
# 状态与推理全部经 `_model_service`（见文件上部）转接；前端只跟本服务说话。

# --- 任务中心：把长任务变成可查询、可取消、带阶段与日志的 job -----------------
# 详见 web/jobhub.py。此处只 import 并绑定模型缓存引用；runner 需要 get_model 与
# validate_target_smiles，这两个在文件后面才定义 ⇒ 真正装配放到文件末尾的
# _setup_jobhub()（由 /api/jobs 首次请求时触发，也在启动时预调用一次）。
try:
    import jobhub

    # 🔴 模型已经移出本进程 ⇒ 这里没有「已加载模型」的引用可以绑了。
    #    传空字典即可：jobhub 的 _peek_loaded() 目前没有调用点，保留只为兼容。
    jobhub.bind_loaded_cache({})
except Exception as _e:  # noqa: BLE001
    jobhub = None  # type: ignore[assignment]
    print(f"[app] 注意：任务中心未启用（{_e}），/api/predict 仍可用但无进度", flush=True)

_PREDICT_RUNNER = None
_SEARCH_RUNNER = None

# --- 大模型工艺分析（投料顺序 / 当量 / 条件 / 副产物 / LCMS / NMR） -------------
# 走独立的 OpenAI 兼容客户端，与模型推理链路完全解耦：不占主线程 pump、不 spawn 子进程。
try:
    import llm

    print(f"[app] 大模型分析：{'已配置' if llm.is_configured() else '未配置（.env 里填 RETRO_LLM_*）'}", flush=True)
except Exception as _e:  # noqa: BLE001
    llm = None  # type: ignore[assignment]
    print(f"[app] 注意：大模型分析模块未启用（{_e}）", flush=True)


def _setup_jobhub():
    """装配预测 + 搜索 runner（幂等）。"""
    global _PREDICT_RUNNER, _SEARCH_RUNNER
    if jobhub is None:
        return None
    if _PREDICT_RUNNER is None:
        _PREDICT_RUNNER = jobhub.make_predict_runner(
            validate_target_smiles=validate_target_smiles,
            predict_fn=predict_remote,
            model_keys=set(MODELS.keys()),
        )
    if _SEARCH_RUNNER is None:
        _SEARCH_RUNNER = jobhub.make_search_runner(
            validate_target_smiles=validate_target_smiles,
            search_fn=search_remote,
            model_keys=set(MODELS.keys()),
            # 开启「可行性过滤」前的闸门：在开跑之前就判正向模型在不在，
            # 免得搜索整趟跑完才失败（见 _require_forward_ready 的注释）。
            forward_check=_require_forward_ready,
        )
    return _PREDICT_RUNNER


def _require_model_ready(model_key: str = "") -> dict:
    """模型服务必须就绪，否则抛出**说得清**的原因。

    🔴 从前模型未就绪时下游只会甩一句类型错误（对着一份没加载的模型调推理），
       用户看到的是「NoneType is not callable」这类完全指不出方向的话。
       把「正在加载 / 没启动 / 加载的是别的模型」三种情况分开讲清楚。
    """
    st = _model_service.status()
    if st.get("state") == "loading":
        elapsed = float(st.get("elapsed") or 0)
        raise RuntimeError(
            f"模型「{st.get('model') or model_key or '默认'}」正在加载中"
            f"（{st.get('phase') or '加载中'}，已 {elapsed:.0f}s）—— 就绪后重试即可"
        )
    if not st.get("running"):
        raise RuntimeError("模型服务未运行 —— 请在「运行参数 → 预测模型」里点「加载模型」")
    if st.get("state") != "ready":
        raise RuntimeError(
            f"模型服务状态异常（{st.get('state')}）"
            + (f"：{st['error']}" if st.get("error") else "")
        )
    cur = st.get("model") or ""
    if model_key and cur and cur != model_key:
        raise RuntimeError(
            f"模型服务当前加载的是「{cur}」，"
            f"请先在「运行参数 → 预测模型」里切到「{model_key}」"
        )
    return st


def service_status() -> dict:
    """模型服务当前状态（HTTP 优先，文件回退）。"""
    return _model_service.status()


def predict_remote(smiles: str, model_key: str, num_results: int) -> list:
    """推理桥：HTTP 转接到常驻模型服务。

    从前这里是把请求丢进 multiprocessing 队列、阻塞等 worker 回结果；现在模型在
    **独立常驻服务**里（web/model_server.py），本进程只做转发 —— 因此不可能再
    碰到 Windows「多线程进程里 spawn 死锁」，也不会因为本进程重启而丢掉已加载的模型。
    """
    _require_model_ready(model_key)
    return _model_service.predict(smiles, num_results)


def _require_forward_ready() -> None:
    """「可行性过滤」要用正向模型当判据 —— 把没加载 / 在加载 / 加载失败分开讲清。

    🔴 为什么要在这里提前拦：不拦的话，这条错误会在**搜索已经开跑之后**才从模型
       服务冒出来。用户先看到进度条动了、再看到失败，白等一整趟。这里把它提到
       提交之前，错在哪儿当场说清。
    """
    fwd = service_status().get("forward") or {}
    state = str(fwd.get("state") or "idle")
    if state == "ready":
        return
    if state == "loading":
        raise RuntimeError("正向模型正在加载中 —— 等它变成「已就绪」再开「可行性过滤」")
    if state == "failed":
        raise RuntimeError(f"正向模型加载失败：{fwd.get('error') or '原因见模型服务窗口'}")
    if state == "unavailable":
        raise RuntimeError(
            "正向模型文件不在 models/ 目录里 —— 先跑 scripts/download_model.py 放好文件，"
            "再在「运行参数 → 拆分约束 → 可行性过滤」里点「加载正向模型」"
        )
    raise RuntimeError(
        "正向模型还没加载 —— 在「运行参数 → 拆分约束 → 可行性过滤」里点「加载正向模型」"
    )


def search_remote(params: dict) -> dict:
    """搜索桥：HTTP 转接到常驻模型服务（返回 run_retro_search 的那个 dict）。"""
    _require_model_ready(str(params.get("model") or ""))
    if params.get("use_forward_filter"):
        _require_forward_ready()
    return _model_service.search(params)


# --------------------------------------------------------------------------
# 退出清理：模型服务是**常驻**的，默认不随本进程退出
# --------------------------------------------------------------------------
# 从前这里有一大套 _iter_rule_servers / _shutdown_workers / _stop_worker，
# 用来在退出时回收规则子进程 —— 那是因为模型就住在本进程里。
# 现在模型搬进了独立的模型服务进程（web/model_server.py），它自己负责回收
# （见那边的 _bye / SetConsoleCtrlHandler）。本进程退出时**默认不动它**：
# 「重启 API 不重载模型」正是靠这一点成立的。
# 想让模型服务跟着 API 一起退（省内存）就设 RETRO_MODEL_STOP_WITH_API=1。


def _install_exit_hooks():
    import atexit

    atexit.register(_model_service.shutdown_if_owned)


# --------------------------------------------------------------------------
# 分子库存 / 价值函数 / 路线搜索：**单一来源在 model_runtime**
# --------------------------------------------------------------------------
# 🔴 canon_smiles / make_inventory / make_value_function / run_retro_search
#    都已在文件顶部从 model_runtime import —— 模型服务进程要用同一份实现。
#    这里再定义一遍就会出现「API 算出的起始物料」和「模型服务搜索时用的口径」
#    不一致，而且属于平时看不出来、只在边界分子上冒头的那种不一致。


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
# 🔴 本服务**只提供 API，不托管任何前端页面**（前端在 retroweb 仓库）。
#    访问根路径给一份自描述，方便浏览器直接打开时知道这是哪儿、有哪些接口。
_API_INDEX = {
    "service": "RetroChimera API",
    "hint": (
        "本服务只提供 API。前端界面在 retroweb 仓库，由它来调用这些接口。"
        "模型跑在独立的常驻服务里（默认 127.0.0.1:8001），本服务只做转接 —— "
        "所以本服务可以秒起，模型在后台慢慢加载。"
    ),
    "endpoints": [
        {"method": "GET", "path": "/api/models", "desc": "模型状态矩阵（每个模型 idle/loading/ready/failed + 服务概览）"},
        {"method": "POST", "path": "/api/load", "desc": "加载/切换模型（后启动；立刻返回，进度看 /api/models）"},
        {"method": "POST", "path": "/api/forward/load", "desc": "加载正向模型（可行性过滤判据；立刻返回，进度看 /api/models 的 forward）"},
        {"method": "POST", "path": "/api/model-service/<action>", "desc": "模型服务 start / stop / restart"},
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
        {"method": "POST", "path": "/api/feedback", "desc": "记录一条「预测 vs 实测」反馈（落盘 feedback.jsonl 供训练）"},
        {"method": "GET", "path": "/api/feedback", "desc": "读回已收集的反馈（供导出/查看）"},
    ],
    # 🔴 搜索算法的可选值**由后端给出**，前端不要自己硬编码一份列表：
    #    合法值的唯一判据是 model_runtime.SEARCH_ALGORITHMS（新增算法改那一处即可），
    #    前端硬编码就会出现「后端加了算法、界面选不到」或「界面能选、后端默默退回默认」。
    #    形状与 /api/models 的 models 列表对齐：[{key, label, desc}, ...]
    "search_algorithms": [
        {"key": key, "label": label, "desc": desc}
        for key, (label, desc) in SEARCH_ALGORITHMS.items()
    ],
    "default_search_algorithm": DEFAULT_SEARCH_ALGORITHM,
}


@app.route("/")
def index():
    return jsonify(_API_INDEX)


@app.route("/api")
def api_index():
    return jsonify(_API_INDEX)


@app.route("/api/models")
def api_models():
    """模型状态矩阵 —— 前端只认这一个入口。

    每行给出：文件在不在、当前什么状态（idle / loading / ready / failed /
    unavailable）、多大、是不是当前加载的那个；外加一份 service 概览
    （模型服务在不在跑、加载到哪一步、错误是什么）。

    🔴 从前这里把所有模型都报 `loaded: 全局布尔` —— 实际只有预加载那一个真的
       加载了，另一个是假的，前端于是显示「已加载」却一预测就失败。现在按
       **每个模型**的真实状态报，没加载的就是 idle，点一下才后启动。

    另外附带一个 `forward` 字段：正向模型（「可行性过滤」的判据）。它**不是**
    搜索主模型，所以不混进 `models` —— 否则前端会把它渲染成一格可选的"预测模型"，
    而选中它去跑逆合成必然失败。
    """
    return jsonify(_model_service.catalog(models_catalog(), forward_models_catalog()))


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
    """加载 / 切换模型 —— 「模型后启动」就发生在这里。

    body: {model: "pistachio"}（不传则用 RETRO_PRELOAD，再退到第一个可用的）

    语义：
      - 模型服务没在跑 ⇒ 拉起它（模型在后台慢慢加载）
      - 在跑但加载的是别的 ⇒ 停掉旧的、拉起新的（一次只留一个，省内存）
      - 已经就是它 ⇒ 直接返回，不折腾

    🔴 返回的是「**已经开始加载**」，不是「已经就绪」。加载要 40~90 秒，
       让这个 HTTP 请求挂在这里等，前端就只能看到一个转圈，也分不清是卡死
       还是在正常加载。前端应当拿返回的状态继续轮询 /api/models，
       直到该模型 state 变成 ready。
    """
    data = request.get_json(force=True, silent=True) or {}
    key = str(data.get("model") or "").strip()
    if not key:
        key = str(os.environ.get("RETRO_PRELOAD") or "").strip()
    catalog = models_catalog()
    if not key or key not in MODELS:
        key = next((m["key"] for m in catalog if m["exists"]), "")
    if not key:
        return jsonify({"ok": False, "error": "没有可用模型：models/ 目录是空的"}), 400

    row = next((m for m in catalog if m["key"] == key), None)
    if row is not None and not row["exists"]:
        return jsonify({"ok": False, "error": f"模型文件不存在：{row['path']}"}), 400

    try:
        svc = _model_service.load(key)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return (
            jsonify({"ok": False, "error": f"启动模型服务失败：{type(e).__name__}: {e}"}),
            500,
        )
    return jsonify({"ok": True, "model": key, "started": True, "service": svc})


@app.route("/api/forward/load", methods=["POST"])
def api_forward_load():
    """加载正向模型（「可行性过滤」的判据）。

    body: {forward?: "forward_pistachio"}（不传则由模型服务按 RETRO_PRELOAD_FORWARD /
    注册表第一项兜底）

    语义与 /api/load 一样是「**已受理**」，不是「已就绪」：
      - 模型服务没在跑 ⇒ 直接报错（正向模型挂在它下面，乱拉一份会多一个进程）
      - 已在加载同一个 ⇒ already:true，不会载两份

    🔴 与 /api/load 的关键区别：**不需要重启模型服务**。正向模型是纯 transformer，
       构造时不 spawn 规则子进程 ⇒ 可以在 HTTP 起来之后热挂（见 model_server.py）。
       所以这里没有「换模型 = 重启进程」那一步，也不会连带把已加载的逆向模型踢掉。
    """
    data = request.get_json(force=True, silent=True) or {}
    key = str(data.get("forward") or "").strip()
    st = _model_service.status()
    if not st.get("running"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "模型服务未运行 —— 请先在「运行参数 → 预测模型」里点「加载模型」",
                }
            ),
            400,
        )
    try:
        out = _model_service.load_forward(key)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({**out, "ok": True, "started": bool(out.get("started", True))})


@app.route("/api/model-service/<action>", methods=["POST"])
def api_model_service(action: str):
    """模型服务的启停（前端「加载模型 / 停止模型服务」两个按钮）。

    action: start | stop | restart
    body:   {model?: "..."}（start/restart 时可指定要加载哪个）

    🔴 单独开这个口子是为了让「模型服务」和「其他后端」真的能分开操作：
       改完代码重启 API 时，模型服务不受影响；反过来想省内存时，
       可以只停模型服务，API 照常提供解析 / 绘制 / AI 分析等能力。
    """
    act = action.strip().lower()
    data = request.get_json(force=True, silent=True) or {}
    key = str(data.get("model") or "").strip()

    if act == "stop":
        res = _model_service.stop()
        return jsonify({"ok": True, "action": "stop", "service": res})
    if act in ("start", "restart"):
        if not key:
            key = str(os.environ.get("RETRO_PRELOAD") or "").strip()
        if not key:
            key = next((m["key"] for m in models_catalog() if m["exists"]), "")
        if not key or key not in MODELS:
            return jsonify({"ok": False, "error": f"未知模型: {key or '(空)'}"}), 400
        try:
            # 🔴 restart 必须**真的重启**：load() 默认在「已在跑同一模型」时直接复用
            #    （那是 start 的语义）。不 force 的话，改完 model_runtime 的代码点
            #    「重启」等于没重启，而返回体照样是 ok/already —— 从结果上完全看不出来。
            res = _model_service.load(key, force=(act == "restart"))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        return jsonify({"ok": True, "action": act, "model": key, "service": res})
    return jsonify({"ok": False, "error": f"未知操作: {action}（可用 start / stop / restart）"}), 400


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
    # 🔴 请求级 LLM 凭据覆盖（Node 中转按用户身份注入）：白名单 base_url / api_keys / model。
    #    前端不传这个字段（它不该知道官方 key）；只有 Node 中转在服务端注入。
    profile = None
    raw_profile = data.get("profile")
    if isinstance(raw_profile, dict):
        p = {}
        base_url = str(raw_profile.get("base_url") or "").strip()
        if base_url:
            p["base_url"] = base_url
        raw_keys = raw_profile.get("api_keys")
        if isinstance(raw_keys, list):
            keys = [str(k).strip() for k in raw_keys if str(k).strip()]
            if keys:
                p["api_keys"] = keys
        model = str(raw_profile.get("model") or "").strip()
        if model:
            p["model"] = model
        if p:
            profile = p
    return {
        "product": str(data.get("product") or "").strip(),
        "precursors": [str(p).strip() for p in precursors if str(p).strip()],
        "probability": data.get("probability"),
        "model": str(data.get("model") or ""),
        "context": data.get("context") or {},
        "continue_from": str(data.get("continue_from") or "").strip(),
        "modules": modules,
        "profile": profile,
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


# --------------------------------------------------------------------------
# 错误反馈学习 —— 用户做实验后发现预测与实际偏差较大，通过反馈把「预测值 vs 实测值」
# 沉淀下来，供后续微调 / 蒸馏训练使用。
#
# 数据落盘到 <web>/data/feedback.jsonl，每行一条 JSON（JSON Lines，方便逐条喂训练）。
# 设计约定见仓库里的「反馈学习机制」文档。
# --------------------------------------------------------------------------
_FEEDBACK_DIR = os.path.join(HERE, "data")
_FEEDBACK_PATH = os.path.join(_FEEDBACK_DIR, "feedback.jsonl")
_FEEDBACK_LOCK = threading.Lock()

# 反馈体里允许出现的字段 → 落盘前的白名单 + 截断上限。🔴 不白名单 = 任意字段都能
# 注入，既可能撑爆文件，也可能混进不可信内容污染训练集。
_FEEDBACK_FIELDS = {
    "kind": 32,            # 反馈类型：tlc（点板/过柱）/ yield（收率）/ condition（条件）…
    "target": 256,         # 目标分子 SMILES（可选，整条路线上下文）
    "product": 256,        # 这一步产物 SMILES
    "precursors": 1024,    # 前体 SMILES，逗号分隔
    "reaction_id": 128,    # 这一步反应 id（前端确定性 reactionId）
    "model": 64,           # 预测用的模型名
    "predicted": 4096,     # 预测内容（Rf 范围、洗脱剂、收率…）
    "observed": 4096,      # 实测结果（用户实验后填的）
    "delta_note": 4096,    # 用户对「差异多大、为什么差」的自由描述
    "module": 32,          # 对应哪个分析模块（tlc/outcome/conditions…）
}
_FEEDBACK_LIST_FIELDS = ("precursors",)  # 需要拆成列表的字段


def _sanitize_feedback(data: dict) -> dict | None:
    """白名单 + 截断 + 补时间戳。返回 None 表示没有可落盘的字段。"""
    if not isinstance(data, dict) or not data:
        return None
    out: dict = {}
    for key, limit in _FEEDBACK_FIELDS.items():
        val = data.get(key)
        if val is None:
            continue
        if key in _FEEDBACK_LIST_FIELDS:
            items = val if isinstance(val, list) else [val]
            cleaned = [str(x).strip()[:limit] for x in items if str(x).strip()]
            if cleaned:
                out[key] = cleaned
            continue
        s = str(val).strip()
        if s:
            out[key] = s[:limit]
    # 一条反馈至少要能说明「预测了什么、实测是什么」才有训练价值
    if not out.get("predicted") and not out.get("observed"):
        return None
    out["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return out


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """接收一条「预测 vs 实测」反馈，追加到 feedback.jsonl。

    请求体：{ kind?, target?, product?, precursors?, reaction_id?, model?,
              predicted?, observed?, delta_note?, module? }
    全部可选，但 predicted / observed 至少要给一个，否则 400。

    🔴 不依赖大模型模块：即使 llm 未配置，反馈收集也要能用（用户实验反馈与 AI 无关）。
    """
    data = request.get_json(force=True, silent=True) or {}
    cleaned = _sanitize_feedback(data)
    if cleaned is None:
        return jsonify({"ok": False, "error": "至少需要 predicted 或 observed 之一"}), 400
    try:
        os.makedirs(_FEEDBACK_DIR, exist_ok=True)
        line = json.dumps(cleaned, ensure_ascii=False)
        with _FEEDBACK_LOCK:
            with open(_FEEDBACK_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"写入反馈失败：{e}"}), 500
    return jsonify({"ok": True, "saved": cleaned["at"]})


@app.route("/api/feedback", methods=["GET"])
def api_feedback_list():
    """读回已收集的反馈（供前端导出 / 用户查看），带条数上限，避免一次性吐全量。"""
    limit = request.args.get("limit", "200")
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    if not os.path.exists(_FEEDBACK_PATH):
        return jsonify({"ok": True, "count": 0, "items": []})
    items: list = []
    try:
        with open(_FEEDBACK_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for raw in lines[-limit:]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                items.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except OSError as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"读取反馈失败：{e}"}), 500
    return jsonify({"ok": True, "count": len(items), "items": items})


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
    """创建一个后台任务，立即返回 job_id（不阻塞）。

    kind=predict（默认，单步逆合成）：
      请求体：{"smiles": "...", "model": "pistachio", "num_results": 5}
    kind=search（多步路线搜索，一次搜到底）：
      请求体：{"smiles": "...", "model": "pistachio", "max_routes": 5,
               "max_calls": 60, "time_limit": 120, "max_depth": 3,
               "max_heavy_atoms": 10, "value_fn": "complexity",
               "algorithm": "retro_star",
               "purchasable": [], "required_materials": [],
               "use_forward_filter": false, "forward_top_k": 5}

    响应：  {"ok": true, "job_id": "..."}
    """
    if jobhub is None or _setup_jobhub() is None:
        return jsonify({"ok": False, "error": "任务中心不可用"}), 500
    data = request.get_json(force=True, silent=True) or {}
    if not str(data.get("smiles") or "").strip():
        return jsonify({"ok": False, "error": "请输入目标分子的结构或 SMILES"}), 400

    kind = str(data.get("kind") or "predict").strip().lower()
    if kind == "search":
        try:
            forward_top_k = max(1, min(int(data.get("forward_top_k", 5)), 50))
        except (TypeError, ValueError):
            forward_top_k = 5
        params = {
            "smiles": str(data.get("smiles")).strip(),
            "model": data.get("model", "pistachio"),
            "max_routes": int(data.get("max_routes", 5)),
            "max_calls": int(data.get("max_calls", 60)),
            "time_limit": float(data.get("time_limit", 120)),
            "max_depth": int(data.get("max_depth", 3)),
            "max_heavy_atoms": int(data.get("max_heavy_atoms", 10)),
            "value_fn": str(data.get("value_fn") or "complexity"),
            "algorithm": str(data.get("algorithm") or "").strip().lower(),
            "purchasable": data.get("purchasable") or [],
            "required_materials": data.get("required_materials") or [],
            # 可行性过滤（正向模型当判据）。默认关 —— 它要额外常驻一份正向模型。
            "use_forward_filter": bool(data.get("use_forward_filter")),
            "forward_top_k": forward_top_k,
        }
        job = jobhub.submit("search", params, _SEARCH_RUNNER)
    else:
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
        t0 = time.time()
        raw = predict_remote(smiles, model_key, num_results)
        infer_s = time.time() - t0

        results = []
        for i, c in enumerate(raw, 1):
            # 🔴 与 /api/jobs 的 runner 保持同一口径：模型给的是**反应 SMILES**
            #    （"reactants>>product"），前体必须取 ">>" 左侧，否则最后一个片段
            #    会带着 ">>产品"，下游会当成非法分子。
            rxn = str(c.get("reaction") or c.get("precursors") or "")
            prob = float(c.get("probability") or 0.0)
            left, sep, right = rxn.partition(">>")
            reactant_str = left if sep else rxn
            parts = [p for p in reactant_str.split(".") if p]
            results.append(
                {
                    "rank": i,
                    "precursors": reactant_str,
                    "precursor_list": parts,
                    "reaction": rxn,
                    "product": right if sep else "",
                    "probability": prob,
                    "num_molecules": len(parts),
                    "level": "high" if prob > 0.5 else ("mid" if prob > 0.1 else "low"),
                }
            )

        return jsonify(
            {
                "ok": True,
                "product": smiles,
                "model": model_key,
                "results": results,
                "infer_seconds": round(infer_s, 1),
            }
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


# --------------------------------------------------------------------------
# 路线搜索：**单一来源在 model_runtime.run_retro_search**
# --------------------------------------------------------------------------
# 🔴 搜索必须复用**已经加载的那一份模型**（一份连带规则子进程约 18GB），
#    所以它只能在模型服务进程里跑。API 侧一律经 `_model_service.search()` 转接。
#    从前这里有一份 run_retro_search 的完整拷贝 + `get_model(key)` 就地加载 ——
#    那会让 API 进程也载一份模型（内存翻倍），而且 API 进程是多线程的，
#    在里面 spawn 规则子进程会死锁。


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
    # 搜索算法：非法值不报错，交给下游静默退回默认 —— 这里不重复维护白名单，
    # 唯一的合法性判据是 model_runtime.SEARCH_ALGORITHMS。
    algorithm = str(data.get("algorithm") or "").strip().lower()
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

    use_forward_filter = bool(data.get("use_forward_filter"))
    try:
        forward_top_k = max(1, min(int(data.get("forward_top_k", 5)), 50))
    except (TypeError, ValueError):
        forward_top_k = 5

    try:
        t0 = time.time()
        # 🔴 搜索一律转接到模型服务（复用那份已加载的模型，本进程不载第二份）。
        #    这里传 model 进去是为了让「未就绪 / 加载的是别的模型」能给出明确报错。
        result = search_remote(
            {
                "smiles": smiles,
                "model": model_key,
                "max_routes": max_routes,
                "max_calls": max_calls,
                "time_limit": time_limit,
                "max_depth": max_depth,
                "max_heavy_atoms": max_heavy_atoms,
                "value_fn": value_fn,
                "algorithm": algorithm,
                "purchasable": purchasable,
                "required_materials": required,
                # 可行性过滤：正向模型当判据（见 model_runtime.wrap_with_forward_filter）。
                # 正向模型对象常驻在模型服务里，由那边按真实状态注入 —— 客户端只说要不要。
                "use_forward_filter": use_forward_filter,
                "forward_top_k": forward_top_k,
            }
        )
        # 同步接口补上 model（异步 job 的 result 由 runner 统一补元信息）
        result["model"] = model_key
        return jsonify(result)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def _autostart_model_service() -> None:
    """**后台**把模型服务拉起来 —— 本函数绝不阻塞 API 启动。

    这是「模型后启动」的落点：API 先就绪，模型才开始慢慢加载。
    这段时间前端照常能用解析 / 绘制 / AI 分析，并轮询 /api/models 看加载进度。
    """
    flag = os.environ.get("RETRO_AUTOSTART_MODEL", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        print("[app] 已关闭模型自动启动（RETRO_AUTOSTART_MODEL=0）", flush=True)
        return

    target = str(os.environ.get("RETRO_PRELOAD") or "").strip()
    if target.lower() in ("none", "0", "false", "off"):
        print("[app] RETRO_PRELOAD=none —— 不自动加载模型", flush=True)
        return
    if not target or target not in MODELS:
        target = next((m["key"] for m in models_catalog() if m["exists"]), "")
    if not target:
        print("[app] models/ 目录里没有可用模型，跳过自动加载", flush=True)
        return

    def _work() -> None:
        try:
            st = _model_service.ensure(target, wait=8.0)
            if st.get("running"):
                print(
                    f"[app] 模型服务已在运行（模型「{st.get('model') or '?'}」，"
                    f"状态 {st.get('state')}）",
                    flush=True,
                )
            elif st.get("state") == "loading":
                print(f"[app] 模型服务已在加载「{st.get('model') or target}」", flush=True)
            else:
                print("[app] 模型服务已拉起（在独立窗口里，模型正在加载）", flush=True)
        except Exception as e:  # noqa: BLE001
            print(
                f"[app] 注意：模型服务启动失败（{type(e).__name__}: {e}）"
                "—— 预测不可用，其余功能正常",
                flush=True,
            )

    threading.Thread(target=_work, name="model-autostart", daemon=True).start()


def main():
    port = int(os.environ.get("RETRO_PORT", "8000"))
    host = "127.0.0.1"
    print("=" * 68, flush=True)
    print("  RetroChimera API —— 逆合成预测 + 合成路径搜索", flush=True)
    print("=" * 68, flush=True)

    # 🔴 任务中心：预测/搜索现在都是 HTTP 转发（纯 IO 阻塞，本进程不再 spawn），
    #    用普通的单线程池就够。从前那个「把任务调度回主线程」的 _MainThreadExecutor
    #    是为了绕开 Windows spawn 死锁 —— 模型搬出去之后它就没有存在意义了。
    if jobhub is not None:
        jobhub.set_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="retrojob")
        )
        print("[app] ✓ 任务中心已装配", flush=True)

    # rdkit 预热：/api/parse、/api/depict、目标分子校验都要用
    try:
        import rdkit  # noqa: F401

        print("[app] ✓ rdkit 已预热", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[app] 注意：rdkit 预热失败（{e}），首次解析会慢一点", flush=True)

    # 🔴 模型服务**后台**拉起 —— 绝不阻塞。API 立刻就绪，模型慢慢加载。
    _autostart_model_service()

    print("", flush=True)
    print(f"  API 已就绪:  http://{host}:{port}", flush=True)
    print(f"  模型服务:    http://127.0.0.1:{_model_service.port}（在另一个窗口）", flush=True)
    print("  前端在 retroweb 仓库（npm run dev，端口 5177）", flush=True)
    print("", flush=True)
    print("  按 Ctrl+C 停止 API。模型服务是**常驻**的，默认不跟着退 ——", flush=True)
    print("  这样你重启 API 时模型不用重载。想一起停就设", flush=True)
    print("  RETRO_MODEL_STOP_WITH_API=1。", flush=True)
    print("=" * 68, flush=True)

    # 模型已不在本进程，不需要主线程 pump ⇒ Flask 直接在主线程跑
    try:
        app.run(host=host, port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        _model_service.shutdown_if_owned()
        print("[app] API 服务已停止", flush=True)


if __name__ == "__main__":
    _install_exit_hooks()
    main()
