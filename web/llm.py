# -*- coding: utf-8 -*-
"""大模型「工艺分析」——对外入口（原 2343 行已拆成四个模块，见下方）。

分拆后的四个模块（依赖单向，无环）：
    llm_config.py     端点配置（profiles）+ API Key 池轮换
    llm_prompts.py    提示词构建与常量
    llm_transport.py  HTTP 传输与响应解析
    llm_stream.py     分析 / 条件枚举 / 追问的流式编排

🔴 本文件**只做再导出**：app.py 用的是 `import llm; llm.xxx()`，
   所以这里必须把对外名字一字不差地补齐 —— 加新函数时别忘了加进来。

🔴 两个刻意的设计（原文件的设计说明，保留在此）：
  1) **LCMS 的数值不交给大模型算**。精确质量是确定性计算，交给 RDKit（mass_info），
     大模型只负责解释与补充。让模型去算 m/z 是幻觉的高发区。
  2) **流式与结构化并存**。逐 token 原样转发（前端能实时看到输出在长），
     流结束时抽出 JSON 一并下发。
"""
from __future__ import annotations

# ---- llm_config ----
from llm_config import (  # noqa: F401
    LlmConfigError,
    MultiKeyError,
    _active_id,
    _active_profile,
    _bool_of,
    _chat_url,
    _env_keys,
    _env_or,
    _float_in,
    _guess_name,
    _host_of,
    _int_in,
    _load_env_file,
    _max_tokens_in,
    _norm_profile,
    _norm_profiles,
    _pool,
    _pool_keys,
    _pool_of,
    _profile_creds,
    _read_settings_file,
    _slug,
    _split_keys,
    _tiny_tokens_text,
    _tokens_hint,
    _write_settings_file,
    activate_profile,
    config,
    current_profiles,
    get_settings,
    is_configured,
    key_pool,
    key_states,
    key_usable,
    mark_key_bad,
    mask_key,
    public_status,
    reset_key_state,
    save_settings,
)

# ---- llm_prompts ----
from llm_prompts import (  # noqa: F401
    _build_system,
    _fmt_chosen_condition,
    _fmt_mass_row,
    _norm_conditions,
    _one_mass,
    build_conditions_messages,
    build_followup_messages,
    build_messages,
    mass_info,
)

# ---- llm_transport ----
from llm_transport import (  # noqa: F401
    _body,
    _delta_of,
    _error_text,
    _headers,
    _is_transient,
    _request,
    _think_of,
    extract_json,
    list_models,
    network_error_text,
    probe,
    proxies_setting,
    request_chat,
)

# ---- llm_stream ----
from llm_stream import (  # noqa: F401
    analyze,
    sse_stream_with_cleanup,
    stream_analyze,
    stream_conditions,
    stream_followup,
)

__all__ = [
    "LlmConfigError",
    "MultiKeyError",
    "_active_id",
    "_active_profile",
    "_bool_of",
    "_chat_url",
    "_env_keys",
    "_env_or",
    "_float_in",
    "_guess_name",
    "_host_of",
    "_int_in",
    "_load_env_file",
    "_max_tokens_in",
    "_norm_profile",
    "_norm_profiles",
    "_pool",
    "_pool_keys",
    "_pool_of",
    "_profile_creds",
    "_read_settings_file",
    "_slug",
    "_split_keys",
    "_tiny_tokens_text",
    "_tokens_hint",
    "_write_settings_file",
    "activate_profile",
    "config",
    "current_profiles",
    "get_settings",
    "is_configured",
    "key_pool",
    "key_states",
    "key_usable",
    "mark_key_bad",
    "mask_key",
    "public_status",
    "reset_key_state",
    "save_settings",
    "_build_system",
    "_fmt_chosen_condition",
    "_fmt_mass_row",
    "_norm_conditions",
    "_one_mass",
    "build_conditions_messages",
    "build_followup_messages",
    "build_messages",
    "mass_info",
    "_body",
    "_delta_of",
    "_error_text",
    "_headers",
    "_is_transient",
    "_request",
    "_think_of",
    "extract_json",
    "list_models",
    "network_error_text",
    "probe",
    "proxies_setting",
    "request_chat",
    "analyze",
    "sse_stream_with_cleanup",
    "stream_analyze",
    "stream_conditions",
    "stream_followup",
]
