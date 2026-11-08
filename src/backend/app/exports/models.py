"""整组交付：不可变确认、可重试尝试、原子成功快照及鉴权产物。"""
from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped,mapped_column
from app.infra.models_base import Base,TenantMixin,created_at_column


class ReviewConfirmation(TenantMixin,Base):
    __tablename__="review_confirmations"
    __table_args__=(UniqueConstraint("tenant_id","run_id","review_fingerprint"),)
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("cases.id"),nullable=False)
    run_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("runs.id"),nullable=False)
    task_spec_hash:Mapped[str]=mapped_column(String(64),nullable=False)
    review_fingerprint:Mapped[str]=mapped_column(String(64),nullable=False)
    business_revision_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("business_revisions.id"),nullable=False)
    translation_revision_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("translation_revisions.id"),nullable=False)
    pairing_manifest:Mapped[dict]=mapped_column(JSONB,nullable=False)
    review_manifest:Mapped[dict]=mapped_column(JSONB,nullable=False)
    output_formats:Mapped[list[str]]=mapped_column(JSONB,nullable=False)
    confirmed_by:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    confirmed_at:Mapped[datetime]=created_at_column()


class ExportAttempt(TenantMixin,Base):
    __tablename__="export_attempts"
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("cases.id"),nullable=False)
    run_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("runs.id"),nullable=False)
    confirmation_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("review_confirmations.id"),nullable=False)
    review_fingerprint:Mapped[str]=mapped_column(String(64),nullable=False)
    status:Mapped[str]=mapped_column(String(24),nullable=False)
    execution_epoch:Mapped[int]=mapped_column(Integer,nullable=False)
    snapshot_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True))
    error_summary:Mapped[str|None]=mapped_column(Text)
    created_by:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    updated_at:Mapped[datetime]=created_at_column()


class ExportAttemptItem(TenantMixin,Base):
    __tablename__="export_attempt_items"
    __table_args__=(UniqueConstraint("tenant_id","attempt_id","ordinal"),)
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("cases.id"),nullable=False)
    attempt_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("export_attempts.id"),nullable=False)
    document_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True))
    document_version:Mapped[int|None]=mapped_column(Integer)
    ordinal:Mapped[int]=mapped_column(Integer,nullable=False)
    kind:Mapped[str]=mapped_column(String(16),nullable=False)
    filename:Mapped[str]=mapped_column(String(255),nullable=False)
    status:Mapped[str]=mapped_column(String(24),nullable=False)
    object_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True),ForeignKey("stored_objects.id"))
    size_bytes:Mapped[int|None]=mapped_column(Integer)
    sha256:Mapped[str|None]=mapped_column(String(64))
    verify_manifest:Mapped[dict|None]=mapped_column(JSONB)
    error_summary:Mapped[str|None]=mapped_column(Text)


class ExportSnapshot(TenantMixin,Base):
    __tablename__="export_snapshots"
    __table_args__=(UniqueConstraint("tenant_id","attempt_id"),)
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("cases.id"),nullable=False)
    run_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("runs.id"),nullable=False)
    confirmation_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("review_confirmations.id"),nullable=False)
    attempt_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("export_attempts.id"),nullable=False)
    manifest:Mapped[dict]=mapped_column(JSONB,nullable=False)
    exported_by:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    exported_at:Mapped[datetime]=created_at_column()


class ExportArtifact(TenantMixin,Base):
    __tablename__="export_artifacts"
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("cases.id"),nullable=False)
    snapshot_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("export_snapshots.id"),nullable=False)
    document_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True))
    document_version:Mapped[int|None]=mapped_column(Integer)
    kind:Mapped[str]=mapped_column(String(16),nullable=False)
    object_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("stored_objects.id"),nullable=False)
    filename:Mapped[str]=mapped_column(String(255),nullable=False)
    size_bytes:Mapped[int]=mapped_column(Integer,nullable=False)
    sha256:Mapped[str]=mapped_column(String(64),nullable=False)
