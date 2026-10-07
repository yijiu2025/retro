# -*- coding: utf-8 -*-
"""RetroChimera 安装验证脚本：加载模型并对示例分子做逆合成预测。

用法（在仓库根执行）:
    python scripts/check_install.py [模型目录] [返回结果数]

示例:
    python scripts/check_install.py
    python scripts/check_install.py <仓库根>\\models\\retrochimera_pistachio 3

不传模型目录时按「环境变量优先 → 仓库内 models/」推导：
    RETRO_MODELS_DIR          整个模型根目录
    RETRO_MODEL_PISTACHIO     单独指定 pistachio 模型目录
"""
import os
import sys
import time

# 仓库根入 sys.path，让 bootstrap/ 可导入（envfix 必须在第三方 import 之前）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bootstrap import envfix  # noqa: F401, E402

_DEFAULT_MODEL_DIR = os.environ.get("RETRO_MODEL_PISTACHIO") or os.path.join(
    os.environ.get("RETRO_MODELS_DIR") or os.path.join(_ROOT, "models"),
    "retrochimera_pistachio",
)


def main() -> int:
    model_dir = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_MODEL_DIR
    num_results = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    if not os.path.isdir(model_dir):
        print(f"[错误] 找不到模型目录: {model_dir}")
        print("       9GB 权重不入库，换机需手动放置；或用 RETRO_MODELS_DIR 指定")
        return 2

    print(f"[1/3] 加载模型: {model_dir}")
    t0 = time.time()

    from retrochimera import RetroChimeraModel
    from syntheseus import Molecule

    model = RetroChimeraModel(model_dir=model_dir)
    print(f"      加载完成，耗时 {time.time() - t0:.1f}s, device = {model.device}")

    print(f"[2/3] 对示例分子进行逆合成预测 (num_results={num_results})")
    mol = Molecule("Oc1ccc(OCc2ccccc2)c(Br)c1")  # 示例分子（阿司匹林类似物）
    t1 = time.time()
    predictions = model([mol], num_results=num_results)
    print(f"      推理完成，耗时 {time.time() - t1:.1f}s")

    print("[3/3] 预测结果（前体分子，括号内为概率）:")
    for p in predictions[0]:
        print(f"  {p}  ({100. * p.metadata['probability']:.2f}%)")
    print("OK - 安装验证通过")
    return 0


if __name__ == "__main__":
    # Windows 上模型内部使用 multiprocessing 启动规则应用子进程，
    # 必须有 main 保护，否则子进程会重复执行本脚本导致 RuntimeError。
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
