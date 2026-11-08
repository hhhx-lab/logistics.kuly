"""真实 PostgreSQL + 生产 HTTP 路由，外层回滚，不调用 OCR/模型/对象存储。"""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.exceptions import ResponseValidationError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import current_principal, get_db, write_principal
from app.api.main import create_app
from app.config import get_settings
from app.documents.models import Case, DocumentVersion, SourceDocument, StoredObject, StoredObjectCategory
from app.identity.models import User
from app.identity.service import Principal
from app.infra.database import create_database_engine
from app.workflows import commands
from app.workflows.models import Run, TaskDraft


def test_comparison_event_payloads_are_published_without_relaxing_validation():
    from pydantic import ValidationError
    from app.api.response_dto import RunEventPayloadView

    uid = str(uuid4())
    payloads = [
        {"batch_id": uid, "business_revision_id": uid, "parse_artifact_ids": [uid],
         "red": 0, "yellow": 1, "unknown": 2, "files": 3, "checks": 6,
         "pending_relations": 0, "rules_status": "completed"},
        {"batch_id": uid, "reason": "incompatible", "required_action": "new_run"},
        {"recompute_run_id": uid, "scope_revision_id": uid,
         "previous_comparison_binding_id": None},
        {"business_revision_id": uid, "parse_artifact_ids": [uid]},
        {"issue_id": uid, "agent_request_no": 1},
    ]
    for payload in payloads:
        assert RunEventPayloadView.model_validate(payload).model_dump(mode="json", exclude_unset=True) == payload
    assert RunEventPayloadView.model_validate({"step_id":uid}).model_dump(mode="json",exclude_unset=True)=={"step_execution_id":uid}
    with pytest.raises(ValidationError):
        RunEventPayloadView.model_validate({"unpublished_configuration": "hidden"})


def test_run_failure_explains_current_receipt_without_reusing_old_epoch():
    from types import SimpleNamespace
    from app.workflows.commands import public_run_failure
    from app.workflows.models import RunStatus, StepStatus

    now = datetime.now(UTC)
    run = SimpleNamespace(status=RunStatus.PAUSED_RECONCILE, current_epoch=3, failure_reason='Current local failure')
    current = SimpleNamespace(epoch=3, status=StepStatus.NEEDS_RECONCILE, created_at=now,
        usage={'model_calls':[{'outcome':'needs_reconcile','http_status':524,'provider_model_id':'private-value'}]})
    old = SimpleNamespace(epoch=2, status=StepStatus.FAILED, created_at=now + timedelta(seconds=1),
        usage={'model_calls':[{'outcome':'permanent_error','http_status':401}]})
    reason = public_run_failure(run, [old, current])
    assert 'HTTP 524' in reason and 'private-value' not in reason and 'authentication' not in reason
    current.usage['model_calls'][0]['http_status'] = 503
    assert 'HTTP 503' in public_run_failure(run, [current])
    current.usage['model_calls'].append({'outcome':'succeeded','http_status':200})
    assert public_run_failure(run, [current]) == run.failure_reason
    assert public_run_failure(run, [old]) == run.failure_reason
    current.usage['model_calls'] = [{'outcome':'needs_reconcile','http_status':None}]
    assert 'interrupted' in public_run_failure(run, [current])
    current.error_class = 'binding_mismatch'
    assert 'controlled model migration' in public_run_failure(run, [current])
    current.error_class = 'budget_exhausted'
    current.usage = {'model_calls': []}
    reason = public_run_failure(run, [current])
    assert 'limit before the next request' in reason and 'Increase the processing budget' in reason
    assert 'preserved' in reason and 'model service' not in reason
    current.error_class = 'expired_lease_acceptance_unknown'
    current.usage = {'model_calls':[{'outcome':'interrupted','http_status':None}]}
    assert 'expired task lease' in public_run_failure(run, [current])
    run.status = RunStatus.PROCESSING
    assert public_run_failure(run, [current]) is None


@pytest.fixture
async def http_db():
    settings = get_settings()
    engine = create_database_engine(settings)
    async with engine.connect() as conn:
        outer = await conn.begin()
        user = (await conn.execute(select(User.id, User.tenant_id, User.display_name, User.permissions)
            .where(User.account == "demo"))).mappings().one()
        principal = Principal(user["id"], user["tenant_id"], user["display_name"], tuple(user["permissions"]), uuid4(), "unused")
        await conn.execute(text(f"SET LOCAL app.tenant_id = '{principal.tenant_id}'"))
        await conn.execute(text("SET LOCAL ROLE logistics_app"))
        app = create_app()

        async def session():
            async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db:
                yield db

        app.dependency_overrides[get_db] = session
        app.dependency_overrides[current_principal] = lambda: principal
        app.dependency_overrides[write_principal] = lambda: principal
        created = []
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
                yield http, conn, principal, created
        finally:
            await outer.rollback()
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL app.tenant_id = '{principal.tenant_id}'"))
                for model in (Case, SourceDocument, DocumentVersion, StoredObject, TaskDraft, Run):
                    key = model.id if model is Case else model.case_id
                    assert (await conn.execute(select(func.count()).select_from(model).where(key.in_(created)))).scalar_one() == 0
    await engine.dispose()


async def seed_source(conn, principal, created):
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db:
        async with db.begin():
            now = datetime.now(UTC)
            case = Case(tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
                name="响应契约回滚样本", short_code=uuid4().hex[:16],
                last_material_event_at=now, retention_expires_at=now + timedelta(days=30))
            db.add(case)
            await db.flush()
            created.append(case.id)
            source = SourceDocument(tenant_id=principal.tenant_id, case_id=case.id, current_version=1)
            obj = StoredObject(tenant_id=principal.tenant_id, case_id=case.id,
                object_key=f"contract-fixture/{uuid4()}", category=StoredObjectCategory.SOURCE)
            db.add_all([source, obj])
            await db.flush()
            db.add(DocumentVersion(tenant_id=principal.tenant_id, case_id=case.id, document_id=source.id,
                version=1, filename="真实元数据_响应契约.pdf", mime="application/pdf", size_bytes=128,
                sha256="0" * 64, object_id=obj.id, uploaded_by=principal.user_id))
            await db.flush()
            return str(case.id), str(source.id)


@pytest.mark.asyncio
async def test_real_read_response_and_http_validation(http_db, monkeypatch):
    http, conn, p, _ = http_db
    now = datetime.now(UTC)
    run_id = (await conn.execute(select(Run.id).join(Case, Run.case_id == Case.id).where(
        Case.owner_user_id == p.user_id, Case.retention_expires_at > now,
        Case.status == "active").order_by(Run.updated_at.desc()).limit(1))).scalar_one()
    captured = []
    original = commands.run_view

    async def capture(*args, **kwargs):
        value = await original(*args, **kwargs)
        captured.append(value)
        return value

    monkeypatch.setattr(commands, "run_view", capture)
    response = await http.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    value = response.json()
    assert set(value) == set(captured[-1])  # 包含规则状态、运营提示等所有实际字段。
    assert value["run_id"] == str(run_id)
    assert len(value["source_versions"]) == len(captured[-1]["source_versions"]) > 0
    assert set(value["source_versions"][0]) == set(captured[-1]["source_versions"][0])
    assert value["source_versions"][0]["filename"] == captured[-1]["source_versions"][0]["filename"]
    assert value["latest_export_attempt_id"] == captured[-1]["latest_export_attempt_id"]

    async def missing_required(*args, **kwargs):
        value = await original(*args, **kwargs)
        value.pop("task_spec_hash")
        return value

    monkeypatch.setattr(commands, "run_view", missing_required)
    with pytest.raises(ResponseValidationError) as error:
        await http.get(f"/api/v1/runs/{run_id}")
    assert any(e["type"] == "missing" and e["loc"] == ("response", "task_spec_hash") for e in error.value.errors())

    async def unexpected(*args, **kwargs):
        value = await original(*args, **kwargs)
        value["unpublished_extension"] = True
        return value

    monkeypatch.setattr(commands, "run_view", unexpected)
    with pytest.raises(ResponseValidationError) as error:
        await http.get(f"/api/v1/runs/{run_id}")
    assert any(e["type"] == "extra_forbidden" for e in error.value.errors())


@pytest.mark.asyncio
async def test_workflow_list_and_postgres_draft_create_patch(http_db, monkeypatch):
    http, conn, p, created = http_db
    workflows = await http.get("/api/v1/workflows")
    assert workflows.status_code == 200, workflows.text
    actual = commands.workflows_view(get_settings())
    assert workflows.json() == {"items": actual}
    case_id, document_id = await seed_source(conn, p, created)
    captured = []
    original = commands.draft_view

    def capture(draft):
        value = original(draft)
        captured.append(value)
        return value

    monkeypatch.setattr(commands, "draft_view", capture)
    for workflow_type, parameters in [("read_index", {"index_scope": "selected_documents"}),
        ("translate_review", {"target_language": "zh-CN"})]:
        response = await http.post("/api/v1/task-drafts", headers={"Idempotency-Key": uuid4().hex}, json={
            "case_id": case_id, "workflow_type": workflow_type,
            "source_versions": [{"document_id": document_id, "document_version": 1}], "draft_parameters": parameters})
        assert response.status_code == 200, response.text
        draft = response.json()
        assert set(draft) == set(captured[-1])
        assert draft["draft_parameters"] == captured[-1]["draft_parameters"]
        assert draft["source_versions"][0]["filename"] == "真实元数据_响应契约.pdf"
        assert draft["started_run_id"] is None
        draft_id = UUID(draft["draft_task_id"])
        assert (await conn.execute(select(TaskDraft.id).where(TaskDraft.id == draft_id))).scalar_one() == draft_id
        read = await http.get(f"/api/v1/task-drafts/{draft_id}")
        assert read.json() == draft
        patch = await http.patch(f"/api/v1/task-drafts/{draft_id}", headers={"Idempotency-Key": uuid4().hex}, json={
            "expected_draft_version": draft["draft_version"], "draft_parameters": parameters})
        assert patch.status_code == 200, patch.text
        assert patch.json()["draft_version"] == draft["draft_version"] + 1
        assert patch.json()["draft_parameters"] == captured[-1]["draft_parameters"]
        # 仅冻结配置并写入未提交队列；外层回滚前没有 Worker 能读取本 Run。
        assert patch.json()["status"] == "ready", "本机工作流配置须就绪才能验证真实启动"
        key = uuid4().hex
        body = {"draft_task_id": str(draft_id), "expected_draft_version": patch.json()["draft_version"]}
        started = await http.post("/api/v1/runs", headers={"Idempotency-Key": key}, json=body)
        assert started.status_code == 200, started.text
        run = started.json()
        assert run["case_id"] == case_id and run["status"] == "queued"
        assert run["source_versions"] == draft["source_versions"]
        replay = await http.post("/api/v1/runs", headers={"Idempotency-Key": key}, json=body)
        assert replay.json() == run
        cancelled = await http.post(f"/api/v1/runs/{run['run_id']}/cancel", headers={"Idempotency-Key": uuid4().hex})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"


def test_issue_agent_guidance_contract_accepts_published_evidence_and_rejects_extra():
    from app.api.response_dto import IssueGuidanceView
    from pydantic import ValidationError
    payload={"skill_version":"consignment-review@1", "category_label":"conflict", "explanation":"100/98",
        "impact":"需要核对", "recommendation":"核对原件", "target_kind":"material", "can_apply":False,
        "agent_source_segment_ids":[str(uuid4())], "recommended_action":"human-review"}
    assert IssueGuidanceView.model_validate(payload).recommended_action == "human-review"
    with pytest.raises(ValidationError):
        IssueGuidanceView.model_validate({**payload,"unpublished_secret":"rejected"})
