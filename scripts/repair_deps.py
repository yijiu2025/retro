# -*- coding: utf-8 -*-
"""环境自检 + 自修复：以「逐个导入探测」为准，坏哪个重装哪个，循环到通过，最后跑推理冒烟测试。

用法（在仓库根执行）:
    python scripts/repair_deps.py

流程:
    1. 逐个 import 推理必需的模块，收集失败的
    2. 失败项按同名版本 `pip install --force-reinstall --no-deps` 重装（torch 走 cu121 源）
    3. 最多 6 轮，直到全部通过
    4. 通过后以子进程方式跑 scripts/check_install.py 做真实推理验证

🔴 这里**故意不导入 bootstrap.envfix** —— 自检要暴露环境的真实状态，
   而不是先替它把 DLL 路径补好。
"""
import importlib
import os
import subprocess
import sys

PY = sys.executable

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
        except BaseException:  # noqa: BLE001
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
    os.chdir(_ROOT)
    # 用子进程独立运行测试脚本，避免模型内部 multiprocessing 与 exec 冲突
    smoke = os.path.join(_ROOT, "scripts", "check_install.py")
    r = subprocess.run([PY, smoke, "3"], cwd=_ROOT)
    return r.returncode


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
