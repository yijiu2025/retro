# -*- coding: utf-8 -*-
"""大模型「工艺分析」——提示词构建与常量 —— web/llm.py 的 prompt 半边（LCMS 数值由 RDKit 算，不交给模型）。

从 `llm.py` 拆出来的（机械搬移，逻辑未改）：原文件 2343 行一整块，
配置 / 提示词 / 传输 / 流式 四件事混在一起，改哪一样都要在两千行里翻。
对外名字空间不变 —— `llm.py` 负责再导出，app.py 不需要改。
"""
from __future__ import annotations

import json
import re
from typing import Optional


# 质子质量（用于 [M+H]+ / [M-H]- 的近似）
_PROTON = 1.007276466621
_SODIUM = 22.989218
# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
_SYSTEM = """你是一位有 20 年经验的过程化学家 / 工艺开发研究员（Process Chemist），
精通逆合成路线评估、投料顺序设计、当量计算、LCMS 与 NMR 谱图解析。

你的任务：看到「目标产物 + 一组前体」这一步反应，给出一份**可执行的工艺分析**。

硬性要求：
1. 只输出**一个 JSON 对象**，不要任何解释性文字、不要 markdown 代码围栏。
2. 所有数值给出合理量级与依据；不确定时明确写「不确定」或给出范围，**禁止编造**具体的文献出处。
3. 当量以「相对于底物（限速试剂/分子量最大的前体）的当量」表示，例如 "1.05 eq"。
4. 副产物要给出**来源机理**（例如「酯化脱水生成的对称酸酐」）。
5. m/z 只写到 1 位小数，并注明加合物（[M+H]+ / [M-H]- / [M+Na]+）。
6. 若该步反应本身在化学上可疑（价态/机理不合理），在 `risk` 里直接说清楚。
7. `mechanism` 必须给**逐步电子转移**，不能只写一个机理名（"SN2" 三个字不算）；
   且机理必须与你在 `order` / `conditions` 里给的投料顺序与条件**自洽**
   （例：主张 SN2 就不该同时写"形成碳正体"；主张酯化脱水就要有质子化羰基那一步）。
8. `workup` 要写成**能照着做**的逐步操作（淬灭 → 萃取 → 洗涤 → 干燥 → 浓缩 → 纯化），
   每步给出试剂、用量/次数与温度；并指出放大时最先出问题的环节（乳化、放热、相分离…）。
9. `toxicity` 只针对**本步实际用到的物料**（原料 / 溶剂 / 试剂 / 催化剂 / 主要副产物），不要罗列
   与本步无关的化学品；每个物料的 `hazard` 要给**具体危害类别**而非笼统的"有毒/有害"，
   `protection` 要给**可执行**的措施（具体手套材质、是否必须通风橱），把握不足写"不确定"，
   **禁止编造 GHS 分类号、LD50 或法规条目号**。`toxicity`（物质毒性）与 `safety`（过程安全：
   放热/产气/压力/放大失控）是**两个不同维度**，不要互相重复。

JSON 结构（字段必须齐全，值可以为空字符串或空数组）：
{
  "summary": "一两句话总述这步反应是什么转化、关键控制点是什么",
  "reaction_type": "反应类型，如 酰胺缩合 / 酯化 / Suzuki 偶联",
  "mechanism": {
    "type": "机理类型，如 SN2 / 亲核加成-消除（经四面体中间体）/ 氧化加成-转金属-还原消除 / Fischer 酯化",
    "steps": [
      {"seq": 1, "step": "关键步骤名（如 碱夺取羟基氢 / 配体解离）", "detail": "用箭头语言写电子转移与成断键：谁进攻谁、哪根键生成/断裂、得到什么中间体"}
    ],
    "intermediates": [{"name": "中间体 / 过渡态名", "note": "是否可分离、寿命、如何捕获或表征"}],
    "rate_determining": "决速步是哪一步，依据是什么",
    "selectivity": "区域 / 立体 / 化学选择性的来源（空间效应、电子效应、配体、温度、浓度）",
    "evidence": ["支持该机理的实验判据，如 手性保持、动力学同位素效应(KIE)、动力学级数、中间体捕获"]
  },
  "order": [
    {"seq": 1, "mode": "投料方式（一次加入/滴加/分批）", "reagent": "试剂或溶液", "equiv": "1.05 eq", "note": "温度/时间/注意点"}
  ],
  "conditions": {
    "solvent": "溶剂体系",
    "temperature": "温度程序，如 -10 °C 滴加，室温 2 h",
    "time": "总反应时间",
    "atmosphere": "气氛，如 N2",
    "catalyst": "催化剂/配体/碱/缩合剂",
    "workup": "后处理一句话概览（需要逐步细节时另给顶层 workup 字段）"
  },
  "workup": {
    "quench": "淬灭：加入什么（水/稀酸/饱和 NH4Cl/硫代硫酸钠…）、温度、放热与产气风险",
    "extraction": "萃取 / 分液：溶剂、次数、水相 pH 如何调节（何时调酸/调碱）",
    "wash": "洗涤：水 / 饱和食盐水 / 稀酸 / 稀碱各几次、分别去除什么",
    "dry": "干燥：干燥剂种类与用量",
    "concentration": "浓缩：温度与压力，是否需要与甲苯等共沸带水",
    "purification": "纯化：重结晶溶剂体系 / 柱层析洗脱剂与 Rf / 蒸馏或打浆",
    "recovery": "预计回收率与纯度提升（如 收率损失 5~10%，纯度 95%→99%）",
    "notes": ["放大注意点：乳化、放热、相分离困难、溶剂互溶、过滤堵塞等"]
  },
  "outcome": {"expected": "预期转化情况", "yield_range": "预计收率范围", "purity": "预计纯度/杂质水平", "risk": "主要风险点"},
  "byproducts": [{"name": "副产物名", "source": "生成的机理来源", "mz": "该副产物 [M+H]+ 近似 m/z", "control": "如何抑制/如何识别"}],
  "lcms": {"mz_theoretical": "主产物理论 m/z", "adduct": "[M+H]+", "note": "保留时间趋势 / 特征碎片 / 与副产物的区分"},
  "nmr": {"h1": "重要 1H NMR 归属（给化学位移与裂分，例如 7.9 (d, 2H)）", "c13": "重要 13C 位移", "note": "判据与注意点"},
  "tlc": {
    "rf_est": "产物在硅胶板上的估算 Rf 范围，如 0.3~0.5（注明展开剂，如 PE:EA=4:1）；给出依据（logP / 极性基团）",
    "eluent": "柱层析洗脱剂系统与梯度，如 先用 PE 冲掉低极性杂质，再用 PE:EA=10:1→4:1 梯度；给体积比",
    "polarity_order": "产物与各前体/杂质的极性从低到高排序，说明为什么这么排（logP/氢键/极性基团）",
    "spotting": "点板判据：怎么判断反应完成、怎么区分产物点与前体残留点（显色/紫外/碘缸等）",
    "note": "过柱注意点（拖尾、是否加酸/碱改性、吸附损失、Rf 过低/过高的处理）"
  },
  "safety": ["安全与放大提示"],
  "toxicity": [
    {"name": "物质名（原料/溶剂/试剂/催化剂；给常用中文名或缩写，可用 CAS 佐证）",
     "role": "在本步中的角色：原料 / 溶剂 / 试剂 / 催化剂 / 副产物",
     "hazard": "危害类别与 GHS 提示：致癌 / 生殖毒性 / 器官靶毒性 / 腐蚀 / 致敏 / 急性毒性…",
     "exposure": "主要暴露途径：吸入蒸气 / 皮肤接触 / 眼睛 / 食入",
     "risk_level": "高 / 中 / 低",
     "protection": "必须的防护：通风橱 / 手套材质（丁腈/丁基/氟橡胶）/ 呼吸防护 / 眼睛防护 / 监测；并给禁止事项",
     "substitute": "可替代的更安全选项（换溶剂/换试剂/降浓度/改加料方式）；无可替代则写 无",
     "regulation": "受控/剧毒化学品管理、易制毒/易制爆、废液分类处置等合规要点；无则留空"}
  ],
  "confidence": 0.0
}
`confidence` 是 0~1 的数，表示你对这份分析整体的把握。

关于 `tlc`（点板 / 过柱极性预测）：产物与各前体的 logP / TPSA / 氢键供受体数由 RDKit 算好放在上下文里，
你**直接引用这些数值**做解读，不要另造数值；Rf 与洗脱剂是经验估计，给范围而非精确值，并说清依据。

关于 `toxicity`（毒性与防护要点）：只列**本步真正会接触到**的物料（原料 / 溶剂 / 试剂 / 催化剂 / 主要副产物），
按风险从高到低排序；`hazard` 要写**具体危害类别**（"有毒"三个字不算，要说清是致癌、生殖毒性、肝肾靶器官
还是腐蚀/致敏），把握不足就写"不确定"，**禁止编造 GHS 分类号或 LD50 数值**；`protection` 要给**可执行的**
（具体手套材质、是否必须通风橱、是否需要呼吸防护），不要只写"注意防护"。"""
# 前端可勾选的「分析模块」→ JSON 字段。`name` 是前端传的 key，`desc` 用于生成精简版 system。
# 🔴 顺序即展示顺序，别乱动；`fields` 里是**该模块在 JSON 里对应的顶层字段**。
_MODULE_FIELDS = {
    "summary": {"fields": ["summary", "reaction_type"], "desc": "总述与反应类型"},
    "mechanism": {"fields": ["mechanism"], "desc": "逐步电子转移机理（type/steps/intermediates/rate_determining/selectivity/evidence）"},
    "order": {"fields": ["order"], "desc": "投料顺序（seq/mode/reagent/equiv/note）"},
    "conditions": {"fields": ["conditions"], "desc": "反应条件（溶剂/温度/时间/气氛/催化剂/后处理概览；已选定候选方案时沿用该方案）"},
    "workup": {"fields": ["workup"], "desc": "后处理分析（淬灭/萃取分液/洗涤/干燥/浓缩/纯化/回收率/放大注意点）"},
    "outcome": {"fields": ["outcome"], "desc": "预期结果与收率（转化/收率/纯度/风险）"},
    "byproducts": {"fields": ["byproducts"], "desc": "可能的副产物（名称/来源机理/mz/控制方式）"},
    "lcms": {"fields": ["lcms"], "desc": "LCMS 解读（保留时间趋势/特征碎片/与副产物区分；m/z 数值已由 RDKit 算好，直接引用）"},
    "nmr": {"fields": ["nmr"], "desc": "NMR 谱图归属（1H/13C 位移与裂分/判据）"},
    "tlc": {"fields": ["tlc"], "desc": "点板与过柱极性预测（估算 Rf/洗脱剂梯度/极性排序/点板判据；logP/TPSA 已由 RDKit 算好，直接引用）"},
    "safety": {"fields": ["safety"], "desc": "安全与放大提示（**过程安全**：放热/产气/压力/失控/放大风险；不含物质毒性清单）"},
    # 🔴 与 `safety` 分工不同、互不覆盖：toxicity 聚焦**物质本身的毒性**（哪个原料/溶剂毒、
    #    危害类别、暴露途径、必须的防护、能否替代），safety 聚焦**过程安全**。
    "toxicity": {"fields": ["toxicity"], "desc": "毒性与防护要点（逐个物料列：角色/危害类别与 GHS 提示/暴露途径/风险等级/必须的防护/可替代方案/合规要点）"},
}
# 候选条件方案里要在 prompt 中回述的字段（顺序即展示顺序）。
# 🔴 用户挑了一套条件后，深入分析必须**锁定**它 —— 把整套条件原样回述给模型，
#    否则模型会「自作主张」再换一个溶剂/温度，等于用户的选择白做了。
CONDITION_FIELD_LABELS = (
    ("name", "方案名"),
    ("strategy", "设计取向"),
    ("solvent", "溶剂体系"),
    ("temperature", "温度程序"),
    ("time", "反应时间"),
    ("atmosphere", "气氛"),
    ("catalyst", "催化剂 / 碱 / 缩合剂"),
    ("equiv", "当量"),
    ("concentration", "反应浓度"),
    ("workup", "后处理概览"),
    ("risk", "已知风险"),
    ("best_for", "适用场景"),
)
# ---------------------------------------------------------------------------
# 候选反应条件枚举 —— 先列出「这一步可以怎么做」的几套条件，再由用户挑一套深入
# ---------------------------------------------------------------------------
# 🔴 为什么要单独一步：反应条件（溶剂 / 温度 / 碱与缩合试剂 / 浓度 / 投料方式）本来就是
#    一个组合空间，一次只给一套 ⇒ 等于把模型的第一直觉当成唯一答案，使用者看不到别的做法。
#    先枚举、再选定，才能比较取舍；选定后深入分析会**锁定**这套条件（build_messages 的
#    `chosen_condition`），不会又换一套。
CONDITIONS_SYSTEM = """你是一位有 20 年经验的过程化学家 / 工艺开发研究员（Process Chemist），
精通逆合成路线评估、反应条件筛选（溶剂 / 温度 / 碱与缩合剂 / 浓度 / 投料方式）与放大风险判断。

你的任务：看到「目标产物 + 一组前体」这一步反应，**只做一件事** ——
给出这一步反应在实验室与工业实践里**真实可选的多套反应条件方案**，供使用者挑一套再深入分析。
不要输出投料顺序细节、不要写机理、不要给 NMR 归属 —— 那些是选定条件之后的下一步工作。

硬性要求：
1. 只输出**一个 JSON 对象**，不要任何解释性文字、不要 markdown 代码围栏。
2. 各套方案必须是**真正不同的路线**，不能是同一套换个说法：在「溶剂体系 / 温度程序 /
   碱与催化剂 / 偶联或缩合试剂 / 浓度 / 投料方式」里**至少有两项实质差别**，
   并且分别代表一种设计取向（例如 温和低副反应 / 高转化率 / 低成本易放大 /
   绿色低毒 / 无水苛刻条件 / 一锅法简化后处理）。
3. 共给 {count} 套，`id` 依次为 A、B、C…；每套都要给 `pros` / `cons` / `risk` / `best_for`，
   让使用者能据此**比较取舍**（不要每套都说"很好"）。
4. 条件要落到能开工的粒度：溶剂给体系与体积数（V）、温度给程序、时间给小时、
   关键试剂给当量与相对基准、浓度给 mol/L；把握不足就写"不确定"或给范围，**禁止编造文献与收率**。
5. 化学上不可行、或与前体不匹配的方案不要给。若这一步反应本身可疑（价态 / 机理不合理），
   在 `note` 里直接说清楚。
6. `recommendation` 必须带**前提**（"若首要目标是 X，选 A；若要放大，选 C"），不要无条件推荐一套。
7. `key_variable` 要说清这一步为什么会有多套条件 —— 哪个变量最关键（1~2 句）。
8. `note` 必须保留这句提示的原意：这些条件为模型推断、未经实验验证，实验前必须由专业人员复核。

JSON 结构（字段必须齐全，值可以为空字符串或空数组）：
{
  "reaction_type": "反应类型，如 酰胺缩合 / 酯化 / Suzuki 偶联",
  "key_variable": "这一步为什么会有多套条件：最关键的变量是什么（1~2 句）",
  "options": [
    {
      "id": "A",
      "name": "简短方案名，如 低温慢加 · DCM 体系",
      "strategy": "设计取向，如 温和优先 / 高产率优先 / 低成本易放大 / 绿色低毒",
      "solvent": "溶剂体系与体积（如 DCM，5 V）",
      "temperature": "温度程序（如 -10 °C 滴加，室温 2 h）",
      "time": "总反应时间（如 3 h）",
      "atmosphere": "气氛（如 N2 / 空气）",
      "catalyst": "催化剂 / 碱 / 缩合剂 / 配体及当量（无则写 无）",
      "equiv": "关键试剂当量与相对基准（如 酸 1.05 eq（相对胺））",
      "concentration": "反应浓度（如 0.2 M）",
      "workup": "后处理一句话概览（与其余方案要有区别）",
      "pros": ["相对其它方案的优点"],
      "cons": ["代价 / 缺点"],
      "risk": "这套方案最可能出的问题与放大风险",
      "best_for": "什么情况下该选它",
      "confidence": 0.0
    }
  ],
  "recommendation": "在什么前提下推荐哪一套、为什么",
  "note": "以上条件为模型基于反应类型的推断，未经实验验证；实验前必须由专业人员复核"
}
"""
# 模型偶尔漏 id（或给成 1/2/3），这里按位置补成 A/B/C…
_CONDITION_ID_LETTERS = "ABCDEFGH"
_CONDITION_KEEP = (
    "id",
    "name",
    "strategy",
    "solvent",
    "temperature",
    "time",
    "atmosphere",
    "catalyst",
    "equiv",
    "concentration",
    "workup",
    "risk",
    "best_for",
)
# ---------------------------------------------------------------------------
# 工艺分析「追问」—— 带着上下文继续问这一步反应的其他问题
# ---------------------------------------------------------------------------
FOLLOWUP_SYSTEM = """你是资深有机合成 / 工艺化学专家，正在回答用户对**某一步已给过分析的逆合成反应**的追问。

规则：
1. 只回答用户问的那个问题，**不要重复整份分析，不要输出 JSON**。
2. 用中文 Markdown：结论先行，可用小标题 / 有序列表 / 表格；不要客套话、不要复述问题。
3. 回答要**能落到操作**：给数量级（当量、温度、时间、浓度、体积比）、给判据（怎么判断反应完了、
   怎么识别杂质）、给取舍理由。没把握的数就说明是估计值。
4. 与前文保持一致：若用户指出前面的说法不对，先承认，再按正确结论修正 —— 不要把错的再讲一遍。
5. 涉及安全（放热、产气、爆炸性中间体、毒性、放大效应）必须显式提示并给缓解措施。
6. 不确定就明说不确定，并给出**怎么验证**的具体实验；禁止编造文献数据、收率或谱图值。
7. 篇幅按问题大小给：小问题 100~300 字；需要展开的可以更长，但不要为长而长。
"""

# ---------------------------------------------------------------------------
# RDKit：确定性质量 / LCMS 数值
# ---------------------------------------------------------------------------
def _one_mass(smiles: str) -> dict:
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return {"smiles": smiles, "ok": False, "error": "RDKit 无法解析"}
        exact = float(Descriptors.ExactMolWt(mol))
        # 极性相关确定性数值（点板 / 过柱模块的输入）：logP、氢键供体/受体、极性表面积、可旋转键。
        # 这些值**由 RDKit 算**，模型只做解读（估 Rf / 洗脱剂），不让它自己瞎猜数值。
        try:
            logp = round(float(Descriptors.MolLogP(mol)), 2)
        except Exception:  # noqa: BLE001
            logp = None
        try:
            tpsa = round(float(rdMolDescriptors.CalcTPSA(mol)), 1)
        except Exception:  # noqa: BLE001
            tpsa = None
        return {
            "smiles": smiles,
            "ok": True,
            "formula": rdMolDescriptors.CalcMolFormula(mol),
            "mw": round(float(Descriptors.MolWt(mol)), 2),
            "exact_mass": round(exact, 4),
            "mz_pos": round(exact + _PROTON, 4),        # [M+H]+
            "mz_neg": round(exact - _PROTON, 4),        # [M-H]-
            "mz_na": round(exact + _SODIUM, 4),         # [M+Na]+
            "heavy_atoms": int(mol.GetNumHeavyAtoms()),
            # 点板 / 过柱极性
            "logp": logp,
            "tpsa": tpsa,
            "hbd": int(rdMolDescriptors.CalcNumHBD(mol)),
            "hba": int(rdMolDescriptors.CalcNumHBA(mol)),
            "rotatable": int(rdMolDescriptors.CalcNumRotatableBonds(mol)),
            "aromatic_rings": int(rdMolDescriptors.CalcNumAromaticRings(mol)),
        }
    except Exception as e:  # noqa: BLE001
        return {"smiles": smiles, "ok": False, "error": f"{type(e).__name__}: {e}"}

def mass_info(smiles_list: list) -> dict:
    """计算产物 / 各前体的分子式、精确质量与常见加合物 m/z。"""
    uniq: list[str] = []
    for s in smiles_list:
        s = str(s or "").strip()
        if s and s not in uniq:
            uniq.append(s)
    rows = [_one_mass(s) for s in uniq]
    by_smiles = {r["smiles"]: r for r in rows}
    return {"rows": rows, "by_smiles": by_smiles}

# 「summary + reaction_type」是每个分析都要有的基础信息，前端永远勾上（不作为可取消项）。
# 其余是可选项。confidence 始终保留（帮助判断整体把握）。


def _build_system(modules: list[str] | None, extra_note: str = "") -> str:
    """按勾选模块生成 system 提示：只要求输出勾选的字段。

    modules 为空 / None = 全量（走默认 _SYSTEM）。
    extra_note: 额外约束（如「条件已由用户选定，必须沿用」），追加在最后。
    """
    tail = ("\n\n" + extra_note) if extra_note else ""
    if not modules:
        return _SYSTEM + tail
    # 去重 + 过滤未知 key + 保持 _MODULE_FIELDS 的固定顺序
    wanted = [m for m in _MODULE_FIELDS if m in set(modules)]
    if not wanted:  # 全被过滤了，退化为全量
        return _SYSTEM + tail
    # summary 永远带上（基础信息）
    if "summary" not in wanted:
        wanted = ["summary", *wanted]

    fields: list[str] = []
    desc_lines: list[str] = []
    for m in wanted:
        for f in _MODULE_FIELDS[m]["fields"]:
            fields.append(f)
        desc_lines.append(f"- {_MODULE_FIELDS[m]['desc']}")

    field_json = ", ".join(f'"{f}": …' for f in fields)
    # 🔴 `desc_lines` 必须真的拼进去（曾经算完就丢，只剩字段名）：
    #    字段名只说"要有 toxicity 这个键"，没说"toxicity 该写什么、和 safety 有何分工"。
    #    模型于是退回泛化猜测 —— 勾了 toxicity 会得到一张与 safety 高度重复、
    #    只写"注意防护"的清单，这正是这个模块要解决的问题。
    extra = (
        "本次只需分析用户勾选的这几个方面，输出一个 JSON 对象，**只包含以下字段**："
        f"{field_json}。不要输出其它任何字段；未勾选的方面（如未要求 NMR 就不要给 nmr 字段）。\n"
        "各字段要求：\n" + "\n".join(desc_lines)
    )
    return _SYSTEM + "\n\n" + extra + tail

def _fmt_mass_row(r: dict) -> str:
    if not r.get("ok"):
        return f"- {r.get('smiles')}（无法解析）"
    base = (
        f"- {r['smiles']}　分子式 {r.get('formula')}　MW {r.get('mw')}　"
        f"精确质量 {r.get('exact_mass')}　[M+H]+ {r.get('mz_pos')}　[M-H]- {r.get('mz_neg')}"
    )
    # 极性相关数值（点板/过柱模块用）：logP / TPSA / 氢键供受体 / 可旋转键 / 芳环数
    polarity = (
        f"　logP {r.get('logp')}　TPSA {r.get('tpsa')}　"
        f"HBD {r.get('hbd')} HBA {r.get('hba')}　可旋转键 {r.get('rotatable')}　芳环 {r.get('aromatic_rings')}"
    )
    return base + polarity

def _fmt_chosen_condition(chosen: dict) -> list[str]:
    """把选定的候选条件渲染成 prompt 里的几行（只列有值的字段）。"""
    lines = ["## 已选定的反应条件（用户从候选方案里挑的，本次分析必须锁定这套条件）"]
    for key, label in CONDITION_FIELD_LABELS:
        val = str(chosen.get(key) or "").strip()
        if val:
            lines.append(f"- {label}：{val}")
    for key, label in (("pros", "优点"), ("cons", "代价")):
        items = [str(x).strip() for x in (chosen.get(key) or []) if str(x).strip()]
        if items:
            lines.append(f"- {label}：" + "；".join(items))
    return lines

def build_messages(req: dict, masses: dict, json_footer: bool = True) -> list:
    """把一步反应上下文拼成对话 prompt。

    req: {product, precursors:[...], probability, model, context?, modules?, chosen_condition?}
    modules: 前端勾选的分析模块（['mechanism','order','lcms',...]），
             为空/缺省 = 全量分析。仅要求模型输出勾选字段。
    chosen_condition: 用户选定的候选条件方案（dict）。给了就把整套条件写进上下文，
             并要求 `conditions` 字段沿用这套条件，不许另换。
    json_footer: 末尾那句「请按 JSON 结构输出」是否要带上。
             **追问**场景复用的就是这段上下文，但它要的是自由文本回答 ⇒ 传 False，
             否则模型会照着 JSON 骨架答非所问。
    """
    product = str(req.get("product") or "").strip()
    precursors = [str(p).strip() for p in (req.get("precursors") or []) if str(p).strip()]
    prob = req.get("probability")
    model_name = req.get("model") or ""
    ctx = req.get("context") or {}
    modules = req.get("modules")
    chosen = req.get("chosen_condition")
    if not isinstance(chosen, dict) or not chosen:
        chosen = None

    by = masses.get("by_smiles") or {}
    lines = []
    lines.append("## 这一步反应")
    lines.append(f"目标产物（产物侧）：{product}")
    if by.get(product, {}).get("ok"):
        p = by[product]
        lines.append(f"　其分子式 {p['formula']}，MW {p['mw']}，精确质量 {p['exact_mass']}")
    lines.append("前体（反应物侧，来自逆合成模型预测）：")
    for i, s in enumerate(precursors, 1):
        m = by.get(s, {})
        if m.get("ok"):
            lines.append(f"  {i}. {s}　分子式 {m['formula']}　MW {m['mw']}　[M+H]+ {m['mz_pos']}")
        else:
            lines.append(f"  {i}. {s}")
    if prob is not None:
        lines.append(f"逆合成模型给的这一步置信度：{prob}")
    if model_name:
        lines.append(f"预测模型：{model_name}")

    if ctx:
        lines.append("")
        lines.append("## 路线上下文")
        if ctx.get("target"):
            lines.append(f"整条路线的最终目标分子：{ctx['target']}")
        if ctx.get("step_no"):
            lines.append(f"这是整条路线中的第 {ctx['step_no']} 步（第 1 步为最接近目标产物的那步）")
        if ctx.get("prior_steps"):
            lines.append("从目标分子往下到这里已经走过的步骤：")
            for s in ctx["prior_steps"]:
                lines.append(f"  - {s}")

    if chosen:
        lines.append("")
        lines.extend(_fmt_chosen_condition(chosen))

    # 把确定性的质量表一起喂进去，让模型在此基础上解释，而不是自己瞎算
    rows = [r for r in (masses.get("rows") or [])]
    if rows:
        lines.append("")
        lines.append("## 已知的确定性数据（RDKit 计算，可直接引用，不要改动这些数值）")
        for r in rows:
            lines.append(_fmt_mass_row(r))

    lines.append("")
    if json_footer:
        if chosen:
            lines.append(
                "请按系统提示里的 JSON 结构输出这一步的工艺分析（含 mechanism：逐步电子转移机理）。"
                "**注意：反应条件已由用户在候选方案中选定（见上）** —— `conditions` 字段必须沿用这套条件，"
                "不要另换溶剂 / 温度 / 试剂体系；投料顺序、后处理、机理都要与它自洽。"
                "如果你认为这套条件有问题，仍按它写，把问题写进 `outcome.risk` 与 `safety`。"
            )
        else:
            lines.append("请按系统提示里的 JSON 结构输出这一步的工艺分析（含 mechanism：逐步电子转移机理）。")

    system_note = ""
    if chosen:
        system_note = (
            "用户已从候选条件方案里选定了一套条件（见用户消息里的「已选定的反应条件」）。"
            "`conditions` 字段必须原样沿用它的溶剂 / 温度 / 试剂体系与当量，不要另起一套；"
            "你的任务是把它展开成可执行的细节，而不是重新选条件。"
        )

    return [
        {"role": "system", "content": _build_system(modules, system_note)},
        {"role": "user", "content": "\n".join(lines)},
    ]

def _norm_conditions(data: Optional[dict], count: int) -> Optional[dict]:
    """归一化模型输出的候选条件方案。

    🔴 为什么必须归一化：前端直接把这些字段渲染成卡片，模型漏字段 / 把 `pros` 写成
       一整段字符串都是常见输出（实测），不兜底就会出现整块空白或 "undefined"。
       这里统一成「字符串字段一定是 str、pros/cons 一定是字符串数组、id 一定存在且唯一」。
    """
    if not isinstance(data, dict):
        return None
    raw = data.get("options")
    if not isinstance(raw, list):
        return None

    def _text(obj: dict, key: str) -> str:
        val = obj.get(key)
        if val is None or isinstance(val, (dict, list)):
            return ""
        return str(val).strip()

    def _list(obj: dict, key: str) -> list[str]:
        val = obj.get(key)
        if isinstance(val, list):
            return [str(x).strip() for x in val if not isinstance(x, (dict, list)) and str(x).strip()]
        if isinstance(val, str):
            # 「温和；不需严格无水」这类挤成一行的写法按分隔符拆开
            return [part.strip() for part in re.split(r"[；;\n]+", val) if part.strip()]
        return []

    options: list[dict] = []
    seen: set[str] = set()
    # 🔴 必须先过滤非对象、再按 count 截断：反过来的话，模型混进来的一条字符串
    #    （实测会出现在 options 里）会白吃掉一个方案配额 ⇒ 用户要 4 套只拿到 3 套。
    items = [x for x in raw if isinstance(x, dict)]
    for i, item in enumerate(items[: max(1, count)]):
        out: dict = {}
        for key in _CONDITION_KEEP:
            out[key] = _text(item, key)
        # 空名 / 缺 id 都要兜住：id 是前端「选定方案」的键，不能缺也不能重
        if not out["id"] or out["id"] in seen:
            out["id"] = _CONDITION_ID_LETTERS[len(options) % len(_CONDITION_ID_LETTERS)]
        seen.add(out["id"])
        if not out["name"]:
            out["name"] = f"方案 {out['id']}"
        out["pros"] = _list(item, "pros")
        out["cons"] = _list(item, "cons")
        try:
            conf = item.get("confidence")
            out["confidence"] = round(max(0.0, min(1.0, float(conf))), 2) if conf is not None else None
        except (TypeError, ValueError):
            out["confidence"] = None
        options.append(out)

    if not options:
        return None
    data["options"] = options
    for key in ("reaction_type", "key_variable", "recommendation", "note"):
        data[key] = _text(data, key)
    # 「未经实验验证」这句是安全底线，模型漏了就补上（前端直接展示 note）
    if not data.get("note"):
        data["note"] = "以上条件为模型基于反应类型的推断，未经实验验证；实验前必须由专业人员复核。"
    return data

def build_conditions_messages(req: dict, masses: dict, count: int = 4) -> list:
    """把「枚举这一步的候选反应条件」拼成对话 prompt。

    🔴 上下文与深入分析**同一份**（复用 build_messages 的正文，json_footer=False，且显式
       把 chosen_condition 置空）—— 否则枚举时会看到上一次选定的条件，被带偏成"再给一套类似的"。
    """
    ctx_text = build_messages(
        {**req, "modules": None, "chosen_condition": None}, masses, json_footer=False
    )[-1]["content"]
    system = CONDITIONS_SYSTEM.replace("{count}", str(count))
    user = (
        f"以下是这一步反应的背景资料。请先读完，然后给出 {count} 套可行的反应条件方案供选择。\n\n"
        + ctx_text
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

def build_followup_messages(req: dict, masses: dict) -> list:
    """把「这一步反应的追问」拼成对话 prompt。

    req: {product, precursors, probability, model, context?,
          analysis?（上一次抽出的结构化分析）, history?[{role,content}], question}

    🔴 上下文必须与首次分析**同一份**（复用 build_messages 的正文，json_footer=False），
       否则模型会答成对另一个反应的评论 —— 追问的价值全在「带着上下文」这一点上。
    """
    # 复用首轮分析的上下文正文（产物 / 前体 / 路线 / RDKit 确定性数据），但不带 JSON 输出要求
    ctx_text = build_messages({**req, "modules": None}, masses, json_footer=False)[-1]["content"]
    messages: list[dict] = [
        {"role": "system", "content": FOLLOWUP_SYSTEM},
        {
            "role": "user",
            "content": "以下是这一步反应的背景资料，请先读完，再回答后面的追问。\n\n" + ctx_text,
        },
    ]

    data = req.get("analysis")
    if isinstance(data, dict) and data:
        # 截断保护：正常分析 JSON 只有几 KB，超过 12k 说明结构异常，别把上下文撑爆
        blob = json.dumps(data, ensure_ascii=False)
        messages.append(
            {
                "role": "user",
                "content": (
                    "以下是我此前已经给出的这份工艺分析（JSON）。回答时要与它保持一致；"
                    "如果用户指出其中某条不对，就按正确结论修正。\n\n" + blob[:12000]
                ),
            }
        )
        messages.append({"role": "assistant", "content": "收到，背景与之前的分析我都看过了，请提问。"})

    history = req.get("history") or []
    for m in history[-12:]:  # 只带最近 12 条，避免上下文无限膨胀
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": str(req.get("question") or "").strip()})
    return messages
