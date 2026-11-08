from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, InvalidRequest
from app.api.idempotency import request_digest
from app.config import Settings
from app.conversation.evidence import deterministic_answer, query_evidence, source_versions, verify_answer
from app.conversation.models import AssistantContinuation, Conversation, ConversationTurn, ReferenceSnapshot, TurnEvent
from app.documents.evidence_models import ParseArtifact
from app.workflows.models import RunEvent
from app.conversation.service import append_event, append_message, finish_answer, message_view
from app.documents.models import Case
from app.documents.retention import load_case_for_read
from app.identity.models import User
from app.identity.service import Principal
from app.infra.database import tenant_transaction
from app.workflows.models import QueueOutbox, Run, RunDocument, RunStatus

ACTIVE_CONTINUATIONS = {"waiting", "awaiting_retry", "awaiting_reconcile", "running"}


def enqueue_run_continuations(db: Session, run: Run) -> None:
    """Worker 在持 Case→Run 行锁的状态事务内调用；状态通知与 Outbox 一并提交。"""
    rows = list(db.execute(select(AssistantContinuation).where(AssistantContinuation.run_id == run.id,
        AssistantContinuation.tenant_id == run.tenant_id, AssistantContinuation.status.in_(ACTIVE_CONTINUATIONS))
        .order_by(AssistantContinuation.id).with_for_update()).scalars())
    for continuation in rows:
        state = run.status.value if hasattr(run.status, "value") else str(run.status)
        if state in {"indexed", "checked"}:
            key = f"continuation:{continuation.id}:epoch:{run.current_epoch}"
            existing = db.execute(select(QueueOutbox).where(QueueOutbox.tenant_id == run.tenant_id,
                QueueOutbox.dedupe_key == key)).scalar_one_or_none()
            if existing is None:
                db.add(QueueOutbox(tenant_id=run.tenant_id, topic="continuation", dedupe_key=key,
                    payload={"continuation_id": str(continuation.id)}, status="pending"))
            continue
        mapping = {"failed_retryable": ("awaiting_retry", "识别暂时失败，安全重试后仍会回答原问题。", "retry"),
            "paused_reconcile": ("awaiting_reconcile", "识别调用结果需核查，核查后继续回答原问题。", "reconcile"),
            "failed_final": ("failed", "识别已终止，无法确认原问题中的单证事实。", "new_run"),
            "cancelled": ("cancelled", "识别任务已取消。", "new_run"),
            "queued": ("waiting", "识别已恢复，等待本次结果。", ""),
            "processing": ("waiting", "识别已恢复，等待本次结果。", "")}
        if state not in mapping:
            continue
        status, text, next_action = mapping[state]
        if status == continuation.status:
            continue
        continuation.status = status
        continuation.updated_at = datetime.now(UTC)
        continuation.lease_token = None
        continuation.lease_until = None
        turn = db.execute(select(ConversationTurn).where(ConversationTurn.id == continuation.parent_turn_id).with_for_update()).scalar_one()
        turn.next_event_seq += 1
        db.add(TurnEvent(tenant_id=turn.tenant_id, turn_id=turn.id, seq=turn.next_event_seq,
            event_type="continuation_final" if status in {"failed", "cancelled"} else "continuation_status",
            payload={"continuation_id": str(continuation.id), "status": status, "stage": "只读识别", "message": text, "text": text,
                "next_action": next_action}))


async def notify_run_state(db, run):
    await db.run_sync(lambda sync_db: enqueue_run_continuations(sync_db, run))


async def register_check_continuation(db, conversation, turn, run, question):
    versions = [{"document_id": v["document_id"], "document_version": v["document_version"]}
        for v in run.task_spec["source_versions"]]
    continuation = AssistantContinuation(tenant_id=turn.tenant_id, conversation_id=conversation.id, parent_turn_id=turn.id,
        case_id=conversation.case_id, run_id=run.id, question=question, source_versions=versions,
        idempotency_key="check-turn:" + str(turn.id), status="waiting", attempts=0)
    db.add(continuation)
    await db.flush()
    await append_event(db, turn, "continuation_pending", {"continuation_id": str(continuation.id),
        "run_id": str(run.id), "status": "pending", "text": "核对完成后会继续汇总本票差异。"})
    return continuation


async def register_read_continuation(db, conversation, turn, snapshot, question, principal, settings, *, workflow_version=1, index_scope="selected_documents"):
    from app.workflows.commands import load_run, prepare_draft, start_run
    versions = source_versions(snapshot.frozen)
    # TaskSpec 只接受稳定原件版本，不携带解析附加元数据。
    versions = [{"document_id": v["document_id"], "document_version": v["document_version"]} for v in versions]
    if not conversation.case_id or not versions:
        raise InvalidRequest("请先选择已验收的文件")
    # 与 Worker 保持相同 Case→Run 排序，复用只接受精确输入与相同解析配置。
    await load_case_for_read(db, conversation.case_id, user_id=principal.user_id, settings=settings)
    await db.execute(select(Case).where(Case.id == conversation.case_id).with_for_update())
    runs = list((await db.execute(select(Run).where(Run.case_id == conversation.case_id, Run.workflow_type == "read_index",
        Run.workflow_version == workflow_version,
        Run.status.in_([RunStatus.QUEUED, RunStatus.PROCESSING, RunStatus.INDEXED, RunStatus.FAILED_RETRYABLE, RunStatus.PAUSED_RECONCILE]))
        .order_by(Run.created_at.desc()))).scalars())
    run = None
    wanted = {(v["document_id"], v["document_version"]) for v in versions}
    for candidate in runs:
        docs = list((await db.execute(select(RunDocument).where(RunDocument.run_id == candidate.id))).scalars())
        if {(str(d.document_id), d.document_version) for d in docs} == wanted and candidate.task_spec.get("index_scope", "selected_documents") == index_scope:
            run, _ = await load_run(db, principal, settings, candidate.id, write=True)
            break
    if run is None:
        draft = await prepare_draft(db, principal, settings, case_id=conversation.case_id, workflow_type="read_index",
            workflow_version=workflow_version,
            source_versions=versions, draft_parameters={"index_scope": index_scope}, conversation_id=conversation.id)
        run = await start_run(db, principal, settings, draft_task_id=draft.id, expected_draft_version=draft.draft_version,
            idempotency_key="read-turn:" + str(turn.id))
    conversation.active_run_id = run.id
    continuation = AssistantContinuation(tenant_id=turn.tenant_id, conversation_id=conversation.id, parent_turn_id=turn.id,
        case_id=conversation.case_id, run_id=run.id, question=question, source_versions=versions,
        idempotency_key="read-turn:" + str(turn.id), status="waiting", attempts=0)
    db.add(continuation)
    await db.flush()
    text = "识别已完成，正在核验来源。" if run.status == RunStatus.INDEXED else "正在识别以回答此问题。"
    await append_event(db, turn, "tool_finished", {"name": "read_index", "run_id": str(run.id), "status": "succeeded"})
    message = await finish_answer(db, conversation, turn, [text], [{"type": "run_link", "run_id": str(run.id)}])
    message.continuation = {"status": "pending", "text": text}
    await append_event(db, turn, "continuation_pending", {"continuation_id": str(continuation.id), "run_id": str(run.id), "status": "pending", "text": text}, message.id)
    await notify_run_state(db, run)
    return continuation


async def answer_continuation(session_factory, settings: Settings, continuation_id: UUID, tenant_id: UUID) -> None:
    """独立只读续答入口；tenant_id 必须由受限数据库解析取得，不采信队列载荷。"""
    async with session_factory() as db, tenant_transaction(db, tenant_id):
        continuation = await db.get(AssistantContinuation, continuation_id)
        if continuation is None or continuation.tenant_id != tenant_id or continuation.status not in ACTIVE_CONTINUATIONS:
            return
        # Case→Run→Continuation→Conversation→Turn，与登记和 Worker 的业务锁顺序一致。
        await db.execute(select(Case).where(Case.id == continuation.case_id).with_for_update())
        run = (await db.execute(select(Run).where(Run.id == continuation.run_id).with_for_update())).scalar_one_or_none()
        continuation = (await db.execute(select(AssistantContinuation).where(AssistantContinuation.id == continuation_id).with_for_update())).scalar_one()
        if continuation.status not in ACTIVE_CONTINUATIONS or run is None or run.status not in {RunStatus.INDEXED, RunStatus.CHECKED}:
            return
        now = datetime.now(UTC)
        if continuation.status == "running" and continuation.lease_until and continuation.lease_until > now:
            return
        conversation = (await db.execute(select(Conversation).where(Conversation.id == continuation.conversation_id).with_for_update())).scalar_one()
        turn = (await db.execute(select(ConversationTurn).where(ConversationTurn.id == continuation.parent_turn_id).with_for_update())).scalar_one()
        async def conclude(status, text, cards=None):
            continuation.status = status
            continuation.updated_at = now
            continuation.lease_token = None
            continuation.lease_until = None
            message = None
            if status not in {"closed", "restricted"}:
                message = await append_message(db, conversation, turn, text=text, cards=cards or [], parent_turn_id=turn.id,
                    continuation={"status": status, "text": "基于这次识别结果"})
                continuation.answer_message_id = message.id
            await append_event(db, turn, "continuation_final", {"continuation_id": str(continuation.id),
                "status": "conversation_closed" if status == "closed" else status, "text": text,
                "message": message_view(message) if message else text}, message.id if message else None)
        if conversation.status != "active" or conversation.archived_at or conversation.deleted_at:
            await conclude("closed", "索引已完成，需在新会话追问。")
            return
        user = await db.get(User, conversation.owner_user_id)
        try:
            if user is None or not user.is_active or user.tenant_id != tenant_id:
                raise ApiError()
            await load_case_for_read(db, continuation.case_id, user_id=user.id, settings=settings)
        except ApiError:
            await conclude("restricted", "当前授权已失效，自动续答已停止。")
            return
        docs = list((await db.execute(select(RunDocument).where(RunDocument.run_id == run.id).order_by(RunDocument.ordinal))).scalars())
        allowed = {(str(d.document_id), d.document_version) for d in docs}
        if any((v["document_id"], v["document_version"]) not in allowed for v in continuation.source_versions):
            await conclude("restricted", "原件归属已变化，自动续答已停止。")
            return
        original = await db.get(ReferenceSnapshot, turn.reference_snapshot_id)
        indexed_event = (await db.execute(select(RunEvent).where(RunEvent.run_id == run.id,
            RunEvent.status.in_(["indexed", "checked"])).order_by(RunEvent.seq.desc()).limit(1))).scalar_one_or_none()
        if indexed_event is None or not (indexed_event.payload or {}).get("business_revision_id"):
            await conclude("failed", "识别完成事件缺少冻结业务版本，请核对处理记录后重新提问。")
            return
        business_id = UUID(indexed_event.payload["business_revision_id"])
        parse_ids = (indexed_event.payload or {}).get("parse_artifact_ids", [])
        parses = list((await db.execute(select(ParseArtifact).where(ParseArtifact.id.in_([UUID(i) for i in parse_ids]),
            ParseArtifact.case_id == continuation.case_id, ParseArtifact.is_synthetic.is_(False)))).scalars()) if parse_ids else []
        frozen = []
        for version in continuation.source_versions:
            parse = next((p for p in parses if str(p.document_id) == version["document_id"] and p.document_version == version["document_version"]), None)
            if parse is None or parse.case_id != continuation.case_id:
                await conclude("failed", "索引已完成，但来源产物不可用，请在复核页面核对后重新提问。")
                return
            frozen.append({"kind": "document", "id": version["document_id"], "case_id": str(continuation.case_id),
                **version, "parse_artifact_id": str(parse.id), "business_revision_id": str(business_id)})
        if run.workflow_type == "consignment_check":
            from app.consignment.models import ComparisonBatch
            batch_id = (indexed_event.payload or {}).get("batch_id")
            batch = await db.get(ComparisonBatch, UUID(batch_id)) if batch_id else None
            if batch is None or batch.case_id != continuation.case_id or batch.run_id != run.id or batch.status != "completed" or batch.business_revision_id != business_id:
                await conclude("failed", "核对批次不可用，请重新打开当前核对结果。")
                return
            comparison = {"batch_id": str(batch.id), "scope_revision_id": str(batch.scope_revision_id),
                "input_fingerprint": batch.input_fingerprint, "manifest": batch.manifest, "issue_disposition_ids": []}
            frozen[0]["comparison"] = comparison
        fingerprint = request_digest(frozen)
        if continuation.answer_message_id and continuation.indexed_artifact_set_hash == fingerprint:
            return
        continuation.status = "running"
        continuation.attempts += 1
        continuation.lease_token = uuid4()
        continuation.lease_until = now + timedelta(seconds=120)
        continuation.indexed_artifact_set_hash = fingerprint
        await append_event(db, turn, "continuation_status", {"continuation_id": str(continuation.id), "status": "running", "text": "正在核验本次识别来源。"})
        # 临时只读快照只取原问题和本 Run 源版，不装配后续消息或后续标签。
        snapshot = ReferenceSnapshot(id=original.id, tenant_id=tenant_id, conversation_id=conversation.id,
            case_id=continuation.case_id, selections=original.selections, frozen=frozen, capabilities=[],
            business_revision_id=business_id, created_at=indexed_event.occurred_at)
        if run.workflow_type == "consignment_check":
            summary = batch.summary
            text = f"同票核对已完成：发现 {summary.get('red', 0)} 项红色冲突、{summary.get('yellow', 0)} 项黄色差异、{summary.get('unknown', 0)} 项待确认内容。"
            text += "\n\n请在“对比”页面查看关联、原始证据和处理建议。待确认内容不代表检查通过；原始冲突不会因修改译文而消失。"
            await conclude("completed", text, [{"type": "run_link", "run_id": str(run.id)}])
            return
        facts = await query_evidence(db, snapshot, continuation.question, settings)
        texts, cards = await verify_answer(db, snapshot, deterministic_answer(facts), facts)
        await conclude("completed", "基于这次识别结果：\n\n" + "\n\n".join(texts), [{"type": "evidence", "cards": cards}] if cards else [])
