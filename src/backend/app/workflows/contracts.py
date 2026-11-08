from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.config import ModelPrice, ModelProfile, ModelRole
from app.router.contracts import DocumentTypeInput, SelectedSectionRef, SourceVersion
from app.review.jurisdiction import JurisdictionInputs


class TranslateParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_language: str | None = None
    translation_scope: str = "all"
    selected_section_refs: list[SelectedSectionRef] = Field(default_factory=list)
    scope_selector_version: str | None = None
    output_formats: list[str] = Field(default_factory=lambda: ["docx", "pdf"])
    glossary_version: str | None = None
    rule_pack_version: str = "core-document@3"
    jurisdiction_inputs: JurisdictionInputs | None = None
    document_type_inputs: list[DocumentTypeInput] = Field(default_factory=list)


class ReadParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_scope: str = "selected_documents"
    document_type_inputs: list[DocumentTypeInput] = Field(default_factory=list)


class ConsignmentParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_revision_id: UUID | None = None
    comparison_rule_version: Literal["consignment-review@1"] = "consignment-review@1"
    previous_batch_id: UUID | None = None
    document_type_inputs: list[DocumentTypeInput] = Field(default_factory=list)


class TranslateConsignmentParameters(TranslateParameters):
    scope_revision_id: UUID | None = None
    comparison_source_versions: list[SourceVersion] = Field(default_factory=list)
    comparison_rule_version: Literal["consignment-review@1"] = "consignment-review@1"


class ModelBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: ModelRole
    base_url: str
    model_name: str
    protocol: str
    timeout_seconds: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_concurrency: int = Field(gt=0)
    context_window_tokens: int = Field(default=16000, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    enable_thinking: bool | None = None
    public_config_hash: str
    adapter_version: str
    price: ModelPrice | None = None

    @classmethod
    def from_profile(
        cls, role: ModelRole, profile: ModelProfile, adapter_version: str
    ) -> "ModelBinding":
        return cls(
            role=role,
            base_url=profile.base_url,
            model_name=profile.name,
            protocol=profile.protocol,
            timeout_seconds=profile.timeout_seconds,
            max_output_tokens=profile.max_output_tokens,
            max_concurrency=profile.max_concurrency,
            context_window_tokens=profile.context_window_tokens,
            temperature=profile.temperature,
            enable_thinking=profile.enable_thinking,
            public_config_hash=profile.public_fingerprint(),
            adapter_version=adapter_version,
            price=profile.price,
        )


class ExecutionEpoch(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    epoch: int = Field(ge=1)
    workflow_version: int = Field(ge=1)
    purpose: Literal["initial", "recompute", "export"]
    task_spec_hash: str
    input_business_revision_id: str | None = None
    input_parse_artifact_ids: tuple[str, ...] = ()
    model_bindings: tuple[ModelBinding, ...]
    created_at: AwareDatetime
