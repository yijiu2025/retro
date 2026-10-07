"""命令行启动器 —— 在 WorkBuddy 会话的 Python shim 环境下也能正常启动 API 服务。

背景
----
1. 在 WorkBuddy 会话里执行 Python 时，宿主会往 sys.path 前面注入

     D:\\Program Files\\WorkBuddy\\resources\\app.asar.unpacked\\cli\\vendor\\shim

   该目录含一个 sitecustomize.py（monkey-patch os.unlink，做安全删除拦截），
   会干扰第三方包的导入 —— 表现为 `ModuleNotFoundError: No module named 'flask'`。
   本文件先把这类注入路径剔掉，再启动服务。

2. 🔴 app.py 的模型 worker 走 Windows `multiprocessing` 的 **spawn**：子进程会按
   `sys.modules['__main__'].__file__` 重新加载主模块，以便找回 worker 函数。
   因此本文件必须把 app.py **注册成 `sys.modules['__main__']`**，而不能只用
   `exec(src, {"__name__": "__main__"})` 糊一个命名空间 —— 那样
   `sys.modules['__main__']` 仍指向 run_api.py，子进程找不到 `model_worker_main`，
   一跑预测就 `PicklingError`（实测踩过，见本文件末尾 git 历史）。

用法
----
本文件位于**仓库根目录**。推荐直接双击根目录的 `启动后端API.bat`（不依赖本文件）；
需要在终端手动跑时：

    python run_api.py                  # 默认端口 8000
    set RETRO_PORT=8011 && python run_api.py

它等价于 `python web/app.py`，只是多做一步 sys.path 清理。
本文件只负责启动，不含任何业务逻辑。
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))  # 仓库根
_WEB = os.path.join(_HERE, "web")  # API 代码目录（app.py 所在）
_CORE = os.path.join(_HERE, "core")  # 上游 retrochimera 源码（vendored）

# 1) 移除 shim / 无关注入路径
_REMOVE_MARKERS = ("vendor\\shim", "vendor/shim", "ChemScript")
sys.path[:] = [p for p in sys.path if not any(m in p for m in _REMOVE_MARKERS)]


def _fix_paths():
    """把 仓库根 / core / web 依次放到 sys.path 最前，并补回 site-packages。

    最终顺序：web 优先（app.py），core 次之（上游包），仓库根最后（bootstrap/）。
    """
    for _d in (_HERE, _CORE, _WEB):
        if _d in sys.path:
            sys.path.remove(_d)
        sys.path.insert(0, _d)
    # 清理时可能误删本环境的 site-packages，补回来
    for _cand in (
        os.path.join(sys.prefix, "Lib", "site-packages"),
        os.path.join(sys.prefix, "lib", "site-packages"),
    ):
        if os.path.isdir(_cand) and _cand not in sys.path:
            sys.path.append(_cand)


_fix_paths()

# 2) 应用 DLL 修复（必须在 torch 等第三方 import 之前）
from bootstrap import envfix  # noqa: F401, E402

# 3) envfix / 后续 import 可能又把 shim 路径带回来，再清一次
sys.path[:] = [p for p in sys.path if not any(m in p for m in _REMOVE_MARKERS)]
_fix_paths()

# 4) 预导入 flask，确保 import 失败时立刻暴露（且此时路径已干净）
import flask  # noqa: F401, E402


def _run_app():
    """以 `__main__` 的身份执行 web/app.py。"""
    _app = os.path.join(_WEB, "app.py")

    # 🔴 关键：注册成 sys.modules['__main__']，且 __file__ 指向 app.py。
    # 这样 multiprocessing.spawn 的子进程会 run_path(app.py, "__mp_main__")
    # 重建出含 model_worker_main 的模块，pickle 才能成功。
    main_mod = types.ModuleType("__main__")
    main_mod.__file__ = _app
    main_mod.__loader__ = None
    main_mod.__spec__ = None  # 为 None 才能让 spawn 走「按路径重建」分支
    sys.modules["__main__"] = main_mod

    sys.argv = [_app, *sys.argv[1:]]
    with open(_app, encoding="utf-8") as f:
        _code = compile(f.read(), _app, "exec")
    exec(_code, main_mod.__dict__)  # noqa: S102


_run_app()
