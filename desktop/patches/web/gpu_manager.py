# -*- coding: utf-8 -*-
"""GPU 加速包（cu121）管理 —— 「后面再装、装完可切」的落点。

设计要点
--------
1. **默认包只有 CPU 版 torch**（约 1GB）。cu121 版单独约 2GB，按需下载，
   装进 `data/gpu-pkgs/`，由 `sitecustomize.py` 在解释器启动时**前置**到
   sys.path —— 普通（非冻结）解释器的路径查找天然支持这种「影子覆盖」，
   不需要重打包、不需要动已装好的 CPU 版。
2. **CPU / GPU 可切**：切的是 `data/config.json` 的 `compute` 字段
   （auto / cpu / gpu），下次模型服务进程启动时生效；前端切完点「重启模型服务」。
3. ** wheels 先下后装**：先从官方索引页解析出确切文件名 → 断点续传下载到
   `data/wheels-gpu/` → `pip install --no-index --no-deps --target` 离线安装。
   分两步是为了拿到**真实字节进度**（pip 自己的输出没法驱动进度条），
   也为了下载中断后不必重新解析。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urljoin

import config_store
from downloader import DownloadCanceled, resumable_download

# --------------------------------------------------------------------------
# wheel 规格表（版本与 retro/web/requirements.txt 的实测组合保持一致）
# --------------------------------------------------------------------------
#: torch / torchvision：PyTorch 官方 cu121 索引（每包一个自动索引页）
_TORCH_INDEX = "https://download.pytorch.org/whl/cu121"
#: PyG 家族：官方 wheel 目录页（目录名 torch-2.2.0 是上游笔误，照抄即可）
_PYG_INDEX = "https://data.pyg.org/whl/torch-2.2.0+cu121.html"

WHEEL_SPECS = [
    {
        "name": "torch",
        "page": f"{_TORCH_INDEX}/torch/",
        #: 锁精确版本 —— cu121 索引页上还有 2.3/2.4 等新版，不能取最大
        "pin": "2.2.2+cu121",
    },
    {
        "name": "torchvision",
        "page": f"{_TORCH_INDEX}/torchvision/",
        "pin": "0.17.2+cu121",
    },
    # PyG 家族：官方 wheel 目录页，取与 torch 2.2 匹配的最新版
    {"name": "torch_scatter", "page": _PYG_INDEX, "pin": ""},
    {"name": "torch_sparse", "page": _PYG_INDEX, "pin": ""},
    {"name": "torch_cluster", "page": _PYG_INDEX, "pin": ""},
]

#: 安装完成后的粗略体积（UI 先显示估值，真实值以下载字节为准）
ESTIMATED_BYTES = 2_200_000_000

_state_path = os.path.join(config_store.DATA_DIR, "gpu", "state.json")
_lock = threading.Lock()
_state: dict = {"phase": "idle", "error": "", "wheels": [], "done": 0, "total": 0,
                "bytes_per_sec": 0, "eta_sec": 0, "updated_at": 0}
_cancel: threading.Event | None = None


def _persist() -> None:
    try:
        os.makedirs(os.path.dirname(_state_path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_state_path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, ensure_ascii=False)
        os.replace(tmp, _state_path)
    except OSError:
        pass


def _load() -> None:
    global _state
    try:
        with open(_state_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("wheels", [])
            if data.get("phase") in ("resolving", "downloading", "installing"):
                data["phase"] = "interrupted"
            _state = data
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# 索引解析
# --------------------------------------------------------------------------
def _fetch(url: str, timeout: int = 30) -> str:
    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def resolve_wheels() -> list:
    """从官方索引页解析出确切 wheel 文件名与 URL。

    返回 [{name, version, file, url, bytes}]；bytes 可能为 0（页面不给大小，
    下载时从 Content-Length 补）。

    🔴 解析方式：先抓页面上**所有** `href="*.whl"`，再按
    「包名 + cp39 + win_amd64」过滤。不能直接对整页 HTML 跑包名正则 ——
    PyG 的目录页锚文本里也带完整包名（`<a href="x.whl">x</a>`），
    贪婪匹配会跨引号把 href 和锚文本拼成一个"文件名"，URL 全是乱的
    （这个坑踩过：解析出的 URL 里带 `">torch_scatter-` 字样）。
    """
    pages: dict[str, str] = {}
    out = []
    for spec in WHEEL_SPECS:
        if spec["page"] not in pages:
            pages[spec["page"]] = _fetch(spec["page"])
        html = pages[spec["page"]]
        cands = []
        # 🔴 两种索引页格式都要兼容：
        #   · PyTorch 自动索引：href 是**绝对 URL**且带 `#sha256=...` 后缀，
        #     所以抓取正则必须把 `#` 排除掉（文件名里不会有 #）
        #   · PyG 目录页：href 是**相对路径**且带一层 `torch-2.2.0%2Bcu121/` 子目录
        # 🔴 版本与平台标签之间的 `+` 在 URL 里是 `%2B`（本地版本段），
        #    正则必须两种都认。
        for href in re.findall(r'href="([^"#]+\.whl)', html):
            fname = href.split("#")[0].split("/")[-1]
            m = re.match(
                rf'^{re.escape(spec["name"])}-(\d+(?:\.\d+)*)((?:\+|%2B)[^-]+)?'
                r"-cp39-cp39-win_amd64\.whl$",
                fname,
            )
            if not m:
                continue
            ver = (m.group(1) + (m.group(2) or "")).replace("%2B", "+")
            cands.append((fname, ver, href))
        if not cands:
            raise RuntimeError(f"索引页没找到 {spec['name']} 的 cp39/win_amd64 wheel：{spec['page']}")
        if spec["pin"]:
            picked = next((c for c in cands if c[1] == spec["pin"]), None)
            if picked is None:
                raise RuntimeError(
                    f"索引页没找到 {spec['name']}=={spec['pin']}（cp39/win_amd64）"
                    f"—— 官方可能下架了该版本"
                )
        else:
            picked = max(cands, key=lambda c: [int(x) for x in c[1].split("+")[0].split(".")])
        fname, ver, href = picked
        # URL 用页面上的原始 href（+ 已编码成 %2B，服务端按此解析）；
        # 落盘文件名用解码后的（wheel 本地版本段的 + 是合法字符）
        url = urljoin(spec["page"], href.split("#")[0])
        out.append({"name": spec["name"], "version": ver,
                    "file": fname.replace("%2B", "+"), "url": url, "bytes": 0})
    return out


# --------------------------------------------------------------------------
# 状态查询
# --------------------------------------------------------------------------
def status() -> dict:
    # 🔴 不在此处 _load()：同 model_download，轮询重载会把安装线程的
    #    实时进度换成磁盘旧快照，还被 reconcile 成 interrupted。
    #    磁盘状态只在导入时加载一次（见文件末尾 _load()）。
    st = dict(_state)
    st["installed"] = config_store.gpu_pkgs_installed()
    st["installed_bytes"] = _dir_size(config_store.GPU_PKGS_DIR) if st["installed"] else 0
    st["estimated_bytes"] = ESTIMATED_BYTES
    st["compute"] = config_store.compute_mode()
    st["gpu_pkgs_dir"] = config_store.GPU_PKGS_DIR
    return st


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def backend_info() -> dict:
    """模型服务进程实际在用的后端（sitecustomize 激活 GPU 包时写盘）。"""
    path = os.path.join(config_store.DATA_DIR, "model-service", "backend.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"gpu_active": False, "reason": "模型服务尚未启动"}


def probe() -> dict:
    """探测本机 GPU 是否真的可用（强制走 GPU 包起一个短命进程 import torch）。"""
    if not config_store.gpu_pkgs_installed():
        return {"gpu_installed": False, "cuda_available": False,
                "reason": "GPU 加速包未安装"}
    env = dict(os.environ)
    env["RETRO_COMPUTE_FORCE"] = "gpu"
    code = (
        "import torch,sys;"
        "print(torch.__version__);"
        "print(torch.cuda.is_available());"
        "print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
    )
    try:
        r = subprocess.run(
            [config_store.PYTHON_EXE, "-c", code],
            capture_output=True, text=True, timeout=180, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if r.returncode != 0 or len(lines) < 2:
            return {"gpu_installed": True, "cuda_available": False,
                    "reason": (r.stderr or r.stdout or "探测失败").strip()[-400:]}
        return {"gpu_installed": True, "cuda_available": lines[1] == "True",
                "torch_version": lines[0], "device": lines[2] if len(lines) > 2 else "",
                "reason": "" if lines[1] == "True" else "未检测到可用的 NVIDIA 显卡/驱动"}
    except Exception as e:  # noqa: BLE001
        return {"gpu_installed": True, "cuda_available": False,
                "reason": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# 安装 / 取消 / 卸载
# --------------------------------------------------------------------------
def is_busy() -> bool:
    return _state.get("phase") in ("resolving", "downloading", "installing")


def install() -> dict:
    global _cancel
    if is_busy():
        raise RuntimeError("正在安装中")
    if config_store.gpu_pkgs_installed():
        raise RuntimeError("GPU 加速包已安装（如需重装请先卸载）")
    _cancel = threading.Event()
    t = threading.Thread(target=_install_work, name="gpu-install", daemon=True)
    t.start()
    return status()


def cancel() -> None:
    if _cancel is not None:
        _cancel.set()


def uninstall() -> None:
    if is_busy():
        raise RuntimeError("正在安装中，先取消再卸载")
    import shutil

    if os.path.isdir(config_store.GPU_PKGS_DIR):
        shutil.rmtree(config_store.GPU_PKGS_DIR)
    if os.path.isdir(config_store.GPU_WHEELS_DIR):
        shutil.rmtree(config_store.GPU_WHEELS_DIR)
    with _lock:
        _state.update({"phase": "idle", "error": "", "wheels": [], "done": 0, "total": 0,
                       "updated_at": time.time()})
        _persist()


def _install_work() -> None:
    started = time.time()
    try:
        with _lock:
            _state.update({"phase": "resolving", "error": "", "wheels": [], "done": 0,
                           "total": 0, "updated_at": time.time()})
            _persist()

        # 1) 解析索引页 → 确切 wheel 清单
        wheels = resolve_wheels()
        total_known = 0
        with _lock:
            _state["wheels"] = [{**w, "done": 0, "state": "pending"} for w in wheels]
            _persist()

        # 2) 逐个下载（断点续传）
        os.makedirs(config_store.GPU_WHEELS_DIR, exist_ok=True)
        for w in _state["wheels"]:
            if _cancel.is_set():
                raise DownloadCanceled()
            dest = os.path.join(config_store.GPU_WHEELS_DIR, w["file"])
            w["state"] = "downloading"
            last = [0.0, 0]

            def on_progress(done: int, total: int, _w=w, _last=last):
                now = time.time()
                if total:
                    _w["total"] = total
                    total_known = total
                _w["done"] = done
                if now - _last[0] >= 0.5:
                    _last[0], _last[1] = now, done
                agg_done = sum(x.get("done", 0) for x in _state["wheels"])
                agg_total = sum(x.get("total", 0) or 0 for x in _state["wheels"])
                with _lock:
                    _state.update({
                        "phase": "downloading", "done": agg_done, "total": agg_total,
                        "bytes_per_sec": int((agg_done) / max(0.1, now - started)),
                        "eta_sec": int((agg_total - agg_done) / max(1, agg_done / max(0.1, now - started))),
                        "updated_at": now,
                    })
                    _persist()

            resumable_download(w["url"], dest, on_progress=on_progress,
                               cancel_event=_cancel)
            w["state"] = "downloaded"
            with _lock:
                _persist()

        # 3) 离线安装到 gpu-pkgs（--no-deps：其余依赖 site-packages 里都有）
        with _lock:
            _state.update({"phase": "installing", "updated_at": time.time()})
            _persist()
        wheel_paths = [os.path.join(config_store.GPU_WHEELS_DIR, w["file"])
                       for w in _state["wheels"]]
        cmd = [
            config_store.PYTHON_EXE, "-m", "pip", "install",
            "--no-index", "--no-deps", "--no-warn-script-location",
            "--target", config_store.GPU_PKGS_DIR, *wheel_paths,
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            raise RuntimeError(f"pip 安装失败：{(r.stderr or r.stdout)[-500:]}")

        with _lock:
            _state.update({"phase": "done", "error": "", "updated_at": time.time()})
            _persist()
    except DownloadCanceled:
        with _lock:
            _state.update({"phase": "canceled", "error": "已取消（已下的 wheel 保留，重装即续传）",
                           "updated_at": time.time()})
            _persist()
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state.update({"phase": "error", "error": f"{type(e).__name__}: {e}",
                           "updated_at": time.time()})
            _persist()


_load()
