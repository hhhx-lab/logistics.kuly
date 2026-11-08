"""本人会话管理，单据与任务生命周期独立。"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from app.api import idempotency
from app.api.errors import ApiError, NotFoundOrForbidden, VersionConflict
from app.conversation.models import AssistantContinuation, CheckpointRef, Conversation, ConversationTurn
from app.conversation.service import load_conversation
from app.extensions.models import McpChallenge
from app.workflows.models import TaskDraft


async def manage_conversation(db, principal, settings, conversation_id, body, key, action):
    scope = f"{'DELETE' if action == 'delete' else 'POST'} /conversations/{conversation_id}" + ("" if action == "delete" else f"/{action}")
    outcome = await idempotency.begin(db, tenant_id=principal.tenant_id, actor_id=principal.user_id,
        scope=scope, key=key, payload=body.model_dump(mode="json"))
    if outcome.replayed and action == "delete":
        return None
    conversation = await load_conversation(db, principal, settings, conversation_id, write=True, allow_archived=True)
    if outcome.replayed:
        return conversation
    if conversation.conversation_version != body.expected_conversation_version:
        raise VersionConflict(current={"conversation_version":conversation.conversation_version})
    if action == "archive":
        busy = (await db.execute(select(ConversationTurn.id).where(ConversationTurn.conversation_id == conversation.id,
            ConversationTurn.status.in_(["pending", "running"])).limit(1))).scalar_one_or_none()
        continuation = (await db.execute(select(AssistantContinuation.id).where(AssistantContinuation.conversation_id == conversation.id,
            AssistantContinuation.status.in_(["waiting", "awaiting_retry", "awaiting_reconcile", "running"])).limit(1))).scalar_one_or_none()
        challenge = (await db.execute(select(McpChallenge.id).where(McpChallenge.conversation_id == conversation.id,
            McpChallenge.status == "pending", McpChallenge.expires_at > datetime.now(UTC)).limit(1))).scalar_one_or_none()
        if busy or continuation or challenge:
            raise ApiError("本会话仍有回答、续答或工具授权待处理，请完成或结束服务后归档。", code="CONVERSATION_BUSY", status_code=409)
        conversation.archived_at = conversation.archived_at or datetime.now(UTC)
    elif action == "restore":
        conversation.archived_at = None
    elif action == "delete":
        if not conversation.archived_at:
            raise ApiError("请先归档会话，再从设置删除。", code="CONVERSATION_NOT_ARCHIVED", status_code=409)
        conversation.deleted_at = datetime.now(UTC)
        conversation.purge_after = conversation.deleted_at + timedelta(seconds=settings.workflow_task_hard_limit_seconds + settings.purge_inflight_grace_seconds)
    else:
        raise ValueError("未知会话管理动作")
    conversation.conversation_version += 1
    conversation.updated_at = datetime.now(UTC)
    idempotency.complete(outcome.record, {"conversation_id":str(conversation.id)})
    return conversation


def purge_deleted_conversations(db, settings, moment, batch_size):
    """恢复扫描事务内清理；外部检查点删除失败保留tombstone以便重试。"""
    rows = list(db.scalars(select(Conversation).where(Conversation.deleted_at.is_not(None), Conversation.purge_after <= moment)
        .order_by(Conversation.purge_after).limit(batch_size).with_for_update(skip_locked=True)))
    if not rows:
        return 0
    from app.infra.checkpoints import checkpoint_database_url, open_checkpointer
    with open_checkpointer(checkpoint_database_url(settings)) as saver:
        for conversation in rows:
            threads = list(db.scalars(select(CheckpointRef.thread_id).where(CheckpointRef.conversation_id == conversation.id)))
            threads.extend(f"continuation:{value}" for value in db.scalars(select(AssistantContinuation.id).where(AssistantContinuation.conversation_id == conversation.id)))
            for thread in set(threads):
                saver.delete_thread(thread)
            db.execute(delete(McpChallenge).where(McpChallenge.conversation_id == conversation.id))
            db.execute(update(TaskDraft).where(TaskDraft.conversation_id == conversation.id).values(conversation_id=None))
            db.execute(delete(CheckpointRef).where(CheckpointRef.conversation_id == conversation.id))
            db.delete(conversation)
        db.flush()
    return len(rows)
