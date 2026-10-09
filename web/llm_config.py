# -*- coding: utf-8 -*-
"""大模型「工艺分析」——端点配置（多套 profiles）+ API Key 池轮换 —— web/llm.py 的配置半边。

从 `llm.py` 拆出来的（机械搬移，逻辑未改）：原文件 2343 行一整块，
配置 / 提示词 / 传输 / 流式 四件事混在一起，改哪一样都要在两千行里翻。
对外名字空间不变 —— `llm.py` 负责再导出，app.py 不需要改。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional


_HERE = os.path.dirname(os.path.abspath(__file__))
# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_ENV_LOADED = False
# ---------------------------------------------------------------------------
# UI 设置层（llm_settings.json）：优先级高于 .env
# ---------------------------------------------------------------------------
# 「AI 工艺分析」面板里可直接改 接口地址 / 模型 / token 上限 / API Key 列表，
# 保存到 web/llm_settings.json（带 mtime 缓存，改完立即生效，无需重启 Flask）。
# 优先级：llm_settings.json > .env / 环境变量；.env 仍作为初始默认。
# 🔴 该文件含明文 API Key，必须进 .gitignore（与 web/.env 同待遇）。
_SETTINGS_FILE = os.path.join(_HERE, "llm_settings.json")
_SETTINGS_STAMP: dict = {"mtime": -1.0, "data": {}}
# 主机名里没有信息量的段（用来给新配置猜一个可读名字）
_HOST_NOISE = ("www", "api", "openai", "com", "cn", "net", "org", "io", "ai", "co")
# 🔴 输出额度的**可用下限**（token）。低于它的值不是「省 token」，是「必然一个字都出不来」：
#    推理模型的思考 token 也计入这个额度 —— 实测一次候选条件枚举，光思考就吐了 9288 字符，
#    16 个 token 连思考都开不了头。`_max_tokens_in` 的**格式下限 16 挡不住这种情况**
#    （16 本身就在坏区间里），所以另设一道「可用性」门槛，在**发请求之前**就拦掉，
#    别让用户白等一整轮（那次要等思考跑完才报出「截断」）。
MT_MIN_USABLE = 1024
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
# 🔴 轮换状态必须**按配置分桶**：bad 是按 key 的**下标**记的。共用一份的话，
#    「MiMo 配置里第 2 个 key 失效」会把「DeepSeek 配置里第 2 个 key」一起标成失效
#    —— 两个池子毫无关系，白丢一个可用 key。
_KEY_STATE: dict = {"pool": {}}  # pool: profile_id -> {"idx": int, "bad": {idx: {...}}}

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
# 「端点配置」（profile）——接口地址 + 模型 + 一组 Key 绑成一套，可存多套手动切换
# ---------------------------------------------------------------------------
# 🔴 为什么必须绑成一套：base_url / model / key 三者是**互相依赖**的 —— MiMo 的 key
#    配上 DeepSeek 的地址、或 DeepSeek 的地址配 MiMo 的模型名，都是无效组合。
#    之前它们各自是顶层散字段，换一家供应商要挨个改三处，改漏一处就得到一个
#    「看着配好了、一调就 401/404」的状态。现在每次切换只动一个 id。
#
# 存储形态（llm_settings.json）：
#     {"active_profile": "<id>", "profiles": [ {id, name, base_url, model, api_keys, ...} ]}
# 老版本的扁平结构（base_url / model / api_keys 直接放顶层）在**读的时候**懒迁移成
# 一套名为「默认配置」的 profile，用户升级后配置照常可用，不必手工改文件。
def _chat_url(base: str) -> str:
    """把接口地址补成完整的 chat/completions（允许只填到 /v1，也允许填全）。"""
    base = str(base or "").strip().rstrip("/")
    if base and not base.endswith("/chat/completions"):
        base = base + "/chat/completions"
    return base

def _slug(raw: str, fallback: str = "profile") -> str:
    """id 只留小写字母数字与 -_（它会进 url / 落盘，别让奇怪字符进来）。"""
    s = re.sub(r"[^a-z0-9_-]+", "-", str(raw or "").strip().lower()).strip("-")
    return s[:40].strip("-") or fallback

def _host_of(url: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", str(url or "").strip(), re.I)
    return m.group(1).split("@")[-1].split(":")[0].lower() if m else ""

def _guess_name(url: str) -> str:
    """从接口地址猜一个可读名字：`https://api.deepseek.com/v1` → `Deepseek`。"""
    host = _host_of(url)
    if not host:
        return ""
    parts = [p for p in host.split(".") if p and p not in _HOST_NOISE]
    seg = parts[0] if parts else host.split(".")[0]
    return seg[:1].upper() + seg[1:]

def _int_in(raw, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(raw)))
    except (TypeError, ValueError):
        return default

def _max_tokens_in(raw) -> int:
    """max_tokens 专用：0（或空）表示「无限制」——不设上限，交给模型/网关默认值。

    非 0 时才 clamp 到 [16, 1_000_000]（下限 16 是为了防「额度小到思考 token 吃光、
    正文 0 字符」；上限 1_000_000 防离谱值）。0 本身是合法语义，不能像 `_int_in`
    那样被 clamp 到 lo。
    """
    if raw is None or raw == "":
        return 0
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return min(1_000_000, max(16, v))

class LlmConfigError(RuntimeError):
    """配置本身就不可能跑通。

    与「网络故障 / 额度用尽 / 模型不可用」区分开：这一类改配置即可，重试多少次都一样。
    """

def _tokens_hint(cfg: dict) -> str:
    """截断报错的「怎么调」完整从句。

    🔴 必须区分 max_tokens 的**生效来源**：它可能来自「端点配置」(llm_settings.json)、
       老式顶层扁平字段、或 web/.env。优先级是 profile > 顶层 > .env —— 所以当生效值
       来自 profile 时，让用户去改 .env 是**永远无效的指引**（实测：.env 里写着 32768、
       界面上这套是 16，用户照文案反复改 .env 只是白忙一场）。
    """
    mt = cfg.get("max_tokens")
    if not mt:
        return (
            "当前输出上限为「无限制」（不传 max_tokens），仍被截断说明撞到了模型/网关"
            "自身的上下文上限 —— 请换一个上下文更长的模型，或联系网关方放宽上限"
        )
    if cfg.get("max_tokens_source") in ("profile", "settings"):
        return (
            f"请在「运行参数 → 大模型设置 → 端点配置」里，把当前这套配置的「输出上限」"
            f"改大（当前 {mt}），或勾上「无限制」"
        )
    return f"请调大 web/.env 里的 RETRO_LLM_MAX_TOKENS（当前 {mt}）"

def _tiny_tokens_text(cfg: dict) -> str:
    """额度小到不可能出正文时的报错文案；返回 "" = 没问题。

    🔴 为什么要**前置**拦：额度不够的表现是「静默截断」—— 用户在流式面板上只看到思考
       在滚、正文一直不出来，等到最后才收到一条「被截断」。等它等到那一刻，一整轮已经过去了。
       这里在发请求前就直说，并一并给出「去哪改」。
    """
    mt = int(cfg.get("max_tokens") or 0)
    if not mt or mt >= MT_MIN_USABLE:
        return ""
    model = cfg.get("model") or "当前模型"
    return (
        f"输出上限只有 {mt} 个 token —— 推理模型（{model}）的思考 token 也记在这个额度里，"
        f"思考还没结束额度就用光了，正文一个字都出不来。{_tokens_hint(cfg)}"
    )

def _float_in(raw, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(raw)))
    except (TypeError, ValueError):
        return default

def _bool_of(raw, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off")

def _norm_profile(raw: object, used: set) -> Optional[dict]:
    """把一条配置归一化（字段类型兜底 + id 去重 + 名称兜底）。

    返回 None = 空壳（三要素都没填），不进列表 —— 否则 UI 上会攒下一排没用的空项。
    """
    if not isinstance(raw, dict):
        return None
    base_url = str(raw.get("base_url") or "").strip()
    model = str(raw.get("model") or "").strip()
    # 🔴 `api_keys` 可能是**字符串**（手写的 llm_settings.json、或从环境变量直接读来的）——
    #    直接 `for k in raw_keys` 会把 "sk-a,sk-b" 拆成一个个**字符**，变成一堆单字符假 key。
    raw_keys = raw.get("api_keys")
    keys = (
        _split_keys(raw_keys)
        if isinstance(raw_keys, str)
        else [str(k).strip() for k in (raw_keys or []) if str(k).strip()]
    )
    name = str(raw.get("name") or "").strip()
    if not (base_url or model or keys or name):
        return None
    base = _slug(raw.get("id") or name or _host_of(base_url), "profile")
    pid = base
    i = 2
    while pid in used:  # 重名不冲突，但 id 必须唯一（active_profile 靠它指认）
        pid = f"{base}-{i}"
        i += 1
    used.add(pid)
    return {
        "id": pid,
        "name": name or _guess_name(base_url) or pid,
        "base_url": base_url,
        "model": model,
        "api_keys": keys,
        "max_tokens": _max_tokens_in(raw.get("max_tokens")),
        "temperature": _float_in(raw.get("temperature"), 0.3, 0.0, 2.0),
        "timeout": _int_in(raw.get("timeout"), 300, 5, 3600),
        "json_mode": _bool_of(raw.get("json_mode"), True),
        "note": str(raw.get("note") or "").strip()[:200],
    }

def _env_or(ov: dict, name: str):
    """取配置项：老的顶层扁平字段 → 环境变量（.env）。"""
    v = ov.get(name)
    if v is None or v == "":
        v = os.environ.get(f"RETRO_LLM_{name.upper()}")
    return v

def _norm_profiles(ov: dict) -> dict:
    """→ {"active": <id>, "profiles": [ ... ]}（含老扁平结构的懒迁移）。

    🔴 迁移时缺的字段用当期 .env / 环境变量补上：这一步的语义是「把当时的有效配置
       固化下来」。不补的话，用户原本靠 .env 配好的 base_url / model / max_tokens
       会正好在迁进 profile 结构时丢掉（迁出方有值、迁入方拿默认值）。
    """
    _load_env_file()  # 幂等；迁移分支要读环境变量，不能假设调用方已经加载过
    declared = "profiles" in ov  # 显式写过 profiles（哪怕是空数组）⇒ 不再回落扁平字段
    used: set = set()
    profiles: list[dict] = []
    if isinstance(ov.get("profiles"), list):
        for item in ov["profiles"]:
            p = _norm_profile(item, used)
            if p:
                profiles.append(p)
    if not profiles and not declared:
        # 老结构（base_url / model / api_keys 在顶层）→ 折成一套，别让用户的配置凭空消失
        legacy = _norm_profile(
            {
                "id": "default",
                "name": _guess_name(str(_env_or(ov, "base_url") or "")) or "默认配置",
                "base_url": _env_or(ov, "base_url"),
                "model": _env_or(ov, "model"),
                # 🔴 顶层扁平字段（升级前写的 llm_settings.json）优先；都没有才从
                #    `.env` 的 key 池取 —— 名字必须走 `_env_keys()` 的双名回落，
                #    直接 `_env_or(ov, "api_keys")` 只会找 `RETRO_LLM_API_KEYS` 而漏掉单数。
                "api_keys": ov.get("api_keys") or _env_keys(),
                "max_tokens": _env_or(ov, "max_tokens"),
                "temperature": _env_or(ov, "temperature"),
                "timeout": _env_or(ov, "timeout"),
                "json_mode": _env_or(ov, "json_mode"),
            },
            used,
        )
        if legacy:
            profiles.append(legacy)
    active = str(ov.get("active_profile") or "").strip()
    if not any(p["id"] == active for p in profiles):
        active = profiles[0]["id"] if profiles else ""
    return {"active": active, "profiles": profiles}

def current_profiles() -> dict:
    """（内部用）当前设置的归一化视图。"""
    return _norm_profiles(_read_settings_file())

def _active_profile(norm: Optional[dict] = None) -> Optional[dict]:
    n = norm if norm is not None else current_profiles()
    for p in n["profiles"]:
        if p["id"] == n["active"]:
            return p
    return None

def _active_id() -> str:
    return current_profiles()["active"]

def _profile_creds(profile: dict) -> tuple:
    """从「配置引用」解析出 (base_url, keys)。

    profile 可以是：已保存配置的 `{id}`、还没保存的草稿 `{id?, base_url, api_keys}`
    （新建端点时要能先看看有哪些模型可选，不能只让用户盲填模型名）。
    草稿里的脱敏串（含 `*`）按**该配置自己**的 key 池映射回明文 —— 拿别的池匹配会串。

    🔴 池为空时**只给「激活那套」回落 `.env`**（`key_pool()` 也是只对激活那套回落）。
       不补这一级 ⇒ 「`profiles[].api_keys` 是空的、key 只在 `.env`」的配置：
       面板显示（走 `_pool_keys`，它补了 .env）是对的，拉模型（走这里）却拿不到 key。
       同一份真值两处口径不一致，症状 = 「拉取返回空列表，错误信息还是空串」。
       非激活那套**不能**回落：那等于把激活那套的 key 当成它的，与保存时的
       `allow_env` 口径相反（会出现「拉得到模型、保存却说没 key」）。
    """
    norm = current_profiles()  # 一次读盘，active 与 profiles 取同一份快照
    pid = str(profile.get("id") or "").strip()
    saved: dict = {}
    if pid:
        saved = next((p for p in norm["profiles"] if p["id"] == pid), None) or {}
    base_url = str(profile.get("base_url") or saved.get("base_url") or "").strip()
    # 池 = 这套自己存的 key（空则按上面说明回落 .env）
    pool = [str(k).strip() for k in (saved.get("api_keys") or []) if str(k).strip()]
    if not pool and pid and pid == norm["active"]:
        pool = _env_keys()
    raw_keys = profile.get("api_keys")
    if raw_keys is None:
        keys = list(pool)
    else:
        keys = []
        for item in raw_keys:
            s = str(item).strip()
            if not s:
                continue
            if "*" in s:  # 脱敏串 → 映射回该配置池里的真实 key
                for k in pool:
                    if mask_key(k) == s and k not in keys:
                        keys.append(k)
                        break
            elif s not in keys:
                keys.append(s)
        if not keys:
            keys = list(pool)
    return base_url, keys

class MultiKeyError(RuntimeError):
    """全部 API Key 都不可用（附带每个 key 的失败原因，给前端直接展示）。"""

def _pool_of(profile_id: str) -> dict:
    """取某套配置的轮换状态桶（懒建）。空 id（还没配任何端点）落进一个公共桶。"""
    return _KEY_STATE["pool"].setdefault(str(profile_id or "_default"), {"idx": 0, "bad": {}})

def _pool() -> dict:
    """当前激活配置的轮换状态桶。"""
    return _pool_of(_active_id())

def _split_keys(raw: str) -> list[str]:
    """把逗号/空白分隔的 key 串拆成去重列表（兼容 .env 里一格多 key 的填法）。"""
    out: list[str] = []
    for part in re.split(r"[\s,;，；]+", raw or ""):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out

def _env_keys() -> list[str]:
    """环境变量里的 key 池（回落链的最后一级）。

    🔴 名字有**两个**，必须都认：
      · `RETRO_LLM_API_KEYS`（复数）—— 与前缀约定 `RETRO_LLM_<字段名>` 一致（字段叫 api_keys）；
      · `RETRO_LLM_API_KEY`（单数）—— README / `.env.example` 里写的、用户手上 `.env` 里
        一直用的就是这个名字。
    只认复数会踩一个很迷惑的坑：配置文件从 `.env` **懒迁移**成 profile 时 key 静默丢失，
    面板显示「API Keys（0）」而实际请求照常跑（`key_pool()` 有单数回落）——
    于是用户看到「没 key 却能分析」、「保存被拦（至少一个 Key 必填）」。
    """
    return _split_keys(os.environ.get("RETRO_LLM_API_KEYS") or "") or _split_keys(
        os.environ.get("RETRO_LLM_API_KEY") or ""
    )

def key_pool() -> list[str]:
    """当前激活配置的 key 池：UI 设置优先，回落 .env（支持一格多 key）。

    🔴 内置 key 为空时**回落 .env** 是刻意的：老用户的 key 只存在于 `.env`，
        迁进 profile 结构后这一级不能断，否则升级即「未配置任何 API Key」。
    """
    _load_env_file()  # 幂等（mtime 缓存）：key_pool 可能先于 config() 被调
    prof = _active_profile()
    keys = [str(k).strip() for k in ((prof or {}).get("api_keys") or []) if str(k).strip()]
    if not keys:
        keys = _env_keys()
    return keys

def mask_key(k: str) -> str:
    """key 脱敏：sk-L7w15y****ab3f（前端列表展示 / 回传映射都靠这个串）。

    🔴 短 key 也必须**保留尾部**：只留前 3 位时 `sk-aaa1` 与 `sk-bbb2` 会脱敏成同一个串，
       而回传保存是按这个串**反查真实 key** 的 —— 撞上就等于把它们认成同一把（静默丢 key）。
       真实 key 都很长，但开发用的占位 key / 短串一样会走到这条路径。
    """
    if len(k) <= 8:
        return k[:2] + "****" + k[-2:]
    if len(k) <= 16:
        return k[:4] + "****" + k[-3:]
    return f"{k[:8]}****{k[-4:]}"

def key_usable(idx: int, profile_id: str = "") -> bool:
    """cooldown 到期 = 恢复可用；until=0 = 永久失效（直到「重试失效 Key」或改设置复位）。"""
    info = _pool_of(profile_id or _active_id())["bad"].get(idx)
    if not info:
        return True
    until = info.get("until") or 0
    if until <= 0:
        return False
    return time.time() >= until

def mark_key_bad(idx: int, reason: str, cooldown: float = 0.0, profile_id: str = "") -> None:
    _pool_of(profile_id or _active_id())["bad"][idx] = {
        "reason": reason,
        "until": (time.time() + cooldown) if cooldown else 0.0,
    }

def reset_key_state(profile_id: str = "") -> None:
    """清空失效/冷却标记（保存设置 / 手动「重试失效 Key」时调用）。

    给了 profile_id 就只清那一套（面板上的按钮作用于当前编辑的配置）；
    不给 = 全部清（保存设置后调用：配置变了，旧的失效标记一律作废）。
    """
    if profile_id:
        _KEY_STATE["pool"].pop(str(profile_id), None)
        return
    _KEY_STATE["pool"].clear()

def _pool_keys(p: Optional[dict], active: bool) -> list[tuple[str, str]]:
    """某套配置的 key 池 + **每把的来源**：`[("sk-…", "profile"), ("sk-…", "env")]`。

    🔴 激活那套要带上 `.env` 回落 —— 那正是 `key_pool()` 拿去发请求的那把。
       只回显 `profiles[].api_keys` 里的数组，就会出现
       「面板写 API Keys（0）· 保存被『至少一个 Key 必填』拦住 · 模型却跑得动」三头矛盾
       （一条链上的两处名字/口径不一致，实测踩到）。
    🔴 来源必须标出来：`.env` 那把**删不掉**（它是回落默认值，要改 .env 才生效），
       UI 因此不给删除按钮 —— 否则又是一个「看着能删、其实删不掉」。
    """
    own = [str(k).strip() for k in ((p or {}).get("api_keys") or []) if str(k).strip()]
    if own or not active:
        return [(k, "profile") for k in own]
    return [(k, "env") for k in _env_keys()]

def key_states(profile_id: str = "", keys: Optional[list] = None) -> list[dict]:
    """给 UI 的 key 池状态（脱敏）：active=使用中 / cooldown=冷却 / bad=失效 / idle=备用。

    不传 profile_id = 当前激活那套的真实状态；传了 = 那套自己的状态
    （每套各一张表，切换配置时看到的状态不会串）。
    `keys` 可显式传（元素是 `(key, 来源)`），不传就按该套的池算 —— 见 `_pool_keys`。
    """
    if profile_id:
        uid = str(profile_id)
        if keys is None:
            prof = next((p for p in current_profiles()["profiles"] if p["id"] == uid), None)
            keys = _pool_keys(prof, uid == _active_id())
    else:
        uid = _active_id()
        if keys is None:
            keys = _pool_keys(_active_profile(), True)
    pool = _pool_of(uid)
    n = len(keys)
    now = time.time()
    # 「使用中」= 下一次请求**真正会用到**的那把：从当前下标起第一个可用的 key。
    # 🔴 不能只标 `pool["idx"]` 指向的那把 —— 它失效时面板会显示成「没有任何 key 在使用中」，
    #    而实际请求早就顺延到后面的备用 key 了（多 key 轮换最该看清的恰恰是这个）。
    live = -1
    for offset in range(n):
        cand = (pool["idx"] + offset) % n
        if key_usable(cand, uid):
            live = cand
            break
    out: list[dict] = []
    for i, item in enumerate(keys):
        k, src = item if isinstance(item, (tuple, list)) else (item, "profile")
        info = pool["bad"].get(i) or {}
        until = info.get("until") or 0
        if i == live:
            state = "active"
        elif until and until > now:
            state = "cooldown"
        elif info:
            state = "bad"
        else:
            state = "idle"
        out.append(
            {
                "masked": mask_key(str(k)),
                "state": state,
                "reason": str(info.get("reason") or ""),
                "source": str(src),
            }
        )
    return out

def config(override: Optional[dict] = None) -> dict:
    """读**当前激活的端点配置**（改完下一次请求即生效）。

    优先级：profiles[active] → 老的顶层扁平字段 → .env / 环境变量。
    中间那一级只为兼容「升级前写下的 llm_settings.json」而留。

    🔴 `override` 是「请求级凭据覆盖」：Node 中转按用户身份解析出该用户该用哪套 LLM
       （用户自建 → 官方 → 都不命中则不传），把 `{base_url, api_keys, model}` 塞进
       请求体的 `profile` 字段。这里命中 override 时**不再读盘**，凭据完全由请求方注入
       —— 官方 key 只在内网 Node→Python 出现，永不出前端、永不落盘。
       字段白名单：base_url / api_keys / model；其余（timeout/max_tokens/temperature 等）
       仍走默认，避免请求方把超时/温度也当成注入口。
    """
    if override:
        base_url = str(override.get("base_url") or "").strip()
        api_keys = [str(k).strip() for k in (override.get("api_keys") or []) if str(k).strip()]
        model = str(override.get("model") or "").strip()
        if base_url and api_keys and model:
            keys = api_keys
            idx = 0
            return {
                "profile_id": "",
                "profile_name": "(请求注入)",
                "base_url": _chat_url(base_url),
                "api_keys": keys,
                "api_key": keys[idx],
                "model": model,
                "timeout": 180.0,
                "max_tokens": 0,
                "max_tokens_source": "override",
                "json_mode": True,
                "temperature": 0.3,
            }
        # override 字段不全（base_url/api_keys/model 缺一）⇒ 忽略，回落读盘，别半套凭据去调模型

    _load_env_file()
    ov = _read_settings_file()
    prof = _active_profile(_norm_profiles(ov)) or {}

    def _raw(name: str):
        v = prof.get(name)
        if v is None or v == "":
            v = ov.get(name)
        if v is None or v == "":
            v = os.environ.get(f"RETRO_LLM_{name.upper()}")
        return v

    def _raw_src(name: str) -> str:
        """同 `_raw`，但回的是**生效来源**：`profile` / `settings` / `env`。

        🔴 报错文案要靠它决定「让用户去哪改」—— 指错地方比不指还糟：用户会照着反复改
           一个**被上层盖掉**的地方，看什么都没变。
        """
        if prof.get(name) not in (None, ""):
            return "profile"
        if ov.get(name) not in (None, ""):
            return "settings"
        return "env"

    def _num(name: str, env_default: str, cast, lo: float, hi: float):
        raw = _raw(name)
        if raw is None or raw == "":
            raw = env_default
        try:
            v = cast(raw)
        except (TypeError, ValueError):
            v = cast(env_default)
        return min(hi, max(lo, v))

    def _flag(name: str, env_default: str) -> bool:
        return _bool_of(_raw(name), _bool_of(env_default, True))

    keys = key_pool()
    idx = _pool()["idx"] % len(keys) if keys else 0
    return {
        "profile_id": str(prof.get("id") or ""),
        "profile_name": str(prof.get("name") or ""),
        "base_url": _chat_url(str(_raw("base_url") or "")),
        "api_keys": keys,
        "api_key": keys[idx] if keys else "",  # 当前活跃 key（向后兼容旧调用方）
        "model": str(_raw("model") or "").strip(),
        "timeout": _num("timeout", "180", float, 5.0, 3600.0),
        # 0 = 无限制（不设上限，交给模型默认）；非 0 走 [16, 1_000_000] clamp
        "max_tokens": _max_tokens_in(_raw("max_tokens")),
        # 🔴 生效来源：截断报错要靠它说清「去哪改」（见 _tokens_hint）。profile 会盖掉 .env，
        #    所以「来自 profile」时提示 .env 是无效指引。
        "max_tokens_source": _raw_src("max_tokens"),
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
        "profile_id": c["profile_id"],
        "profile_name": c["profile_name"],
        "base_url": c["base_url"],
        "model": c["model"],
        "has_key": bool(c["api_keys"]),
        "keys": key_states(),
        "max_tokens": c["max_tokens"],
        "json_mode": c["json_mode"],
        "hint": "" if is_configured() else (
            "请在「运行参数 → 大模型设置 → 端点配置」里新建一套：接口地址 / 模型 / API Key 会绑在一起"
            "（也可以编辑 web/.env 的 RETRO_LLM_*，改完立即生效，无需重启）。"
        ),
    }

def get_settings() -> dict:
    """设置面板的当前值：全部端点配置（key 脱敏回传）+ 当前激活那套的展开字段。

    `base_url / model / ... / keys` 这几个扁平字段与 `profiles[active]` 等价，
    是给「只关心当前配置」的老界面留的兼容形态。
    """
    _load_env_file()
    ov = _read_settings_file()
    norm = _norm_profiles(ov)
    prof = _active_profile(norm) or {}
    c = config()
    profiles: list[dict] = []
    for p in norm["profiles"]:
        profiles.append(
            {
                "id": p["id"],
                "name": p["name"],
                "base_url": p["base_url"],
                "model": p["model"],
                "max_tokens": p["max_tokens"],
                "temperature": p["temperature"],
                "timeout": p["timeout"],
                "json_mode": p["json_mode"],
                "note": p["note"],
                "keys": key_states(p["id"]),
                "active": p["id"] == norm["active"],
            }
        )
    return {
        "ok": True,
        "active_profile": norm["active"],
        "profiles": profiles,
        # —— 兼容形态：当前激活配置的展开 ——
        "base_url": str(prof.get("base_url") or os.environ.get("RETRO_LLM_BASE_URL") or "").strip(),
        "model": c["model"],
        "max_tokens": c["max_tokens"],
        "temperature": c["temperature"],
        "timeout": int(c["timeout"]),
        "json_mode": c["json_mode"],
        "keys": key_states(),
        "active_index": _pool()["idx"],
    }

def save_settings(payload: dict) -> dict:
    """保存设置面板 → web/llm_settings.json（优先级高于 .env），并复位 key 轮换。

    两种 payload 形态：
      · **整包**（新）：`{profiles: [...], active_profile: "<id>"}` —— 增 / 改 / 删都走它，
        提交什么就是什么（删掉一套 = 数组里没有它）。
      · **扁平**（老界面兼容）：`{base_url, model, api_keys, ...}` —— 合并进当前激活那套。

    每套里的 `api_keys`：元素含 `*` = 按脱敏串保留该套已有的 key；明文 = 新增 / 替换。
    🔴 脱敏串优先只在**它自己那套**的 key 池里匹配 —— 跨配置乱匹配会取到别家的 key。
    🔴 再兜一级：**激活那套**允许从 `.env` 的 key 池里认领。面板把 .env 那把（回落默认值）
       列在「API Keys」里，用户点「保存设置」就是想把它固定下来；不认领就等于
       「面板上有、一保存就没了」，而且文件里从此留下一份空池（实测：本机就这么被写坏的）。
    """
    ov = _read_settings_file()
    saved = {p["id"]: p for p in _norm_profiles(ov)["profiles"]}

    def _resolve(items, pid: str, allow_env: bool = False) -> list[str]:
        own = (saved.get(pid) or {}).get("api_keys") or []
        others: list[str] = []
        for other_id, p in saved.items():
            if other_id != pid:
                others.extend(p.get("api_keys") or [])
        out: list[str] = []
        for item in items or []:
            s = str(item).strip()
            if not s:
                continue
            if "*" not in s:  # 明文 = 直接采用
                if s not in out:
                    out.append(s)
                continue
            hit = next((k for k in own if mask_key(k) == s), "")
            if not hit:
                # 「复制配置」场景：新 id 的池是空的，脱敏串得回溯到别处 —— 只认**唯一**匹配
                cand = {k for k in others if mask_key(k) == s}
                if len(cand) == 1:
                    hit = cand.pop()
            if not hit and allow_env:
                # 激活那套的最后一级回落：面板上那把本来就是从 .env 来的（见 _pool_keys）
                hit = next((k for k in _env_keys() if mask_key(k) == s), "")
            if hit and hit not in out:
                out.append(hit)
        return out

    rows: list = []
    active_in = ""
    if isinstance(payload.get("profiles"), list):
        rows = payload["profiles"]
        active_in = str(payload.get("active_profile") or "").strip()
    else:
        # 老界面：扁平字段只改**当前激活**那套。🔴 其它配置必须原样保留 ——
        # 否则一次老的保存动作会把用户新建的其它端点整片删掉（数组被覆盖成单项）。
        aid = current_profiles()["active"]
        merged = dict(saved.get(aid) or {"id": aid or "default", "name": "默认配置"})
        for key in ("base_url", "model", "max_tokens", "temperature", "timeout", "json_mode", "note"):
            if payload.get(key) is not None:
                merged[key] = payload[key]
        if "api_keys" in payload:
            merged["api_keys"] = _resolve(payload["api_keys"], aid, allow_env=True)
        rows = [dict(saved[k]) for k in saved]
        hit = next((i for i, r in enumerate(rows) if str(r.get("id") or "").strip() == aid), -1)
        if hit >= 0:
            rows[hit] = merged  # 顺序不变：面板里的排列不会因为一次保存而跳动
        else:
            rows.append(merged)
        active_in = aid

    used: set = set()
    id_map: dict = {}  # 前端给的 id → 归一化后的 id（slug 化可能改写法）
    profiles: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        pid_raw = str(item.get("id") or "").strip()
        if isinstance(item.get("api_keys"), list):
            item["api_keys"] = _resolve(item["api_keys"], pid_raw, allow_env=pid_raw == active_in)
        p = _norm_profile(item, used)
        if p:
            if pid_raw:
                id_map[pid_raw] = p["id"]
            profiles.append(p)

    active = id_map.get(active_in, active_in)
    if not any(p["id"] == active for p in profiles):
        active = profiles[0]["id"] if profiles else ""
    # 🔴 落盘只写新形态：顶层扁平字段一旦留着就等于多出一套「隐藏配置」，
    #    在用户删光全部配置后会被迁移分支复活。
    _write_settings_file({"active_profile": active, "profiles": profiles})
    reset_key_state()  # 配置变了，旧的失效标记全部作废
    return get_settings()

def activate_profile(profile_id: str) -> dict:
    """切换当前使用的端点配置（只改 active_profile）→ 下一次请求立即生效，无需保存。"""
    pid = str(profile_id or "").strip()
    ov = _read_settings_file()
    if not any(p["id"] == pid for p in _norm_profiles(ov)["profiles"]):
        out = get_settings()
        out["ok"] = False
        out["error"] = f"没有这个端点配置：{profile_id or '(空)'}"
        return out
    ov = dict(ov)
    ov["active_profile"] = pid
    _write_settings_file(ov)
    return get_settings()
