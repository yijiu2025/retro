# -*- coding: utf-8 -*-
"""多步逆合成搜索：用 RetroChimera + syntheseus Retro* 搜出完整合成路径。

用法（在仓库根执行）:
    python scripts/search.py "目标SMILES" [最大模型调用次数] [时间限制秒] [最大深度]

示例:
    python scripts/search.py "Oc1ccc(OCc2ccccc2)c(Br)c1" 80 150 3

模型目录按「环境变量优先 → 仓库内 models/」推导，不写死盘符：
    RETRO_MODELS_DIR          整个模型根目录
    RETRO_MODEL_PISTACHIO     单独指定 pistachio 模型目录
"""
import os
import sys
import time

# --- 仓库根入 sys.path，让 bootstrap/ 可导入（必须在第三方 import 之前） -----
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bootstrap import envfix  # noqa: F401, E402  # 必须在 torch 之前导入

MODEL_DIR = os.environ.get("RETRO_MODEL_PISTACHIO") or os.path.join(
    os.environ.get("RETRO_MODELS_DIR") or os.path.join(_ROOT, "models"),
    "retrochimera_pistachio",
)


def build_inventory(max_heavy_atoms: int = 10):
    """启发式库存：重原子数 <= N 的分子视为『可购买』，作为搜索终止条件。

    说明：真实场景应替换为供应商目录（CSV/SDF 库存文件），
    例如 syntheseus.search.mol_inventory.SmilesListInventory 加载 ZINC/eMolecules 清单。
    这里用复杂度阈值让搜索能收敛出一个完整合成树。
    """
    from rdkit import Chem
    from syntheseus.search.mol_inventory import BaseMolInventory

    class ComplexityInventory(BaseMolInventory):
        def __init__(self, max_atoms: int):
            self.max_atoms = max_atoms

        def is_purchasable(self, mol) -> bool:
            try:
                m = Chem.MolFromSmiles(mol.smiles)
                return m is not None and m.GetNumHeavyAtoms() <= self.max_atoms
            except Exception:  # noqa: BLE001
                return False

    return ComplexityInventory(max_heavy_atoms)


def parse_routes(graph, max_routes=5):
    """从搜索图中提取结构化的合成路径数据。

    返回: [{steps: [{reactants, product, probability}], ...}, ...]
    """
    from syntheseus.search.analysis.route_extraction import iter_routes_time_order
    from syntheseus.search.graph.and_or import AndNode

    routes = []
    try:
        raw_routes = list(iter_routes_time_order(graph, max_routes=max_routes))
    except Exception as e:  # noqa: BLE001
        print(f"    [警告] 路径提取失败: {e}")
        return []

    for nodes in raw_routes:
        steps = []
        for n in nodes:
            if isinstance(n, AndNode):
                rxn = n.reaction
                try:
                    prob = rxn.metadata.get("probability")
                except Exception:  # noqa: BLE001
                    prob = None
                steps.append(
                    {
                        "product": rxn.product.smiles,
                        "reactants": sorted(m.smiles for m in rxn.reactants),
                        "probability": float(prob) if prob is not None else None,
                    }
                )
        if steps:
            routes.append({"steps": steps, "num_steps": len(steps)})
    return routes


def main() -> int:
    smiles = sys.argv[1] if len(sys.argv) > 1 else "Oc1ccc(OCc2ccccc2)c(Br)c1"
    max_calls = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    time_limit = float(sys.argv[3]) if len(sys.argv) > 3 else 150.0
    max_depth = int(sys.argv[4]) if len(sys.argv) > 4 else 3

    if not os.path.isdir(MODEL_DIR):
        print(f"[错误] 找不到模型目录: {MODEL_DIR}")
        print("       用 RETRO_MODELS_DIR 或 RETRO_MODEL_PISTACHIO 指定（见本文件头部说明）")
        return 2

    from retrochimera import RetroChimeraModel
    from syntheseus import Molecule
    from syntheseus.search.algorithms.best_first.retro_star import RetroStarSearch
    from syntheseus.search.graph.and_or import AndNode, OrNode
    from syntheseus.search.node_evaluation.common import (
        ConstantNodeEvaluator,
        ReactionModelLogProbCost,
    )

    print(f"[1] 加载模型: {MODEL_DIR}")
    t0 = time.time()
    model = RetroChimeraModel(model_dir=MODEL_DIR)
    print(f"    完成 {time.time()-t0:.1f}s, device={model.device}")

    print(f"[2] 目标分子: {smiles}")
    inventory = build_inventory(10)
    print("[3] 库存策略: 重原子数<=10 视为可购买（起点分子）")

    print(f"[4] 搜索参数: max_calls={max_calls}, time_limit={time_limit}s, max_depth={max_depth}")

    print("[5] 运行 Retro* 搜索...")
    # 同 web/model_runtime.py：num_calls() 是**累计**的缓存未命中数（只在 reset() 归零），
    # 所以要传「基线 + 本次预算」。本脚本每次运行都重新加载模型、基线恒为 0，
    # 两种写法等价；写成相对量是为了与常驻模型服务那条路径保持同一口径。
    _calls_base = model.num_calls()
    t1 = time.time()
    searcher = RetroStarSearch(
        reaction_model=model,
        mol_inventory=inventory,
        value_function=ConstantNodeEvaluator(0.0),
        and_node_cost_fn=ReactionModelLogProbCost(),
        limit_reaction_model_calls=_calls_base + max_calls,
        time_limit_s=time_limit,
        max_expansion_depth=max_depth,
    )
    graph, _ = searcher.run_from_mol(Molecule(smiles))
    print(f"    完成 {time.time()-t1:.1f}s")

    or_nodes = [n for n in graph.nodes() if isinstance(n, OrNode)]
    and_nodes = [n for n in graph.nodes() if isinstance(n, AndNode)]
    solved = [n for n in or_nodes if getattr(n, "is_solved", False)]
    print(f"[6] 图规模: 分子节点 {len(or_nodes)}, 反应节点 {len(and_nodes)}, 已解决 {len(solved)}")

    print("[7] 提取合成路径...")
    routes = parse_routes(graph, max_routes=5)
    print(f"    共 {len(routes)} 条路径\n")

    print("=" * 76)
    for ri, route in enumerate(routes, 1):
        print(f"路径 {ri}（{route['num_steps']} 步）:")
        for si, st in enumerate(route["steps"], 1):
            prob = f"  [{100*st['probability']:.1f}%]" if st["probability"] is not None else ""
            print(f"  步骤{si}: {' + '.join(st['reactants'])}")
            print(f"        ==> {st['product']}{prob}")
        print()
    print("=" * 76)

    # 打印 JSON 以便前端对接
    import json

    print("\nJSON 格式（供前端使用）:")
    print(json.dumps({"target": smiles, "routes": routes}, ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
