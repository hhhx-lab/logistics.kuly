from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import idempotency
from app.api.errors import ApiError, IdempotencyConflict, InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.config import ModelRole, Settings
from app.conversation.contracts import CreateConversationBody, TurnBody
from app.conversation.models import AssistantContinuation, CheckpointRef, Conversation, ConversationTurn, ConversationWorkItem, Message, ReferenceSnapshot, TurnEvent
from app.conversation.references import ReferenceSelection, freeze_references
from app.documents.models import UploadFile, UploadFileStatus
from app.documents.retention import load_case_for_read
from app.identity.service import Principal
from app.infra.model_gateway import build_model_gateway
from app.prompts.registry import get_prompt_registry

GRAPH_VERSION = "conversation@1"
OPEN = {"pending", "running"}
LEASE_SECONDS = 120
WORK_RETENTION_UNTIL = datetime(9999, 12, 30, tzinfo=UTC)


class TurnBusy(ApiError):
    status_code = 409
    code = "TURN_BUSY"
    message = "本会话还有一轮正在处理，请等待完成后再发送。"


async def lock_conversation_binding(db: AsyncSession, principal: Principal, conversation_id: UUID) -> None:
    """先于Case行锁取得，串行化空会话首绑；同事务可重入。"""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {
        "scope": f"conversation-binding:{principal.tenant_id}:{conversation_id}",
    })


async def load_conversation(db: AsyncSession, principal: Principal, settings: Settings, conversation_id: UUID, *, write: bool = False, allow_archived: bool = False) -> Conversation:
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == principal.tenant_id,
        Conversation.owner_user_id == principal.user_id, Conversation.deleted_at.is_(None))
    if write:
        await lock_conversation_binding(db, principal, conversation_id)
        probe = (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
        probe_case_id = probe.case_id if probe else None
        if probe and probe.case_id:
            from app.documents.models import Case
            await db.execute(select(Case).where(Case.id == probe.case_id).with_for_update()
                .execution_options(populate_existing=True))
        query = query.with_for_update().execution_options(populate_existing=True)
    conversation = (await db.execute(query)).scalar_one_or_none()
    if conversation is None:
        raise NotFoundOrForbidden()
    if write and conversation.archived_at and not allow_archived:
        raise ApiError("会话已归档，请在设置中恢复后继续。", code="CONVERSATION_ARCHIVED", status_code=409)
    if write and conversation.case_id != probe_case_id:
        raise ApiError("会话已关联另一票，请重新打开或新建会话", code="CONVERSATION_CASE_CONFLICT", status_code=409)
    if conversation.case_id:
        await load_case_for_read(db, conversation.case_id, user_id=principal.user_id, settings=settings)
    return conversation


async def conversation_view(db, conversation):
    from app.documents.models import Case
    case = await db.get(Case, conversation.case_id) if conversation.case_id else None
    return {"conversation_id": str(conversation.id), "title": conversation.title, "updated_at": conversation.updated_at,
        "status": conversation.status, "case_id": str(conversation.case_id) if conversation.case_id else None,
        "archived_at": conversation.archived_at, "deleted_at": conversation.deleted_at,
        "case_name": case.name if case else None, "case_status_label": case.status.value if case else None,
        "conversation_version": conversation.conversation_version,
        "active_run_id": str(conversation.active_run_id) if conversation.active_run_id else None,
        "active_task_draft_id": str(conversation.active_task_draft_id) if conversation.active_task_draft_id else None,
        "case_business_revision_no": case.business_revision_no if case else None,
        "initial_reference_selections": conversation.initial_reference_selections, "entry_source": conversation.entry_source}


def message_view(message):
    return {"message_id": str(message.id), "turn_id": str(message.turn_id),
        "parent_turn_id": str(message.parent_turn_id) if message.parent_turn_id else None, "role": message.role,
        "author": message.author, "created_at": message.created_at, "status": message.status, "text": message.text,
        "references": message.references, "capabilities": message.capabilities, "attachments": message.attachments,
        "cards": message.cards, "continuation": message.continuation}


def turn_view(turn):
    return {"turn_id": str(turn.id), "conversation_id": str(turn.conversation_id),
        "status": "running" if turn.status == "pending" else turn.status,
        "last_seq": turn.next_event_seq, "reference_snapshot_id": str(turn.reference_snapshot_id)}


async def create_conversation(db, principal, settings, body: CreateConversationBody, key: str):
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope="POST /conversations", key=key, payload=body.model_dump(mode="json"))
    if outcome.replayed:
        return await load_conversation(db, principal, settings, UUID(outcome.record.result_ref["conversation_id"]))
    case_id, initial_frozen = await freeze_references(db, selections=body.initial_reference_selections, bound_case_id=None,
        user_id=principal.user_id, settings=settings)
    conversation = Conversation(tenant_id=principal.tenant_id, owner_user_id=principal.user_id, title="新对话", status="active",
        case_id=case_id, active_run_id=next((UUID(f["run_id"]) for f in initial_frozen if f.get("run_id")), None),
        initial_reference_selections=[s.model_dump(mode="json") for s in body.initial_reference_selections],
        entry_source=body.entry_source, conversation_version=1, next_message_seq=0, turn_count=0)
    db.add(conversation)
    await db.flush()
    db.add(CheckpointRef(tenant_id=principal.tenant_id, case_id=case_id, conversation_id=conversation.id,
        thread_id="conversation:" + uuid4().hex))
    idempotency.complete(outcome.record, {"conversation_id": str(conversation.id)})
    return conversation


async def append_event(db, turn, event_type, payload, message_id=None):
    turn.next_event_seq += 1
    event = TurnEvent(tenant_id=turn.tenant_id, turn_id=turn.id, seq=turn.next_event_seq,
        event_type=event_type, payload=jsonable_encoder({"status": turn.status, "stage": "对话", "message": payload.get("text", ""), **payload}), message_id=message_id)
    db.add(event)
    await db.flush()
    return event


async def append_message(db, conversation, turn, *, text, role="assistant", cards=None, parent_turn_id=None, continuation=None):
    if role == "assistant":
        from app.conversation.safety import guard_text, guard_cards
        text, cards = guard_text(text), guard_cards(cards or [])
    conversation.next_message_seq += 1
    conversation.updated_at = datetime.now(UTC)
    message = Message(tenant_id=conversation.tenant_id, conversation_id=conversation.id, seq=conversation.next_message_seq,
        turn_id=turn.id, parent_turn_id=parent_turn_id, role=role, author="单证助手" if role == "assistant" else "用户",
        status="completed", text=text, cards=cards or [], references=[], capabilities=[], attachments=[],
        reference_snapshot_id=turn.reference_snapshot_id, continuation=continuation)
    db.add(message)
    await db.flush()
    return message


async def finish_answer(db, conversation, turn, texts, cards=None):
    turn.status = "completed"
    message = await append_message(db, conversation, turn, text="\n\n".join(texts), cards=cards)
    preview = (await db.execute(select(TurnEvent.id).where(TurnEvent.turn_id == turn.id,
        TurnEvent.event_type == "answer_preview_started").limit(1))).scalar_one_or_none()
    if not preview:
        await append_event(db, turn, "message_delta", {"text": message.text}, message.id)
    for card in message.cards:
        if card.get("type") == "evidence":
            await append_event(db, turn, "evidence_card", {"cards": card["cards"]}, message.id)
    turn.final_message_id = message.id
    turn.finished_at = datetime.now(UTC)
    turn.lease_token = None
    turn.lease_until = None
    await append_event(db, turn, "message_final", {"message": message_view(message), "preview_id": str(turn.id) if preview else None}, message.id)
    return message


def failure_notice(unknown=False, error_class=None):
    if error_class == "MODEL_STREAM_PROTOCOL_FAILED":
        return "模型服务返回的回复流格式异常，本轮没有生成可用正文。你的原问题已保存。"
    if error_class == "MODEL_AUTH_FAILED":
        return "对话模型鉴权失败，请检查服务端的模型密钥及访问权限。你的原问题已保存。"
    if error_class == "MODEL_INTENT_FAILED":
        return "对话模型未能完成意图识别，这是服务调用失败。你的原问题已保存，无需补充字段来解决此错误。"
    return "模型服务响应中断，请求受理待核查；请勿重复提交同一问题。" if unknown else "本轮未能完成，请检查服务状态。你的原问题已保存。"


async def fail_answer(db, conversation, turn, error_class):
    """失败也保存可恢复消息；未知受理仍保留原调用账本。"""
    unknown = error_class == "MODEL_ACCEPTANCE_UNKNOWN"
    turn.status, turn.error_class = "failed", error_class
    turn.lease_token, turn.lease_until = None, None
    turn.finished_at = datetime.now(UTC)
    notice = failure_notice(unknown, error_class)
    message = await append_message(db, conversation, turn, text=notice,
        cards=[{"type":"system_error", "kind":"interrupted", "text":notice}])
    message.status = "failed"
    turn.final_message_id = message.id
    await append_event(db, turn, "message_error", {"text":notice, "error_class":error_class, "preview_id":str(turn.id)}, message.id)
    return message


async def freeze_capabilities(db, principal, settings, selections):
    if not selections:
        return []
    from app.conversation.tools import validate_capabilities
    return await validate_capabilities(db, principal, settings, selections)


async def inherited_reference_selections(db, conversation):
    """追问延续上一轮文件范围；兼容旧版自动回退整票的无名引用。"""
    snapshots = (await db.execute(select(ReferenceSnapshot, ConversationWorkItem.payload).join(ConversationTurn,
        ConversationTurn.reference_snapshot_id == ReferenceSnapshot.id).outerjoin(ConversationWorkItem,
            (ConversationWorkItem.turn_id == ConversationTurn.id) & (ConversationWorkItem.kind == "request")).where(
            ReferenceSnapshot.tenant_id == conversation.tenant_id,
            ReferenceSnapshot.conversation_id == conversation.id,
            ReferenceSnapshot.case_id == conversation.case_id,
            ConversationTurn.status == "completed").order_by(ConversationTurn.turn_no.desc()).limit(20))).all()
    for snapshot, request in snapshots:
        meaningful = any(f.get("reference_origin") in {"explicit", "inherited"} for f in snapshot.frozen) or bool(request and (request.get("references") or request.get("upload_file_ids"))) or any(s.get("kind") in {"document", "section", "snapshot"} or s.get("label")
            or (s.get("kind") == "run" and str(conversation.active_run_id) == s.get("id")) for s in snapshot.selections)
        if not meaningful:
            continue
        result = []
        context = next((f for f in snapshot.frozen if f.get("kind") == "translation_context"), {})
        for selection in snapshot.selections:
            value = {**selection, "label": None}
            if selection["kind"] in {"document", "section"}:
                fixed = next((f for f in snapshot.frozen if f.get("kind") == selection["kind"] and f.get("id") == selection["id"]), None)
                if fixed is None:
                    raise NotFoundOrForbidden()
                value.update({key: fixed[key] for key in ("document_id", "document_version", "parse_artifact_id", "logical_document_id", "section_id") if key in fixed})
                value["selection_mode"] = "fixed"
                if context.get("translation_revision_id"):
                    value.update(translation_revision_id=context["translation_revision_id"], target_language=context["target_language"])
            result.append(ReferenceSelection.model_validate(value))
        return result
    return []


async def create_turn(db, principal, settings, conversation_id, body: TurnBody, key):
    conversation = await load_conversation(db, principal, settings, conversation_id, write=True)
    digest = idempotency.request_digest(body.model_dump(mode="json"))
    existing = (await db.execute(select(ConversationTurn).where(ConversationTurn.conversation_id == conversation_id,
        ConversationTurn.tenant_id == principal.tenant_id, ConversationTurn.idempotency_key == key))).scalar_one_or_none()
    if existing:
        if existing.request_hash != digest:
            raise IdempotencyConflict()
        return existing, False
    if conversation.status != "active":
        raise ApiError("会话已结束，请新建会话。", code="CONVERSATION_ENDED", status_code=409)
    active = (await db.execute(select(ConversationTurn).where(ConversationTurn.conversation_id == conversation_id,
        ConversationTurn.status.in_(OPEN)).with_for_update())).scalar_one_or_none()
    now = datetime.now(UTC)
    if active:
        if active.lease_until and active.lease_until > now:
            raise TurnBusy()
        active.status = "completed" if active.final_message_id else "interrupted"
        active.finished_at = now
        active.lease_token = None
        active.lease_until = None
        if not active.final_message_id:
            await append_event(db, active, "message_error", {"text": "生成已中断，请重新提问。"})
    if not body.text.strip() and not body.references and not body.capabilities and not body.upload_file_ids:
        raise InvalidRequest("消息不能为空")
    selections = list(body.references)
    uploaded = []
    for file_id in body.upload_file_ids:
        file = await db.get(UploadFile, file_id)
        if file is None or file.tenant_id != principal.tenant_id or file.status != UploadFileStatus.VERIFIED or not file.document_id:
            raise InvalidRequest("文件尚未验收，不能用于本轮任务")
        selections.append(ReferenceSelection(kind="document", id=file.document_id, case_id=file.case_id,
            selection_mode="fixed", document_id=file.document_id, document_version=file.document_version, label=file.filename))
        uploaded.append({"case_id": str(file.case_id), "document_id": str(file.document_id),
            "document_version": file.document_version, "filename": file.filename})
    visible_selections = list(selections)
    reference_inherited = False
    if not selections:
        selections = await inherited_reference_selections(db, conversation)
        reference_inherited = bool(selections)
    if not selections:
        selections = [ReferenceSelection.model_validate(s) for s in conversation.initial_reference_selections]
    if not selections and conversation.case_id:
        if conversation.active_run_id:
            selections = [ReferenceSelection(kind="run", id=conversation.active_run_id, case_id=conversation.case_id, selection_mode="current")]
        else:
            selections = [ReferenceSelection(kind="case", id=conversation.case_id, case_id=conversation.case_id, selection_mode="current")]
    case_id, frozen = await freeze_references(db, selections=selections, bound_case_id=conversation.case_id,
        user_id=principal.user_id, settings=settings)
    if body.task_draft_id:
        from app.workflows.commands import load_draft
        draft, _ = await load_draft(db, principal, settings, body.task_draft_id)
        if draft.case_id != case_id or (draft.conversation_id and draft.conversation_id != conversation.id):
            raise NotFoundOrForbidden()
    capabilities = await freeze_capabilities(db, principal, settings, body.capabilities)
    from app.conversation.references import freeze_translation_context
    # 此时Case已鉴权；临时赋值用于固定本轮译文，不读取跨票语境。
    conversation.case_id = case_id
    frozen = await freeze_translation_context(db, conversation, frozen, selections, body.text)
    if visible_selections or reference_inherited:
        frozen = [{**item, "reference_origin": "explicit" if visible_selections else "inherited"} for item in frozen]
    registry = get_prompt_registry()
    prompts = registry.freeze(["chat.system", "chat.intent", "chat.answer", "chat.session_summary", "chat.general"])
    binding = build_model_gateway(settings).binding_for(ModelRole.CHAT)
    prompts["stream_enabled"] = settings.chat_stream_enabled
    prompts["stream_max_bytes"] = settings.chat_stream_max_bytes
    prompts["stream_event_max_bytes"] = settings.chat_stream_event_max_bytes
    prompts["stream_max_chars"] = settings.chat_stream_max_chars
    prompts["stream_event_chars"] = settings.chat_stream_event_chars
    prompts["stream_pending_chars"] = settings.chat_stream_pending_chars
    prompts["model_binding"] = binding.model_dump(mode="json") if binding else None
    prompts["chat_max_model_cost_cny"] = str(settings.chat_max_model_cost_cny) if settings.chat_max_model_cost_cny is not None else None
    conversation.case_id = case_id
    conversation.turn_count += 1
    snapshot = ReferenceSnapshot(tenant_id=principal.tenant_id, conversation_id=conversation.id, case_id=case_id,
        selections=[s.model_dump(mode="json") for s in selections], frozen=frozen, capabilities=capabilities,
        business_revision_id=next((UUID(f["business_revision_id"]) for f in sorted(frozen, key=lambda item: item["kind"] != "snapshot") if f.get("business_revision_id")), None))
    db.add(snapshot)
    await db.flush()
    from app.preferences.service import get_preferences, list_memories
    # 内容与版本共同冻结；避免记忆写入恰好穿过两次读取的边界。
    preferences = await get_preferences(db, principal, lock=True)
    memories = await list_memories(db, principal)
    prompts["confirmed_preferences"] = memories["items"]
    prompts["interface_language"] = preferences.interface_language if preferences else "zh-CN"
    from app.workflows.models import Run
    task_run_id = next((f.get("run_id") for f in frozen if f.get("run_id")), None) or conversation.active_run_id
    task_run = await db.get(Run, UUID(str(task_run_id))) if task_run_id else None
    prompts["current_task_spec"] = dict(task_run.task_spec) if task_run and task_run.case_id == case_id else {}
    if body.task_draft_id or conversation.active_task_draft_id:
        from app.workflows.commands import load_draft
        task_draft, _ = await load_draft(db, principal, settings, body.task_draft_id or conversation.active_task_draft_id)
        if task_draft.case_id == case_id and not task_draft.started_run_id:
            prompts["current_task_spec"] = {"workflow_type": task_draft.workflow_type, "workflow_version": task_draft.workflow_version,
                "parameters": dict(task_draft.draft_parameters), "status": "not_started"}
    turn = ConversationTurn(tenant_id=principal.tenant_id, conversation_id=conversation.id, turn_no=conversation.turn_count,
        idempotency_key=key, request_hash=digest, status="pending", reference_snapshot_id=snapshot.id,
        graph_version=GRAPH_VERSION, prompt_versions=jsonable_encoder(prompts), memory_version=memories["memory_version"], model_binding_hash=binding.public_config_hash if binding else None,
        lease_token=uuid4(), lease_until=now + timedelta(seconds=LEASE_SECONDS), next_event_seq=0)
    db.add(turn)
    await db.flush()
    message = await append_message(db, conversation, turn, text=body.text, role="user")
    message.references = [s.model_dump(mode="json") for s in visible_selections]
    message.capabilities = [s.model_dump(mode="json") for s in body.capabilities]
    message.attachments = uploaded
    await store_work(db, turn, "request", body.model_dump(mode="json"))
    from app.workflows.models import QueueOutbox
    db.add(QueueOutbox(tenant_id=turn.tenant_id, topic="turn", dedupe_key="turn:" + str(turn.id),
        payload={"turn_id": str(turn.id)}, status="pending"))
    await db.flush()
    return turn, True


async def store_work(db, turn, kind, payload):
    item = ConversationWorkItem(tenant_id=turn.tenant_id, conversation_id=turn.conversation_id, turn_id=turn.id,
        kind=kind, payload=payload, expires_at=datetime.now(UTC) + timedelta(days=1))
    if kind == "domain_rejected":
        item.expires_at = WORK_RETENTION_UNTIL
    db.add(item)
    await db.flush()
    return str(item.id)


async def load_work(db, turn, item_id, kind):
    item = await db.get(ConversationWorkItem, UUID(item_id))
    if item is None or item.turn_id != turn.id or item.tenant_id != turn.tenant_id or item.kind != kind or item.expires_at < datetime.now(UTC):
        raise NotFoundOrForbidden("本轮暂存结果已过期，请重新提问")
    return item.payload


async def rename_conversation(db, principal, settings, conversation_id, body, key):
    conversation = await load_conversation(db, principal, settings, conversation_id, write=True)
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope=f"PATCH /conversations/{conversation_id}", key=key, payload=body.model_dump())
    if not outcome.replayed:
        if conversation.conversation_version != body.expected_conversation_version:
            raise VersionConflict(current=await conversation_view(db, conversation))
        conversation.title = body.title.strip()
        conversation.conversation_version += 1
        conversation.updated_at = datetime.now(UTC)
        idempotency.complete(outcome.record, {"conversation_id": str(conversation.id)})
    return conversation


async def end_conversation(db, principal, settings, conversation_id, key):
    conversation = await load_conversation(db, principal, settings, conversation_id, write=True)
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope=f"POST /conversations/{conversation_id}/end", key=key, payload={})
    if not outcome.replayed:
        latest = (await db.execute(select(ConversationTurn).where(ConversationTurn.conversation_id == conversation.id)
            .order_by(ConversationTurn.turn_no.desc()).limit(1))).scalar_one_or_none()
        if latest:
            from app.workflows.models import Run
            run = await db.get(Run, conversation.active_run_id) if conversation.active_run_id else None
            labels = {"queued": "等待处理", "processing": "仍在处理", "indexed": "识别与索引已完成，无译文", "awaiting_review": "等待复核",
                "ready_to_export": "已复核，等待导出", "exporting": "仍在导出", "exported": "已成功导出", "failed": "处理失败",
                "cancelled": "已取消", "paused_reconcile": "等待核查"}
            status_text = labels.get(run.status.value, run.status.value) if run else "尚未启动任务"
            text = f"会话已结束。任务状态：{status_text}。"
            await append_message(db, conversation, latest, text=text,
                cards=[{"type": "completion", "run_id": str(run.id) if run else None, "ended": True, "status_text": status_text}])
        conversation.status = "ended"
        conversation.ended_at = datetime.now(UTC)
        conversation.conversation_version += 1
        from app.extensions.models import McpChallenge
        for continuation in (await db.execute(select(AssistantContinuation).where(AssistantContinuation.conversation_id == conversation.id,
            AssistantContinuation.status.in_(["waiting", "awaiting_retry", "awaiting_reconcile", "running"])))).scalars():
            continuation.status = "closed"
            continuation.lease_token, continuation.lease_until = None, None
        for challenge in (await db.execute(select(McpChallenge).where(McpChallenge.conversation_id == conversation.id, McpChallenge.status == "pending"))).scalars():
            challenge.status = "denied"
        for turn in (await db.execute(select(ConversationTurn).where(ConversationTurn.conversation_id == conversation.id,
            ConversationTurn.status.in_(OPEN)).with_for_update())).scalars():
            turn.status = "interrupted"
            turn.lease_token = None
            turn.lease_until = None
            turn.finished_at = datetime.now(UTC)
            await append_event(db, turn, "message_error", {"text": "会话已结束。"})
        idempotency.complete(outcome.record, {"conversation_id": str(conversation.id)})
    return conversation


async def decorate_continuations(db, messages):
    ids = {m.turn_id for m in messages}
    rows = list((await db.execute(select(AssistantContinuation).where(AssistantContinuation.parent_turn_id.in_(ids)))).scalars()) if ids else []
    by_turn = {row.parent_turn_id: row for row in rows}
    failed = {t.id:t for t in (await db.execute(select(ConversationTurn).where(ConversationTurn.id.in_(ids),
        ConversationTurn.status == "failed", ConversationTurn.final_message_id.is_(None)))).scalars()} if ids else {}
    unknown_ids = set((await db.execute(select(ConversationWorkItem.turn_id).where(ConversationWorkItem.turn_id.in_(failed),
        ConversationWorkItem.kind == "model_call", ConversationWorkItem.payload["outcome"].astext == "needs_reconcile"))).scalars()) if failed else set()
    result = []
    for message in messages:
        view = message_view(message)
        if message.role == "user" and message.turn_id in failed:
            view["cards"] = list(view["cards"]) + [{"type":"system_error", "kind":"interrupted",
                "text":failure_notice(message.turn_id in unknown_ids or failed[message.turn_id].error_class == "MODEL_ACCEPTANCE_UNKNOWN")}]
        if message.continuation and message.parent_turn_id is None and message.turn_id in by_turn:
            continuation = by_turn[message.turn_id]
            status = {"waiting": "pending", "closed": "conversation_closed"}.get(continuation.status, continuation.status)
            text = {"awaiting_retry": "识别暂时失败，等待安全重试。", "awaiting_reconcile": "识别结果待核查。",
                "completed": "原问题已续答。", "failed": "自动续答未完成。", "cancelled": "识别已取消。",
                "restricted": "授权已失效。", "closed": "需在新会话追问。"}.get(continuation.status, "等待本次识别结果。")
            view["continuation"] = {"status": status, "text": text}
        result.append(view)
    return result
