# -*- coding: utf-8 -*-
"""模型运行时 —— 与 Web 框架无关的纯逻辑（模型定义 + 推理 + 搜索）。

为什么单独一个模块
------------------
模型现在跑在**独立常驻服务**里（`web/model_server.py`，默认 127.0.0.1:8001），
API 服务（`web/app.py`）只通过 HTTP 转接，自己**不再加载模型**。

于是有两份代码都要用到「模型怎么找、怎么加载、怎么推理、怎么搜索」：

    model_server.py  →  真正加载 + 执行
    app.py           →  只读模型清单（给 /api/models 那张状态矩阵）

🔴 所以本模块**绝不能有 Flask 副作用**（不建 app、不注册路由、不起线程）：
   `model_server.py` 要在**单线程阶段** import 它并加载模型，才能安全 spawn
   规则子进程 —— 一旦顶部顺带建了 Flask / 起了线程，Windows 的 spawn 就会死锁
   （这是本项目最贵的一个坑，见 app.py 顶部与 model_server.py 的注释）。

🔴 也绝不能 import app.py：那会把 Flask app、jobhub、llm 全拖进来，子进程
   re-import 时重复执行一遍。
"""
from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)  # 仓库根

# --- 上游源码（core/）挂载兜底 ---------------------------------------------
# 与 app.py 同一套逻辑：正常已 `pip install -e core`，这里再兜一层，
# 避免换机器时只报一句 ModuleNotFoundError 看不出缘由。
_CORE_DIR = os.path.join(PARENT, "core")
if os.path.isdir(_CORE_DIR) and _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)


# --------------------------------------------------------------------------
# 模型目录：**默认按相对位置推导，可用环境变量覆盖**
# --------------------------------------------------------------------------
# 🔴 曾经写死 `F:\retrochimera\models\...` ⇒ 换台机器/换盘符就直接崩，
#    而且报错是"找不到模型文件"，看不出是路径问题。
#    现在的解析顺序（先到先用）：
#      ① 环境变量 RETRO_MODELS_DIR      （整个模型根目录，推荐：换机只改这一处）
#      ② 各模型单独的 RETRO_MODEL_<KEY> （只想挪某一个时用）
#      ③ <仓库根>/models/<key>           （默认：web/ 的上一层）
def models_root() -> str:
    """模型根目录。"""
    return os.environ.get("RETRO_MODELS_DIR") or os.path.join(PARENT, "models")


def model_path(env_key: str, dirname: str) -> str:
    """单个模型的目录：环境变量优先，否则 <模型根>/<dirname>。"""
    return os.environ.get(env_key) or os.path.join(models_root(), dirname)


#: 可用模型表。新增模型只需在这里加一条 + 把目录放进 models/。
#:
#: `figshare_file` 是官方 checkpoint 的 figshare **文件 id**（不是文章 id）——
#: `scripts/download_model.py` 靠它一条命令把缺失的模型拉下来（可断点续传）。
#: 三个官方 checkpoint 分别挂在文章 30591107 / 30601718 / 30597563 下。
MODELS = {
    "pistachio": {
        "path": model_path("RETRO_MODEL_PISTACHIO", "retrochimera_pistachio"),
        "label": "Pistachio（主推，性能最强）",
        "figshare_file": 59468882,
        "zip_bytes": 4213968927,
    },
    "uspto50k": {
        "path": model_path("RETRO_MODEL_USPTO50K", "retrochimera_uspto50k"),
        "label": "USPTO-50K（基准模型，较弱）",
        "figshare_file": 59511926,
        "zip_bytes": 284852815,
    },
    # 🔴 USPTO-FULL 与 Pistachio 同族（都是 USPTO 全量训练），但**模板库小得多**，
    #    所以比 Pistachio 弱、比 USPTO-50K 强，介于两者之间。checkpoint 压缩包
    #    4.6 GB（解压后 ~4.6 GB，含 template_classification + template_localization
    #    两个子模型），本机 models/ 里默认没有 —— 用 scripts/download_model.py 拉。
    "uspto_full": {
        "path": model_path("RETRO_MODEL_USPTO_FULL", "retrochimera_uspto_full"),
        "label": "USPTO-FULL（全量 USPTO，中等）",
        "figshare_file": 59494598,
        "zip_bytes": 4607889148,
    },
}

#: 正向模型表 —— **不参与逆合成搜索的模型选择**，它只给「可行性过滤」当一个判别器：
#: 把逆向模型给出的每条候选反应，交给正向模型算「这些前体真能生成该产物吗」，
#: 不在 top-k 里的候选直接扔掉（syntheseus 的 ForwardReactionFilterModel）。
#:
#: 🔴 为什么单独一张表、不塞进 MODELS：MODELS 里每一行都是「可以当搜索主模型」的
#:    逆向模型，前端那张状态矩阵会把它渲染成一格可选的"预测模型"。正向模型不是
#:    可选项而是**附件**，混进去就会出现「选中 ForwardChimera 去跑逆合成」这种
#:    必然失败的组合。
FORWARD_MODELS = {
    "forward_pistachio": {
        "path": model_path("RETRO_MODEL_FORWARD", "forwardchimera_denovo_pistachio"),
        "label": "ForwardChimera（Pistachio 预训练）",
        # 该 checkpoint 只有一个 835 MB 的压缩包，里面是 smiles_transformer 的
        # combined.ckpt + vocab.txt（目录结构与逆向的 SmilesTransformerModel 同构）。
        "figshare_file": 66654872,
        "zip_bytes": 835317085,
    },
}


def _pick_key(env_name: str, table: dict, default: str) -> str:
    """解析「预加载哪个」：环境变量优先，`none/0/false/off` 表示不预加载。

    🔴 `"none"` 这类字面值必须在这里就解析成空串：从前 model_server 直接用
       `os.environ.get()`，于是 `RETRO_PRELOAD=none` 被当成模型名传下去 →
       「未知模型: none」。
    """
    key = str(os.environ.get(env_name, default)).strip()
    if key.lower() in ("", "none", "0", "false", "off"):
        return ""
    return key if key in table else default


#: 默认预加载哪个逆向模型（可用 RETRO_PRELOAD 覆盖；none/0/false 表示不预加载）
def default_model_key() -> str:
    return _pick_key("RETRO_PRELOAD", MODELS, "pistachio")


#: 默认加载哪个正向模型。🔴 **默认不预加载**（空串）：它只服务于一个可选开关，
#: 而每次加载都要额外吃约 1 GB 内存 —— 没打算用可行性过滤的人不该替它买单。
#: 想让它一启动就绪：`set RETRO_PRELOAD_FORWARD=forward_pistachio`。
def default_forward_key() -> str:
    return _pick_key("RETRO_PRELOAD_FORWARD", FORWARD_MODELS, "")


def forward_models_catalog() -> list:
    """正向模型的静态清单（不触发加载），形状与 models_catalog() 一致。"""
    out = []
    for key, info in FORWARD_MODELS.items():
        path = info["path"]
        exists = os.path.isdir(path) and bool(os.listdir(path))
        out.append(
            {
                "key": key,
                "label": info["label"],
                "path": path,
                "exists": exists,
                "size_bytes": dir_size_bytes(path) if exists else 0,
            }
        )
    return out


def dir_size_bytes(path: str) -> int:
    """目录总大小（用于状态矩阵里显示模型体积）。失败返回 0。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def models_catalog() -> list:
    """静态清单（不触发加载）：每个模型是否存在、多大、路径在哪。

    这是「状态矩阵」的**静态半边**；动态半边（加载到哪一步了）由
    `ModelHost.status()` 与模型服务的 state 文件提供。
    """
    out = []
    for key, info in MODELS.items():
        path = info["path"]
        exists = os.path.isdir(path) and bool(os.listdir(path))
        out.append(
            {
                "key": key,
                "label": info["label"],
                "path": path,
                "exists": exists,
                "size_bytes": dir_size_bytes(path) if exists else 0,
            }
        )
    return out


# --------------------------------------------------------------------------
# 纯算子：分子规范化 / 库存判定 / 价值函数
# --------------------------------------------------------------------------
_canon_cache: dict = {}


def canon_smiles(smi):
    """把 SMILES 规范化成 RDKit canonical 形式；无法解析返回 None。

    为什么必须规范化：搜索过程中模型返回的分子写法，和用户手工输入/从
    ChemDraw 复制的写法经常不同（原子顺序、等价分支的书写顺序都可能不一样），
    直接做字符串比较会导致「指定的起始物料」几乎永远匹配不上，白名单形同虚设。
    """
    if not smi:
        return None
    if smi in _canon_cache:
        return _canon_cache[smi]
    try:
        from rdkit import Chem

        mol = Chem.MolFromSmiles(smi)
        result = Chem.MolToSmiles(mol) if mol is not None else None
    except Exception:  # noqa: BLE001
        result = None
    _canon_cache[smi] = result
    return result


def make_inventory(max_heavy_atoms=10, purchasable=None):
    """构造分子库存判定器。

    max_heavy_atoms: 重原子数不超过该值的分子视为「可购买」。
    purchasable:     额外指定的可购买分子 SMILES 列表。命中即视为可购买
                     （搜索**不再往下拆它**），与上面的重原子规则是「或」关系。
                     比较用 canonical SMILES，因此写法不同也能正确匹配。
    """
    from rdkit import Chem
    from syntheseus.search.mol_inventory import BaseMolInventory

    extra = {c for c in (canon_smiles(s) for s in (purchasable or [])) if c}

    class HeuristicInventory(BaseMolInventory):
        def is_purchasable(self, mol) -> bool:
            if extra:
                canon = canon_smiles(mol.smiles)
                if canon is not None and canon in extra:
                    return True
            try:
                m = Chem.MolFromSmiles(mol.smiles)
                return m is not None and m.GetNumHeavyAtoms() <= max_heavy_atoms
            except Exception:  # noqa: BLE001
                return False

    return HeuristicInventory()


def make_value_function(kind, inventory, max_heavy_atoms):
    """构造 Retro* 的 value function：估计「从该分子出发还需要几步」。

    - "constant"   : 恒为 0。这是 syntheseus 的最简默认值，效果等于关闭启发式
                     引导，搜索退化为纯粹按反应概率（-logP）做 best-first。
    - "complexity" : 按重原子数估计剩余步数——分子越大，通常越需要多拆几步。
                     搜索因此会优先处理「已经接近可购买」的分支，在预算有限时
                     更容易把某一条分支一路走通，而不是把预算平摊在所有分支上。

    该值是「代价估计」，越大表示越不值得优先扩展。启发式以 1 步为下界
    （任何不可购买的分子至少还要 1 步），不会低估，因此不会牺牲找到可行路线的能力。
    """
    from syntheseus.search.node_evaluation.common import ConstantNodeEvaluator

    if kind != "complexity":
        return ConstantNodeEvaluator(0.0)

    from rdkit import Chem
    from syntheseus.search.graph.and_or import OrNode
    from syntheseus.search.node_evaluation.base import NoCacheNodeEvaluator

    atoms_per_step = 5.0  # 每多 5 个重原子，估计多需要 1 步

    class ComplexityValueFunction(NoCacheNodeEvaluator[OrNode]):
        def _evaluate_nodes(self, nodes, graph=None):
            values = []
            for node in nodes:
                mol = getattr(node, "mol", None)
                if mol is None:
                    values.append(0.0)
                    continue
                try:
                    if inventory.is_purchasable(mol):
                        values.append(0.0)
                        continue
                    rd_mol = Chem.MolFromSmiles(mol.smiles)
                    n_atoms = (
                        rd_mol.GetNumHeavyAtoms() if rd_mol is not None else max_heavy_atoms
                    )
                except Exception:  # noqa: BLE001
                    values.append(1.0)
                    continue
                values.append(1.0 + max(0, n_atoms - max_heavy_atoms) / atoms_per_step)
            return values

    return ComplexityValueFunction()


def wrap_with_forward_filter(backward_model, forward_model, top_k: int = 5):
    """把逆向模型包一层「正向可行性过滤」，供搜索使用。

    机制（syntheseus 自带，不是自研）：逆向模型每吐出一条候选反应，就用正向模型
    「这些前体 → 目标产物」再预测一次；只有当产物命中了正向模型 top-k 的输出，
    这条候选才被保留。目的是砍掉**幻觉前体** —— 逆向模型（尤其是样板库小的）经常
    给出化学上根本走不通的前体组合，它们会白白吃掉搜索预算、把树撑歪。

    返回的包装对象是一个 `BackwardReactionModel`，可以直接交给 RetroStarSearch；
    它还额外暴露 `acceptance_rate`（通过率），用来回答「过滤到底滤掉了多少」。

    🔴 两条必须知道的语义：
    ① 构造这个包装对象会**顺带 `backward_model.reset()`**（syntheseus 的
       `FilteredBackwardReactionModel.reset` 就是这么写的）。所以调用方**必须**
       在构造完成之后再去读 `num_calls()` 作为预算基线 —— 反过来读到的会是
       构造前的旧计数，预算立刻算错。
    ② `num_calls()` 是包装层自己的计数，与底层逆向模型各算各的。预算基线取
       **真正交给搜索器的那个对象**（`run_retro_search` 里就是 `search_model`），
       这样开不开过滤都是同一套语义。
    """
    if forward_model is None:
        raise RuntimeError(
            "可行性过滤需要一个正向模型作判据，但当前没有加载任何正向模型。"
            "请在「运行参数 → 拆分约束 → 可行性过滤」里点「加载正向模型」"
            "（或先跑 scripts/download_model.py 把 checkpoint 放好）。"
        )
    from syntheseus.reaction_prediction.filters.forward import ForwardReactionFilterModel
    from syntheseus.reaction_prediction.filters.wrapper import FilteredBackwardReactionModel

    return FilteredBackwardReactionModel(
        backward_model=backward_model,
        filter_models={
            "forward": ForwardReactionFilterModel(forward_model=forward_model, top_k=int(top_k))
        },
    )


# --------------------------------------------------------------------------
# 搜索算法注册表
# --------------------------------------------------------------------------
# 🔴 只登记**不需要任何额外训练产物**就能跑的算法 —— 也就是客户端换个下拉就能切。
#    syntheseus 里还有 MCTS / PDVN-MCTS，但它们要 policy 模型（PDVN 构造里直接
#    `assert self.policy is not None`），缺了产物就起不来，所以不进这张表。
#
#    key -> (展示名, 一句话说明)。后端在这里做**唯一**的合法性判定，
#    非法值一律退回默认算法（`run_retro_search` 里的 `if algorithm not in ...`）。
SEARCH_ALGORITHMS = {
    "retro_star": ("Retro*", "最佳优先：按「剩余步数估计 + 反应代价」挑最有望的节点先展开"),
    "bfs": ("广度优先 BFS", "逐层铺开：不做取舍，覆盖最全，但调用预算吃得很凶"),
}
DEFAULT_SEARCH_ALGORITHM = "retro_star"


def _build_searcher(algorithm, *, reaction_model, mol_inventory, value_fn,
                    max_heavy_atoms, **common):
    """按算法名构造搜索器。各算法**共用同一套** `SearchAlgorithm` 构造参数
    （`limit_reaction_model_calls` / `time_limit_s` / `max_expansion_depth` …），
    差别只在要不要 value function 这类**算法专属**入参 —— 所以这里是一个 switch
    而不是两条搜索流程。

    🔴 为什么必须分开构造、不能统一传 `value_function=...`：
        `SearchAlgorithm.__init__` 收的是 **kwargs 直通 `object.__init__`**，
        多传一个它不认识的 `value_function` 会直接 TypeError。Retro* 那两个专属
        参数来自 `ValueFunctionMixin`，BFS 不吃也不认。
    """
    if algorithm == "bfs":
        from syntheseus.search.algorithms.breadth_first import AndOr_BreadthFirstSearch

        return AndOr_BreadthFirstSearch(
            reaction_model=reaction_model,
            mol_inventory=mol_inventory,
            **common,
        )

    from syntheseus.search.algorithms.best_first.retro_star import RetroStarSearch
    from syntheseus.search.node_evaluation.common import ReactionModelLogProbCost

    return RetroStarSearch(
        reaction_model=reaction_model,
        mol_inventory=mol_inventory,
        value_function=make_value_function(value_fn, mol_inventory, max_heavy_atoms),
        and_node_cost_fn=ReactionModelLogProbCost(),
        **common,
    )


def run_retro_search(model, *, smiles, max_routes=5, max_calls=60, time_limit=120.0,
                     max_depth=3, max_heavy_atoms=10, value_fn="complexity",
                     purchasable=None, required_materials=None,
                     use_forward_filter=False, forward_top_k=5, forward_model=None,
                     algorithm="retro_star"):
    """按 `algorithm` 跑一次多步搜索并提取路线（**纯函数，只依赖传入的 model 对象**）。

    🔴 为什么接受「model 对象」而不是 model_key：
       搜索任务要在**模型服务进程**里跑（复用那里已加载的模型，不在别处
       再载一份）。同步 /api/search 走 HTTP 转发后同样落到这里。
       两者共用这一份逻辑，避免「异步任务和同步接口各写一遍、结果不一致」。

    use_forward_filter / forward_top_k / forward_model：
       见 `wrap_with_forward_filter`。关上时（默认）`forward_model` 传什么都会被忽略。

    algorithm：
       搜索算法，见 `SEARCH_ALGORITHMS`。两种算法共用同一套 `SearchAlgorithm`
       构造参数（详见 `_build_searcher`），差异只在要不要 value function /
       AND 节点代价，所以这里是「选一个构造子」而不是写两条搜索流程。

    返回 dict（不含 jsonify 包装）：{ok, target, model, routes, starting_materials,
    purchasable, required_materials, filtered_out, algorithm, value_fn, forward_filter,
    forward_top_k, acceptance_rate, stats, load_seconds, search_seconds}。
    异常由调用方决定如何呈现。
    """
    from syntheseus import Molecule
    from syntheseus.search.analysis.route_extraction import iter_routes_time_order
    from syntheseus.search.graph.and_or import AndNode, OrNode

    # 参数保护（与 /api/search 同一套边界，别让服务端收到越界值）
    max_routes = max(1, min(int(max_routes), 20))
    max_calls = max(5, min(int(max_calls), 400))
    time_limit = max(10.0, min(float(time_limit), 600.0))
    max_depth = max(1, min(int(max_depth), 8))
    max_heavy_atoms = max(1, min(int(max_heavy_atoms), 40))
    forward_top_k = max(1, min(int(forward_top_k), 50))
    if algorithm not in SEARCH_ALGORITHMS:
        algorithm = DEFAULT_SEARCH_ALGORITHM
    if value_fn not in ("constant", "complexity"):
        value_fn = "complexity"

    inventory = make_inventory(max_heavy_atoms, purchasable)

    # ---- 可行性过滤：把逆向模型包一层（不启用时 search_model 就是原模型） ----
    # 🔴 包装要在**读预算基线之前**完成：构造包装会 reset 底层模型（见
    #    wrap_with_forward_filter 的注释），反过来的话基线会拿到旧计数。
    wrapped = False
    search_model = model
    if use_forward_filter:
        search_model = wrap_with_forward_filter(model, forward_model, top_k=forward_top_k)
        wrapped = True

    # 🔴 预算是「本次搜索新增」的调用数，**绝不能**把 max_calls 直接交给 syntheseus。
    #    should_stop_search 比的是 reaction_model.num_calls() >= limit，而
    #    num_calls() 返回的是**缓存未命中累计数** —— 只在 reaction_model.reset()
    #    时才归零。官方 CLI 每次搜索前都会调 alg.reset()
    #    （syntheseus/cli/search.py，它内部就是 self.reaction_model.reset()），
    #    所以那边天然是「每次」语义。我们的模型服务是**常驻**的（见 model_server.py），
    #    一旦漏掉这一步，这个计数就跨搜索一路上涨：涨过 max_calls 之后，每次搜索
    #    开局即被判「预算耗尽」⇒ **0 秒 0 结果**。
    #    （用户报的「多步拆分没结果」正是这个 —— 不是拆不通，是预算被历史调用吃掉了：
    #     实测 max_calls<=80 恒 0 秒 or_nodes=1，max_calls=120 才跑得动。）
    #    这里用「当前基线 + 本次预算」表达同一含义，同时**不清 cache** ——
    #    跨搜索复用同一分子的预测结果能显著加速「换参数重跑同一目标」。
    _calls_base = search_model.num_calls()
    t1 = time.time()
    searcher = _build_searcher(
        algorithm,
        reaction_model=search_model,
        mol_inventory=inventory,
        value_fn=value_fn,
        max_heavy_atoms=max_heavy_atoms,
        limit_reaction_model_calls=_calls_base + max_calls,
        time_limit_s=time_limit,
        max_expansion_depth=max_depth,
    )
    graph, _ = searcher.run_from_mol(Molecule(smiles))
    search_s = time.time() - t1

    # ---- 提取路径 ----
    or_nodes = [n for n in graph.nodes() if isinstance(n, OrNode)]
    and_nodes = [n for n in graph.nodes() if isinstance(n, AndNode)]

    routes = []
    for nodes in iter_routes_time_order(graph, max_routes=max_routes):
        steps = []
        for n in nodes:
            if not isinstance(n, AndNode):
                continue
            rxn = n.reaction
            try:
                prob = rxn.metadata.get("probability")
            except Exception:  # noqa: BLE001
                prob = None
            reactants = sorted(m.smiles for m in rxn.reactants)
            steps.append(
                {
                    "product": rxn.product.smiles,
                    "reactants": reactants,
                    "reactants_joined": ".".join(reactants),
                    "probability": float(prob) if prob is not None else None,
                }
            )
        if steps:
            probs = [s["probability"] for s in steps if s["probability"] is not None]
            total = 1.0
            for p in probs:
                total *= p
            routes.append(
                {
                    "steps": steps,
                    "num_steps": len(steps),
                    "total_probability": total if probs else None,
                    "material_count": sum(
                        1
                        for s in steps
                        for r in s["reactants"]
                        if not any(r == t["product"] for t in steps)
                    ),
                }
            )

    # ---- 「路线必须包含指定原料」过滤 ----
    filtered_out = 0
    if required_materials:
        required_set = set(required_materials)
        kept = []
        for rt in routes:
            present = set()
            for step in rt["steps"]:
                present.add(step["product"])
                present.update(step["reactants"])
            present_canon = {c for c in (canon_smiles(x) for x in present) if c}
            if required_set.issubset(present_canon):
                kept.append(rt)
        filtered_out = len(routes) - len(kept)
        routes = kept

    # 按总概率降序
    routes.sort(key=lambda r: (r["total_probability"] is None, -(r["total_probability"] or 0)))

    # 路线里出现的所有分子（用于标注起始物料）
    all_products = {s["product"] for r in routes for s in r["steps"]}
    starting_materials = set()
    for r in routes:
        for s in r["steps"]:
            for react in s["reactants"]:
                if react not in all_products:
                    starting_materials.add(react)

    return {
        "ok": True,
        "target": smiles,
        "routes": routes,
        "starting_materials": sorted(starting_materials),
        "purchasable": sorted(purchasable or []),
        "required_materials": sorted(required_materials or []),
        "filtered_out": filtered_out,
        # 跑的到底是哪个算法：同一个分子换个算法结果会差很多，没有它用户只能猜
        "algorithm": algorithm,
        "value_fn": value_fn,
        # 可行性过滤：开没开、判据用的 top-k、以及**实际通过率**。
        # acceptance_rate 是「候选反应里有多少条通过了正向校验」—— 它把「过滤真的
        # 在起作用」和「过滤器把一切都放行了（等价于没开）」区分开。缺了它，用户
        # 只能看到路线条数变化，猜不出过滤器有没有生效。
        "forward_filter": wrapped,
        "forward_top_k": forward_top_k if wrapped else None,
        "acceptance_rate": round(search_model.acceptance_rate, 4) if wrapped else None,
        "stats": {
            "or_nodes": len(or_nodes),
            "and_nodes": len(and_nodes),
            "expansions": len(and_nodes),
            "max_heavy_atoms": max_heavy_atoms,
        },
        "search_seconds": round(search_s, 1),
    }


# --------------------------------------------------------------------------
# 规则子进程清理（Ctrl+C / 关窗口时 finalize 来不及执行 ⇒ 这里显式回收）
# --------------------------------------------------------------------------
# 背景：RetroChimera 的 RuleApplicationServer 会 spawn 若干子进程（每个约 4.4 GB），
# 官方在 __init__ 里注册了 weakref.finalize 做清理，但 **Ctrl+C 或直接关窗口时
# finalize 来不及执行** → 子进程变成孤儿、并在控制台刷出满屏
# "BrokenPipeError: [WinError 109] 管道已结束" 的 traceback（看着像崩溃，其实无害）。
def iter_rule_servers(obj, seen=None, depth=0):
    """在模型对象里找出所有 RuleApplicationServer 实例。

    实测结构（retrochimera 1.3.0）：
        RetroChimeraModel
          └─ _models: list[BackwardReactionModel]
               ├─ TemplateClassificationModel._server = RuleApplicationServer
               └─ TemplateLocalizationModel._server   = RuleApplicationServer
    所以必须同时遍历「属性」和「容器元素」，否则一个都找不到。
    """
    if seen is None:
        seen = set()
    if obj is None or depth > 7 or id(obj) in seen:
        return
    seen.add(id(obj))

    # 命中目标
    if type(obj).__name__ == "RuleApplicationServer":
        yield obj
        return

    # 容器：list / tuple / set / dict value
    if isinstance(obj, dict):
        for v in list(obj.values()):
            yield from iter_rule_servers(v, seen, depth + 1)
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for v in list(obj):
            yield from iter_rule_servers(v, seen, depth + 1)
        return

    # 只下潜到自定义对象，避免遍历 torch/numpy 的大对象图
    mod = type(obj).__module__ or ""
    if mod.startswith(("torch", "numpy", "rdkit", "syntheseus")):
        return

    # 1) 已知属性名
    for attr in ("_server", "_rule_model", "_reaction_model", "model", "_model", "_models"):
        try:
            child = getattr(obj, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if child is not None:
            yield from iter_rule_servers(child, seen, depth + 1)

    # 2) 兜底：按属性名模糊扫描（属性名单可能随版本变化）
    try:
        attrs = vars(obj)
    except Exception:  # noqa: BLE001
        attrs = {}
    for name, child in list(attrs.items()):
        if name.startswith("__") or child is None:
            continue
        low = name.lower()
        if ("server" in low or "rule" in low) and name not in (
            "_server", "_rule_model", "_reaction_model", "model", "_model", "_models",
        ):
            yield from iter_rule_servers(child, seen, depth + 1)


def shutdown_rule_servers(*models) -> int:
    """关闭给定模型对象上的所有规则子进程（幂等、绝不抛异常）。返回关闭组数。"""
    closed = 0
    for model in models:
        if model is None:
            continue
        for server in iter_rule_servers(model):
            try:
                if getattr(server, "_running", False):
                    server.close()
                    closed += 1
            except Exception:  # noqa: BLE001
                pass
    return closed


# --------------------------------------------------------------------------
# ModelHost：一次只持有一个已加载模型（内存实测约 18GB/模型，见启动脚本注释）
# --------------------------------------------------------------------------
# 🔴 加载**必须**在进程的单线程阶段完成：RetroChimeraModel 内部要 spawn 规则
#    子进程，而 Windows 的 spawn 在「已有其他线程存活」的进程里会死锁。
#    这就是「换模型 = 重启模型服务进程」而不是「热切换」的原因（见 model_server.py）。
class ModelHost:
    """模型宿主：加载 / 卸载 / 推理 / 搜索，并对外报告阶段。

    state 取值：
        idle     尚未加载任何模型
        loading  正在加载（phase 给出更细的阶段）
        ready    已就绪，可推理
        failed   加载失败（error 给原因）
    """

    def __init__(self) -> None:
        self._model = None
        self.model_key = ""
        self.state = "idle"
        self.phase = ""
        self.error = ""
        self.started_at = 0.0
        self.ready_at = 0.0
        self.load_seconds = 0.0
        #: 加载进度（0~100）。**是估算**不是精确值：三个阶段（import / model / rules）
        #:   各自只给一个锚点，阶段内部按"已耗时占该阶段预估时长"插值，最终封顶 99
        #:   （100 只留给 state == "ready"，见 status()）。前端据此画真进度条。
        self.progress = 0.0
        #: 当前阶段起点（秒级时间戳）。`_progress_now` 用它算「阶段内已耗时」，
        #:   而不是拿「总耗时」直接套 —— 总耗时含前面阶段的时长，直接除会让
        #:   阶段一进来就跳一截（实测 model 阶段一进就 30%+）。
        self._stage_started_at = 0.0

        # --- 正向模型（「可行性过滤」的判据，与逆向模型互不影响）---
        self._forward_model = None
        self.forward_key = ""
        #: idle | loading | ready | failed
        self.forward_state = "idle"
        self.forward_error = ""
        self.forward_load_seconds = 0.0
        #: 正向模型加载进度（0~100，**估算**）。与逆向模型的 `progress` 同义但
        #: **独立存储** —— 两个模型可以同时在加载，共用一个变量会互相覆盖成对方的进度。
        self.forward_progress = 0.0
        #: 正向模型当前阶段的起点（秒级时间戳），供 _forward_progress_now 算阶段内耗时。
        self._forward_stage_started_at = 0.0
        #: 加载串行化 —— 前端可能连点两次「加载」，而它要几十秒，不串行就会载两份。
        self._forward_lock = threading.Lock()

    # ---------- 加载 / 卸载 ----------
    def load(self, key: str, on_phase=None) -> None:
        """同步加载模型。**只能在单线程阶段调用**（见类注释）。"""
        if key not in MODELS:
            raise ValueError(f"未知模型: {key}")

        def _phase(name: str, text: str) -> None:
            # 🔴 phase 存**文案**而不是代号：它会被 API 直接拿去展示
            #    （「模型正在加载中（导入推理依赖…）」）。存 "import" / "model"
            #    这种代号会原样透到用户眼前，看着像报错信息。
            self.phase = text
            # 进度锚点：三个阶段各占一段。这里只在**进入阶段时**给一个下界，
            #    阶段内部的爬升交给 status() 用「阶段内耗时」插值（见 _progress_now）。
            #    🔴 同时重记阶段起点：否则插值拿总耗时直接除，阶段一进就跳一截。
            self.progress = {
                "import": 0.0,
                "model": 30.0,
                "rules": 88.0,
            }.get(name, self.progress)
            self._stage_started_at = time.time()
            if on_phase:
                try:
                    on_phase(name, text)
                except Exception:  # noqa: BLE001
                    pass

        info = MODELS[key]
        path = info["path"]
        if not (os.path.isdir(path) and os.listdir(path)):
            raise FileNotFoundError(f"模型目录不存在或不完整: {path}")

        self.state = "loading"
        self.error = ""
        self.started_at = time.time()
        # 🔴 `model_key` 必须**一进 loading 就设**，不能等加载完才赋值：加载权重那几十秒
        #    （`RetroChimeraModel(...)` 阻塞）里 state 文件的 `model` 字段一直是空串，
        #    model_client 据此算出的 `current` 也是空 ⇒ 前端 `models` 三行全 idle、
        #    「当前选中模型」state 是 idle ⇒ 进度条（只在 state==='loading' 时渲染）
        #    永远不出现。提前设上，加载全程 `model` 都有值、进度条才出得来。
        self.model_key = key
        _phase("import", "导入推理依赖（retrochimera / syntheseus）")
        from retrochimera import RetroChimeraModel

        _phase("model", f"加载模型权重：{os.path.basename(path)}")
        model = RetroChimeraModel(model_dir=path)

        _phase("rules", "启动规则应用子进程")
        self._model = model
        self.model_key = key
        self.load_seconds = round(time.time() - self.started_at, 1)
        self.ready_at = time.time()
        self.phase = ""
        self.state = "ready"

    def unload(self) -> None:
        """释放模型与规则子进程。"""
        try:
            shutdown_rule_servers(self._model)
        finally:
            self._model = None
            self.model_key = ""
            self.state = "idle"
            self.phase = ""
            self.ready_at = 0.0
            self.progress = 0.0
            self._stage_started_at = 0.0

    # ---------- 正向模型（「可行性过滤」的判据） ----------
    def load_forward(self, key: str = "", on_phase=None) -> None:
        """加载正向模型。**幂等**：已经是它且就绪时直接返回，不会重载一份。

        🔴 与逆向模型不同，它**不需要重启进程**：正向模型是个纯 transformer，
           构造时不 spawn 规则子进程（唯一用到 multiprocessing 的地方在推理时的
           `_get_reactions`，与逆向模型共用同一套已经跑通的写法）。所以完全可以在
           HTTP 已经起来之后按需加载 —— 这正是「开关一打开就能用」能成立的前提。
        """
        key = key or default_forward_key()
        if key not in FORWARD_MODELS:
            raise ValueError(f"未知正向模型: {key}")

        def _phase(text: str, anchor: float) -> None:
            # 进度锚点：正向模型只有两段（导入依赖 15 / 加载权重 99），比逆向模型少一段
            # 规则加载。阶段内爬升交给 _forward_progress_now 插值，所以这里只给下界。
            self.forward_progress = anchor
            self._forward_stage_started_at = time.time()
            if on_phase:
                try:
                    on_phase(text)
                except Exception:  # noqa: BLE001
                    pass

        with self._forward_lock:
            if (
                self.forward_state == "ready"
                and self.forward_key == key
                and self._forward_model is not None
            ):
                return  # 已经在跑同一个，别白载

            info = FORWARD_MODELS[key]
            path = info["path"]
            if not (os.path.isdir(path) and os.listdir(path)):
                self.forward_state = "failed"
                self.forward_key = key
                self.forward_error = f"正向模型目录不存在或不完整: {path}"
                raise FileNotFoundError(self.forward_error)

            self.forward_state = "loading"
            self.forward_key = key
            self.forward_error = ""
            t0 = time.time()
            # 进度归零重开：换模型重载时不能沿用上一轮的数字（否则界面一进来就显示
            # 88% 然后再退回 15%，看起来像倒退卡死）。
            self.forward_progress = 0.0
            self._forward_stage_started_at = 0.0
            try:
                _phase("导入正向模型依赖（retrochimera / torch）", 15.0)
                from retrochimera import ForwardChimeraDeNovoModel

                _phase(f"加载正向模型权重：{os.path.basename(path)}", 88.0)
                # device 由基类自动定（有 CUDA 用 cuda:0，否则 cpu），不用显式传。
                model = ForwardChimeraDeNovoModel(model_dir=path)
            except Exception as e:  # noqa: BLE001
                # 失败也要把状态落到 failed：否则界面会永远停在「加载中」，
                # 用户只会以为它卡死了，而真正的原因（缺文件 / 权重不匹配）埋在日志里。
                self._forward_model = None
                self.forward_state = "failed"
                self.forward_error = f"{type(e).__name__}: {e}"
                raise
            self._forward_model = model
            self.forward_load_seconds = round(time.time() - t0, 1)
            self.forward_state = "ready"
            self.forward_progress = 100.0

    def unload_forward(self) -> None:
        """释放正向模型（内存紧的时候用；不影响正在跑的逆向模型）。"""
        with self._forward_lock:
            self._forward_model = None
            self.forward_key = ""
            self.forward_state = "idle"
            self.forward_error = ""
            self.forward_load_seconds = 0.0
            self.forward_progress = 0.0
            self._forward_stage_started_at = 0.0

    def _forward_progress_now(self) -> float:
        """正向模型加载进度（0~100）。分段：导入依赖 0→15、加载权重 15→99，100 属ready。

        🔴 与 `_progress_now` 同口径（都是**估算**，都用「阶段内耗时」而非总耗时插值），
           但**锚点不同**：正向模型不 spawn 规则子进程，少一段最慢的规则加载。
        """
        if self.forward_state == "ready":
            return 100.0
        if self.forward_state != "loading" or not self._forward_stage_started_at:
            return self.forward_progress
        stage_elapsed = max(0.0, time.time() - self._forward_stage_started_at)
        if self.forward_progress < 15.0:
            lo, hi, est = 0.0, 15.0, 12.0
        else:
            lo, hi, est = 15.0, 99.0, 40.0
        frac = min(1.0, stage_elapsed / max(est, 0.001))
        return min(hi, lo + (hi - lo) * frac)

    def forward_status(self) -> dict:
        """正向模型状态。与逆向模型的 status 同构（都有 progress / load_seconds），
        前端画加载进度条可以用同一套逻辑，不必为正向模型单开一条分支。

        🔴 progress 是**估算**（与逆向模型的 `_progress_now` 同口径）：
           正向模型只有「导入依赖 → 加载权重」两段，比逆向模型少一段规则加载，
           所以分段锚点是 0→15 / 15→99，而不是 0→30 / 30→88 / 88→99。
        """
        return {
            "key": self.forward_key,
            "state": self.forward_state,
            "error": self.forward_error,
            "load_seconds": self.forward_load_seconds,
            # 🔴 与逆向模型的 `progress` 同名同义（0~100，100 只在 ready）。
            #    正向模型走自己的一条 _phase 链，锚点少一段：0→15、15→99。
            "progress": self._forward_progress_now(),
        }

    # ---------- 推理 / 搜索 ----------
    def _require(self):
        if self.state != "ready" or self._model is None:
            raise RuntimeError(
                f"模型未就绪（当前状态：{self.state}"
                + (f"，{self.error}" if self.error else "")
                + "）"
            )
        return self._model

    def predict(self, smiles: str, num_results: int) -> list:
        """单步逆合成预测。返回 [{reaction, probability}, ...]。"""
        from syntheseus import Molecule

        model = self._require()
        preds = model([Molecule(smiles)], num_results=num_results)
        # 🔴 str(p) 是**反应 SMILES**（reactants>>product），不是纯前体；
        #    反应物侧的拆解在上层（jobhub 的 runner）做（reaction -> precursor_list）。
        return [
            {"reaction": str(p), "probability": float(p.metadata["probability"])}
            for p in preds[0]
        ]

    def search(self, params: dict) -> dict:
        """多步路线搜索。params 见 run_retro_search 的签名（含 `algorithm`）。

        🔴 正向模型由**这里**注入，而不是让调用方把对象传进来：正向模型常驻在本
           进程，调用方（API / 前端）根本不持有它。调用方只说「要不要过滤」
           （`use_forward_filter`），对象由宿主按当前真实状态补上 —— 没就绪就传
           None，`wrap_with_forward_filter` 会给出说得清原因的错误。
        """
        model = self._require()
        params = dict(params)
        params["forward_model"] = (
            self._forward_model if self.forward_state == "ready" else None
        )
        return run_retro_search(model, **params)

    # ---------- 状态 ----------
    def status(self) -> dict:
        out = {
            "state": self.state,
            "model": self.model_key,
            "phase": self.phase,
            "error": self.error,
            "load_seconds": self.load_seconds,
            "progress": self._progress_now(),
            # 正向模型（可行性过滤用）单独一份，不跟逆向模型的 state 混：
            # 两者可以同时在跑，且生命周期完全独立（一个是进程级、一个可热挂）。
            "forward": self.forward_status(),
        }
        if self.started_at:
            if self.state == "loading":
                out["elapsed"] = round(time.time() - self.started_at, 1)
            elif self.ready_at:
                out["elapsed"] = round(self.ready_at - self.started_at, 1)
        return out

    def _progress_now(self) -> float:
        """当前加载进度（0~100）。三个阶段（import→30 / model→88 / rules→99），
        100 只属于 ready。阶段内用「**阶段内已耗时** / 该阶段预估时长」插值，让进度条
        平滑爬行而不是只在阶段切换时跳一下 —— 阶段时长不可预测，所以是**估算**：
        权重加载那步（model）实际由外部库内部循环决定，这里只能按经验给个上限。

        🔴 必须用 `_stage_started_at`（阶段起点）算阶段内耗时，**不能**用
           `time.time() - started_at`（总耗时）：总耗时含前面阶段的时长，直接除会让
           阶段一进来就跳一截（model 阶段进来时总耗时已 ≥ import 的十几秒，直接 /45
           就是 30%+）。"""
        if self.state == "ready":
            return 100.0
        if self.state != "loading" or not self.started_at:
            return self.progress
        if self._stage_started_at:
            stage_elapsed = max(0.0, time.time() - self._stage_started_at)
        else:
            # 阶段起点还没记（理论上 load() 第一个 _phase 就会记）—— 兜底用总耗时
            stage_elapsed = max(0.0, time.time() - self.started_at)
        # 三段各自的预估时长（秒）与目标上界
        if self.progress < 30.0:
            lo, hi, est = 0.0, 30.0, 20.0
        elif self.progress < 88.0:
            lo, hi, est = 30.0, 88.0, 45.0
        else:
            lo, hi, est = 88.0, 99.0, 12.0
        frac = min(1.0, stage_elapsed / max(est, 0.001))
        return min(hi, lo + (hi - lo) * frac)
