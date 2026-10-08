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
MODELS = {
    "pistachio": {
        "path": model_path("RETRO_MODEL_PISTACHIO", "retrochimera_pistachio"),
        "label": "Pistachio（主推，性能最强）",
    },
    "uspto50k": {
        "path": model_path("RETRO_MODEL_USPTO50K", "retrochimera_uspto50k"),
        "label": "USPTO-50K（基准模型，较弱）",
    },
}

#: 默认预加载哪个（可用 RETRO_PRELOAD 覆盖；none/0/false 表示不预加载）
def default_model_key() -> str:
    key = str(os.environ.get("RETRO_PRELOAD", "pistachio")).strip()
    if key.lower() in ("", "none", "0", "false", "off"):
        return ""
    return key if key in MODELS else "pistachio"


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


def run_retro_search(model, *, smiles, max_routes=5, max_calls=60, time_limit=120.0,
                     max_depth=3, max_heavy_atoms=10, value_fn="complexity",
                     purchasable=None, required_materials=None):
    """跑一次 Retro* 搜索并提取路线（**纯函数，只依赖传入的 model 对象**）。

    🔴 为什么接受「model 对象」而不是 model_key：
       搜索任务要在**模型服务进程**里跑（复用那里已加载的模型，不在别处
       再载一份）。同步 /api/search 走 HTTP 转发后同样落到这里。
       两者共用这一份逻辑，避免「异步任务和同步接口各写一遍、结果不一致」。

    返回 dict（不含 jsonify 包装）：{ok, target, model, routes, starting_materials,
    purchasable, required_materials, filtered_out, value_fn, stats, load_seconds,
    search_seconds}。异常由调用方决定如何呈现。
    """
    from syntheseus import Molecule
    from syntheseus.search.algorithms.best_first.retro_star import RetroStarSearch
    from syntheseus.search.analysis.route_extraction import iter_routes_time_order
    from syntheseus.search.graph.and_or import AndNode, OrNode
    from syntheseus.search.node_evaluation.common import ReactionModelLogProbCost

    # 参数保护（与 /api/search 同一套边界，别让服务端收到越界值）
    max_routes = max(1, min(int(max_routes), 20))
    max_calls = max(5, min(int(max_calls), 400))
    time_limit = max(10.0, min(float(time_limit), 600.0))
    max_depth = max(1, min(int(max_depth), 8))
    max_heavy_atoms = max(1, min(int(max_heavy_atoms), 40))
    if value_fn not in ("constant", "complexity"):
        value_fn = "complexity"

    inventory = make_inventory(max_heavy_atoms, purchasable)

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
    _calls_base = model.num_calls()
    t1 = time.time()
    searcher = RetroStarSearch(
        reaction_model=model,
        mol_inventory=inventory,
        value_function=make_value_function(value_fn, inventory, max_heavy_atoms),
        and_node_cost_fn=ReactionModelLogProbCost(),
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
        "value_fn": value_fn,
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
        """多步路线搜索（Retro*）。params 见 run_retro_search 的签名。"""
        model = self._require()
        return run_retro_search(model, **params)

    # ---------- 状态 ----------
    def status(self) -> dict:
        out = {
            "state": self.state,
            "model": self.model_key,
            "phase": self.phase,
            "error": self.error,
            "load_seconds": self.load_seconds,
        }
        if self.started_at:
            if self.state == "loading":
                out["elapsed"] = round(time.time() - self.started_at, 1)
            elif self.ready_at:
                out["elapsed"] = round(self.ready_at - self.started_at, 1)
        return out
