import asyncio
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from app.api.dependencies import CurrentUser, SettingsDep
from app.ops.observability import collect_operational_metrics, prometheus_text
from app.workflows.commands import require_permission

from app.api import response_dto as d
router = APIRouter(tags=["ops"])


@router.get("/ops/metrics", response_model=d.OperationalMetricsView, response_model_exclude_unset=True)
async def metrics(principal: CurrentUser, settings: SettingsDep):
    require_permission(principal, "ops_health")
    return await asyncio.to_thread(collect_operational_metrics, settings, tenant_id=principal.tenant_id)


@router.get("/ops/metrics/prometheus", response_class=PlainTextResponse)
async def prometheus(principal: CurrentUser, settings: SettingsDep):
    require_permission(principal, "ops_health")
    report = await asyncio.to_thread(collect_operational_metrics, settings, tenant_id=principal.tenant_id)
    return prometheus_text(report)
