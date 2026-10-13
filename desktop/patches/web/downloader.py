# -*- coding: utf-8 -*-
"""可断点续传的下载器 —— 模型权重与 GPU wheels 共用。

为什么不用 `scripts/download_model.py` 里的 curl 方案：
那脚本是**命令行**工具，面向开发者；桌面版要把进度实时显示在设置页里，
还得能取消，curl 的子进程管道做不到细粒度回调。这里用 requests 流式下载，
语义与那脚本保持一致（半截文件留下、重跑同一条任务即续传）。
"""
from __future__ import annotations

import os
import threading


class DownloadCanceled(Exception):
    """用户点了「取消」。"""


def resumable_download(
    url: str,
    dest: str,
    *,
    total_hint: int = 0,
    on_progress=None,
    cancel_event: threading.Event | None = None,
    chunk_size: int = 1 << 20,
    timeout: int = 60,
) -> int:
    """下载 url 到 dest，返回最终字节数。

    · 断点续传：dest 已存在且服务端支持 Range（回 206）时从断点继续；
      服务端忽略 Range（回 200）则从头下（此时旧文件内容直接覆盖）。
    · on_progress(done, total)：每写一个 chunk 回调一次（total 可能为 0 表示未知）。
    · cancel_event：置位后抛 DownloadCanceled（半截文件保留，下次续传）。
    """
    import requests  # 延迟导入：本模块被 API 进程加载，但不该为此拖慢启动

    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    done = have
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    with requests.get(url, stream=True, headers=headers, timeout=timeout) as resp:
        if resp.status_code == 206:
            mode, done = "ab", have
        elif resp.status_code == 200:
            mode, done = "wb", 0
        else:
            raise RuntimeError(f"HTTP {resp.status_code}（{url}）")
        total = int(resp.headers.get("Content-Length") or 0) + done or int(total_hint or 0)
        with open(dest, mode) as fh:
            for chunk in resp.iter_content(chunk_size):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCanceled()
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if on_progress is not None:
                    on_progress(done, total)
    return done
