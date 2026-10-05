# -*- coding: utf-8 -*-
"""一体化修复+测试 v2：以导入探测为准，坏哪个修哪个，循环直至通过，最后跑推理测试。"""
import importlib
import json
import os
import subprocess
import sys

PY = sys.executable

# 推理必需的模块 -> pip 包名映射
MODULES = {
    "numpy": "numpy", "pandas": "pandas", "scipy": "scipy",
    "torch": "torch", "torchvision": "torchvision",
    "torch_geometric": "torch-geometric",
    "torch_scatter": "torch-scatter", "torch_sparse": "torch-sparse",
    "torch_cluster": "torch-cluster",
    "pytorch_lightning": "pytorch-lightning",
    "lightning_fabric": "lightning-fabric",
    "lightning_utilities": "lightning-utilities",
    "torchmetrics": "torchmetrics",
    "omegaconf": "omegaconf", "antlr4": "antlr4-python3-runtime",
    "h5py": "h5py", "rdkit": "rdkit", "rdchiral": "rdchiral",
    "tqdm": "tqdm", "joblib": "joblib", "more_itertools": "more-itertools",
    "pebble": "pebble", "wandb": "wandb", "requests": "requests",
    "yaml": "PyYAML", "pytz": "pytz", "six": "six", "colorama": "colorama",
    "markupsafe": "MarkupSafe", "jinja2": "Jinja2", "click": "click",
    "typing_extensions": "typing-extensions", "packaging": "packaging",
    "filelock": "filelock", "fsspec": "fsspec", "sympy": "sympy",
    "networkx": "networkx", "syntheseus": "syntheseus",
    "syntheseus_graphium": "syntheseus-graphium",
    "retrochimera": "retrochimera",
}


def installed_version(pkg):
    r = subprocess.run(
        [PY, "-m", "pip", "show", pkg], capture_output=True, text=True, timeout=120
    )
    for line in (r.stdout or "").splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def probe_imports():
    """逐个导入，返回失败的模块名列表。"""
    failed = []
    for mod in MODULES:
        try:
            importlib.import_module(mod)
        except BaseException as e:  # noqa: BLE001
            failed.append(mod)
    return failed


def reinstall(pkg):
    ver = installed_version(pkg)
    spec = f"{pkg}=={ver}" if ver else pkg
    index = None
    if pkg in ("torch", "torchvision"):
        index = "https://download.pytorch.org/whl/cu121"
    cmd = [PY, "-m", "pip", "install", "--no-deps", "--force-reinstall", "--quiet", spec]
    if index:
        cmd += ["--index-url", index]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    ok = r.returncode == 0
    print(f"  重装 {spec}: {'OK' if ok else 'FAILED ' + (r.stderr or r.stdout).strip()[-200:]}")
    return ok


def main():
    max_rounds = 6
    for round_no in range(1, max_rounds + 1):
        failed = probe_imports()
        print(f"[轮次 {round_no}] 导入失败 {len(failed)} 个: {failed}")
        if not failed:
            break
        for mod in failed:
            pkg = MODULES[mod]
            try:
                reinstall(pkg)
            except Exception as e:  # noqa: BLE001
                print(f"  重装 {pkg} 异常: {e}")

    # 最终确认
    failed = probe_imports()
    if failed:
        print(f"仍失败: {failed} —— 放弃")
        return 1
    print("所有关键模块导入通过!\n")

    print("=== 开始推理测试 ===")
    os.chdir(r"F:\retrochimera")
    # 用子进程独立运行测试脚本，避免模型内部 multiprocessing 与 exec 冲突
    r = subprocess.run(
        [PY, r"F:\retrochimera\test_install.py",
         r"F:\retrochimera\models\retrochimera_pistachio", "3"],
        cwd=r"F:\retrochimera",
    )
    return r.returncode


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
