"""已登记删除作业的幂等清理、TTL扫描与独立账本恢复重放。"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, or_, select, text, update

from app.config import get_settings
from app.documents.models import Case, CaseStatus, IdempotencyRecord
from app.identity.models import Tenant
from app.infra.database import create_sync_engine, create_sync_session_factory, sync_tenant_transaction
from app.infra.models_base import Base
from app.infra.object_store import ObjectStore
from app.management.deletion import register_deletion
from app.management.ledger import DeletionLedger
from app.management.models import CaseDeletion
from app.workflows.models import QueueOutbox


UUID_TOKEN=re.compile(r'(?<![0-9a-zA-Z])([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?![0-9a-zA-Z])')


def references_identifier(value,identifiers,*,scope=False):
    """JSON引用逐叶精确匹配，scope按完整UUID词界匹配，避免内容子串误删。"""
    if isinstance(value,dict):return any(references_identifier(child,identifiers) for child in value.values())
    if isinstance(value,(list,tuple)):return any(references_identifier(child,identifiers) for child in value)
    if not isinstance(value,(str,UUID)):return False
    value=str(value)
    return any(match.lower() in identifiers for match in UUID_TOKEN.findall(value)) if scope else value.lower() in identifiers


def selected_rows(db, case_id):
    # 导入领域模型，确保删除闭包包括所有已启用模块。
    from app.conversation import models as cm
    from app.documents import evidence_models
    from app.exports import models
    from app.extensions import models
    from app.preferences import models
    from app.review import models
    from app.workflows import models
    from app.consignment import models
    tables = list(Base.metadata.sorted_tables)
    selected = {}
    for table in tables:
        if table.name == "case_deletions" or "id" not in table.c or "tenant_id" not in table.c:
            continue
        condition = table.c.id == case_id if table.name == "cases" else table.c.case_id == case_id if "case_id" in table.c else None
        if condition is not None:
            selected[table.name] = set(db.scalars(select(table.c.id).where(condition)))
    # 一些父引用（会话暂存、行匹配、账本）无Case列，用真实FK闭包定位，不猜UUID文本。
    changed = True
    while changed:
        changed = False
        for table in tables:
            if table.name in {"case_deletions", "user_memories", "user_memory_versions"} or "id" not in table.c:
                continue
            conditions = [fk.parent.in_(selected[fk.column.table.name]) for fk in table.foreign_keys
                          if fk.column.name == "id" and selected.get(fk.column.table.name)]
            if "conversation_id" in table.c and selected.get("conversations"):
                conditions.append(table.c.conversation_id.in_(selected["conversations"]))
            if "run_id" in table.c and selected.get("runs"):
                conditions.append(table.c.run_id.in_(selected["runs"]))
            if not conditions:
                continue
            ids = set(db.scalars(select(table.c.id).where(or_(*conditions))))
            before = selected.setdefault(table.name, set())
            if ids - before:
                before.update(ids)
                changed = True
    return tables, selected


def delete_domain_rows(db, case_id, deletion_id):
    from app.preferences.models import UserMemory
    from app.workflows.models import Run
    tables, selected = selected_rows(db, case_id)
    # 投影指针不属于事实依赖；先清空再按FK逆序删除，失败整个事务回滚。
    db.execute(update(Case).where(Case.id==case_id).values(current_scope_revision_id=None,current_comparison_batch_id=None))
    db.execute(update(Run).where(Run.case_id==case_id).values(current_comparison_binding_id=None))
    conversations = selected.get("conversations", set())
    if conversations:
        db.execute(update(UserMemory).where(UserMemory.source_conversation_id.in_(conversations)).values(source_conversation_id=None))
    identifiers = {str(identifier) for ids in selected.values() for identifier in ids}
    # 只删能定位到该票的去重结果／队列消息，不清其他票的执行保护。
    for row in db.scalars(select(IdempotencyRecord)):
        if references_identifier(row.result_ref,identifiers) or references_identifier(row.scope,identifiers,scope=True):
            db.delete(row)
    for row in db.scalars(select(QueueOutbox)):
        if references_identifier(row.payload,identifiers|{str(deletion_id)}):
            db.delete(row)
    db.flush()
    for table in reversed(tables):
        ids = selected.get(table.name)
        if ids and table.name != "case_deletions":
            db.execute(delete(table).where(table.c.id.in_(ids)))
    # 在同一事务确认无领域行残留，失败回滚DB清理，访问仍关闭。
    _, remaining = selected_rows(db, case_id)
    if any(remaining.values()):
        raise RuntimeError("整票关联行尚未全部清除")


def execute_purge(deletion_id: str, *, now=None):
    settings = get_settings()
    engine = create_sync_engine(settings)
    factory = create_sync_session_factory(engine)
    moment = now or datetime.now(UTC)
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE logistics_worker"))
            tenant = conn.execute(text("SELECT worker_tenant_of_deletion(:id)"), {"id": UUID(deletion_id)}).scalar_one_or_none()
        if tenant is None:
            return {"status": "missing"}
        with factory() as db, sync_tenant_transaction(db, tenant):
            db.execute(text("SET LOCAL ROLE logistics_worker"))
            value = db.scalar(select(CaseDeletion).where(CaseDeletion.id == UUID(deletion_id)).with_for_update())
            if value.status == "completed":
                return {"status": "completed"}
            if value.purge_after > moment:
                return {"status": "queued", "purge_after": value.purge_after.isoformat()}
            value.status = "purging"
            manifest = value.purge_manifest
            case_id = value.case_id
        store = ObjectStore(settings)
        # 前缀涵盖已签PUT、正式对象各版本与没有DB元数据的迟到孤儿。
        removed = store.delete_prefix(f"t/{tenant}/c/{case_id}/")
        for key in manifest.get("object_keys", []):
            if not key.startswith(f"t/{tenant}/c/{case_id}/"):
                raise RuntimeError("删除对象不属于登记单票")
            removed += store.delete_all_versions(key)
        from app.infra.checkpoints import open_checkpointer,checkpoint_database_url
        with open_checkpointer(checkpoint_database_url(settings)) as saver:
            for thread in manifest.get("threads", []):
                saver.delete_thread(thread)
        # 明确二次空扫，在最晚签名/租约/在途时限之后才可能完成。
        removed += store.delete_prefix(f"t/{tenant}/c/{case_id}/")
        with factory() as db, sync_tenant_transaction(db, tenant):
            db.execute(text("SET LOCAL ROLE logistics_worker"))
            value = db.scalar(select(CaseDeletion).where(CaseDeletion.id == UUID(deletion_id)).with_for_update())
            if value.status == "completed":
                return {"status": "completed"}
            case = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
            if case and case.status != CaseStatus.DELETING:
                raise RuntimeError("未撤权单票不能被清理")
            delete_domain_rows(db, case_id, value.id)
            value.status = "completed"
            value.completed_at = moment
            value.last_error_class = None
            value.purge_manifest = {}
            value.ledger_sequence = DeletionLedger(settings).append(deletion_id=value.id, tenant_id=value.tenant_id,
                case_id=value.case_id, requested_at=value.requested_at, state="completed")
        return {"status": "completed", "removed_object_versions": removed}
    except Exception:
        if "tenant" in locals() and tenant:
            with factory() as db, sync_tenant_transaction(db, tenant):
                db.execute(text("SET LOCAL ROLE logistics_worker"))
                value = db.get(CaseDeletion, UUID(deletion_id))
                if value and value.status != "completed":
                    value.status = "failed"
                    value.last_error_class = "purge_dependency_failed"
        raise
    finally:
        engine.dispose()


def scan_retention(*, batch_size=50):
    settings = get_settings()
    ledger = DeletionLedger(settings)
    engine = create_sync_engine(settings)
    factory = create_sync_session_factory(engine)
    moment = datetime.now(UTC)
    count = 0
    try:
        with engine.begin() as conn:
            tenants = list(conn.execute(select(Tenant.id)).scalars())
        for tenant in tenants:
            with factory() as db, sync_tenant_transaction(db, tenant):
                db.execute(text("SET LOCAL ROLE logistics_worker"))
                expired = list(db.scalars(select(Case).where(Case.status == CaseStatus.ACTIVE, Case.retention_expires_at <= moment)
                    .order_by(Case.retention_expires_at).limit(batch_size).with_for_update(skip_locked=True)))
                for case in expired:
                    from app.api.idempotency import request_digest
                    register_deletion(db, case, settings, actor_id=case.owner_user_id,
                        key_hash=request_digest({"retention":case.id}), request_hash=request_digest({"retention":case.id}))
                    count += 1
                for value in db.scalars(select(CaseDeletion).where(CaseDeletion.status != "completed", CaseDeletion.purge_after <= moment)):
                    outbox = db.scalar(select(QueueOutbox).where(QueueOutbox.dedupe_key == f"purge:{value.id}"))
                    if outbox:
                        outbox.status = "pending"
                        outbox.next_attempt_at = moment
                    else:
                        db.add(QueueOutbox(tenant_id=tenant, topic="purge", dedupe_key=f"purge:{value.id}", payload={"deletion_id": str(value.id)}))
    finally:
        engine.dispose()
    return count


def replay_deletion_ledger(settings):
    """运维恢复命令：先保守撤权账本全部意图，再由purge作业清理，之后重新启动服务。"""
    rows = DeletionLedger(settings).records()
    targets = {(UUID(r["tenant_id"]), UUID(r["case_id"])) for r in rows}
    engine = create_sync_engine(settings)
    factory = create_sync_session_factory(engine)
    try:
        for tenant, case_id in targets:
            with factory() as db, sync_tenant_transaction(db, tenant):
                db.execute(text("SET LOCAL ROLE logistics_worker"))
                case = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
                existing = db.scalar(select(CaseDeletion).where(CaseDeletion.case_id == case_id).limit(1))
                if case and existing is None:
                    from app.api.idempotency import request_digest
                    register_deletion(db, case, settings, actor_id=case.owner_user_id,
                        key_hash=request_digest({"restore": case_id}), request_hash=request_digest({"restore": case_id}))
                elif case:
                    case.status = CaseStatus.DELETING
                    case.execution_fence += 1
    finally:
        engine.dispose()
