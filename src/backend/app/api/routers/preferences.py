from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header

from app.api.dependencies import CurrentUser, DbSession, WriteUser, set_tenant_context
from app.preferences import service

from app.api import response_dto as d
router = APIRouter(tags=["preferences"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=80)]


@router.get("/me/preferences", response_model=d.PreferencesView, response_model_exclude_unset=True)
async def get(principal: CurrentUser, db: DbSession):
    async with db.begin():
        await set_tenant_context(db, principal)
        return service.preferences_view(await service.get_preferences(db, principal))


@router.put("/me/preferences", response_model=d.PreferencesView, response_model_exclude_unset=True)
async def save(body: service.PreferencesBody, principal: WriteUser, db: DbSession, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return service.preferences_view(await service.save_preferences(db, principal, body, idempotency_key))


@router.get("/me/timezones", response_model=d.PageView[d.TimezoneView], response_model_exclude_unset=True)
async def timezones(principal: CurrentUser):
    return {"items": service.timezone_options()}


@router.get("/me/memories", response_model=d.MemoryListView, response_model_exclude_unset=True)
async def memories(principal: CurrentUser, db: DbSession):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.list_memories(db, principal)


@router.post("/me/memories/preview", response_model=d.MemoryPreviewView, response_model_exclude_unset=True)
async def preview(body: service.MemoryBody, principal: WriteUser):
    return service.preview_memory(body)


@router.post("/me/memories", status_code=201, response_model=d.MemoryView, response_model_exclude_unset=True)
async def create(body: service.MemoryBody, principal: WriteUser, db: DbSession, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.memory_view(db, await service.create_memory(db, principal, body, idempotency_key))


@router.patch("/me/memories/{memory_id}", response_model=d.MemoryView, response_model_exclude_unset=True)
async def edit(memory_id: UUID, body: service.EditMemoryBody, principal: WriteUser, db: DbSession, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.memory_view(db, await service.edit_memory(db, principal, memory_id, body, idempotency_key))


@router.post("/me/memories/{memory_id}/delete", response_model=d.OkView, response_model_exclude_unset=True)
async def delete(memory_id: UUID, body: service.DeleteMemoryBody, principal: WriteUser, db: DbSession, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        await service.edit_memory(db, principal, memory_id, body, idempotency_key, delete=True)
        return {"ok": True}


@router.get("/me/memory-suggestions", response_model=d.PageView[d.MemorySuggestionView], response_model_exclude_unset=True)
async def suggestions(principal: CurrentUser, db: DbSession):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.list_suggestions(db, principal)


@router.post("/me/memory-suggestions/{suggestion_id}/confirm", response_model=d.MemoryView, response_model_exclude_unset=True)
async def confirm(suggestion_id: UUID, principal: WriteUser, db: DbSession, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.memory_view(db, await service.resolve_suggestion(db, principal, suggestion_id, idempotency_key))


@router.post("/me/memory-suggestions/{suggestion_id}/ignore", response_model=d.OkView, response_model_exclude_unset=True)
async def ignore(suggestion_id: UUID, principal: WriteUser, db: DbSession, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        await service.resolve_suggestion(db, principal, suggestion_id, idempotency_key, ignore=True)
        return {"ok": True}
