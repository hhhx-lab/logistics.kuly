"""
幂等入口（契约第 2 节）。

唯一键为 tenant + actor + 动作作用域 + 键；请求体规范化后取 SHA-256。
同键同内容返回先前结果，同键异内容返回 409，仍在执行返回已有操作 ID。
只存受控结果 ID 与小响应，不存原件正文。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import IdempotencyConflict
from app.documents.models import IdempotencyRecord


def request_digest(payload: Any) -> str:
    """规范化序列化后取哈希：键顺序与空白差异不应被当成不同请求。"""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class IdempotencyOutcome:
    record: IdempotencyRecord
    replayed: bool


async def begin(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    scope: str,
    key: str,
    payload: Any,
) -> IdempotencyOutcome:
    """
    在事务内锁定幂等键。

    `replayed=True` 表示这是重复请求，调用方应直接返回 `record.result_ref`
    而不是再执行一次业务动作。
    """
    digest = request_digest(payload)
    existing = (
        await session.execute(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.key == key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.request_hash != digest:
            raise IdempotencyConflict()
        return IdempotencyOutcome(record=existing, replayed=existing.status == "completed")

    record = IdempotencyRecord(
        tenant_id=tenant_id,
        actor_id=actor_id,
        scope=scope,
        key=key,
        request_hash=digest,
        status="in_progress",
    )
    session.add(record)
    await session.flush()
    return IdempotencyOutcome(record=record, replayed=False)


def complete(record: IdempotencyRecord, result_ref: dict[str, Any]) -> None:
    record.status = "completed"
    record.result_ref = result_ref
