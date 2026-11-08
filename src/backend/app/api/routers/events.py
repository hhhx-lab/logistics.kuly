"""
事件流 SSE 路由。

三条流各有独立 seq，不共用游标：
- `GET /upload-sessions/{id}/events/stream`（U04）
- `GET /runs/{id}/events/stream`（W04）
- `GET /conversations/{cid}/turns/{tid}/events/stream`（C05）

JSON 补齐接口（不带 `/stream` 后缀）保留给轮询降级与断线核对；
两者读同一张事件表，结果一致。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, SettingsDep, set_tenant_context
from app.api.errors import NotFoundOrForbidden, Unauthenticated
from app.api.sse import BATCH_LIMIT, StreamEvent, resolve_cursor, sse_response
from app.conversation.models import AssistantContinuation, Conversation, ConversationTurn, TurnEvent
from app.documents.models import UploadEvent, UploadSession
from app.documents.retention import load_case_for_read
from app.identity.service import Principal, SESSION_COOKIE, resolve_session
from app.workflows.models import Run, RunEvent, RunStatus

router = APIRouter(tags=["events"])

LastEventId = Annotated[str | None, Header(alias="Last-Event-ID")]

_RUN_TERMINAL = {RunStatus.EXPORTED, RunStatus.INDEXED, RunStatus.FAILED_FINAL, RunStatus.CANCELLED}


def _session_opener(request: Request):
    return request.app.state.session_factory


async def _authorize_case(session: AsyncSession, principal: Principal, case_id: UUID, settings) -> None:
    await load_case_for_read(session, case_id, user_id=principal.user_id, settings=settings)


async def _authorize_stream(session: AsyncSession, request: Request, principal: Principal) -> None:
    current = await resolve_session(session, request.cookies.get(SESSION_COOKIE))
    if current is None or (current.user_id, current.tenant_id, current.session_id) != (
        principal.user_id, principal.tenant_id, principal.session_id
    ):
        raise Unauthenticated()
    await set_tenant_context(session, current)


async def _start_stream(request, *, cursor, fetch, open_session, poll_seconds):
    # 首次读取在响应头之前完成，使拒绝访问保持标准错误封套。
    async with open_session() as session:
        initial = await fetch(session, cursor)
    return sse_response(request, cursor=cursor, fetch=fetch, open_session=open_session,
        poll_seconds=poll_seconds, initial=initial)


@router.get("/upload-sessions/{session_id}/events/stream")
async def stream_upload_events(
    session_id: UUID,
    request: Request,
    principal: CurrentUser,
    settings: SettingsDep,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: LastEventId = None,
):
    async def fetch(session: AsyncSession, cursor: int) -> tuple[list[StreamEvent], bool]:
        async with session.begin():
            await _authorize_stream(session, request, principal)
            upload = await session.get(UploadSession, session_id)
            if upload is None:
                raise NotFoundOrForbidden()
            await _authorize_case(session, principal, upload.case_id, settings)
            rows = (
                await session.execute(
                    select(UploadEvent)
                    .where(UploadEvent.upload_session_id == session_id, UploadEvent.seq > cursor)
                    .order_by(UploadEvent.seq)
                    .limit(BATCH_LIMIT)
                )
            ).scalars()
            events = [
                StreamEvent(
                    seq=e.seq,
                    event_type=e.event_type,
                    data={
                        "seq": e.seq,
                        "occurred_at": e.occurred_at,
                        "upload_file_id": str(e.upload_file_id) if e.upload_file_id else None,
                        "status": e.status,
                        "message": e.message,
                    },
                )
                for e in rows
            ]
            return events, upload.status.value in {"completed", "partial", "expired"}

    return await _start_stream(
        request,
        cursor=resolve_cursor(after_seq, last_event_id),
        fetch=fetch,
        open_session=_session_opener(request),
        poll_seconds=float(settings.event_poll_seconds),
    )


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(
    run_id: UUID,
    request: Request,
    principal: CurrentUser,
    settings: SettingsDep,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: LastEventId = None,
):
    async def fetch(session: AsyncSession, cursor: int) -> tuple[list[StreamEvent], bool]:
        async with session.begin():
            await _authorize_stream(session, request, principal)
            run = await session.get(Run, run_id)
            if run is None:
                raise NotFoundOrForbidden()
            await _authorize_case(session, principal, run.case_id, settings)
            rows = (
                await session.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                    .order_by(RunEvent.seq)
                    .limit(BATCH_LIMIT)
                )
            ).scalars()
            events = []
            for e in rows:
                data = {
                    "seq": e.seq,
                    "occurred_at": e.occurred_at,
                    "status": e.status,
                    "stage": e.stage,
                    "message": e.message,
                }
                # 只有分母可靠时才带进度数值
                if e.total is not None and e.completed is not None and e.unit:
                    data.update(completed=e.completed, total=e.total, unit=e.unit)
                if e.payload:
                    data["payload"] = e.payload
                events.append(StreamEvent(seq=e.seq, event_type=e.event_type, data=data))
            # 终态 Run 仍可能因修订回到处理中，因此只在「终态且无新事件」时结束流
            return events, run.status in _RUN_TERMINAL
    return await _start_stream(
        request,
        cursor=resolve_cursor(after_seq, last_event_id),
        fetch=fetch,
        open_session=_session_opener(request),
        poll_seconds=float(settings.event_poll_seconds),
    )


@router.get("/conversations/{conversation_id}/turns/{turn_id}/events/stream")
async def stream_turn_events(
    conversation_id: UUID,
    turn_id: UUID,
    request: Request,
    principal: CurrentUser,
    settings: SettingsDep,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: LastEventId = None,
):
    async def fetch(session: AsyncSession, cursor: int) -> tuple[list[StreamEvent], bool]:
        async with session.begin():
            await _authorize_stream(session, request, principal)
            conv = await session.get(Conversation, conversation_id)
            turn = await session.get(ConversationTurn, turn_id)
            if conv is None or turn is None or turn.conversation_id != conv.id or conv.owner_user_id != principal.user_id:
                raise NotFoundOrForbidden()
            if conv.case_id is not None:
                await _authorize_case(session, principal, conv.case_id, settings)
            rows = (
                await session.execute(
                    select(TurnEvent)
                    .where(TurnEvent.turn_id == turn_id, TurnEvent.seq > cursor)
                    .order_by(TurnEvent.seq)
                    .limit(BATCH_LIMIT)
                )
            ).scalars()
            events = [
                StreamEvent(
                    seq=e.seq,
                    event_type=e.event_type,
                    data={"seq": e.seq, "occurred_at": e.occurred_at, "message_id": str(e.message_id) if e.message_id else None, **e.payload},
                )
                for e in rows
            ]
            # 续答仍挂在原回合上：回合 completed 但续答未终结时流不能关
            continuation_open = (await session.execute(select(AssistantContinuation.id).where(AssistantContinuation.parent_turn_id == turn.id, AssistantContinuation.status.in_(["waiting", "awaiting_retry", "awaiting_reconcile", "running"])).limit(1))).scalar_one_or_none() is not None
            terminal = turn.status in {"completed", "interrupted", "failed"} and not continuation_open
            return events, terminal and not events

    return await _start_stream(
        request,
        cursor=resolve_cursor(after_seq, last_event_id),
        fetch=fetch,
        open_session=_session_opener(request),
        poll_seconds=float(settings.chat_stream_poll_seconds),
    )
