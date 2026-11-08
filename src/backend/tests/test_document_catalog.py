from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.catalog_checks import evaluate_catalog_checks
from app.documents.extraction import field_label, field_schema
from app.documents.preprocessing import split_document_segments
from app.documents.type_catalog import cargo_candidates, detect_document_type, document_type_choices, normalize_cargo_profiles
from app.documents import evidence_models as em
from app.workflows.models import Run, RunEvent, ExecutionEpoch, RunStatus
from tests.test_workflow_response_contracts import http_db, seed_source
from tests.test_review_field_gate import seed_fields


def test_specific_titles_and_compatible_split_are_preserved():
    assert detect_document_type("PROFORMA INVOICE") == "proforma_invoice"
    assert detect_document_type("PHYTOSANITARY CERTIFICATE FOR RE-EXPORT") == "phytosanitary_reexport"
    assert detect_document_type("This invoice refers to a packing list") == "unknown"
    groups = split_document_segments([SimpleNamespace(kind="title", text="AIR WAYBILL"), SimpleNamespace(kind="paragraph", text="AWB No: 123456")])
    assert groups[0]["type_candidate"] == "supporting"
    assert groups[0]["document_type"] == "air_waybill"


def test_contact_labels_and_selected_profile_change_allowed_extraction():
    assert field_label("address", "seller") == "卖方地址"
    assert field_label("address", "buyer") == "买方地址"
    assert field_label("tax_no", "consignee") == "收货人税号"
    basic = {(field["field_key"], field["role"]) for field in field_schema("commercial_invoice")["fields"]}
    specialized = {(field["field_key"], field["role"]) for field in field_schema("commercial_invoice", ["cold_chain"])["fields"]}
    assert ("temperature_min", "transport") not in basic
    assert ("temperature_min", "transport") in specialized
    assert ("air_waybill_no", "document") in {(field["field_key"], field["role"]) for field in field_schema("air_waybill")["fields"]}


def test_commodity_candidates_require_real_terms_not_payment_currency():
    assert not cargo_candidates([{"id":"s1", "text":"Lead time 12 days. Currency: USD"}])
    candidates = cargo_candidates([{"id":"s2", "text":"Frozen vegetables packed in cartons"}])
    assert {item["code"] for item in candidates} == {"vegetable", "cold_chain"}
    assert all(item["source_segment_ids"] == ["s2"] for item in candidates)
    assert normalize_cargo_profiles(["cold_chain", "cold_chain"]) == ["cold_chain"]


def test_type_choices_include_generic_and_source_sections():
    options = document_type_choices(["section1"])
    assert len(options) == 35
    assert {"air_waybill", "currency_declaration", "generic_document", "dual_use_license"} <= {item["option_id"] for item in options}
    assert all(item["section_ids"] == ["section1"] for item in options)


def test_source_label_without_extraction_is_warning_not_invented_required_field():
    documents = [{"logical_document_id": "ld", "document_type": "air_waybill"}]
    sources = [{"id": "s1", "logical_document_id": "ld", "text": "AWB No: 123456"}]
    gaps = evaluate_catalog_checks(logical_documents=documents, fields=[], source_rows=sources)
    assert len(gaps) == 1
    assert gaps[0].severity == "yellow" and not gaps[0].blocks_export
    assert gaps[0].source_segment_ids == ("s1",)
    absent = evaluate_catalog_checks(logical_documents=documents, fields=[], source_rows=[{**sources[0], "text": "Goods: cotton towels"}])
    assert absent[0].status == "passed"


def test_split_table_label_requires_real_adjacent_value():
    documents=[{"logical_document_id":"ld","document_type":"air_waybill"}]
    label={"id":"s1","logical_document_id":"ld","text":"AWB No","structure":"table_cell","section_id":"section1","table_row":2,"table_col":0}
    value={**label,"id":"s2","text":"123456","table_col":1}
    gaps=evaluate_catalog_checks(logical_documents=documents,fields=[],source_rows=[label,value])
    assert gaps[0].source_segment_ids == ("s1","s2")
    unrelated=evaluate_catalog_checks(logical_documents=documents,fields=[],source_rows=[label,{**value,"section_id":"section2"}])
    assert unrelated[0].status == "passed"


@pytest.mark.asyncio
async def test_prestart_type_choice_persists_freezes_and_prunes(http_db):
    http, conn, principal, created = http_db
    case_id, document_id = await seed_source(conn, principal, created)
    source = {"document_id": document_id, "document_version": 1}
    choice = {**source, "document_type": "air_waybill", "cargo_profile_ids": ["vegetable", "cold_chain"]}
    for workflow in ("read_index", "translate_review"):
        result = await http.post("/api/v1/task-drafts", headers={"Idempotency-Key": uuid4().hex}, json={
            "case_id": case_id, "workflow_type": workflow, "source_versions": [source],
            "draft_parameters": {"target_language": "zh-CN"} if workflow == "translate_review" else {}})
        assert result.status_code == 200, result.text
        draft = result.json()
        path = f"/api/v1/task-drafts/{draft['draft_task_id']}"
        for invalid in ([{**choice, "document_version": 2}], [choice, choice], [{**choice, "document_type": "unknown"}], [{**choice, "cargo_profile_ids": ["invalid"]}]):
            response = await http.patch(path, headers={"Idempotency-Key": uuid4().hex}, json={
                "expected_draft_version": draft["draft_version"], "draft_parameters": {"document_type_inputs": invalid}})
            assert response.status_code in (400, 422), response.text
        response = await http.patch(path, headers={"Idempotency-Key": uuid4().hex}, json={
            "expected_draft_version": draft["draft_version"], "draft_parameters": {"document_type_inputs": [choice]}})
        assert response.status_code == 200, response.text
        draft = response.json()
        assert (await http.get(path)).json()["draft_parameters"]["document_type_inputs"] == [choice]
        started = await http.post("/api/v1/runs", headers={"Idempotency-Key": uuid4().hex}, json={
            "draft_task_id": draft["draft_task_id"], "expected_draft_version": draft["draft_version"]})
        assert started.status_code == 200, started.text
        frozen = (await conn.execute(select(Run.task_spec).where(Run.id == UUID(started.json()["run_id"])))).scalar_one()
        assert frozen["document_type_inputs"] == [choice]
    # 同文件换成新版本后，旧版本声明不跟随迁移。
    from app.config import get_settings
    from app.workflows.commands import patch_draft, prepare_draft
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        draft = await prepare_draft(db, principal, get_settings(), case_id=UUID(case_id), workflow_type="read_index",
            source_versions=[source], draft_parameters={"document_type_inputs": [choice]})
        from app.documents.models import DocumentVersion
        old = await db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == UUID(document_id), DocumentVersion.version == 1))
        db.add(DocumentVersion(tenant_id=principal.tenant_id, case_id=UUID(case_id), document_id=UUID(document_id), version=2,
            filename=old.filename, mime=old.mime, size_bytes=old.size_bytes, sha256=old.sha256, object_id=old.object_id, uploaded_by=principal.user_id))
        await db.flush()
        draft = await patch_draft(db, principal, get_settings(), draft_id=draft.id, expected_draft_version=draft.draft_version,
            source_versions=[{**source, "document_version": 2}])
        assert draft.draft_parameters["document_type_inputs"] == []


@pytest.mark.asyncio
async def test_prestart_type_changes_extraction_and_mixed_file_requires_confirmation(http_db):
    from contextlib import contextmanager
    from app.config import get_settings
    from app.workflows.commands import prepare_draft, start_run
    from app.workflows.worker import DocumentWorker
    from app.workflows.models import InputRequest, InputRequestStatus
    from datetime import UTC, datetime
    http, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        draft = await prepare_draft(db, principal, get_settings(), case_id=UUID(ids["case_id"]), workflow_type="read_index",
            source_versions=[{"document_id": ids["document_id"], "document_version": 1}], draft_parameters={"document_type_inputs": [
                {"document_id": ids["document_id"], "document_version": 1, "document_type": "air_waybill", "cargo_profile_ids": ["cold_chain"]}]})
        run = await start_run(db, principal, get_settings(), draft_task_id=draft.id, expected_draft_version=draft.draft_version, idempotency_key=uuid4().hex)
        def exercise(sync):
            worker = DocumentWorker.__new__(DocumentWorker)
            worker.run_id = run.id; worker.case_id = run.case_id; worker.tenant = principal.tenant_id; worker.purpose = "initial"
            worker.input_business_id = ids["revision_id"]; worker.parse_ids = [ids["parse_id"]]; worker.input_parse_ids = [str(ids["parse_id"])]
            worker.epoch = 1; worker.fence = None
            worker.settings = get_settings()
            @contextmanager
            def transaction():yield sync
            worker.transaction = transaction
            calls = []
            worker.step = lambda name, payload, operation: calls.append(payload) or []
            worker.extract_fields({})
            assert calls[0]["document_type"] == "air_waybill" and worker.assertion_ids == []
            assert "temperature_min" in {value["field_key"] for value in calls[0]["schema"]["fields"]}
            worker.verify_sources({}); sync.flush()
            decision = sync.scalar(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.business_revision_id == run.current_business_revision_id))
            assert decision.logical_document_type == "air_waybill" and decision.cargo_profile_ids == ["cold_chain"]
            logical = sync.get(em.LogicalDocument, ids["logical_id"])
            assert logical.type_candidate == em.LogicalDocumentType.COMMERCIAL_INVOICE
            # 同一文件分为两种单据时，文件级声明不能套在两个部分上。
            other = em.LogicalDocument(tenant_id=principal.tenant_id, case_id=run.case_id, parse_artifact_id=ids["parse_id"], ordinal=1, type_candidate=em.LogicalDocumentType.PACKING_LIST)
            sync.add(other); sync.flush()
            sync.add(em.DocumentSection(tenant_id=principal.tenant_id, case_id=run.case_id, parse_artifact_id=ids["parse_id"], logical_document_id=other.id, ordinal=1, kind="body")); sync.flush()
            state = {"artifact_ids": [str(ids["parse_id"])]}
            assert worker.classify_sections(state)["branch_decision"] == "awaiting_input"
            first = sync.scalar(select(InputRequest).where(InputRequest.run_id == run.id, InputRequest.status == InputRequestStatus.ACTIVE))
            assert first.source_refs == [{"section_id": str(ids["section_id"])}]
            first.status = InputRequestStatus.RESOLVED; first.answer = {"selected_options": [next(option for option in first.options if option["option_id"] == "commercial_invoice")]}; first.resolved_at = datetime.now(UTC); sync.flush()
            assert worker.classify_sections(state)["branch_decision"] == "awaiting_input"
            second = sync.scalar(select(InputRequest).where(InputRequest.run_id == run.id, InputRequest.status == InputRequestStatus.ACTIVE))
            assert second.id != first.id and second.source_refs[0]["section_id"] != str(ids["section_id"])
            sync.flush()
            return second.id
        second_id = await db.run_sync(exercise)
    current = (await http.get(f"/api/v1/runs/{run.id}/input-requests")).json()["items"][0]
    response = await http.post(f"/api/v1/input-requests/{second_id}/resolve", headers={"Idempotency-Key": uuid4().hex}, json={
        "selected_option_ids": ["packing_list"], "expected_request_version": current["request_version"],
        "expected_parse_artifact_id": current["expected_parse_artifact_id"], "expected_business_revision_id": current["expected_business_revision_id"],
        "cargo_profile_ids": ["vegetable"]})
    assert response.status_code == 200, response.text
    business_id = UUID(response.json()["current_business_revision_id"])
    saved = (await conn.execute(select(em.SectionAssignmentDecision.logical_document_type, em.SectionAssignmentDecision.cargo_profile_ids).where(
        em.SectionAssignmentDecision.business_revision_id == business_id))).all()
    assert ("air_waybill", ["cold_chain"]) in saved and ("packing_list", ["vegetable"]) in saved


@pytest.mark.asyncio
async def test_http_type_cargo_revision_roundtrip_and_stale_cas_rejection(http_db,monkeypatch):
    http,conn,principal,created=http_db
    ids=await seed_fields(conn,principal,created)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        run=await db.get(Run,ids["run_id"])
        run.next_event_seq=1
        db.add(RunEvent(tenant_id=principal.tenant_id,run_id=run.id,seq=1,event_type="parse_published",status="awaiting_review",
            message="发布回滚测试解析",payload={"parse_artifact_ids":[str(ids["parse_id"])]}))
        other=em.LogicalDocument(tenant_id=principal.tenant_id,case_id=UUID(ids["case_id"]),parse_artifact_id=ids["parse_id"],ordinal=1,type_candidate=em.LogicalDocumentType.PACKING_LIST)
        db.add(other);await db.flush()
        untouched=em.FieldAssertion(tenant_id=principal.tenant_id,case_id=UUID(ids["case_id"]),parse_artifact_id=ids["parse_id"],logical_document_id=other.id,
            field_key="packing_list_no",role="document",group="identifiers",source_text="PL001",candidate_value="PL001",source_segment_ids=[])
        db.add(untouched);await db.flush()
        db.add(em.BusinessRevisionField(tenant_id=principal.tenant_id,business_revision_id=ids["revision_id"],field_assertion_id=untouched.id))
        untouched_id=untouched.id
        second_source=em.SourceSegment(tenant_id=principal.tenant_id,case_id=UUID(ids["case_id"]),parse_artifact_id=ids["parse_id"],logical_document_id=ids["logical_id"],section_id=ids["section_id"],ordinal=1,text="Goods 5",structure="paragraph")
        db.add(second_source);await db.flush()
        db.add(em.LineItem(tenant_id=principal.tenant_id,case_id=UUID(ids["case_id"]),parse_artifact_id=ids["parse_id"],logical_document_id=ids["logical_id"],line_no=1,description="Goods",quantity=5,
            source_segment_ids=[str(ids["source_id"]),str(second_source.id)]))
        second_source_id=second_source.id
    base=f"/api/v1/cases/{ids['case_id']}"
    body={"doc_type":"air_waybill","cargo_profile_ids":["vegetable","cold_chain"],"reason":"按原件用途确认货物领域",
        "expected_business_revision_id":str(ids["revision_id"]),"expected_business_revision_no":1,"expected_parse_artifact_id":str(ids["parse_id"])}
    response=await http.post(base+f"/sections/{ids['section_id']}/assign",headers={"Idempotency-Key":uuid4().hex},json=body)
    assert response.status_code==200,response.text
    assert response.json()["business_revision_no"]==2
    current=await http.get(base+"/review-view",params={"run_id":str(ids["run_id"])})
    assert current.status_code==200,current.text
    logical=current.json()["files"][0]["logical_documents"][0]
    assert logical["doc_type"]=="air_waybill"
    assert logical["cargo_profile_ids"]==["vegetable","cold_chain"]
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        run=await db.get(Run,ids["run_id"])
        assert run.status==RunStatus.QUEUED and run.current_business_revision_id!=ids["revision_id"]
        assignment=(await db.execute(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.business_revision_id==run.current_business_revision_id))).scalar_one()
        assert assignment.cargo_profile_ids==["vegetable","cold_chain"]
        epoch=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id))).scalar_one()
        assert epoch.purpose=="recompute" and epoch.input_business_revision_id==run.current_business_revision_id
        assert epoch.input_parse_artifact_ids==[str(ids["parse_id"])]
        # 执行生产抽取入口，仅截取Step调度，不进入模型网关、OCR或后台Worker。
        def exercise_extract(sync):
            from contextlib import contextmanager
            from app.workflows.worker import DocumentWorker
            worker=DocumentWorker.__new__(DocumentWorker)
            worker.run_id=run.id;worker.tenant=principal.tenant_id;worker.case_id=UUID(ids["case_id"])
            worker.purpose="recompute";worker.input_business_id=run.current_business_revision_id;worker.parse_ids=[ids["parse_id"]]
            @contextmanager
            def transaction():yield sync
            worker.transaction=transaction
            called=[]
            def step(name,payload,operation):called.append((name,payload));return []
            worker.step=step
            worker.extract_fields({})
            assert len(called)==1 and called[0][1]["logical_id"]==str(ids["logical_id"])
            assert called[0][1]["document_type"]=="air_waybill"
            keys={field["field_key"] for field in called[0][1]["schema"]["fields"]}
            assert {"air_waybill_no","temperature_min","temperature_max"}<=keys
            assert worker.assertion_ids==[str(untouched_id)]
        await db.run_sync(exercise_extract)
        # 新schema执行真实抽取闭包；替身网关返回反序源ID的新分组，既有明细不能被复制。
        from app.infra.model_gateway import Succeeded,CallBudget
        from app.prompts.schemas import ExtractionResult,ExtractedLineItem,ExtractedCell
        from app.prompts.registry import PromptRegistry
        registry=PromptRegistry.load()
        reply=ExtractionResult(fields=[],line_items=[ExtractedLineItem(row_source_segment_ids=[str(second_source_id),str(ids["source_id"])],cells=[ExtractedCell(column_key="quantity",raw_value="5",source_segment_ids=[str(second_source_id)])])])
        class Gateway:
            async def structured_generate(self,*args,**kwargs):return Succeeded(reply,(),False)
            async def aclose(self):pass
        monkeypatch.setattr("app.workflows.worker.build_model_gateway",lambda *args,**kwargs:Gateway())
        def exercise_closed_extraction(sync):
            from contextlib import contextmanager
            from app.workflows.worker import DocumentWorker
            worker=DocumentWorker.__new__(DocumentWorker)
            worker.run_id=run.id;worker.tenant=principal.tenant_id;worker.case_id=UUID(ids["case_id"])
            worker.purpose="recompute";worker.input_business_id=run.current_business_revision_id;worker.parse_ids=[ids["parse_id"]]
            worker.registry=registry;worker.prompt_versions=registry.freeze(["document.extract"])
            worker.bindings=[{"role":"extract","base_url":"https://fixture.invalid/v1","model_name":"fixture","protocol":"openai_compatible","timeout_seconds":30,"max_output_tokens":3000,"max_concurrency":1,"public_config_hash":"0"*64,"adapter_version":"fixture"}]
            worker.settings=SimpleNamespace();worker.factory=lambda:None
            worker.ensure_time=lambda *args:None;worker.submit_intent=lambda *args:None
            worker.model_budget=lambda binding:CallBudget(max_calls=1)
            worker.request_key=lambda step_id:"fixture";worker.complete_step=lambda *args:None;worker.locked=lambda sync:run
            @contextmanager
            def transaction():yield sync
            worker.transaction=transaction;worker.step=lambda name,payload,operation:operation(uuid4())
            def immediate(coro):
                try:coro.send(None)
                except StopIteration as result:return result.value
                raise AssertionError("本验收替身不允许异步外发或等待")
            worker.runner=SimpleNamespace(run=immediate);worker.extract_fields({})
            assert len(list(sync.scalars(select(em.LineItem).where(em.LineItem.logical_document_id==ids["logical_id"]))))==1
        await db.run_sync(exercise_closed_extraction)
    stale=await http.post(base+f"/sections/{ids['section_id']}/assign",headers={"Idempotency-Key":uuid4().hex},json=body)
    assert stale.status_code==409,stale.text
