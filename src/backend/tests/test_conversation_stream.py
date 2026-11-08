"""真实分块协议夹具：完成前正文、跨块安全及重试预览重置。"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings, ModelRole
from app.conversation.preview import AnswerPreview
from app.infra.model_gateway import DefaultModelGateway, OpenAICompatibleAdapter, InMemoryCallLedger, Succeeded, Failed, NeedsReconcile
from app.prompts import PromptRegistry
from app.prompts.schemas import StreamKnowledgeAnswer
from app.prompts.schemas import ExtractionResult
from tests.test_model_gateway import _settings


def prompts():
    r = PromptRegistry.load()
    return [r.get("chat.system").render(tool_catalog=[]), r.get("chat.general").render(question="CIF保险是谁安排？", context={}, task_spec={}, preferences=[], knowledge_sources=[], skill_material=[])]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts, *, done=True): self.parts, self.done = parts, done

    async def __aiter__(self):
        for part in self.parts:
            payload = {"id":"test-stream", "choices":[{"index":0,"delta":{"content":part},"finish_reason":None}]}
            yield ("data: " + json.dumps(payload, ensure_ascii=False) + "\r\n\r\n").encode()
            await asyncio.sleep(.001)
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n'
        if self.done: yield b'data: [DONE]\n\n'


async def test_workflow_buffered_stream_validates_complete_json_and_repairs_once():
    from tests.test_model_gateway import _extract_prompt
    calls=[]
    async def handler(request):
        body=json.loads(request.content);calls.append(body)
        value='{"fields":[],"line_items":[]}' if len(calls)==2 else '{"fields":[]}'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks([value]))
    ledger=InMemoryCallLedger()
    gateway=DefaultModelGateway(_settings(model_workflow_stream=True),
        adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))),ledger=ledger)
    try:
        result=await gateway.structured_generate(gateway.binding_for(ModelRole.EXTRACT),[_extract_prompt()],ExtractionResult,'buffered-json')
        assert isinstance(result,Succeeded) and len(calls)==2
        assert all(body['stream'] is True for body in calls)
        assert [record.outcome for record in ledger.records]==['invalid_output','succeeded']
    finally:await gateway.aclose()


@pytest.mark.parametrize("frozen_value", [None, False, True])
async def test_stream_uses_frozen_thinking_option(frozen_value):
    from tests.test_model_gateway import _extract_prompt
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            stream=Chunks(['{"fields":[],"line_items":[]}']))
    ledger = InMemoryCallLedger()
    gateway = DefaultModelGateway(_settings(extract_model_enable_thinking=True),
        adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))), ledger=ledger)
    frozen = DefaultModelGateway(_settings(extract_model_enable_thinking=frozen_value)).binding_for(ModelRole.EXTRACT)
    async def collect(delta):
        pass
    try:
        result = await gateway.structured_stream(frozen, [_extract_prompt()], ExtractionResult, "frozen-thinking", collect)
        assert isinstance(result, Succeeded) and len(calls) == 1
        if frozen_value is None:
            assert "enable_thinking" not in calls[0]
        else:
            assert calls[0]["enable_thinking"] is frozen_value
        assert ledger.records[0].params["enable_thinking"] is frozen_value
    finally:
        await gateway.aclose()


async def test_stream_preview_arrives_before_completion_and_is_accounted(monkeypatch):
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    events, calls = [], []
    async def emit(kind, payload): events.append((kind, payload))
    preview = AnswerPreview(emit, Settings(_env_file=None, chat_stream_event_chars=64))
    text = "保险通常由卖方安排。" * 20
    parts = ['{"domain":"related_material","source_ids":[],"paragraphs":["' + text[:240], text[240:] + '"]}']
    class Stream(Chunks):
        async def __aiter__(self):
            n = 0
            async for chunk in super().__aiter__():
                if n == 1: assert any(kind == "answer_preview_delta" for kind, _ in events)
                n += 1
                yield chunk
    async def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, stream=Stream(parts))
    ledger = InMemoryCallLedger()
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))), ledger=ledger)
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "stream-case", preview.feed)
        await preview.flush()
        assert isinstance(result, Succeeded)
        assert len(calls) == 1 and calls[0]["stream"] is True
        assert "stream_options" not in calls[0]
        assert ledger.records[0].usage.total_tokens == 30
        assert ledger.records[0].first_delta_ms is not None
        assert "".join(p["text"] for k, p in events if k == "answer_preview_delta") == text
    finally: await gateway.aclose()


async def test_missing_done_without_preview_reset_does_not_finalize(monkeypatch):
    calls = []
    async def handler(req):
        calls.append(req)
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, stream=Chunks(['{"domain":"logistics","source_ids":[],"paragraphs":["保险"]}'], done=False))
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    async def emit(text): pass
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "truncated-stream", emit)
        assert isinstance(result, NeedsReconcile)
        assert len(calls) == 1
    finally: await gateway.aclose()


@pytest.mark.parametrize("workflow", [False, True])
async def test_interrupted_stream_retries_clean_preview_or_buffer(monkeypatch, workflow):
    from tests.test_model_gateway import _extract_prompt, Sleeps
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    calls, events = [], []
    async def emit(kind, payload): events.append((kind, payload))
    preview = AnswerPreview(emit, Settings(_env_file=None, chat_stream_event_chars=64))
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            text = '{"fields":[]' if workflow else '{"domain":"logistics","source_ids":[],"paragraphs":["过时回答。'
            yield ('data: ' + json.dumps({"id":"old", "choices":[{"index":0,"delta":{"content":text}}]}, ensure_ascii=False) + '\n\n').encode()
            if not workflow:
                await preview.flush()
            raise httpx.RemoteProtocolError("disconnected")
    async def handler(request):
        calls.append(request)
        stream = Broken() if len(calls) == 1 else Chunks(['{"fields":[],"line_items":[]}'] if workflow else ['{"domain":"logistics","source_ids":[],"paragraphs":["新的完整回答。"]}'])
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, stream=stream)
    ledger, sleeps = InMemoryCallLedger(), Sleeps()
    gateway = DefaultModelGateway(_settings(model_workflow_stream=True),
        adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))), ledger=ledger, sleep=sleeps)
    try:
        if workflow:
            result = await gateway.structured_generate(gateway.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "buffer-retry")
            assert result.value.fields == []
        else:
            async with preview:
                result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer,
                    "preview-retry", preview.feed, on_retry=preview.reset)
                await preview.flush()
            assert result.value.paragraphs == ["新的完整回答。"]
            reset = next(i for i, (kind, _) in enumerate(events) if kind == "answer_preview_reset")
            assert ''.join(p['text'] for k,p in events[reset+1:] if k == 'answer_preview_delta') == '新的完整回答。'
        assert isinstance(result, Succeeded) and len(calls) == 2 and sleeps.calls == [2.0]
        assert [r.outcome for r in ledger.records] == ["interrupted", "succeeded"]
        assert ledger.records[0].http_status == 200 and ledger.records[0].provider_response_id == "old"
    finally:
        await gateway.aclose()


async def test_late_metadata_unicode_and_source_binding(monkeypatch):
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    preview = AnswerPreview(emit, Settings(_env_file=None), sources=[{"source_id":"s","facts":["已核对事实"]}])
    raw = '{"paragraphs":["\\u4fdd\\u9669 \\ud83d\\ude80 [[s:0]]"],"source_ids":["s"],"domain":"related_material"}'
    boundary = raw.index('"domain"')
    for character in raw[:boundary]: await preview.feed(character)
    assert not events
    for character in raw[boundary:]: await preview.feed(character)
    await preview.flush()
    assert "".join(p["text"] for k,p in events if k == "answer_preview_delta") == "保险 🚀 已核对事实"


async def test_unknown_general_reference_is_omitted_without_losing_safe_answer(monkeypatch):
    from app.conversation.knowledge import verify_knowledge_paragraphs
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    sources = [{"source_id":"s", "facts":["费用与风险应分别核对。"]}]
    preview = AnswerPreview(emit, Settings(_env_file=None), sources=sources)
    text = "卖方通常安排保险。[[unknown:9]][[s:0]]"
    raw = json.dumps({"domain":"logistics", "source_ids":["s","unknown"], "paragraphs":[text]}, ensure_ascii=False)
    for char in raw: await preview.feed(char)
    await preview.flush()
    rendered = "".join(payload["text"] for kind,payload in events if kind == "answer_preview_delta")
    assert rendered == verify_knowledge_paragraphs([text], ["s","unknown"], sources)[0]
    assert rendered == "卖方通常安排保险。费用与风险应分别核对。"


async def test_sensitive_split_is_never_previewed(monkeypatch):
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ("private-key-for-test",))
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ("private-key-for-test",))
    events = []
    async def emit(kind, payload): events.append((kind,payload))
    preview = AnswerPreview(emit, Settings(_env_file=None))
    await preview.feed('{"domain":"logistics","source_ids":[],"paragraphs":["private-key-')
    import pytest
    with pytest.raises(ValueError): await preview.feed('for-test"]}')
    assert not events


async def test_preview_flushes_during_provider_pause(monkeypatch):
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    emitted = asyncio.Event()
    async def emit(kind, payload):
        if kind == "answer_preview_delta": emitted.set()
    preview = AnswerPreview(emit, Settings(_env_file=None, chat_stream_event_chars=256))
    async with preview:
        await preview.feed('{"domain":"logistics","source_ids":[],"paragraphs":["' + "保" * 110)
        await asyncio.wait_for(emitted.wait(), .3)
    assert preview.offset == 110


@pytest.mark.parametrize("ending", [b"\r", b"\r\n", b"\n"])
async def test_sse_line_endings_and_partial_receipt(ending):
    class RawStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            rows = [b'data: {"id":"stream-receipt","choices":[{"index":0,"delta":{"content":"invalid"},"finish_reason":"stop"}],"usage":{"total_tokens":17}}', b'', b'data: [DONE]', b'']
            raw = ending.join(rows) + ending
            for byte in raw: yield bytes([byte])
    async def handler(req): return httpx.Response(200, headers={"content-type":"text/event-stream"}, stream=RawStream())
    async def emit(text): pass
    ledger = InMemoryCallLedger()
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))), ledger=ledger)
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "bad-content", emit)
        assert isinstance(result, Failed) and result.kind == "invalid_output"
        assert ledger.records[0].provider_response_id == "stream-receipt"
        assert ledger.records[0].usage.total_tokens == 17
    finally: await gateway.aclose()


async def test_truncated_stream_preserves_usage_and_response_id():
    async def handler(req):
        raw = b'data: {"id":"partial-receipt","choices":[],"usage":{"total_tokens":17}}\n\n'
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, content=raw)
    async def emit(text): pass
    ledger = InMemoryCallLedger()
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))), ledger=ledger)
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "partial-content", emit)
        assert isinstance(result, NeedsReconcile)
        assert ledger.records[0].provider_response_id == "partial-receipt"
        assert ledger.records[0].usage.total_tokens == 17
        assert len(ledger.records) == 1
    finally: await gateway.aclose()


async def test_transport_error_after_accepted_stream_is_never_resent():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"accepted","choices":[{"index":0,"delta":{"content":"{"},"finish_reason":null}]}\n\n'
            raise httpx.ConnectError("connection lost after response")
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, stream=BrokenStream())
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    async def emit(text): pass
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "accepted-disconnect", emit)
        from app.infra.model_gateway import NeedsReconcile
        assert isinstance(result, NeedsReconcile) and len(calls) == 1
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("exception,reason", [
    (ValueError("段落来源不匹配"), "paragraph_source_mismatch"),
    (ValueError("private-response-value"), "ValueError"),
    (TypeError("private-response-value"), "TypeError"),
    (KeyError("private-response-value"), "KeyError"),
    (AttributeError("private-response-value"), "AttributeError"),
])
async def test_stream_consumer_validation_is_local_and_preserves_receipt(exception, reason):
    calls = []
    async def handler(request):
        calls.append(request)
        raw = b'data: {"id":"consumer-receipt","choices":[{"index":0,"delta":{"content":"candidate"},"finish_reason":null}],"usage":{"total_tokens":17}}\n\n'
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, content=raw)
    async def reject(text): raise exception
    ledger = InMemoryCallLedger()
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))), ledger=ledger)
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "consumer-validation", reject)
        assert isinstance(result, Failed) and result.kind == "invalid_output"
        assert len(calls) == 1 and len(ledger.records) == 1
        record = ledger.records[0]
        assert record.error_class == "stream_consumer:" + reason
        assert record.http_status == 200 and record.provider_response_id == "consumer-receipt"
        assert record.usage.total_tokens == 17 and record.first_delta_ms is not None
        assert "private-response-value" not in record.model_dump_json() + result.message
    finally:
        await gateway.aclose()


async def test_stream_provider_payload_error_is_not_reclassified_as_consumer():
    async def handler(request):
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, content=b'data: {"choices":"invalid"}\n\n')
    received = []
    async def emit(text): received.append(text)
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "provider-payload", emit)
        assert isinstance(result, Failed) and result.kind == "provider_protocol_error"
        assert result.records[0].error_class == "protocol:choices_type" and not received
    finally:
        await gateway.aclose()


async def test_stream_metadata_and_extra_choices_keep_only_primary_answer():
    events = [
        {"object": "chat.completion.chunk", "choices": None},
        {"id": "metadata-only", "usage": None},
        {"choices": [{"index": 1, "delta": {"content": "wrong"}},
                     {"index": 0, "delta": {"content": '{"domain":"logistics","source_ids":[],"paragraphs":["保险"]}'}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": None, "usage": {"total_tokens": 30}},
    ]
    raw = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events) + 'data: [DONE]\n\n'
    async def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())
    received = []
    async def emit(text): received.append(text)
    gateway = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    try:
        result = await gateway.structured_stream(gateway.binding_for(ModelRole.CHAT), prompts(), StreamKnowledgeAnswer, "metadata-chunks", emit)
        assert isinstance(result, Succeeded) and result.value.paragraphs == ["保险"]
        assert "wrong" not in ''.join(received) and result.records[0].usage.total_tokens == 30
    finally:
        await gateway.aclose()


async def test_short_natural_preview_and_final_value_share_rendering(monkeypatch):
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    facts = [{"evidence_id": "invoice", "claim_kind": "source_value", "value": "INV-001", "canonical_text": "发票号INV-001"}]
    preview = AnswerPreview(emit, Settings(_env_file=None), facts=facts)
    await preview.feed('{"domain":"logistics","paragraphs":[{"claim_kind":"source_value","evidence_ids":["invoice"],"status":"asserted","text":"发票编号是[[invoice]]，货物')
    await preview.flush()
    assert "".join(p["text"] for k,p in events if k == "answer_preview_delta") == "发票编号是INV-001，货物"


@pytest.mark.parametrize("value,unit,suffix,expected", [
    ("60", "件", " 件，数量保持不变。", "60 件，数量保持不变。"),
    ("120 pcs", "pcs", " pcs。", "120 pcs。"),
    ("100", "kg", " kg。", "100 kg。"),
    ("100", "kg", " kgf 是不同单位。", "100 kg kgf 是不同单位。"),
    ("INV-001", None, " 件货物。", "INV-001 件货物。"),
])
async def test_duplicate_unit_after_fact_has_stable_character_stream(monkeypatch, value, unit, suffix, expected):
    from app.conversation.evidence import replace_fact_placeholders
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    fact = {"evidence_id": "q", "claim_kind": "source_value", "value": value, "unit": unit, "canonical_text": value}
    preview = AnswerPreview(emit, Settings(_env_file=None), facts=[fact])
    text = "[[q]]" + suffix
    raw = json.dumps({"domain": "logistics", "paragraphs": [{"claim_kind": "source_value", "evidence_ids": ["q"], "status": "asserted", "text": text}]}, ensure_ascii=False)
    for char in raw:
        await preview.feed(char)
    await preview.flush()
    assert "".join(p["text"] for k, p in events if k == "answer_preview_delta") == expected
    assert replace_fact_placeholders(text, {"q": fact}) == expected


def test_sensitive_stream_syntax_prefixes_are_held():
    from app.conversation.preview import safe_stream_prefix
    assert safe_stream_prefix("发票信息：/Us", ()) == "发票信息："
    assert safe_stream_prefix("解释如下 sk-abc", ()) == "解释如下 "
    assert safe_stream_prefix("说明 Bearer abc", ()) == "说明 "
    assert safe_stream_prefix("系统提示词如下：部分", ()) == ""
    assert safe_stream_prefix("这是木箱运输单据。", ("private-key-for-test",)) == "这是木箱运输单据。"


async def test_natural_preview_accepts_mixed_fact_types_and_additional_authorized_support(monkeypatch):
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    facts = [{"evidence_id": "quantity", "claim_kind": "adopted_value", "value": "100", "unit": "kg", "canonical_text": "100 kg"},
        {"evidence_id": "name", "claim_kind": "source_value", "value": "Fresh strawberries", "canonical_text": "Fresh strawberries"}]
    raw = json.dumps({"domain": "logistics", "paragraphs": [{"claim_kind": "source_value", "status": "asserted",
        "evidence_ids": ["quantity", "name"], "text": "草莓的采用重量为[[quantity]]。"}]}, ensure_ascii=False)
    preview = AnswerPreview(emit, Settings(_env_file=None), facts=facts)
    for char in raw: await preview.feed(char)
    await preview.flush()
    assert "".join(p["text"] for k, p in events if k == "answer_preview_delta") == "草莓的采用重量为100 kg。"
    preview = AnswerPreview(emit, Settings(_env_file=None), facts=facts)
    with pytest.raises(ValueError, match="段落来源不匹配"):
        await preview.feed(raw.replace('"name"', '"unauthorized"'))


async def test_unknown_display_metadata_normalizes_without_skipping_evidence_checks(monkeypatch):
    from app.prompts.schemas import StreamNaturalAnswer
    from pydantic import ValidationError
    monkeypatch.setattr("app.conversation.preview.protected_values", lambda: ())
    monkeypatch.setattr("app.conversation.safety.protected_values", lambda: ())
    events = []
    async def emit(kind, payload): events.append((kind, payload))
    facts = [{"evidence_id": "name", "claim_kind": "source_value", "value": "Fresh strawberries", "canonical_text": "Fresh strawberries"}]
    raw = json.dumps({"domain": "logistics", "paragraphs": [{"claim_kind": "document_summary", "status": "candidate",
        "evidence_ids": ["name"], "text": "货物为[[name]]。"}]}, ensure_ascii=False)
    preview = AnswerPreview(emit, Settings(_env_file=None), facts=facts)
    for char in raw: await preview.feed(char)
    await preview.flush()
    parsed = StreamNaturalAnswer.model_validate_json(raw)
    assert parsed.paragraphs[0].claim_kind == "general_explanation" and parsed.paragraphs[0].status == "cannot_confirm"
    assert "".join(p["text"] for k, p in events if k == "answer_preview_delta") == "货物为Fresh strawberries。"
    invalid = json.loads(raw); invalid["paragraphs"][0]["evidence_ids"] = {"name": True}
    with pytest.raises(ValidationError): StreamNaturalAnswer.model_validate(invalid)
    invalid = json.loads(raw); invalid["paragraphs"][0].pop("claim_kind")
    with pytest.raises(ValidationError): StreamNaturalAnswer.model_validate(invalid)
