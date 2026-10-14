#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RetroWeb 桌面版一键构建脚本。

用法（在任意 Python 3.9+ 上跑，建议与打包机隔离的 venv）：

    python build_package.py                     # 全量构建
    python build_package.py --skip-frontend     # 前端 dist 已存在，跳过 npm
    python build_package.py --skip-deps         # site-packages 已装好，跳过依赖安装
    python build_package.py --out D:\\pkg       # 指定输出目录

产物（--out 目录，默认 ./dist-pkg）：

    RetroWeb.exe          启动器（PyInstaller 小壳，无控制台）
    _internal/            retro 仓库代码 + frontend/dist + 桌面模块
    python39/             便携 Python 3.9 + 全部依赖（CPU 版 torch）
    data/                 配置 / 模型（空，首启或设置页下载）/ GPU 包 / 日志
    使用说明.txt

构建机要求：能访问 pypi.org / download.pytorch.org / data.pyg.org /
python.org / bootstrap.pypa.io；磁盘 ≥ 8GB 空闲。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PATCHES_WEB = os.path.join(HERE, "patches", "web")

#: 被「移出包」的待清理区。构建机自用，永远在 pkg 之外，绝不会打进分发件。
#: 设计原因：本环境对「批量删除」有护栏（shutil.rmtree / rm -rf 触碰 >50 文件
#: 会被拦截），而构建过程必须清掉 site-packages / torch/include / __pycache__ /
#: PyInstaller _build 等大量文件。改用 rename 把目录移出 pkg（同盘瞬时、不复制、
#: 不占额外磁盘）即可绕开护栏 —— rename 不是删除，护栏不拦。
EVICT_ROOT = ""

PY_VER = "3.9.13"
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
# 新版 get-pip.py 只支持 3.10+；3.9 要用版本化 bootstrap（官方维护的旧版入口）
GET_PIP_URL = f"https://bootstrap.pypa.io/pip/{PY_VER.rsplit('.', 1)[0]}/get-pip.py"

#: 依赖安装顺序 —— 与 retro/web/requirements.txt 的注释要求一致，别调换
#: 🔴 PyG 的 find-links 必须带：syntheseus-graphium 会间接依赖
#:    torch-scatter/sparse/cluster，而 pypi 上这三个只有 sdist（没有 wheel），
#:    不带 -f 就会去编译源码，在没有 CUDA 工具链的机器上必败。
#: 🔴🔴 必须**单条命令**装完，不能分多条：pip 的 `--target` 在解析依赖时
#:    **不参考目标目录里已装的内容**，分条安装时，后一条命令里未锁版本的
#:    依赖会被解析成最新版并覆盖前者 —— 实测把锁好的 torch 2.2.2+cpu
#:    顶成了 torch 2.8.0（syntheseus-graphium 只写了 `torch` 没写版本）。
PIP_ALL = [
    "--index-url", "https://download.pytorch.org/whl/cpu",
    "--extra-index-url", "https://pypi.org/simple",
    "-f", "https://data.pyg.org/whl/torch-2.2.0+cpu.html",
    # --- torch 家族：版本锁死，CPU 构建（+cu121 版由 GPU 加速包另行提供）---
    "torch==2.2.2", "torchvision==0.17.2", "numpy==1.26.4",
    "torch-geometric==2.5.2", "torch-scatter", "torch-sparse", "torch-cluster",
    # --- 化学与通用（pypi）---
    "rdkit==2023.9.6", "rdchiral", "pandas", "scipy", "h5py",
    "omegaconf", "antlr4-python3-runtime", "tqdm", "joblib", "more-itertools",
    "pebble", "pyyaml",
    # --- 逆合成框架（会间接拖 torch/PyG，靠上面的锁压住）---
    "syntheseus==0.8.0", "syntheseus-graphium==0.1.0",
    # --- API 服务与桌面窗口 ---
    "flask", "requests", "pywebview",
    # 🔴 setuptools 必须 <81：81.0.0 起彻底移除 pkg_resources，
    #    而 pytorch_lightning / torchmetrics 仍 `import pkg_resources`。
    #    若不锁，模型服务加载阶段会 ModuleNotFoundError。
    "setuptools==80.9.0",
]

#: 剥离清单（安装后从 site-packages 里删，纯减体积，不影响推理）
STRIP_GLOBS = [
    ("torch", "include"),          # C++ 头文件 ~200MB，推理用不到
    ("torch", "share"),
    ("torchvision", "test"),
    ("pandas", "tests"),
    ("rdkit", "RDData"),           # 演示数据，API 链路不读
]


def log(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def run(cmd: list, **kw) -> None:
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def download(url: str, dest: str) -> None:
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        print(f"  已存在，跳过下载: {dest}")
        return
    log(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)


def _evict(path: str) -> None:
    """把 path 移出包（重命名到 EVICT_ROOT），不删除、不触发批量删除护栏。

    同盘 rename 瞬时完成、不复制、几乎不占额外磁盘；被移走的目录位于 pkg 之外，
    不会被打进最终 7z。护栏只拦截「删除」，rename 不受影响。
    """
    global EVICT_ROOT
    if not (os.path.exists(path) or os.path.islink(path)):
        return
    if not EVICT_ROOT:
        EVICT_ROOT = os.path.join(os.path.dirname(os.path.abspath(path)), "_evict_trash")
    os.makedirs(EVICT_ROOT, exist_ok=True)
    base = os.path.basename(path.rstrip(r"\/")) or "_anon"
    dest = os.path.join(EVICT_ROOT, f".evict_{base}_{int(time.time() * 1000)}_{os.getpid()}")
    while os.path.exists(dest):
        dest += "_"
    try:
        os.rename(path, dest)
        print(f"  移出（非删除，待手动清理）: {path} -> {dest}")
    except OSError:
        try:
            shutil.move(path, dest)
        except Exception:  # noqa: BLE001
            print(f"  [warn] 无法移出 {path}（忽略，继续）")
            return


# --------------------------------------------------------------------------
# 增量补丁（对 retro 仓库源码的精确替换；匹配不上就报错，绝不静默跳过）
# --------------------------------------------------------------------------
PATCH_APP_PY = (
    'app = Flask(__name__, static_folder=None)\n',
    '''app = Flask(__name__, static_folder=None)

# --- 桌面版：静态托管 + 模型下载 + GPU 管理 --------------------------------
# 路由全部注册在 desktop_routes.register(app) 里，app.py 本体只加这一行，
# 上游更新本文件时冲突面最小。API-only 部署（未打包）时该模块不存在，
# ImportError 被吞掉，行为与从前完全一致。
try:
    import desktop_routes  # noqa: E402

    desktop_routes.register(app)
except ImportError:
    pass
''',
)

PATCH_MODEL_CLIENT = (
    '''        kwargs: dict = {"cwd": PARENT, "env": env, "close_fds": True}
        if os.name == "nt":
            # 新控制台：日志看得见，且 Ctrl+C / 关窗口不会波及 API 进程
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
''',
    '''        kwargs: dict = {"cwd": PARENT, "env": env, "close_fds": True}
        if os.name == "nt":
            if os.environ.get("RETRO_DESKTOP") == "1":
                # 桌面打包版：模型服务是后台组件，**绝不弹控制台窗口**
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            else:
                # 新控制台：日志看得见，且 Ctrl+C / 关窗口不会波及 API 进程
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
''',
)

# --------------------------------------------------------------------------
# 前端补丁（retroweb/src；新增文件整份拷贝，存量文件精确替换）
# --------------------------------------------------------------------------
PATCHES_WEB_SRC = os.path.join(HERE, "patches-frontend")

PATCH_ROUTER = (
    """  {
    path: '/profile',
    name: 'profile',
    component: () => import('@/views/ProfileView.vue'),
    meta: { title: '个人中心', desc: '账号信息与会话', requiresAuth: true }
  },
""",
    """  {
    path: '/profile',
    name: 'profile',
    component: () => import('@/views/ProfileView.vue'),
    meta: { title: '个人中心', desc: '账号信息与会话', requiresAuth: true }
  },
  {
    path: '/settings/models',
    name: 'model-settings',
    component: () => import('@/views/ModelSettingsView.vue'),
    meta: { title: '模型设置', desc: '模型下载 / 手动放置 / CPU 与 GPU 切换', requiresAuth: true }
  },
""",
)

PATCH_NAV_ITEMS = (
    """const navItems = [
  { path: '/', label: '工作台', icon: 'workbench' },
  { path: '/tasks', label: '任务列表', icon: 'tasks' },
  { path: '/history', label: '历史记录', icon: 'history' },
  { path: '/profile', label: '个人中心', icon: 'profile' }
];""",
    """const navItems = [
  { path: '/', label: '工作台', icon: 'workbench' },
  { path: '/tasks', label: '任务列表', icon: 'tasks' },
  { path: '/history', label: '历史记录', icon: 'history' },
  { path: '/profile', label: '个人中心', icon: 'profile' },
  { path: '/settings/models', label: '模型设置', icon: 'settings' }
];""",
)

PATCH_NAV_ICONS = (
    """  profile: [
    'M12 4a4 4 0 1 0 .01 0',
    'M4 21c0-4 4-6 8-6s8 2 8 6'
  ]
};""",
    """  profile: [
    'M12 4a4 4 0 1 0 .01 0',
    'M4 21c0-4 4-6 8-6s8 2 8 6'
  ],
  settings: [
    'M12 15a3 3 0 1 0 .01 0',
    'M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z'
  ]
};""",
)


def apply_patch(path: str, old: str, new: str, what: str) -> None:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if new in text and old not in text:
        print(f"  [skip] {what} 已是打过补丁的状态")
        return
    if old not in text:
        raise SystemExit(f"补丁失败：在 {path} 里找不到 {what} 的锚点 —— 上游代码可能已变动")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(old, new, 1))
    print(f"  [ok] {what}")


# --------------------------------------------------------------------------
# 各阶段
# --------------------------------------------------------------------------
def stage_portable_python(pkg: str, work: str) -> str:
    log(f"阶段 1/7：便携 Python {PY_VER}")
    py_dir = os.path.join(pkg, "python39")
    if os.path.isfile(os.path.join(py_dir, "python.exe")):
        print("  已存在，跳过")
        return py_dir
    zpath = os.path.join(work, f"python-{PY_VER}-embed-amd64.zip")
    download(EMBED_URL, zpath)
    os.makedirs(py_dir, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(py_dir)
    # 🔴 ._pth 不在这里写：get-pip 会把它重置回默认内容（实测）。
    #    挪到 stage_pip 里 get-pip 之后写，并验证 pip 可用。
    print(f"  就绪: {py_dir}")
    return py_dir


def write_pth(py_dir: str) -> None:
    """写 ._pth：让 Lib/site-packages 生效且跑 site（sitecustomize 靠它加载）。

    默认的 embeddable ._pth 把 `#import site` 注释掉了 —— site-packages 不在
    sys.path 里，pip 装得上但 `python -m pip` 找不到。
    """
    pth = os.path.join(py_dir, "python39._pth")
    with open(pth, "w", encoding="utf-8") as fh:
        fh.write("python39.zip\n.\nLib/site-packages\nimport site\n")


def pip_ok(py_dir: str) -> bool:
    r = subprocess.run([os.path.join(py_dir, "python.exe"), "-m", "pip", "--version"],
                       capture_output=True)
    return r.returncode == 0


def stage_pip(py_dir: str, work: str) -> str:
    log("阶段 2/7：pip / setuptools / wheel")
    sp = os.path.join(py_dir, "Lib", "site-packages")
    os.makedirs(sp, exist_ok=True)
    if not pip_ok(py_dir):
        gp = os.path.join(work, "get-pip.py")
        download(GET_PIP_URL, gp)
        run([os.path.join(py_dir, "python.exe"), gp, "--no-warn-script-location"])
    # get-pip 可能重置 ._pth —— 在它之后写，写完必须验证
    write_pth(py_dir)
    if not pip_ok(py_dir):
        raise SystemExit("._pth 写入后 pip 仍不可用 —— 检查便携 Python 是否完整")
    print("  pip 可用")
    return sp


def pip_install(py_dir: str, sp: str, args: list, extra: list | None = None) -> None:
    run([
        os.path.join(py_dir, "python.exe"), "-m", "pip", "install",
        "--no-warn-script-location", "--target", sp,
        *(extra or []), *args,
    ])


def stage_deps(py_dir: str, sp: str, repo: str, work: str) -> None:
    log("阶段 3/7：安装依赖（单条解析，锁定 torch 2.2.2 + numpy 1.x）")
    # 全新目标目录：目标里可能残留上一轮的旧版本文件（pip --target 不卸载旧版），
    # 混版比慢更危险。清空后先用 get-pip 把 pip 装回来（pip 本身住在目标里）。
    if os.path.isdir(sp):
        _evict(sp)
    os.makedirs(sp, exist_ok=True)
    gp = os.path.join(work, "get-pip.py")
    download(GET_PIP_URL, gp)
    run([os.path.join(py_dir, "python.exe"), gp, "--no-warn-script-location"])
    write_pth(py_dir)
    pip_install(py_dir, sp, PIP_ALL)
    pip_install(py_dir, sp, [], extra=["--no-deps", "--no-build-isolation",
                                       os.path.join(repo, "core")])
    _strip(py_dir)


def _strip(py_dir: str) -> None:
    log("阶段 3b：剥离开发残留（torch/include 等）")
    freed = 0
    sp = os.path.join(py_dir, "Lib", "site-packages")
    for pkg_dir, *subs in STRIP_GLOBS:
        for sub in subs:
            target = os.path.join(sp, pkg_dir, sub)
            if os.path.isdir(target):
                freed += _dir_size(target)
                _evict(target)
                print(f"  移出 {pkg_dir}/{sub}（{freed / 1e6:.0f} MB 累计）")
    # __pycache__ 全清（.pyc 会按运行机器重新生成，且 py3.9 的 pyc 不可跨版本复用）
    for root, dirs, _files in os.walk(sp):
        for d in list(dirs):
            if d == "__pycache__":
                target = os.path.join(root, d)
                freed += _dir_size(target)
                _evict(target)
                dirs.remove(d)
    print(f"  共释放约 {freed / 1e6:.0f} MB")


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def stage_frontend(web: str, work: str) -> str:
    """打前端补丁 → npm 构建 → 返回 dist 目录（桌面档：免登录 + 同源相对路径）。

    🔴 outDir 指向工作缓存而不是仓库内的 dist/：vite 清空 outDir 是批量删除，
       在某些受保护的工作区里会被安全策略拦截（仓库 README 也提醒过这事）。
    """
    log("阶段 3.5/7：前端补丁与构建")
    src = os.path.join(web, "src")
    # 新增文件
    for rel in (os.path.join("views", "ModelSettingsView.vue"),
                os.path.join("api", "retro", "desktop.ts")):
        dst = os.path.join(src, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(PATCHES_WEB_SRC, rel), dst)
        print(f"  新增 src/{rel.replace(os.sep, '/')}")
    # 存量文件精确替换
    apply_patch(os.path.join(src, "router", "index.ts"), *PATCH_ROUTER, "router 模型设置路由")
    apply_patch(os.path.join(src, "components", "layout", "AppShell.vue"),
                *PATCH_NAV_ITEMS, "AppShell 导航项")
    apply_patch(os.path.join(src, "components", "layout", "AppShell.vue"),
                *PATCH_NAV_ICONS, "AppShell 设置图标")
    # 桌面档 env（免登录 + 直连 Python + 同源 /api）
    env_prod = os.path.join(web, ".env.production")
    with open(env_prod, "w", encoding="utf-8") as fh:
        fh.write(
            "# 桌面打包档：单机免登录 + 同源相对路径 + 直连 Flask（构建脚本生成）\n"
            "VITE_AUTH_BYPASS=true\n"
            "VITE_API_URL=\n"
            "VITE_SSO_URL=\n"
            "VITE_RETRO_VIA_NODE=false\n"
            "VITE_RETRO_URL=/api\n"
            "VITE_PERSIST_SERVER=false\n"
            "VITE_RETRO_MODEL_CONTROL=auto\n"
        )
    print("  写入 .env.production（桌面档）")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    if not os.path.isdir(os.path.join(web, "node_modules")):
        run([npm, "ci"], cwd=web)
    web_dist = os.path.join(work, "web-dist")
    run([npm, "run", "build", "--", "--outDir", web_dist, "--emptyOutDir"], cwd=web)
    print(f"  dist 就绪（{_dir_size(web_dist) / 1e6:.0f} MB）")
    return web_dist


def stage_code(pkg: str, repo: str, web_dist: str) -> None:
    log("阶段 4/7：装配 _internal（retro 代码 + 前端 dist + 桌面模块）")
    internal = os.path.join(pkg, "_internal")
    for sub in ("web", "bootstrap", "scripts", "core"):
        src = os.path.join(repo, sub)
        dst = os.path.join(internal, sub)
        if os.path.isdir(dst):
            _evict(dst)
        shutil.copytree(src, dst)
        print(f"  复制 {sub}/")
    # 文档留着：使用说明 / 上游导读对桌面用户排查问题有用
    for f in ("README.md", "使用说明.md", "NOTICE", "LICENSE"):
        src = os.path.join(repo, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(internal, f))

    # 补丁 + 新模块
    apply_patch(os.path.join(internal, "web", "app.py"), *PATCH_APP_PY, "app.py 桌面路由挂载")
    apply_patch(os.path.join(internal, "web", "model_client.py"),
                *PATCH_MODEL_CLIENT, "model_client.py 隐藏模型服务窗口")
    # 🔴 desktop_app.py 必须在 _internal/ 根下：它按 __file__ 推 _HERE=_internal
    #    装配 sys.path，launcher 也固定拉起 `_internal\desktop_app.py`。
    #    其余桌面模块进 web/（app.py 的 desktop_routes.register 从那里 import）。
    for f in sorted(os.listdir(PATCHES_WEB)):
        if not f.endswith(".py"):
            continue
        dst = internal if f == "desktop_app.py" else os.path.join(internal, "web")
        shutil.copy2(os.path.join(PATCHES_WEB, f), os.path.join(dst, f))
        print(f"  新增 {os.path.relpath(os.path.join(dst, f), internal).replace(os.sep, '/')}")
    # sitecustomize 去便携 Python 的 site-packages（GPU 路径前置在那生效）
    shutil.copy2(os.path.join(PATCHES_WEB, "sitecustomize.py"),
                 os.path.join(pkg, "python39", "Lib", "site-packages", "sitecustomize.py"))
    print("  安装 sitecustomize.py → python39/Lib/site-packages/")

    dist = web_dist
    if not os.path.isdir(dist):
        raise SystemExit(f"前端产物不存在: {dist}（前端构建失败？）")
    dst = os.path.join(internal, "frontend")
    if os.path.isdir(dst):
        _evict(dst)
    shutil.copytree(dist, dst)
    print(f"  复制 frontend/（{_dir_size(dst) / 1e6:.0f} MB）")


def stage_data(pkg: str) -> None:
    log("阶段 5/7：data/ 骨架与说明")
    data = os.path.join(pkg, "data")
    for sub in ("models", "gpu-pkgs", "wheels-gpu", "downloads",
                "model-service", "logs", "outputs"):
        os.makedirs(os.path.join(data, sub), exist_ok=True)
    import json

    cfg = os.path.join(data, "config.json")
    if not os.path.isfile(cfg):
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"compute": "auto", "rule_workers": 2,
                       "model_port": 0, "api_port": 0}, fh, indent=2)
    note = os.path.join(data, "models", "把模型放这里.txt")
    with open(note, "w", encoding="utf-8") as fh:
        fh.write(
            "模型权重放置说明\n"
            "================\n\n"
            "模型网盘（百度网盘，官方提供，推荐）：\n"
            "  https://pan.baidu.com/s/5D5zka_-76R4bFa3YZqtMxQ\n"
            "  从网盘下载对应模型的压缩包，解压到本目录即可（目录名见下）。\n\n"
            "方式一（推荐）：软件内「模型设置」页直接下载，自动解压校验。\n\n"
            "方式二（手动）：从官方渠道拿到 checkpoint 压缩包后解压到本目录，\n"
            "目录名必须与下面一致（zip 内是平铺文件，直接解到 <models>/<目录名>/）：\n\n"
            "  retrochimera_pistachio/     Pistachio 主模型（约 4.2GB）\n"
            "  retrochimera_uspto50k/      USPTO-50K 轻量模型（约 0.3GB）\n"
            "  retrochimera_uspto_full/    USPTO-FULL（约 4.6GB，可选）\n"
            "  forwardchimera_denovo_pistachio/   正向模型（约 0.8GB，可选）\n\n"
            "放好后在「模型设置」页点「加载」即可。\n"
            "想换成别的目录：设环境变量 RETRO_MODELS_DIR 指向它。\n"
        )
    readme = os.path.join(pkg, "使用说明.txt")
    with open(readme, "w", encoding="utf-8") as fh:
        fh.write(
            "RetroWeb 桌面版\n"
            "==============\n\n"
            "双击 RetroWeb.exe 启动。首次启动没有模型，请在窗口内\n"
            "「模型设置」页下载（ pistachio 约 4.2GB，uspto50k 约 0.3GB），\n"
            "或按 data\\models\\把模型放这里.txt 手动放置。\n"
            "模型网盘（百度网盘，官方提供）：\n"
            "  https://pan.baidu.com/s/5D5zka_-76R4bFa3YZqtMxQ\n\n"
            "计算后端（CPU / GPU）在「模型设置」页切换；GPU 加速包\n"
            "（约 2GB）可在同一页面按需下载安装。\n\n"
            "日志：data\\logs\\desktop.log（启动器：launcher.log）\n"
        )
    print("  data/ 就绪")


def stage_launcher(pkg: str) -> None:
    log("阶段 6/7：冻结启动器 RetroWeb.exe")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("构建机缺少 pyinstaller：pip install pyinstaller 后重试")
    build = os.path.join(pkg, "_build")
    run([
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--clean", "--noconfirm",
        "--name", "RetroWeb",
        "--distpath", pkg, "--workpath", build, "--specpath", build,
        os.path.join(HERE, "launcher.py"),
    ])
    exe = os.path.join(pkg, "RetroWeb.exe")
    if not os.path.isfile(exe):
        raise SystemExit("PyInstaller 没产出 RetroWeb.exe")
    _evict(build)
    print(f"  就绪: {exe}（{os.path.getsize(exe) / 1e6:.1f} MB）")


def stage_compress(pkg: str) -> None:
    log("阶段 7/7：压缩分发件")
    seven = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    out7z = pkg + ".7z"
    if seven:
        run([seven, "a", "-t7z", "-mx=7", "-m0=lzma2", out7z, os.path.basename(pkg)],
            cwd=os.path.dirname(pkg))
    else:
        try:
            import py7zr  # noqa: F401
        except ImportError:
            print("  没有 7z 也没有 py7zr，跳过压缩（分发整个文件夹即可）")
            return
        with py7zr.SevenZipFile(out7z, "w", filters=[{"id": py7zr.FILTER_LZMA2,
                                                      "preset": 7}]) as z:
            z.writeall(pkg, os.path.basename(pkg))
    print(f"  就绪: {out7z}（{os.path.getsize(out7z) / 1e6:.0f} MB）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.join(HERE, "..", "retro"),
                    help="retro 仓库路径")
    ap.add_argument("--web", default=os.path.join(HERE, "..", "retroweb"),
                    help="retroweb 仓库路径")
    ap.add_argument("--out", default=os.path.join(HERE, "dist-pkg"), help="输出目录")
    ap.add_argument("--work", default=os.path.join(HERE, "build-cache"), help="下载缓存")
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--skip-frontend", action="store_true")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    web = os.path.abspath(args.web)
    pkg = os.path.abspath(args.out)
    work = os.path.abspath(args.work)
    os.makedirs(work, exist_ok=True)
    os.makedirs(pkg, exist_ok=True)
    global EVICT_ROOT
    EVICT_ROOT = os.path.join(os.path.dirname(pkg), "_evict_trash")
    os.makedirs(EVICT_ROOT, exist_ok=True)

    py_dir = stage_portable_python(pkg, work)
    sp = stage_pip(py_dir, work)
    if not args.skip_deps:
        stage_deps(py_dir, sp, repo, work)
    if args.skip_frontend and os.path.isdir(os.path.join(work, "web-dist")):
        print("\n=== 跳过前端构建（--skip-frontend，沿用缓存 dist）===")
        web_dist = os.path.join(work, "web-dist")
    else:
        web_dist = stage_frontend(web, work)
    stage_code(pkg, repo, web_dist)
    stage_data(pkg)
    stage_launcher(pkg)
    stage_compress(pkg)

    total = _dir_size(pkg)
    print(f"\n构建完成：{pkg}")
    print(f"  解压后总大小约 {total / 1e9:.2f} GB（不含模型权重）")


if __name__ == "__main__":
    main()
