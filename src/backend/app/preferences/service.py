from __future__ import annotations
from app.languages import LanguageCode, requested_language

import re

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text

from app.api import idempotency
from app.api.errors import InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.preferences.models import MemorySuggestion, UserMemory, UserMemoryVersion, UserPreference


class PreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interface_language: LanguageCode
    timezone: str = Field(max_length=80)
    panel_width: int = Field(ge=520, le=900)
    motion: Literal["system", "reduce", "standard"]
    expected_version: int = Field(ge=0)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("请选择有效的 IANA 时区") from exc
        return value


class MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["answer_language", "target_language", "presentation", "terminology"]
    content: str = Field(min_length=1, max_length=120)
    applies_when: str | None = Field(default=None, max_length=300)


class EditMemoryBody(MemoryBody):
    expected_version: int = Field(ge=1)


class DeleteMemoryBody(BaseModel):
    expected_version: int = Field(ge=1)


def preferences_view(row):
    return {"interface_language": row.interface_language if row else "zh-CN", "timezone": row.timezone if row else "Asia/Shanghai",
        "panel_width": row.panel_width if row else 640, "motion": row.motion if row else "system",
        "version": row.version if row else 0, "saved_at": row.saved_at if row else None}


async def get_preferences(db, principal, *, lock=False):
    query = select(UserPreference).where(UserPreference.user_id == principal.user_id, UserPreference.tenant_id == principal.tenant_id)
    if lock:
        # 首次创建也串行化，但 API 角色没有 users UPDATE 权限，不能锁身份表。
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
            {"owner": f"preferences:{principal.tenant_id}:{principal.user_id}"})
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def save_preferences(db, principal, body, key):
    row = await get_preferences(db, principal, lock=True)
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope="PUT /me/preferences", key=key, payload=body.model_dump())
    if outcome.replayed:
        return row
    if (row.version if row else 0) != body.expected_version:
        raise VersionConflict(current=preferences_view(row))
    if row is None:
        row = UserPreference(tenant_id=principal.tenant_id, user_id=principal.user_id, version=0, memory_version=0)
        db.add(row)
    for name, value in body.model_dump(exclude={"expected_version"}).items():
        setattr(row, name, value)
    row.version += 1
    row.saved_at = datetime.now(UTC)
    await db.flush()
    idempotency.complete(outcome.record, {"preference_id": str(row.id)})
    return row


def preview_memory(body):
    reason, field = None, None
    for name, value in (("content", body.content), ("applies_when", body.applies_when or "")):
        if re.search(r"(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|-----BEGIN.+PRIVATE KEY|(?:api[_ -]?key|token|密码|密钥)\s*[:=：]\s*\S+)", value, re.I):
            reason, field = "记忆不能保存密钥、令牌或口令。", name
            break
        if re.search(r"(?:客户|这票|本票|金额|收货人|发货人|税号|地址|手机号|电话号码|账户|银行|身份证|发票号|提单号|订单号)|\b(?:INV|BL|PO)[-_]?\d{4,}", value, re.I):
            reason, field = "记忆只保存稳定表达和术语偏好，不能保存客户或单票事实。", name
            break
    if not reason and body.category in {"answer_language", "target_language"} and requested_language(body.content) is None:
        reason, field = "语言偏好请选择中文、英语、法语、西班牙语或俄语。", "content"
    elif not reason and not body.content.strip():
        reason, field = "偏好内容不能为空。", "content"
    return {"affects": ["以后新回合的回答表达或术语展示", "常用目标语言仅作为建议"],
        "not_affects": ["已启动任务、译本和历史消息", "字段真值、审核与导出门禁"],
        "rejected_reason": reason, "rejected_field": field}


def ensure_memory(body):
    preview = preview_memory(body)
    if preview["rejected_reason"]:
        raise InvalidRequest(preview["rejected_reason"], details={"field": preview["rejected_field"]})


async def bump_memory_version(db, principal):
    row = await get_preferences(db, principal, lock=True)
    if row is None:
        row = UserPreference(tenant_id=principal.tenant_id, user_id=principal.user_id, version=0, memory_version=0,
            interface_language="zh-CN", timezone="Asia/Shanghai", panel_width=640, motion="system")
        db.add(row)
    row.memory_version += 1
    await db.flush()
    return row.memory_version


async def memory_view(db, memory):
    version = (await db.execute(select(UserMemoryVersion).where(UserMemoryVersion.memory_id == memory.id,
        UserMemoryVersion.version == memory.current_version))).scalar_one()
    return {"memory_id": str(memory.id), "category": version.category, "content": version.content,
        "applies_when": version.applies_when, "source": memory.source, "source_label": "手动确认" if memory.source == "manual" else "建议确认",
        "confirmed_at": version.confirmed_at, "version": memory.current_version}


async def load_memory(db, principal, memory_id, *, lock=False):
    query = select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == principal.user_id,
        UserMemory.tenant_id == principal.tenant_id, UserMemory.revoked_at.is_(None))
    if lock:
        query = query.with_for_update()
    memory = (await db.execute(query)).scalar_one_or_none()
    if memory is None:
        raise NotFoundOrForbidden()
    return memory


async def list_memories(db, principal):
    rows = list((await db.execute(select(UserMemory).where(UserMemory.user_id == principal.user_id,
        UserMemory.tenant_id == principal.tenant_id, UserMemory.revoked_at.is_(None)).order_by(UserMemory.created_at))).scalars())
    preference = await get_preferences(db, principal)
    return {"items": [await memory_view(db, row) for row in rows], "memory_version": preference.memory_version if preference else 0}


async def create_memory(db, principal, body, key, *, source="manual", conversation_id=None):
    ensure_memory(body)
    await get_preferences(db, principal, lock=True)
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope="POST /me/memories", key=key, payload={**body.model_dump(), "source": source})
    if outcome.replayed:
        return await load_memory(db, principal, UUID(outcome.record.result_ref["memory_id"]))
    memory = UserMemory(tenant_id=principal.tenant_id, user_id=principal.user_id, current_version=1, source=source,
        source_conversation_id=conversation_id)
    db.add(memory)
    await db.flush()
    db.add(UserMemoryVersion(tenant_id=principal.tenant_id, memory_id=memory.id, version=1,
        **body.model_dump(), confirmed_at=datetime.now(UTC)))
    await bump_memory_version(db, principal)
    idempotency.complete(outcome.record, {"memory_id": str(memory.id)})
    return memory


async def edit_memory(db, principal, memory_id, body, key, *, delete=False):
    await get_preferences(db, principal, lock=True)
    query = select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == principal.user_id).with_for_update()
    memory = (await db.execute(query)).scalar_one_or_none()
    if memory is None:
        raise NotFoundOrForbidden()
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope=f"{'DELETE' if delete else 'PATCH'} /me/memories/{memory_id}", key=key, payload=body.model_dump())
    if outcome.replayed:
        return memory
    if memory.revoked_at or memory.current_version != body.expected_version:
        raise VersionConflict(current=await memory_view(db, memory))
    if delete:
        memory.revoked_at = datetime.now(UTC)
    else:
        ensure_memory(body)
        memory.current_version += 1
        db.add(UserMemoryVersion(tenant_id=principal.tenant_id, memory_id=memory.id, version=memory.current_version,
            **body.model_dump(exclude={"expected_version"}), confirmed_at=datetime.now(UTC)))
    await bump_memory_version(db, principal)
    idempotency.complete(outcome.record, {"memory_id": str(memory.id)})
    return memory


async def list_suggestions(db, principal):
    rows = list((await db.execute(select(MemorySuggestion).where(MemorySuggestion.user_id == principal.user_id,
        MemorySuggestion.status == "pending", MemorySuggestion.expires_at > datetime.now(UTC)).order_by(MemorySuggestion.created_at.desc()))).scalars())
    from app.conversation.models import Conversation
    conversation_ids = {row.conversation_id for row in rows if row.conversation_id}
    sources = {row.id: row for row in (await db.execute(select(Conversation).where(Conversation.id.in_(conversation_ids),
        Conversation.owner_user_id == principal.user_id, Conversation.tenant_id == principal.tenant_id))).scalars()} if conversation_ids else {}
    items = []
    for row in rows:
        source = sources.get(row.conversation_id)
        restricted = bool(row.conversation_id and source is None)
        if source and source.case_id:
            from app.config import get_settings
            from app.documents.retention import CaseGone, load_case_for_read
            try:
                await load_case_for_read(db, source.case_id, user_id=principal.user_id, settings=get_settings())
            except (NotFoundOrForbidden, CaseGone):
                restricted = True
        items.append({"suggestion_id": str(row.id), "category": row.category, "content": row.content, "applies_when": row.applies_when,
            "from_conversation": source.title if source and not restricted else None, "source_restricted": restricted,
            "origin": row.origin, "created_at": row.created_at})
    return {"items": items}


async def resolve_suggestion(db, principal, suggestion_id, key, *, ignore=False):
    await get_preferences(db, principal, lock=True)
    suggestion = (await db.execute(select(MemorySuggestion).where(MemorySuggestion.id == suggestion_id,
        MemorySuggestion.user_id == principal.user_id).with_for_update())).scalar_one_or_none()
    if suggestion is None:
        raise NotFoundOrForbidden()
    if ignore and suggestion.status == "ignored":
        return None
    if suggestion.status == "confirmed" and suggestion.memory_id and not ignore:
        return await load_memory(db, principal, suggestion.memory_id)
    if suggestion.status != "pending" or suggestion.expires_at < datetime.now(UTC):
        raise InvalidRequest("建议已处理或已过期")
    if ignore:
        suggestion.status = "ignored"
        return None
    memory = await create_memory(db, principal, MemoryBody(category=suggestion.category, content=suggestion.content,
        applies_when=suggestion.applies_when), "suggestion:" + str(suggestion.id), source="suggestion", conversation_id=suggestion.conversation_id)
    suggestion.status, suggestion.memory_id = "confirmed", memory.id
    return memory


async def propose_memory(db, principal, body, *, conversation_id=None, parent_turn_id=None):
    ensure_memory(body)
    suggestion = MemorySuggestion(tenant_id=principal.tenant_id, user_id=principal.user_id, conversation_id=conversation_id,
        parent_turn_id=parent_turn_id, **body.model_dump(), origin="explicit_request", status="pending",
        expires_at=datetime.now(UTC)+timedelta(days=30))
    db.add(suggestion)
    await db.flush()
    return suggestion


def timezone_options():
    now = datetime.now(UTC)
    return [{"id": name, "label": name, "offset": now.astimezone(ZoneInfo(name)).strftime("%z")} for name in sorted(available_timezones()) if not name.startswith(("posix/", "right/"))]
