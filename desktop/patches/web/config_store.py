# -*- coding: utf-8 -*-
"""桌面版路径与配置 —— **单一来源**。

打包后的目录布局（解压即用，全相对路径，不写死盘符）：

    <包根>/
    ├─ RetroWeb.exe            ← 启动器（PyInstaller 小壳，无控制台）
    ├─ _internal/              ← retro 仓库代码（web/ bootstrap/ scripts/ core/）
    │   ├─ web/                ← 本文件所在目录
    │   └─ frontend/           ← retroweb 的 dist（构建产物）
    ├─ python39/               ← 便携 Python 3.9（embeddable）+ Lib/site-packages
    └─ data/                   ← 全部可变数据（模型 / GPU 包 / 配置 / 日志）
        ├─ config.json
        ├─ models/
        ├─ gpu-pkgs/
        ├─ wheels-gpu/
        ├─ downloads/
        ├─ model-service/
        └─ logs/

🔴 为什么可变数据要集中在 data/：打包目录可能放在只读位置或被人整体拷贝，
   「解压即用」要求所有写入都落到同一个确定的位置。模型目录仍可用
   RETRO_MODELS_DIR 环境变量覆盖（高级用户想把 4GB 模型放别的盘时用）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

# --------------------------------------------------------------------------
# 路径推导（全部相对本文件，换目录 / 换盘符都不用改）
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))          # _internal/web
_INTERNAL = os.path.dirname(_HERE)                            # _internal
PKG_ROOT = os.path.dirname(_INTERNAL)                         # 包根

#: 便携 Python（与包同级）。构建时装依赖，运行时被 API / 模型服务 / GPU 安装共用。
PYTHON_DIR = os.path.join(PKG_ROOT, "python39")
PYTHON_EXE = os.path.join(PYTHON_DIR, "python.exe")

#: retro 仓库三件套在包内的位置（app.py 的 _PARENT 指向这里）
REPO_ROOT = _INTERNAL
WEB_DIR = _HERE
CORE_DIR = os.path.join(_INTERNAL, "core")
SCRIPTS_DIR = os.path.join(_INTERNAL, "scripts")

#: 前端构建产物
FRONTEND_DIR = os.path.join(_INTERNAL, "frontend")

#: 可变数据根
DATA_DIR = os.path.join(PKG_ROOT, "data")

#: GPU 加速包（cu121 wheels 解压安装的目标，运行时**前置**到 sys.path）
GPU_PKGS_DIR = os.path.join(DATA_DIR, "gpu-pkgs")
#: GPU wheels 下载缓存（断点续传的落点）
GPU_WHEELS_DIR = os.path.join(DATA_DIR, "wheels-gpu")

_lock = threading.Lock()


def models_dir() -> str:
    """模型根目录：环境变量优先，否则 <包根>/data/models。"""
    return os.environ.get("RETRO_MODELS_DIR") or os.path.join(DATA_DIR, "models")


def config_path() -> str:
    return os.path.join(DATA_DIR, "config.json")


_DEFAULT_CONFIG = {
    # 计算后端：auto = 有 GPU 包且机器有 NVIDIA 显卡就用 GPU，否则 CPU
    #           cpu = 强制 CPU（即使装了 GPU 包）
    #           gpu = 强制 GPU（未装 GPU 包时模型服务会报错并回退提示）
    "compute": "auto",
    #: 规则子进程数。4 ≈ 18GB 内存；16GB 的机器建议 2（见 retro README）。
    "rule_workers": 2,
    #: 模型服务端口（0 = 每次启动随机挑空闲端口）
    "model_port": 0,
    #: API 端口（0 = 随机）
    "api_port": 0,
}


def load_config() -> dict:
    """读配置。文件不存在 / 损坏 → 回退默认值（不抛，启动路径不能败在这）。"""
    cfg = dict(_DEFAULT_CONFIG)
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    return cfg


def save_config(**patch) -> dict:
    """合并写回配置（原子写：tmp + os.replace）。"""
    with _lock:
        cfg = load_config()
        cfg.update(patch)
        os.makedirs(DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, config_path())
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return cfg


def compute_mode() -> str:
    return str(load_config().get("compute") or "auto").lower()


def gpu_pkgs_installed() -> bool:
    """GPU 包是否已安装（以 gpu-pkgs 下有没有 torch 为准）。"""
    return os.path.isfile(os.path.join(GPU_PKGS_DIR, "torch", "__init__.py"))


def rule_workers() -> int:
    try:
        return max(1, int(load_config().get("rule_workers") or 2))
    except (TypeError, ValueError):
        return 2


def ensure_dirs() -> None:
    """把 data/ 骨架建出来（幂等）。启动第一步调用。"""
    for d in (
        DATA_DIR,
        models_dir(),
        GPU_PKGS_DIR,
        GPU_WHEELS_DIR,
        os.path.join(DATA_DIR, "downloads"),
        os.path.join(DATA_DIR, "model-service"),
        os.path.join(DATA_DIR, "logs"),
        os.path.join(DATA_DIR, "outputs"),
    ):
        os.makedirs(d, exist_ok=True)
