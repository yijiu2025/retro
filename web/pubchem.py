# -*- coding: utf-8 -*-
"""PubChem 物质查询（PUG REST）—— 「CAS 号 / 名称 → 结构」的服务端实现。

## 为什么后端也做一份（前端不是已经有吗）

前端 `retroweb/src/utils/cas-lookup.ts` 已经能直连 PubChem（它有 CORS 头）。
这里再实现一遍是为了：

1. **两个数据源入口统一**。前端选 PubChem 还是 ChemIndex 走的是**同一个**
   `/api/substance` 接口，不必维护两套分支逻辑（一套本地、一套走代理）。
2. **桌面打包版**可能跑在离线/内网环境，外部请求由服务端统一出口更可控。

## 能力（与 ChemIndex 互补）

| | 本模块（PubChem） | ChemIndex |
|---|---|---|
| CAS → 结构 | ✅ 直接给 SMILES | ✅（InChI 转） |
| 名称 → CAS | ✅ | ✅（仅英文名） |
| 中文名 | ✅ | ❌ |
| 物性（熔点/沸点/闪点） | 部分 | ✅ 齐全 |
| 危害符号 / 风险代码 | ✅ | ✅ |

## 实测事实（2026-10-20）

· `https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/<CAS>/property/.../JSON`
· 响应头 `Access-Control-Allow-Origin: *`（但本模块是服务端用，无所谓）
· 404 = 没收录（响应体 `Fault.Message: "No CID found"`）
· CAS 号只有数字和连字符，**可以安全放进 URL 路径**
  （物质名称里有 `/`、`#` 等字符，必须 URL 编码）
· 返回的 SMILES 是 **Kekulé 形式**，交给调用方规范化
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: PUG REST 基础路径
_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name"

#: 需要的属性（CAS/SMILES/分子式/分子量/IUPAC 名）
_PROPS = "CanonicalSMILES,IsomericSMILES,MolecularFormula,MolecularWeight,IUPACName"

#: 单次请求超时（秒）
_TIMEOUT = 15

#: 缓存有效期：CAS/名称是稳定数据，24 小时
_CACHE_TTL = 24 * 3600

#: CAS 号形态（与 app.py 的 `_CAS_FORMAT_RE`、前端 `cas-regex.ts` 一致）
_CAS_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")

#: 进程内缓存：键 → (到期时间, 结果)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

#: 最小间隔（PubChem 限流 5 req/s，这里留一倍余量）
_MIN_GAP = 0.25
_last_call = 0.0
_gap_lock = threading.Lock()


class PubChemError(RuntimeError):
    """PubChem 查询失败（网络 / 服务端错误 / 限流）。"""


def _throttle() -> None:
    global _last_call
    with _gap_lock:
        now = time.monotonic()
        wait = _MIN_GAP - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _fetch(query: str) -> dict[str, Any]:
    """查一次 PubChem，返回规范化字段；查无此物返回 {}。

    两次请求：先拿结构属性，再拿同义词（CAS 号混在同义词里）。
    🔴 只在**按名称查**时才做第二次 —— 按 CAS 号查时 CAS 就是查询本身，
       没必要再打一次网络（限流 5 req/s，能省则省）。
    """
    url = f"{_BASE}/{urllib.parse.quote(query)}/property/{_PROPS}/JSON"
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": "retrochimera-web"})
    try:
        # 🔴 强制直连（ProxyHandler({})）：绕开环境里可能失效的 HTTP_PROXY。
        #    实测继承到 `http://127.0.0.1:49232` 时对本进程不通，报
        #    `os error 10061`，症状是「上游查询 502」。详见 web/chemindex.py 同款注释。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}  # 查无此物
        raise PubChemError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise PubChemError(f"{type(e).__name__}: {e}") from e

    rows = data.get("PropertyTable", {}).get("Properties", [])
    if not rows:
        return {}
    p = rows[0]

    # `SMILES` 含立体、`ConnectivitySMILES` 不含；老版本只有 SMILES
    smiles = str(p.get("SMILES") or "").strip()
    out: dict[str, Any] = {}
    if smiles:
        out["smiles"] = smiles
    if p.get("IUPACName"):
        out["name"] = str(p["IUPACName"]).strip()
    if p.get("MolecularFormula"):
        out["formula"] = str(p["MolecularFormula"]).strip()
    try:
        if p.get("MolecularWeight"):
            out["mw"] = float(p["MolecularWeight"])
    except (TypeError, ValueError):
        pass
    if p.get("CID"):
        out["cid"] = int(p["CID"])

    # CAS 号：按 CAS 号查就直接回显；按名称查要去同义词里翻
    if _CAS_RE.match(query):
        out["cas"] = query
    else:
        cas = _fetch_cas_from_synonyms(query)
        if cas:
            out["cas"] = cas

    return out


def _fetch_cas_from_synonyms(query: str) -> str:
    """按名称查时，从同义词列表里翻 CAS 号。

    🔴 CAS 号**混在同义词里**（`aspirin` 的第 1 条就是 `50-78-2`，
        后面还有 `98201-60-6` 等历史号）⇒ 取**第一条**命中的即可。
    失败不影响主结果（拿不到 CAS 只是少一个字段）。
    """
    url = f"{_BASE}/{urllib.parse.quote(query)}/synonyms/JSON"
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": "retrochimera-web"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 同义词拿不到不算失败
        return ""

    info = (data.get("InformationList", {}).get("Information") or [{}])[0]
    for s in info.get("Synonym") or []:
        s = str(s).strip()
        if _CAS_RE.match(s):
            return s
    return ""


def lookup_pubchem(query: str, choice: str = "cas") -> dict[str, Any]:
    """按 CAS 号或名称查 PubChem。

    :param query: CAS 号（如 `50-78-2`）或物质名称（如 `aspirin`）
    :param choice: `cas` 或 `name`（只影响查询语义，PubChem 接口相同）
    :return: ``{}`` 表示查无此物；否则含 `smiles`/`name`/`formula` 等字段
    :raises PubChemError: 网络或服务端错误
    """
    key = f"{choice}:{query.strip().lower()}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return dict(hit[1])

    data = _fetch(query.strip())
    with _cache_lock:
        _cache[key] = (now + _CACHE_TTL, data)
    return dict(data)


def clear_cache() -> None:
    """清空进程内缓存（测试用）。"""
    with _cache_lock:
        _cache.clear()
