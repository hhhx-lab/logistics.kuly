"""真实PG回滚：独立计量口径、角色声明和已有组不互相覆盖。"""
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.consignment import models as cm, service
from app.consignment.dto import ScopePayload
from app.documents import evidence_models as em
from app.documents.models import Case, SourceDocument, DocumentVersion
from app.workflows.models import Run
from tests.test_review_field_gate import seed_fields
from tests.test_workflow_response_contracts import http_db


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["generic_completed", "not_extracted", "logistics", "unadopted_parse"])
async def test_empty_generic_fields_require_completed_adopted_extraction(http_db, mode):
    from sqlalchemy import delete
    from app.api.errors import InvalidRequest
    from app.workflows.models import StepExecution, StepStatus

    _, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        case = await db.get(Case, run.case_id)
        logical = await db.get(em.LogicalDocument, ids["logical_id"])
        logical.type_candidate = em.LogicalDocumentType.SUPPORTING if mode != "logistics" else em.LogicalDocumentType.COMMERCIAL_INVOICE
        if mode != "logistics":
            db.add(em.SectionAssignmentDecision(tenant_id=p.tenant_id, case_id=case.id,
                business_revision_id=ids["revision_id"], section_id=ids["section_id"],
                logical_document_type="generic_document", decided_by=p.user_id, reason="用户确认通用文档"))
        await db.execute(delete(em.BusinessRevisionField).where(em.BusinessRevisionField.business_revision_id == ids["revision_id"]))
        if mode != "not_extracted":
            db.add(StepExecution(tenant_id=p.tenant_id, run_id=run.id, epoch=run.current_epoch,
                step_name=f"抽取:{logical.id}:0", input_fingerprint="1" * 64, status=StepStatus.SUCCEEDED, artifact_ids=[]))
        await db.flush()
        scope = await db.run_sync(lambda s: service.ensure_scope(s, case, p.user_id))
        parses = [] if mode == "unadopted_parse" else [ids["parse_id"]]
        def verify(sync):
            if mode != "generic_completed":
                with pytest.raises(InvalidRequest, match="no adopted field view"):
                    service.assemble_revision(sync, run, case, scope, parses, [])
                return
            revision, selected_parses, fields = service.assemble_revision(sync, run, case, scope, parses, [])
            assert fields == [] and [p.id for p in selected_parses] == [ids["parse_id"]]
            assert run.current_business_revision_id == revision.id
        await db.run_sync(verify)


@pytest.mark.asyncio
async def test_material_replacement_refreshes_scope_without_reincluding_exclusions(http_db):
    from uuid import UUID
    from sqlalchemy import select
    from app.workflows.models import RunDocument

    _, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        case = await db.get(Case, UUID(ids['case_id']))
        source = await db.get(SourceDocument, UUID(ids['document_id']))
        version = await db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == source.id, DocumentVersion.version == 1))
        excluded = SourceDocument(tenant_id=p.tenant_id, case_id=case.id, current_version=1)
        db.add(excluded); await db.flush()
        db.add(DocumentVersion(tenant_id=p.tenant_id, case_id=case.id, document_id=excluded.id, version=1,
            filename='excluded.pdf', mime=version.mime, size_bytes=version.size_bytes, sha256='e'*64,
            object_id=version.object_id, uploaded_by=p.user_id))
        await db.flush()
        def initial(sync):
            payload=ScopePayload(materials=service.active_materials(sync,case), completeness='complete')
            payload=payload.model_copy(update={'materials':[m.model_copy(update={'purpose':'excluded'}) if m.document_id==excluded.id else m for m in payload.materials]})
            scope=service.append_scope(sync,case,p.user_id,payload)
            assert service.ensure_scope(sync,case,p.user_id).id==scope.id
            return scope
        old=await db.run_sync(initial)
        old_payload=old.payload.copy()
        db.add(DocumentVersion(tenant_id=p.tenant_id,case_id=case.id,document_id=source.id,version=2,
            filename='replacement.pdf',mime=version.mime,size_bytes=version.size_bytes,sha256='2'*64,
            object_id=version.object_id,uploaded_by=p.user_id))
        source.current_version=2
        await db.flush()
        current=await db.run_sync(lambda s:service.ensure_scope(s,case,p.user_id))
        payload=ScopePayload.model_validate(current.payload)
        assert current.id!=old.id and current.parent_revision_id==old.id
        assert payload.completeness=='unknown' and payload.parties==payload.legs==payload.cargo==[]
        assert len(payload.materials)==2
        assert next(m for m in payload.materials if m.document_id==excluded.id).purpose=='excluded'
        assert next(m for m in payload.materials if m.document_id==source.id).document_version==2
        assert old.payload==old_payload
        assert (await db.run_sync(lambda s:service.ensure_scope(s,case,p.user_id))).id==current.id
        frozen=await db.scalar(select(RunDocument).where(RunDocument.run_id==ids['run_id']))
        assert frozen.document_version==1


@pytest.mark.asyncio
async def test_quantity_weights_and_declared_party_have_separate_evidence(http_db):
    _, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        case = await db.get(Case, run.case_id)
        other = em.LogicalDocument(tenant_id=principal.tenant_id, case_id=case.id, parse_artifact_id=ids["parse_id"],
            ordinal=1, type_candidate=em.LogicalDocumentType.PACKING_LIST)
        db.add(other); await db.flush()
        section = em.DocumentSection(tenant_id=principal.tenant_id, case_id=case.id, parse_artifact_id=ids["parse_id"],
            logical_document_id=other.id, ordinal=1, kind="body")
        db.add(section); await db.flush()
        fields, lines = [], []
        for index, (logical, section_id, qty, net, gross, email) in enumerate([
            (ids["logical_id"], ids["section_id"], 400, 3200, 3440, "receiving@example.test"),
            (other.id, section.id, 390, 3120, 3354, "receving@example.test"),
        ]):
            source = em.SourceSegment(tenant_id=principal.tenant_id, case_id=case.id, parse_artifact_id=ids["parse_id"],
                logical_document_id=logical, section_id=section_id, ordinal=1,
                text=f"FRA-8 LOT-1 {qty} cartons; net {net} kg; gross {gross} kg; consignee {email}; shipper shipper{index}@example.test", structure="paragraph")
            db.add(source); await db.flush()
            field = em.FieldAssertion(tenant_id=principal.tenant_id, case_id=case.id, parse_artifact_id=ids["parse_id"],
                logical_document_id=logical, section_id=section_id, field_key="email", role="consignee", group="parties",
                source_text=email, candidate_value=email, source_segment_ids=[str(source.id)])
            fields.append(field); db.add(field)
            # 相同field_key、另一角色不能导致收货人整项跳过或混比发货人。
            shipper=em.FieldAssertion(tenant_id=principal.tenant_id, case_id=case.id, parse_artifact_id=ids["parse_id"],
                logical_document_id=logical, section_id=section_id, field_key="email", role="shipper", group="parties",
                source_text=f"shipper{index}@example.test", candidate_value=f"shipper{index}@example.test", source_segment_ids=[str(source.id)])
            fields.append(shipper); db.add(shipper)
            line = em.LineItem(tenant_id=principal.tenant_id, case_id=case.id, parse_artifact_id=ids["parse_id"],
                logical_document_id=logical, line_no=1, sku="FRA-8", lot_no="LOT-1", quantity=Decimal(qty), quantity_unit="cartons",
                source_segment_ids=[str(source.id)], measurements=[
                    {"quantity_kind":"net_weight", "value":str(net), "unit":"kg"},
                    {"quantity_kind":"gross_weight", "value":str(gross), "unit":"kg"}])
            lines.append(line); db.add(line)
        await db.flush()
        logicals=[ids["logical_id"],other.id]
        payload=ScopePayload(materials=[], completeness="complete", cargo=[{"id":"cargo","label":"demo","logical_document_ids":logicals}],
            legs=[{"id":"sea","mode":"sea","logical_document_ids":logicals}],
            parties=[{"id":"receiver","label":"Receiver","role":"consignee","logical_document_ids":logicals}])
        scope=cm.CaseScopeRevision(tenant_id=principal.tenant_id, case_id=case.id, revision_no=1,
            payload=payload.model_dump(mode="json"), fingerprint="1"*64, created_by=principal.user_id)
        db.add(scope); await db.flush()
        def verify(sync):
            group_ids, relation_ids=service.freeze_relationships(sync,case,scope,payload,lines,fields)
            groups=[sync.get(cm.CargoMatchGroup,gid) for gid in group_ids]
            assert {group.scope["quantity_kind"] for group in groups} == {"quantity","net_weight","gross_weight"}
            assert len(groups)==3 and len(relation_ids)==1
            relation=sync.get(cm.RelationAssertion,relation_ids[0])
            assert relation.state=="confirmed" and relation.basis["role"]=="consignee"
            batch=cm.ComparisonBatch(tenant_id=principal.tenant_id,case_id=case.id,run_id=run.id,epoch=run.current_epoch,
                scope_revision_id=scope.id,business_revision_id=ids["revision_id"],input_fingerprint="2"*64,status="building",summary={},
                manifest={"line_ids":[str(line.id) for line in lines],"assertion_ids":[str(field.id) for field in fields],
                    "match_group_ids":[str(gid) for gid in group_ids],"relation_ids":[str(rid) for rid in relation_ids],"materials":[]})
            sync.add(batch);sync.flush()
            summary=service.publish_comparison(sync,run,case,batch)
            assert summary["red"]==2 and summary["yellow"]==2 and summary["unknown"]==4
            for group in groups:
                result=service.compare_measurements(lines[:1],lines[1:],group.scope,group.state)
                if group.scope["quantity_kind"]=="quantity":
                    assert result["severity"]=="red" and result["delta"]=="10"
                else:
                    assert result["status"]=="undetermined" and result["delta"] in {"80","86"}
            # 只确认净重组后，数量及毛重组仍存在，不被同货物行覆盖判定吞掉。
            net=next(group for group in groups if group.scope["quantity_kind"]=="net_weight")
            existing=payload.model_copy(update={"match_group_ids":[net.id]})
            gids,_=service.freeze_relationships(sync,case,scope,existing,lines,fields)
            assert {sync.get(cm.CargoMatchGroup,gid).scope["quantity_kind"] for gid in gids} == {"quantity","net_weight","gross_weight"}
        await db.run_sync(verify)
