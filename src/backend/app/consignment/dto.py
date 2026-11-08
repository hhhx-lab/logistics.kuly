"""同票比较公开契约，来源链复用复核EvidenceTarget。"""
from typing import Literal
from uuid import UUID
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, ConfigDict, Field, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Material(StrictModel):
    document_id: UUID
    document_version: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    purpose: Literal["active", "attachment", "excluded"] = "active"

class ScopeParty(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=40)
    label: str = Field(max_length=200)
    logical_document_ids: list[UUID] = Field(default_factory=list, max_length=100)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)

class ScopeLeg(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    mode: Literal["sea", "air", "road", "rail", "courier", "unknown"] = "unknown"
    origin: str | None = Field(default=None,max_length=300)
    destination: str | None = Field(default=None,max_length=300)
    logical_document_ids: list[UUID] = Field(default_factory=list,max_length=100)
    evidence_ids: list[UUID] = Field(default_factory=list,max_length=100)

class ScopeCargo(StrictModel):
    id: str = Field(min_length=1,max_length=100)
    label: str = Field(max_length=300)
    logical_document_ids: list[UUID] = Field(default_factory=list,max_length=100)
    evidence_ids: list[UUID] = Field(default_factory=list,max_length=100)

class ScopePayload(StrictModel):
    schema_version: Literal[1] = 1
    baseline_business_revision_id: UUID | None = None
    materials: list[Material] = Field(max_length=200)
    parties: list[ScopeParty] = Field(default_factory=list, max_length=100)
    legs: list[ScopeLeg] = Field(default_factory=list, max_length=100)
    cargo: list[ScopeCargo] = Field(default_factory=list, max_length=200)
    relation_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    match_group_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    @model_validator(mode="after")
    def unique_materials(self):
        ids = [m.document_id for m in self.materials]
        if len(ids) != len(set(ids)): raise ValueError("Duplicate material document")
        return self

class ScopeView(StrictModel):
    case_id: UUID
    scope_revision_id: UUID | None
    revision_no: int | None
    payload: ScopePayload
    case_version: int
    business_revision_id: UUID | None
    review_state_version: int
    stale: bool
    fingerprint: str

class Preconditions(StrictModel):
    expected_case_version: int
    expected_scope_revision_id: UUID | None
    expected_business_revision_id: UUID | None
    expected_review_state_version: int

class ScopeCommand(Preconditions):
    payload: ScopePayload

class RelationDecision(Preconditions):
    relation_id: UUID
    decision: Literal["confirm", "reject", "correct"]
    kind: str | None = Field(default=None, max_length=40)
    reason: str = Field(default="", max_length=2000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=50)

class MatchScope(StrictModel):
    cargo_id: str = Field(min_length=1, max_length=100)
    leg_id: str = Field(min_length=1, max_length=100)
    quantity_kind: str = Field(default="quantity", max_length=40)
    package_level: str | None = Field(default=None, max_length=40)
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    absolute_tolerance: str | None = Field(default=None, max_length=30)
    @model_validator(mode="after")
    def valid_measurement(self):
        allowed={"quantity","statutory_quantity_1","statutory_quantity_2","declared_quantity","received_quantity","net_weight","gross_weight","chargeable_weight","package_count"}
        if self.quantity_kind not in allowed:raise ValueError("Unsupported measurement kind")
        if self.absolute_tolerance is not None:
            try:value=Decimal(self.absolute_tolerance)
            except InvalidOperation:raise ValueError("Invalid tolerance") from None
            if not value.is_finite() or value<0:raise ValueError("Invalid tolerance")
        return self

class CargoDecision(Preconditions):
    left_line_ids: list[UUID] = Field(min_length=1, max_length=200)
    right_line_ids: list[UUID] = Field(min_length=1, max_length=200)
    scope: MatchScope
    basis: str = Field(min_length=1, max_length=2000)
    supersedes_id: UUID | None = None

class RecomputeCommand(Preconditions):
    previous_batch_id: UUID | None = None

class RelationView(StrictModel):
    id: UUID
    kind: str
    state: str
    left_logical_id: UUID
    right_logical_id: UUID
    basis: dict

class RelationPage(StrictModel):
    items: list[RelationView]
    next_cursor: str | None = None

class BatchView(StrictModel):
    batch_id: UUID
    case_id: UUID
    run_id: UUID
    epoch: int
    scope_revision_id: UUID
    business_revision_id: UUID
    status: str
    current: bool
    stale: bool
    summary: dict
    manifest: dict
    input_fingerprint: str
    created_at: datetime

class BatchPage(StrictModel):
    items: list[BatchView]
    next_cursor: str | None = None

class MatrixRow(StrictModel):
    row_id: UUID
    batch_id: UUID
    rule_check_id: UUID
    rule_id: str
    rule_version: int
    comparison_basis: str
    group_id: UUID | None = None
    status: str
    difference_kind: str
    severity: str
    description: str
    members: dict
    calculation: dict
    sources: list[dict]
    issue_id: UUID | None = None
    disposition: dict | None = None
    dependency_fingerprint: str

class MatrixPage(StrictModel):
    items: list[MatrixRow]
    next_cursor: str | None = None
    review_state_version: int

class EvidenceView(StrictModel):
    row_id: UUID
    sides: dict[str, list[dict]]

class MutationView(StrictModel):
    case_id: UUID
    scope_revision_id: UUID
    case_version: int
    review_state_version: int
    run_id: UUID | None = None
    epoch: int | None = None
    batch_id: UUID | None = None
