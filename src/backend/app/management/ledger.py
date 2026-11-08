"""独立加密删除意图账本：提交前持久意图、提交后确认；恢复保守拒绝被删票。

账本目录须独立于 PostgreSQL 备份，密钥通过 env 提供。不是 WAL/CDC 替代品：
生产备份恢复仍须先验证部署的连续变更复制，再运行本模块的恢复门禁。
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class LedgerUnavailable(RuntimeError):
    pass


_denied_cache = {}


class DeletionLedger:
    def __init__(self, settings):
        if not settings.deletion_ledger_path or not settings.deletion_ledger_key:
            raise LedgerUnavailable("独立删除账本尚未配置")
        self.directory = Path(settings.deletion_ledger_path).expanduser()
        raw = settings.deletion_ledger_key.get_secret_value()
        try:
            key = base64.urlsafe_b64decode(raw)
            if len(key) != 32:
                raise ValueError
            self.cipher = AESGCM(key)
            self.key_fingerprint = hashlib.sha256(key).hexdigest()
        except (ValueError, TypeError) as exc:
            raise LedgerUnavailable("删除账本密钥格式不正确") from exc
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "deletions.jsonl.enc"

    def _decode(self, lines):
        rows = []
        previous = "0" * 64
        for seq, line in enumerate(lines, 1):
            try:
                envelope = json.loads(line)
                if envelope["seq"] != seq or envelope["previous"] != previous:
                    raise ValueError
                aad = f"{seq}:{previous}".encode()
                row = json.loads(self.cipher.decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]), aad))
                rows.append(row)
                previous = hashlib.sha256(line).hexdigest()
            except Exception as exc:
                raise LedgerUnavailable("删除账本校验失败，禁止恢复后开放服务") from exc
        return rows, previous

    def records(self):
        with open(self.path, "a+b") as handle:
            os.chmod(self.path, 0o600)
            fcntl.flock(handle, fcntl.LOCK_SH)
            handle.seek(0)
            rows, _ = self._decode(handle.readlines())
            return rows

    def is_denied(self, tenant_id, case_id):
        stamp = self.path.stat().st_mtime_ns if self.path.exists() else 0
        cache_key = (str(self.path), self.key_fingerprint)
        cached = _denied_cache.get(cache_key)
        if cached is None or cached[0] != stamp:
            denied = {(r["tenant_id"], r["case_id"]) for r in self.records()}
            cached = (stamp, denied)
            _denied_cache[cache_key] = cached
        return (str(tenant_id), str(case_id)) in cached[1]

    def append(self, *, deletion_id, tenant_id, case_id, requested_at, state):
        record = {"deletion_id": str(UUID(str(deletion_id))), "tenant_id": str(UUID(str(tenant_id))),
                  "case_id": str(UUID(str(case_id))), "requested_at": requested_at.isoformat(), "state": state}
        with open(self.path, "a+b") as handle:
            os.chmod(self.path, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            rows, previous = self._decode(handle.readlines())
            for index, row in enumerate(rows, 1):
                if row["deletion_id"] == record["deletion_id"] and row["state"] == state:
                    if row != record:
                        raise LedgerUnavailable("删除账本同一作业记录冲突")
                    return index
            seq = len(rows) + 1
            nonce = os.urandom(12)
            encrypted = self.cipher.encrypt(nonce, json.dumps(record, sort_keys=True).encode(), f"{seq}:{previous}".encode())
            line = json.dumps({"seq": seq, "previous": previous, "nonce": base64.b64encode(nonce).decode(),
                               "ciphertext": base64.b64encode(encrypted).decode()}, sort_keys=True).encode() + b"\n"
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return seq


def guard_restored_database(settings):
    """启动门禁：恢复出了账本中已撤权的 active Case 时拒绝开放，先重放删除。

    DB中的每个未完成删除也必须在独立账本出现。已物理完成的最小回执仍保留。
    """
    from sqlalchemy import select
    from app.documents.models import Case, CaseStatus
    from app.identity.models import Tenant
    from app.infra.database import create_sync_engine, create_sync_session_factory, sync_tenant_transaction
    from app.management.models import CaseDeletion
    ledger = DeletionLedger(settings)
    rows = ledger.records()
    registered = {r["deletion_id"] for r in rows}
    by_tenant = {}
    for row in rows:
        by_tenant.setdefault(UUID(row["tenant_id"]), set()).add(UUID(row["case_id"]))
    engine = create_sync_engine(settings)
    factory = create_sync_session_factory(engine)
    try:
        with engine.begin() as conn:
            tenants = list(conn.execute(select(Tenant.id)).scalars())
        for tenant in tenants:
            with factory() as db, sync_tenant_transaction(db, tenant):
                if by_tenant.get(tenant) and db.scalar(select(Case.id).where(Case.id.in_(by_tenant[tenant]), Case.status == CaseStatus.ACTIVE).limit(1)):
                    raise LedgerUnavailable("发现恢复的已删除票据，必须先重放删除后再启动")
                for deletion in db.scalars(select(CaseDeletion)):
                    sequence=deletion.ledger_sequence
                    if str(deletion.id) not in registered or not sequence or sequence>len(rows) or rows[sequence-1]["deletion_id"]!=str(deletion.id):
                        raise LedgerUnavailable("主库删除回执未见于连续独立账本，禁止开放服务")
    finally:
        engine.dispose()
