# -*- coding: utf-8 -*-
"""大模型「工艺分析」——把一步逆合成反应，展开成可执行的工艺细节。

职责（单一）
------------
给定一步反应（产物 + 前体 + 模型概率），让一个 OpenAI 兼容的对话模型输出**结构化 JSON**：

    投料顺序 / 当量 / 反应条件 / 预期结果与收率 / 可能副产物 / LCMS / NMR / 安全提示

本模块**不碰**模型（RetroChimera）也不碰 Flask：它只负责
「构造 prompt → 调 LLM → 抽取 JSON」，由 app.py 包成 HTTP 端点。

🔴 两个刻意的设计
-----------------
1) **LCMS 的数值不交给大模型算**。精确质量是确定性计算，交给 RDKit（`mass_info`），
   大模型只负责解释与补充（保留时间趋势、特征碎片、加合物选择）。
   让模型去算 m/z 是幻觉的高发区，而这里是化学家真正会去核对的数字。

2) **流式与结构化并存**。LLM 逐 token 吐，我们原样转发（前端能实时看到输出在长），
   同时在流结束时抽出 JSON 一并下发。前端既能看到「它在写」，又能拿到规整的表格。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Iterator, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))

# 质子质量（用于 [M+H]+ / [M-H]- 的近似）
_PROTON = 1.007276466621
_SODIUM = 22.989218

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_ENV_LOADED = False


def _load_env_file() -> None:
    """极简 .env 读取（不引入 python-dotenv 依赖）。

    🔴 两条语义（原先实现与本文档不符，已修正）
    1) **按 mtime 热重载**：文件没改就走缓存，改了立刻生效 —— 不需要重启 Flask。
    2) **空值 = 未设置**：`RETRO_LLM_API_KEY=`（空）不会占住位置挡住后续填值；
       真实环境变量只要**非空**就优先于文件（便于 `set RETRO_LLM_MODEL=...` 临时覆盖）。
       文件里**删掉**的行也会真正失效（重置回未被文件污染的基线）。
    """
    global _ENV_LOADED
    paths = (os.path.join(_HERE, ".env"), os.path.join(os.path.dirname(_HERE), ".env"))

    stamp = 0.0
    for path in paths:
        try:
            stamp = max(stamp, os.path.getmtime(path))
        except OSError:
            pass
    if _ENV_LOADED and stamp == _load_env_file.stamp:  # type: ignore[attr-defined]
        return
    _ENV_LOADED = True
    _load_env_file.stamp = stamp  # type: ignore[attr-defined]

    # 首次调用：快照「真实环境变量」作为基线，之后每轮都从基线重放，
    # 这样从 .env 里删掉的行才能真正失效（而不是永久留在 os.environ 里）。
    if not hasattr(_load_env_file, "baseline"):
        _load_env_file.baseline = {  # type: ignore[attr-defined]
            k: v for k, v in os.environ.items() if k.startswith("RETRO_") and v
        }
    baseline: dict = _load_env_file.baseline  # type: ignore[attr-defined]
    for k in [k for k in os.environ if k.startswith("RETRO_") and k not in baseline]:
        del os.environ[k]
    os.environ.update(baseline)

    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and not os.environ.get(key):
                        os.environ[key] = val
        except OSError:
            continue


def config() -> dict:
    """读当前 LLM 配置（每次都重读 ⇒ 改 .env 后下一次请求即生效）。"""
    _load_env_file()
    base = (os.environ.get("RETRO_LLM_BASE_URL") or "").strip().rstrip("/")
    # 允许填到 /v1 为止，也允许直接填完整的 chat/completions
    if base and not base.endswith("/chat/completions"):
        base = base + "/chat/completions"
    return {
        "base_url": base,
        "api_key": (os.environ.get("RETRO_LLM_API_KEY") or "").strip(),
        "model": (os.environ.get("RETRO_LLM_MODEL") or "").strip(),
        "timeout": float(os.environ.get("RETRO_LLM_TIMEOUT", "180") or 180),
        "max_tokens": int(os.environ.get("RETRO_LLM_MAX_TOKENS", "8192") or 8192),
        "json_mode": (os.environ.get("RETRO_LLM_JSON_MODE", "1") or "1").strip() not in ("0", "false", "False", "no"),
        "temperature": float(os.environ.get("RETRO_LLM_TEMPERATURE", "0.3") or 0.3),
    }


def is_configured() -> bool:
    c = config()
    return bool(c["base_url"] and c["model"] and c["api_key"])


def public_status() -> dict:
    """给前端的「脱敏」状态：不回传 api_key。"""
    c = config()
    return {
        "configured": is_configured(),
        "base_url": c["base_url"],
        "model": c["model"],
        "has_key": bool(c["api_key"]),
        "json_mode": c["json_mode"],
        "hint": "" if is_configured() else (
            "请在 F:\\retrochimera\\web\\.env 里填 RETRO_LLM_BASE_URL / RETRO_LLM_API_KEY / RETRO_LLM_MODEL"
            "（改完立即生效，无需重启）。"
        ),
    }


def probe() -> dict:
    """真发一次最小请求，验证 **key + 模型** 是否可用。

    为什么需要它：`is_configured()` 只检查「填没填」，填了但 key 无效 / 模型不在套餐里，
    要等到跑完整分析才报错 —— 那可能是几分钟之后。这个探针把它变成秒级的明确答案。
    """
    import requests

    cfg = config()
    if not cfg["api_key"] or not cfg["model"] or not cfg["base_url"]:
        return {"ok": False, "verified": False, "error": public_status()["hint"]}
    try:
        resp = requests.post(
            cfg["base_url"],
            headers={**_headers(cfg), "Accept": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 16,
            },
            timeout=min(60.0, cfg["timeout"]),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "verified": False, "error": f"{type(e).__name__}: {e}", "model": cfg["model"]}
    if resp.status_code >= 400:
        return {"ok": False, "verified": False, "error": _error_text(resp), "model": cfg["model"]}
    return {
        "ok": True,
        "verified": True,
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "latency_hint": "ok",
    }


def list_models() -> dict:
    """列出该 key 能看到的模型，并逐个标注是否**真的能调**。

    🔴 只看 `/v1/models` 会被骗：它会列出「存在但不在当前套餐内」的模型
    （实测 `deepseek-v4.1-flash` 就出现在列表里，一调就 permission_denied）。
    """
    import requests

    cfg = config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"ok": False, "error": public_status()["hint"], "models": []}
    root = cfg["base_url"]
    for suffix in ("/chat/completions", "/completions"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    try:
        resp = requests.get(root + "/models", headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=30)
        if resp.status_code >= 400:
            return {"ok": False, "error": _error_text(resp), "models": []}
        rows = (resp.json() or {}).get("data") or []
        return {"ok": True, "models": [str(m.get("id")) for m in rows if m.get("id")]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}


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

JSON 结构（字段必须齐全，值可以为空字符串或空数组）：
{
  "summary": "一两句话总述这步反应是什么转化、关键控制点是什么",
  "reaction_type": "反应类型，如 酰胺缩合 / 酯化 / Suzuki 偶联",
  "order": [
    {"seq": 1, "mode": "投料方式（一次加入/滴加/分批）", "reagent": "试剂或溶液", "equiv": "1.05 eq", "note": "温度/时间/注意点"}
  ],
  "conditions": {
    "solvent": "溶剂体系",
    "temperature": "温度程序，如 -10 °C 滴加，室温 2 h",
    "time": "总反应时间",
    "atmosphere": "气氛，如 N2",
    "catalyst": "催化剂/配体/碱/缩合剂",
    "workup": "后处理与纯化（淬灭、萃取、结晶、柱层析）"
  },
  "outcome": {"expected": "预期转化情况", "yield_range": "预计收率范围", "purity": "预计纯度/杂质水平", "risk": "主要风险点"},
  "byproducts": [{"name": "副产物名", "source": "生成的机理来源", "mz": "该副产物 [M+H]+ 近似 m/z", "control": "如何抑制/如何识别"}],
  "lcms": {"mz_theoretical": "主产物理论 m/z", "adduct": "[M+H]+", "note": "保留时间趋势 / 特征碎片 / 与副产物的区分"},
  "nmr": {"h1": "重要 1H NMR 归属（给化学位移与裂分，例如 7.9 (d, 2H)）", "c13": "重要 13C 位移", "note": "判据与注意点"},
  "safety": ["安全与放大提示"],
  "confidence": 0.0
}
`confidence` 是 0~1 的数，表示你对这份分析整体的把握。"""


def _fmt_mass_row(r: dict) -> str:
    if not r.get("ok"):
        return f"- {r.get('smiles')}（无法解析）"
    return (
        f"- {r['smiles']}　分子式 {r.get('formula')}　MW {r.get('mw')}　"
        f"精确质量 {r.get('exact_mass')}　[M+H]+ {r.get('mz_pos')}　[M-H]- {r.get('mz_neg')}"
    )


def build_messages(req: dict, masses: dict) -> list:
    """把一步反应上下文拼成对话 prompt。

    req: {product, precursors:[...], probability, model, context?}
    """
    product = str(req.get("product") or "").strip()
    precursors = [str(p).strip() for p in (req.get("precursors") or []) if str(p).strip()]
    prob = req.get("probability")
    model_name = req.get("model") or ""
    ctx = req.get("context") or {}

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

    # 把确定性的质量表一起喂进去，让模型在此基础上解释，而不是自己瞎算
    rows = [r for r in (masses.get("rows") or [])]
    if rows:
        lines.append("")
        lines.append("## 已知的确定性数据（RDKit 计算，可直接引用，不要改动这些数值）")
        for r in rows:
            lines.append(_fmt_mass_row(r))

    lines.append("")
    lines.append("请按系统提示里的 JSON 结构输出这一步的工艺分析。")

    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


# ---------------------------------------------------------------------------
# JSON 抽取（模型偶尔会带围栏或前后废话）
# ---------------------------------------------------------------------------
def extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        pass
    # 退而求其次：扫第一个平衡的 {...}
    start = t.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = t[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:  # noqa: BLE001
                        break
        start = t.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# 调用 LLM
# ---------------------------------------------------------------------------
def _headers(cfg: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        "Accept": "text/event-stream",
    }


def _body(cfg: dict, messages: list, stream: bool) -> dict:
    body: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"],
        "stream": stream,
    }
    # 🔴 必须显式给 max_tokens：不给就用网关默认值，而**推理模型的思考 token 也算在
    #    输出额度里**（实测 max_tokens=8 时思考就吃光、正文一个字都没有、finish_reason=length）。
    #    额度太小 ⇒ 返回被静默截断 ⇒ JSON 抽不出来，报错还看不出是截断导致的。
    if cfg.get("max_tokens"):
        body["max_tokens"] = int(cfg["max_tokens"])
    if cfg["json_mode"]:
        body["response_format"] = {"type": "json_object"}
    return body


def _error_text(resp) -> str:
    try:
        payload = resp.json()
        err = payload.get("error")
        if isinstance(err, dict):
            return f"HTTP {resp.status_code}: {err.get('message') or json.dumps(err, ensure_ascii=False)}"
        if err:
            return f"HTTP {resp.status_code}: {err}"
        return f"HTTP {resp.status_code}: {json.dumps(payload, ensure_ascii=False)[:400]}"
    except Exception:  # noqa: BLE001
        try:
            return f"HTTP {resp.status_code}: {(resp.text or '')[:400]}"
        except Exception:  # noqa: BLE001
            return f"HTTP {resp.status_code}"


def _delta_of(chunk: dict) -> str:
    """从流式 chunk 里取正文增量。"""
    try:
        choice = (chunk.get("choices") or [{}])[0]
    except Exception:  # noqa: BLE001
        return ""
    delta = choice.get("delta") or {}
    if isinstance(delta, dict):
        text = delta.get("content")
        if text:
            return str(text)
    # 少数服务把增量放在 message.content（非标准流式）
    msg = choice.get("message") or {}
    if isinstance(msg, dict) and msg.get("content"):
        return str(msg["content"])
    return ""


def _think_of(chunk: dict) -> str:
    """取「思考」增量（推理模型专用）。

    🔴 为什么必须单独取：`deepseek-v4-pro` / `kimi-k3` 这类推理模型，开头几十秒
    只会吐 `reasoning_content`，而 `delta.content` 一直是**空字符串**。
    只读 content 的话，前端在整个思考阶段**一个字都不显示**，看起来像卡死了，
    用户会以为服务挂了然后去点取消。
    """
    try:
        choice = (chunk.get("choices") or [{}])[0]
    except Exception:  # noqa: BLE001
        return ""
    delta = choice.get("delta") or {}
    if isinstance(delta, dict):
        for key in ("reasoning_content", "reasoning"):
            val = delta.get(key)
            if val:
                return str(val)
    return ""


def analyze(req: dict) -> dict:
    """非流式：一次性拿到结构化结果。"""
    import requests

    cfg = config()
    if not is_configured():
        raise RuntimeError(public_status()["hint"] or "大模型未配置")

    masses = mass_info([req.get("product") or ""] + list(req.get("precursors") or []))
    messages = build_messages(req, masses)

    resp = requests.post(
        cfg["base_url"],
        headers={**_headers(cfg), "Accept": "application/json"},
        json=_body(cfg, messages, stream=False),
        timeout=cfg["timeout"],
    )
    if resp.status_code >= 400:
        raise RuntimeError(_error_text(resp))
    payload = resp.json()
    raw = ""
    finish_reason = ""
    try:
        choice = (payload.get("choices") or [{}])[0] or {}
        raw = str((choice.get("message") or {}).get("content") or "")
        finish_reason = str(choice.get("finish_reason") or "")
    except Exception:  # noqa: BLE001
        raw = ""
    data = extract_json(raw)
    if finish_reason == "length" and not data:
        raise RuntimeError(
            f"模型输出被截断（finish_reason=length，正文 {len(raw)} 字符）："
            f"调大 .env 里的 RETRO_LLM_MAX_TOKENS（当前 {cfg.get('max_tokens')}）"
        )
    return {"raw": raw, "data": data, "computed": masses, "model": cfg["model"], "finish_reason": finish_reason}


def stream_analyze(req: dict) -> Iterator[str]:
    """流式：yield SSE 行。

    事件序列：
        meta         → {model, computed}          （先到，前端可立刻画出质量表）
        thinking     → {text}                     （推理模型专用，反复；非推理模型不发）
        thinking_end → {chars}                    （思考结束、正文开始）
        delta        → {text}                     （正文增量，反复）
        done         → {data, computed, raw, model, reasoning, finish_reason}
        error        → {error}
    """
    import requests

    def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    try:
        cfg = config()
        if not is_configured():
            yield sse({"type": "error", "error": public_status()["hint"] or "大模型未配置"})
            return

        masses = mass_info([req.get("product") or ""] + list(req.get("precursors") or []))
        yield sse({"type": "meta", "model": cfg["model"], "computed": masses})

        messages = build_messages(req, masses)
        collected: list[str] = []
        think_buf: list[str] = []
        think_sent = 0
        finish_reason = ""

        with requests.post(
            cfg["base_url"],
            headers=_headers(cfg),
            json=_body(cfg, messages, stream=True),
            timeout=cfg["timeout"],
            stream=True,
        ) as resp:
            if resp.status_code >= 400:
                yield sse({"type": "error", "error": _error_text(resp)})
                return
            for raw_line in resp.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                try:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                except Exception:  # noqa: BLE001
                    continue
                if not line.startswith("data:"):
                    continue
                chunk_str = line[5:].strip()
                if not chunk_str or chunk_str == "[DONE]":
                    continue
                try:
                    chunk = json.loads(chunk_str)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    fr = ((chunk.get("choices") or [{}])[0] or {}).get("finish_reason") or ""
                    if fr:
                        finish_reason = str(fr)
                except Exception:  # noqa: BLE001
                    pass
                think = _think_of(chunk)
                if think:
                    think_buf.append(think)
                    yield sse({"type": "thinking", "text": think})
                text = _delta_of(chunk)
                if text:
                    # 正文一开始，思考阶段就结束了
                    if think_sent == 0 and think_buf:
                        think_sent = 1
                        yield sse({"type": "thinking_end", "chars": len("".join(think_buf))})
                    collected.append(text)
                    yield sse({"type": "delta", "text": text})

        raw = "".join(collected)
        data = extract_json(raw)
        # 🔴 被截断时必须**响亮地失败**：否则前端只看到「JSON 解析失败 / 数据为空」，
        #    根本猜不到是 max_tokens 不够（推理模型的思考会先吃掉额度）。
        if finish_reason == "length" and not data:
            yield sse(
                {
                    "type": "error",
                    "error": (
                        f"模型输出被截断（finish_reason=length，已收到 {len(raw)} 字符正文）。"
                        f"推理模型的思考 token 也计入输出额度 ⇒ 调大 .env 里的 RETRO_LLM_MAX_TOKENS"
                        f"（当前 {cfg.get('max_tokens')}）。"
                    ),
                }
            )
            return
        yield sse(
            {
                "type": "done",
                "data": data,
                "computed": masses,
                "raw": raw,
                "model": cfg["model"],
                "reasoning": "".join(think_buf),
                "finish_reason": finish_reason,
            }
        )
    except Exception as e:  # noqa: BLE001
        yield sse({"type": "error", "error": f"{type(e).__name__}: {e}"})


def sse_stream_with_cleanup(req: dict) -> Iterator[str]:
    """把生成器包一层，保证客户端断开时不留下半截连接（Werkzeug 会自行收尾）。"""
    gen: Iterator[str] = stream_analyze(req)
    for chunk in gen:
        yield chunk
