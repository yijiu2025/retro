# -*- coding: utf-8 -*-
"""便携 Python 的解释器级路径引导（装在 python39/Lib/site-packages/ 下）。

site.py 在每个解释器启动时都会 import sitecustomize —— 包括模型服务的
multiprocessing spawn 子进程、pip 子进程、探测子进程。把「GPU 包前置」
放在这里，意味着**整棵进程树**自动获得一致的 CPU/GPU 路径视图，
不需要 PYTHONPATH 环境变量（embeddable Python 的 ._pth 会忽略它），
也不需要给每个入口脚本各写一遍。

决策来源：`<包根>/data/config.json` 的 `compute` 字段
  · auto = 装了 GPU 包就前置
  · cpu  = 不前置（即使装了）
  · gpu  = 前置；没装则回退 CPU 并记录原因
环境变量 RETRO_COMPUTE_FORCE=gpu/cpu 可临时覆盖（GPU 探测用）。

🔴 本文件在每个 Python 进程启动时同步执行，必须**快**且**绝不抛异常**：
   任何失败都静默跳过（退回默认 site-packages 顺序 = CPU）。
"""
import json
import os
import sys

try:  # noqa: SIM105 —— 整个文件就是「最好成功，失败也无害」
    _SP = os.path.dirname(os.path.abspath(__file__))          # .../python39/Lib/site-packages
    _PKG = os.path.dirname(os.path.dirname(os.path.dirname(_SP)))  # 包根

    def _mode() -> str:
        forced = os.environ.get("RETRO_COMPUTE_FORCE", "").strip().lower()
        if forced in ("cpu", "gpu"):
            return forced
        try:
            with open(os.path.join(_PKG, "data", "config.json"), encoding="utf-8") as fh:
                return str(json.load(fh).get("compute") or "auto").lower()
        except (OSError, ValueError):
            return "auto"

    _gpu_dir = os.path.join(_PKG, "data", "gpu-pkgs")
    _installed = os.path.isfile(os.path.join(_gpu_dir, "torch", "__init__.py"))
    _mode_v = _mode()

    _gpu_active = False
    _reason = "cpu"
    if _mode_v in ("auto", "gpu"):
        if _installed:
            if _gpu_dir not in sys.path:
                sys.path.insert(0, _gpu_dir)
            _gpu_active = True
            _reason = "gpu-pkgs"
        elif _mode_v == "gpu":
            _reason = "gpu-requested-but-not-installed"

    # 把生效结果落盘，API 的 /api/gpu/status 读它展示「当前后端」
    try:
        _bf = os.path.join(_PKG, "data", "model-service", "backend.json")
        _payload = {
            "gpu_active": _gpu_active,
            "reason": _reason,
            "compute": _mode_v,
            "gpu_pkgs_installed": _installed,
            "python": sys.executable,
            "pid": os.getpid(),
            "updated_at": __import__("time").time(),
        }
        _old = None
        try:
            with open(_bf, encoding="utf-8") as fh:
                _old = json.load(fh)
        except (OSError, ValueError):
            pass
        # 只在「结论变化」时写：pip 子进程也会跑到这里，别每次都落盘
        if not _old or _old.get("gpu_active") != _gpu_active or _old.get("reason") != _reason \
                or _old.get("compute") != _mode_v:
            os.makedirs(os.path.dirname(_bf), exist_ok=True)
            with open(_bf, "w", encoding="utf-8") as fh:
                json.dump(_payload, fh, ensure_ascii=False)
    except OSError:
        pass
except Exception:  # noqa: BLE001 —— sitecustomize 失败绝不能阻止解释器启动
    pass
