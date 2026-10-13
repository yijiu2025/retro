#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""烤制 GPU(cu121) 独立包：把 CUDA 12.1 版 PyTorch 全家桶装进 data/gpu-pkgs。

复用桌面模块的 resolve_wheels + resumable_download + pip --target 安装
（与软件内「下载并安装 GPU 加速包」完全同一条路径，只是这里同步跑完、不落状态机）。

用法：
  python bake_gpu.py --pkg <解压后的包根> [--config-gpu]
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", required=True, help="解压后的 RetroWeb 包根目录")
    ap.add_argument("--config-gpu", action="store_true",
                    help="把 data/config.json 的 compute 设为 gpu（默认 auto）")
    args = ap.parse_args()

    pkg = os.path.abspath(args.pkg)
    py = os.path.join(pkg, "python39", "python.exe")
    if not os.path.isfile(py):
        print(f"ERROR: 找不到便携 Python：{py}")
        return 2
    if not os.path.isdir(os.path.join(pkg, "_internal", "web")):
        print(f"ERROR: 不是 RetroWeb 包（缺 _internal/web）：{pkg}")
        return 2

    # 在包内运行：cwd=pkg 让 config_store 按 __file__ 推出正确的包根。
    # sys.path 注入 _internal/web 以导入 gpu_manager / config_store。
    inner = r'''
import os, sys, subprocess
sys.path.insert(0, os.path.join(os.getcwd(), "_internal", "web"))
import gpu_manager as g
import config_store as cs

print("== 解析 cu121 wheel 索引 ==")
wheels = g.resolve_wheels()
print("wheels:", [(w["name"], w["version"]) for w in wheels])

os.makedirs(cs.GPU_WHEELS_DIR, exist_ok=True)
for w in wheels:
    dest = os.path.join(cs.GPU_WHEELS_DIR, w["file"])
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        print("  已存在，跳过:", w["file"])
        continue
    print("== 下载", w["file"], "==", w["url"])
    g.resumable_download(w["url"], dest)
    print("  完成:", w["file"])

print("== 离线安装到 data/gpu-pkgs ==")
wheel_paths = [os.path.join(cs.GPU_WHEELS_DIR, w["file"]) for w in wheels]
r = subprocess.run(
    [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
     "--no-warn-script-location", "--target", cs.GPU_PKGS_DIR, *wheel_paths],
    capture_output=True, text=True,
)
print(r.stdout[-2500:])
print("PIP_STDERR:", r.stderr[-2500:])
print("INSTALL_RC", r.returncode)

torch_ok = os.path.isfile(os.path.join(cs.GPU_PKGS_DIR, "torch", "__init__.py"))
print("TORCH_IN_GPU_PKGS", torch_ok)
if not torch_ok:
    sys.exit("INSTALL_MISSING_TORCH")
'''

    env = dict(os.environ)  # 继承代理等
    env["RETRO_COMPUTE_FORCE"] = "gpu"  # 让安装期任何 import 走 gpu 视角（无害）
    print(f"== 用包内 Python 烤制 GPU 包：{py} ==")
    r = subprocess.run([py, "-c", inner], cwd=pkg, env=env,
                       capture_output=False, text=True)
    if r.returncode != 0:
        print(f"\nBAKE FAILED (rc={r.returncode})")
        return 1

    # 写配置
    try:
        import json
        cfg_path = os.path.join(pkg, "data", "config.json")
        cfg = {}
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        cfg["compute"] = "gpu" if args.config_gpu else "auto"
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        print(f"== 已写 data/config.json: compute={cfg['compute']} ==")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 写配置失败（不影响 GPU 包本身）：{e}")

    print("\nBAKE OK：data/gpu-pkgs 已就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
