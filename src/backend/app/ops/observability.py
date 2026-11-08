"""进程循环活性和有界聚合指标；无票据内容/身份标签，失败不影响领域事务。"""
from datetime import UTC, datetime, timedelta
import time

from redis import Redis
from sqlalchemy import func, select, text
from decimal import Decimal

CYCLES = ("publish", "recovery", "retention", "monitor")


def outbox_counters(db, now):
    """计划内退避不算积压，延迟从应投递时间起算。"""
    from app.workflows.models import QueueOutbox
    due = (QueueOutbox.status.in_(("pending", "failed")), QueueOutbox.next_attempt_at <= now)
    oldest = db.scalar(select(func.min(QueueOutbox.next_attempt_at)).where(*due))
    return {"outbox_pending": db.scalar(select(func.count()).select_from(QueueOutbox).where(QueueOutbox.status == "pending")),
        "outbox_failed": db.scalar(select(func.count()).select_from(QueueOutbox).where(QueueOutbox.status == "failed")),
        "outbox_due_count": db.scalar(select(func.count()).select_from(QueueOutbox).where(*due)),
        "outbox_oldest_age_seconds": max(0, (now - oldest).total_seconds()) if oldest else 0.0}


def record_cycle(settings, category, *, success, elapsed=0):
    if category not in CYCLES or not settings.redis_url:
        return False
    try:
        with Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2) as client:
            pipeline = client.pipeline(transaction=True)
            key = "lda:operations:cycle:" + category
            pipeline.hincrby(key, "successes" if success else "failures", 1)
            values = {"last_attempt": time.time(), "last_duration_seconds": max(0, elapsed)}
            if success:
                values["last_success"] = values["last_attempt"]
            pipeline.hset(key, mapping=values)
            pipeline.expire(key, 86400)
            pipeline.execute()
        return True
    except Exception:
        return False


def collect_operational_metrics(settings, *, check_workers=False, tenant_id=None):
    """HTTP只传当前tenant；本机运维工具枚举认证目录后仍逐tenant FORCE RLS。"""
    from app.conversation.models import ConversationTurn
    from app.identity.models import Tenant
    from app.infra.database import create_sync_engine, create_sync_session_factory, sync_tenant_transaction
    from app.management.models import CaseDeletion
    from app.workflows.models import QueueOutbox, Run, RunStatus, StepExecution, StepStatus
    now = datetime.now(UTC)
    alerts = []
    cycles = []
    workers = []
    with Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3) as client:
        for category in CYCLES:
            row = client.hgetall("lda:operations:cycle:" + category)
            threshold = max(90, settings.recovery_scan_seconds * 3) if category == "recovery" else max(90, settings.case_purge_scan_seconds * 3) if category == "retention" else 90
            age = max(0, now.timestamp() - float(row["last_success"])) if row.get("last_success") else None
            active = age is not None and age <= threshold
            cycles.append({"category": category, "active": active, "last_success_age_seconds": age,
                "successes": int(row.get("successes", 0)), "failures": int(row.get("failures", 0)), "last_duration_seconds": float(row.get("last_duration_seconds", 0))})
            if not active:
                alerts.append({"code": "CYCLE_STALE", "component": category, "action": "检查API后台循环、数据库/Redis及对应扫描器；不要盲重投未知供应商请求"})
    if check_workers:
        from app.workflows.worker import queue
        replies = queue.control.ping(timeout=3)
        names = [name for reply in replies for name in reply]
        unknown_consumer = any(not name.startswith(("documents@", "conversation@", "maintenance@")) for name in names)
        for category in ("documents", "conversation", "maintenance"):
            active = any(name.startswith(category + "@") for name in names)
            # 老进程未按队列命名时ping只能证实存活，不能推断它消费哪个队列。
            observed = active or not unknown_consumer
            workers.append({"queue": category, "active": active if observed else None, "state": "active" if active else "inactive" if observed else "unknown"})
            if observed and not active:
                alerts.append({"code": "WORKER_UNAVAILABLE", "component": category, "action": "检查该队列消费者进程、broker连接和积压；业务结果仍以领域账本为准"})
    model_rows = []
    aggregate = {"outbox_pending": 0, "outbox_failed": 0, "outbox_due_count": 0, "outbox_oldest_age_seconds": 0.0, "run_paused_reconcile": 0,
        "step_retryable_failures": 0, "turn_expired_leases": 0, "deletion_overdue_24h": 0}
    engine = create_sync_engine(settings)
    try:
        with engine.connect() as connection:
            tenants = [tenant_id] if tenant_id else list(connection.execute(select(Tenant.id)).scalars())
        factory = create_sync_session_factory(engine)
        for tenant in tenants:
            with factory() as db, sync_tenant_transaction(db, tenant):
                db.execute(text("SET LOCAL ROLE logistics_worker"))
                for key, value in outbox_counters(db, now).items():
                    aggregate[key] = max(aggregate[key], value) if key == "outbox_oldest_age_seconds" else aggregate[key] + value
                aggregate["run_paused_reconcile"] += db.scalar(select(func.count()).select_from(Run).where(Run.status == RunStatus.PAUSED_RECONCILE))
                aggregate["step_retryable_failures"] += db.scalar(select(func.count()).select_from(StepExecution).where(StepExecution.status == StepStatus.FAILED, StepExecution.created_at >= now - timedelta(hours=24)))
                # 24h且每租户最多1000步/1000会话账本；仅投影计量键，不把提示词/单据内容取入指标。
                step_calls = db.scalars(select(StepExecution.usage["model_calls"]).where(
                    StepExecution.created_at >= now - timedelta(hours=24), StepExecution.usage.is_not(None)).order_by(StepExecution.created_at.desc()).limit(1000))
                from app.conversation.models import ConversationWorkItem
                chat_calls = db.scalars(select(ConversationWorkItem.payload).where(ConversationWorkItem.kind == "model_call",
                    ConversationWorkItem.created_at >= now - timedelta(hours=24)).order_by(ConversationWorkItem.created_at.desc()).limit(1000))
                for calls in step_calls:
                    model_rows.extend(calls or [])
                model_rows.extend(chat_calls)
                aggregate["turn_expired_leases"] += db.scalar(select(func.count()).select_from(ConversationTurn).where(ConversationTurn.status.in_(("pending", "running")), ConversationTurn.lease_until < now))
                aggregate["deletion_overdue_24h"] += db.scalar(select(func.count()).select_from(CaseDeletion).where(CaseDeletion.status != "completed", CaseDeletion.requested_at < now - timedelta(hours=24)))
    finally:
        engine.dispose()
    real = [row for row in model_rows if not row.get("is_fake")]
    durations = sorted(max(0, row.get("duration_ms", 0)) for row in real)
    known_cost = sum((Decimal(str(row["cost_cny"])) for row in real if row.get("cost_cny") is not None), Decimal(0))
    metering = {"window_hours": 24, "bounded_per_tenant": True, "model_call_count": len(real),
        "model_tokens": sum((row.get("usage") or {}).get("total_tokens") or 0 for row in real),
        "missing_usage_count": sum((row.get("usage") or {}).get("total_tokens") is None for row in real),
        "known_model_cost_cny": str(known_cost), "unknown_cost_count": sum(row.get("cost_cny") is None for row in real),
        "model_duration_p50_ms": durations[min(len(durations)-1, int(len(durations)*0.5))] if durations else None,
        "model_duration_p95_ms": durations[min(len(durations)-1, int(len(durations)*0.95))] if durations else None,
        "cost_scope": "model_only", "ocr_cost_known": False}
    if aggregate["outbox_oldest_age_seconds"] > 300:
        alerts.append({"code": "OUTBOX_DELAY", "component": "publish", "action": "检查投递失败类、循环心跳与broker；确认事务和消费者幂等后恢复"})
    if aggregate["deletion_overdue_24h"]:
        alerts.append({"code": "DELETION_SLA", "component": "retention", "action": "立即核查独立删除账本、对象所有版本和检查点清理；禁止恢复访问"})
    return {"status": "attention" if alerts else "ok", "probed_at": now.isoformat(), "cycles": cycles,
        "workers": workers, "model_metering": metering, "metrics": aggregate, "alerts": alerts, "scope": "current_tenant" if tenant_id else "deployment_aggregate"}


def prometheus_text(report):
    lines = ["# 运维聚合，无票据或用户标签"]
    for name, value in report["metrics"].items():
        lines.append(f"logistics_{name} {value}")
    for name, value in report.get("model_metering", {}).items():
        if name in {"model_call_count", "model_tokens", "missing_usage_count", "unknown_cost_count", "model_duration_p50_ms", "model_duration_p95_ms", "known_model_cost_cny"} and value is not None:
            lines.append(f"logistics_{name} {value}")
    for row in report["cycles"]:
        labels = '{category="' + row["category"] + '"}'
        lines.extend((f"logistics_cycle_active{labels} {int(row['active'])}", f"logistics_cycle_failures{labels} {row['failures']}"))
    for row in report["workers"]:
        if row["active"] is not None:
            lines.append('logistics_worker_active{queue="' + row["queue"] + '"} ' + str(int(row["active"])))
    return "\n".join(lines) + "\n"
