"""有界 OpenAI SSE 读取；协议回包不直接发送到用户页面。"""
import json
import time

import httpx


async def complete_stream(client, binding, request, api_key):
    from app.infra.model_gateway import (
        RawCompletion, _NotSent, _Permanent, _Rejected, _AcceptanceUnknown,
        _ProtocolError, _StreamConsumerError, _parse_usage, _parse_retry_after, chat_completions_url,
    )
    if not api_key:
        raise _Permanent("unavailable", None)
    body = {"model": binding.model_name, "messages": request.messages,
            "max_tokens": request.max_tokens, "stream": True}
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.response_format:
        body["response_format"] = request.response_format
    if binding.enable_thinking is not None:
        body["enable_thinking"] = binding.enable_thinking
    timeout = httpx.Timeout(binding.timeout_seconds, connect=min(10, binding.timeout_seconds))
    content, data, buffer = [], [], bytearray()
    event_bytes, skip_lf = 0, False
    total, first, finish, done, response_id, model, usage = 0, None, None, False, None, None, None
    accepted = False
    started = time.monotonic()
    def partial():
        return RawCompletion(content=None, finish_reason=finish, usage=usage, provider_model_id=model,
            provider_response_id=response_id, http_status=200, first_delta_ms=first)

    async def consume():
        nonlocal finish, done, response_id, model, usage, first, event_bytes
        event_bytes = 0
        if not data:
            return
        value = "\n".join(data)
        data.clear()
        if value == "[DONE]":
            if done or finish not in {"stop", "length"}: raise _ProtocolError(200)
            done = True
            return
        if done:
            raise _ProtocolError(200)
        try:
            payload = json.loads(value)
            if payload.get("error"):
                raise ValueError
            choices = payload.get("choices")
            metadata_keys = {"id", "object", "created", "model", "system_fingerprint", "usage", "choices"}
            if choices is None and payload and set(payload).issubset(metadata_keys):
                choices = []
            if not isinstance(choices, list):
                response_events = {"response.created", "response.in_progress", "response.completed", "response.output_text.delta"}
                reason = "choices_null_response_event" if payload.get("type") in response_events else "choices_null"
                raise _ProtocolError(200, reason=reason if choices is None else "choices_type")
            primary = [choice for choice in choices if isinstance(choice, dict) and choice.get("index") == 0]
            if choices and len(primary) != 1:
                raise _ProtocolError(200, reason="primary_choice_missing" if not primary else "primary_choice_duplicate")
            if isinstance(payload.get("id"), str): response_id = payload["id"]
            if isinstance(payload.get("model"), str): model = payload["model"]
            if payload.get("usage") is not None: usage = _parse_usage(payload["usage"])
            if not choices:
                return
            choice = primary[0]
            if choice.get("index") != 0:
                raise _ProtocolError(200, reason="choice_index")
            delta = choice.get("delta") or {}
            if delta.get("tool_calls") or delta.get("function_call") or delta.get("refusal"):
                raise _ProtocolError(200, reason="non_answer_delta")
            text = delta.get("content")
            if text is not None and not isinstance(text, str):
                raise _ProtocolError(200, reason="content_type")
            if text:
                if finish is not None: raise _ProtocolError(200, reason="content_after_finish")
                if first is None: first = int((time.monotonic() - started) * 1000)
                content.append(text)
            if choice.get("finish_reason") is not None:
                if finish is not None: raise _ProtocolError(200, reason="duplicate_finish")
                finish = choice["finish_reason"]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _ProtocolError(200) from None
        if text:
            try:
                await request.on_delta(text)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                # 只登记固定分类，异常文本可能包含原文或秘密，不能直接进账本。
                reasons = {"JSON重复字段": "json_duplicate_key", "JSON嵌套超限": "json_depth_limit",
                    "内部信息候选被阻止": "internal_information", "未知来源": "unknown_source",
                    "未知知识来源": "unknown_knowledge_source", "知识来源越界": "knowledge_source_range",
                    "未闭合来源": "unclosed_source", "无效Unicode正文": "invalid_unicode",
                    "正文缓冲超限": "text_buffer_limit", "协议字段缓冲超限": "metadata_buffer_limit",
                    "知识来源未授权": "unauthorized_knowledge_source", "段落元数据无效": "paragraph_metadata",
                    "段落来源不匹配": "paragraph_source_mismatch", "正文前缀已改变": "changed_text_prefix",
                    "正文超限": "text_size_limit"}
                reason = reasons.get(str(exc), type(exc).__name__)
                raise _StreamConsumerError(200, partial=partial(), reason=reason) from None
    try:
        async with client.stream("POST", chat_completions_url(binding.base_url), json=body,
                headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout) as response:
            status = response.status_code
            if status in {408, 429}: raise _Rejected(status, _parse_retry_after(response.headers.get("Retry-After")))
            if status in {401, 403}: raise _Permanent("auth", status)
            if 400 <= status < 500: raise _Permanent("invalid_request", status)
            if status != 200: raise _AcceptanceUnknown(status, reason="upstream_unavailable")
            accepted = True
            if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                raise _ProtocolError(status, reason="content_type")
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > request.stream_max_bytes:
                    raise _ProtocolError(status, reason="stream_size_limit")
                for byte in chunk:
                    if skip_lf:
                        skip_lf = False
                        if byte == 10: continue
                    if byte not in (10, 13):
                        buffer.append(byte)
                        if event_bytes + len(buffer) > request.stream_event_max_bytes:
                            raise _ProtocolError(status, reason="event_size_limit")
                        continue
                    skip_lf = byte == 13
                    try: line = buffer.decode("utf-8")
                    except UnicodeDecodeError: raise _ProtocolError(status) from None
                    event_bytes += len(buffer) + 1
                    buffer.clear()
                    if event_bytes > request.stream_event_max_bytes: raise _ProtocolError(status, reason="event_size_limit")
                    if not line:
                        await consume()
                    elif line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
            if buffer or data or not done or finish not in {"stop", "length"}:
                raise _AcceptanceUnknown(status, reason="incomplete_end", partial=partial())
    except _ProtocolError as exc:
        exc.partial = partial()
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        if accepted:
            raise _AcceptanceUnknown(200) from None
        raise _NotSent() from None
    except (httpx.UnsupportedProtocol, httpx.InvalidURL):
        raise _Permanent("unavailable", None) from None
    except httpx.TransportError as exc:
        raise _AcceptanceUnknown(200 if accepted else None, reason=type(exc).__name__, partial=partial() if accepted else None) from None
    return RawCompletion(content="".join(content), finish_reason=finish, usage=usage,
        provider_model_id=model, provider_response_id=response_id, http_status=200,
        first_delta_ms=first)
