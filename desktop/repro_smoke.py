# -*- coding: utf-8 -*-
"""最小复现：隔离 smoke_test 失败原因（子进程启动 vs urllib 代理）。"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

PKG = r"C:\Users\22701\AppData\Local\Temp\retroweb-build\dist-pkg"
PY = os.path.join(PKG, "python39", "python.exe")
APP = os.path.join(PKG, "_internal", "desktop_app.py")

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
API = f"http://127.0.0.1:{port}"

env = dict(os.environ)
env["RETRO_DESKTOP_HEADLESS"] = "1"
env["RETRO_DESKTOP"] = "1"
env["RETRO_PORT"] = str(port)

out = open(r"C:\Users\22701\AppData\Local\Temp\retroweb-build\child_out.log", "w")
err = open(r"C:\Users\22701\AppData\Local\Temp\retroweb-build\child_err.log", "w")
print(f"启动子进程，端口 {port} ...", flush=True)
PROC = subprocess.Popen([PY, APP], cwd=PKG, env=env, stdout=out, stderr=err,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
time.sleep(20)
print(f"子进程 poll = {PROC.poll()}（None=还在跑）", flush=True)

# 1) 走当前环境（含代理）
try:
    with urllib.request.urlopen(f"{API}/api/desktop/info", timeout=5) as r:
        print("urllib(带代理环境):", r.status, r.read()[:80], flush=True)
except Exception as e:  # noqa: BLE001
    print(f"urllib(带代理环境) 失败: {type(e).__name__}: {e}", flush=True)

# 2) 绕过代理
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{API}/api/desktop/info", timeout=5) as r:
        print("urllib(绕过代理):", r.status, r.read()[:80], flush=True)
except Exception as e:  # noqa: BLE001
    print(f"urllib(绕过代理) 失败: {type(e).__name__}: {e}", flush=True)

# 3) requests 库（与 desktop 下载同款客户端）
try:
    import requests
    r = requests.get(f"{API}/api/desktop/info", timeout=5)
    print("requests:", r.status_code, r.text[:80], flush=True)
except Exception as e:  # noqa: BLE001
    print(f"requests 失败: {type(e).__name__}: {e}", flush=True)

PROC.kill()
out.close()
err.close()
print("---- child_out.log ----", flush=True)
print(open(r"C:\Users\22701\AppData\Local\Temp\retroweb-build\child_out.log", encoding="utf-8", errors="replace").read()[:2000], flush=True)
print("---- child_err.log ----", flush=True)
print(open(r"C:\Users\22701\AppData\Local\Temp\retroweb-build\child_err.log", encoding="utf-8", errors="replace").read()[:2000], flush=True)
