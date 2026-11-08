"""候选材料、业务完整性和专业审定边界；HTTP验证真实PG且全部回滚。"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.review.jurisdiction import MATERIAL_PACK, evaluate_consumer_materials, published_rule_packs
from app.review.rules import RULE_PACK_V3, evaluate_rules
from app.workflows.commands import normalize_parameters
from tests.test_workflow_response_contracts import http_db, seed_source
from tests.test_review_field_gate import seed_fields


def material_fixture():
    rows = [{"id": kind, "text": title, "document_id": str(uuid4()), "document_version": 1}
        for kind, title in [("commercial_invoice", "COMMERCIAL INVOICE"), ("packing_list", "PACKING LIST"), ("transport_document", "BILL OF LADING")]]
    inputs = {"kind": "consumer_material_check", "dispatch_country": "CN", "destination_country": "DE",
        "eu_customs_territory": True, "general_consumer_goods": True, "non_dangerous": True,
        "declaration_reason": "用户按本票委托与货物说明确认；仅测试输入", "material_set_complete": True,
        "materials": [{"kind": row["id"], "source": {"document_id": row["document_id"], "document_version": 1},
                       "same_consignment": True, "basis": "委托单据票号TEST-001已人工核对"} for row in rows]}
    documents = [{"id": "parse", "segment_count": 3, "page_count": 1, "seen_pages": [1], "quality_flags": [], "is_synthetic": False}]
    return inputs, rows, documents


def test_provided_materials_have_sources_and_never_claim_legal_clearance():
    inputs, rows, documents = material_fixture()
    results = evaluate_consumer_materials(inputs, rows, documents)
    assert len(results) == 3
    assert all(result.status == "passed" and not result.blocks_export for result in results)
    assert {ref for result in results for ref in result.source_segment_ids} == {row["id"] for row in rows}
    for result in results:
        assert result.calculation["legal_review_status"] == "pending"
        assert result.calculation["customs_compliance"] == "not_assessed"
        assert result.calculation["source_review_window"]["is_legal_validity"] is False
    pack = next(pack for pack in published_rule_packs() if pack["version"] == MATERIAL_PACK)
    assert pack["status"] == "trial_material_check" and pack["legal_review_status"] == "pending"


def test_missing_defaults_to_nonblocking_unless_explicit_business_requirement():
    inputs, rows, documents = material_fixture()
    inputs["materials"] = inputs["materials"][:1]
    default = evaluate_consumer_materials(inputs, rows[:1], documents)
    assert [result.status for result in default] == ["passed", "undetermined", "undetermined"]
    assert not any(result.blocks_export for result in default)
    inputs["required_materials"] = ["packing_list"]
    assert not any(result.blocks_export for result in evaluate_consumer_materials(inputs, rows[:1], documents))
    inputs["required_materials_basis"] = "本票客户委托明确要求装箱单"
    required = evaluate_consumer_materials(inputs, rows[:1], documents)
    assert required[1].status == "failed" and required[1].blocks_export
    assert required[2].status == "undetermined" and not required[2].blocks_export
    for change in [{"page_count": None}, {"page_count": 2}, {"quality_flags": ["ocr_unavailable"]}, {"is_synthetic": True}]:
        assert not any(result.blocks_export for result in evaluate_consumer_materials(inputs, rows[:1], [{**documents[0], **change}]))
    assert not any(result.blocks_export for result in evaluate_consumer_materials({**inputs, "material_set_complete": False}, rows[:1], documents))


def test_unconfirmed_title_or_cross_version_cannot_pass_and_uncovered_stays_explicit():
    inputs, rows, documents = material_fixture()
    for patch in [{"same_consignment": None}, {"basis": ""}, {"source": {"document_id": rows[0]["document_id"], "document_version": 2}}]:
        changed = deepcopy(inputs)
        changed["materials"][0].update(patch)
        assert evaluate_consumer_materials(changed, rows, documents)[0].status == "undetermined"
    changed_rows = deepcopy(rows)
    changed_rows[0]["text"] = "No commercial invoice provided"
    assert evaluate_consumer_materials(inputs, changed_rows, documents)[0].status == "undetermined"
    for patch in [{"dispatch_country": "US"}, {"destination_country": "US"}, {"non_dangerous": False}, {"general_consumer_goods": False}, {"eu_customs_territory": False}]:
        assert evaluate_consumer_materials({**inputs, **patch}, rows, documents)[0].status == "not_covered"
    assert evaluate_consumer_materials(None, rows, documents)[0].status == "undetermined"


def test_material_pack_extends_frozen_core_v3_and_requires_matching_parameters():
    inputs, rows, documents = material_fixture()
    core = evaluate_rules(documents=documents, fields=[], lines=[], rule_pack=RULE_PACK_V3, source_rows=rows)
    extended = evaluate_rules(documents=documents, fields=[], lines=[], rule_pack=MATERIAL_PACK, source_rows=rows, jurisdiction_inputs=inputs)
    assert [result for result in extended if not result.rule_id.startswith("jurisdiction.consumer_materials")] == core
    normalized, missing, _ = normalize_parameters("translate_review", {"target_language": "en", "rule_pack_version": MATERIAL_PACK})
    assert not missing and "jurisdiction_inputs" not in normalized
    with pytest.raises(Exception):
        normalize_parameters("translate_review", {"rule_pack_version": MATERIAL_PACK, "jurisdiction_inputs": {"commodity": "fresh_apples"}})
    with pytest.raises(Exception):
        normalize_parameters("translate_review", {"jurisdiction_inputs": inputs})


@pytest.mark.asyncio
async def test_http_draft_freezes_inputs_and_rejects_unselected_material_version(http_db):
    http, conn, principal, created = http_db
    case_id, document_id = await seed_source(conn, principal, created)
    inputs, _, _ = material_fixture()
    inputs["materials"] = [{"kind": "commercial_invoice", "source": {"document_id": document_id, "document_version": 1}, "same_consignment": True, "basis": "本票委托已人工确认"}]
    request = {"case_id": case_id, "workflow_type": "translate_review", "source_versions": [{"document_id": document_id, "document_version": 1}],
               "draft_parameters": {"target_language": "en", "rule_pack_version": MATERIAL_PACK, "jurisdiction_inputs": inputs}}
    response = await http.post("/api/v1/task-drafts", headers={"Idempotency-Key": uuid4().hex}, json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["draft_parameters"]["jurisdiction_inputs"]["materials"][0]["source"]["document_id"] == document_id
    assert result["draft_parameters"]["jurisdiction_inputs"]["kind"] == "consumer_material_check"
    assert result["draft_parameters"]["rule_pack_version"] == MATERIAL_PACK
    inputs["materials"][0]["source"]["document_version"] = 2
    rejected = await http.post("/api/v1/task-drafts", headers={"Idempotency-Key": uuid4().hex}, json=request)
    assert rejected.status_code == 422, rejected.text


@pytest.mark.asyncio
async def test_production_rules_node_persists_sources_pending_review_and_no_duplicates(http_db):
    from app.documents import evidence_models as em
    from app.review import models as rm
    from app.workflows.models import Run, RunDocument, TaskDraft
    from app.workflows.translation import TranslationNodes

    http, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db:
        async with db.begin():
            section = (await db.execute(select(em.DocumentSection).where(em.DocumentSection.logical_document_id == ids["logical_id"]))).scalar_one()
            source = em.SourceSegment(tenant_id=principal.tenant_id, case_id=UUID(ids["case_id"]), parse_artifact_id=ids["parse_id"],
                logical_document_id=ids["logical_id"], section_id=section.id, ordinal=1, text="COMMERCIAL INVOICE", structure="paragraph")
            db.add(source)
            await db.flush()
            db.add(em.SourceAnchor(tenant_id=principal.tenant_id, case_id=UUID(ids["case_id"]), parse_artifact_id=ids["parse_id"],
                source_segment_id=source.id, precision=em.AnchorPrecision.PAGE_ONLY, page_no=1))
            draft = TaskDraft(tenant_id=principal.tenant_id, case_id=UUID(ids["case_id"]), created_by=principal.user_id,
                workflow_type="translate_review", workflow_version=1, source_versions=[], draft_parameters={}, status="ready",
                missing_fields=[], questions=[], task_summary="材料规则节点回滚验证", expires_at=datetime.now(UTC) + timedelta(hours=1))
            db.add(draft)
            await db.flush()
            inputs, _, _ = material_fixture()
            inputs["materials"] = [{"kind": "commercial_invoice", "source": {"document_id": ids["document_id"], "document_version": 1},
                                     "same_consignment": True, "basis": "本票TEST-001人工确认"}]
            run = Run(draft_task_id=draft.id, tenant_id=principal.tenant_id, case_id=UUID(ids["case_id"]), workflow_type="translate_review", workflow_version=1,
                task_spec={"rule_pack_version": MATERIAL_PACK, "jurisdiction_inputs": inputs}, task_spec_hash="0" * 64, prompt_versions={},
                initiated_by=principal.user_id, status="awaiting_review", current_stage="待复核", current_business_revision_id=ids["revision_id"])
            db.add(run)
            await db.flush()
            db.add(RunDocument(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id, ordinal=0, document_id=UUID(ids["document_id"]), document_version=1))
            await db.flush()
            worker = SimpleNamespace(tenant=principal.tenant_id, case_id=run.case_id, run_id=run.id, business_id=ids["revision_id"], parse_ids=[ids["parse_id"]], spec=run.task_spec)
            def rules(sync):
                node = TranslationNodes(worker)
                node._rules(sync, sync.get(Run, run.id), [sync.get(em.ParseArtifact, ids["parse_id"])])
            await db.run_sync(rules)
            await db.flush()
            checks = (await db.execute(select(rm.RuleCheck).where(rm.RuleCheck.run_id == run.id, rm.RuleCheck.rule_id.like("jurisdiction.consumer_materials.%")))).scalars().all()
            assert len(checks) == 3
            invoice = next(check for check in checks if check.rule_id.endswith("commercial_invoice"))
            assert invoice.status == "passed" and invoice.source_segment_ids == [str(source.id)]
            assert invoice.calculation["legal_review_status"] == "pending"
            count = (await db.execute(select(func.count()).select_from(rm.RuleCheck).where(rm.RuleCheck.run_id == run.id))).scalar_one()
            await db.run_sync(rules)
            await db.flush()
            assert (await db.execute(select(func.count()).select_from(rm.RuleCheck).where(rm.RuleCheck.run_id == run.id))).scalar_one() == count
            new_run_id = run.id
    response = await http.get(f"/api/v1/cases/{ids['case_id']}/issues", params={"run_id": str(new_run_id)})
    assert response.status_code == 200, response.text
