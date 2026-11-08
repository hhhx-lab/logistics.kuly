"""同票范围、关系决定及固定批次矩阵。读查询不创建任务或刷新留存。"""
from __future__ import annotations

import base64
import json
from uuid import UUID,uuid4
from fastapi import APIRouter, Query
from sqlalchemy import select, or_
from app.api.dependencies import CurrentUser,WriteUser,DbSession,SettingsDep,set_tenant_context
from app.api.errors import InvalidRequest,NotFoundOrForbidden,VersionConflict
from app.api import idempotency
from app.api.routers.workflows import IdempotencyKey,_begin
from app.consignment import dto as d,models as cm,service as cs
from app.consignment.engine import CONFIRMED
from app.documents import evidence_models as em
from app.documents.models import AuditEvent
from app.documents.retention import load_case_for_read,load_case_for_write,touch_material_event
from app.review import models as rm
from app.review.issue_state import issue_resolved
from app.workflows import commands as wc
from app.workflows.models import Run
from app.exports.models import ExportSnapshot

router=APIRouter(tags=["consignment"])


def _encode(payload):return base64.urlsafe_b64encode(json.dumps(payload,separators=(",",":")).encode()).decode()
def _cursor(value,batch,filters,version):
    if not value:return 0
    try:
        obj=json.loads(base64.urlsafe_b64decode(value))
        if obj["batch"]!=str(batch) or obj["filter"]!=idempotency.request_digest(filters) or obj["version"]!=version:
            raise VersionConflict("Comparison page changed; reload",code="PAGE_STALE")
        offset=int(obj["offset"])
        if offset<0:raise ValueError()
        return offset
    except VersionConflict:raise
    except (ValueError,KeyError,TypeError,json.JSONDecodeError):raise InvalidRequest("Invalid comparison cursor") from None


def _next(offset,total,batch,filters,version):
    return _encode({"offset":offset,"batch":str(batch),"filter":idempotency.request_digest(filters),"version":version}) if offset<total else None


def _scope_view(session,case,scope):
    payload=d.ScopePayload.model_validate(scope.payload) if scope else d.ScopePayload(materials=cs.active_materials(session,case))
    return d.ScopeView(case_id=case.id,scope_revision_id=scope.id if scope else None,revision_no=scope.revision_no if scope else None,
        payload=payload,case_version=case.case_version,business_revision_id=case.current_business_revision_id,
        review_state_version=case.review_state_version,stale=cs.scope_stale(session,case,scope) if scope else True,
        fingerprint=scope.fingerprint if scope else idempotency.request_digest(payload.model_dump(mode="json")))


def _batch_view(session,case,batch,*,frozen_disposition_ids=None):
    current=cs.batch_current(session,case,batch)
    return d.BatchView(batch_id=batch.id,case_id=case.id,run_id=batch.run_id,epoch=batch.epoch,
        scope_revision_id=batch.scope_revision_id,business_revision_id=batch.business_revision_id,status=batch.status,
        current=case.current_comparison_batch_id==batch.id,stale=not current,
        summary=cs.batch_summary(session,batch,frozen_disposition_ids=frozen_disposition_ids),
        manifest=batch.manifest,input_fingerprint=batch.input_fingerprint,created_at=batch.created_at)


def _batch(session,case,batch_id):
    batch=cs.owned(session.get(cm.ComparisonBatch,batch_id),case)
    if batch.status!="completed":raise VersionConflict("Comparison batch is not published",code="COMPARISON_NOT_READY")
    for m in batch.manifest["materials"]:
        if m.get("purpose")!="excluded":
            from app.documents.models import DocumentVersion
            version=session.scalar(select(DocumentVersion).where(DocumentVersion.case_id==case.id,
                DocumentVersion.document_id==UUID(m["document_id"]),DocumentVersion.version==m["document_version"]))
            if version is None:raise NotFoundOrForbidden()
    return batch


def _snapshot_comparison(session,case,snapshot_id,*,batch_id=None,run_id=None):
    snapshot=cs.owned(session.get(ExportSnapshot,snapshot_id),case)
    if run_id and snapshot.run_id!=run_id:raise NotFoundOrForbidden()
    manifest=snapshot.manifest
    review=manifest.get("review_manifest") or manifest
    comparison=manifest.get("comparison") or review.get("comparison") or {}
    reference=comparison.get("batch_id") or manifest.get("comparison_batch_id") or review.get("comparison_batch_id")
    if reference is None:
        if batch_id:raise NotFoundOrForbidden()
        return snapshot,None,[]
    if batch_id and str(batch_id)!=reference:raise NotFoundOrForbidden()
    batch=_batch(session,case,UUID(reference))
    if comparison.get("input_fingerprint") and comparison["input_fingerprint"]!=batch.input_fingerprint:
        raise NotFoundOrForbidden()
    frozen_ids=comparison.get("issue_disposition_ids",review.get("issue_disposition_ids",[]))
    return snapshot,batch,frozen_ids


@router.get("/cases/{case_id}/consignment-scope",response_model=d.ScopeView)
async def scope(case_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,scope_revision_id:UUID|None=None):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        row=await db.get(cm.CaseScopeRevision,scope_revision_id or case.current_scope_revision_id) if scope_revision_id or case.current_scope_revision_id else None
        if row:cs.owned(row,case)
        return await db.run_sync(lambda s:_scope_view(s,case,row))


async def _mutation(db,principal,settings,case,scope,key,previous=None,*,launch=True):
    touch_material_event(case,settings)
    db.add(AuditEvent(tenant_id=case.tenant_id,case_id=case.id,actor_id=principal.user_id,action="consignment.scope_decision",
        object_ref={"scope_revision_id":str(scope.id)},seen_versions={"case_version":case.case_version,"review_state_version":case.review_state_version}))
    result=d.MutationView(case_id=case.id,scope_revision_id=scope.id,case_version=case.case_version,review_state_version=case.review_state_version)
    if launch:
        payload=d.ScopePayload.model_validate(scope.payload)
        sources=[{"document_id":str(m.document_id),"document_version":m.document_version} for m in payload.materials if m.purpose!="excluded"]
        parameters={"scope_revision_id":str(scope.id),"previous_batch_id":str(previous) if previous else None}
        draft=await wc.prepare_draft(db,principal,settings,case_id=case.id,workflow_type="consignment_check",workflow_version=1,
            source_versions=sources,draft_parameters=parameters)
        run=await wc.start_run(db,principal,settings,draft_task_id=draft.id,expected_draft_version=draft.draft_version,idempotency_key=key)
        result.run_id=run.id;result.epoch=run.current_epoch
        result.case_version=case.case_version;result.review_state_version=case.review_state_version
    return result


async def _write_begin(db,principal,settings,case_id,key,body,path):
    await set_tenant_context(db,principal)
    wc.require_permission(principal,"review_revise")
    case=await load_case_for_write(db,case_id,user_id=principal.user_id,settings=settings)
    outcome=await _begin(db,principal,scope=path,key=key,payload=body.model_dump(mode="json"))
    if not outcome.replayed:cs.check_preconditions(case,body)
    return case,outcome


@router.post("/cases/{case_id}/consignment-scope/revisions",response_model=d.MutationView)
async def revise_scope(case_id:UUID,body:d.ScopeCommand,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async with db.begin():
        case,outcome=await _write_begin(db,principal,settings,case_id,idempotency_key,body,f"scope:{case_id}")
        if outcome.replayed:return outcome.record.result_ref
        scope=await db.run_sync(lambda s:cs.append_scope(s,case,principal.user_id,body.payload))
        result=await _mutation(db,principal,settings,case,scope,idempotency_key,launch=False)
        idempotency.complete(outcome.record,result.model_dump(mode="json"));return result


@router.get("/cases/{case_id}/relations",response_model=d.RelationPage)
async def relations(case_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,scope_revision_id:UUID|None=None,
                    batch_id:UUID|None=None,kind:str|None=None,state:str|None=None,cursor:str|None=None,limit:int=Query(default=50,ge=1,le=100)):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        if batch_id:
            batch=await db.run_sync(lambda s:_batch(s,case,batch_id));scope_revision_id=batch.scope_revision_id
        scope_revision_id=scope_revision_id or case.current_scope_revision_id
        if not scope_revision_id:return d.RelationPage(items=[])
        scoped=cs.owned(await db.get(cm.CaseScopeRevision,scope_revision_id),case)
        query=select(cm.RelationAssertion).where(cm.RelationAssertion.case_id==case.id)
        if batch_id:query=query.where(cm.RelationAssertion.id.in_(batch.manifest["relation_ids"]))
        else:query=query.where(or_(cm.RelationAssertion.scope_revision_id==scope_revision_id,cm.RelationAssertion.id.in_(scoped.payload["relation_ids"])))
        if kind:query=query.where(cm.RelationAssertion.kind==kind)
        if state:query=query.where(cm.RelationAssertion.state==state)
        rows=list((await db.execute(query.order_by(cm.RelationAssertion.created_at,cm.RelationAssertion.id))).scalars())
        superseded={x.supersedes_id for x in rows if x.supersedes_id};rows=[x for x in rows if x.id not in superseded]
        filters={"kind":kind,"state":state};offset=_cursor(cursor,scope_revision_id,filters,case.review_state_version)
        return d.RelationPage(items=[d.RelationView(id=x.id,kind=x.kind,state=x.state,left_logical_id=x.left_logical_id,
            right_logical_id=x.right_logical_id,basis=x.basis) for x in rows[offset:offset+limit]],
            next_cursor=_next(offset+limit,len(rows),scope_revision_id,filters,case.review_state_version))


@router.post("/cases/{case_id}/relation-decisions",response_model=d.MutationView)
async def decide_relation(case_id:UUID,body:d.RelationDecision,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async with db.begin():
        case,outcome=await _write_begin(db,principal,settings,case_id,idempotency_key,body,f"relation:{case_id}")
        if outcome.replayed:return outcome.record.result_ref
        old=cs.owned(await db.get(cm.RelationAssertion,body.relation_id),case)
        current=cs.owned(await db.get(cm.CaseScopeRevision,case.current_scope_revision_id),case)
        if old.scope_revision_id!=current.id and str(old.id) not in current.payload["relation_ids"]:raise VersionConflict("Relation candidate is stale")
        if body.decision=="correct" and not body.kind:raise InvalidRequest("Corrected relation kind is required")
        kinds={"identical_file","document_revision","language_version","attachment","explicit_reference","master_house","same_goods","transport_handover","same_party"}
        kind=body.kind or old.kind
        if kind not in kinds:raise InvalidRequest("Unsupported relation kind")
        for sid in body.evidence_ids:cs.owned(await db.get(em.SourceSegment,sid),case)
        new=cm.RelationAssertion(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,scope_revision_id=current.id,
            kind=kind,state="rejected" if body.decision=="reject" else "confirmed",left_logical_id=old.left_logical_id,
            right_logical_id=old.right_logical_id,basis={**old.basis,"reason":body.reason,"evidence_ids":[str(x) for x in body.evidence_ids]},
            supersedes_id=old.id,decided_by=principal.user_id)
        db.add(new);await db.flush()
        payload=d.ScopePayload.model_validate(current.payload)
        payload=payload.model_copy(update={"relation_ids":[x for x in payload.relation_ids if x!=old.id]+[new.id]})
        scope=await db.run_sync(lambda s:cs.append_scope(s,case,principal.user_id,payload))
        result=await _mutation(db,principal,settings,case,scope,idempotency_key,case.current_comparison_batch_id)
        idempotency.complete(outcome.record,result.model_dump(mode="json"));return result


@router.post("/cases/{case_id}/cargo-match-decisions",response_model=d.MutationView)
async def decide_cargo(case_id:UUID,body:d.CargoDecision,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async with db.begin():
        case,outcome=await _write_begin(db,principal,settings,case_id,idempotency_key,body,f"cargo:{case_id}")
        if outcome.replayed:return outcome.record.result_ref
        current=cs.owned(await db.get(cm.CaseScopeRevision,case.current_scope_revision_id),case)
        payload=d.ScopePayload.model_validate(current.payload)
        if body.scope.cargo_id not in {x.id for x in payload.cargo} or body.scope.leg_id not in {x.id for x in payload.legs}:
            raise InvalidRequest("Confirm cargo and transport stage in scope before matching")
        left,right=body.left_line_ids,body.right_line_ids
        if len(left)>1 and len(right)>1:raise InvalidRequest("Many-to-many cargo matching must be split into explicit groups")
        if len(set(left+right))!=len(left+right):raise InvalidRequest("Cargo lines cannot be counted twice")
        lines=[cs.owned(await db.get(em.LineItem,x),case) for x in left+right]
        allowed={(m.document_id,m.document_version) for m in payload.materials if m.purpose!="excluded"}
        for line in lines:
            parse=cs.owned(await db.get(em.ParseArtifact,line.parse_artifact_id),case)
            if (parse.document_id,parse.document_version) not in allowed:raise InvalidRequest("Cargo line is outside current scope")
        group=cm.CargoMatchGroup(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,scope_revision_id=current.id,state="confirmed",
            cardinality="1:1" if len(left)==len(right)==1 else "1:n" if len(left)==1 else "n:1",scope=body.scope.model_dump(mode="json"),
            basis={"reason":body.basis},supersedes_id=body.supersedes_id,decided_by=principal.user_id)
        if body.supersedes_id:cs.owned(await db.get(cm.CargoMatchGroup,body.supersedes_id),case)
        db.add(group);await db.flush()
        for side,ids in (("left",left),("right",right)):
            for line_id in ids:db.add(cm.CargoMatchMember(tenant_id=case.tenant_id,case_id=case.id,group_id=group.id,side=side,line_item_id=line_id,basis={"reason":body.basis}))
        await db.flush()
        payload=payload.model_copy(update={"match_group_ids":[x for x in payload.match_group_ids if x!=body.supersedes_id]+[group.id]})
        scope=await db.run_sync(lambda s:cs.append_scope(s,case,principal.user_id,payload))
        result=await _mutation(db,principal,settings,case,scope,idempotency_key,case.current_comparison_batch_id)
        idempotency.complete(outcome.record,result.model_dump(mode="json"));return result


@router.post("/cases/{case_id}/comparisons/recompute",response_model=d.MutationView)
async def recompute(case_id:UUID,body:d.RecomputeCommand,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async with db.begin():
        case,outcome=await _write_begin(db,principal,settings,case_id,idempotency_key,body,f"recompute:{case_id}")
        if outcome.replayed:return outcome.record.result_ref
        if body.previous_batch_id:cs.owned(await db.get(cm.ComparisonBatch,body.previous_batch_id),case)
        scope=await db.run_sync(lambda s:cs.ensure_scope(s,case,principal.user_id))
        result=await _mutation(db,principal,settings,case,scope,idempotency_key,body.previous_batch_id)
        idempotency.complete(outcome.record,result.model_dump(mode="json"));return result


@router.get("/cases/{case_id}/comparisons",response_model=d.BatchPage)
async def comparisons(case_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,run_id:UUID|None=None,
                      snapshot_id:UUID|None=None,scope_revision_id:UUID|None=None,status:str|None=None,cursor:str|None=None,
                      limit:int=Query(default=50,ge=1,le=100)):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        query=select(cm.ComparisonBatch).where(cm.ComparisonBatch.case_id==case.id)
        frozen_ids=None;snapshot=None
        if snapshot_id:
            snapshot,batch,frozen_ids=await db.run_sync(lambda s:_snapshot_comparison(s,case,snapshot_id,run_id=run_id))
            if batch is None:return d.BatchPage(items=[])
            query=query.where(cm.ComparisonBatch.id==batch.id)
        elif run_id:
            run=cs.owned(await db.get(Run,run_id),case)
            binding=await db.run_sync(lambda s:cs.bound_batch(s,run,case,strict_current=False))
            query=query.where(cm.ComparisonBatch.id==binding.id) if binding else query.where(cm.ComparisonBatch.run_id==run.id)
        if scope_revision_id:query=query.where(cm.ComparisonBatch.scope_revision_id==scope_revision_id)
        if status:query=query.where(cm.ComparisonBatch.status==status)
        rows=list((await db.execute(query.order_by(cm.ComparisonBatch.created_at.desc(),cm.ComparisonBatch.id.desc()))).scalars())
        filters={"run":str(run_id),"snapshot":str(snapshot_id),"scope":str(scope_revision_id),"status":status}
        version=str(snapshot.id) if snapshot else case.review_state_version
        offset=_cursor(cursor,case.id,filters,version)
        items=await db.run_sync(lambda s:[_batch_view(s,case,x,frozen_disposition_ids=frozen_ids) for x in rows[offset:offset+limit]])
        return d.BatchPage(items=items,next_cursor=_next(offset+limit,len(rows),case.id,filters,version))


@router.get("/cases/{case_id}/comparisons/{batch_id}",response_model=d.BatchView)
async def comparison(case_id:UUID,batch_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,snapshot_id:UUID|None=None):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        def read(session):
            if snapshot_id:
                _,batch,frozen_ids=_snapshot_comparison(session,case,snapshot_id,batch_id=batch_id)
                return _batch_view(session,case,batch,frozen_disposition_ids=frozen_ids)
            return _batch_view(session,case,_batch(session,case,batch_id))
        return await db.run_sync(read)


def _matrix_rows(session,case,batch,*,status,severity,group_id,logical_ids,frozen_disposition_ids=None):
    checks=list(session.scalars(select(rm.RuleCheck).where(rm.RuleCheck.comparison_batch_id==batch.id).order_by(rm.RuleCheck.rule_id,rm.RuleCheck.id)))
    issues={i.rule_check_id:i for i in session.scalars(select(rm.Issue).where(rm.Issue.comparison_batch_id==batch.id))}
    dispositions=cs.dispositions_for(session,list(issues.values()),frozen_disposition_ids=frozen_disposition_ids)
    rows=[]
    for check in checks:
        calc=check.calculation;issue=issues.get(check.id);disposition=dispositions.get(issue.id) if issue else None
        resolved=issue_resolved(issue,disposition) if issue else False
        rowseverity="unknown" if not issue or issue.severity=="undetermined" else issue.severity
        if severity and rowseverity!=severity:continue
        if group_id and calc.get("group_id")!=str(group_id):continue
        member_ids={x.get("logical_id") for members in calc.get("members",{}).values() for x in members}
        if logical_ids and not member_ids.intersection(logical_ids):continue
        if status in {"open","unresolved"} and (not issue or resolved):continue
        if status=="done" and not resolved:continue
        if status=="normal" and check.status!="passed":continue
        if status=="pending_relation" and check.status!="undetermined":continue
        rows.append(d.MatrixRow(row_id=check.id,batch_id=batch.id,rule_check_id=check.id,rule_id=check.rule_id,rule_version=check.rule_version,
            comparison_basis=check.comparison_basis,group_id=UUID(calc["group_id"]) if calc.get("group_id") else None,status=check.status,
            difference_kind=calc.get("difference_kind","scope_insufficient"),severity=rowseverity,description=check.explanation,
            members=calc.get("members",{}),calculation={k:v for k,v in calc.items() if k not in {"members","sources"}},sources=calc.get("sources",[]),
            issue_id=issue.id if issue else None,disposition={"id":str(disposition.id),"action":disposition.action,"reason":disposition.reason,"resolved":resolved} if disposition else None,
            dependency_fingerprint=check.input_fingerprint))
    return rows


@router.get("/cases/{case_id}/comparisons/{batch_id}/rows",response_model=d.MatrixPage)
async def comparison_rows(case_id:UUID,batch_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,status:str="open",
                          severity:str|None=None,group_id:UUID|None=None,logical_ids:str|None=None,cursor:str|None=None,
                          review_state_version:int|None=None,snapshot_id:UUID|None=None,limit:int=Query(default=50,ge=1,le=100)):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        if not snapshot_id and review_state_version is not None and case.review_state_version!=review_state_version:raise VersionConflict("Comparison page changed",code="PAGE_STALE")
        frozen_ids=None;snapshot=None
        if snapshot_id:
            snapshot,batch,frozen_ids=await db.run_sync(lambda s:_snapshot_comparison(s,case,snapshot_id,batch_id=batch_id))
        else:batch=await db.run_sync(lambda s:_batch(s,case,batch_id))
        ids=set(logical_ids.split(",")) if logical_ids else set()
        filters={"status":status,"severity":severity,"group":str(group_id),"logical":sorted(ids),"snapshot":str(snapshot_id)}
        version=str(snapshot.id) if snapshot else case.review_state_version
        offset=_cursor(cursor,batch.id,filters,version)
        rows=await db.run_sync(lambda s:_matrix_rows(s,case,batch,status=status,severity=severity,group_id=group_id,logical_ids=ids,frozen_disposition_ids=frozen_ids))
        return d.MatrixPage(items=rows[offset:offset+limit],next_cursor=_next(offset+limit,len(rows),batch.id,filters,version),review_state_version=case.review_state_version)


@router.get("/cases/{case_id}/comparisons/{batch_id}/rows/{row_id}/evidence",response_model=d.EvidenceView)
async def row_evidence(case_id:UUID,batch_id:UUID,row_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        await db.run_sync(lambda s:_batch(s,case,batch_id))
        check=cs.owned(await db.get(rm.RuleCheck,row_id),case)
        if check.comparison_batch_id!=batch_id:raise NotFoundOrForbidden()
        return d.EvidenceView(row_id=row_id,sides={side:[x for x in check.calculation.get("sources",[]) if x.get("side")==side] for side in ("left","right")})
