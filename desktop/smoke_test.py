#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面包冒烟测试 —— 不起窗口，验证整条链路。

流程：起桌面进程（headless）→ 等 API → 查端点 → 下载 uspto50k（约285MB）
→ 加载模型 → 跑一次真实预测（阿司匹林）→ 汇报。

用法：python smoke_test.py [包目录]
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

PKG = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "./dist-pkg")
PY = os.path.join(PKG, "python39", "python.exe")
APP = os.path.join(PKG, "_internal", "desktop_app.py")
API = None
PROC = None


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get(path: str, timeout: int = 15):
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(f"{API}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_api(timeout: float = 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            get("/api/desktop/info", timeout=3)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return False


def main() -> int:
    global API, PROC
    # 自选空闲端口：桌面进程支持 RETRO_PORT 固定端口（否则随机无从得知）
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    API = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["RETRO_DESKTOP_HEADLESS"] = "1"
    env["RETRO_DESKTOP"] = "1"
    env["RETRO_PORT"] = str(port)
    log(f"启动桌面进程（headless）：{APP}  API={API}")
    child_log = os.path.join(PKG, "data", "logs", "smoke_child.log")
    os.makedirs(os.path.dirname(child_log), exist_ok=True)
    _out = open(child_log, "w", encoding="utf-8")
    PROC = subprocess.Popen([PY, APP], cwd=PKG, env=env, stdout=_out, stderr=_out,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log(f"子进程日志：{child_log}")
    try:
        if not wait_api():
            log("FAIL: API 60 秒内未就绪")
            return 1
        log("API 就绪")

        info = get("/api/desktop/info")
        log(f"desktop/info: frontend_ok={info['frontend_ok']} models_dir={info['models_dir']}")

        with urllib.request.urlopen(f"{API}/", timeout=10) as r:
            html = r.read().decode("utf-8", "replace")
        assert "<div id=" in html or "<title>" in html, "首页不像 SPA 页面"
        log(f"GET / → HTML {len(html)} 字节（静态托管 OK）")

        with urllib.request.urlopen(f"{API}/settings/models", timeout=10) as r:
            spa = r.read().decode("utf-8", "replace")
        assert spa == html, "SPA fallback 与首页不一致"
        log("GET /settings/models → SPA fallback OK")

        gpu = get("/api/gpu/status")
        log(f"gpu/status: installed={gpu['installed']} compute={gpu['compute']} "
            f"backend={gpu['backend']}")

        # --- 模型：已存在则跳过下载，否则下载 uspto50k（285MB，断点续传）---
        #     交付包本就不带模型（用户进软件后下载 / 手动放）。
        #     冒烟默认不重复下载；传 --download 才真正走一遍下载链路。
        do_download = "--download" in sys.argv
        rows0 = get("/api/models/download/status")["models"]
        row0 = next(r for r in rows0 if r["key"] == "uspto50k")
        if row0.get("installed"):
            log("uspto50k 已在包内（手动放置 / 先前下载），跳过下载")
        elif not do_download:
            log("uspto50k 不在包内且未传 --download，跳过下载（仅验证加载+预测）")
        else:
            try:
                st = post("/api/models/download", {"key": "uspto50k"})
                log(f"开始下载 uspto50k: {st.get('phase')}")
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    log("下载端点返回 409（已在下载/已存在），继续")
                else:
                    raise
            last = -1
            end = time.time() + 3600
            while time.time() < end:
                rows = get("/api/models/download/status")["models"]
                row = next(r for r in rows if r["key"] == "uspto50k")
                pct = int(row["done"] * 100 / row["total"]) if row["total"] else 0
                if pct != last:
                    log(f"  下载 {pct}%  {row['phase']}")
                    last = pct
                if row["phase"] == "done" and row["installed"]:
                    log("uspto50k 下载 + 解压 + 校验完成")
                    break
                if row["phase"] in ("error", "canceled"):
                    log(f"FAIL: 下载失败 {row['error']}")
                    return 1
                time.sleep(3)
            else:
                log("FAIL: 下载超时")
                return 1

        # --- 加载模型 ---
        post("/api/load", {"model": "uspto50k"})
        log("已请求加载 uspto50k，等待 ready……")
        end = time.time() + 600
        ready = False
        while time.time() < end:
            cat = get("/api/models")
            row = next(m for m in cat["models"] if m["key"] == "uspto50k")
            if row.get("state") == "ready":
                ready = True
                break
            if row.get("state") == "failed":
                log(f"FAIL: 模型加载失败 {row.get('error')}")
                return 1
            time.sleep(5)
        if not ready:
            log("FAIL: 模型 10 分钟内未 ready")
            return 1
        log("模型 ready")

        # --- 真实预测（阿司匹林单步逆合成）---
        job = post("/api/jobs", {"kind": "predict", "smiles": "CC(=O)Oc1ccccc1C(=O)O",
                                 "model": "uspto50k", "num_results": 5})
        jid = job.get("job_id") or job.get("id")
        log(f"预测任务 {jid}")
        end = time.time() + 900
        while time.time() < end:
            snap = get(f"/api/jobs/{jid}")
            # jobhub 快照顶层字段是 state：queued/running/succeeded/failed/cancelled
            state = snap.get("state") or snap.get("status")
            if state in ("succeeded", "done", "completed", "finished"):
                result = snap.get("result") or {}
                results = result.get("results") if isinstance(result, dict) else result
                n = len(results) if isinstance(results, list) else "?"
                log(f"PASS: 预测完成，返回 {n} 条候选")
                sample = results[0] if isinstance(results, list) and results else result
                log(f"样例: {json.dumps(sample, ensure_ascii=False)[:200]}")
                return 0
            if state in ("failed", "error", "cancelled", "canceled"):
                log(f"FAIL: 任务 {state}: {json.dumps(snap, ensure_ascii=False)[:400]}")
                return 1
            time.sleep(4)
        log("FAIL: 预测任务超时")
        return 1
    finally:
        if PROC is not None:
            PROC.kill()


if __name__ == "__main__":
    sys.exit(main())
