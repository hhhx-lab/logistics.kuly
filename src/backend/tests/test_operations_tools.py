import importlib.util
from pathlib import Path
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
import base64
import os

import pytest
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("recovery_tools", ROOT / "ops/disaster_recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def test_backup_encryption_rejects_tamper_and_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    plain = tmp_path / "plain"
    plain.write_bytes(os.urandom(recovery.BLOCK + 123))
    encrypted = tmp_path / "encrypted"
    recovery.encrypt(plain, encrypted, recovery.cipher())
    recovery.decrypt(encrypted, tmp_path / "restored", recovery.cipher())
    assert (tmp_path / "restored").read_bytes() == plain.read_bytes()
    damaged = tmp_path / "damaged"
    damaged.write_bytes(encrypted.read_bytes()[:-32])
    with pytest.raises(Exception): recovery.decrypt(damaged, tmp_path / "bad", recovery.cipher())
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    with pytest.raises(Exception): recovery.decrypt(encrypted, tmp_path / "wrong-key", recovery.cipher())


def test_independent_ledger_mirror_and_rotation(tmp_path):
    from app.management.ledger import DeletionLedger
    settings = SimpleNamespace(deletion_ledger_path=str(tmp_path / "source"), deletion_ledger_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()))
    ledger = DeletionLedger(settings)
    ledger.append(deletion_id=uuid4(), tenant_id=uuid4(), case_id=uuid4(), requested_at=datetime.now(UTC), state="intent")
    anchor = recovery.mirror_ledger(settings, tmp_path / "mirror")
    assert anchor["sequence"] == 1
    assert (tmp_path / "mirror/deletions.jsonl.enc").read_bytes() == ledger.path.read_bytes()
    with pytest.raises(ValueError): recovery.mirror_ledger(settings, tmp_path / "source")
    old = tmp_path / "old.lda.enc"; old.write_bytes(b"encrypted")
    os.utime(old, (0, 0))
    assert recovery.rotate(tmp_path, 7)["removed"] == 1
    with pytest.raises(ValueError): recovery.rotate(tmp_path, 8)


def test_prometheus_metrics_are_content_free():
    from app.ops.observability import prometheus_text
    text = prometheus_text({"metrics": {"outbox_pending": 4}, "cycles": [{"category": "publish", "active": False, "failures": 2}], "workers": []})
    assert 'logistics_cycle_active{category="publish"} 0' in text
    assert "tenant" not in text and "case" not in text


def test_outbox_backoff_only_counts_due_age_with_real_postgres():
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session
    from app.config import get_settings
    from app.identity.models import Tenant
    from app.infra.database import create_sync_engine
    from app.ops.observability import outbox_counters
    from app.workflows.models import QueueOutbox
    engine = create_sync_engine(get_settings())
    try:
        with engine.connect() as connection:
            outer = connection.begin()
            try:
                tenant = connection.scalar(select(Tenant.id).limit(1))
                connection.execute(text(f"SET LOCAL app.tenant_id = '{tenant}'"))
                connection.execute(text("SET LOCAL ROLE logistics_worker"))
                with Session(bind=connection, join_transaction_mode="create_savepoint") as db, db.begin():
                    now = datetime.now(UTC)
                    before = outbox_counters(db, now)
                    future = QueueOutbox(tenant_id=tenant, topic="run", dedupe_key=uuid4().hex,
                        payload={}, status="pending", created_at=now - timedelta(days=1), next_attempt_at=now + timedelta(minutes=10))
                    db.add(future)
                    db.flush()
                    delayed = outbox_counters(db, now)
                    assert delayed["outbox_pending"] == before["outbox_pending"] + 1
                    assert delayed["outbox_due_count"] == before["outbox_due_count"]
                    assert delayed["outbox_oldest_age_seconds"] == before["outbox_oldest_age_seconds"]
                    due_time = now - timedelta(seconds=max(301, before["outbox_oldest_age_seconds"] + 1))
                    due = QueueOutbox(tenant_id=tenant, topic="run", dedupe_key=uuid4().hex,
                        payload={}, status="failed", created_at=now - timedelta(days=1), next_attempt_at=due_time)
                    db.add(due)
                    db.flush()
                    overdue = outbox_counters(db, now)
                    assert overdue["outbox_due_count"] == before["outbox_due_count"] + 1
                    assert overdue["outbox_failed"] == before["outbox_failed"] + 1
                    assert overdue["outbox_oldest_age_seconds"] == (now - due_time).total_seconds()
            finally:
                outer.rollback()
    finally:
        engine.dispose()


def test_postgres_tools_preserve_tls_without_credentials_in_arguments(monkeypatch):
    monkeypatch.delenv("BACKUP_POSTGRES_CONTAINER", raising=False)
    captured = []
    monkeypatch.setattr(recovery.subprocess, "run", lambda args, **kwargs: captured.append((args, kwargs)))
    recovery.postgres_command("pg_dump", "postgresql+asyncpg://example:private@host:5432/logistics?sslmode=verify-full&sslrootcert=%2Fsecure%2Froot.pem", ["--schema-only"])
    assert captured[0][1]["env"]["PGSSLMODE"] == "verify-full"
    assert captured[0][1]["env"]["PGSSLROOTCERT"] == "/secure/root.pem"
    assert all("private" not in value for value in captured[0][0])
    with pytest.raises(ValueError):
        recovery.postgres_command("pg_dump", "postgresql://example:private@host/logistics?unrecognized=value", [])


def test_restore_rejects_live_checkpoint_entity_even_with_different_user(monkeypatch):
    settings = SimpleNamespace(database_url="postgresql+asyncpg://runtime:private@db/logistics", checkpoint_database_url="postgresql://runtime:private@db/logistics_restore_existing", s3_bucket="live")
    monkeypatch.setenv("RESTORE_DATABASE_URL", "postgresql+asyncpg://owner:other@db:5432/logistics_restore_existing")
    monkeypatch.setenv("RESTORE_S3_BUCKET", "logistics-restore-new")
    with pytest.raises(ValueError, match="restore_target_is_live"):
        recovery.restore(settings, "unused", "pg_restore", {})


def test_restore_requires_empty_bucket_including_deleted_versions(monkeypatch):
    import psycopg
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return self
        def fetchone(self): return (False,)
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connection())
    store = SimpleNamespace(_bucket="isolated", _internal=SimpleNamespace(list_object_versions=lambda **kwargs: {"DeleteMarkers": [{"Key": "old"}]}))
    with pytest.raises(ValueError, match="restore_bucket_must_be_empty"):
        recovery.require_empty_restore_targets(["postgresql://owner@db/logistics_restore_new"], store)
