"""
Model Gateway 定向测试。全部经 httpx.MockTransport 模拟供应商，不调用任何付费接口。

覆盖：假适配器确定性与标记、严格 json_schema 请求形态、结构无效只修复一次、
模型传输中断有限重试及开关、可重试错误的次数与间隔、永久错误不重试、
截断不修复、冻结绑定与配置不符、未探测能力降级、预算与按角色并发上限、密钥不入账本与日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.config import AdapterKind, ModelRole, Settings
from app.infra.model_gateway import (
    FAKE_MARKER,
    CallBudget,
    CallLedgerWriteError,
    DefaultModelGateway,
    Failed,
    ImageInput,
    InMemoryCallLedger,
    NeedsReconcile,
    OpenAICompatibleAdapter,
    Succeeded,
    build_model_gateway,
)
from app.prompts import PromptRegistry
from app.prompts.schemas import ExtractionResult, IntentCandidate, TranslationResult, VisionTranscription, resolve_output_schema

SECRET = "sk-test-SECRET-should-never-leak"
BASE = "https://relay.example.com"

REGISTRY = PromptRegistry.load()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"model_adapter": AdapterKind.OPENAI_COMPATIBLE}
    for role in ("chat", "extract", "translate", "vision"):
        values.update(
            {
                f"{role}_model_base_url": BASE,
                f"{role}_model_api_key": SECRET,
                f"{role}_model_name": "grok-4.6",
                f"{role}_model_protocol": "openai_chat_completions",
                f"{role}_model_timeout_seconds": 30,
                f"{role}_model_max_output_tokens": 8000,
                f"{role}_model_max_concurrency": 2,
                f"{role}_model_temperature": 0,
            }
        )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _extract_prompt():
    return REGISTRY.get("document.extract").render(
        document_type="commercial_invoice",
        field_schema=[{"field_key": "invoice_no", "role": "invoice"}],
        segments=[{"id": "seg-1", "text": "Invoice No. INV-001"}],
    )


VALID_EXTRACTION = {
    "fields": [
        {
            "field_key": "invoice_no",
            "role": "invoice",
            "raw_value": "INV-001",
            "source_segment_ids": ["seg-1"],
            "confidence_label": "high",
            "ambiguity": None,
        }
    ],
    "line_items": [],
}


def _completion(content: str, *, finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": "grok-4.6-provider",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 1019,
            "total_tokens": 1119,
            "completion_tokens_details": {"reasoning_tokens": 992},
        },
    }


class Script:
    """按顺序返回预设响应或抛出传输异常，并记录每次请求。"""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("simulated", request=request)
        if isinstance(step, httpx.Response):
            return step
        return httpx.Response(200, json=step)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _gateway(script: Script, settings: Settings | None = None) -> tuple[DefaultModelGateway, InMemoryCallLedger, Sleeps]:
    ledger = InMemoryCallLedger()
    sleeps = Sleeps()
    client = httpx.AsyncClient(transport=httpx.MockTransport(script))
    gw = DefaultModelGateway(
        settings or _settings(), adapter=OpenAICompatibleAdapter(client), ledger=ledger, sleep=sleeps
    )
    return gw, ledger, sleeps


# ---- 假适配器 ----


async def test_fake_adapter_is_deterministic_and_marked() -> None:
    settings = Settings(_env_file=None, model_adapter=AdapterKind.DETERMINISTIC_FAKE)
    gw = build_model_gateway(settings)
    binding = gw.binding_for(ModelRole.TRANSLATE)
    assert binding is not None and binding.protocol == "deterministic_fake"
    prompt = REGISTRY.get("document.translate").render(
        target_language="en", glossary={}, segments=[{"id": "s1", "text": "品名 ⟦P1⟧"}, {"id": "s2", "text": "数量"}]
    )
    first = await gw.structured_generate(binding, [prompt], TranslationResult, "req-1")
    second = await gw.structured_generate(binding, [prompt], TranslationResult, "req-2")
    assert isinstance(first, Succeeded) and isinstance(second, Succeeded)
    assert first.is_fake and all(r.is_fake for r in first.records)
    assert first.value == second.value
    assert first.records[0].input_fingerprint == second.records[0].input_fingerprint
    assert [s.source_segment_ids for s in first.value.segments] == [["s1"], ["s2"]]
    assert all(FAKE_MARKER in s.translated_text for s in first.value.segments)
    assert "⟦P1⟧" in first.value.segments[0].translated_text


async def test_fake_intent_is_conservative() -> None:
    gw = build_model_gateway(Settings(_env_file=None, model_adapter=AdapterKind.DETERMINISTIC_FAKE))
    prompt = REGISTRY.get("chat.intent").render(action_catalog=[], authorized_labels=[], question="帮我翻译", skill_material=[])
    out = await gw.structured_generate(gw.binding_for(ModelRole.CHAT), [prompt], resolve_output_schema(prompt.output_schema), "req")
    assert isinstance(out, Succeeded)
    assert out.value.action == "clarify" and out.value.target_language is None


async def test_fake_mode_refuses_real_binding() -> None:
    real_binding = build_model_gateway(_settings()).binding_for(ModelRole.EXTRACT)
    fake_gw = build_model_gateway(Settings(_env_file=None, model_adapter=AdapterKind.DETERMINISTIC_FAKE))
    out = await fake_gw.structured_generate(real_binding, [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Failed) and out.kind == "binding_mismatch"


# ---- 请求形态与账本 ----


async def test_strict_schema_request_and_ledger_without_secret(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    script = Script(_completion(json.dumps(VALID_EXTRACTION)))
    gw, ledger, _ = _gateway(script)
    binding = gw.binding_for(ModelRole.EXTRACT)
    prompt = _extract_prompt()
    out = await gw.structured_generate(binding, [prompt], ExtractionResult, "req-strict")
    assert isinstance(out, Succeeded) and not out.is_fake
    assert out.value.fields[0].raw_value == "INV-001"

    req = script.requests[0]
    assert str(req.url) == f"{BASE}/v1/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {SECRET}"
    body = json.loads(req.content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False and set(schema["required"]) == {"fields", "line_items"}
    assert body["max_tokens"] == 8000 and body["temperature"] == 0

    [record] = ledger.records
    assert record.outcome == "succeeded"
    assert record.model_id == "grok-4.6" and record.provider_model_id == "grok-4.6-provider"
    assert record.endpoint_config_hash == binding.public_config_hash
    assert record.prompt_refs[0].key == "document.extract" and record.prompt_refs[0].sha256 == prompt.sha256
    assert record.usage and record.usage.reasoning_tokens == 992
    assert record.cost_cny is None
    dumped = record.model_dump_json()
    assert SECRET not in dumped
    # 账本不存渲染后的全文
    assert "Invoice No. INV-001" not in dumped
    assert SECRET not in caplog.text and "INV-001" not in caplog.text


# ---- 结构无效只修复一次 ----


async def test_thinking_option_is_frozen_and_legacy_configuration_stays_compatible() -> None:
    import hashlib
    from app.workflows.contracts import ModelBinding

    legacy = _settings().model_profile(ModelRole.EXTRACT)
    historical = legacy.model_dump(exclude={"api_key", "enable_thinking"}, mode="json")
    assert legacy.public_fingerprint() == hashlib.sha256(
        json.dumps(historical, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    script = Script(*[_completion(json.dumps(VALID_EXTRACTION)) for _ in range(2)])
    gateway, ledger, _ = _gateway(script, _settings(extract_model_enable_thinking=True, model_workflow_stream=False))
    frozen = build_model_gateway(_settings(extract_model_enable_thinking=False)).binding_for(ModelRole.EXTRACT)
    old = frozen.model_dump(exclude={"enable_thinking"})
    old_binding = ModelBinding.model_validate(old)
    try:
        assert old_binding.enable_thinking is None
        assert frozen.public_config_hash != legacy.public_fingerprint()
        for binding in (frozen, old_binding):
            result = await gateway.structured_generate(binding, [_extract_prompt()], ExtractionResult, str(binding.enable_thinking))
            assert isinstance(result, Succeeded)
        assert json.loads(script.requests[0].content)["enable_thinking"] is False
        assert "enable_thinking" not in json.loads(script.requests[1].content)
        assert ledger.records[0].params["enable_thinking"] is False
        assert SECRET not in ledger.records[0].model_dump_json()
    finally:
        await gateway.aclose()


async def test_invalid_output_repaired_once() -> None:
    script = Script(_completion('{"fields": "oops"}'), _completion(json.dumps(VALID_EXTRACTION)))
    gw, ledger, _ = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Succeeded)
    assert [r.attempt_kind for r in ledger.records] == ["initial", "repair"]
    assert [r.outcome for r in ledger.records] == ["invalid_output", "succeeded"]
    assert ledger.records[1].repair_instruction_sha256 is not None
    repair_body = json.loads(script.requests[1].content)
    assert repair_body["messages"][-2] == {"role": "assistant", "content": '{"fields": "oops"}'}


async def test_invalid_output_twice_fails_without_third_request() -> None:
    script = Script(_completion("not json"), _completion('{"fields": []}'))
    gw, ledger, _ = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Failed) and out.kind == "invalid_output"
    assert len(script.requests) == 2 and not script.steps


async def test_truncated_output_is_not_repaired() -> None:
    script = Script(_completion('{"fields": [', finish="length"))
    gw, _, _ = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Failed) and out.kind == "output_truncated"
    assert len(script.requests) == 1


# ---- 模型传输中断：有限重试并保留未知费用 ----


@pytest.mark.parametrize(
    "step",
    [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.WriteError, httpx.Response(502), httpx.Response(504), httpx.Response(500), httpx.Response(503), httpx.Response(529)],
)
async def test_interrupted_model_request_retries_and_retains_attempt(step: Any) -> None:
    script = Script(step, _completion(json.dumps(VALID_EXTRACTION)))
    gw, ledger, sleeps = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req-x")
    assert isinstance(out, Succeeded)
    assert len(script.requests) == 2 and sleeps.calls == [2.0]
    assert [r.outcome for r in ledger.records] == ["interrupted", "succeeded"]
    assert all(r.provider_request_key == "req-x" for r in ledger.records)


async def test_repair_timeout_retries_within_overall_attempt_limit() -> None:
    script = Script(_completion("not json"), httpx.ReadTimeout, _completion(json.dumps(VALID_EXTRACTION)))
    gw, _, _ = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Succeeded)
    assert len(script.requests) == 3


async def test_interrupted_retries_exhaust_and_can_be_retried_later():
    script = Script(httpx.ReadTimeout, httpx.ReadTimeout, httpx.ReadTimeout)
    gw, ledger, sleeps = _gateway(script)
    result = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(result, Failed) and result.kind == "transient_exhausted" and result.retry_later
    assert len(script.requests) == 3 and sleeps.calls == [2.0, 4.0]
    assert all(r.outcome == "interrupted" for r in ledger.records)


async def test_interrupted_retry_off_budget_or_revocation_stops_resending():
    for policy in ("off", "budget", "revoked"):
        class Ledger(InMemoryCallLedger):
            checks = 0
            def before_send(self):
                self.checks += 1
                if policy == "revoked" and self.checks > 1:
                    raise RuntimeError("deleted")
        ledger, sleeps = Ledger(), Sleeps()
        script = Script(httpx.ReadTimeout, _completion(json.dumps(VALID_EXTRACTION)))
        gw = DefaultModelGateway(_settings(model_retry_interrupted=policy != "off"),
            adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(script))), ledger=ledger, sleep=sleeps)
        try:
            result = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req",
                budget=CallBudget(max_calls=1) if policy == "budget" else None)
            assert len(script.requests) == 1
            if policy == "off": assert isinstance(result, NeedsReconcile)
            else: assert isinstance(result, Failed) and result.kind == ("budget_exhausted" if policy == "budget" else "unavailable")
        finally:
            await gw.aclose()


# ---- 可重试错误 ----


@pytest.mark.parametrize("mode",["expired","revoked"])
async def test_preflight_sleep_or_revoked_owner_never_sends(mode):
    class Ledger(InMemoryCallLedger):
        def begin_attempt(self,*args):
            return datetime.now(UTC)-timedelta(seconds=1) if mode=="expired" else datetime.now(UTC)+timedelta(seconds=60)
        def before_send(self):
            if mode=="revoked":raise RuntimeError("owner changed")
    ledger=Ledger();script=Script(_completion(json.dumps(VALID_EXTRACTION)))
    gateway=DefaultModelGateway(_settings(),adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(script))),ledger=ledger)
    try:
        result=await gateway.structured_generate(gateway.binding_for(ModelRole.EXTRACT),[_extract_prompt()],ExtractionResult,"preflight-sleep")
        assert isinstance(result,Failed) and not script.requests
        assert len(ledger.records)==1 and ledger.records[0].outcome=="not_sent"
    finally:await gateway.aclose()


async def test_connect_error_retried_with_min_interval() -> None:
    script = Script(httpx.ConnectError, httpx.ConnectTimeout, _completion(json.dumps(VALID_EXTRACTION)))
    gw, ledger, sleeps = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Succeeded)
    assert sleeps.calls == [2.0, 2.0]
    assert [r.attempt_kind for r in ledger.records] == ["initial", "transport_retry", "transport_retry"]


@pytest.mark.parametrize('first_response',[httpx.Response(429,headers={'Retry-After':'2'}),_completion('{"fields":"invalid"}')])
async def test_case_preflight_rechecks_before_retry_or_repair(first_response):
    class RevokedLedger(InMemoryCallLedger):
        checks=0
        def before_send(self):
            self.checks+=1
            if self.checks>1:raise RuntimeError('synthetic case revoked')
    ledger=RevokedLedger();script=Script(first_response,_completion(json.dumps(VALID_EXTRACTION)))
    gateway=DefaultModelGateway(_settings(),adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(script))),ledger=ledger,sleep=Sleeps())
    result=await gateway.structured_generate(gateway.binding_for(ModelRole.EXTRACT),[_extract_prompt()],ExtractionResult,'revocation-preflight')
    assert isinstance(result,Failed) and result.kind=='unavailable'
    assert ledger.checks==2 and len(script.requests)==1


async def test_rate_limit_respects_retry_after() -> None:
    script = Script(httpx.Response(429, headers={"Retry-After": "7"}), _completion(json.dumps(VALID_EXTRACTION)))
    gw, _, sleeps = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Succeeded)
    assert sleeps.calls == [7.0]


async def test_retries_capped_at_three_attempts() -> None:
    script = Script(httpx.Response(429), httpx.Response(429), httpx.Response(408))
    gw, _, sleeps = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Failed) and out.kind == "transient_exhausted" and out.retry_later
    assert len(script.requests) == 3
    assert all(s >= 2.0 for s in sleeps.calls) and len(sleeps.calls) == 2


@pytest.mark.parametrize("status,kind", [(400, "invalid_request"), (422, "invalid_request"), (401, "auth"), (403, "auth")])
async def test_permanent_errors_not_retried(status: int, kind: str) -> None:
    script = Script(httpx.Response(status))
    gw, _, sleeps = _gateway(script)
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Failed) and out.kind == kind and not out.retry_later
    assert len(script.requests) == 1 and sleeps.calls == []


# ---- 绑定与能力位 ----


async def test_frozen_binding_mismatch_is_refused() -> None:
    old_gw, _, _ = _gateway(Script())
    frozen = old_gw.binding_for(ModelRole.EXTRACT)
    script = Script()
    new_gw, _, _ = _gateway(script, _settings(extract_model_name="another-model"))
    out = await new_gw.structured_generate(frozen, [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Failed) and out.kind == "binding_mismatch"
    assert script.requests == []


async def test_unprobed_model_falls_back_to_json_text() -> None:
    script = Script(_completion(json.dumps(VALID_EXTRACTION)))
    gw, ledger, _ = _gateway(script, _settings(extract_model_name="unprobed-model"))
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Succeeded)
    body = json.loads(script.requests[0].content)
    assert "response_format" not in body
    assert "JSON Schema" in body["messages"][-1]["content"]
    assert ledger.records[0].params["output_mode"] == "prompt_json"


async def test_base_url_with_v1_is_config_error() -> None:
    script = Script()
    gw, _, _ = _gateway(script, _settings(extract_model_base_url=f"{BASE}/v1"))
    out = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req")
    assert isinstance(out, Failed) and out.kind == "unavailable"
    assert script.requests == []


async def test_image_input_only_for_probed_vision_role() -> None:
    image = ImageInput(data=b"\x89PNG\r\n\x1a\nfake", mime="image/png")
    vision_prompt = REGISTRY.get("document.vision_region").render(region_context={"page": 1}, ocr_text="INV-0O1")

    script = Script(_completion(json.dumps({"transcription": "INV-001", "legible": True, "uncertain_spans": []})))
    gw, _, _ = _gateway(script)
    out = await gw.image_generate(
        gw.binding_for(ModelRole.VISION), [vision_prompt], image, "req", result_type=VisionTranscription
    )
    assert isinstance(out, Succeeded) and out.value.transcription == "INV-001"
    content = json.loads(script.requests[0].content)["messages"][-1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    # 用视觉 Prompt 配抽取绑定属于编程错误：立即抛出，不发请求
    no_img = Script()
    gw2, _, _ = _gateway(no_img)
    with pytest.raises(ValueError):
        await gw2.image_generate(gw2.binding_for(ModelRole.EXTRACT), [vision_prompt], image, "req", result_type=VisionTranscription)
    assert no_img.requests == []


async def test_image_refused_when_vision_model_unprobed() -> None:
    script = Script()
    gw, _, _ = _gateway(script, _settings(vision_model_name="unprobed-vision"))
    prompt = REGISTRY.get("document.vision_region").render(region_context={}, ocr_text="")
    out = await gw.image_generate(
        gw.binding_for(ModelRole.VISION), [prompt], ImageInput(data=b"x", mime="image/png"), "req",
        result_type=VisionTranscription,
    )
    assert isinstance(out, Failed) and out.kind == "unavailable"
    assert script.requests == []


async def test_prompt_schema_must_match_result_type() -> None:
    gw, _, _ = _gateway(Script())
    with pytest.raises(ValueError):
        await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], TranslationResult, "req")


# ---- 预算与并发 ----


async def test_budget_counts_repairs() -> None:
    script = Script(_completion("not json"), _completion(json.dumps(VALID_EXTRACTION)))
    gw, _, _ = _gateway(script)
    budget = CallBudget(max_calls=1)
    out = await gw.structured_generate(
        gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req", budget=budget
    )
    assert isinstance(out, Failed) and out.kind == "budget_exhausted"
    assert len(script.requests) == 1


async def test_concurrency_limited_per_role() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, json=_completion(json.dumps(VALID_EXTRACTION)))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(extract_model_max_concurrency=2)
    gw = DefaultModelGateway(settings, adapter=OpenAICompatibleAdapter(client))
    binding = gw.binding_for(ModelRole.EXTRACT)
    results = await asyncio.gather(
        *(gw.structured_generate(binding, [_extract_prompt()], ExtractionResult, f"req-{i}") for i in range(6))
    )
    assert all(isinstance(r, Succeeded) for r in results)
    assert peak == 2


async def test_user_material_never_promoted_to_system() -> None:
    script = Script(_completion(json.dumps(VALID_EXTRACTION)))
    gw, _, _ = _gateway(script)
    prompt = _extract_prompt()
    result = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [prompt, prompt], ExtractionResult, "req")
    assert isinstance(result, Succeeded)
    assert [m["role"] for m in json.loads(script.requests[0].content)["messages"]] == ["user", "user"]
    await gw.aclose()
    await gw.aclose()
    assert isinstance(await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [prompt], ExtractionResult, "req"), Failed)


async def test_missing_usage_consumes_conservative_token_budget() -> None:
    response = _completion("not json")
    response.pop("usage")
    script = Script(response)
    gw, _, _ = _gateway(script)
    budget = CallBudget(max_total_tokens=12000)
    result = await gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req", budget=budget)
    assert isinstance(result, Failed) and result.kind == "budget_exhausted"
    assert budget.tokens_used >= 8000 and len(script.requests) == 1


async def test_cancelled_in_flight_call_is_recorded_for_reconciliation() -> None:
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    gw = DefaultModelGateway(_settings(), adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler))), ledger=InMemoryCallLedger())
    task = asyncio.create_task(gw.structured_generate(gw.binding_for(ModelRole.EXTRACT), [_extract_prompt()], ExtractionResult, "req"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gw._ledger.records[-1].outcome == "needs_reconcile"
    await gw.aclose()


async def test_ledger_failure_stops_repair_and_requires_reconciliation():
    class BrokenLedger:
        def append(self,record):raise RuntimeError("db failure")
    script=Script(_completion("not json"))
    gateway=DefaultModelGateway(_settings(),adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(script))),ledger=BrokenLedger())
    with pytest.raises(CallLedgerWriteError) as exc:
        await gateway.structured_generate(gateway.binding_for(ModelRole.EXTRACT),[_extract_prompt()],ExtractionResult,"req")
    assert exc.value.unknown and len(script.requests)==1
    await gateway.aclose()
