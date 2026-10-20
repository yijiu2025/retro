# -*- coding: utf-8 -*-
"""ChemIndex 查询客户端（https://www.chemindex.com/）—— 「CAS / 名称 → 物质信息」。

## 为什么放在后端而不是前端

ChemIndex 有**两道**前端过不去的坎（2026-10-20 实测）：

1. **没有 CORS 头**。实测响应头里**没有** `Access-Control-Allow-Origin`
   ⇒ 浏览器里的 `fetch` 会被 CORS 策略直接拒掉。
2. **有反爬挑战**。首次请求返回一段 JS：把响应里的 `_0x2` 写进
   `HW_CHECK` cookie 再重发（≈700 字节的「正在进行安全检查」页面）。
   这套机制靠**执行 JS**才能通过，纯 HTTP 客户端第一发必被拦。

⇒ 只能由服务端代劳：服务端能执行那段 JS 的等价逻辑（值就在响应里），
   再把结果整理成 JSON 交给前端。

## 它的独有价值（相对 PubChem）

| | PubChem | ChemIndex |
|---|---|---|
| CAS → 结构 | ✅ 直接给 SMILES | 给 **InChI**（无 SMILES） |
| 名称 → CAS | ✅ | ✅（英文名） |
| 中文名 | ✅ | ❌（实测查不到） |
| 物性（熔点/沸点/闪点/溶解度/蒸气压） | 部分 | ✅ 齐全 |
| 危害符号 / 风险代码 / 安全说明 | ✅ | ✅ |
| 供应商列表 | ❌ | ✅ |

⇒ **两者互补**：PubChem 负责「结构」，ChemIndex 负责「物质档案 + 供应商」。

## InChI → SMILES

ChemIndex **不给 SMILES**，只给 InChI。转换用**完整的** Python RDKit
（前端用的 RDKit MinimalLib 只有 `get_inchi`（结构→InChI），
**没有** InChI→结构，方向反了 ⇒ 前端做不了这个转换）。

## 缓存

ChemIndex 没给限流说明，但反爬本身意味着**不能打太猛**。
这里按「同一查询 24 小时内只打一次」缓存（CAS 号是稳定数据）。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# --- Windows SSL：**必须在进程最开头修** ---------------------------------
# 🔴 这里刻意**不做**修复，只做说明。原因（2026-10-20 实测踩过）：
#
#    `http.client` 在模块顶层这样做：
#        try:
#            import ssl
#        except ImportError:
#            pass          # ← ssl 不可用就**不定义** HTTPSConnection
#        else:
#            class HTTPSConnection(HTTPConnection): ...
#
#    ⇒ conda 未激活时 Python 启动后不久 `_ssl` 就加载失败（Library/bin 不在
#      PATH），这个缺陷**永久烙在 http.client 模块里**。之后在本文件里
#      `from bootstrap import envfix` 再补 PATH 已经晚了 —— 报
#      "URLError: unknown url type: https"，而且**不是** SSL 报错，
#      极难查。实测：envfix 放本文件里 = 无效。
#
#    ✅ 正确位置是**进程最外层入口**：`run_api.py`（第 63 行，早于任何第三方
#      import）、`web/model_server.py`。API 服务走的就是这条路径 ⇒ **线上没问题**。
#
# ⚠️ 直接 `python -c "import chemindex"` 或单测文件裸跑会踩这个坑。
#    测这个模块请用仓库提供的入口（run_api.py 或 start_api.sh 起服务后打接口），
#    或在测试脚本最开头手动补 PATH + CDLL：
#        import os, sys, ctypes
#        _bin = os.path.join(os.path.dirname(sys.executable), "Library", "bin")
#        os.add_dll_directory(_bin)
#        [ctypes.CDLL(os.path.join(_bin, n)) for n in
#            ("libcrypto-1_1-x64.dll", "libssl-1_1-x64.dll") if os.path.exists(...)]

#: 反爬挑战里的 cookie 名（实测：响应 JS 里 document.cookie = "HW_CHECK" + "=" + _0x2）
_COOKIE_NAME = "HW_CHECK"

#: 挑战页里的十六进制值
_CHALLENGE_RE = re.compile(r'var _0x2\s*=\s*"([0-9a-fA-F]+)"')

#: 安全检查页的标志（出现了就说明这发被拦了）
_CHALLENGE_MARK = "安全检查"

#: 浏览器 UA —— 不给 UA 会被直接 433 拒掉（实测）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_BASE = "https://www.chemindex.com/index/"

#: 单次请求超时（秒）
_TIMEOUT = 15

#: 缓存有效期：24 小时
_CACHE_TTL = 24 * 3600

#: 进程内缓存：查询键 → (到期时间, 结果)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

#: 同一个查询的最小间隔（秒）—— 反爬很敏感，别连发
_min_gap = 0.6
_last_call = 0.0
_gap_lock = threading.Lock()


class ChemIndexError(RuntimeError):
    """ChemIndex 查询失败（网络 / 被拦 / 查无此物）。"""


def _throttle() -> None:
    """全局最小间隔，避免连发触发更严的限流。"""
    global _last_call
    with _gap_lock:
        now = time.monotonic()
        wait = _min_gap - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _http_get(url: str, cookie: str = "") -> tuple[int, str]:
    """GET 一个 URL，返回 (状态码, 正文)。

    🔴 **强制直连**（不用环境里的代理变量）：本服务跑在 Windows / Linux 上时，
       `HTTP_PROXY` / `HTTPS_PROXY` 常常是别的工具留下的、已经失效的地址
       （实测：agent 环境继承了 `http://127.0.0.1:49232`，而这个端口对本进程
       不通 ⇒ 报 `os error 10061 目标计算机积极拒绝`，症状是「上游查询 502」，
       极难查。curl 反而正常，因为…算了，不提了）。
       ⇒ 物质查询这种「直接打公网」的请求，**显式绕开代理**最稳。
       真需要走代理的场景（企业内网）由部署方在系统层面解决。
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    if cookie:
        req.add_header("Cookie", f"{_COOKIE_NAME}={cookie}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
        # 站点是 UTF-8（实测 charset=utf-8）
        return resp.status, raw.decode("utf-8", errors="replace")


def _strip_tags(html: str) -> str:
    """去掉 script/style 与标签，只留纯文本。"""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    return text


def _field(html: str, label: str) -> str:
    """从详情页的「标签 | 值」表格里取一个字段。

    页面结构是 `<td>Chemical Name</td><td>Acetylsalicylic acid</td>`。
    """
    pat = re.compile(
        re.escape(label) + r"\s*</td>\s*<td[^>]*>\s*(.*?)\s*</td>",
        re.S | re.I,
    )
    m = pat.search(html)
    if not m:
        return ""
    val = m.group(1)
    # 值里可能夹着 <a>/<b> 等标签
    val = re.sub(r"<[^>]+>", "", val)
    val = val.replace("&nbsp;", " ").replace("&amp;", "&").strip()
    return re.sub(r"\s+", " ", val)


def _parse_detail(html: str) -> dict[str, Any]:
    """从详情页 HTML 里抽出结构化字段。"""
    out: dict[str, Any] = {}

    cas = _field(html, "CAS Registry Number")
    if cas:
        out["cas"] = cas

    name = _field(html, "Chemical Name")
    if name:
        out["name"] = name

    formula = _field(html, "Molecular Formula")
    if formula:
        out["formula"] = formula

    mw = _field(html, "Molecular Weight")
    if mw:
        try:
            out["mw"] = float(mw)
        except ValueError:
            pass

    inch = _field(html, "InChl") or _field(html, "InChI")
    if inch:
        out["inchi"] = inch

    synonyms = _field(html, "Synonyms")
    if synonyms:
        # 分隔符实测是 `; `，也可能有 `, `
        parts = [p.strip() for p in re.split(r"[;,]", synonyms) if p.strip()]
        if parts:
            out["synonyms"] = parts[:20]

    # 物性与安全信息（ChemIndex 的独有部分）
    for key, label in (
        ("melting_point", "Melting Point"),
        ("boiling_point", "Boiling Point"),
        ("flash_point", "Flash Point"),
        ("water_solubility", "Water Solubility"),
        ("vapour_pressure", "Vapour Pressur"),  # 站点原文少个 e，别改
        ("hazard_symbols", "Hazard Symbols"),
        ("risk_codes", "Risk Codes"),
        ("safety_description", "Safety Description"),
        ("einecs", "EINECS"),
    ):
        val = _field(html, label)
        if val:
            out[key] = val

    return out


def _fetch_detail(terms: str, choice: str) -> dict[str, Any]:
    """查一次 ChemIndex，返回解析好的字段。

    两步走：
      1. 先请求一次 —— 要么直接给详情页，要么给挑战页；
      2. 若是挑战页，从里面取出 `_0x2` 当 cookie 再请求一次。
    """
    url = (
        _BASE
        + "?f=show_cas_info&lang=en&choice="
        + urllib.parse.quote(choice)
        + "&terms="
        + urllib.parse.quote(terms)
    )

    _throttle()
    status, html = _http_get(url)

    # 被反爬拦了：把挑战值当 cookie 重发一次
    if _CHALLENGE_MARK in html or _CHALLENGE_RE.search(html):
        m = _CHALLENGE_RE.search(html)
        if not m:
            raise ChemIndexError("ChemIndex 反爬挑战无法识别")
        _throttle()
        status, html = _http_get(url, cookie=m.group(1))
        if _CHALLENGE_MARK in html:
            raise ChemIndexError("ChemIndex 反爬校验未通过")

    if status != 200:
        raise ChemIndexError(f"ChemIndex 返回 HTTP {status}")

    # 详情页判定：有 CAS 字段 ⇒ 查到了
    data = _parse_detail(html)
    if not data.get("cas"):
        # 没有详情 ⇒ 查无此物
        return {}

    return data


def _inchi_to_smiles(inchi: str) -> str:
    """InChI → 规范 SMILES（用完整的 Python RDKit）。

    🔴 前端那套 RDKit MinimalLib 只有 `get_inchi`（结构→InChI），
       **没有** InChI→结构 ⇒ 只能在这儿转。

    **必须中性化**：ChemIndex 给的 InChI 常带 `/p-1`（去质子层），
    直接转出来的是 **阴离子**。实测阿司匹林 `50-78-2`：
      原样转   → `CC(=O)Oc1ccccc1C(=O)[O-]`
      中性化后 → `CC(=O)Oc1ccccc1C(=O)O`  ← 与 PubChem 的一致
    ⇒ 不中性化的话，用户从 ChemIndex 查到的结构和从 PubChem 查到的
      长得不一样，同一种物质会在界面里呈现两种写法。
    """
    if not inchi:
        return ""
    try:
        from rdkit import Chem

        mol = Chem.MolFromInchi(inchi)
        if mol is None:
            return ""
        smiles = Chem.MolToSmiles(mol)
        # 只在中性化真能改变结果时才做（省得白白 import MolStandardize）
        if "[" in smiles and ("-" in smiles or "+" in smiles):
            try:
                from rdkit.Chem.MolStandardize import rdMolStandardize

                uncharged = rdMolStandardize.Uncharger().uncharge(mol)
                fixed = Chem.MolToSmiles(uncharged)
                # 保守起见：只接受「电荷确实消失了」的结果
                if "[" not in fixed and fixed:
                    return fixed
            except Exception:
                pass
        return smiles
    except Exception:
        return ""


def lookup(terms: str, choice: str = "cas") -> dict[str, Any]:
    """查一个 CAS 号或名称。

    :param terms: CAS 号（如 `50-78-2`）或物质名称（如 `aspirin`）
    :param choice: `cas`（按 CAS 号查）或 `name`（按名称查）
    :return: ``{}`` 表示查无此物；否则是含 `cas`/`name`/`formula`/`inchi`/
             `smiles` 等字段的字典
    :raises ChemIndexError: 网络失败 / 被反爬拦住
    """
    key = f"{choice}:{terms.strip().lower()}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return dict(hit[1])

    data = _fetch_detail(terms.strip(), choice)
    if not data:
        # 查无此物也缓存 —— 免得反复打它
        with _cache_lock:
            _cache[key] = (now + _CACHE_TTL, {})
        return {}

    # 补上 SMILES（ChemIndex 不给，用 InChI 转）
    if data.get("inchi") and not data.get("smiles"):
        data["smiles"] = _inchi_to_smiles(data["inchi"])

    with _cache_lock:
        _cache[key] = (now + _CACHE_TTL, data)
    return dict(data)


def clear_cache() -> None:
    """清空进程内缓存（测试用）。"""
    with _cache_lock:
        _cache.clear()
