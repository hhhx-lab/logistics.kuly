"""规则、人工处置与不可变译文视图。来源依赖复用 documents.evidence_models。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.documents.evidence_models import ArtifactDependency
from app.infra.models_base import Base, TenantMixin


class RuleCheck(TenantMixin, Base):
    __tablename__ = "rule_checks"
    comparison_batch_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("comparison_batches.id", ondelete="CASCADE"))
    comparison_basis: Mapped[str | None] = mapped_column(String(12))
    stable_check_key: Mapped[str | None] = mapped_column(String(64))
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id"), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rule_pack_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    input_field_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_segment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    calculation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class Issue(TenantMixin, Base):
    __tablename__ = "issues"
    comparison_batch_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("comparison_batches.id", ondelete="CASCADE"))
    stable_issue_key: Mapped[str | None] = mapped_column(String(64))
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id"), nullable=False)
    translation_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("translation_revisions.id"))
    rule_check_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("rule_checks.id"))
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    blocks_export: Mapped[bool] = mapped_column(Boolean, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    guidance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 一条来源含 document/version/parse/logical/section/field/segment 与原值。
    sources: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    supersedes_issue_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("issues.id"))


class IssueDisposition(TenantMixin, Base):
    __tablename__ = "issue_dispositions"
    __table_args__=(Index("uq_issue_dispositions_case_review_sequence","tenant_id","case_id","review_sequence",unique=True),
        CheckConstraint("review_sequence > 0",name="review_sequence_positive"))
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    issue_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("issues.id"), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id"), nullable=False)
    review_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    basis: Mapped[str | None] = mapped_column(Text)
    adopted_assertion_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("field_assertions.id"))
    decided_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class ResolvedTranslationScope(TenantMixin, Base):
    __tablename__ = "resolved_translation_scopes"
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("parse_artifacts.id"), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id"), nullable=False)
    selector_version: Mapped[str] = mapped_column(String(80), nullable=False)
    source_segment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_section_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))


class TranslationRevision(TenantMixin, Base):
    __tablename__ = "translation_revisions"
    __table_args__ = (UniqueConstraint("tenant_id", "run_id", "target_language", "revision_no"),)
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id"), nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("translation_revisions.id"))
    parse_artifact_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    resolved_scope_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 全视图的有序片段 ID；新版复用未变片段，只换编辑片段，不复制/覆盖旧译文。
    segment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    prompt_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_bindings: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    glossary_version: Mapped[str | None] = mapped_column(String(80))
    qa_status: Mapped[str] = mapped_column(String(24), nullable=False)
    qa_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))


class TranslationSegment(TenantMixin, Base):
    __tablename__ = "translation_segments"
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("parse_artifacts.id"), nullable=False)
    section_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("document_sections.id"), nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_segment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    protected_tokens: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    supersedes_segment_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("translation_segments.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class SourceTranslationLink(TenantMixin, Base):
    __tablename__ = "source_translation_links"
    __table_args__ = (UniqueConstraint("tenant_id", "source_segment_id", "translation_segment_id"),)
    source_segment_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("source_segments.id"), nullable=False)
    translation_segment_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("translation_segments.id"), nullable=False)


class TranslationCompatibility(TenantMixin, Base):
    __tablename__ = "translation_compatibilities"
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id"), nullable=False)
    translation_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("translation_revisions.id"), nullable=False)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    qa_summary_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PreviewArtifact(TenantMixin, Base):
    __tablename__ = "preview_artifacts"
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_artifact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("parse_artifacts.id"), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id"), nullable=False)
    translation_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("translation_revisions.id"), nullable=False)
    resolved_scope_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("resolved_translation_scopes.id"), nullable=False)
    compatibility_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("translation_compatibilities.id"))
    target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    preview_version: Mapped[int] = mapped_column(Integer, nullable=False)
    render_config_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reading_html: Mapped[str] = mapped_column(Text, nullable=False)
    segment_mappings: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    draft_pdf_object_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    draft_pdf_disabled_reason: Mapped[str | None] = mapped_column(Text)


class RuleInputDecision(TenantMixin, Base):
    """每个业务版本的人工适用事实；旧版本、原候选及规则结论不改写。"""
    __tablename__ = "rule_input_decisions"
    __table_args__ = (UniqueConstraint("tenant_id", "business_revision_id", "kind", "scope_key"),)
    case_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    business_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("business_revisions.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
