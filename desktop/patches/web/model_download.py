# -*- coding: utf-8 -*-
"""模型下载管理 —— 桌面版「模型设置页」的后端。

与 `scripts/download_model.py` 的关系：那脚本是**开发者 CLI**（一条命令装模型），
本模块是**应用内服务**（进度 / 取消 / 状态查询 / 装完即用）。两者的注册表
（figshare 文件 id、期望字节数、解压规则）单一来源在 `model_runtime` +
`scripts/download_model.py` 的 `registry()` / `extract()` / `verify()`，
这里只做编排，不重复定义模型表。
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import time

import config_store
from downloader import DownloadCanceled, resumable_download

# scripts/download_model.py 按路径加载（scripts/ 不是包）
_DL_MOD = None


def _download_module():
    global _DL_MOD
    if _DL_MOD is None:
        path = os.path.join(config_store.SCRIPTS_DIR, "download_model.py")
        spec = importlib.util.spec_from_file_location("_retro_download_model", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _DL_MOD = mod
    return _DL_MOD


def _state_path() -> str:
    return os.path.join(config_store.DATA_DIR, "downloads", "state.json")


_lock = threading.Lock()
#: key -> 运行态（含进度）。持久化到 data/downloads/state.json，刷新页面不丢。
_state: dict[str, dict] = {}
_threads: dict[str, threading.Thread] = {}
_cancels: dict[str, threading.Event] = {}


def _persist() -> None:
    try:
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_state_path()), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_state, fh, ensure_ascii=False)
        os.replace(tmp, _state_path())
    except OSError:
        pass  # 状态文件写不了不致命：内存态照样驱动 UI


def _load_state() -> None:
    global _state
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _state = data
    except (OSError, ValueError):
        _state = {}
    # 上次异常退出的「下载中」一律按中断处理（zip 留着，下次续传）
    for st in _state.values():
        if st.get("phase") in ("downloading", "extracting", "queued"):
            st["phase"] = "interrupted"


def _entry(key: str) -> dict:
    if key not in _state:
        _state[key] = {"key": key, "phase": "idle", "done": 0, "total": 0, "error": "", "updated_at": 0}
    return _state[key]


def _fmt_speed(done: int, total: int, started: float) -> dict:
    elapsed = max(0.1, time.time() - started)
    return {
        "bytes_per_sec": int(done / elapsed),
        "eta_sec": int((total - done) / (done / elapsed)) if total and done else 0,
    }


# --------------------------------------------------------------------------
# 注册表（模型表 + 正向模型表，形状与前端设置页对齐）
# --------------------------------------------------------------------------
def registry() -> dict:
    """key -> {label, path, figshare_file, zip_bytes, kind, installed}。"""
    mod = _download_module()
    out = {}
    for key, info in mod.registry().items():
        out[key] = {**info, "installed": mod.is_present(info)}
    return out


def status() -> list:
    """全部模型的下载状态（含静态信息）。设置页只认这一个入口。

    🔴 这里**不能**调 `_load_state()`：状态由本进程独占（Flask 单进程 +
       下载线程同进程），每次轮询都从磁盘重载会把线程正在写的内存态
       换成旧快照，随后 `_load_state` 的 reconcile 还会把 downloading
       误判成 interrupted（实测：模型下完装完，UI 却永远停在 0% 中断）。
       磁盘状态只在模块导入时加载一次（断点续传的恢复入口）。
    """
    rows = []
    for key, info in registry().items():
        st = dict(_entry(key))
        row = {
            "key": key,
            "label": info["label"],
            "kind": info["kind"],
            "path": info["path"],
            "zip_bytes": int(info.get("zip_bytes") or 0),
            "installed": bool(info["installed"]),
        }
        row.update(st)
        rows.append(row)
    return rows


def is_busy(key: str) -> bool:
    return _entry(key).get("phase") in ("queued", "downloading", "extracting")


# --------------------------------------------------------------------------
# 下载 / 取消 / 删除
# --------------------------------------------------------------------------
def start(key: str) -> dict:
    reg = registry()
    if key not in reg:
        raise KeyError(f"未知模型: {key}")
    info = reg[key]
    if is_busy(key):
        raise RuntimeError("该模型正在下载中")
    if info["installed"]:
        raise RuntimeError("模型已安装（如需重下请先删除）")

    ev = threading.Event()
    _cancels[key] = ev
    _entry(key).update(
        {"phase": "queued", "done": 0, "total": int(info.get("zip_bytes") or 0),
         "error": "", "updated_at": time.time()}
    )
    _persist()

    t = threading.Thread(target=_work, args=(key,), name=f"dl-{key}", daemon=True)
    _threads[key] = t
    t.start()
    return _entry(key)


def cancel(key: str) -> None:
    ev = _cancels.get(key)
    if ev is not None:
        ev.set()


def remove(key: str) -> None:
    """删除已安装的模型目录（用户主动释放磁盘空间）。"""
    reg = registry()
    if key not in reg:
        raise KeyError(f"未知模型: {key}")
    if is_busy(key):
        raise RuntimeError("正在下载中，先取消再删除")
    import shutil

    path = reg[key]["path"]
    if os.path.isdir(path):
        shutil.rmtree(path)
    _entry(key).update({"phase": "idle", "done": 0, "error": "", "updated_at": time.time()})
    _persist()


def _work(key: str) -> None:
    mod = _download_module()
    reg = registry()
    info = reg[key]
    st = _entry(key)
    started = time.time()
    last = [0.0, 0]  # [时间戳, 字节] 用于算瞬时速度

    def on_progress(done: int, total: int) -> None:
        now = time.time()
        if now - last[0] >= 0.5:
            last[0], last[1] = now, done
        st.update({"phase": "downloading", "done": done, "total": total or st["total"],
                   "updated_at": now, **_fmt_speed(done, total or st["total"], started)})
        if now - st.get("_persisted_at", 0) >= 1.0:
            st["_persisted_at"] = now
            _persist()

    try:
        st.update({"phase": "downloading"})
        url = f"https://ndownloader.figshare.com/files/{info['figshare_file']}"
        dest = mod.zip_path_for(info)
        resumable_download(
            url, dest,
            total_hint=int(info.get("zip_bytes") or 0),
            on_progress=on_progress,
            cancel_event=_cancels.get(key),
        )
        got = os.path.getsize(dest)
        want = int(info.get("zip_bytes") or 0)
        if want and got != want:
            raise RuntimeError(f"下载不完整: {got} / {want}（重下同一模型即可续传）")
        st.update({"phase": "extracting", "done": got, "total": got, "updated_at": time.time()})
        _persist()
        mod.extract(dest, info)
        ok, why = mod.verify(info)
        st.update({"phase": "done" if ok else "error", "error": "" if ok else why,
                   "done": got, "total": got, "updated_at": time.time()})
    except DownloadCanceled:
        st.update({"phase": "canceled", "error": "已取消（zip 已保留，重下即续传）",
                   "updated_at": time.time()})
    except Exception as e:  # noqa: BLE001
        st.update({"phase": "error", "error": f"{type(e).__name__}: {e}",
                   "updated_at": time.time()})
    finally:
        _persist()


_load_state()
