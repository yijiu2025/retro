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
import time
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


# ---------------------------------------------------------------------------
# UI 设置层（llm_settings.json）：优先级高于 .env
# ---------------------------------------------------------------------------
# 「AI 工艺分析」面板里可直接改 接口地址 / 模型 / token 上限 / API Key 列表，
# 保存到 web/llm_settings.json（带 mtime 缓存，改完立即生效，无需重启 Flask）。
# 优先级：llm_settings.json > .env / 环境变量；.env 仍作为初始默认。
# 🔴 该文件含明文 API Key，必须进 .gitignore（与 web/.env 同待遇）。
_SETTINGS_FILE = os.path.join(_HERE, "llm_settings.json")
_SETTINGS_STAMP: dict = {"mtime": -1.0, "data": {}}


def _read_settings_file() -> dict:
    """读 UI 保存的设置（带 mtime 缓存；没有文件 / 内容非法 = 空覆盖，回落 .env）。"""
    try:
        stamp = os.path.getmtime(_SETTINGS_FILE)
    except OSError:
        _SETTINGS_STAMP["mtime"] = -1.0
        _SETTINGS_STAMP["data"] = {}
        return {}
    if stamp == _SETTINGS_STAMP["mtime"]:
        return _SETTINGS_STAMP["data"]
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    _SETTINGS_STAMP["mtime"] = stamp
    _SETTINGS_STAMP["data"] = data
    return data


def _write_settings_file(data: dict) -> None:
    """原子写入（tmp + os.replace），避免写一半被读到残缺 JSON。"""
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _SETTINGS_FILE)
    _SETTINGS_STAMP["mtime"] = os.path.getmtime(_SETTINGS_FILE)
    _SETTINGS_STAMP["data"] = data


# ---------------------------------------------------------------------------
# 多 API Key 池 + 自动轮换
# ---------------------------------------------------------------------------
# 🔴 为什么要在后端轮换：单 key 限流（429）/ 额度用尽 / 被禁用时，一次长分析
#    会整段失败。把多个 key 放进池子，命中「可轮换错误」就标记当前 key 并换下一个重试，
#    对前端完全透明（UI 在设置面板里能看到每个 key 的状态）。
#
# 轮换判据：
#   · HTTP 401 / 403 / 429（key 无效 / 无权限 / 限流）
#   · 4xx 错误文本命中套餐/额度类关键词（部分网关用 400 报 "token plan" 类错）
# 标记语义：
#   · 429 → 冷却 60s 后自动恢复（限流是暂时的）
#   · 401/403/额度 → 标记失效，直到「重试失效 Key」或改设置才复位
_KEY_STATE: dict = {"idx": 0, "bad": {}}  # bad: idx -> {"reason": str, "until": float}
_COOLDOWN_429 = 60.0
_ROTATE_STATUS = (401, 403, 429)
_ROTATE_PAT = re.compile(
    r"token plan|quota|insufficient|balance|arrears|unauthorized|invalid.{0,12}key|"
    r"额度|欠费|余额不足|鉴权|无权限|无效的.{0,6}key",
    re.I,
)


class MultiKeyError(RuntimeError):
    """全部 API Key 都不可用（附带每个 key 的失败原因，给前端直接展示）。"""


def _split_keys(raw: str) -> list[str]:
    """把逗号/空白分隔的 key 串拆成去重列表（兼容 .env 里一格多 key 的填法）。"""
    out: list[str] = []
    for part in re.split(r"[\s,;，；]+", raw or ""):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def key_pool() -> list[str]:
    """当前生效的 key 池：UI 设置优先，回落 .env（支持一格多 key）。"""
    _load_env_file()  # 幂等（mtime 缓存）：key_pool 可能先于 config() 被调
    ov = _read_settings_file()
    keys = [str(k).strip() for k in (ov.get("api_keys") or []) if str(k).strip()]
    if not keys:
        keys = _split_keys(os.environ.get("RETRO_LLM_API_KEY") or "")
    return keys


def mask_key(k: str) -> str:
    """key 脱敏：sk-L7w15y****ab3f（前端列表展示 / 回传映射都靠这个串）。"""
    if len(k) <= 12:
        return k[:3] + "****"
    return f"{k[:8]}****{k[-4:]}"


def key_usable(idx: int) -> bool:
    """cooldown 到期 = 恢复可用；until=0 = 永久失效（直到「重试失效 Key」或改设置复位）。"""
    info = _KEY_STATE["bad"].get(idx)
    if not info:
        return True
    until = info.get("until") or 0
    if until <= 0:
        return False
    return time.time() >= until


def mark_key_bad(idx: int, reason: str, cooldown: float = 0.0) -> None:
    _KEY_STATE["bad"][idx] = {"reason": reason, "until": (time.time() + cooldown) if cooldown else 0.0}


def reset_key_state() -> None:
    """清空失效/冷却标记（保存设置 / 手动「重试失效 Key」时调用）。"""
    _KEY_STATE["idx"] = 0
    _KEY_STATE["bad"].clear()


def key_states() -> list[dict]:
    """给 UI 的 key 池状态（脱敏）：active=使用中 / cooldown=冷却 / bad=失效 / idle=备用。"""
    keys = key_pool()
    n = len(keys)
    now = time.time()
    out: list[dict] = []
    for i, k in enumerate(keys):
        info = _KEY_STATE["bad"].get(i) or {}
        until = info.get("until") or 0
        if n and i == _KEY_STATE["idx"] % n and key_usable(i):
            state = "active"
        elif until and until > now:
            state = "cooldown"
        elif info:
            state = "bad"
        else:
            state = "idle"
        out.append({"masked": mask_key(k), "state": state, "reason": str(info.get("reason") or "")})
    return out


def config() -> dict:
    """读当前 LLM 配置（.env + UI 设置合并，UI 设置优先；改完下一次请求即生效）。"""
    _load_env_file()
    ov = _read_settings_file()
    base = str(ov.get("base_url") or os.environ.get("RETRO_LLM_BASE_URL") or "").strip().rstrip("/")
    # 允许填到 /v1 为止，也允许直接填完整的 chat/completions
    if base and not base.endswith("/chat/completions"):
        base = base + "/chat/completions"
    keys = key_pool()
    idx = _KEY_STATE["idx"] % len(keys) if keys else 0

    def _num(name: str, env_default: str, cast, lo: float, hi: float):
        raw = ov.get(name)
        if raw is None or raw == "":
            raw = os.environ.get(f"RETRO_LLM_{name.upper()}", env_default)
        try:
            v = cast(raw)
        except (TypeError, ValueError):
            v = cast(env_default)
        return min(hi, max(lo, v))

    def _flag(name: str, env_default: str) -> bool:
        raw = ov.get(name)
        if raw is None:
            raw = os.environ.get(f"RETRO_LLM_{name.upper()}", env_default)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off")

    return {
        "base_url": base,
        "api_keys": keys,
        "api_key": keys[idx] if keys else "",  # 当前活跃 key（向后兼容旧调用方）
        "model": str(ov.get("model") or os.environ.get("RETRO_LLM_MODEL") or "").strip(),
        "timeout": _num("timeout", "180", float, 5.0, 3600.0),
        "max_tokens": int(_num("max_tokens", "8192", int, 16, 1_000_000)),
        "json_mode": _flag("json_mode", "1"),
        "temperature": _num("temperature", "0.3", float, 0.0, 2.0),
    }


def is_configured() -> bool:
    c = config()
    return bool(c["base_url"] and c["model"] and c["api_keys"])


def public_status() -> dict:
    """给前端的「脱敏」状态：不回传 api_key 明文，回传 key 池状态列表。"""
    c = config()
    return {
        "configured": is_configured(),
        "base_url": c["base_url"],
        "model": c["model"],
        "has_key": bool(c["api_keys"]),
        "keys": key_states(),
        "max_tokens": c["max_tokens"],
        "json_mode": c["json_mode"],
        "hint": "" if is_configured() else (
            "请在下方「API 设置」里填 接口地址 / API Key / 模型"
            "（或编辑 web/.env 的 RETRO_LLM_*，改完立即生效，无需重启）。"
        ),
    }


def get_settings() -> dict:
    """设置面板的当前值（key 脱敏回传）。base_url 给编辑态（不带 /chat/completions 后缀）。"""
    _load_env_file()
    ov = _read_settings_file()
    c = config()
    return {
        "ok": True,
        "base_url": str(ov.get("base_url") or os.environ.get("RETRO_LLM_BASE_URL") or "").strip(),
        "model": c["model"],
        "max_tokens": c["max_tokens"],
        "temperature": c["temperature"],
        "timeout": int(c["timeout"]),
        "json_mode": c["json_mode"],
        "keys": key_states(),
        "active_index": _KEY_STATE["idx"],
    }


def save_settings(payload: dict) -> dict:
    """保存 UI 设置 → llm_settings.json（优先级高于 .env），并复位 key 轮换状态。

    api_keys 语义：数组元素含 `*` 视为「保留原 key」（按脱敏串匹配现有 key 池）；
    明文元素 = 新增/替换。全部过滤空值并去重。
    🔴 UI 传空列表 = 回落到 .env 的 key（想清空请直接编辑 .env）。
    """
    ov = _read_settings_file()
    cur_keys = key_pool()

    if "api_keys" in payload:
        new_keys: list[str] = []
        for item in payload.get("api_keys") or []:
            s = str(item).strip()
            if not s:
                continue
            if "*" in s:  # 脱敏串 → 映射回现有池里的真实 key
                for k in cur_keys:
                    if mask_key(k) == s and k not in new_keys:
                        new_keys.append(k)
                        break
            elif s not in new_keys:
                new_keys.append(s)
        ov["api_keys"] = new_keys

    if "base_url" in payload:
        ov["base_url"] = str(payload.get("base_url") or "").strip()
    if "model" in payload:
        ov["model"] = str(payload.get("model") or "").strip()
    for num in ("max_tokens", "timeout"):
        if payload.get(num) is not None:
            try:
                ov[num] = max(1, int(payload[num]))
            except (TypeError, ValueError):
                pass
    if payload.get("temperature") is not None:
        try:
            ov["temperature"] = min(2.0, max(0.0, float(payload["temperature"])))
        except (TypeError, ValueError):
            pass
    if payload.get("json_mode") is not None:
        ov["json_mode"] = bool(payload["json_mode"])

    _write_settings_file(ov)
    reset_key_state()  # key 池/配置变了，旧的失效标记全部作废
    return get_settings()


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


def _is_transient(err: BaseException) -> bool:
    try:
        from requests import exceptions as rex
    except Exception:  # noqa: BLE001  （requests 没装时退化为类名判断）
        return type(err).__name__ in _TRANSIENT_NAMES
    if isinstance(err, (rex.SSLError, rex.ConnectionError, rex.Timeout, rex.ChunkedEncodingError)):
        return True
    return type(err).__name__ in _TRANSIENT_NAMES


# 🔴 关掉代理**不能**传 `proxies={}`：requests 的 `merge_environment_settings` 会对空 dict
#    做 `setdefault`，把环境变量里的代理又填回来 ⇒ 等于没关。必须显式给 None 值占位。
_DIRECT: dict = {"http": None, "https": None}


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
        raise MultiKeyError("未配置任何 API Key（在「API 设置」里添加，或填 .env 的 RETRO_LLM_API_KEY）")
    notes: list[str] = []
    start = _KEY_STATE["idx"] % n
    resp = None
    for offset in range(n):
        idx = (start + offset) % n
        if not key_usable(idx):
            info = _KEY_STATE["bad"].get(idx) or {}
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
            mark_key_bad(idx, reason[:200], _COOLDOWN_429 if resp.status_code == 429 else 0.0)
            notes.append(f"#{idx + 1} {mask_key(keys[idx])} → {reason[:120]}")
            resp = None
            continue
        _KEY_STATE["idx"] = idx  # 记住可用的 key，下次请求从它开始
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
    if not cfg["model"] or not cfg["base_url"] or not cfg["api_keys"]:
        return {"ok": False, "verified": False, "error": public_status()["hint"]}
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
        return {"ok": False, "verified": False, "error": str(e), "model": cfg["model"]}
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            return {
                "ok": False,
                "verified": False,
                "error": network_error_text(e, tried, cfg["base_url"]),
                "model": cfg["model"],
                "proxy_mode": mode,
            }
        return {"ok": False, "verified": False, "error": f"{type(e).__name__}: {e}", "model": cfg["model"]}
    if resp.status_code >= 400:
        return {"ok": False, "verified": False, "error": _error_text(resp), "model": cfg["model"]}
    return {
        "ok": True,
        "verified": True,
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "key": mask_key(cfg["api_keys"][key_idx]),
        "proxy_mode": mode,
        "latency_hint": "ok",
    }


def list_models() -> dict:
    """列出该 key 能看到的模型，并逐个标注是否**真的能调**。

    🔴 只看 `/v1/models` 会被骗：它会列出「存在但不在当前套餐内」的模型
    （实测 `deepseek-v4.1-flash` 就出现在列表里，一调就 permission_denied）。
    """
    cfg = config()
    if not cfg["api_key"] or not cfg["base_url"]:
        return {"ok": False, "error": public_status()["hint"], "models": []}
    root = cfg["base_url"]
    for suffix in ("/chat/completions", "/completions"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    tried: list[str] = []
    try:
        resp = _request(
            "GET",
            root + "/models",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=30,
            tried=tried,
        )
        if resp.status_code >= 400:
            return {"ok": False, "error": _error_text(resp), "models": []}
        rows = (resp.json() or {}).get("data") or []
        return {"ok": True, "models": [str(m.get("id")) for m in rows if m.get("id")]}
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            return {"ok": False, "error": network_error_text(e, tried, root + "/models"), "models": []}
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
7. `mechanism` 必须给**逐步电子转移**，不能只写一个机理名（"SN2" 三个字不算）；
   且机理必须与你在 `order` / `conditions` 里给的投料顺序与条件**自洽**
   （例：主张 SN2 就不该同时写"形成碳正体"；主张酯化脱水就要有质子化羰基那一步）。
8. `workup` 要写成**能照着做**的逐步操作（淬灭 → 萃取 → 洗涤 → 干燥 → 浓缩 → 纯化），
   每步给出试剂、用量/次数与温度；并指出放大时最先出问题的环节（乳化、放热、相分离…）。

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
  "safety": ["安全与放大提示"],
  "confidence": 0.0
}
`confidence` 是 0~1 的数，表示你对这份分析整体的把握。"""

# 前端可勾选的「分析模块」→ JSON 字段。`name` 是前端传的 key，`desc` 用于生成精简版 system。
# 🔴 顺序即展示顺序，别乱动；`fields` 里是**该模块在 JSON 里对应的顶层字段**。
_MODULE_FIELDS = {
    "summary": {"fields": ["summary", "reaction_type"], "desc": "总述与反应类型"},
    "mechanism": {"fields": ["mechanism"], "desc": "逐步电子转移机理（type/steps/intermediates/rate_determining/selectivity/evidence）"},
    "order": {"fields": ["order"], "desc": "投料顺序（seq/mode/reagent/equiv/note）"},
    "conditions": {"fields": ["conditions"], "desc": "反应条件（溶剂/温度/时间/气氛/催化剂/后处理概览）"},
    "workup": {"fields": ["workup"], "desc": "后处理分析（淬灭/萃取分液/洗涤/干燥/浓缩/纯化/回收率/放大注意点）"},
    "outcome": {"fields": ["outcome"], "desc": "预期结果与收率（转化/收率/纯度/风险）"},
    "byproducts": {"fields": ["byproducts"], "desc": "可能的副产物（名称/来源机理/mz/控制方式）"},
    "lcms": {"fields": ["lcms"], "desc": "LCMS 解读（保留时间趋势/特征碎片/与副产物区分；m/z 数值已由 RDKit 算好，直接引用）"},
    "nmr": {"fields": ["nmr"], "desc": "NMR 谱图归属（1H/13C 位移与裂分/判据）"},
    "safety": {"fields": ["safety"], "desc": "安全与放大提示"},
}

# 「summary + reaction_type」是每个分析都要有的基础信息，前端永远勾上（不作为可取消项）。
# 其余是可选项。confidence 始终保留（帮助判断整体把握）。


def _build_system(modules: list[str] | None) -> str:
    """按勾选模块生成 system 提示：只要求输出勾选的字段。

    modules 为空 / None = 全量（走默认 _SYSTEM）。
    """
    if not modules:
        return _SYSTEM
    # 去重 + 过滤未知 key + 保持 _MODULE_FIELDS 的固定顺序
    wanted = [m for m in _MODULE_FIELDS if m in set(modules)]
    if not wanted:  # 全被过滤了，退化为全量
        return _SYSTEM
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
    # 关键：明确只输出这些字段，其余一概不输出，避免模型习惯性补全所有字段
    extra = (
        "本次只需分析用户勾选的这几个方面，输出一个 JSON 对象，**只包含以下字段**："
        f"{field_json}。不要输出其它任何字段；未勾选的方面（如未要求 NMR 就不要给 nmr 字段）。"
    )
    return _SYSTEM + "\n\n" + extra


def _fmt_mass_row(r: dict) -> str:
    if not r.get("ok"):
        return f"- {r.get('smiles')}（无法解析）"
    return (
        f"- {r['smiles']}　分子式 {r.get('formula')}　MW {r.get('mw')}　"
        f"精确质量 {r.get('exact_mass')}　[M+H]+ {r.get('mz_pos')}　[M-H]- {r.get('mz_neg')}"
    )


def build_messages(req: dict, masses: dict, json_footer: bool = True) -> list:
    """把一步反应上下文拼成对话 prompt。

    req: {product, precursors:[...], probability, model, context?, modules?}
    modules: 前端勾选的分析模块（['mechanism','order','lcms',...]），
             为空/缺省 = 全量分析。仅要求模型输出勾选字段。
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
    if json_footer:
        lines.append("请按系统提示里的 JSON 结构输出这一步的工艺分析（含 mechanism：逐步电子转移机理）。")

    return [
        {"role": "system", "content": _build_system(modules)},
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


def analyze(req: dict) -> dict:
    """非流式：一次性拿到结构化结果。"""
    cfg = config()
    if not is_configured():
        raise RuntimeError(public_status()["hint"] or "大模型未配置")

    masses = mass_info([req.get("product") or ""] + list(req.get("precursors") or []))
    messages = build_messages(req, masses)

    tried: list[str] = []
    try:
        resp, _key_idx = request_chat(
            cfg,
            json_body=_body(cfg, messages, stream=False),
            stream=False,
            timeout=cfg["timeout"],
            accept="application/json",
            tried=tried,
        )
    except MultiKeyError as e:
        raise RuntimeError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            raise RuntimeError(network_error_text(e, tried, cfg["base_url"])) from e
        raise
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
    def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    cfg = config()
    tried: list[str] = []
    try:
        if not is_configured():
            yield sse({"type": "error", "error": public_status()["hint"] or "大模型未配置"})
            return

        masses = mass_info([req.get("product") or ""] + list(req.get("precursors") or []))
        yield sse({"type": "meta", "model": cfg["model"], "computed": masses})

        messages = build_messages(req, masses)
        # 🔴 「接着分析」：前端把上次被截断时已收到的正文原文传进来，作为 assistant 前缀，
        #    让模型从上次断掉的地方接着写（而不是从头重来）。续写时不需要再思考，
        #    只补完剩余字段。
        continue_from = str(req.get("continue_from") or "").strip()
        if continue_from:
            messages = messages + [
                {
                    "role": "assistant",
                    "content": continue_from,
                    "prefix": True,  # 兼容支持 assistant prefix 的服务（如部分网关）
                },
                # 🔴 兜底：即便网关不识别 assistant prefix 续写，也靠这条指令让它「接着补完」
                #    上次没写完的 JSON，而不是另起炉灶重写一份。
                {
                    "role": "user",
                    "content": (
                        "你上一条回复（assistant 消息）是这个 JSON 的一部分，被截断了。"
                        "请**只**接着它继续输出剩余未完成的字段，把它补成一个完整的 JSON 对象；"
                        "不要重复已经写过的部分，不要输出任何解释或 markdown 围栏。"
                    ),
                },
            ]
        collected: list[str] = []
        think_buf: list[str] = []
        think_sent = 0
        finish_reason = ""

        try:
            resp, _key_idx = request_chat(
                cfg,
                json_body=_body(cfg, messages, stream=True),
                stream=True,
                timeout=cfg["timeout"],
                tried=tried,
            )
        except MultiKeyError as e:
            # 所有 key 都失效/限流：响亮报错（附每个 key 的原因），别让用户猜
            yield sse({"type": "error", "error": str(e)})
            return
        with resp:
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
        # 🔴 续写模式：把上次断点前的正文接回来，才能抽到完整 JSON。
        if continue_from:
            raw = continue_from + raw
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
                        f"（当前 {cfg.get('max_tokens')}）。也可以点「接着分析」从断点续写。"
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
        # 瞬时网络故障（TLS 被掐 / 代理端口失效 / 读半截）翻成人话，别甩一个
        # SSLEOFError 让用户猜 —— 实测这类错误重发就好，指引里直接给开关。
        if _is_transient(e):
            yield sse({"type": "error", "error": network_error_text(e, tried, cfg["base_url"])})
        else:
            yield sse({"type": "error", "error": f"{type(e).__name__}: {e}"})


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


def stream_followup(req: dict) -> Iterator[str]:
    """追问的流式回答（SSE）：**自由文本**，不做 JSON 抽取。

    事件序列与 stream_analyze 一致（复用前端同一套解析）：
        meta → {model}
        thinking → {text}        （推理模型专用）
        thinking_end → {chars}
        delta → {text}
        done → {text, model, reasoning, finish_reason}
        error → {error}

    🔴 与 stream_analyze 的唯一本质差别：`_body(..., json_mode=False)`。
       追问的答案是人读的段落，不是机器解析的 JSON。
    """
    def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    cfg = config()
    tried: list[str] = []
    try:
        if not is_configured():
            yield sse({"type": "error", "error": public_status()["hint"] or "大模型未配置"})
            return
        question = str(req.get("question") or "").strip()
        if not question:
            yield sse({"type": "error", "error": "追问内容为空"})
            return

        masses = mass_info([req.get("product") or ""] + list(req.get("precursors") or []))
        yield sse({"type": "meta", "model": cfg["model"]})

        messages = build_followup_messages(req, masses)
        collected: list[str] = []
        think_buf: list[str] = []
        think_sent = 0
        finish_reason = ""

        try:
            resp, _key_idx = request_chat(
                cfg,
                json_body=_body(cfg, messages, stream=True, json_mode=False),
                stream=True,
                timeout=cfg["timeout"],
                tried=tried,
            )
        except MultiKeyError as e:
            yield sse({"type": "error", "error": str(e)})
            return
        with resp:
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
                    if think_sent == 0 and think_buf:
                        think_sent = 1
                        yield sse({"type": "thinking_end", "chars": len("".join(think_buf))})
                    collected.append(text)
                    yield sse({"type": "delta", "text": text})

        answer = "".join(collected)
        # 思考吃光额度、正文一个字没有 ⇒ 响亮报错，别给用户一个空回答
        if finish_reason == "length" and not answer:
            yield sse(
                {
                    "type": "error",
                    "error": (
                        f"回答被截断（finish_reason=length，正文 0 字符）："
                        f"推理模型的思考 token 也计入输出额度，请调大输出上限"
                        f"（当前 {cfg.get('max_tokens')}）。"
                    ),
                }
            )
            return
        yield sse(
            {
                "type": "done",
                "text": answer,
                "model": cfg["model"],
                "reasoning": "".join(think_buf),
                "finish_reason": finish_reason,
            }
        )
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            yield sse({"type": "error", "error": network_error_text(e, tried, cfg["base_url"])})
        else:
            yield sse({"type": "error", "error": f"{type(e).__name__}: {e}"})


def sse_stream_with_cleanup(req: dict) -> Iterator[str]:
    """把生成器包一层，保证客户端断开时不留下半截连接（Werkzeug 会自行收尾）。"""
    gen: Iterator[str] = stream_analyze(req)
    for chunk in gen:
        yield chunk
