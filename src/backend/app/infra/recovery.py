"""有界恢复扫描：过期未发请求可恢复，未知收费状态暂停等待运营核查。"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text,func,String

from app.config import get_settings
from app.documents.models import Case, CaseStatus
from app.identity.models import Tenant
from app.infra.database import create_sync_engine, create_sync_session_factory, sync_tenant_transaction
from app.workflows.models import ExecutionEpoch, QueueOutbox, Run, RunEvent, RunStatus, StepExecution, StepStatus


def closed_interrupted_model(step):
    """只恢复已关闭的同一模型尝试；未知受理与后续在途请求继续核查。"""
    usage=step.usage or {}
    marker=usage.get("model_attempt") or {}
    calls=usage.get("model_calls") or []
    return bool(calls and step.provider_request_key
        and marker.get("state")=="finished" and marker.get("lease_token")==str(step.lease_token)
        and marker.get("provider_request_key")==step.provider_request_key
        and all(isinstance(call,dict) and call.get("provider_request_key")==step.provider_request_key
            and call.get("outcome")!="needs_reconcile" for call in calls)
        and calls[-1].get("call_id")==marker.get("call_id") and calls[-1].get("finished_at")
        and calls[-1].get("outcome")=="interrupted" and calls[-1].get("role") in {"chat","extract","translate","vision"}
        and not step.external_task_id)


def retryable_interrupted_model(step, *, max_attempts, attempts_used):
    return attempts_used<max_attempts and closed_interrupted_model(step)


def recover_expired_turn(db, turn, moment):
    from app.conversation.models import ConversationWorkItem, TurnEvent
    if turn.status not in {"pending", "running"} or turn.final_message_id or turn.lease_until and turn.lease_until > moment:
        return False
    def interrupt(code, message, event_code=None):
        turn.status = "interrupted"
        turn.error_class = code
        turn.finished_at = moment
        turn.lease_token = None
        turn.lease_until = None
        turn.next_event_seq += 1
        db.add(TurnEvent(tenant_id=turn.tenant_id, turn_id=turn.id, seq=turn.next_event_seq, event_type="message_error",
            payload={"code": event_code or code, "text": message}))
    pending = db.scalar(select(ConversationWorkItem.id).where(ConversationWorkItem.turn_id == turn.id,
        (ConversationWorkItem.kind.in_(["model_pending", "general_pending", "answer_pending"]) |
         (ConversationWorkItem.kind == "summary_pending") & ConversationWorkItem.payload["owner_user_id"].astext.is_(None))).limit(1))
    if pending:
        interrupt("lease_expired_acceptance_unknown", "模型请求受理结果待核查，本轮未自动重投", "MODEL_ACCEPTANCE_UNKNOWN")
        return False
    key = f"turn-recovery:{turn.id}"
    existing = db.scalar(select(QueueOutbox).where(QueueOutbox.dedupe_key == key))
    if existing and existing.status != "published":
        return False
    recoveries = db.scalar(select(func.count()).select_from(ConversationWorkItem).where(
        ConversationWorkItem.turn_id == turn.id, ConversationWorkItem.kind == "safe_recovery"))
    if recoveries >= 3:
        interrupt("safe_recovery_budget_exhausted", "本轮安全恢复次数已用完，请重新提问；已完成步骤记录保留。")
        return False
    turn.status = "pending"
    turn.lease_token = None
    turn.lease_until = None
    if existing:
        existing.status = "pending"
        existing.next_attempt_at = moment
        existing.published_at = None
        existing.last_error = None
    else:
        db.add(QueueOutbox(tenant_id=turn.tenant_id, topic="turn", dedupe_key=key, payload={"turn_id": str(turn.id)}))
    db.add(ConversationWorkItem(tenant_id=turn.tenant_id, conversation_id=turn.conversation_id, turn_id=turn.id,
        kind="safe_recovery", payload={"attempt": recoveries + 1}, expires_at=moment + timedelta(days=1)))
    return True


def recovery_tick(*,batch_size:int=50)->dict:
    settings=get_settings();engine=create_sync_engine(settings);factory=create_sync_session_factory(engine)
    recovered=paused=0;moment=datetime.now(UTC)
    try:
        with engine.begin() as conn:tenants=list(conn.execute(select(Tenant.id)).scalars())
        for tenant_id in tenants:
            with factory() as db,sync_tenant_transaction(db,tenant_id):
                db.execute(text("SET LOCAL ROLE logistics_worker"))
                from app.conversation.lifecycle import purge_deleted_conversations
                purge_deleted_conversations(db, settings, moment, batch_size)
                from app.conversation.summary import recover_summary_jobs
                recover_summary_jobs(db, moment, batch_size)
                from app.exports.worker import recover_expired_exports
                recovered += recover_expired_exports(db, moment, batch_size)
                # 与所有写命令一致，先锁Case后Run；SKIP LOCKED不争抢活跃写事务。
                live_lease=select(StepExecution.id).where(StepExecution.run_id==Run.id,StepExecution.epoch==Run.current_epoch,
                    StepExecution.status.in_([StepStatus.RUNNING,StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE]),StepExecution.lease_until>moment).exists()
                pending_delivery=select(QueueOutbox.id).where(QueueOutbox.topic=='run',QueueOutbox.status.in_(['pending','failed']),
                    QueueOutbox.payload['run_id'].astext==Run.id.cast(String),QueueOutbox.payload['epoch'].astext==Run.current_epoch.cast(String)).exists()
                stale_step=select(StepExecution.id).where(StepExecution.run_id==Run.id,StepExecution.epoch==Run.current_epoch,
                    StepExecution.status.in_([StepStatus.RUNNING,StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE])).exists()
                closed_interruption=select(StepExecution.id).where(StepExecution.run_id==Run.id,
                    StepExecution.epoch==Run.current_epoch,StepExecution.error_class=="expired_lease_acceptance_unknown",
                    StepExecution.usage["model_attempt"]["state"].astext=="finished",
                    StepExecution.usage["model_attempt"]["outcome"].astext=="interrupted").exists()
                eligible_status=Run.status.in_([RunStatus.QUEUED,RunStatus.PROCESSING]) | (
                    (Run.status==RunStatus.PAUSED_RECONCILE) & closed_interruption)
                candidate=eligible_status & ~live_lease & ~pending_delivery & (stale_step | (Run.updated_at<moment-timedelta(minutes=15)))
                # Only actionable Runs consume the batch; oldest checked work rotates
                # because every recovery action updates Run.updated_at.
                case_candidates=select(Run.case_id,func.min(Run.updated_at).label('oldest')).where(candidate).group_by(Run.case_id).subquery()
                cases=list(db.scalars(select(Case).join(case_candidates,case_candidates.c.case_id==Case.id)
                    .where(Case.status==CaseStatus.ACTIVE,Case.retention_expires_at>moment)
                    .order_by(case_candidates.c.oldest,Case.id).limit(batch_size).with_for_update(skip_locked=True,of=Case)))
                for case in cases:
                    runs=list(db.scalars(select(Run).where(Run.case_id==case.id,candidate)
                        .order_by(Run.updated_at,Run.id).limit(batch_size).with_for_update(skip_locked=True)))
                    for run in runs:
                        steps=list(db.scalars(select(StepExecution).where(StepExecution.run_id==run.id,StepExecution.epoch==run.current_epoch,
                            StepExecution.status.in_([StepStatus.RUNNING,StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE])).with_for_update()))
                        if any(step.lease_until and step.lease_until>moment for step in steps):continue
                        known_ocr=[step for step in steps if step.status==StepStatus.SUBMITTING and step.step_name.startswith("解析:") and step.external_task_id]
                        unknown=[step for step in steps if step.status in {StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE} and step not in known_ocr]
                        interrupted=[];exhausted=[]
                        for step in unknown:
                            if run.status==RunStatus.PAUSED_RECONCILE and step.error_class!="expired_lease_acceptance_unknown":
                                continue
                            usages=db.scalars(select(StepExecution.usage).where(StepExecution.run_id==run.id,
                                StepExecution.input_fingerprint==step.input_fingerprint,StepExecution.epoch==step.epoch,
                                (StepExecution.id==step.id) | (StepExecution.error_class=="interrupted_model_recovery")))
                            attempts=sum(len((usage or {}).get("model_calls") or []) for usage in usages)
                            if settings.model_retry_interrupted and retryable_interrupted_model(step,
                                max_attempts=settings.model_max_attempts,attempts_used=attempts):
                                interrupted.append(step)
                            elif settings.model_retry_interrupted and closed_interrupted_model(step):
                                exhausted.append(step)
                        if run.status==RunStatus.PAUSED_RECONCILE and (not unknown or len(interrupted)+len(exhausted)!=len(unknown)):
                            continue
                        if exhausted and len(interrupted)+len(exhausted)==len(unknown) and not known_ocr:
                            for step in interrupted+exhausted:
                                step.status=StepStatus.FAILED;step.error_class="transient_exhausted";step.lease_until=None
                            run.status=RunStatus.FAILED_RETRYABLE;run.execution_fence+=1
                            run.failure_reason="Model requests were interrupted and the automatic retry limit was reached. Completed results are preserved; you can retry this task."
                            paused+=1
                        elif unknown and len(interrupted)==len(unknown) and not known_ocr:
                            for step in interrupted:
                                step.status=StepStatus.FAILED;step.error_class="interrupted_model_recovery"
                                step.lease_until=None;step.finished_at=moment
                            for step in steps:
                                if step.status==StepStatus.RUNNING:
                                    step.status=StepStatus.FAILED;step.error_class="lease_expired_before_submit";step.lease_until=None
                            run.status=RunStatus.QUEUED;run.failure_reason=None;run.execution_fence+=1
                            db.add(QueueOutbox(tenant_id=tenant_id,topic="run",
                                dedupe_key=f"run-model-resume:{run.id}:{run.current_epoch}:{run.execution_fence}",
                                payload={"run_id":str(run.id),"epoch":run.current_epoch}))
                            recovered+=1
                        elif unknown:
                            for step in unknown:step.status=StepStatus.NEEDS_RECONCILE;step.error_class="expired_lease_acceptance_unknown"
                            run.status=RunStatus.PAUSED_RECONCILE;run.failure_reason="请求租约过期且供应商受理不明，需运营核查"
                            run.execution_fence+=1;paused+=1
                        elif known_ocr:
                            pending=db.scalar(select(QueueOutbox.id).where(QueueOutbox.status.in_(["pending","failed"]),QueueOutbox.topic=="run",
                                QueueOutbox.payload["run_id"].astext==str(run.id),QueueOutbox.payload["epoch"].astext==str(run.current_epoch)).limit(1))
                            if pending:continue
                            for step in known_ocr:step.lease_until=None;step.error_class="ocr_poll_pending"
                            run.execution_fence+=1;run.status=RunStatus.QUEUED
                            db.add(QueueOutbox(tenant_id=tenant_id,topic="run",dedupe_key=f"run-ocr-resume:{run.id}:{run.current_epoch}:{run.execution_fence}",payload={"run_id":str(run.id),"epoch":run.current_epoch}))
                            recovered+=1
                        elif steps:
                            for step in steps:step.status=StepStatus.FAILED;step.error_class="lease_expired_before_submit"
                            epoch=db.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id,ExecutionEpoch.epoch==run.current_epoch))
                            run.current_epoch+=1;run.execution_fence+=1;run.status=RunStatus.QUEUED
                            purpose=epoch.purpose if epoch.purpose in {"issue_repair","issue_assessment","quality_preview","layout_refresh"} else "retry"
                            db.add(ExecutionEpoch(tenant_id=tenant_id,run_id=run.id,epoch=run.current_epoch,purpose=purpose,graph_version=epoch.graph_version,
                                input_business_revision_id=run.current_business_revision_id,input_parse_artifact_ids=epoch.input_parse_artifact_ids,model_bindings=epoch.model_bindings,
                                repair_request=dict(epoch.repair_request or {}) if purpose!="retry" else {}))
                            db.add(QueueOutbox(tenant_id=tenant_id,topic="run",dedupe_key=f"run:{run.id}:{run.current_epoch}",payload={"run_id":str(run.id),"epoch":run.current_epoch}))
                            recovered+=1
                        elif run.updated_at<moment-timedelta(minutes=15):
                            existing=db.scalar(select(QueueOutbox).where(QueueOutbox.dedupe_key==f"run:{run.id}:{run.current_epoch}"))
                            if existing and existing.status in {"pending","failed"}:continue
                            if existing:
                                existing.status="pending";existing.next_attempt_at=moment
                            else:
                                db.add(QueueOutbox(tenant_id=tenant_id,topic="run",dedupe_key=f"run:{run.id}:{run.current_epoch}",payload={"run_id":str(run.id),"epoch":run.current_epoch}))
                            recovered+=1
                        else:continue
                        run.next_event_seq+=1;run.updated_at=moment
                        db.add(RunEvent(tenant_id=tenant_id,run_id=run.id,seq=run.next_event_seq,event_type="run_status",status=run.status.value,
                            stage="恢复检查",message=run.failure_reason or "检测到中断，已安排有界安全恢复",kind="ops"))
                from app.conversation.models import AssistantContinuation,Conversation,ConversationTurn,ConversationWorkItem,TurnEvent
                def continuation_final(item,status,message):
                    item.status=status;item.updated_at=moment;item.lease_token=None;item.lease_until=None
                    turn=db.scalar(select(ConversationTurn).where(ConversationTurn.id==item.parent_turn_id).with_for_update())
                    if turn:
                        turn.next_event_seq+=1
                        db.add(TurnEvent(tenant_id=tenant_id,turn_id=turn.id,seq=turn.next_event_seq,event_type="continuation_final",
                            payload={"continuation_id":str(item.id),"status":status,"text":message}))
                continuations=list(db.scalars(select(AssistantContinuation).where(AssistantContinuation.status.in_(["waiting","awaiting_retry","running"]),
                    (AssistantContinuation.next_attempt_at.is_(None)) | (AssistantContinuation.next_attempt_at<=moment))
                    .order_by(AssistantContinuation.updated_at).limit(batch_size)))
                for continuation in continuations:
                    if continuation.lease_until and continuation.lease_until>moment:continue
                    case=db.scalar(select(Case).where(Case.id==continuation.case_id).with_for_update(skip_locked=True))
                    if case is None:continue
                    conversation=db.scalar(select(Conversation).where(Conversation.id==continuation.conversation_id).with_for_update(skip_locked=True))
                    if conversation is None:continue
                    turn=db.scalar(select(ConversationTurn).where(ConversationTurn.id==continuation.parent_turn_id).with_for_update(skip_locked=True))
                    if turn is None:continue
                    continuation=db.scalar(select(AssistantContinuation).where(AssistantContinuation.id==continuation.id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
                    if continuation is None or continuation.status not in {"waiting","awaiting_retry","running"} or continuation.lease_until and continuation.lease_until>moment:continue
                    if case is None or case.status!=CaseStatus.ACTIVE or case.retention_expires_at<=moment:
                        continuation_final(continuation,"restricted","单票已不可访问，后续回答已停止");continue
                    if conversation is None or conversation.status!="active":continuation_final(continuation,"closed","会话已关闭，后续回答已停止");continue
                    run=db.get(Run,continuation.run_id)
                    if run is None or run.status!=RunStatus.INDEXED:continue
                    if continuation.answer_message_id:
                        continuation.status="completed";continue
                    if continuation.attempts>=3:
                        continuation.error_class="retry_budget_exhausted";continuation_final(continuation,"failed","后续回答安全恢复次数已用完，请重新提问");continue
                    # 当前续答消费者只做确定性证据查询，无模型提交；到期可安全恢复。
                    continuation.status="awaiting_retry";continuation.lease_token=None;continuation.lease_until=None
                    continuation.next_attempt_at=moment+timedelta(seconds=30)
                    key=f"continuation-recovery:{continuation.id}:{continuation.attempts}"
                    outbox=db.scalar(select(QueueOutbox).where(QueueOutbox.dedupe_key==key))
                    if outbox is None:db.add(QueueOutbox(tenant_id=tenant_id,topic="continuation",dedupe_key=key,payload={"continuation_id":str(continuation.id)}))
                    recovered+=1
                turns=list(db.scalars(select(ConversationTurn).where(ConversationTurn.status.in_(["pending","running"]),ConversationTurn.created_at<moment-timedelta(minutes=3))
                    .order_by(ConversationTurn.created_at).limit(batch_size)))
                for turn in turns:
                    if turn.lease_until and turn.lease_until>moment:continue
                    conversation=db.get(Conversation,turn.conversation_id)
                    if conversation is None:continue
                    if conversation.case_id:
                        case=db.scalar(select(Case).where(Case.id==conversation.case_id).with_for_update(skip_locked=True))
                        if case is None:continue
                    conversation=db.scalar(select(Conversation).where(Conversation.id==turn.conversation_id).with_for_update(skip_locked=True))
                    if conversation is None:continue
                    turn=db.scalar(select(ConversationTurn).where(ConversationTurn.id==turn.id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
                    if turn is None or turn.status not in {"pending","running"} or turn.lease_until and turn.lease_until>moment:continue
                    if turn.final_message_id:turn.status="completed";continue
                    recovered += int(recover_expired_turn(db, turn, moment))
    finally:engine.dispose()
    return {"recovered":recovered,"paused_reconcile":paused}
