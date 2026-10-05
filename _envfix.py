# -*- coding: utf-8 -*-
"""环境修复：让本目录下所有脚本在「conda 环境未激活」时也能正常 import。

用法：在脚本最顶部（任何第三方 import 之前）执行
    import _envfix  # noqa: F401

解决的问题：Windows 上 conda 未激活时 <env>\Library\bin 不在 PATH 中，
_ssl.pyd 依赖的 libcrypto/libssl DLL 找不到，报
"DLL load failed while importing _ssl: 找不到指定的模块"。
"""
import os
import sys

_ENV_ROOT = os.path.dirname(os.path.abspath(sys.executable))
_BIN_DIR = os.path.join(_ENV_ROOT, "Library", "bin")

if os.path.isdir(_BIN_DIR):
    # 1) 加入 PATH（对后续 LoadLibrary 生效）
    if _BIN_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _BIN_DIR + os.pathsep + os.environ.get("PATH", "")

    # 2) 显式预加载 OpenSSL DLL（最可靠）
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
