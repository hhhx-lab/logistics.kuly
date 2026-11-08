from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api import response_dto as d
from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser, set_tenant_context
from app.api.errors import NotFoundOrForbidden
from app.documents.retention import CaseGone
from app.extensions import service
from app.extensions.models import ExtensionCall, McpToolGrant

router = APIRouter(tags=["extensions"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=80)]


class ImportBody(BaseModel):
    text: str = Field(max_length=32768)


@router.get("/extensions", response_model=d.ExtensionListView, response_model_exclude_unset=True)
async def list_extensions(principal: CurrentUser, db: DbSession, settings: SettingsDep, kind: str | None = None, row_state: str | None = None, q: str = ""):
    async with db.begin():
        await set_tenant_context(db, principal)
        return {"items": await service.extension_rows(db, principal, settings, kind=kind, row_state=row_state, q=q), "warnings": service.public_registry_warnings(settings)}


@router.get("/skill-actions", response_model=d.SkillActionsView, response_model_exclude_unset=True)
async def actions(principal: CurrentUser):
    return {"combination_policy": service.SKILL_ACTION_POLICY, "items": [{"action_id": action, "label": label, "group": group,
        "available": group == "read_only" or "run_start" in principal.permissions,
        "disabled_reason": None if group == "read_only" or "run_start" in principal.permissions else "无发起任务权限"} for action, (label, group) in service.ACTIONS.items()]}


@router.get("/mcp-templates", response_model=d.PageView[d.McpTemplateView], response_model_exclude_unset=True)
async def templates(principal: CurrentUser, settings: SettingsDep):
    return service.templates(settings)


@router.get("/extension-calls", response_model=d.PageView[d.ExtensionCallView], response_model_exclude_unset=True)
async def calls(principal: CurrentUser, db: DbSession, settings: SettingsDep, extension_id: UUID | None = None):
    async with db.begin():
        await set_tenant_context(db, principal)
        query = select(ExtensionCall).where(ExtensionCall.owner_user_id == principal.user_id)
        if extension_id:
            query = query.where(ExtensionCall.extension_id == extension_id)
        rows = list((await db.execute(query.order_by(ExtensionCall.created_at.desc()).limit(100))).scalars())
        from app.conversation.models import Conversation, ConversationTurn, Message
        from app.documents.retention import load_case_for_read
        items = []
        for row in rows:
            conversation = await db.get(Conversation, row.conversation_id) if row.conversation_id else None
            if conversation and conversation.case_id:
                try:
                    await load_case_for_read(db, conversation.case_id, user_id=principal.user_id, settings=settings)
                except (NotFoundOrForbidden, CaseGone):
                    continue
            turn = await db.get(ConversationTurn, row.parent_turn_id) if row.parent_turn_id else None
            message_id = (await db.execute(select(Message.id).where(Message.turn_id == row.parent_turn_id,
                Message.conversation_id == row.conversation_id, Message.role == "assistant").order_by(Message.seq).limit(1))).scalar_one_or_none() if turn else None
            items.append({"call_id": str(row.id), "extension_id": str(row.extension_id), "name": row.name, "action": row.action,
                "at": row.created_at, "caller": principal.display_name, "extension_version": row.extension_version, "tool_version": row.tool_schema_hash,
                "status": row.status, "reject_class": row.reject_class, "error_class": row.error_class, "result_redacted": row.result_redacted,
                "case_ref": str(conversation.case_id) if conversation and conversation.case_id else None,
                "conversation_id": str(conversation.id) if conversation else None, "parent_turn_id": str(turn.id) if turn else None,
                "message_id": str(message_id) if message_id else None, "reference_snapshot_id": str(turn.reference_snapshot_id) if turn else None,
                "params_redacted": row.params_redacted, "destination": row.destination, "started_at": row.started_at,
                "finished_at": row.finished_at, "duration_ms": row.duration_ms})
        return {"items": items}


@router.post("/skills/import-preview", response_model=d.SkillImportView, response_model_exclude_unset=True)
async def import_skill(body: ImportBody, principal: WriteUser):
    return service.preview_import(body.text)


@router.post("/mcp-connections/import-preview", response_model=d.McpImportView, response_model_exclude_unset=True)
async def import_mcp(body: ImportBody, principal: WriteUser):
    return service.preview_import(body.text, mcp=True)


@router.post("/skills", status_code=201, response_model=d.SkillCreatedView, response_model_exclude_unset=True)
async def create_skill(body: service.SkillBody, principal: WriteUser, db: DbSession, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        skill, version = await service.create_skill(db, principal, body.model_dump(), idempotency_key)
        return {"skill_id": str(skill.id), "version": service.skill_view(version)}


@router.get("/skills/{skill_id}", response_model=d.SkillDetailView, response_model_exclude_unset=True)
async def skill(skill_id: UUID, principal: CurrentUser, db: DbSession):
    async with db.begin():
        await set_tenant_context(db, principal)
        row = await service.load_skill(db, principal, skill_id)
        versions = [service.skill_view(await service.skill_version(db, row, v)) for v in range(1, row.next_version+1)]
        return {"skill_id": str(row.id), "builtin": False, "versions": versions, "enabled_version": row.enabled_version}


def skill_command(command, *, versioned=False, patch=False):
    if versioned:
        async def handler(skill_id: UUID, version: int, principal: WriteUser, db: DbSession, idempotency_key: Key, body: dict = {}):
            async with db.begin():
                await set_tenant_context(db, principal)
                _, result = await service.change_skill(db, principal, skill_id, command, idempotency_key, version_no=version, body=body)
                return service.skill_view(result)
    else:
        async def handler(skill_id: UUID, principal: WriteUser, db: DbSession, idempotency_key: Key):
            async with db.begin():
                await set_tenant_context(db, principal)
                _, result = await service.change_skill(db, principal, skill_id, command, idempotency_key)
                return {"ok": True} if command in {"disable", "archive"} else service.skill_view(result)
    return handler


router.add_api_route("/skills/{skill_id}/versions", skill_command("new"), methods=["POST"], response_model=d.SkillVersionView, response_model_exclude_unset=True)
router.add_api_route("/skills/{skill_id}/versions/{version}", skill_command("patch", versioned=True), methods=["PATCH"], response_model=d.SkillVersionView, response_model_exclude_unset=True)
for command in ("validate", "enable"):
    router.add_api_route("/skills/{skill_id}/versions/{version}/"+command, skill_command(command, versioned=True), methods=["POST"], response_model=(d.OkView if command in {"disable", "archive"} else d.SkillVersionView), response_model_exclude_unset=True)
for command in ("disable", "archive"):
    router.add_api_route("/skills/{skill_id}/"+command, skill_command(command), methods=["POST"], response_model=(d.OkView if command in {"disable", "archive"} else d.SkillVersionView), response_model_exclude_unset=True)


@router.post("/mcp-connections", status_code=201, response_model=d.McpCreatedView, response_model_exclude_unset=True)
async def create_mcp(body: dict, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        connection, version = await service.create_connection(db, principal, settings, body, idempotency_key)
        return {"connection_id": str(connection.id), "version": await service.connection_view(db, principal, connection, version)}


@router.get("/mcp-connections/{connection_id}", response_model=d.McpDetailView, response_model_exclude_unset=True)
async def mcp(connection_id: UUID, principal: CurrentUser, db: DbSession):
    async with db.begin():
        await set_tenant_context(db, principal)
        connection = await service.load_connection(db, principal, connection_id)
        return {"connection_id": str(connection.id), "versions": [await service.connection_view(db, principal, connection,
            await service.connection_version(db, connection, n)) for n in range(1, connection.next_version+1)], "enabled_version": connection.enabled_version}


@router.get("/mcp-connections/{connection_id}/versions/{version}/tools", response_model=d.PageView[d.McpToolView], response_model_exclude_unset=True)
async def mcp_tools(connection_id: UUID, version: int, principal: CurrentUser, db: DbSession):
    async with db.begin():
        await set_tenant_context(db, principal)
        connection = await service.load_connection(db, principal, connection_id)
        return {"items": await service.tools_view(db, principal, await service.connection_version(db, connection, version))}


def mcp_command(command, *, versioned=False):
    if versioned:
        async def handler(connection_id: UUID, version: int, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key, body: dict = {}):
            async with db.begin():
                await set_tenant_context(db, principal)
                connection, row = await service.change_connection(db, principal, settings, connection_id, command, idempotency_key, version_no=version, body=body)
                return await service.connection_view(db, principal, connection, row)
    else:
        async def handler(connection_id: UUID, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
            async with db.begin():
                await set_tenant_context(db, principal)
                connection, row = await service.change_connection(db, principal, settings, connection_id, command, idempotency_key)
                return {"ok": True} if command in {"disable", "archive"} else await service.connection_view(db, principal, connection, row)
    return handler


router.add_api_route("/mcp-connections/{connection_id}/versions", mcp_command("new"), methods=["POST"], response_model=d.McpVersionView, response_model_exclude_unset=True)
router.add_api_route("/mcp-connections/{connection_id}/versions/{version}", mcp_command("patch", versioned=True), methods=["PATCH"], response_model=d.McpVersionView, response_model_exclude_unset=True)
for command in ("test", "enable", "grant"):
    router.add_api_route("/mcp-connections/{connection_id}/versions/{version}/"+("tool-grants" if command == "grant" else command), mcp_command(command, versioned=True), methods=["POST"], response_model=(d.OkView if command in {"disable", "archive"} else d.McpVersionView), response_model_exclude_unset=True)
for command in ("disable", "archive"):
    router.add_api_route("/mcp-connections/{connection_id}/"+command, mcp_command(command), methods=["POST"], response_model=(d.OkView if command in {"disable", "archive"} else d.McpVersionView), response_model_exclude_unset=True)


@router.post("/mcp-tool-grants/{grant_id}/revoke", response_model=d.OkView, response_model_exclude_unset=True)
async def revoke(grant_id: UUID, principal: WriteUser, db: DbSession, idempotency_key: Key):
    from datetime import UTC, datetime
    async with db.begin():
        await set_tenant_context(db, principal)
        grant = (await db.execute(select(McpToolGrant).where(McpToolGrant.id == grant_id,
            McpToolGrant.subject_user_id == principal.user_id).with_for_update())).scalar_one_or_none()
        if grant is None:
            raise NotFoundOrForbidden()
        from app.api import idempotency
        outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
            scope=f"POST /mcp-tool-grants/{grant_id}/revoke", key=idempotency_key, payload={})
        if not outcome.replayed:
            grant.revoked_at = datetime.now(UTC)
            idempotency.complete(outcome.record, {"grant_id": str(grant.id)})
        return {"ok": True}


@router.get("/mcp-challenges/{challenge_id}", response_model=d.McpChallengeView, response_model_exclude_unset=True)
async def challenge(challenge_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.challenge_view(db, principal, await service.load_challenge(db, principal, settings, challenge_id))


@router.post("/mcp-challenges/{challenge_id}/approve", response_model=d.McpChallengeView, response_model_exclude_unset=True)
async def approve(challenge_id: UUID, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.challenge_view(db, principal, await service.resolve_challenge(db, principal, settings, challenge_id, idempotency_key))


@router.post("/mcp-challenges/{challenge_id}/reject", response_model=d.McpChallengeView, response_model_exclude_unset=True)
async def reject(challenge_id: UUID, principal: WriteUser, db: DbSession, settings: SettingsDep, idempotency_key: Key):
    async with db.begin():
        await set_tenant_context(db, principal)
        return await service.challenge_view(db, principal, await service.resolve_challenge(db, principal, settings, challenge_id, idempotency_key, reject=True))
