"""W01–W06 工作流接口；历史事件与实时 SSE 分开读取。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.api import response_dto as d
from app.api import idempotency
from app.api.dto import RunView, TaskDraftView, WorkflowListView
from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser, set_tenant_context
from app.router.contracts import SourceVersion
from app.workflows import commands
from app.workflows.models import InputRequest, InputRequestStatus, RunEvent

router = APIRouter(tags=["workflows"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)]


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepareBody(CommandBody):
    case_id: UUID
    workflow_type: str
    workflow_version: int = Field(default=1, ge=1)
    source_versions: list[SourceVersion]
    draft_parameters: dict | None = None
    conversation_id: UUID | None = None


class PatchBody(CommandBody):
    expected_draft_version: int = Field(ge=1)
    draft_parameters: dict | None = None
    source_versions: list[SourceVersion] | None = None
    source_version_document_ids: list[UUID] | None = None


class DiscardBody(CommandBody):
    expected_draft_version: int = Field(ge=1)


class StartBody(CommandBody):
    draft_task_id: UUID
    expected_draft_version: int = Field(ge=1)


class ResolveBody(CommandBody):
    selected_option_ids: list[str] = Field(min_length=1, max_length=100)
    cargo_profile_ids: list[str] | None = Field(default=None, max_length=16)
    reason: str | None = Field(default=None, max_length=2000)
    expected_request_version: int = Field(ge=1)
    expected_parse_artifact_id: UUID
    expected_business_revision_id: UUID | None


async def _begin(db, principal, *, scope: str, key: str, payload):
    if len(scope)>80:
        scope="action:"+idempotency.request_digest(scope)
    # 幂等记录尚不存在时也要串行化，不能把唯一索引冲突暴露成 500。
    token = f"{principal.tenant_id}:{principal.user_id}:{scope}:{key}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:token, 0))"), {"token": token})
    return await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
                                   scope=scope, key=key, payload=payload)


@router.get("/workflows", response_model=WorkflowListView, response_model_exclude_unset=True)
async def list_workflows(principal: CurrentUser, settings: SettingsDep) -> dict:
    return {"items": commands.workflows_view(settings)}


@router.post("/task-drafts", response_model=TaskDraftView, response_model_exclude_unset=True)
async def prepare(body: PrepareBody, principal: WriteUser, db: DbSession, settings: SettingsDep,
                  idempotency_key: IdempotencyKey) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await _begin(db, principal, scope="POST /task-drafts", key=idempotency_key, payload=body.model_dump(mode="json"))
        if outcome.replayed:
            draft, _ = await commands.load_draft(db, principal, settings, UUID(outcome.record.result_ref["draft_task_id"]))
        else:
            draft = await commands.prepare_draft(db, principal, settings, **body.model_dump())
            idempotency.complete(outcome.record, {"draft_task_id": str(draft.id)})
        return commands.draft_view(draft)


@router.get("/task-drafts/{draft_id}", response_model=TaskDraftView, response_model_exclude_unset=True)
async def get_draft(draft_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        draft, _ = await commands.load_draft(db, principal, settings, draft_id)
        return commands.draft_view(draft)


@router.patch("/task-drafts/{draft_id}", response_model=TaskDraftView, response_model_exclude_unset=True)
async def patch(draft_id: UUID, body: PatchBody, principal: WriteUser, db: DbSession,
                settings: SettingsDep, idempotency_key: IdempotencyKey) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await _begin(db, principal, scope=f"PATCH /task-drafts/{draft_id}", key=idempotency_key, payload=body.model_dump(mode="json"))
        if outcome.replayed:
            draft, _ = await commands.load_draft(db, principal, settings, draft_id)
        else:
            draft = await commands.patch_draft(db, principal, settings, draft_id=draft_id, **body.model_dump())
            idempotency.complete(outcome.record, {"draft_task_id": str(draft.id)})
        return commands.draft_view(draft)


@router.delete("/task-drafts/{draft_id}", status_code=204)
async def discard(draft_id: UUID, body: DiscardBody, principal: WriteUser, db: DbSession,
                  settings: SettingsDep, idempotency_key: IdempotencyKey) -> Response:
    async with db.begin():
        await set_tenant_context(db, principal)
        commands.require_permission(principal, "run_start")
        outcome = await _begin(db, principal, scope=f"DELETE /task-drafts/{draft_id}", key=idempotency_key, payload=body.model_dump())
        draft, _ = await commands.load_draft(db, principal, settings, draft_id, write=True, allow_discarded=True)
        if not outcome.replayed:
            commands.check_draft_edit(draft, body.expected_draft_version)
            if not draft.discarded_at:
                from datetime import UTC, datetime
                draft.discarded_at = datetime.now(UTC)
                draft.draft_version += 1
            idempotency.complete(outcome.record, {"draft_task_id": str(draft.id)})
        return Response(status_code=204)


@router.post("/runs", response_model=RunView, response_model_exclude_unset=True)
async def start(body: StartBody, principal: WriteUser, db: DbSession, settings: SettingsDep,
                idempotency_key: IdempotencyKey) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await _begin(db, principal, scope="POST /runs", key=idempotency_key, payload=body.model_dump(mode="json"))
        if outcome.replayed:
            run, case = await commands.load_run(db, principal, settings, UUID(outcome.record.result_ref["run_id"]))
        else:
            run = await commands.start_run(db, principal, settings, **body.model_dump(), idempotency_key=idempotency_key)
            _, case = await commands.load_run(db, principal, settings, run.id)
            idempotency.complete(outcome.record, {"run_id": str(run.id)})
        return await commands.run_view(db, run, case, principal)


@router.get("/runs/{run_id}", response_model=RunView, response_model_exclude_unset=True)
async def get_run(run_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        run, case = await commands.load_run(db, principal, settings, run_id)
        return await commands.run_view(db, run, case, principal)


@router.get("/runs/{run_id}/events", response_model=d.RunEventsView, response_model_exclude_unset=True)
async def get_events(run_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep,
                     after_seq: Annotated[int, Query(ge=0)] = 0,
                     limit: Annotated[int, Query(ge=1, le=200)] = 100) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        run, _ = await commands.load_run(db, principal, settings, run_id)
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
                        .order_by(RunEvent.seq).limit(limit + 1))).scalars().all()
        items = [{"seq": e.seq, "occurred_at": e.occurred_at, "status": e.status,
                  "stage": e.stage, "message": e.message, "event_type": e.event_type,
                  "kind": e.kind, "completed": e.completed, "total": e.total, "unit": e.unit,
                  "payload": e.payload} for e in events[:limit]]
        return {"items": items, "last_seq": run.next_event_seq,
                "next_cursor": events[limit - 1].seq if len(events) > limit else None}


@router.get("/runs/{run_id}/input-requests", response_model=d.PageView[d.InputRequestView], response_model_exclude_unset=True)
async def get_inputs(run_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        await commands.load_run(db, principal, settings, run_id)
        items = (await db.execute(select(InputRequest).where(InputRequest.run_id == run_id,
                       InputRequest.status == InputRequestStatus.ACTIVE).order_by(InputRequest.created_at))).scalars().all()
        return {"items": [await commands.input_request_view(db, item) for item in items], "has_more": False, "next_cursor": None}


@router.post("/input-requests/{request_id}/resolve", response_model=RunView, response_model_exclude_unset=True)
async def resolve(request_id: UUID, body: ResolveBody, principal: WriteUser, db: DbSession,
                  settings: SettingsDep, idempotency_key: IdempotencyKey) -> dict:
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await _begin(db, principal, scope=f"POST /input-requests/{request_id}/resolve", key=idempotency_key, payload=body.model_dump(mode="json"))
        if outcome.replayed:
            run, case = await commands.load_run(db, principal, settings, UUID(outcome.record.result_ref["run_id"]))
        else:
            run, case = await commands.resolve_input(db, principal, settings, request_id=request_id, **body.model_dump())
            idempotency.complete(outcome.record, {"run_id": str(run.id)})
        return await commands.run_view(db, run, case, principal)


async def _run_action(run_id, action, principal, db, settings, key):
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await _begin(db, principal, scope=f"POST /runs/{run_id}/{action}", key=key, payload={})
        if outcome.replayed:
            run, case = await commands.load_run(db, principal, settings, run_id)
        else:
            command = commands.cancel_run if action == "cancel" else commands.retry_run
            run, case = await command(db, principal, settings, run_id)
            idempotency.complete(outcome.record, {"run_id": str(run.id)})
        return await commands.run_view(db, run, case, principal)


@router.post("/runs/{run_id}/cancel", response_model=RunView, response_model_exclude_unset=True)
async def cancel(run_id: UUID, principal: WriteUser, db: DbSession, settings: SettingsDep,
                 idempotency_key: IdempotencyKey) -> dict:
    return await _run_action(run_id, "cancel", principal, db, settings, idempotency_key)


@router.post("/runs/{run_id}/retry", response_model=RunView, response_model_exclude_unset=True)
async def retry(run_id: UUID, principal: WriteUser, db: DbSession, settings: SettingsDep,
                idempotency_key: IdempotencyKey) -> dict:
    return await _run_action(run_id, "retry", principal, db, settings, idempotency_key)
