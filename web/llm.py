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


# 主机名里没有信息量的段（用来给新配置猜一个可读名字）
_HOST_NOISE = ("www", "api", "openai", "com", "cn", "net", "org", "io", "ai", "co")


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
        "max_tokens": _int_in(raw.get("max_tokens"), 32768, 16, 1_000_000),
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
    （新建端点时要先看看有哪些模型可选，不能只让用户盲填模型名）。
    草稿里的脱敏串（含 `*`）按**该配置自己**的 key 池映射回明文 —— 拿别的池匹配会串。
    """
    pid = str(profile.get("id") or "").strip()
    saved: dict = {}
    if pid:
        saved = next((p for p in current_profiles()["profiles"] if p["id"] == pid), None) or {}
    base_url = str(profile.get("base_url") or saved.get("base_url") or "").strip()
    raw_keys = profile.get("api_keys")
    if raw_keys is None:
        keys = list(saved.get("api_keys") or [])
    else:
        pool = saved.get("api_keys") or []
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
_COOLDOWN_429 = 60.0
_ROTATE_STATUS = (401, 403, 429)
_ROTATE_PAT = re.compile(
    r"token plan|quota|insufficient|balance|arrears|unauthorized|invalid.{0,12}key|"
    r"额度|欠费|余额不足|鉴权|无权限|无效的.{0,6}key",
    re.I,
)


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


def config() -> dict:
    """读**当前激活的端点配置**（改完下一次请求即生效）。

    优先级：profiles[active] → 老的顶层扁平字段 → .env / 环境变量。
    中间那一级只为兼容「升级前写下的 llm_settings.json」而留。
    """
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
    """
    if profile:
        base_url, keys = _profile_creds(profile)
        key = keys[0] if keys else ""
    else:
        cfg = config()
        base_url, key = cfg["base_url"], cfg["api_key"]
    if not key or not base_url:
        return {"ok": False, "error": public_status()["hint"], "models": []}
    root = base_url
    for suffix in ("/chat/completions", "/completions"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    tried: list[str] = []
    try:
        resp = _request(
            "GET",
            root + "/models",
            headers={"Authorization": f"Bearer {key}"},
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
    "conditions": {"fields": ["conditions"], "desc": "反应条件（溶剂/温度/时间/气氛/催化剂/后处理概览；已选定候选方案时沿用该方案）"},
    "workup": {"fields": ["workup"], "desc": "后处理分析（淬灭/萃取分液/洗涤/干燥/浓缩/纯化/回收率/放大注意点）"},
    "outcome": {"fields": ["outcome"], "desc": "预期结果与收率（转化/收率/纯度/风险）"},
    "byproducts": {"fields": ["byproducts"], "desc": "可能的副产物（名称/来源机理/mz/控制方式）"},
    "lcms": {"fields": ["lcms"], "desc": "LCMS 解读（保留时间趋势/特征碎片/与副产物区分；m/z 数值已由 RDKit 算好，直接引用）"},
    "nmr": {"fields": ["nmr"], "desc": "NMR 谱图归属（1H/13C 位移与裂分/判据）"},
    "safety": {"fields": ["safety"], "desc": "安全与放大提示"},
}

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
    # 关键：明确只输出这些字段，其余一概不输出，避免模型习惯性补全所有字段
    extra = (
        "本次只需分析用户勾选的这几个方面，输出一个 JSON 对象，**只包含以下字段**："
        f"{field_json}。不要输出其它任何字段；未勾选的方面（如未要求 NMR 就不要给 nmr 字段）。"
    )
    return _SYSTEM + "\n\n" + extra + tail


def _fmt_mass_row(r: dict) -> str:
    if not r.get("ok"):
        return f"- {r.get('smiles')}（无法解析）"
    return (
        f"- {r['smiles']}　分子式 {r.get('formula')}　MW {r.get('mw')}　"
        f"精确质量 {r.get('exact_mass')}　[M+H]+ {r.get('mz_pos')}　[M-H]- {r.get('mz_neg')}"
    )


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


def stream_conditions(req: dict) -> Iterator[str]:
    """候选反应条件枚举（SSE 流式：思考 / 正文增量照常转发，结束时下发抽好的 JSON）。

    事件序列与 stream_analyze **完全一致**（前端复用同一套解析）：
        meta → {model, computed}
        thinking → {text} / thinking_end → {chars}
        delta → {text}
        done → {data, computed, raw, model, reasoning, finish_reason}
        error → {error}
    """
    def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    cfg = config()
    tried: list[str] = []
    try:
        if not is_configured():
            yield sse({"type": "error", "error": public_status()["hint"] or "大模型未配置"})
            return
        count = req.get("count")
        try:
            count = max(2, min(6, int(count)))
        except (TypeError, ValueError):
            count = 4

        masses = mass_info([req.get("product") or ""] + list(req.get("precursors") or []))
        yield sse({"type": "meta", "model": cfg["model"], "computed": masses})

        messages = build_conditions_messages(req, masses, count)
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

        raw = "".join(collected)
        data = _norm_conditions(extract_json(raw), count)
        # 🔴 枚举失败必须响亮报错：前端要靠 options 渲染卡片，静默 empty 会显示成"没有可选条件"
        if finish_reason == "length" and not data:
            yield sse(
                {
                    "type": "error",
                    "error": (
                        f"枚举被截断（finish_reason=length，已收到 {len(raw)} 字符正文）："
                        f"多方案输出较长，请调大输出上限（当前 {cfg.get('max_tokens')}）"
                        f"或减少方案数。"
                    ),
                }
            )
            return
        if not data:
            yield sse({"type": "error", "error": "未能解析出候选条件方案，请重试（或减少方案数）"})
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
