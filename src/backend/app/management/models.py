"""管理台检索摘要与不含票据内容的独立删除回执。"""
from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import Integer,String,Text,DateTime,UniqueConstraint,ForeignKey,Index,Boolean,text
from sqlalchemy.dialects.postgresql import UUID as PgUUID,JSONB
from sqlalchemy.orm import Mapped,mapped_column
from app.infra.models_base import Base,TenantMixin,created_at_column


class RunDocumentSummary(TenantMixin,Base):
    __tablename__="run_document_summaries"
    __table_args__=(UniqueConstraint("tenant_id","run_id","document_id","document_version"),)
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("cases.id"),nullable=False)
    run_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("runs.id"),nullable=False)
    document_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    document_version:Mapped[int]=mapped_column(Integer,nullable=False)
    parse_artifact_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True))
    summary:Mapped[str]=mapped_column(Text,nullable=False)
    state:Mapped[str]=mapped_column(String(16),nullable=False)
    dependency_fingerprint:Mapped[str]=mapped_column(String(64),nullable=False)


class CaseDeletion(TenantMixin,Base):
    __tablename__="case_deletions"
    __table_args__=(UniqueConstraint("tenant_id","actor_id","case_id","idempotency_key_hash"),)
    # 不绑定Case外键：Case消失后仍可查询和重放，不保留文件名／正文。
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    actor_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    idempotency_key_hash:Mapped[str]=mapped_column(String(64),nullable=False)
    request_hash:Mapped[str]=mapped_column(String(64),nullable=False)
    status:Mapped[str]=mapped_column(String(16),nullable=False)
    requested_at:Mapped[datetime]=created_at_column()
    completed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_error_class:Mapped[str|None]=mapped_column(String(80))
    purge_after:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    purge_manifest:Mapped[dict]=mapped_column(JSONB,nullable=False,default=dict)
    ledger_sequence:Mapped[int|None]=mapped_column(Integer)


class SearchProjection(TenantMixin,Base):
    __tablename__="search_projections"
    __table_args__=(Index("ix_search_projections_run_snapshot","tenant_id","run_id","snapshot_id"),
        Index("ix_search_projection_filename_trgm","filename",postgresql_using="gin",postgresql_ops={"filename":"gin_trgm_ops"}),
        Index("uq_search_projection_scope","tenant_id","run_id",
            text("coalesce(snapshot_id,'00000000-0000-0000-0000-000000000000'::uuid)"),"document_id","document_version",
            text("coalesce(logical_document_id,'00000000-0000-0000-0000-000000000000'::uuid)"),unique=True))
    case_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("cases.id"),nullable=False)
    run_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("runs.id"),nullable=False)
    snapshot_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True),ForeignKey("export_snapshots.id"))
    document_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    document_version:Mapped[int]=mapped_column(Integer,nullable=False)
    parse_artifact_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True))
    logical_document_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True))
    business_revision_id:Mapped[uuid.UUID|None]=mapped_column(PgUUID(as_uuid=True))
    filename:Mapped[str]=mapped_column(String(255),nullable=False)
    doc_type:Mapped[str]=mapped_column(String(40),nullable=False)
    summary:Mapped[str]=mapped_column(Text,nullable=False)
    uploaded_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    uploaded_by:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    state:Mapped[str]=mapped_column(String(16),nullable=False)
    dependency_fingerprint:Mapped[str]=mapped_column(String(64),nullable=False)


class SearchFieldValue(TenantMixin,Base):
    __tablename__="search_field_values"
    __table_args__=(Index("ix_search_field_values_projection_role","tenant_id","projection_id","role"),
        Index("ix_search_fields_value_trgm","normalized_value",postgresql_using="gin",postgresql_ops={"normalized_value":"gin_trgm_ops"}))
    projection_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),ForeignKey("search_projections.id",ondelete="CASCADE"),nullable=False)
    field_assertion_id:Mapped[uuid.UUID]=mapped_column(PgUUID(as_uuid=True),nullable=False)
    role:Mapped[str|None]=mapped_column(String(80))
    field_key:Mapped[str]=mapped_column(String(80),nullable=False)
    normalized_value:Mapped[str]=mapped_column(Text,nullable=False)
    state:Mapped[str]=mapped_column(String(16),nullable=False)
