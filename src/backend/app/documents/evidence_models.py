"""
解析、来源与业务版本表（迁移③）。

归属链：每个源片段与字段都必须能验证
`(tenant, case, document, document_version, parse_artifact, logical_document, section)`。
复合外键阻止把甲票的区段挂到乙票的解析版本；应用层再检查来源链，
不把数据库约束当作唯一授权。

不变量：
- 同源重识别新增 ParseArtifact，不改 DocumentVersion。
- 区段不能跨 ParseArtifact；人工改类型／归属写业务覆盖，不覆盖机器候选。
- 业务 Revision 只追加，当前指针以 compare-and-swap 前移。
- `rect` 精度才有真实 bbox；坐标不可靠时退到页／段级，绝不猜框。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models_base import Base, created_at_column, tenant_column, uuid_pk


class ParseQuality(enum.StrEnum):
    VALID = "valid"
    # 可用但有局部质量疑点（低清、手写、跨页续表不完整），须人工留意
    DEGRADED = "degraded"
    FAILED = "failed"


class AnchorPrecision(enum.StrEnum):
    RECT = "rect"
    PAGE_ONLY = "page_only"
    PARAGRAPH_ONLY = "paragraph_only"
    CELL = "cell"
    NONE = "none"


class LogicalDocumentType(enum.StrEnum):
    COMMERCIAL_INVOICE = "commercial_invoice"
    PACKING_LIST = "packing_list"
    BILL_OF_LADING = "bill_of_lading"
    SUPPORTING = "supporting"
    # 分类不清时保留 unknown 节与候选，不把混合文件整份标成「发票」
    UNKNOWN = "unknown"


class ParseArtifact(Base):
    __tablename__ = "parse_artifacts"
    __table_args__ = (
        Index("ix_parse_artifacts_tenant_id_document_version", "tenant_id", "document_id", "document_version"),
        UniqueConstraint("tenant_id", "case_id", "id", name="uq_parse_artifacts_tenant_id_case_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "document_version"],
            ["document_versions.tenant_id", "document_versions.document_id", "document_versions.version"],
            name="fk_parse_artifacts_document_version",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 解析器与配置版本进入输入指纹：换解析器必须能区分产物来源
    parser_name: Mapped[str] = mapped_column(String(40), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    parser_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    layout_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_response_object_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    markdown_object_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    page_count: Mapped[int | None] = mapped_column(Integer)
    quality: Mapped[ParseQuality] = mapped_column(
        SAEnum(ParseQuality, name="parse_quality", native_enum=False, length=16), nullable=False
    )
    quality_flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # 供应商异步任务 ID：可凭它核查受理状态，避免崩溃后盲重投
    external_task_id: Mapped[str | None] = mapped_column(String(128))
    # 使用了本地假解析时为真，报告与界面必须显著标注
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_column()


class LogicalDocument(Base):
    """一份物理原件内的一张逻辑单据；混合 PDF 可有多张。"""

    __tablename__ = "logical_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "parse_artifact_id", "id", name="uq_logical_documents_tenant_id_parse_artifact_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "parse_artifact_id"],
            ["parse_artifacts.tenant_id", "parse_artifacts.case_id", "parse_artifacts.id"],
            name="fk_logical_documents_parse_artifact_same_case",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    type_candidate: Mapped[LogicalDocumentType] = mapped_column(
        SAEnum(LogicalDocumentType, name="logical_document_type", native_enum=False, length=32), nullable=False
    )
    # 机器候选的置信说明，不是人工确认
    type_confidence: Mapped[str | None] = mapped_column(String(16))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = created_at_column()


class DocumentSection(Base):
    __tablename__ = "document_sections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "parse_artifact_id", "id", name="uq_document_sections_tenant_id_parse_artifact_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "parse_artifact_id", "logical_document_id"],
            ["logical_documents.tenant_id", "logical_documents.parse_artifact_id", "logical_documents.id"],
            name="fk_document_sections_logical_document_same_artifact",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    logical_document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    detected_language: Mapped[str | None] = mapped_column(String(16))
    # 检测结果不冒充用户指定；混排或不确定时显式标记
    language_uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_column()


class SourceSegment(Base):
    __tablename__ = "source_segments"
    __table_args__ = (
        Index("ix_source_segments_tenant_id_section_id_ordinal", "tenant_id", "section_id", "ordinal"),
        UniqueConstraint("tenant_id", "parse_artifact_id", "id", name="uq_source_segments_tenant_id_parse_artifact_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "parse_artifact_id", "section_id"],
            ["document_sections.tenant_id", "document_sections.parse_artifact_id", "document_sections.id"],
            name="fk_source_segments_section_same_artifact",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    logical_document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    section_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # 段落／标题／表格单元格／表格行；表格结构另有行列坐标
    structure: Mapped[str] = mapped_column(String(24), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    table_row: Mapped[int | None] = mapped_column(Integer)
    table_col: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()


class SourceAnchor(Base):
    """
    原件上的位置。只有 `precision=rect` 才带 bbox，且坐标系与变换随行保存；
    DOCX 的渲染页码必须绑定渲染版本。
    """

    __tablename__ = "source_anchors"
    __table_args__ = (Index("ix_source_anchors_tenant_id_segment_id", "tenant_id", "source_segment_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_segment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("source_segments.id", ondelete="CASCADE"), nullable=False
    )
    precision: Mapped[AnchorPrecision] = mapped_column(
        SAEnum(AnchorPrecision, name="anchor_precision", native_enum=False, length=16), nullable=False
    )
    page_no: Mapped[int | None] = mapped_column(Integer)
    # [x0, y0, x1, y1]，坐标系由 coord_system 声明
    bbox: Mapped[list[float] | None] = mapped_column(JSONB)
    coord_system: Mapped[str | None] = mapped_column(String(40))
    transform: Mapped[dict | None] = mapped_column(JSONB)
    render_version: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = created_at_column()


class FieldAssertion(Base):
    """
    字段断言分四层：原件转写 → 候选 → 规范值 → 人工采用值。

    人工采用值是**新的不可变断言**并引用原始断言，原始候选永不覆盖。
    主体角色（Seller/Buyer/Shipper/Consignee/Notify Party）分别保存，不硬合并。
    """

    __tablename__ = "field_assertions"
    __table_args__ = (
        Index("ix_field_assertions_tenant_id_logical_document_id_field_key", "tenant_id", "logical_document_id", "field_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    logical_document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    section_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    field_key: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str | None] = mapped_column(String(40))
    group: Mapped[str] = mapped_column(String(40), nullable=False)
    source_text: Mapped[str | None] = mapped_column(Text)
    candidate_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    # 金额与精确数量用 Decimal，不用浮点
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    unit: Mapped[str | None] = mapped_column(String(16))
    source_segment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # candidate / confirmed / conflict / undetermined
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    ambiguity: Mapped[str | None] = mapped_column(String(200))
    # 人工采用值断言引用被修订的原始断言
    supersedes_assertion_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    is_human: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    basis: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class LineItem(Base):
    __tablename__ = "line_items"
    __table_args__ = (Index("ix_line_items_tenant_id_logical_document_id_line_no", "tenant_id", "logical_document_id", "line_no"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    logical_document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sku: Mapped[str | None] = mapped_column(Text)
    specification: Mapped[str | None] = mapped_column(Text)
    lot_no: Mapped[str | None] = mapped_column(Text)
    source_item_no: Mapped[str | None] = mapped_column(Text)
    package_marks: Mapped[str | None] = mapped_column(Text)
    package_range: Mapped[str | None] = mapped_column(Text)
    country_of_origin: Mapped[str | None] = mapped_column(Text)
    net_weight_unit: Mapped[str | None] = mapped_column(String(16))
    gross_weight_unit: Mapped[str | None] = mapped_column(String(16))
    chargeable_weight_unit: Mapped[str | None] = mapped_column(String(16))
    chargeable_weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    measurements: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    cell_sources: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    hs_code: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    quantity_unit: Mapped[str | None] = mapped_column(String(16))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str | None] = mapped_column(String(8))
    package_count: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    package_unit: Mapped[str | None] = mapped_column(String(16))
    net_weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    gross_weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    weight_unit: Mapped[str | None] = mapped_column(String(8))
    source_segment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_column()


class LineMatchEdge(Base):
    """跨单据行关系是多对多边并附依据，不按行号硬配。"""

    __tablename__ = "line_match_edges"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    left_line_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    right_line_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    relation: Mapped[str] = mapped_column(String(16), nullable=False)
    basis: Mapped[str | None] = mapped_column(Text)
    # candidate / confirmed / rejected
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_column()


class BusinessRevision(Base):
    __tablename__ = "business_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "revision_no", name="uq_business_revisions_tenant_id_case_id_revision_no"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # baseline_extraction / field_revision / section_assignment / issue_adjudication / reparse
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_column()


class BusinessRevisionField(Base):
    """业务版引用的有效断言；新版复用未变断言，不复制原文。"""

    __tablename__ = "business_revision_fields"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "business_revision_id", "field_assertion_id",
            name="uq_business_revision_fields_revision_assertion",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    business_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("business_revisions.id", ondelete="CASCADE"), nullable=False
    )
    field_assertion_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class SectionAssignmentDecision(Base):
    """人工改类型／区段归属：保留解析层机器候选，写业务覆盖。"""

    __tablename__ = "section_assignment_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    section_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    logical_document_type: Mapped[str | None] = mapped_column(String(32))
    cargo_profile_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    target_logical_document_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    confirmed_language: Mapped[str | None] = mapped_column(String(16))
    decided_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class FieldReviewEvent(Base):
    """「确认无误／无法判断」的审核记录；确认字段不等于裁定红项。"""

    __tablename__ = "field_review_events"
    __table_args__ = (Index("ix_field_review_events_tenant_id_field_assertion_id", "tenant_id", "field_assertion_id"),
        Index("uq_field_review_events_case_review_sequence","tenant_id","case_id","review_sequence",unique=True),
        CheckConstraint("review_sequence > 0",name="review_sequence_positive"))

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    field_assertion_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    review_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # confirmed / undetermined
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    required_materials: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class ArtifactDependency(Base):
    """
    派生产物实际读取的输入与指纹。

    只让依赖变化的规则、译段、预览、摘要过期；
    不因 Case 版本号改变就全票重复 OCR。
    """

    __tablename__ = "artifact_dependencies"
    __table_args__ = (Index("ix_artifact_dependencies_tenant_id_artifact", "tenant_id", "artifact_kind", "artifact_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    depends_on: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_column()
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
