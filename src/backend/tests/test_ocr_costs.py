"""OCR费用估算与实际隔离：本地/mock及真实PG回滚，零供应商外发。"""
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OCRPrice, Settings
from app.documents.models import DocumentVersion
from app.workflows import commands
from app.workflows.costs import estimate_ocr_request, estimate_workflow_cost, freeze_ocr_binding, workflow_cost_summary
from app.workflows.models import Run, StepExecution, StepStatus
from tests.test_workflow_response_contracts import http_db, seed_source


def quote(**updates):
    values = dict(version="fixture-only-v1", evidence_reference="fixture-not-supplier-price", base_url="https://ocr.invalid/api/v4",
                  model_version="vlm", language="ch", verified=True, billing_basis="all_inclusive_per_page", unit_cny="0.2")
    return OCRPrice(**{**values, **updates})


def configured(**updates):
    return Settings(_env_file=None, mineru_base_url="https://ocr.invalid/api/v4", ocr_prices=[quote()], **updates)


def test_ocr_price_modes_unknown_counts_and_conflicting_quotes():
    binding = freeze_ocr_binding(configured())
    estimate = estimate_ocr_request(binding, pages=3)
    assert Decimal(estimate["estimate_upper_cny"]) == Decimal(".6")
    assert estimate["measurement_kind"] == "estimate" and estimate["actual_cost_cny"] is None
    by_task = freeze_ocr_binding(Settings(_env_file=None, mineru_base_url="https://ocr.invalid/api/v4", ocr_prices=[quote(billing_basis="all_inclusive_per_task", unit_cny="1.5")]))
    assert estimate_ocr_request(by_task, pages=3, tasks=2)["estimate_upper_cny"] == "3.0"
    assert estimate_ocr_request(by_task, pages=None)["estimate_upper_cny"] is None
    for prices in ([quote(verified=False)], [quote(model_version="pipeline")], [quote(language="en")], [quote(), quote(version="conflict")]):
        settings = Settings(_env_file=None, mineru_base_url="https://ocr.invalid/api/v4", ocr_prices=prices)
        assert freeze_ocr_binding(settings)["price"] is None
    with pytest.raises(ValidationError):
        quote(currency="USD")
    with pytest.raises(ValidationError):
        quote(unit_cny="-1")


def test_draft_ocr_bounds_and_real_fee_are_not_interchanged():
    sources = [{"mime":"application/pdf", "page_count":10}, {"mime":"image/png", "page_count":None},
               {"mime":"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "page_count":None}]
    estimate = estimate_workflow_cost(configured(), "read_index", sources, {})
    assert estimate["ocr_estimated_pages_upper"] == 19 and estimate["ocr_estimated_tasks_upper"] == 10
    assert estimate["ocr_estimate_upper_cny"] == "3.8" and not estimate["total_cost_cap_enforced"]
    sources.append({"mime":"application/pdf", "page_count":None})
    assert estimate_workflow_cost(configured(), "read_index", sources, {})["ocr_estimate_upper_cny"] is None
    request = estimate_ocr_request(freeze_ocr_binding(configured()), pages=2)
    steps = [{"step_id":"s", "external_task_id":"task", "ocr_cost":request}]
    summary = workflow_cost_summary([{"call_id":"m", "outcome":"succeeded", "cost_cny":".1"}], steps * 2)
    assert summary["ocr_estimate_upper_cny"] == "0.4" and summary["ocr_task_count"] == 1
    assert summary["actual_ocr_cost_cny"] is None and summary["actual_total_cost_cny"] is None
    assert workflow_cost_summary([], [{"step_id":"old", "external_task_id":"old-task"}])["ocr_estimate_upper_cny"] is None
    assert workflow_cost_summary([], [])["actual_total_cost_cny"] == "0"
    for status, steps, pending, unknown in [("queued", [], 0, 0), ("processing", [{"status":"submitting"}], 1, 0), ("paused_reconcile", [{"status":"needs_reconcile"}], 0, 1)]:
        summary=workflow_cost_summary([],[],model_steps=steps,run_status=status)
        assert summary["actual_model_cost_cny"] is None and summary["actual_total_cost_cny"] is None
        assert summary["pending_model_step_count"] == pending and summary["unknown_model_step_count"] == unknown


def test_worker_saves_cost_before_submit_and_reuses_old_task(monkeypatch):
    import app.workflows.worker as module
    from app.documents.parsing.mineru import MinerUStatus
    steps = {uuid4(): SimpleNamespace(external_task_id=None, usage={})}
    step_id, step = next(iter(steps.items()))
    run = SimpleNamespace(task_spec={"ocr_binding":freeze_ocr_binding(configured())})
    events = []
    class Database:
        def get(self, model, key): return steps[key]
        def scalar(self, query): return 0
    class Worker:
        settings = configured(mineru_api_key="fixture-key")
        run_id = uuid4()
        @contextmanager
        def transaction(self): yield Database()
        def locked(self, db): return run
        def ensure_time(self, seconds): pass
        def submit_intent(self, key, external_id=None):
            if external_id: steps[key].external_task_id = external_id
    class Adapter:
        def __init__(self, settings, **kwargs): self.options=kwargs["options"]
        def submit(self, data, name, on_task_id):
            assert step.usage["ocr_cost"]["estimate_upper_cny"] == "0.4"
            assert step.usage["ocr_cost"]["actual_cost_cny"] is None
            events.append("submit")
            on_task_id("existing-task")
            return "existing-task"
        def get_status(self, task):
            events.append(("status", task, self.options.model_version))
            return MinerUStatus(task, "pending")
        def close(self): pass
    monkeypatch.setattr(module,"MinerUAdapter",Adapter)
    worker=Worker()
    for _ in range(2):
        with pytest.raises(module.YieldExecution):
            module.DocumentWorker.ocr_result(worker,step_id,b"local-only","sample.pdf",page_map=(2,4))
        worker.settings = Settings(_env_file=None, mineru_base_url="https://ocr.invalid/api/v4", mineru_api_key="fixture-key", mineru_model_version="pipeline", ocr_prices=[quote(unit_cny="99")])
    assert events.count("submit") == 1 and all(item[2] == "vlm" for item in events if isinstance(item,tuple))
    assert step.usage["ocr_cost"]["price_snapshot"]["unit_cny"] == "0.2"
    assert step.usage["ocr_cost"]["actual_cost_cny"] is None
    # 历史已受理任务无冻结计价记录，即使环境已有报价也不能回填。
    step.external_task_id="historical-task"
    step.usage={}
    run.task_spec={}
    with pytest.raises(module.YieldExecution):
        module.DocumentWorker.ocr_result(worker,step_id,b"local-only","sample.pdf",page_map=(2,4))
    assert step.usage["ocr_cost"]["price_snapshot"] is None
    assert step.usage["ocr_cost"]["estimate_upper_cny"] is None and events.count("submit") == 1


@pytest.mark.asyncio
async def test_real_pg_start_freezes_ocr_quote_and_http_reports_unknown_actual(http_db):
    http, conn, principal, created = http_db
    case_id, doc_id = await seed_source(conn, principal, created)
    settings = configured(model_adapter="deterministic_fake", parser_adapter="deterministic_fake")
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        source = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id==UUID(doc_id)))).scalar_one()
        source.page_count=6
        draft = await commands.prepare_draft(db,principal,settings,case_id=UUID(case_id),workflow_type="read_index",source_versions=[{"document_id":doc_id,"document_version":1}])
        assert commands.draft_view(draft)["cost_estimate"]["ocr_estimate_upper_cny"] == "1.2"
        run = await commands.start_run(db,principal,settings,draft_task_id=draft.id,expected_draft_version=1,idempotency_key=uuid4().hex)
        run_id=run.id
        frozen=run.task_spec["ocr_binding"]
        assert frozen["price"]["version"] == "fixture-only-v1"
        request=estimate_ocr_request(frozen,pages=2)
        db.add(StepExecution(tenant_id=principal.tenant_id,run_id=run_id,epoch=1,step_name="解析:报价回滚样本",input_fingerprint="0"*64,status=StepStatus.SUBMITTING,external_task_id="fixture-not-provider-task",usage={"ocr_cost":request}))
    response=await http.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200,response.text
    value=response.json()["cost_summary"]
    assert value["ocr_estimate_upper_cny"] == "0.4"
    assert value["actual_ocr_cost_cny"] is None and value["actual_total_cost_cny"] is None
    assert value["unknown_ocr_task_count"] == 1 and not value["total_cost_cap_enforced"]
