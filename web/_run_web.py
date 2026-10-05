"""会话内启动器 —— 在 WorkBuddy 会话的 Python shim 环境下也能正常启动 Flask 服务。

背景
----
在 WorkBuddy 会话里执行 Python 时，宿主会往 sys.path 前面注入
  D:\\Program Files\\WorkBuddy\\resources\\app.asar.unpacked\\cli\\vendor\\shim
该目录含一个 sitecustomize.py（monkey-patch os.unlink，做安全删除拦截），
会干扰第三方包的导入，导致 `ModuleNotFoundError: No module named 'flask'`。

用法（推荐给用户手动跑的方式是 web\\启动网页版.bat，不依赖本文件）：

    python _run_web.py            # 默认端口 8000
    set RETRO_PORT=8011 && python _run_web.py

本文件只做「清理 sys.path + 启动 app」，不改动任何业务逻辑。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# 1) 移除 shim / 无关注入路径
_REMOVE_MARKERS = ("vendor\\shim", "vendor/shim", "ChemScript")
sys.path[:] = [p for p in sys.path if not any(m in p for m in _REMOVE_MARKERS)]

# 2) 把自己目录 + 上一级目录放最前，保证 app.py / _envfix.py 可被找到
_PARENT = os.path.dirname(_HERE)
for _d in (_PARENT, _HERE):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 3) 补回本环境的 site-packages（清理时可能被误删）
for _cand in (
    os.path.join(sys.prefix, "Lib", "site-packages"),
    os.path.join(sys.prefix, "lib", "site-packages"),
):
    if os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.append(_cand)

# 4) 应用 DLL 修复（必须在 torch 等第三方 import 之前）
import _envfix  # noqa: F401

# 4b) 再清一次：_envfix / 后续 import 可能又把 shim 路径带回来，
#     不清理会导致 `ModuleNotFoundError: No module named 'flask'`。
sys.path[:] = [p for p in sys.path if not any(m in p for m in _REMOVE_MARKERS)]
for _d in (_PARENT, _HERE):
    if _d in sys.path:
        sys.path.remove(_d)
    sys.path.insert(0, _d)
for _cand in (
    os.path.join(sys.prefix, "Lib", "site-packages"),
    os.path.join(sys.prefix, "lib", "site-packages"),
):
    if os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.append(_cand)

# 4c) 预导入 flask，确保 import 失败时立刻暴露（且此时路径已干净）
import flask  # noqa: F401

# 5) 以 __main__ 身份运行 app.py（保证 if __name__ == "__main__" 生效）
_APP = os.path.join(_HERE, "app.py")
sys.argv = [_APP]
with open(_APP, encoding="utf-8") as f:
    _src = f.read()
_globals = {
    "__name__": "__main__",
    "__file__": _APP,
    "__builtins__": __builtins__,
}
exec(compile(_src, _APP, "exec"), _globals)
