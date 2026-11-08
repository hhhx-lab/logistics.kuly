"""按租户扫描数据库 Outbox，至少一次投递；消费者用租约与指纹去重。"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text

from app.config import get_settings
from app.identity.models import Tenant
from app.infra.database import create_sync_engine, create_sync_session_factory, sync_tenant_transaction
from app.workflows.models import QueueOutbox
from app.workflows.worker import queue

logger = logging.getLogger(__name__)


def publish_once(*, batch_size: int = 50) -> int:
    if batch_size <= 0:
        return 0
    engine = create_sync_engine(get_settings())
    factory = create_sync_session_factory(engine)
    published = 0
    try:
        with engine.begin() as conn:
            # 部署服务持有配置角色；跨租户扫描只枚举认证目录 ID，业务扫描仍 FORCE RLS。
            tenant_ids = list(conn.execute(select(Tenant.id).order_by(Tenant.id)).scalars())
        if not tenant_ids:return 0
        from redis import Redis
        with Redis.from_url(get_settings().redis_url,socket_connect_timeout=3,socket_timeout=3) as broker:
            position=broker.incrby("lda:outbox:tenant_cursor",batch_size)-batch_size
        tenant_ids=tenant_ids[position%len(tenant_ids):]+tenant_ids[:position%len(tenant_ids)]
        for index in range(batch_size):
            tenant_id=tenant_ids[index%len(tenant_ids)]
            with factory() as db, sync_tenant_transaction(db, tenant_id):
                db.execute(text("SET LOCAL ROLE logistics_worker"))
                rows = db.scalars(select(QueueOutbox).where(QueueOutbox.status.in_(["pending", "failed"]),
                    QueueOutbox.next_attempt_at <= datetime.now(UTC)).order_by(QueueOutbox.created_at, QueueOutbox.id)
                    .limit(1).with_for_update(skip_locked=True))
                for row in rows:
                    row.attempts += 1
                    try:
                        payload = row.payload
                        if row.topic == "run":
                            queue.send_task("document_agent.run", kwargs={"run_id": str(UUID(payload["run_id"])), "epoch": int(payload["epoch"])})
                        elif row.topic == "continuation":
                            queue.send_task("document_agent.continuation", kwargs={"continuation_id": str(UUID(payload["continuation_id"]))})
                        elif row.topic == "turn":
                            queue.send_task("document_agent.turn", kwargs={"turn_id": str(UUID(payload["turn_id"]))})
                        elif row.topic == "summary":
                            queue.send_task("document_agent.summary", kwargs={"job_id": str(UUID(payload["job_id"])), "turn_id": str(UUID(payload["turn_id"]))})
                        elif row.topic == "purge":
                            queue.send_task("document_agent.purge", kwargs={"deletion_id": str(UUID(payload["deletion_id"]))})
                        else:
                            raise ValueError("未知队列主题")
                        row.status = "published"
                        row.published_at = datetime.now(UTC)
                        row.last_error = None
                        published += 1
                    except Exception:
                        row.status = "failed"
                        row.last_error = "投递失败，等待有界退避重试"
                        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
    finally:
        engine.dispose()
    return published


async def publisher_loop(stop: asyncio.Event):
    from time import monotonic
    from app.infra.recovery import recovery_tick
    from app.ops.observability import record_cycle
    next_recovery = monotonic() + get_settings().recovery_scan_seconds
    next_retention = monotonic()
    last_warning = {}
    while not stop.is_set():
        category = "publish"
        started = monotonic()
        try:
            await asyncio.to_thread(publish_once)
            await asyncio.to_thread(record_cycle, get_settings(), category, success=True, elapsed=monotonic()-started)
            if monotonic() >= next_recovery:
                category = "recovery"
                started = monotonic()
                next_recovery = monotonic() + get_settings().recovery_scan_seconds
                await asyncio.to_thread(recovery_tick)
                await asyncio.to_thread(record_cycle, get_settings(), category, success=True, elapsed=monotonic()-started)
            if monotonic() >= next_retention:
                category = "retention"
                started = monotonic()
                next_retention = monotonic() + get_settings().case_purge_scan_seconds
                if get_settings().deletion_ledger_path and get_settings().deletion_ledger_key:
                    from app.management.purge import scan_retention
                    await asyncio.to_thread(scan_retention)
                    await asyncio.to_thread(record_cycle, get_settings(), category, success=True, elapsed=monotonic()-started)
        except Exception as exc:
            await asyncio.to_thread(record_cycle, get_settings(), category, success=False, elapsed=monotonic()-started)
            # Connection/scanner failures can precede any row-level ledger.
            # Never log exception text or traceback: either may contain secrets.
            moment = monotonic()
            if moment - last_warning.get(category, float("-inf")) >= 60:
                logger.warning("Outbox background cycle failed; category=%s error_class=%s", category, type(exc).__name__)
                last_warning[category] = moment
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
