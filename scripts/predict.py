# -*- coding: utf-8 -*-
"""RetroChimera 逆合成预测 —— 单步预测 CLI（带置信度分级 + 生成反应结构图）

用法（在仓库根执行）:
    python scripts/predict.py "目标分子SMILES" [结果数] [模型名]

示例:
    python scripts/predict.py "Oc1ccc(OCc2ccccc2)c(Br)c1"
    python scripts/predict.py "CC(=O)Oc1ccccc1C(=O)O" 5
    python scripts/predict.py "CC(=O)Oc1ccccc1C(=O)O" 5 uspto50k

输出:
    - 终端打印带编号、概率、原子数的结果表
    - 在 <仓库根>/outputs/ 生成 prediction_1.png / prediction_2.png ... 反应结构图

模型目录按「环境变量优先 → 仓库内 models/」推导，不写死盘符：
    RETRO_MODELS_DIR          整个模型根目录
    RETRO_MODEL_PISTACHIO     单独指定 pistachio 模型目录
    RETRO_MODEL_USPTO50K      单独指定 uspto50k 模型目录
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MODELS_ROOT = os.environ.get("RETRO_MODELS_DIR") or os.path.join(_ROOT, "models")

MODELS = {
    "pistachio": os.environ.get("RETRO_MODEL_PISTACHIO")
    or os.path.join(_MODELS_ROOT, "retrochimera_pistachio"),
    "uspto50k": os.environ.get("RETRO_MODEL_USPTO50K")
    or os.path.join(_MODELS_ROOT, "retrochimera_uspto50k"),
}

# 结构图输出目录：放仓库根的 outputs/（不入库），而不是脚本所在目录
OUTDIR = os.environ.get("RETRO_OUTPUT_DIR") or os.path.join(_ROOT, "outputs")


def draw_reaction(product_smiles: str, precursor_smiles: str, prob: float, idx: int, outdir: str):
    """把「前体 -> 产物」画成一张反应图。"""
    from rdkit import Chem
    from rdkit.Chem import AllChem, Draw
    from rdkit.Chem.Draw import rdMolDraw2D

    rxn_smiles = f"{precursor_smiles}>>{product_smiles}"
    rxn = AllChem.ReactionFromSmarts(rxn_smiles)
    if rxn is None:
        return None

    d = rdMolDraw2D.MolDraw2DCairo(1400, 500)
    opts = d.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = 2

    d.DrawReaction(rxn, highlightByReactant=False)
    d.FinishDrawing()
    path = os.path.join(outdir, f"prediction_{idx}.png")
    with open(path, "wb") as f:
        f.write(d.GetDrawingText())
    return path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    product = sys.argv[1]
    num_results = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    model_key = sys.argv[3] if len(sys.argv) > 3 else "pistachio"
    model_dir = MODELS.get(model_key, model_key)

    if not os.path.isdir(model_dir):
        print(f"[错误] 找不到模型目录: {model_dir}")
        print("       用 RETRO_MODELS_DIR 或 RETRO_MODEL_<KEY> 指定（见本文件头部说明）")
        return 2

    from retrochimera import RetroChimeraModel
    from syntheseus import Molecule

    print("=" * 78)
    print(f"  RetroChimera 逆合成预测  |  模型: {model_key}")
    print("=" * 78)
    print(f"  目标产物: {product}")
    print("-" * 78)

    t0 = time.time()
    model = RetroChimeraModel(model_dir=model_dir)
    print(f"  模型加载完成 ({time.time() - t0:.1f}s, device={model.device})")

    t1 = time.time()
    predictions = model([Molecule(product)], num_results=num_results)
    dt = time.time() - t1
    print(f"  推理完成 ({dt:.1f}s)")
    print("-" * 78)

    os.makedirs(OUTDIR, exist_ok=True)
    saved = []
    for i, p in enumerate(predictions[0], 1):
        prob = p.metadata["probability"]
        smi = str(p)
        bar = "#" * int(prob * 40)
        print(f"\n  [{i}] 概率 {100 * prob:6.2f}%  {bar}")
        if prob > 0.5:
            print("      (高置信度)")
        elif prob > 0.1:
            print("      (中等置信度)")
        else:
            print("      (低置信度，可能是幻觉，需谨慎验证)")
        print(f"      前体: {smi}")
        n_react = len(smi.split("."))
        print(f"      前体分子数: {n_react}")
        try:
            path = draw_reaction(product, smi, prob, i, OUTDIR)
            if path:
                saved.append(path)
        except Exception as e:  # noqa: BLE001
            print(f"      (绘图失败: {e})")

    print("\n" + "=" * 78)
    if saved:
        print("  分子结构图已保存:")
        for p in saved:
            print(f"    {p}")
    print("  提示: 排名靠后的结果幻觉风险高，官方建议单个产物请求不超过 5-10 个结果。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
