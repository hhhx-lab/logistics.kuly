"""加密备份/离线隔离恢复工具。生产WAL、KMS、存储复制由部署方提供，不能由本工具宣称达成。"""
import argparse
import base64
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse, unquote, parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/backend"))
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"LDA-BACKUP-1\n"
BLOCK = 1024 * 1024


def cipher():
    raw = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
    key = base64.urlsafe_b64decode(raw)
    if len(key) != 32:
        raise ValueError("backup_key_required")
    return AESGCM(key)


def encrypt(source, target, encryption):
    with open(source, "rb") as reader, open(target, "xb") as writer:
        os.chmod(target, 0o600)
        writer.write(MAGIC)
        index = 0
        while data := reader.read(BLOCK):
            nonce = os.urandom(12)
            packet = nonce + encryption.encrypt(nonce, data, MAGIC + str(index).encode())
            writer.write(struct.pack(">I", len(packet)) + packet)
            index += 1
        # 认证结束帧，截短至完整前一帧也不能冒充完整备份。
        nonce = os.urandom(12)
        packet = nonce + encryption.encrypt(nonce, b"", MAGIC + str(index).encode())
        writer.write(struct.pack(">I", len(packet)) + packet)
        writer.flush()
        os.fsync(writer.fileno())


def decrypt(source, target, encryption):
    with open(source, "rb") as reader, open(target, "xb") as writer:
        os.chmod(target, 0o600)
        if reader.read(len(MAGIC)) != MAGIC:
            raise ValueError("backup_format_invalid")
        index = 0
        while True:
            length = reader.read(4)
            if len(length) != 4:
                raise ValueError("backup_truncated")
            size = struct.unpack(">I", length)[0]
            if not 28 <= size <= BLOCK + 28:
                raise ValueError("backup_frame_invalid")
            packet = reader.read(size)
            if len(packet) != size:
                raise ValueError("backup_truncated")
            data = encryption.decrypt(packet[:12], packet[12:], MAGIC + str(index).encode())
            if not data:
                if reader.read(1):
                    raise ValueError("backup_trailing_data")
                break
            writer.write(data)
            index += 1


def postgres_command(executable, url, args):
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://"))
    environment = {**os.environ, "PGHOST": parsed.hostname or "", "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""), "PGPASSWORD": unquote(parsed.password or ""), "PGDATABASE": parsed.path.lstrip("/")}
    # 保留TLS等连接策略；不允许含未识别参数的URL悄悄降级为普通连接。
    options = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
               "connect_timeout": "PGCONNECT_TIMEOUT", "application_name": "PGAPPNAME", "channel_binding": "PGCHANNELBINDING"}
    for key, value in parse_qsl(parsed.query):
        if key == "ssl":
            key = "sslmode"
            value = {"true": "require", "false": "disable"}.get(value.lower(), value)
        if key not in options:
            raise ValueError("backup_database_connection_option_unsupported")
        environment[options[key]] = value
    # URL/密码只进子进程环境，不出现在argv、日志或错误回包。
    container = os.environ.get("BACKUP_POSTGRES_CONTAINER")
    if not container:
        subprocess.run([executable, *args], env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    # 复用现有PG16容器中的客户端；容器必须与目标PG实例一致，凭据以env传递而非argv。
    environment["PGHOST"], environment["PGPORT"] = "127.0.0.1", "5432"
    command = ["docker", "exec", "-i"]
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", *options.values()):
        if key not in environment:
            continue
        command.extend(("-e", key))
    command.extend((container, Path(executable).name))
    if Path(executable).name == "pg_dump":
        file_arg = next(arg for arg in args if arg.startswith("--file="))
        with open(file_arg.split("=", 1)[1], "wb") as target:
            subprocess.run(command + [arg for arg in args if arg != file_arg], env=environment, stdout=target, stderr=subprocess.DEVNULL, check=True)
    else:
        with open(args[-1], "rb") as source:
            subprocess.run(command + args[:-1], env=environment, stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def file_digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(BLOCK):
            value.update(block)
    return value.hexdigest()


def ledger_anchor(settings):
    from app.management.ledger import DeletionLedger
    ledger = DeletionLedger(settings)
    with open(ledger.path, "rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        handle.seek(0)
        data = handle.read()
        rows, _ = ledger._decode(data.splitlines(keepends=True))
    return {"sequence": len(rows), "sha256": hashlib.sha256(data).hexdigest()}, data


def mirror_ledger(settings, directory):
    target = Path(directory)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path(settings.deletion_ledger_path).resolve()
    if target.resolve() == source or source in target.resolve().parents:
        raise ValueError("mirror_must_be_independent")
    anchor, data = ledger_anchor(settings)
    temporary = target / ".deletions.tmp"
    with open(temporary, "wb") as handle:
        os.chmod(temporary, 0o600)
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    temporary.replace(target / "deletions.jsonl.enc")
    anchor_path = target / "anchor.json"
    anchor_path.write_text(json.dumps(anchor))
    os.chmod(anchor_path, 0o600)
    descriptor = os.open(target, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    return anchor


def backup(settings, output, pg_dump):
    from app.infra.object_store import ObjectStore
    directory = Path(output)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    anchor, _ = ledger_anchor(settings)
    store = ObjectStore(settings)
    try:
        with tempfile.TemporaryDirectory(prefix="logistics-backup-") as work:
            workspace = Path(work) / "data"
            workspace.mkdir(mode=0o700)
            postgres_command(pg_dump, settings.database_url, ["--format=custom", "--no-owner", "--file=" + str(workspace / "database.dump")])
            from sqlalchemy.engine import make_url
            checkpoint_url, database_url = make_url(settings.checkpoint_database_url), make_url(settings.database_url)
            same_database = (checkpoint_url.host, checkpoint_url.port, checkpoint_url.database) == (database_url.host, database_url.port, database_url.database)
            if not same_database:
                postgres_command(pg_dump, settings.checkpoint_database_url, ["--format=custom", "--no-owner", "--file=" + str(workspace / "checkpoints.dump")])
            entries = []
            paginator = store._internal.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=store._bucket, Prefix="t/"):
                for row in page.get("Versions", []):
                    filename = hashlib.sha256((row["Key"] + "\0" + row["VersionId"]).encode()).hexdigest()
                    response = store._internal.get_object(Bucket=store._bucket, Key=row["Key"], VersionId=row["VersionId"])
                    with open(workspace / filename, "wb") as handle:
                        try: shutil.copyfileobj(response["Body"], handle, BLOCK)
                        finally: response["Body"].close()
                    entries.append({"key": row["Key"], "version_id": row["VersionId"], "blob": filename, "sha256": file_digest(workspace / filename), "latest": row["IsLatest"], "modified": row["LastModified"].isoformat(), "content_type": response.get("ContentType", "application/octet-stream")})
            dumps = {name: file_digest(workspace / name) for name in ("database.dump", "checkpoints.dump") if (workspace / name).exists()}
            (workspace / "manifest.json").write_text(json.dumps({"version": 2, "created_at": datetime.now(UTC).isoformat(), "ledger_anchor": anchor, "objects": entries, "dumps": dumps}))
            archive = Path(shutil.make_archive(str(Path(work) / "payload"), "tar", root_dir=workspace))
            target = directory / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".lda.enc")
            encrypt(archive, target, cipher())
            return {"status": "encrypted", "file": target.name, "objects": len(entries), "ledger_sequence": anchor["sequence"]}
    finally:
        store._internal.close(); store._public.close()


def rotate(output, days):
    if not 1 <= days <= 7:
        raise ValueError("backup_retention_must_be_1_to_7_days")
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    removed = 0
    for file in Path(output).glob("*.lda.enc"):
        if file.is_file() and not file.is_symlink() and file.stat().st_mtime < cutoff:
            file.unlink(); removed += 1
    return {"removed": removed, "retention_days": days}


def require_empty_restore_targets(database_urls, store):
    import psycopg
    for url in set(database_urls):
        plain_url = url.replace("postgresql+asyncpg://", "postgresql://", 1).replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(plain_url, connect_timeout=5) as connection:
            occupied = connection.execute("SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','f','S'))").fetchone()[0]
            if occupied:
                raise ValueError("restore_database_must_be_empty")
    page = store._internal.list_object_versions(Bucket=store._bucket, MaxKeys=1)
    if page.get("Versions") or page.get("DeleteMarkers"):
        raise ValueError("restore_bucket_must_be_empty_including_versions")


def restore(settings, archive, pg_restore, expected_anchor):
    import tarfile
    from sqlalchemy.engine import make_url
    from app.infra.object_store import ObjectStore
    from app.management.ledger import guard_restored_database
    from app.management.purge import replay_deletion_ledger
    target_url = os.environ.get("RESTORE_DATABASE_URL", "")
    target_bucket = os.environ.get("RESTORE_S3_BUCKET", "")
    if not make_url(target_url).database.startswith("logistics_restore_") or not target_bucket.startswith("logistics-restore-"):
        raise ValueError("restore_requires_isolated_database_and_bucket")
    def database_identity(url):
        parsed = make_url(url)
        return (parsed.host, parsed.port or 5432, parsed.database)
    live_targets = {database_identity(settings.database_url), database_identity(settings.checkpoint_database_url)}
    if database_identity(target_url) in live_targets or target_bucket == settings.s3_bucket:
        raise ValueError("restore_target_is_live")
    current_anchor, current_ledger = ledger_anchor(settings)
    if current_anchor != expected_anchor:
        raise ValueError("latest_independent_ledger_anchor_mismatch")
    # 必须使用最新独立账本，不能从旧备份恢复删除账本导致被删票复活。
    checkpoint_target = os.environ.get("RESTORE_CHECKPOINT_DATABASE_URL") or target_url
    if not make_url(checkpoint_target).database.startswith("logistics_restore_"):
        raise ValueError("checkpoint_restore_requires_isolated_database")
    if database_identity(checkpoint_target) in live_targets:
        raise ValueError("checkpoint_restore_target_is_live")
    # 重放会记录独立新删除回执，必须写入权威账本；不能复制只读镜像后伪造成功。
    from app.management.ledger import DeletionLedger
    ledger = DeletionLedger(settings)
    with open(ledger.path, "r+b"):
        pass
    target = settings.model_copy(update={"database_url": target_url, "checkpoint_database_url": checkpoint_target, "s3_bucket": target_bucket})
    store = ObjectStore(target)
    try:
        require_empty_restore_targets((target_url, checkpoint_target), store)
        with tempfile.TemporaryDirectory(prefix="logistics-restore-") as work:
            workspace = Path(work)
            decrypt(archive, workspace / "payload.tar", cipher())
            with tarfile.open(workspace / "payload.tar") as bundle:
                bundle.extractall(workspace / "data", filter="data")
            contents = workspace / "data"
            manifest = json.loads((contents / "manifest.json").read_text())
            if manifest["version"] != 2 or current_anchor["sequence"] < manifest["ledger_anchor"]["sequence"]:
                raise ValueError("restoration_ledger_older_than_backup")
            created_at = datetime.fromisoformat(manifest["created_at"])
            if created_at.tzinfo is None or not datetime.now(UTC) - timedelta(days=7) <= created_at <= datetime.now(UTC) + timedelta(minutes=5):
                raise ValueError("backup_older_than_retention_boundary")
            if (contents / "checkpoints.dump").exists() and database_identity(checkpoint_target) == database_identity(target_url):
                raise ValueError("separate_checkpoint_restore_requires_separate_isolated_target")
            if "database.dump" not in manifest["dumps"] or ((contents / "checkpoints.dump").exists() != ("checkpoints.dump" in manifest["dumps"])):
                raise ValueError("backup_database_checksum_missing")
            for name, digest in manifest["dumps"].items():
                if name not in {"database.dump", "checkpoints.dump"} or file_digest(contents / name) != digest:
                    raise ValueError("backup_database_checksum_invalid")
            # 保留原精确ACL/PUBLIC撤权；预建相同受限角色，不能统一授权破坏追加账本边界。
            postgres_command(pg_restore, target_url, ["--exit-on-error", "--single-transaction", "--no-owner", "--dbname=" + make_url(target_url).database, str(contents / "database.dump")])
            if (contents / "checkpoints.dump").exists():
                postgres_command(pg_restore, checkpoint_target, ["--exit-on-error", "--single-transaction", "--no-owner", "--dbname=" + make_url(checkpoint_target).database, str(contents / "checkpoints.dump")])
            denied = {(row["tenant_id"], row["case_id"]) for row in DeletionLedger(settings).records()}
            count = 0
            # 只恢复DB按对象键读取的当前可见版本；旧版本不可冒充当前，已删前缀不恢复。
            for row in manifest["objects"]:
                parts = row["key"].split("/")
                if len(parts) < 6 or parts[0] != "t" or parts[2] != "c":
                    raise ValueError("backup_object_key_scope_invalid")
                from uuid import UUID
                UUID(parts[1]); UUID(parts[3])
                if not row["latest"] or (parts[1], parts[3]) in denied:
                    continue
                if row["blob"] != Path(row["blob"]).name or file_digest(contents / row["blob"]) != row["sha256"]:
                    raise ValueError("backup_blob_invalid")
                with open(contents / row["blob"], "rb") as handle:
                    store._internal.put_object(Bucket=target_bucket, Key=row["key"], Body=handle, ContentType=row["content_type"])
                count += 1
            replay_deletion_ledger(target)
            guard_restored_database(target)
            final_anchor, final_ledger = ledger_anchor(settings)
            if not final_ledger.startswith(current_ledger) or {(row["tenant_id"], row["case_id"]) for row in ledger.records()} != denied:
                raise ValueError("ledger_changed_during_restore_repeat_revocation_before_open")
            return {"status": "revocation_replayed", "restored_objects": count, "ledger_sequence": final_anchor["sequence"], "service_opened": False,
                "remaining_gate": "恢复库授权、物理清理、检查点兼容和供应商未知受理核对后，人工审批开放；不要启动旧Outbox"}
    finally:
        store._internal.close(); store._public.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "rotate", "ledger-mirror", "restore"))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--pg-dump", default="pg_dump")
    parser.add_argument("--pg-restore", default="pg_restore")
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--archive")
    parser.add_argument("--ledger-anchor")
    parser.add_argument("--offline-confirm", action="store_true")
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--env-file", help="独立运维env文件；只加载BACKUP_/RESTORE_配置，不覆盖运行凭据")
    args = parser.parse_args()
    if args.env_file:
        from dotenv import dotenv_values
        for key, value in dotenv_values(args.env_file).items():
            if key.startswith(("BACKUP_", "RESTORE_")) and value is not None:
                os.environ[key] = value
    from app.config import get_settings
    settings = get_settings()
    if args.action == "backup":
        if not args.offline_confirm:
            parser.error("backup须暂停业务写入并明确--offline-confirm，数据库与对象存储必须同一静止边界")
        result = backup(settings, args.directory, args.pg_dump)
        result.update(rotate(args.directory, args.retention_days))
    elif args.action == "rotate":
        while True:
            result = rotate(args.directory, args.retention_days)
            if not args.continuous: break
            time.sleep(3600)
    elif args.action == "ledger-mirror":
        while True:
            result = mirror_ledger(settings, args.directory)
            if not args.continuous: break
            time.sleep(5)
    else:
        if not args.offline_confirm or not args.archive or not args.ledger_anchor:
            parser.error("restore须明确离线并提供加密archive与最新独立ledger-anchor")
        result = restore(settings, args.archive, args.pg_restore, json.loads(Path(args.ledger_anchor).read_text()))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print("disaster_recovery_failed " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
