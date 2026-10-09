# -*- coding: utf-8 -*-
"""大模型「工艺分析」——HTTP 传输与响应解析小工具 —— web/llm.py 的网络半边。

从 `llm.py` 拆出来的（机械搬移，逻辑未改）：原文件 2343 行一整块，
配置 / 提示词 / 传输 / 流式 四件事混在一起，改哪一样都要在两千行里翻。
对外名字空间不变 —— `llm.py` 负责再导出，app.py 不需要改。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional
from llm_config import LlmConfigError, MultiKeyError, _pool_of, _profile_creds, _tiny_tokens_text, config, key_usable, mark_key_bad, mask_key, public_status


_COOLDOWN_429 = 60.0
_ROTATE_STATUS = (401, 403, 429)
_ROTATE_PAT = re.compile(
    r"token plan|quota|insufficient|balance|arrears|unauthorized|invalid.{0,12}key|"
    r"额度|欠费|余额不足|鉴权|无权限|无效的.{0,6}key",
    re.I,
)
# ---------------------------------------------------------------------------
# 网络层：带重试的请求
# ---------------------------------------------------------------------------
# 🔴 为什么要自己写重试（实测，别再删）
# 本机环境里 `HTTPS_PROXY` 指向一个**本机瞬时端口**的出口代理
# （实测 `HTTPS_PROXY=http://127.0.0.1:51795`，端口每次会话都变）。
# 后端进程启动时把这个变量继承进去；一旦那个代理被重启/换端口，POST 就会在
# CONNECT 隧道里被掐断，OpenSSL 看到 EOF：
#
#     SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol (_ssl.c:1129)'))
#
# 而 requests **默认不重试**（urllib3 的 Retry 不把 SSLError 当可重试错误），
# 于是一次网络抖动 = 整次分析白等几分钟后失败。
# 解法两层：① 瞬时错误指数退避重试；② 环境代理整条打不通就**绕开它直连**。
#
# 可用 `RETRO_LLM_PROXY` 显式控制：
#   （不填）/ env  → 按环境变量（默认）
#   off / none     → 强制直连，忽略 HTTP(S)_PROXY
#   http://host:port → 用指定代理
_PROXY_OFF = ("off", "none", "no", "0", "false", "disable", "disabled")
_PROXY_ENV = ("", "env", "auto", "1", "on", "yes", "true")
# 值得重试的瞬时错误（都是「连接/握手/读半截」级别，重发即可能成功）
#
# 🔴 判据以 **isinstance** 为准，类名只作兜底。
#    踩过：先前只用类名匹配，漏了 `ProxyError`（它继承 ConnectionError，
#    但名字不在名单里）⇒ 代理端口一失效就直接抛出，直连兜底根本没机会跑。
_TRANSIENT_NAMES = (
    "SSLError",
    "SSLEOFError",
    "ProxyError",
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "ChunkedEncodingError",
    "ProtocolError",
    "IncompleteRead",
    "RemoteDisconnected",
)
# 🔴 关掉代理**不能**传 `proxies={}`：requests 的 `merge_environment_settings` 会对空 dict
#    做 `setdefault`，把环境变量里的代理又填回来 ⇒ 等于没关。必须显式给 None 值占位。
_DIRECT: dict = {"http": None, "https": None}

def _is_transient(err: BaseException) -> bool:
    try:
        from requests import exceptions as rex
    except Exception:  # noqa: BLE001  （requests 没装时退化为类名判断）
        return type(err).__name__ in _TRANSIENT_NAMES
    if isinstance(err, (rex.SSLError, rex.ConnectionError, rex.Timeout, rex.ChunkedEncodingError)):
        return True
    return type(err).__name__ in _TRANSIENT_NAMES

def proxies_setting() -> Optional[dict]:
    """→ None 表示「按 requests 默认（读环境变量）」，_DIRECT 表示强制直连。"""
    raw = (os.environ.get("RETRO_LLM_PROXY") or "").strip()
    low = raw.lower()
    if low in _PROXY_OFF:
        return _DIRECT
    if low in _PROXY_ENV:
        return None
    return {"http": raw, "https": raw}

def _request(
    method: str,
    url: str,
    *,
    headers: dict,
    json_body: Optional[dict] = None,
    timeout: float,
    stream: bool = False,
    attempts: int = 3,
    tried: Optional[list[str]] = None,
):
    """带指数退避重试 + 直连兜底的 HTTP 请求。

    🔴 只在**建连/握手阶段**重试 —— `stream=True` 时 `requests` 在收到响应头就返回，
    所以握手错误确实落在这一层；而已经开流之后再断，重试会导致重复付费/重复输出，
    不在这里兜（交给上层把错误如实报给用户）。
    """
    import requests

    proxies = proxies_setting()
    # 方案表：
    #   · 显式指定代理 → 只用它（用户意图明确，不偷偷绕过去）
    #   · 强制直连     → 只直连
    #   · 默认(环境)   → 先环境代理，整档打不通再**绕过它直连**
    #     （那个代理是本机临时端口，后端进程继承的是启动那一刻的值，端口一换就失效）
    if proxies == _DIRECT:
        plans: list[tuple[str, Optional[dict]]] = [("直连", _DIRECT)]
    elif proxies is None:
        plans = [("环境代理", None), ("直连（绕过环境代理）", _DIRECT)]
    else:
        plans = [("指定代理", proxies)]

    last: BaseException | None = None
    log = tried if tried is not None else []
    for label, prox in plans:
        log.append(label)
        for i in range(attempts):
            try:
                return requests.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=timeout,
                    stream=stream,
                    proxies=prox,
                )
            except requests.exceptions.RequestException as e:  # noqa: PERF203
                last = e
                if not _is_transient(e):
                    raise
                # 代理自己连不上（端口已失效 / 服务没起）⇒ 在这一档重试没意义，直接换下一档
                if type(e).__name__ == "ProxyError":
                    break
                if i < attempts - 1:
                    time.sleep(0.5 * (2**i))
    assert last is not None
    raise last

def network_error_text(err: BaseException, tried: list[str], url: str) -> str:
    """把网络异常翻成「人话 + 该动哪个开关」。"""
    host = url.split("/")[2] if "//" in url else url
    return (
        f"连不上大模型端点（{host}）。\n"
        f"　原始错误：{type(err).__name__}: {err}\n"
        f"　已尝试：{'、'.join(tried)}\n"
        f"排查顺序：\n"
        f"　1) 若本机开着代理（当前 HTTPS_PROXY={os.environ.get('HTTPS_PROXY') or '未设置'}），"
        f"把它关掉再试：在 web/.env 里加一行 RETRO_LLM_PROXY=off\n"
        f"　2) 若必须走代理（公司网络），显式指定：RETRO_LLM_PROXY=http://127.0.0.1:7890\n"
        f"　3) 确认这台机器能直连 {host}：curl -I https://{host}"
    )

def request_chat(
    cfg: dict,
    *,
    json_body: dict,
    stream: bool,
    timeout: float,
    accept: str = "text/event-stream",
    tried: Optional[list[str]] = None,
):
    """带 **多 Key 自动轮换** 的 chat 请求。返回 (resp, key_idx)。

    从当前活跃 key 开始按序试整个 key 池：
      · 处于失效/冷却中的 key 直接跳过；
      · 响应命中轮换判据（401/403/429 或套餐/额度类错误）→ 标记该 key、试下一个；
      · 其余情况（含其它 4xx，如参数错）原样返回，由调用方处理。
    全部 key 都不可用 → 抛 MultiKeyError（附每个 key 的原因）；网络异常原样上抛
    （换 key 解决不了断网，不值得轮换）。
    """
    keys = cfg["api_keys"]
    n = len(keys)
    if not n:
        raise MultiKeyError(
            "这套端点配置里没有任何 API Key（在「运行参数 → 大模型设置 → 端点配置」里添加，"
            "或填 .env 的 RETRO_LLM_API_KEY）"
        )
    pid = str(cfg.get("profile_id") or "")
    pool = _pool_of(pid)  # 🔴 轮换状态按配置分桶，别串到另一套的 key 上
    notes: list[str] = []
    start = pool["idx"] % n
    resp = None
    for offset in range(n):
        idx = (start + offset) % n
        if not key_usable(idx, pid):
            info = pool["bad"].get(idx) or {}
            notes.append(f"#{idx + 1} {mask_key(keys[idx])} 跳过（{(info.get('reason') or '已标记失效')[:60]}）")
            continue
        resp = _request(
            "POST",
            cfg["base_url"],
            headers=_headers(cfg, keys[idx], accept),
            json_body=json_body,
            timeout=timeout,
            stream=stream,
            tried=tried,
        )
        if resp.status_code in _ROTATE_STATUS or (
            resp.status_code >= 400 and _ROTATE_PAT.search(_error_text(resp))
        ):
            reason = _error_text(resp)
            mark_key_bad(idx, reason[:200], _COOLDOWN_429 if resp.status_code == 429 else 0.0, pid)
            notes.append(f"#{idx + 1} {mask_key(keys[idx])} → {reason[:120]}")
            resp = None
            continue
        pool["idx"] = idx  # 记住可用的 key，下次请求从它开始
        return resp, idx
    raise MultiKeyError(
        f"全部 {n} 个 API Key 均不可用。" + ("；".join(notes) if notes else "")
    )

def probe() -> dict:
    """真发一次最小请求，验证 **key + 模型** 是否可用（带多 key 轮换）。

    为什么需要它：`is_configured()` 只检查「填没填」，填了但 key 无效 / 模型不在套餐里，
    要等到跑完整分析才报错 —— 那可能是几分钟之后。这个探针把它变成秒级的明确答案。
    """
    cfg = config()
    pid = str(cfg.get("profile_id") or "")
    pname = str(cfg.get("profile_name") or "")

    def _out(payload: dict) -> dict:
        """每条返回都带上「验的是哪套配置」—— 多套端点并存时，光看 key/模型名认不出来。"""
        payload["profile_id"] = pid
        payload["profile_name"] = pname
        return payload

    if not cfg["model"] or not cfg["base_url"] or not cfg["api_keys"]:
        return _out({"ok": False, "verified": False, "error": public_status()["hint"]})
    prox = proxies_setting()
    mode = "直连" if prox == _DIRECT else ("环境代理/未指定" if prox is None else "指定代理")
    tried: list[str] = []
    try:
        resp, key_idx = request_chat(
            cfg,
            json_body={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 16,
            },
            stream=False,
            timeout=min(60.0, cfg["timeout"]),
            accept="application/json",
            tried=tried,
        )
    except MultiKeyError as e:
        return _out({"ok": False, "verified": False, "error": str(e), "model": cfg["model"]})
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            return _out(
                {
                    "ok": False,
                    "verified": False,
                    "error": network_error_text(e, tried, cfg["base_url"]),
                    "model": cfg["model"],
                    "proxy_mode": mode,
                }
            )
        return _out(
            {"ok": False, "verified": False, "error": f"{type(e).__name__}: {e}", "model": cfg["model"]}
        )
    if resp.status_code >= 400:
        return _out({"ok": False, "verified": False, "error": _error_text(resp), "model": cfg["model"]})
    return _out(
        {
            "ok": True,
            "verified": True,
            "model": cfg["model"],
            "base_url": cfg["base_url"],
            "key": mask_key(cfg["api_keys"][key_idx]),
            "proxy_mode": mode,
            "latency_hint": "ok",
        }
    )

def list_models(profile: Optional[dict] = None) -> dict:
    """列出这套配置的 key 能看到的模型。

    🔴 只看 `/v1/models` 会被骗：它会列出「存在但不在当前套餐内」的模型
    （实测 `deepseek-v4.1-flash` 就出现在列表里，一调就 permission_denied）。

    profile 不传 = 当前激活那套；传 `{"id": "<已保存的 id>"}` = 指那套；
    传 `{"base_url": ..., "api_keys": [...]}` = **还没保存的草稿** ——
    新建端点时得先看看有哪些模型可选，否则只能盲填模型名。

    🔴 **多把 key 必须轮换**：只试 `keys[0]` 时，第一把失效/冷却就整条拉取失败，
       而真正发请求的链路（key 轮换）早就顺延到后面那把了 —— 又一处
       「拉取口径 ≠ 运行时口径」。只有鉴权类（401/403）与限流（429）才换下一把，
       其余状态码换 key 也不会变好，如实返回、不白打请求。
    """
    if profile:
        base_url, keys = _profile_creds(profile)
    else:
        cfg = config()
        base_url = cfg["base_url"]
        # 从**当前活跃那把**开始（运行时下一次请求就用它），再顺延池里其余，去重
        keys = []
        for k in [cfg.get("api_key") or ""] + list(cfg.get("api_keys") or []):
            if k and k not in keys:
                keys.append(k)
    # 🔴 配错时**不能**回 `public_status()["hint"]` —— 已配置的实例里那个 hint 是空串，
    #    前端只能弹一句毫无信息量的「拉取失败」。这里必须说清是哪一项缺。
    if not base_url:
        return {"ok": False, "error": "这套端点配置还没填接口地址（base_url）", "models": []}
    if not keys:
        return {
            "ok": False,
            "error": "这套端点配置还没有可用的 API Key"
            "（若它是当前激活那套，也可以在 web/.env 里配 RETRO_LLM_API_KEY）",
            "models": [],
        }
    root = base_url
    for suffix in ("/chat/completions", "/completions"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    tried: list[str] = []
    errors: list[str] = []
    for key in keys:
        try:
            resp = _request(
                "GET",
                root + "/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=30,
                tried=tried,
            )
        except Exception as e:  # noqa: BLE001
            if _is_transient(e):
                # 网络层的问题换 key 救不了，如实报（诊断信息在 tried 里）
                return {
                    "ok": False,
                    "error": network_error_text(e, tried, root + "/models"),
                    "models": [],
                }
            errors.append(f"{mask_key(key)}: {type(e).__name__}: {e}")
            continue
        if resp.status_code < 400:
            rows = (resp.json() or {}).get("data") or []
            return {
                "ok": True,
                "models": [str(m.get("id")) for m in rows if m.get("id")],
                "key": mask_key(key),  # 回传脱敏串：多把 key 时得让人知道是哪把拉到的
            }
        errors.append(f"{mask_key(key)}: {_error_text(resp)}")
        if resp.status_code not in (401, 403, 429):
            break  # 与 key 无关的错误（端点/路径/服务端故障），换下一把也一样
    return {"ok": False, "error": "；".join(errors) or "拉取模型列表失败", "models": []}

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
def _headers(cfg: dict, key: str = "", accept: str = "text/event-stream") -> dict:
    """key 不传 = 当前活跃 key（轮换时由 request_chat 显式指定）。"""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key or cfg['api_key']}",
        "Accept": accept,
    }

def _body(cfg: dict, messages: list, stream: bool, json_mode: Optional[bool] = None) -> dict:
    # 🔴 清洗消息：`prefix` 是我们内部标记（assistant prefix 续写），部分网关不认这个 key
    #    会直接 400。这里把 messages 里的 `prefix` 剥掉再发，纯 OpenAI 兼容。
    clean: list[dict] = []
    for m in messages:
        m = {k: v for k, v in m.items() if k != "prefix"}
        clean.append(m)
    body: dict[str, Any] = {
        "model": cfg["model"],
        "messages": clean,
        "temperature": cfg["temperature"],
        "stream": stream,
    }
    # 🔴 必须显式给 max_tokens：不给就用网关默认值，而**推理模型的思考 token 也算在
    #    输出额度里**（实测 max_tokens=8 时思考就吃光、正文一个字都没有、finish_reason=length）。
    #    额度太小 ⇒ 返回被静默截断 ⇒ JSON 抽不出来，报错还看不出是截断导致的。
    #    max_tokens=0 表示「无限制」：用户显式选了不设上限，这里就不发字段、交给模型默认。
    tiny = _tiny_tokens_text(cfg)
    if tiny:
        # 🔴 发请求前就拦：这种额度必然连思考都用不完，发出去只是让用户白等一整轮
        #    再收到一条「被截断」（更糟的是它看起来像网络/模型故障）。
        raise LlmConfigError(tiny)
    if cfg.get("max_tokens"):
        body["max_tokens"] = int(cfg["max_tokens"])
    # 🔴 json_mode 显式传 False 时必须**覆盖**配置里的 json_mode：追问要的是自由文本回答，
    #    带上 response_format=json_object 会把模型逼成输出 JSON（实测会退化成一坨转义字符串）。
    use_json = cfg["json_mode"] if json_mode is None else bool(json_mode)
    if use_json:
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
