"""
三条持久事件流的 SSE 推送（契约 2.1 节、数据方案 6.1 节）。

原则：
- 数据库事件是唯一进度真值；SSE 只复播已提交的事件行，不从 Redis 或图内部流取历史。
- 先快照后订阅：客户端带 `after_seq`（首次）或 `Last-Event-ID`（自动重连），
  服务端始终查询 `seq > cursor ORDER BY seq`，快照与订阅之间的新事件不会丢。
- 不持有跨几分钟的数据库事务：每轮短查询都重新建立租户上下文并**重新鉴权**，
  权限撤销、会话失效或 Case 删除后下一轮即关闭流。
- 心跳不占序号、不含业务正文。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.api.errors import ApiError
from sqlalchemy.ext.asyncio import AsyncSession

# 单轮最多补发的事件数；过大的积压分多轮发，避免一次查询过重
BATCH_LIMIT = 200
HEARTBEAT_SECONDS = 15


@dataclass(frozen=True)
class StreamEvent:
    seq: int
    event_type: str
    data: dict[str, Any]


class StreamClosed(Exception):
    """授权失效、对象被删或进入终态时由拉取函数抛出，正常结束流。"""


# 拉取函数：在已建立租户上下文的事务里取 `seq > after_seq` 的事件；
# 返回 (事件列表, 是否已到终态)。每轮都要先重新鉴权，失败抛 StreamClosed。
FetchEvents = Callable[[AsyncSession, int], Awaitable[tuple[list[StreamEvent], bool]]]


def resolve_cursor(after_seq: int | None, last_event_id: str | None) -> int:
    """
    EventSource 无法自定义请求头，首次订阅用 query 参数；
    自动重连时浏览器带 `Last-Event-ID`。两者同时存在时取较新的那个。
    """
    candidates = [after_seq or 0]
    if last_event_id:
        try:
            candidates.append(int(last_event_id))
        except ValueError:
            # 非法游标不猜测，退回 query 值；客户端缺号时会自行重取快照
            pass
    return max(0, *candidates)


def _format(event: StreamEvent) -> str:
    payload = json.dumps(event.data, ensure_ascii=False, default=_json_default)
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {payload}\n\n"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def gap_event(cursor: int) -> str:
    """游标对应的事件已按保留策略清理时，明确要求客户端重取快照，不能安静跳到最新。"""
    data = json.dumps({"seq": -1, "cursor": cursor, "message": "事件已不可补齐，请重新读取快照"}, ensure_ascii=False)
    return f"event: EVENT_GAP\ndata: {data}\n\n"


def sse_response(
    request: Request,
    *,
    cursor: int,
    fetch: FetchEvents,
    open_session: Callable[[], AsyncSession],
    poll_seconds: float,
    initial: tuple[list[StreamEvent], bool] | None = None,
) -> StreamingResponse:
    async def generator() -> AsyncIterator[str]:
        current = cursor
        idle = 0.0
        first = initial
        while True:
            if await request.is_disconnected():
                return
            try:
                # 每轮一个短会话：不跨轮持有事务或连接
                if first is not None:
                    events, terminal = first
                    first = None
                else:
                    async with open_session() as session:
                        events, terminal = await fetch(session, current)
            except (StreamClosed, ApiError):
                return

            for event in events:
                # 服务端也按序去重，防止批次边界重复
                if event.seq <= current:
                    continue
                if event.seq > current + 1 and current > 0:
                    # 中间缺号说明历史已被清理，交给客户端重取快照
                    yield gap_event(current)
                    return
                current = event.seq
                yield _format(event)

            if terminal and len(events) < BATCH_LIMIT:
                return
            if len(events) >= BATCH_LIMIT:
                # 还有积压，立即拉下一批
                continue

            await asyncio.sleep(poll_seconds)
            idle += poll_seconds
            if idle >= HEARTBEAT_SECONDS:
                idle = 0.0
                # 注释行心跳：不占 seq，不含业务数据
                yield ": heartbeat\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # 反向代理须关闭缓冲，否则事件会被攒批
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
