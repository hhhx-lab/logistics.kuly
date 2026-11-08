"""已发布的工作流响应；固定结构拒绝漂移，动态 JSON Schema 保留扩展。

核心字段必填，历史扩展字段可省略；路由 exclude_unset 保留省略与显式 null。
"""

from __future__ import annotations
from app.languages import LanguageCode

from typing import Literal, Union
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from app.review.jurisdiction import JurisdictionInputs
from app.workflows.registry import WORKFLOWS
from app.workflows.models import DraftStatus, RunStatus


class PublishedView(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowUiContract(PublishedView):
    version: str
    component: str
    parameter_schema: dict[str, JsonValue]
    result_actions: list[str]
    review_component: str


class WorkflowArtifactPolicy(PublishedView):
    output_types: list[str]
    export_snapshot_required: bool
    bilingual_per_source: bool
    translation_only_export: bool
    case_report: bool
    verification_policy: str


class RulePackSource(PublishedView):
    url: str
    version: str
    provisions: list[str]


class PublishedRulePack(PublishedView):
    version: str
    title: str
    status: str
    notice: str | None = None
    parameter_schema: dict[str, JsonValue] | None = None
    legal_review_status: str | None = None
    valid_from: str | None = None
    valid_before: str | None = None
    sources: list[RulePackSource] | None = None


class WorkflowView(PublishedView):
    # 类型由注册表发布，响应模型不另设工作流白名单。
    workflow_type: str
    workflow_version: int
    title: str
    enabled: bool
    disabled_reason: str | None = None
    accepted_mime: list[str]
    max_file_bytes: int
    visible_stages: list[str]
    output_types: list[str]
    stages: list[str] | None = None
    supported_languages: list[str] | None = None
    supported_formats: list[str] | None = None
    required_permissions: list[str] | None = None
    graph_version: str | None = None
    cost_level: str | None = None
    ui_contract: WorkflowUiContract | None = None
    artifact_policy: WorkflowArtifactPolicy | None = None
    task_form_schema: dict[str, JsonValue] | None = None
    is_synthetic: bool | None = None
    published_rule_packs: list[PublishedRulePack] | None = None


class WorkflowListView(PublishedView):
    items: list[WorkflowView]


class WorkflowSourceVersion(PublishedView):
    document_id: UUID
    document_version: int = Field(ge=1)
    case_id: UUID
    filename: str
    mime: str
    size_bytes: int
    uploaded_by: UUID
    uploaded_at: AwareDatetime
    page_count: int | None = Field(default=None, ge=1)
    detected_language: str | None = None
    language_uncertain: bool | None = None


DraftParameters = Union[tuple(dict.fromkeys(definition.parameter_model for definition in WORKFLOWS.values()))]


class ModelCostEstimateView(PublishedView):
    status: Literal["synthetic", "bounded_model_budget", "unknown"]
    cost_level: str
    known_pages: int
    unknown_page_files: int
    target_language_count: int
    target_language: str | None
    scope: str
    model_budget_upper_cny: str | None
    predicted_model_cost_cny: str | None
    predicted_total_cost_cny: str | None
    ocr_cost_cny: str | None
    model_price_versions: list[str]
    ocr_estimated_pages_upper: int
    ocr_estimated_tasks_upper: int
    ocr_unknown_page_files: int
    ocr_estimate_upper_cny: str | None
    ocr_estimate_status: Literal["bounded", "unknown"]
    ocr_price_versions: list[str]
    ocr_estimate_basis: str
    estimated_external_cost_upper_cny: str | None
    total_cost_scope: Literal["model_and_ocr"]
    total_cost_cap_enforced: Literal[False]
    budget_scope: Literal["model_only"]
    notice: str


class ModelCostSummaryView(PublishedView):
    known_model_cost_cny: str
    actual_model_cost_cny: str | None
    unknown_model_call_count: int
    pending_model_step_count: int
    unknown_model_step_count: int
    cost_measurement_status: Literal["pending", "unknown", "complete"]
    model_cost_status: Literal["known", "unknown"]
    ocr_cost_cny: str | None
    actual_ocr_cost_cny: str | None
    ocr_cost_status: Literal["known", "unknown"]
    ocr_task_count: int
    unknown_ocr_task_count: int
    ocr_estimate_scope: Literal["prepared_requests_only"]
    ocr_estimate_upper_cny: str | None
    ocr_estimate_status: Literal["bounded", "unknown"]
    total_cost_scope: Literal["model_and_ocr"]
    total_cost_cap_enforced: Literal[False]
    actual_total_cost_cny: str | None
    budget_scope: Literal["model_only"]
    notice: str


class TaskDraftView(PublishedView):
    draft_task_id: UUID
    case_id: UUID
    workflow_type: str
    workflow_version: int
    source_versions: list[WorkflowSourceVersion]
    # 参数默认值由领域规范化；响应 exclude_unset 不补出此前省略的字段。
    draft_parameters: DraftParameters
    status: DraftStatus
    missing_fields: list[str]
    questions: list[str]
    task_summary: str
    expires_at: AwareDatetime
    draft_version: int
    started_run_id: UUID | None = None
    cost_estimate: ModelCostEstimateView | None = None


class RunView(PublishedView):
    cost_summary: ModelCostSummaryView | None = None
    run_id: UUID
    case_id: UUID
    case_name: str
    workflow_type: str
    workflow_version: int
    task_spec_hash: str
    source_versions: list[WorkflowSourceVersion]
    initiated_by: UUID
    initiated_at: AwareDatetime
    target_language: LanguageCode | None = None
    translation_scope: Literal["all", "goods_description", "selected_sections"] | None = None
    output_formats: list[Literal["docx", "pdf"]] | None = None
    rule_pack_version: str | None = None
    jurisdiction_inputs: JurisdictionInputs | None = None
    status: RunStatus
    current_stage: str
    execution_epoch: int
    last_seq: int
    has_successful_snapshot: bool
    latest_success_snapshot_id: UUID | None = None
    latest_success_snapshot_at: AwareDatetime | None = None
    current_business_revision_id: UUID | None = None
    current_translation_revision_id: UUID | None = None
    current_review_confirmation_id: UUID | None = None
    confirmation_status: Literal["none", "valid", "stale"]
    blocking_issue_count: int
    warning_issue_count: int
    undetermined_issue_count: int | None = None
    rules_status: Literal["run", "not_run"] | None = None
    active_input_request_id: UUID | None = None
    latest_export_attempt_id: UUID | None = None
    latest_export_attempt_status: Literal["queued", "rendering", "checking", "failed_retryable", "failed_final", "succeeded", "paused_reconcile"] | None = None
    latest_export_attempt_error_summary: str | None = None
    allowed_actions: list[str]
    disabled_reasons: dict[str, str]
    ops_reconcile_step_id: UUID | None = None
    pause_explanation: str | None = None
    failure_reason: str | None = None
    updated_at: AwareDatetime
