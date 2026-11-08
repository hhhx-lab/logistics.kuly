"""整票删除命令与固定清单：先撤权，待所有在途写截止后最终清理。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text

from app.api.errors import IdempotencyConflict, InvalidRequest, NotFoundOrForbidden, ServiceUnavailable, VersionConflict
from app.api.idempotency import request_digest
from app.conversation.models import AssistantContinuation, CheckpointRef, Conversation, ConversationTurn
from app.documents.models import Case, CaseAccessGrant, CaseStatus, StoredObject
from app.documents.retention import load_case_for_read
from app.exports.models import ExportAttempt
from app.management.ledger import DeletionLedger, LedgerUnavailable
from app.management.models import CaseDeletion
from app.workflows.models import ExecutionEpoch, InputRequest, InputRequestStatus, QueueOutbox, Run, RunStatus, StepExecution


def deletion_view(value):
    return {"deletion_id": str(value.id), "status": value.status, "requested_at": value.requested_at,
            "completed_at": value.completed_at, "last_error_class": value.last_error_class}


def register_deletion(db, case, settings, *, actor_id, key_hash, request_hash, now=None):
    """调用者持Case行锁。账本意图在同一事务提交前fsync；回滚也保守撤权。"""
    moment = now or datetime.now(UTC)
    runs = list(db.scalars(select(Run).where(Run.case_id == case.id).order_by(Run.id).with_for_update()))
    run_ids = [r.id for r in runs]
    conversations = list(db.scalars(select(Conversation).where(Conversation.case_id == case.id).order_by(Conversation.id).with_for_update()))
    conversation_ids = [c.id for c in conversations]
    threads = set(db.scalars(select(CheckpointRef.thread_id).where((CheckpointRef.case_id == case.id) | CheckpointRef.conversation_id.in_(conversation_ids))))
    for execution in db.scalars(select(ExecutionEpoch).where(ExecutionEpoch.run_id.in_(run_ids))):
        threads.add(f"run:{execution.run_id}:epoch:{execution.epoch}")
    for continuation in db.scalars(select(AssistantContinuation).where(AssistantContinuation.case_id == case.id)):
        threads.add(f"continuation:{continuation.id}")
        continuation.status = "restricted"
        continuation.lease_token = None
        continuation.lease_until = None
    for conversation in conversations:
        conversation.status = "ended"
        conversation.conversation_version += 1
        conversation.ended_at = moment
    for turn in db.scalars(select(ConversationTurn).where(ConversationTurn.conversation_id.in_(conversation_ids))):
        if turn.status in {"pending", "running"}:
            turn.status = "interrupted"
            turn.error_class = "case_deleted"
            turn.lease_token = None
            turn.lease_until = None
    for request in db.scalars(select(InputRequest).where(InputRequest.run_id.in_(run_ids), InputRequest.status == InputRequestStatus.ACTIVE)):
        request.status = InputRequestStatus.CANCELLED
    for attempt in db.scalars(select(ExportAttempt).where(ExportAttempt.case_id == case.id)):
        if attempt.status not in {"succeeded", "failed_final"}:
            attempt.status = "failed_final"
            attempt.error_summary = "单票已删除"
    for run in runs:
        run.execution_fence += 1
        run.status = RunStatus.CANCELLED
    objects = list(db.scalars(select(StoredObject).where(StoredObject.case_id == case.id)))
    grace = max(settings.upload_signed_url_ttl_seconds, settings.workflow_task_hard_limit_seconds) + settings.purge_inflight_grace_seconds
    deadlines = [moment + timedelta(seconds=grace), *[o.lease_until for o in objects if o.lease_until]]
    deadlines += [s.lease_until for s in db.scalars(select(StepExecution).where(StepExecution.run_id.in_(run_ids))) if s.lease_until]
    value = CaseDeletion(tenant_id=case.tenant_id, case_id=case.id, actor_id=actor_id,
        idempotency_key_hash=key_hash, request_hash=request_hash, status="queued", requested_at=moment,
        purge_after=max(deadlines), purge_manifest={"threads": sorted(threads), "object_keys": sorted(o.object_key for o in objects)})
    db.add(value)
    db.flush()
    value.ledger_sequence = DeletionLedger(settings).append(deletion_id=value.id, tenant_id=value.tenant_id,
        case_id=value.case_id, requested_at=value.requested_at, state="intent")
    case.status = CaseStatus.DELETING
    case.execution_fence += 1
    case.case_version += 1
    for grant in db.scalars(select(CaseAccessGrant).where(CaseAccessGrant.case_id == case.id)):
        db.delete(grant)
    db.add(QueueOutbox(tenant_id=case.tenant_id, topic="purge", dedupe_key=f"purge:{value.id}",
        payload={"deletion_id": str(value.id)}, next_attempt_at=value.purge_after))
    return value


async def request_deletion(db, principal, settings, case_id, body, key):
    payload = body.model_dump(mode="json")
    key_hash, digest = request_digest(key), request_digest(payload)
    lock = int(request_digest({"actor": principal.user_id, "case": case_id, "key": key})[:16], 16)
    if lock >= 2**63:
        lock -= 2**64
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    existing = (await db.execute(select(CaseDeletion).where(CaseDeletion.actor_id == principal.user_id,
        CaseDeletion.case_id == case_id, CaseDeletion.idempotency_key_hash == key_hash).with_for_update())).scalar_one_or_none()
    if existing:
        if existing.request_hash != digest:
            raise IdempotencyConflict()
        return existing
    case = await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
    case = (await db.execute(select(Case).where(Case.id == case_id).with_for_update())).scalar_one()
    if case.owner_user_id != principal.user_id:
        raise NotFoundOrForbidden()
    if case.status != CaseStatus.ACTIVE:
        from app.documents.retention import CaseGone
        raise CaseGone()
    if case.case_version != body.expected_case_version:
        raise VersionConflict(code="CASE_CHANGED", current={"case_version": case.case_version})
    if case.short_code != body.confirm_short_code:
        raise InvalidRequest("请输入当前单票的完整短编号确认删除")
    try:
        return await db.run_sync(lambda sync: register_deletion(sync, case, settings,
            actor_id=principal.user_id, key_hash=key_hash, request_hash=digest))
    except LedgerUnavailable as exc:
        raise ServiceUnavailable("独立删除账本不可用，尚未确认删除请求") from exc


def confirm_committed(settings, value):
    try:
        return DeletionLedger(settings).append(deletion_id=value.id, tenant_id=value.tenant_id, case_id=value.case_id,
            requested_at=value.requested_at, state="committed")
    except LedgerUnavailable as exc:
        raise ServiceUnavailable("单票已停止访问，删除账本确认暂不可用，可用同一请求重试") from exc
