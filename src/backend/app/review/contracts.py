from typing import Literal
from app.languages import LanguageCode

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.router.contracts import SourceVersion


class ReviewSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_version: SourceVersion
    parse_artifact_id: UUID


class TranslationCompatibility(BaseModel):
    model_config = ConfigDict(frozen=True)

    compatibility_id: UUID
    run_id: UUID
    target_language: LanguageCode
    business_revision_id: str
    translation_revision_id: str
    dependency_fingerprint: str
    qa_summary_hash: str
    created_at: AwareDatetime


class ConfirmReviewCommand(BaseModel):
    run_id: UUID
    expected_business_revision_id: str
    expected_translation_revision_id: str
    expected_review_fingerprint: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class ReviewConfirmation(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirmation_id: UUID
    run_id: UUID
    task_spec_hash: str
    sources: tuple[ReviewSource, ...] = Field(min_length=1)
    business_revision_id: str
    translation_revision_id: str
    translation_compatibility_id: UUID | None
    issue_summary_hash: str
    qa_summary_hash: str
    review_fingerprint: str = Field(min_length=1)
    output_formats: tuple[Literal["docx", "pdf"], ...]
    confirmed_by: UUID
    confirmed_at: AwareDatetime


class StepReconciliationCommand(BaseModel):
    run_id: UUID
    step_execution_id: UUID
    expected_step_version: int = Field(ge=1)
    decision: Literal["verified_executed", "confirmed_not_executed", "still_unknown", "terminate_run"]
    evidence_reference: str | None = None
    external_task_id: str | None = None
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_verification_evidence(self) -> "StepReconciliationCommand":
        if self.decision in {"verified_executed", "confirmed_not_executed"} and not self.evidence_reference:
            raise ValueError("明确外部请求结论必须附核查凭据")
        if self.decision == "verified_executed" and not self.external_task_id:
            raise ValueError("已执行的外部请求必须提供任务 ID")
        return self


class ModelMigrationCommand(BaseModel):
    run_id: UUID
    candidate_config_fingerprint: str = Field(min_length=1)
    expected_execution_epoch: int = Field(ge=1)
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
