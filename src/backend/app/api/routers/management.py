"""M01–M06 管理台、摘要、共享时间线及owner整票删除。"""
from __future__ import annotations
import base64,json
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, or_
from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser, set_tenant_context
from app.api.errors import NotFoundOrForbidden,InvalidRequest
from app.api import response_dto as d
from app.api.routers.uploads import _case_view
from app.documents.models import Case, DocumentVersion, SourceDocument
from app.documents import evidence_models as em
from app.documents.retention import load_case_for_read
from app.exports.models import ExportSnapshot
from app.identity.models import User
from app.management import service
from app.management.deletion import confirm_committed, deletion_view, request_deletion
from app.management.models import CaseDeletion
from app.review import models as rm
from app.review.commands import issue_view
from app.workflows import commands as wc
from app.workflows.models import Run, RunEvent

router=APIRouter(tags=["management"])
IdempotencyKey=Annotated[str,Header(alias="Idempotency-Key",min_length=8,max_length=80)]


def filters(request):
    result={key:request.query_params.getlist(key) if key in {"doc_type","format","language","risk"} else request.query_params[key]
        for key in request.query_params if key not in {"cursor","limit"}}
    return result


@router.get("/runs", response_model=d.PageView[d.ManagedRunView], response_model_exclude_unset=True)
async def runs(request:Request,principal:CurrentUser,db:DbSession,settings:SettingsDep,cursor:str|None=None,limit:int=Query(default=25,ge=1,le=100)):
    async with db.begin():
        await set_tenant_context(db,principal)
        value=filters(request)
        rows=await service.query_runs(db,principal,settings,value,cursor=cursor,limit=limit+1)
        return service.paginate(rows,cursor,limit,value)


@router.get("/runs/facets", response_model=d.RunFacetsView, response_model_exclude_unset=True)
async def facets(request:Request,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        value=filters(request);value.pop("tab",None)
        return await service.facets(db,principal,settings,value)


@router.get("/runs/search-suggestions", response_model=d.PageView[d.SearchSuggestionView], response_model_exclude_unset=True)
async def search_suggestions(request:Request,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        value=filters(request)
        rows=await service.query_runs(db,principal,settings,value,limit=8)
        return {"items":[{"run_id":row["run_id"],"case_id":row["case_id"],"title":row.get("case_name") or row["case_id"],
            "language":row.get("target_language"),"status":row["current_work_status"],"snapshot_id":row.get("displayed_snapshot_id"),"activity_at":row["last_activity_at"],
            "match_explanation":"角色字段命中" if row.get("matched_sources") else "单票编号／文件名／单号／导航摘要命中", "matched_sources":row.get("matched_sources",[])} for row in rows[:8]]}


@router.get("/runs/actors", response_model=d.PageView[d.ActorView], response_model_exclude_unset=True)
async def actors(principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        # 不依赖检索投影是否生成，也不截取前100条任务。
        cases=list((await db.execute(select(Case.id).where(Case.status==service.CaseStatus.ACTIVE,
            Case.retention_expires_at>service.datetime.now(service.UTC)))).scalars())
        allowed_case_ids=[]
        for case_id in cases:
            try:
                await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
                allowed_case_ids.append(case_id)
            except (service.CaseGone,NotFoundOrForbidden):
                continue
        allowed_run_ids=list((await db.execute(select(Run.id).where(Run.case_id.in_(allowed_case_ids)))).scalars())
        # 候选只能来自本人有权读取的任务，不公开整个租户成员目录。
        ids=set((await db.execute(select(Run.initiated_by).where(Run.id.in_(allowed_run_ids)))).scalars())
        ids.update((await db.execute(select(rm.IssueDisposition.decided_by).join(rm.Issue,rm.Issue.id==rm.IssueDisposition.issue_id).where(rm.Issue.run_id.in_(allowed_run_ids)))).scalars())
        from app.exports.models import ReviewConfirmation
        ids.update((await db.execute(select(ReviewConfirmation.confirmed_by).where(ReviewConfirmation.run_id.in_(allowed_run_ids)))).scalars())
        ids.update((await db.execute(select(ExportSnapshot.exported_by).where(ExportSnapshot.run_id.in_(allowed_run_ids)))).scalars())
        users=list((await db.execute(select(User).where(User.id.in_(ids),User.tenant_id==principal.tenant_id,User.is_active.is_(True)).order_by(User.display_name,User.id))).scalars())
        return {"items":[{"id":str(user.id),"name":user.display_name} for user in users]}


@router.get("/cases/{case_id}/overview", response_model=d.CaseOverviewView, response_model_exclude_unset=True)
async def overview(case_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        owner=await db.get(User,case.owner_user_id)
        sources=(await db.execute(select(SourceDocument).where(SourceDocument.case_id==case.id).order_by(SourceDocument.created_at))).scalars().all()
        runs=(await db.execute(select(Run).where(Run.case_id==case.id).order_by(Run.created_at.desc()))).scalars().all()
        issues=(await db.execute(select(rm.Issue).join(Run, Run.id==rm.Issue.run_id).where(rm.Issue.case_id==case.id,rm.Issue.business_revision_id==case.current_business_revision_id,
            or_(rm.Issue.translation_revision_id.is_(None),rm.Issue.translation_revision_id==Run.current_translation_revision_id)))).scalars().all()
        views=[await issue_view(db,x) for x in issues]
        snapshots=(await db.execute(select(ExportSnapshot).where(ExportSnapshot.case_id==case.id).order_by(ExportSnapshot.exported_at.desc()))).scalars().all()
        versions=list((await db.execute(select(DocumentVersion).where(DocumentVersion.case_id==case.id).order_by(DocumentVersion.document_id,DocumentVersion.version.desc()))).scalars())
        parses=list((await db.execute(select(em.ParseArtifact).where(em.ParseArtifact.case_id==case.id).order_by(em.ParseArtifact.created_at.desc()))).scalars())
        logical=list((await db.execute(select(em.LogicalDocument).where(em.LogicalDocument.case_id==case.id).order_by(em.LogicalDocument.ordinal))).scalars())
        sections=list((await db.execute(select(em.DocumentSection).where(em.DocumentSection.case_id==case.id).order_by(em.DocumentSection.ordinal))).scalars())
        from app.api.idempotency import request_digest
        def parse_directory(parse,version):
            target={"case_id":str(case.id),"document_id":str(version.document_id),"document_version":version.version,"parse_artifact_id":str(parse.id)}
            return {"parse_artifact_id":str(parse.id),"created_at":parse.created_at,"quality":parse.quality.value,"page_count":parse.page_count,
                "parser_name":parse.parser_name,"parser_version":parse.parser_version,"target":target,
                "summary":f"{version.filename} v{version.version}；解析器 {parse.parser_name} {parse.parser_version}；{parse.quality.value}；{parse.page_count or '未知'}页",
                "generated_at":parse.created_at,"state":"ready","dependency_fingerprint":request_digest({"source":str(version.id),"parse":str(parse.id),"parser_config":parse.parser_config_hash}),
                "logical_documents":[{"logical_document_id":str(item.id),"title":item.title or item.type_candidate.value,"generated_at":item.created_at,"state":"ready",
                    "summary":f"{item.title or item.type_candidate.value}；原件 v{version.version}；解析 {parse.id}；页 {item.page_start or '?'}–{item.page_end or '?'}",
                    "dependency_fingerprint":request_digest({"source":str(version.id),"parse":str(parse.id),"logical":str(item.id)}),"page_range":f"{item.page_start or '?'}–{item.page_end or '?'}",
                    "sections":[{"section_id":str(section.id),"title":section.title or "区段","page_range":f"{section.page_start or '?'}–{section.page_end or '?'}",
                        "target":{**target,"logical_document_id":str(item.id),"section_id":str(section.id)}} for section in sections if section.logical_document_id==item.id]} for item in logical if item.parse_artifact_id==parse.id]}
        return {"summary":f"{case.name}；{len(sources)}份原件，{len(versions)}个原件版本；{len(runs)}次处理："+"、".join(f"{r.target_language or '识别索引'} {r.status.value}" for r in runs)+f"；{len(snapshots)}份成功交付；当前业务版 {case.current_business_revision_id or '无'}；当前疑点 {len(views)}；摘要仅供导航",
            "generated_at":service.datetime.now(service.UTC),"dependency_fingerprint":request_digest({"case_version":case.case_version,"review_state":case.review_state_version,"sources":[str(v.id) for v in versions],"parses":[str(p.id) for p in parses],"runs":[[str(r.id),r.status.value,str(r.current_business_revision_id),str(r.current_translation_revision_id)] for r in runs],"snapshots":[str(s.id) for s in snapshots]}),"state":"ready","source_history":[{"source":await service.source_view(db,v.document_id,v.version),"generated_at":v.created_at,"state":"ready",
                "dependency_fingerprint":request_digest({"source":str(v.id),"parses":[str(p.id) for p in parses if p.document_id==v.document_id and p.document_version==v.version]}),"summary":f"{v.filename}；原件 v{v.version}；{v.mime}；{v.size_bytes}字节；上传 {v.created_at.isoformat()}；{sum(p.document_id==v.document_id and p.document_version==v.version for p in parses)}次解析", "parses":[
            parse_directory(p,v)
            for p in parses if p.document_id==v.document_id and p.document_version==v.version]} for v in versions],
            "case":_case_view(case,owner_name=owner.display_name if owner else str(case.owner_user_id),is_owner=case.owner_user_id==principal.user_id),
            "sources":[await service.source_view(db,s.id,s.current_version) for s in sources if s.current_version],
            "runs":[{"run_id":str(r.id),"workflow_type":r.workflow_type,"status":r.status.value,"initiated_at":r.created_at,"target_language":r.target_language} for r in runs],
            "issues":{"blocking":sum(x["severity"]=="blocking" and x["status"] in {"open","unresolved"} for x in views),"warning":sum(x["severity"]=="warning" and x["status"] in {"open","unresolved"} for x in views),"undetermined":sum((x["severity"]=="undetermined" or x["status"]=="unresolved") and x["status"] in {"open","unresolved"} for x in views)},
            "snapshots":[{"snapshot_id":str(s.id),"run_id":str(s.run_id),"exported_at":s.exported_at,"business_revision_no":s.manifest.get("business_revision_no"),"translation_revision_no":s.manifest.get("translation_revision_no"),"target_language":s.manifest.get("target_language")} for s in snapshots],"last_material_event_at":case.last_material_event_at}


@router.get("/runs/{run_id}/summary", response_model=d.RunSummaryView, response_model_exclude_unset=True)
async def summary(run_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,snapshot_id:UUID|None=None):
    async with db.begin():
        await set_tenant_context(db,principal)
        run,case=await wc.load_run(db,principal,settings,run_id)
        snapshot=await db.get(ExportSnapshot,snapshot_id) if snapshot_id else None
        if snapshot_id and (snapshot is None or snapshot.run_id!=run.id):raise NotFoundOrForbidden()
        return await service.summary(db,run,case,snapshot)


@router.get("/runs/{run_id}/timeline", response_model=d.PageView[d.TimelineEventView], response_model_exclude_unset=True)
async def timeline(run_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,cursor:str|None=None,limit:int=Query(default=50,ge=1,le=100)):
    async with db.begin():
        await set_tenant_context(db,principal)
        run,_=await wc.load_run(db,principal,settings,run_id)
        from app.api.idempotency import request_digest
        filters={"run_id":str(run_id),"view":"timeline"};last=0
        if cursor:
            try:
                value=json.loads(base64.urlsafe_b64decode(cursor))
                if value["filter"]!=request_digest(filters):raise ValueError
                last=int(value["last"][0])
                if last<0:raise ValueError
            except (ValueError,KeyError,TypeError) as exc:raise InvalidRequest("时间线游标无效") from exc
        events=(await db.execute(select(RunEvent).where(RunEvent.run_id==run.id,RunEvent.seq>last).order_by(RunEvent.seq).limit(limit+1))).scalars().all()
        rows=[{"seq":e.seq,"occurred_at":e.occurred_at,"kind":e.kind,"actor":e.actor or "系统","message":e.message or "","ops_step_id":e.payload.get("step_execution_id") if e.payload else None} for e in events]
        return service.paginate(rows,None,limit,filters)


@router.get("/cases/{case_id}/deletion-preview", response_model=d.DeletionPreviewView, response_model_exclude_unset=True)
async def deletion_preview(case_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        return {"case_id":str(case.id),"case_version":case.case_version,"short_code":case.short_code,"retention_expires_at":case.retention_expires_at,
            "source_count":(await db.execute(select(func.count(DocumentVersion.id)).where(DocumentVersion.case_id==case.id))).scalar_one(),
            "run_count":(await db.execute(select(func.count(Run.id)).where(Run.case_id==case.id))).scalar_one(),
            "snapshot_count":(await db.execute(select(func.count(ExportSnapshot.id)).where(ExportSnapshot.case_id==case.id))).scalar_one(),
            "allowed":case.owner_user_id==principal.user_id,"disabled_reason":None if case.owner_user_id==principal.user_id else "只有单票所有人可以删除"}


class DeleteBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_case_version:int=Field(ge=1)
    confirm_short_code:str=Field(min_length=1,max_length=16)


@router.post("/cases/{case_id}/delete",status_code=202, response_model=d.DeletionView, response_model_exclude_unset=True)
async def delete_case(case_id:UUID,body:DeleteBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async with db.begin():
        await set_tenant_context(db,principal)
        value=await request_deletion(db,principal,settings,case_id,body,idempotency_key)
    confirm_committed(settings,value)
    return deletion_view(value)


@router.get("/case-deletions/{deletion_id}", response_model=d.DeletionView, response_model_exclude_unset=True)
async def deletion(deletion_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        value=await db.get(CaseDeletion,deletion_id)
        if value is None or value.actor_id!=principal.user_id:raise NotFoundOrForbidden()
        return deletion_view(value)
