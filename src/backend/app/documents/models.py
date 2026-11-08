"""
Case、上传接收与原件版本表。

关键不变量（详见后端数据方案第 2.2 节）：
- Case 是一票业务，UploadSession 是一次接收，两者不能互相替代。
- 原件版本只追加：补件建新 SourceDocument，替换才新增同 Document 的版本。
- 上传中浏览器测得的传输字节与服务端确认接收字节分开记录。
- `retention_expires_at = last_material_event_at + 30 天` 是唯一口径。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models_base import Base, created_at_column, tenant_column, uuid_pk


class CaseStatus(enum.StrEnum):
    ACTIVE = "active"
    # 置 deleting 后所有业务 API、SSE、Range 与新上传签名立即拒绝
    DELETING = "deleting"


class UploadSessionStatus(enum.StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    PARTIAL = "partial"
    EXPIRED = "expired"


class UploadFileStatus(enum.StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    RECEIVED = "received"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StoredObjectCategory(enum.StrEnum):
    TEMP = "temp"
    SOURCE = "source"
    PARSE = "parse"
    PREVIEW = "preview"
    EXPORT = "export"


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        Index("ix_cases_tenant_id_owner_user_id_created_at", "tenant_id", "owner_user_id", "created_at", "id"),
        Index("ix_cases_retention_expires_at", "tenant_id", "status", "retention_expires_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # 短编号只用于展示，不用来关联事实
    short_code: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[CaseStatus] = mapped_column(
        SAEnum(CaseStatus, name="case_status", native_enum=False, length=16),
        nullable=False,
        default=CaseStatus.ACTIVE,
    )
    # 整票删除预览的并发前置；与 review_state_version 是两个不同用途的计数
    case_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 复核分页防串页：当前字段审核、Issue 处置、规则投影变化时同事务递增
    review_state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_business_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    current_scope_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    current_comparison_batch_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    business_revision_no: Mapped[int | None] = mapped_column(Integer)
    # 30 天保留期只由实质性业务变更刷新；普通查询、下载、问答与失败重试都不刷新
    last_material_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 整票删除后递增，迟到的 Worker 与已签发 PUT 都因它失败而只留可清理孤儿
    execution_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_column()


class CaseAccessGrant(Base):
    """首版仅创建真实 owner 授权；团队共享 UI 未开放，但查询入口只认显式授权。"""

    __tablename__ = "case_access_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "subject_user_id", name="uq_case_access_grants_tenant_id_case_id_subject_user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    subject_user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class StoredObject(Base):
    """
    对象元数据。对象键由服务端随机生成，绝不出现在 API 响应或深链里；
    正式下载只走后端鉴权代理。
    """

    __tablename__ = "stored_objects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "object_key", name="uq_stored_objects_tenant_id_object_key"),
        Index("ix_stored_objects_category_visible", "tenant_id", "category", "is_visible"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[StoredObjectCategory] = mapped_column(
        SAEnum(StoredObjectCategory, name="stored_object_category", native_enum=False, length=16), nullable=False
    )
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime: Mapped[str | None] = mapped_column(String(160))
    # finalizing 对象不进入正式下载，且在有效租约内不被孤儿清理回收
    finalizing_attempt_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_visible: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_column()


class SourceDocument(Base):
    """一份物理来源材料。补件是新 SourceDocument，替换才在其下新增版本。"""

    __tablename__ = "source_documents"
    __table_args__ = (
        Index("ix_source_documents_tenant_id_case_id", "tenant_id", "case_id"),
        # 供 document_versions 的复合外键验证「同票归属」
        UniqueConstraint("tenant_id", "case_id", "id", name="uq_source_documents_tenant_id_case_id_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_column()


class DocumentVersion(Base):
    """
    原件版本，只追加不覆盖。重识别不走这里（只新增 ParseArtifact）。
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "version", name="uq_document_versions_tenant_id_document_id_version"),
        # 复合外键阻止把甲票的原件挂到乙票
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "document_id"],
            ["source_documents.tenant_id", "source_documents.case_id", "source_documents.id"],
            name="fk_document_versions_source_document_same_case",
        ),
        Index("ix_document_versions_tenant_id_case_id", "tenant_id", "case_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(160), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    object_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("stored_objects.id"), nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (Index("ix_upload_sessions_tenant_id_case_id_created_at", "tenant_id", "case_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # 仅在告知确认后签发上传；服务配置变化后旧告知版本不能签发新文件上传凭证
    disclosure_version_ack: Mapped[str] = mapped_column(String(64), nullable=False)
    disclosure_acked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disclosure_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[UploadSessionStatus] = mapped_column(
        SAEnum(UploadSessionStatus, name="upload_session_status", native_enum=False, length=16),
        nullable=False,
        default=UploadSessionStatus.OPEN,
    )
    # 事件序号与状态在同一事务内分配
    next_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_column()


class UploadFile(Base):
    """
    逐文件上传槽。部分失败互不吞并；未通过校验的文件不能变成 DocumentVersion。
    """

    __tablename__ = "upload_files"
    __table_args__ = (Index("ix_upload_files_tenant_id_upload_session_id", "tenant_id", "upload_session_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    upload_session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("upload_sessions.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_mime: Mapped[str] = mapped_column(String(160), nullable=False)
    declared_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    declared_sha256: Mapped[str | None] = mapped_column(String(64))
    temp_object_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    status: Mapped[UploadFileStatus] = mapped_column(
        SAEnum(UploadFileStatus, name="upload_file_status", native_enum=False, length=16),
        nullable=False,
        default=UploadFileStatus.PENDING,
    )
    # 服务端确认接收的字节；单次 PUT 模式下只在完整校验后等于文件大小
    confirmed_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    # 替换目标；补件不带
    replace_document_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    expected_document_version: Mapped[int | None] = mapped_column(Integer)
    # 验收成功后回填
    document_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    document_version: Mapped[int | None] = mapped_column(Integer)
    # 同一用户范围内的重复文件提示，不跨用户去重
    duplicate_of_document_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_column()


class UploadEvent(Base):
    """UploadSession 的独立事件序号；只有已提交的数据库事件是进度真值。"""

    __tablename__ = "upload_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "upload_session_id", "seq", name="uq_upload_events_tenant_id_upload_session_id_seq"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    upload_session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("upload_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    upload_file_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    status: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = created_at_column()


class IdempotencyRecord(Base):
    """
    幂等记录：同键同内容返回同结果，同键异内容拒绝。
    只存受控结果 ID 与小响应，不存原件正文。
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "actor_id", "scope", "key", name="uq_idempotency_records_tenant_id_actor_id_scope_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    actor_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(80), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
    result_ref: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_column()


class AuditEvent(Base):
    """审计只追加；整票到期或主动删除时按 30 天策略清除该票关联记录。"""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_tenant_id_case_id_created_at", "tenant_id", "case_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    object_ref: Mapped[dict | None] = mapped_column(JSONB)
    seen_versions: Mapped[dict | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_column()
