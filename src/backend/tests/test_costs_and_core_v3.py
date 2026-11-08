from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import ModelPrice, ModelRole, Settings
from app.infra.model_gateway import fake_binding
from app.review.core_v3 import container_check_digit
from app.review.rules import RULE_PACK_V2, RULE_PACK_V3, evaluate_rules
from app.workflows.costs import ModelCostBudget, estimate_workflow_cost, model_cost_summary, usage_cost


def price(**values):
    return ModelPrice(version="test-price-v1", evidence_reference="test-fixture-only", base_url="https://model.invalid", model_name="fixture", protocol="openai_chat_completions", verified=True,
                      billing_basis="all_inclusive_prompt_completion_tokens", input_cny_per_million="2", output_cny_per_million="10", **values)


def test_complete_usage_prices_reasoning_once_and_unknowns_remain_unknown():
    assert usage_cost(price(), {"prompt_tokens": 1000, "completion_tokens": 500, "reasoning_tokens": 400, "total_tokens": 1500}) == Decimal(".007")
    for usage in [None, {}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}, {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2}]:
        assert usage_cost(price(), usage) is None
    assert usage_cost(None, None, outcome="needs_reconcile") is None
    assert usage_cost(None, None, outcome="not_sent") == 0
    assert usage_cost(None, None, is_fake=True) == 0
    with pytest.raises(ValidationError):
        ModelPrice.model_validate({**price().model_dump(), "currency": "USD"})


def test_frozen_price_and_model_budget_block_unknown_and_excess_cost():
    binding = fake_binding(ModelRole.EXTRACT).model_copy(update={"protocol": "openai_chat_completions", "price": price(), "context_window_tokens": 16000})
    settings = Settings(_env_file=None, run_max_model_cost_cny=".2")
    budget = ModelCostBudget(settings=settings, records=[], binding=binding)
    assert budget.can_start(1000)
    budget.record_cost(Decimal(".05"))
    assert not budget.can_start(1000)
    unknown = ModelCostBudget(settings=settings, records=[{"call_id": "old", "outcome": "succeeded", "usage": {"total_tokens": 1000}}], binding=binding)
    assert not unknown.can_start(1000)
    assert model_cost_summary([{"call_id": "old", "outcome": "succeeded"}])["actual_model_cost_cny"] is None
    assert model_cost_summary([{"call_id": "x", "cost_cny": ".1", "outcome": "succeeded"}] * 2)["actual_model_cost_cny"] == "0.1"
    estimate = estimate_workflow_cost(Settings(_env_file=None), "translate_review", [{"page_count": 3}], {"target_language": "en", "translation_scope": "all"})
    assert estimate["known_pages"] == 3 and estimate["target_language_count"] == 1
    assert estimate["predicted_total_cost_cny"] is None


def test_long_document_budget_increase_preserves_cumulative_usage():
    records = [{"outcome": "succeeded", "usage": {"total_tokens": 6300}} for _ in range(120)]
    binding = fake_binding(ModelRole.TRANSLATE)
    budget = ModelCostBudget(settings=Settings(_env_file=None), records=records, binding=binding)
    assert not budget.can_start(10000)
    configured = Settings(_env_file=None, run_max_model_calls=600, run_max_model_tokens=4000000)
    resumed = ModelCostBudget(settings=configured, records=records, binding=binding)
    assert resumed.calls_used == 120 and resumed.tokens_used == 756000
    assert resumed.can_start(10000)
    resumed.calls_used = 600
    assert not resumed.can_start(10000)
    resumed.calls_used, resumed.tokens_used = 120, 3995000
    assert not resumed.can_start(10000)


@pytest.mark.parametrize("prefix,expected", [("CSQU305438", 3), ("MSCU663987", 0), ("TGHU000000", 8)])
def test_bic_official_calculator_vectors(prefix, expected):
    assert container_check_digit(prefix) == expected


@pytest.mark.parametrize("key,value,status", [("currency", "USD", "passed"), ("currency", "ZZZ", "failed"), ("currency", "XXX", "undetermined"), ("currency", "XTS", "undetermined"), ("currency", "BGN", "undetermined"), ("container_no", "CSQU3054383", "passed"), ("container_no", "CSQU3054384", "failed")])
def test_v3_direct_sources_identity_and_frozen_v2(key, value, status):
    row = {"id": "f", "logical_document_id": "L", "field_key": key, "value": value, "source_segment_ids": ["s"]}
    source = {"id": "s", "logical_document_id": "L", "text": value}
    def evaluate(pack, sources):
        return evaluate_rules(documents=[], fields=[row], lines=[], source_rows=sources, rule_pack=pack)
    rule_id = "field.currency.iso4217" if key == "currency" else "field.container.iso6346_check_digit"
    result = next(rule for rule in evaluate(RULE_PACK_V3, [source]) if rule.rule_id == rule_id)
    assert result.status == status
    assert next(rule for rule in evaluate(RULE_PACK_V3, []) if rule.rule_id == rule_id).status == "undetermined"
    assert not any(rule.rule_id == rule_id for rule in evaluate(RULE_PACK_V2, [source]))


def test_unknown_dates_never_pass_by_calendar_shape():
    rows = [{"id": key, "logical_document_id": "L", "field_key": key, "value": value, "source_segment_ids": [key]} for key, value in [("departure_date", "2026-10-03"), ("arrival_date", "2026-10-02")]]
    sources = [{"id": row["id"], "logical_document_id": "L", "text": row["value"]} for row in rows]
    result = evaluate_rules(documents=[], fields=rows, lines=[], source_rows=sources, rule_pack=RULE_PACK_V3)
    assert next(rule for rule in result if rule.rule_id == "date.transport_sequence").status == "undetermined"


@pytest.mark.asyncio
async def test_gateway_priced_attempts_repair_budget_and_frozen_price():
    import json
    import httpx
    from app.infra.model_gateway import DefaultModelGateway, InMemoryCallLedger, OpenAICompatibleAdapter, Failed, Succeeded
    from app.prompts.schemas import ExtractionResult
    from app.workflows.costs import PricedLedger
    from tests.test_model_gateway import _settings, _extract_prompt, _completion, VALID_EXTRACTION, BASE
    fixture_price = ModelPrice.model_validate({**price().model_dump(), 'base_url': BASE, 'model_name': 'grok-4.6'})
    settings = _settings(model_prices={'extract':fixture_price}, run_max_model_cost_cny='.161')
    binding = DefaultModelGateway(settings).binding_for(ModelRole.EXTRACT)
    budget = ModelCostBudget(settings=settings, records=[], binding=binding)
    ledger = InMemoryCallLedger()
    sent=[]
    def response(request):
        sent.append(request)
        body = _completion('{"invalid":"needs repair"}')
        body['model']='grok-4.6';body['usage']={'prompt_tokens':1000,'completion_tokens':500,'total_tokens':1500}
        return httpx.Response(200,json=body)
    gateway=DefaultModelGateway(settings,adapter=OpenAICompatibleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(response))),ledger=PricedLedger(ledger,binding,budget))
    result=await gateway.structured_generate(binding,[_extract_prompt()],ExtractionResult,'budget-repair',budget=budget)
    await gateway.aclose()
    assert isinstance(result,Failed) and result.kind=='budget_exhausted' and len(sent)==1
    assert ledger.records[0].cost_cny==Decimal('.007')
    assert ledger.records[0].params['price_snapshot']['version']=='test-price-v1'
    assert budget.cost_used==Decimal('.007')
    assert model_cost_summary([ledger.records[0].model_dump(mode='json')])['actual_model_cost_cny']=='0.007'
