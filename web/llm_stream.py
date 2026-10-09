# -*- coding: utf-8 -*-
"""大模型「工艺分析」——工艺分析 / 条件枚举 / 追问的流式编排 —— web/llm.py 的编排半边。

从 `llm.py` 拆出来的（机械搬移，逻辑未改）：原文件 2343 行一整块，
配置 / 提示词 / 传输 / 流式 四件事混在一起，改哪一样都要在两千行里翻。
对外名字空间不变 —— `llm.py` 负责再导出，app.py 不需要改。
"""
from __future__ import annotations

import json
from typing import Iterator
from llm_config import LlmConfigError, MultiKeyError, _tokens_hint, config, public_status
from llm_prompts import _norm_conditions, build_conditions_messages, build_followup_messages, build_messages, mass_info
from llm_transport import _body, _delta_of, _error_text, _is_transient, _think_of, extract_json, network_error_text, request_chat



def analyze(req: dict) -> dict:
    """非流式：一次性拿到结构化结果。"""
    cfg = config(req.get("profile"))
    if not (cfg["base_url"] and cfg["model"] and cfg["api_keys"]):
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
    except LlmConfigError as e:
        # 配置本身跑不通（例如输出额度小到出不来正文）—— 直接把文案交给上层，
        # 别落进下面「瞬时网络故障」那个分支被当成网络问题去重试。
        raise RuntimeError(str(e)) from e
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
            f"{_tokens_hint(cfg)}"
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

    cfg = config(req.get("profile"))
    tried: list[str] = []
    try:
        if not (cfg["base_url"] and cfg["model"] and cfg["api_keys"]):
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
                        f"推理模型的思考 token 也计入输出额度 ⇒ {_tokens_hint(cfg)}。"
                        f"也可以点「接着分析」从断点续写。"
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
    except LlmConfigError as e:
        # 配置本身跑不通 ⇒ 原样下发（不加 `LlmConfigError: ` 这种类名前缀，用户不需要看类名）
        yield sse({"type": "error", "error": str(e)})
        return
    except Exception as e:  # noqa: BLE001
        # 瞬时网络故障（TLS 被掐 / 代理端口失效 / 读半截）翻成人话，别甩一个
        # SSLEOFError 让用户猜 —— 实测这类错误重发就好，指引里直接给开关。
        if _is_transient(e):
            yield sse({"type": "error", "error": network_error_text(e, tried, cfg["base_url"])})
        else:
            yield sse({"type": "error", "error": f"{type(e).__name__}: {e}"})

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

    cfg = config(req.get("profile"))
    tried: list[str] = []
    try:
        if not (cfg["base_url"] and cfg["model"] and cfg["api_keys"]):
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
                        f"多方案输出较长，{_tokens_hint(cfg)}"
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
    except LlmConfigError as e:
        yield sse({"type": "error", "error": str(e)})
        return
    except Exception as e:  # noqa: BLE001
        if _is_transient(e):
            yield sse({"type": "error", "error": network_error_text(e, tried, cfg["base_url"])})
        else:
            yield sse({"type": "error", "error": f"{type(e).__name__}: {e}"})

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

    cfg = config(req.get("profile"))
    tried: list[str] = []
    try:
        if not (cfg["base_url"] and cfg["model"] and cfg["api_keys"]):
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
                        f"推理模型的思考 token 也计入输出额度，{_tokens_hint(cfg)}。"
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
    except LlmConfigError as e:
        yield sse({"type": "error", "error": str(e)})
        return
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
