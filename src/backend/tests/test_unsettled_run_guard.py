"""真实PG回滚：跨批次未决不能推进，当前已知OCR可只读续跑，部分解析可读。"""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import evidence_models as em
from app.documents.models import Case
from app.management.versions import run_parse_ids
from app.workflows.models import ExecutionEpoch, Run, RunStatus, StepExecution, StepStatus
from app.workflows.translation import TranslationNodes
from app.workflows.worker import DocumentWorker, ModelFailure, digest
from tests.test_review_field_gate import seed_fields
from tests.test_workflow_response_contracts import http_db


def worker_for(db, run, principal):
    worker = DocumentWorker.__new__(DocumentWorker)
    worker.run_id, worker.epoch = run.id, run.current_epoch
    worker.tenant, worker.case_id = principal.tenant_id, run.case_id
    worker.lease, worker.fence = uuid4(), None
    worker.settings = SimpleNamespace(deletion_ledger_path=None, deletion_ledger_key=None)
    worker.spec_hash, worker.prompt_versions, worker.bindings = run.task_spec_hash, {}, []
    worker.ensure_time = lambda *args: None

    @contextmanager
    def transaction():
        yield db

    worker.transaction = transaction
    return worker


def test_interrupted_recovery_requires_closed_matching_attempt_and_remaining_budget():
    from app.infra.recovery import retryable_interrupted_model
    owner=uuid4()
    step=SimpleNamespace(provider_request_key="request",lease_token=owner,external_task_id=None,
        usage={"model_attempt":{"call_id":"call","provider_request_key":"request","lease_token":str(owner),"state":"finished"},
               "model_calls":[{"call_id":"call","provider_request_key":"request","outcome":"interrupted","role":"extract","finished_at":"2026-10-16T00:00:00Z"}]})
    assert retryable_interrupted_model(step,max_attempts=3,attempts_used=1)
    assert not retryable_interrupted_model(step,max_attempts=3,attempts_used=3)
    for key,value in [("state","in_flight"),("call_id","next-call"),("lease_token",str(uuid4()))]:
        previous=step.usage["model_attempt"][key]
        step.usage["model_attempt"][key]=value
        assert not retryable_interrupted_model(step,max_attempts=3,attempts_used=1)
        step.usage["model_attempt"][key]=previous
    step.usage["model_calls"][0]["outcome"]="needs_reconcile"
    assert not retryable_interrupted_model(step,max_attempts=3,attempts_used=1)


@pytest.mark.asyncio
async def test_model_attempt_renews_lease_closes_receipt_and_rejects_stale_owner(http_db):
    from app.infra.model_gateway import StepExecutionCallLedger
    from app.infra.recovery import retryable_interrupted_model
    _,conn,principal,created=http_db
    ids=await seed_fields(conn,principal,created)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        run=await db.get(Run,ids["run_id"])
        run.status=RunStatus.PROCESSING
        owner=uuid4()
        step=StepExecution(tenant_id=principal.tenant_id,run_id=run.id,epoch=run.current_epoch,
            step_name="语义QA批次:休眠",input_fingerprint="8"*64,status=StepStatus.SUBMITTING,
            lease_token=owner,lease_until=datetime.now(UTC)-timedelta(hours=1),provider_request_key="test-model-request")
        db.add(step);await db.flush()
        def check(sync):
            class Scope:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def begin(self):return sync.begin_nested()
                def __getattr__(self,name):return getattr(sync,name)
            ledger=StepExecutionCallLedger(Scope,tenant_id=principal.tenant_id,step_execution_id=step.id,
                lease_token=owner,execution_fence=run.execution_fence)
            ledger.begin_attempt("first","test-model-request",180)
            current=sync.get(StepExecution,step.id)
            assert current.lease_until>datetime.now(UTC)+timedelta(seconds=180)
            assert current.usage["model_attempt"]["state"]=="in_flight"
            with pytest.raises(RuntimeError,match="尚未结束"):
                ledger.begin_attempt("other","test-model-request",180)
            payload={"call_id":"first","provider_request_key":"test-model-request","outcome":"interrupted",
                "role":"extract","finished_at":datetime.now(UTC).isoformat()}
            ledger.append(SimpleNamespace(**payload,model_dump=lambda **_:payload))
            assert retryable_interrupted_model(current,max_attempts=3,attempts_used=1)
            ledger.begin_attempt("second","test-model-request",180)
            assert not retryable_interrupted_model(current,max_attempts=3,attempts_used=1)
            run.execution_fence+=1;sync.flush()
            with pytest.raises(RuntimeError,match="所有者"):
                ledger.before_send()
            sync.execute(text("SET LOCAL ROLE logistics_app"))
        await db.run_sync(check)


@pytest.mark.asyncio
async def test_retry_keeps_frozen_parse_baseline_across_legacy_empty_epoch(http_db):
    _, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        run.status, run.current_epoch = RunStatus.QUEUED, 3
        for number, purpose, parses in [(1, "initial", [str(ids["parse_id"])]), (2, "retry", []), (3, "retry", [])]:
            db.add(ExecutionEpoch(tenant_id=principal.tenant_id, run_id=run.id, epoch=number,
                purpose=purpose, graph_version="translate_review@1", input_parse_artifact_ids=parses, model_bindings=[]))
        await db.flush()
        def load(sync):
            worker = worker_for(sync, sync.get(Run, run.id), principal)
            worker.load_spec({})
            assert worker.input_parse_ids == [str(ids["parse_id"])]
            worker.parse_documents({})
            assert not list(sync.scalars(select(StepExecution).where(StepExecution.run_id == run.id)))
        await db.run_sync(load)


@pytest.mark.asyncio
async def test_model_migration_preserves_adopted_parse_and_does_not_reparse(http_db, monkeypatch):
    from app.api.idempotency import request_digest
    from app.ops import service
    from app.workflows.models import ModelMigration

    http, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    candidate = {"role": "extract", "model_name": "replacement"}

    class Gateway:
        def binding_for(self, role):
            return SimpleNamespace(model_dump=lambda **_: candidate)

        def role_ready(self, *args, **kwargs):
            return True, None

        async def aclose(self):
            pass

    monkeypatch.setattr(service, "build_model_gateway", lambda _: Gateway())
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        run.status, run.current_epoch, run.failure_reason = RunStatus.FAILED_RETRYABLE, 3, "旧模型绑定不一致"
        for number, parses in [(1, []), (2, [str(ids["parse_id"])]), (3, [])]:
            db.add(ExecutionEpoch(tenant_id=principal.tenant_id, run_id=run.id, epoch=number,
                purpose="initial" if number == 1 else "resume_input", graph_version="translate_review@1",
                input_parse_artifact_ids=parses, model_bindings=[{"role": "extract", "model_name": "previous"}]))
        db.add(em.ParseArtifact(tenant_id=principal.tenant_id, case_id=run.case_id,
            document_id=UUID(ids["document_id"]), document_version=1, parser_name="unadopted-newer",
            parser_version="1", parser_config_hash="1" * 64, quality=em.ParseQuality.VALID))
    response = await http.post(f"/api/v1/ops/runs/{ids['run_id']}/migrate-model",
        headers={"Idempotency-Key": uuid4().hex}, json={"candidate_config_fingerprint": request_digest([candidate]),
            "expected_execution_epoch": 3, "reason": "保留用户确认解析后更换模型"})
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True, "execution_epoch": 4}
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        epoch = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id,
            ExecutionEpoch.epoch == 4))).scalar_one()
        assert epoch.input_parse_artifact_ids == [str(ids["parse_id"])] and epoch.model_bindings == [candidate]
        assert run.failure_reason is None and run.current_business_revision_id == ids["revision_id"]
        assert (await db.execute(select(ModelMigration).where(ModelMigration.run_id == run.id))).scalar_one().to_epoch == 4

        def resume(sync):
            worker = worker_for(sync, sync.get(Run, run.id), principal)
            worker.load_spec({})
            worker.parse_documents({})
            assert worker.parse_ids == [ids["parse_id"]]
            assert not list(sync.scalars(select(StepExecution).where(StepExecution.run_id == run.id)))

        await db.run_sync(resume)


@pytest.mark.asyncio
async def test_terminal_run_old_pending_blocks_http_confirm_and_recompute(http_db):
    http, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        run.current_epoch, run.status = 2, RunStatus.INDEXED
        db.add(StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=1,
            step_name="解析:" + ids["document_id"], input_fingerprint="1" * 64,
            status=StepStatus.SUBMITTING, external_task_id="existing-only-no-call", lease_until=datetime.now(UTC) - timedelta(minutes=1)))
    base = f"/api/v1/cases/{ids['case_id']}/fields/{ids['field_id']}"
    body = {"expected_business_revision_id": str(ids["revision_id"]), "expected_parse_artifact_id": str(ids["parse_id"])}
    for action, patch in [("confirm", {}), ("revise", {"value": "002", "reason": "核对原件"})]:
        response = await http.post(base + "/" + action, headers={"Idempotency-Key": uuid4().hex}, json={**body, **patch})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "RECONCILE_REQUIRED"
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        case, run = await db.get(Case, UUID(ids["case_id"])), await db.get(Run, ids["run_id"])
        assert case.current_business_revision_id == ids["revision_id"]
        assert run.status == RunStatus.INDEXED and run.current_epoch == 2
        assert len((await db.execute(select(em.BusinessRevision).where(em.BusinessRevision.case_id == case.id))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_worker_guards_old_epoch_and_all_completion_then_resumes_same_ocr(http_db, monkeypatch):
    _, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        run.status, run.current_epoch = RunStatus.QUEUED, 2
        db.add(ExecutionEpoch(tenant_id=principal.tenant_id, run_id=run.id, epoch=2,
            purpose="recompute", graph_version="read_index@1", input_parse_artifact_ids=[str(ids["parse_id"])], model_bindings=[]))
        step = StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=1,
            step_name="解析:" + ids["document_id"], input_fingerprint="1" * 64,
            status=StepStatus.SUBMITTING, external_task_id="persistent-ocr-only", lease_until=datetime.now(UTC) - timedelta(minutes=1))
        db.add(step)
        await db.flush()

        def old_epoch_checks(sync):
            worker = worker_for(sync, sync.get(Run, run.id), principal)
            for call in [lambda: worker.load_spec({}), lambda: worker.publish_index({}),
                         lambda: TranslationNodes(worker).publish_review({}),
                         lambda: worker.step("另一新步骤", {}, lambda _: pytest.fail("不能执行新步骤"))]:
                with pytest.raises(ModelFailure) as error:
                    call()
                assert error.value.unknown
            assert sync.get(Run, run.id).status == RunStatus.QUEUED

        await db.run_sync(old_epoch_checks)
        step.epoch = 2  # 仅回滚夹具构造正常同批次续查，产品不迁移旧步骤。
        await db.flush()

        def resume(sync):
            worker = worker_for(sync, sync.get(Run, run.id), principal)
            worker.load_spec({})
            item = sync.get(StepExecution, step.id)
            item.input_fingerprint = digest({"task": worker.spec_hash, "step": item.step_name,
                "inputs": {}, "prompts": worker.prompt_versions, "models": worker.bindings})
            sync.flush()
            seen = []
            result = worker.step(item.step_name, {}, lambda step_id: seen.append(step_id) or [ids["parse_id"]])
            assert seen == [step.id] and result == [str(ids["parse_id"])]
            assert item.external_task_id == "persistent-ocr-only" and item.status == StepStatus.SUCCEEDED
            waiting_name = "解析:" + str(uuid4())
            waiting = StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=2,
                step_name=waiting_name,
                input_fingerprint=digest({"task": worker.spec_hash, "step": waiting_name,
                    "inputs": {}, "prompts": worker.prompt_versions, "models": worker.bindings}),
                status=StepStatus.SUBMITTING, external_task_id="second-file-existing-ocr",
                error_class="ocr_poll_pending", usage={"ocr_upload_confirmed": True})
            sync.add(waiting)
            sync.flush()
            assert worker.step(item.step_name, {}, lambda _: pytest.fail("已成功的第一份不应重新解析")) == [str(ids["parse_id"])]
            with pytest.raises(ModelFailure):
                worker.step("新的抽取步骤", {}, lambda _: pytest.fail("未决 OCR 期间不能新发请求"))
            for attribute, bad_value in [("epoch", 1), ("external_task_id", None),
                                         ("usage", {}), ("status", StepStatus.NEEDS_RECONCILE)]:
                original = getattr(waiting, attribute)
                setattr(waiting, attribute, bad_value)
                sync.flush()
                with pytest.raises(ModelFailure):
                    worker.step(item.step_name, {}, lambda _: pytest.fail("不能绕过未知或旧批次请求"))
                setattr(waiting, attribute, original)
                sync.flush()
            resumed = []
            worker.step(waiting_name, {}, lambda step_id: resumed.append(step_id) or [ids["parse_id"]])
            assert resumed == [waiting.id] and waiting.status == StepStatus.SUCCEEDED
            assert waiting.external_task_id == "second-file-existing-ocr"
            # 未发送旧本地步骤及明确失败不应阻止完成门禁。
            sync.add_all([StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=1,
                step_name="本地旧步", input_fingerprint="2" * 64, status=StepStatus.RUNNING),
                StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=1,
                step_name="明确未执行", input_fingerprint="3" * 64, status=StepStatus.FAILED)])
            sync.flush()
            worker.require_settled_steps(sync)

        await db.run_sync(resume)

        def external_preflight(sync):
            from app.infra.model_gateway import StepExecutionCallLedger

            class Scope:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def begin(self):
                    return sync.begin_nested()

                def __getattr__(self, name):
                    return getattr(sync, name)

            current = StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=2,
                step_name="抽取当前", input_fingerprint="4" * 64, status=StepStatus.SUBMITTING)
            sync.add(current)
            sync.flush()
            ledger = StepExecutionCallLedger(Scope, tenant_id=principal.tenant_id, step_execution_id=current.id)
            with monkeypatch.context() as patch:
                patch.setattr("app.config.get_settings", lambda: SimpleNamespace(deletion_ledger_path=None, deletion_ledger_key=None))
                ledger.before_send()
                old = StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=1,
                    step_name="旧抽取未知", input_fingerprint="5" * 64, status=StepStatus.NEEDS_RECONCILE)
                sync.add(old)
                sync.flush()
                with pytest.raises(RuntimeError, match="其他外部请求待核查"):
                    ledger.before_send()
            sync.execute(text("SET LOCAL ROLE logistics_app"))

        await db.run_sync(external_preflight)


@pytest.mark.asyncio
async def test_paused_run_reads_own_successful_parse_not_newer_or_inflight_artifacts(http_db):
    http, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        run.status, run.current_epoch = RunStatus.PAUSED_RECONCILE, 2
        run.current_business_revision_id = None
        db.add(ExecutionEpoch(tenant_id=principal.tenant_id, run_id=run.id, epoch=2,
            purpose="initial", graph_version="read_index@1", input_parse_artifact_ids=[], model_bindings=[]))
        newer = em.ParseArtifact(tenant_id=principal.tenant_id, case_id=run.case_id,
            document_id=UUID(ids["document_id"]), document_version=1, parser_name="fixture-newer",
            parser_version="1", parser_config_hash="1" * 64, quality=em.ParseQuality.VALID)
        db.add(newer)
        await db.flush()
        db.add_all([StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=2,
            step_name="解析:" + ids["document_id"], input_fingerprint="1" * 64,
            status=StepStatus.SUCCEEDED, artifact_ids=[str(ids["parse_id"]), str(uuid4())]),
            StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=1,
            step_name="解析:旧批次", input_fingerprint="2" * 64, status=StepStatus.SUCCEEDED, artifact_ids=[str(newer.id)]),
            StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=2,
            step_name="解析:嵌图:仅对象", input_fingerprint="3" * 64,
            status=StepStatus.SUBMITTING, artifact_ids=[str(newer.id)], external_task_id="do-not-poll")])
        await db.flush()
        assert await db.run_sync(lambda sync: run_parse_ids(sync, sync.get(Run, run.id))) == [ids["parse_id"]]
        from app.exports.gate import review_gate
        checklist = await db.run_sync(lambda sync: review_gate(sync, sync.get(Run, run.id)))
        assert any(gap["item"] == "外部请求" for gap in checklist["gaps"])
    response = await http.get(f"/api/v1/cases/{ids['case_id']}/review-view", params={"run_id": str(ids["run_id"])})
    assert response.status_code == 200, response.text
    node = response.json()["files"][0]
    assert node["parse_status"] == "parsed" and node["parse_artifact_id"] == str(ids["parse_id"])
    assert response.json()["allowed_actions"] == ["open_source"]
    assert "核查" in response.json()["gate_summary"]
    fields = await http.get(f"/api/v1/cases/{ids['case_id']}/fields", params={"run_id": str(ids["run_id"])})
    assert fields.status_code == 200, fields.text
    assert {item["field_id"] for item in fields.json()["items"]} == {str(ids["field_id"]), str(ids["no_source_id"])}


def model_receipt(request_key, *, is_fake=False, outcome="succeeded"):
    return {"call_id":uuid4().hex,"provider_request_key":request_key,"outcome":outcome,
        "is_fake":is_fake,"http_status":200,"finished_at":datetime.now(UTC).isoformat()}


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["success", "missing", "mismatched", "unknown", "ledger_failure"])
async def test_model_receipt_ack_distinguishes_local_failure_and_unknown_acceptance(http_db, proof):
    from sqlalchemy.exc import NoReferencedTableError
    from app.infra.model_gateway import CallLedgerWriteError

    _,conn,principal,created=http_db
    ids=await seed_fields(conn,principal,created)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        def execute(sync):
            worker=worker_for(sync,sync.get(Run,ids["run_id"]),principal)
            step_ids=[]

            def local_work(step_id):
                step_ids.append(step_id)
                worker.submit_intent(step_id)
                step=sync.get(StepExecution,step_id)
                receipt=model_receipt(step.provider_request_key)
                if proof=="ledger_failure":raise CallLedgerWriteError("响应账本写入失败")
                if proof=="mismatched":receipt["provider_request_key"]="another-request"
                if proof=="unknown":receipt["outcome"]="needs_reconcile"
                step.usage={"model_calls":[] if proof=="missing" else [receipt]}
                sync.flush()
                result=SimpleNamespace(records=[SimpleNamespace(call_id=receipt["call_id"],outcome="succeeded")])
                worker.record_model_response(step_id,result)
                assert step.status is StepStatus.RUNNING
                assert not step.artifact_ids
                raise NoReferencedTableError("本地元数据未注册","test_missing_metadata")

            with pytest.raises(NoReferencedTableError if proof=="success" else (ModelFailure,CallLedgerWriteError)):
                worker.step("局部修订:回执核查",{},local_work)
            step=sync.get(StepExecution,step_ids[0])
            assert step.status is (StepStatus.FAILED if proof=="success" else StepStatus.NEEDS_RECONCILE)
            assert not step.artifact_ids
            if proof=="success":
                assert step.error_class=="NoReferencedTableError"
                assert step.usage["model_response"]["call_ids"]==[step.usage["model_calls"][0]["call_id"]]
                worker.require_settled_steps(sync)
            else:
                with pytest.raises(ModelFailure):worker.require_settled_steps(sync)

        await db.run_sync(execute)


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["success", "fake", "unknown", "wrong_local_error"])
async def test_ops_known_response_local_failure_is_audited_and_preserves_review_subgraph(http_db, proof):
    from app.workflows.models import StepReconciliation

    http,conn,principal,created=http_db
    ids=await seed_fields(conn,principal,created)
    repair_request={"issue_id":str(ids["issues"][0]),"user_instruction":"请核对数量","return_status":"awaiting_review"}
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        run=await db.get(Run,ids["run_id"])
        run.status=RunStatus.PAUSED_RECONCILE
        db.add(ExecutionEpoch(tenant_id=principal.tenant_id,run_id=run.id,epoch=run.current_epoch,
            purpose="issue_repair",graph_version="translate_review@1",input_parse_artifact_ids=[str(ids["parse_id"])],
            model_bindings=[],repair_request=repair_request))
        key="step:"+uuid4().hex
        step=StepExecution(tenant_id=principal.tenant_id,run_id=run.id,epoch=run.current_epoch,
            step_name="局部修订:回执核查",input_fingerprint="9"*64,status=StepStatus.NEEDS_RECONCILE,
            provider_request_key=key,error_class="RuntimeError" if proof=="wrong_local_error" else "NoReferencedTableError",
            usage={"model_calls":[model_receipt(key,is_fake=proof=="fake",outcome="needs_reconcile" if proof=="unknown" else "succeeded")]})
        db.add(step);await db.flush();step_id=step.id
    endpoint=f"/api/v1/ops/steps/{step_id}"
    viewed=await http.get(endpoint)
    assert viewed.status_code==200,viewed.text
    assert ("known_response_local_failure" in viewed.json()["allowed_decisions"])==(proof=="success")
    response=await http.post(endpoint+"/reconcile",headers={"Idempotency-Key":uuid4().hex},json={
        "expected_step_version":1,"decision":"known_response_local_failure",
        "evidence_reference":"已持久化成功回执及本地元数据异常核查记录","reason":"确认模型响应成功，本地元数据错误已修复"})
    assert response.status_code==(200 if proof=="success" else 409),response.text
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        step=await db.get(StepExecution,step_id);run=await db.get(Run,ids["run_id"])
        audits=(await db.execute(select(StepReconciliation).where(StepReconciliation.step_execution_id==step_id))).scalars().all()
        assert not step.artifact_ids
        if proof=="success":
            assert step.status is StepStatus.FAILED and step.error_class=="NoReferencedTableError"
            assert len(audits)==1 and audits[0].decision=="known_response_local_failure"
            epoch=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id,ExecutionEpoch.epoch==run.current_epoch))).scalar_one()
            assert run.status is RunStatus.QUEUED and run.current_epoch==2
            assert epoch.purpose=="issue_repair" and epoch.repair_request==repair_request
            assert epoch.input_parse_artifact_ids==[str(ids["parse_id"])]
        else:
            assert step.status is StepStatus.NEEDS_RECONCILE and not audits
            assert run.status is RunStatus.PAUSED_RECONCILE and run.current_epoch==1
