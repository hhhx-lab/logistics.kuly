"""R13–R16：审核门禁、整组导出与只读成功快照。"""
from __future__ import annotations
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter,Header,Response,Request
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from app.api.dependencies import CurrentUser,DbSession,SettingsDep,WriteUser,set_tenant_context
from app.api.routers.workflows import _begin
from app.api import response_dto as d
from app.api import idempotency
from app.api.errors import NotFoundOrForbidden
from app.documents.models import StoredObject
from app.documents.retention import load_case_for_read
from app.exports import service
from app.exports.models import ExportAttempt,ExportArtifact,ExportSnapshot
from app.infra.object_store import ObjectStore
from app.workflows import commands as wc

router=APIRouter(tags=["exports"])
IdempotencyKey=Annotated[str,Header(alias="Idempotency-Key",min_length=8,max_length=80)]


class ConfirmBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_review_fingerprint:str=Field(min_length=64,max_length=64)
    expected_business_revision_id:UUID
    expected_translation_revision_id:UUID
    expected_compatibility_id:UUID|None=None


class ExportBody(ConfirmBody):
    confirmation_id:UUID
    include_source_pdf:bool=True


class RetryBody(ConfirmBody):
    confirmation_id:UUID|None=None


@router.get("/runs/{run_id}/review-checklist", response_model=d.ReviewChecklistView, response_model_exclude_unset=True)
async def checklist(run_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        run,_=await wc.load_run(db,principal,settings,run_id)
        return service.public_checklist(await service.checklist(db,run,principal))


async def _write(scope,key,body,principal,db,settings,run_id,callback):
    async with db.begin():
        await set_tenant_context(db,principal)
        outcome=await _begin(db,principal,scope=scope,key=key,payload=body.model_dump(mode="json"))
        await wc.load_run(db,principal,settings,run_id)
        if outcome.replayed:return outcome.record.result_ref
        result=await callback()
        from fastapi.encoders import jsonable_encoder
        idempotency.complete(outcome.record,jsonable_encoder(result))
        return result


@router.post("/runs/{run_id}/review-confirmations", response_model=d.ConfirmationView, response_model_exclude_unset=True)
async def confirm(run_id:UUID,body:ConfirmBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    return await _write(f"POST /runs/{run_id}/review-confirmations",idempotency_key,body,principal,db,settings,run_id,
        lambda:service.confirm(db,principal,settings,run_id,body))


@router.post("/runs/{run_id}/exports", response_model=d.ExportAttemptView, response_model_exclude_unset=True)
async def start_export(run_id:UUID,body:ExportBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    return await _write(f"POST /runs/{run_id}/exports",idempotency_key,body,principal,db,settings,run_id,
        lambda:service.start_export(db,principal,settings,run_id,body))


@router.get("/runs/{run_id}/export-attempts/{attempt_id}", response_model=d.ExportAttemptView, response_model_exclude_unset=True)
async def attempt(run_id:UUID,attempt_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        await wc.load_run(db,principal,settings,run_id)
        value=await db.get(ExportAttempt,attempt_id)
        if value is None or value.run_id!=run_id:raise NotFoundOrForbidden()
        return await service.attempt_view(db,value)


@router.post("/runs/{run_id}/export-attempts/{attempt_id}/retry", response_model=d.ExportAttemptView, response_model_exclude_unset=True)
async def retry(run_id:UUID,attempt_id:UUID,body:RetryBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    return await _write(f"POST /runs/{run_id}/export-attempts/{attempt_id}/retry",idempotency_key,body,principal,db,settings,run_id,
        lambda:service.retry_export(db,principal,settings,run_id,attempt_id,body))


@router.get("/runs/{run_id}/snapshots", response_model=d.PageView[d.SnapshotView], response_model_exclude_unset=True)
async def snapshots(run_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        await wc.load_run(db,principal,settings,run_id)
        values=(await db.execute(select(ExportSnapshot).where(ExportSnapshot.run_id==run_id).order_by(ExportSnapshot.exported_at.desc()))).scalars().all()
        return {"items":[await service.snapshot_view(db,x) for x in values]}


@router.get("/snapshots/{snapshot_id}", response_model=d.SnapshotView, response_model_exclude_unset=True)
async def snapshot(snapshot_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        value=await db.get(ExportSnapshot,snapshot_id)
        if value is None:raise NotFoundOrForbidden()
        await load_case_for_read(db,value.case_id,user_id=principal.user_id,settings=settings)
        return await service.snapshot_view(db,value)


@router.get("/export-artifacts/{artifact_id}/content")
async def content(artifact_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,request:Request,
    range_header:Annotated[str|None,Header(alias="Range")]=None):
    async with db.begin():
        await set_tenant_context(db,principal)
        value=await db.get(ExportArtifact,artifact_id)
        if value is None:raise NotFoundOrForbidden()
        await load_case_for_read(db,value.case_id,user_id=principal.user_id,settings=settings)
        snapshot=await db.get(ExportSnapshot,value.snapshot_id)
        obj=await db.get(StoredObject,value.object_id)
        if snapshot is None or obj is None or not obj.is_visible:raise NotFoundOrForbidden()
        key,size,mime=obj.object_key,value.size_bytes,obj.mime
    headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff","Accept-Ranges":"bytes"}
    from urllib.parse import quote
    headers["Content-Disposition"]=f"attachment; filename*=UTF-8''{quote(value.filename)}"
    from app.api.routers.uploads import _parse_range
    try:bounds=_parse_range(range_header,size)
    except ValueError:return Response(status_code=416,headers={**headers,"Content-Range":f"bytes */{size}"})
    if bounds:
        start,end=bounds
    else:start,end=0,size-1
    from fastapi.responses import StreamingResponse
    from app.infra.content_stream import prepared_bytes
    store=ObjectStore(settings)
    stream=await prepared_bytes(request,principal,settings,value.case_id,store=store,key=key,start=start,end=end,expected_size=size,close_store=True)
    return StreamingResponse(stream,
        status_code=206 if bounds else 200,media_type=mime,headers={**headers,"Content-Length":str(end-start+1),**({"Content-Range":f"bytes {start}-{end}/{size}"} if bounds else {})})
