from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api import response_dto as d
from app.api import idempotency
from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser, set_tenant_context
from app.api.routers.workflows import _begin
from app.ops import service
from app.workflows import commands as wc

router=APIRouter(tags=["ops"])
IdempotencyKey=Annotated[str,Header(alias="Idempotency-Key",min_length=8,max_length=80)]


class ReconcileBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_step_version:int=Field(ge=1)
    decision:str
    evidence_reference:str=Field(default="",max_length=2000)
    reason:str=Field(min_length=1,max_length=2000)


class MigrationBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    candidate_config_fingerprint:str=Field(min_length=64,max_length=64)
    expected_execution_epoch:int=Field(ge=1)
    reason:str=Field(min_length=1,max_length=2000)


@router.get("/ops/steps/{step_id}", response_model=d.OpsStepView, response_model_exclude_unset=True)
async def get_step(step_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    wc.require_permission(principal,"ops_reconcile")
    async with db.begin():
        await set_tenant_context(db,principal)
        step,_,_=await service.load_step(db,principal,settings,step_id)
        return await service.step_view(db,step)


@router.post("/ops/steps/{step_id}/reconcile", response_model=d.OpsStepView, response_model_exclude_unset=True)
async def reconcile(step_id:UUID,body:ReconcileBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    wc.require_permission(principal,"ops_reconcile")
    async with db.begin():
        await set_tenant_context(db,principal)
        outcome=await _begin(db,principal,scope=f"reconcile:{step_id}",key=idempotency_key,payload=body.model_dump())
        if outcome.replayed:
            step,_,_=await service.load_step(db,principal,settings,step_id)
            return await service.step_view(db,step)
        result=await service.reconcile_step(db,principal,settings,step_id,body)
        idempotency.complete(outcome.record,{"step_execution_id":str(step_id)})
        return result


@router.get("/ops/runs/{run_id}/model-migration-options", response_model=d.MigrationOptionsView, response_model_exclude_unset=True)
async def migration_options(run_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        result,_=await service.migration_options(db,principal,settings,run_id)
        return result


@router.post("/ops/runs/{run_id}/migrate-model", response_model=d.MigrationAcceptedView, response_model_exclude_unset=True)
async def migrate(run_id:UUID,body:MigrationBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    wc.require_permission(principal,"ops_migrate_model")
    async with db.begin():
        await set_tenant_context(db,principal)
        outcome=await _begin(db,principal,scope=f"migrate:{run_id}",key=idempotency_key,payload=body.model_dump())
        if outcome.replayed:
            await wc.load_run(db,principal,settings,run_id)
            return outcome.record.result_ref
        result=await service.migrate_model(db,principal,settings,run_id,body)
        idempotency.complete(outcome.record,result)
        return result


@router.get("/ops/health", response_model=d.OpsHealthView, response_model_exclude_unset=True)
async def health(principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        return await service.health_probe(db,principal,settings)


@router.get("/health/ready", response_model=d.HealthReadyView, response_model_exclude_unset=True)
async def ready(request:Request,settings:SettingsDep):
    """匿名部署探针只返回总体；服务细节仍由有ops_health权限的接口读取。"""
    # Do not depend on authentication/DB lookup: failed DB must produce a useful
    # deployment status without disclosing roles, endpoints or failure internals.
    from datetime import UTC,datetime
    import asyncio
    try:
        async with request.app.state.session_factory() as db:
            report=await asyncio.wait_for(service.readiness_probe(db,settings),timeout=45)
        status=report["status"];probed_at=report["probed_at"]
    except Exception:
        status="degraded";probed_at=datetime.now(UTC)
    result = d.HealthReadyView(status=status, probed_at=probed_at, paid_requests=0)
    return JSONResponse(status_code=200 if status=="ready" else 503,content=result.model_dump(mode="json"))
