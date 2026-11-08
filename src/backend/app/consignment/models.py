"""不可变比较输入与发布批次；当前指针仅是可更新投影。"""
from __future__ import annotations

from uuid import UUID
from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.infra.models_base import Base, TenantMixin


class CaseScopeRevision(TenantMixin, Base):
    __tablename__ = "case_scope_revisions"
    __table_args__ = (UniqueConstraint("tenant_id", "case_id", "id"),
        UniqueConstraint("tenant_id", "case_id", "revision_no"),)
    case_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("case_scope_revisions.id"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[UUID] = mapped_column(PgUUID, nullable=False)


class RelationAssertion(TenantMixin, Base):
    __tablename__ = "relation_assertions"
    __table_args__ = (UniqueConstraint("tenant_id", "case_id", "id"),
        ForeignKeyConstraint(["tenant_id", "case_id", "scope_revision_id"],
            ["case_scope_revisions.tenant_id", "case_scope_revisions.case_id", "case_scope_revisions.id"]),)
    case_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    scope_revision_id: Mapped[UUID] = mapped_column(PgUUID, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    left_logical_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("logical_documents.id"), nullable=False)
    right_logical_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("logical_documents.id"), nullable=False)
    basis: Mapped[dict] = mapped_column(JSONB, nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("relation_assertions.id"))
    decided_by: Mapped[UUID | None] = mapped_column(PgUUID)


class CargoMatchGroup(TenantMixin, Base):
    __tablename__ = "cargo_match_groups"
    __table_args__ = (UniqueConstraint("tenant_id", "case_id", "id"),
        ForeignKeyConstraint(["tenant_id", "case_id", "scope_revision_id"],
            ["case_scope_revisions.tenant_id", "case_scope_revisions.case_id", "case_scope_revisions.id"]),)
    case_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    scope_revision_id: Mapped[UUID] = mapped_column(PgUUID, nullable=False)
    relation_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("relation_assertions.id"))
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    cardinality: Mapped[str] = mapped_column(String(12), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    basis: Mapped[dict] = mapped_column(JSONB, nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("cargo_match_groups.id"))
    decided_by: Mapped[UUID | None] = mapped_column(PgUUID)


class CargoMatchMember(TenantMixin, Base):
    __tablename__ = "cargo_match_members"
    __table_args__ = (UniqueConstraint("tenant_id", "group_id", "side", "line_item_id"),
        CheckConstraint("side IN ('left','right')", name="member_side"),
        ForeignKeyConstraint(["tenant_id", "case_id", "group_id"],
            ["cargo_match_groups.tenant_id", "cargo_match_groups.case_id", "cargo_match_groups.id"], ondelete="CASCADE"),)
    case_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    group_id: Mapped[UUID] = mapped_column(PgUUID, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    line_item_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("line_items.id"), nullable=False)
    basis: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ComparisonBatch(TenantMixin, Base):
    __tablename__ = "comparison_batches"
    __table_args__ = (UniqueConstraint("tenant_id", "case_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "epoch"),
        ForeignKeyConstraint(["tenant_id", "case_id", "run_id"],
            ["runs.tenant_id", "runs.case_id", "runs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "case_id", "scope_revision_id"],
            ["case_scope_revisions.tenant_id", "case_scope_revisions.case_id", "case_scope_revisions.id"]),)
    case_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PgUUID, nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_revision_id: Mapped[UUID] = mapped_column(PgUUID, nullable=False)
    business_revision_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("business_revisions.id"), nullable=False)
    parent_batch_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("comparison_batches.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class RunComparisonBinding(TenantMixin, Base):
    __tablename__ = "run_comparison_bindings"
    __table_args__ = (UniqueConstraint("tenant_id", "case_id", "id"),
        ForeignKeyConstraint(["tenant_id", "case_id", "run_id"],
            ["runs.tenant_id", "runs.case_id", "runs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "case_id", "batch_id"],
            ["comparison_batches.tenant_id", "comparison_batches.case_id", "comparison_batches.id"]),)
    case_id: Mapped[UUID] = mapped_column(PgUUID, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PgUUID, nullable=False)
    batch_id: Mapped[UUID] = mapped_column(PgUUID, nullable=False)
    delivery_document_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(PgUUID, ForeignKey("run_comparison_bindings.id"))
