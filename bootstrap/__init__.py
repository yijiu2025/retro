# -*- coding: utf-8 -*-
"""运行时引导（bootstrap）：必须在「重型第三方库」导入之前执行的 Windows 兼容垫片。

两个模块都是**副作用式**的 —— import 它们就完成了修补，不需要再调用任何函数：

- ``envfix``        —— conda 未激活时，把 ``<env>\\Library\\bin`` 补进 PATH 与 DLL 搜索目录，
                       否则 ``_ssl`` / OpenSSL 加载失败，报
                       "DLL load failed while importing _ssl"。
- ``limit_workers`` —— 把 syntheseus 的 ``cpu_count()`` 限制为 4（可用 ``RETRO_RULE_WORKERS``
                       覆盖）。否则 RuleApplicationServer 按 ``os.cpu_count()`` 开子进程，
                       每个约 4.4 GB，16 核合计约 70 GB，触发
                       "WinError 1455 页面文件太小，无法完成操作"。

🔴 **导入顺序是硬约束**：这两个垫片必须早于 torch / retrochimera / syntheseus 导入。
   写成 ``from bootstrap import limit_workers  # noqa: F401``，
   把这一行注释掉、或挪到第三方 import 之后，等于直接卸载该修复。

用法（脚本位于子目录时，先补仓库根到 sys.path）：

    import os
    import sys

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    from bootstrap import envfix, limit_workers  # noqa: F401
"""
