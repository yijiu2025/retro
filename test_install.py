# -*- coding: utf-8 -*-
"""RetroChimera 安装验证脚本：加载模型并对示例分子做逆合成预测。

用法:
    python test_install.py [模型目录] [返回结果数]
    python test_install.py F:\\retrochimera\\models\\retrochimera_pistachio 3
"""
import sys
import time


def main() -> int:
    model_dir = sys.argv[1] if len(sys.argv) > 1 else r"F:\retrochimera\models\retrochimera_pistachio"
    num_results = int(sys.argv[2]) if len(sys.argv) > 2 else 3

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
