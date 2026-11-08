"""成功JSON响应契约。业务结构明确发布；JSON值仅用于注册Schema与版本化依赖元数据。"""
from app.languages import LanguageCode

from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID
from pydantic import AliasChoices, AwareDatetime, Field, JsonValue
from app.api.dto import PublishedView, WorkflowSourceVersion, WorkflowUiContract
from app.review.jurisdiction import JurisdictionInputs

T = TypeVar("T")


class PageView(PublishedView, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool | None = None
    as_of: AwareDatetime | None = None
    review_state_version: int | None = None


class OkView(PublishedView):
    ok: Literal[True]


class SourceView(WorkflowSourceVersion):
    parse_artifact_id: UUID | None = None
    page_count: int | None = None
    detected_language: str | None = None
    language_uncertain: bool | None = None


class TargetView(PublishedView):
    case_id: UUID
    document_id: UUID | None = None
    document_version: int | None = None
    parse_artifact_id: UUID | None = None
    logical_document_id: UUID | None = None
    section_id: UUID | None = None
    source_segment_id: UUID | None = None
    source_anchor_id: UUID | None = None
    business_revision_id: UUID | None = None
    field_id: UUID | None = None
    issue_id: UUID | None = None
    line_item_id: UUID | None = None
    run_id: UUID | None = None
    target_language: LanguageCode | None = None
    translation_revision_id: UUID | None = None
    translation_segment_id: UUID | None = None
    preview_version: int | None = None
    export_snapshot_id: UUID | None = None
    compatibility_id: UUID | None = None


class PreferencesView(PublishedView):
    interface_language: LanguageCode
    timezone: str
    panel_width: int
    motion: Literal["system", "reduce", "standard"]
    version: int
    saved_at: AwareDatetime | None = None


class TimezoneView(PublishedView):
    id: str
    label: str
    offset: str


MemoryCategory = Literal["answer_language", "target_language", "presentation", "terminology"]


class MemoryView(PublishedView):
    source_restricted: bool | None = None
    memory_id: UUID
    category: MemoryCategory
    content: str
    applies_when: str | None = None
    source: Literal["manual", "suggestion"]
    source_label: str
    confirmed_at: AwareDatetime
    version: int


class MemoryListView(PublishedView):
    items: list[MemoryView]
    memory_version: int


class MemoryPreviewView(PublishedView):
    affects: list[str]
    not_affects: list[str]
    rejected_reason: str | None = None
    rejected_field: Literal["category", "content", "applies_when"] | None = None


class MemorySuggestionView(PublishedView):
    suggestion_id: UUID
    category: MemoryCategory
    content: str
    applies_when: str | None = None
    from_conversation: str | None = None
    source_restricted: bool
    origin: Literal["explicit_request", "repeated_setting"]
    created_at: AwareDatetime


class FieldErrorView(PublishedView):
    field: str
    message: str


class SkillParameterView(PublishedView):
    name: str
    type: Literal["string", "enum", "boolean"]
    required: bool = False
    description: str = ""
    options: list[str] | None = None
    default_value: str | bool | None = None


ExtensionStatus = Literal["draft", "validating", "available", "failed", "disabled", "archived"]


class SkillDefinitionView(PublishedView):
    name: str
    identifier: str
    purpose: str
    when_to_use: str
    when_not_to_use: str
    parameters: list[SkillParameterView]
    allowed_actions: list[str]
    result_format: str
    instructions: str


class SkillVersionView(SkillDefinitionView):
    version: int
    expected_version: int
    status: ExtensionStatus
    validated_at: AwareDatetime | None = None
    validation_errors: list[FieldErrorView]
    ever_enabled: bool


class SkillCreatedView(PublishedView):
    skill_id: UUID
    version: SkillVersionView


class SkillDetailView(PublishedView):
    skill_id: UUID
    builtin: bool
    versions: list[SkillVersionView]
    enabled_version: int | None = None


class ImportDiffView(PublishedView):
    item: str
    verdict: Literal["可用", "未知动作", "越权", "不支持脚本", "不支持的传输", "低信任指令"]
    detail: str


class SkillImportView(PublishedView):
    draft: SkillDefinitionView
    diff: list[ImportDiffView]


class McpImportDraftView(PublishedView):
    name: str | None = None
    endpoint: str | None = None
    auth_type: str | None = None


class McpImportView(PublishedView):
    draft: McpImportDraftView
    diff: list[ImportDiffView]


class McpToolView(PublishedView):
    tool_id: str
    name: str
    description: str
    schema_hash: str
    input_schema: dict[str, JsonValue]
    category: Literal["read_only", "write", "egress"]
    scope: str
    data_level: str
    input_schema_summary: str
    previous_schema_hash: str | None = None
    previous_schema_summary: str | None = None
    granted: bool
    public_scope_ready: bool
    public_parameters: dict[str, list[str | bool]] | None = None
    unavailable_reason: str | None = None
    grant_id: UUID | None = None
    schema_changed: bool


class McpTestStageView(PublishedView):
    stage: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    duration_ms: float
    ok: bool
    error_class: str | None = None
    schema_hash: str | None = None


class McpVersionView(PublishedView):
    version: int
    expected_version: int
    status: ExtensionStatus
    name: str
    endpoint: str
    endpoint_summary: str
    template: Literal["streamable_http"]
    template_label: str
    auth_type: Literal["none", "bearer"]
    credential_set: bool
    tested_at: AwareDatetime | None = None
    test_stages: list[McpTestStageView]
    error_class: str | None = None
    tools: list[McpToolView]


class McpCreatedView(PublishedView):
    connection_id: UUID
    version: McpVersionView


class McpDetailView(PublishedView):
    connection_id: UUID
    versions: list[McpVersionView]
    enabled_version: int | None = None


class AuthTypeView(PublishedView):
    id: Literal["none", "bearer"]
    label: str
    available: bool
    disabled_reason: str | None = None


class McpTemplateView(PublishedView):
    template: Literal["streamable_http"]
    label: str
    description: str
    auth_types: list[AuthTypeView]
    credential_store_ready: bool
    credential_store_note: str
    public_registry_ready: bool
    unavailable_reason: str | None = None


class WarningView(PublishedView):
    code: str
    message: str


class ExtensionRowView(PublishedView):
    extension_id: UUID
    kind: Literal["builtin_skill", "user_skill", "mcp"]
    name: str
    status: ExtensionStatus
    row_state: Literal["enabled", "ready_to_enable", "draft", "validating", "disabled", "archived", "validation_failed", "connect_failed", "needs_retest", "needs_recheck", "syncing"]
    version: int
    enabled_version: int | None = None
    updated_at: AwareDatetime
    tested_at: AwareDatetime | None = None
    summary: str
    source_label: str
    permission: str
    transport_label: str | None = None
    granted_tool_count: int | None = None
    discovered_tool_count: int | None = None
    error_class: str | None = None
    owner: str
    read_only: bool
    syncing: bool | None = None
    unavailable_reason: str | None = None


class ExtensionListView(PublishedView):
    items: list[ExtensionRowView]
    warnings: list[WarningView]


class SkillActionView(PublishedView):
    action_id: str
    label: str
    group: Literal["read_only", "controlled_write"]
    available: bool
    disabled_reason: str | None = None


class CombinationPolicyView(PublishedView):
    readonly_actions: list[str]
    max_readonly_steps: int
    max_business_steps: int
    allow_mixed: bool


class SkillActionsView(PublishedView):
    items: list[SkillActionView]
    combination_policy: CombinationPolicyView


class ExtensionCallView(PublishedView):
    case_ref: str | None = None
    conversation_id: UUID | None = None
    parent_turn_id: UUID | None = None
    message_id: UUID | None = None
    reference_snapshot_id: UUID | None = None
    params_redacted: str | None = None
    destination: str | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_ms: int | None = None
    call_id: UUID
    extension_id: UUID
    name: str
    action: str
    at: AwareDatetime
    caller: str
    extension_version: int
    tool_version: str | None = None
    status: Literal["succeeded", "failed", "rejected"]
    reject_class: str | None = None
    error_class: str | None = None
    result_redacted: str


class CapabilityView(PublishedView):
    kind: Literal["builtin_skill", "user_skill", "mcp_tool"]
    extension_id: UUID
    extension_version: int
    tool_id: str | None = None
    tool_schema_hash: str | None = None
    name: str
    purpose: str
    permission: Literal["只读", "受控写入", "外发"]
    connection_status: Literal["可用", "未连接", "不可用"]
    unavailable_reason: str | None = None
    group: Literal["内置 Skill", "已启用 Skill", "可用 MCP 工具"]
    parameters: list[SkillParameterView] | None = None
    workflow_type: str | None = None
    workflow_version: int | None = None
    parameter_schema: dict[str, JsonValue] | None = None
    required_permissions: list[str] | None = None
    cost_level: str | None = None
    accepted_mime: list[str] | None = None
    output_types: list[str] | None = None
    ui_contract: WorkflowUiContract | None = None


class CapabilityListView(PublishedView):
    items: list[CapabilityView]
    warnings: list[WarningView]


class ReferenceView(PublishedView):
    unavailable_reason: str | None = None
    kind: Literal["case", "run", "document", "section", "snapshot"]
    id: UUID
    selection_mode: Literal["current", "fixed"]
    case_id: UUID
    label: str | None = None
    document_id: UUID | None = None
    document_version: int | None = None
    parse_artifact_id: UUID | None = None
    logical_document_id: UUID | None = None
    section_id: UUID | None = None
    snapshot_id: UUID | None = None
    run_id: UUID | None = None
    target_language: LanguageCode | None = None
    translation_revision_id: UUID | None = None


class SelectedCapabilityView(PublishedView):
    kind: Literal["builtin_skill", "user_skill", "mcp_tool"]
    extension_id: UUID
    extension_version: int
    tool_id: str | None = None
    tool_schema_hash: str | None = None
    user_parameters: dict[str, JsonValue] | None = None
    label: str | None = None


class ConversationView(PublishedView):
    restricted: bool | None = None
    conversation_id: UUID
    title: str
    updated_at: AwareDatetime
    status: Literal["active", "ended"]
    archived_at: AwareDatetime | None = None
    deleted_at: AwareDatetime | None = None
    case_id: UUID | None = None
    case_name: str | None = None
    case_status_label: str | None = None
    conversation_version: int
    active_run_id: UUID | None = None
    active_task_draft_id: UUID | None = None
    case_business_revision_no: int | None = None
    initial_reference_selections: list[ReferenceView]
    entry_source: str | None = None


class TurnView(PublishedView):
    turn_id: UUID
    conversation_id: UUID
    status: Literal["running", "completed", "interrupted", "failed"]
    last_seq: int
    reference_snapshot_id: UUID


class ContextView(PublishedView):
    policy_version: str
    memory_version: int
    confirmed_preference_count: int
    case_label: str | None = None
    frozen_references: list[ReferenceView]
    upload_status: str
    turn_range: str
    total_turns: int
    summary_in_use: bool
    token_budget_note: str


class EvidenceCardView(PublishedView):
    card_id: str
    filename: str
    logical_document: str
    location: str
    excerpt: str
    status: Literal["candidate", "confirmed", "conflict", "undetermined", "historical"]
    versions: str
    target: TargetView


class EvidenceMessageCard(PublishedView):
    type: Literal["evidence"]
    cards: list[EvidenceCardView]


class AdoptedMessageCard(PublishedView):
    type: Literal["adopted_value"]
    field: str
    original: str
    adopted: str
    by: str
    at: AwareDatetime
    reason: str
    basis: str | None = None
    target: TargetView


class TranslationMessageCard(PublishedView):
    type: Literal["translation"]
    source: str
    translated: str
    target_language: str
    run_id: UUID
    translation_revision: str
    preview_version: str
    state: Literal["draft", "valid", "historical"]
    target: TargetView


class SourceLocationView(PublishedView):
    source_anchor_id: UUID
    precision: Literal["rect", "page_only", "paragraph_only", "cell", "none"]
    page_no: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    coord_system: str | None = None
    render_version: str | None = None


class SourceValueView(PublishedView):
    label: str
    value: str
    target: TargetView
    location: SourceLocationView | None = None


class IssueMessageCard(PublishedView):
    type: Literal["issue"]
    issue_id: UUID
    severity: Literal["blocking", "warning", "undetermined", "not_covered"]
    category: str
    rule_name: str
    a: SourceValueView
    b: SourceValueView | None = None
    pending_action: str
    difference: str | None = None


class KnowledgeSourceCard(PublishedView):
    type: Literal["knowledge_source"]
    source_id: str
    title: str
    url: str
    version: str
    scope: str


class ToolMessageCard(PublishedView):
    type: Literal["tool_call"]
    name: str
    version: int
    purpose: str
    status: Literal["running", "succeeded", "failed"]
    params_redacted: str
    error: str | None = None
    external_source: str | None = None


class MemoryMessageCard(PublishedView):
    type: Literal["memory_suggestion"]
    suggestion_id: UUID
    category: str
    content: str


class ChallengeMessageCard(PublishedView):
    type: Literal["mcp_challenge"]
    challenge_id: UUID


class ErrorMessageCard(PublishedView):
    type: Literal["system_error"]
    kind: Literal["not_sent", "interrupted", "stream_lost", "permission_changed"]
    text: str


class RunMessageCard(PublishedView):
    type: Literal["run_link"]
    run_id: UUID


class CompletionMessageCard(PublishedView):
    type: Literal["completion"]
    run_id: UUID | None = None
    snapshot_id: UUID | None = None
    ended: bool
    status_text: str


class ReadMessageCard(PublishedView):
    type: Literal["read_prompt"]
    text: str


MessageCardView = Annotated[EvidenceMessageCard | AdoptedMessageCard | TranslationMessageCard | IssueMessageCard | KnowledgeSourceCard | ToolMessageCard | MemoryMessageCard | ChallengeMessageCard | ErrorMessageCard | RunMessageCard | CompletionMessageCard | ReadMessageCard, Field(discriminator="type")]


class ContinuationView(PublishedView):
    status: Literal["pending", "waiting", "running", "awaiting_retry", "awaiting_reconcile", "completed", "failed", "cancelled", "restricted", "closed", "conversation_closed"]
    text: str
    next_action: str | None = None


class AttachmentView(PublishedView):
    case_id: UUID
    document_id: UUID
    document_version: int
    parse_artifact_id: UUID | None = None
    filename: str | None = None
    mime: str | None = None
    size_bytes: int | None = None
    uploaded_by: UUID | None = None
    uploaded_at: AwareDatetime | None = None


class MessageView(PublishedView):
    message_id: UUID
    seq: int | None = None
    turn_id: UUID
    parent_turn_id: UUID | None = None
    role: Literal["user", "assistant"]
    author: str
    created_at: AwareDatetime
    status: Literal["sending", "generating", "completed", "interrupted", "failed"]
    text: str
    references: list[ReferenceView]
    capabilities: list[SelectedCapabilityView]
    attachments: list[AttachmentView]
    cards: list[MessageCardView]
    continuation: ContinuationView | None = None
    restricted: bool | None = None


class ReferenceCandidateView(PublishedView):
    selection: ReferenceView
    group: Literal["Case", "Run", "原件", "成功快照", "区段"]
    title: str
    case_name: str
    time_label: str
    status_label: str
    version_label: str
    hit_source: str | None = None
    summary_state: str | None = None
    cross_case: bool
    selectable: bool
    not_selectable_reason: str | None = None
    children: list["ReferenceCandidateView"] | None = None


class McpChallengeView(PublishedView):
    challenge_id: UUID
    service_name: str
    destination: str
    tool: str
    purpose: str
    scope: list[str]
    data_level: str
    retains_result: bool
    expires_at: AwareDatetime
    status: Literal["pending", "approved", "rejected", "expired"]


class IssueCountsView(PublishedView):
    blocking: int
    warning: int
    undetermined: int
    not_covered: int | None = None
    all: int | None = None
    done: int | None = None


class ReviewSectionView(PublishedView):
    section_id: UUID
    title: str
    translated: bool


class LogicalDocumentView(PublishedView):
    logical_document_id: UUID
    doc_type: str
    cargo_profile_ids: list[str] = Field(default_factory=list)
    label: str
    page_range: str
    needs_confirmation: bool
    sections: list[ReviewSectionView]


class ReviewFileView(PublishedView):
    source: SourceView
    parse_artifact_id: UUID | None = None
    parse_status: Literal["parsed", "parsing", "failed", "not_parsed"]
    preview_id: UUID | None = None
    page_count: int
    issue_counts: IssueCountsView
    logical_documents: list[LogicalDocumentView]


class RunEventCursorView(PublishedView):
    run_id: UUID
    last_seq: int


class ReviewView(PublishedView):
    case_id: UUID
    case_name: str
    run_id: UUID | None = None
    snapshot_id: UUID | None = None
    mode: Literal["working", "historical", "source_only"]
    run_status: str | None = None
    target_language: LanguageCode | None = None
    business_revision_id: UUID | None = None
    business_revision_no: int | None = None
    translation_revision_id: UUID | None = None
    translation_revision_no: int | None = None
    review_state_version: int | None = None
    files: list[ReviewFileView]
    issue_counts: IssueCountsView
    gate_summary: str
    allowed_actions: list[str]
    disabled_reasons: dict[str, str]
    run_event_cursor: RunEventCursorView | None = None
    snapshot_exported_at: AwareDatetime | None = None


class FieldHistoryView(PublishedView):
    at: AwareDatetime
    by: str
    action: str
    value: str | None = None
    reason: str | None = None
    business_revision_id: UUID | None = None


class FieldIssueView(PublishedView):
    issue_id: UUID
    title: str
    severity: str
    resolved: bool
    blocks_confirmation: bool = False


class FieldView(PublishedView):
    field_id: UUID
    source_segment_ids: list[UUID] = Field(default_factory=list)
    group: Literal["parties", "logistics", "goods", "amounts"]
    name: str
    role: str | None = None
    document_id: UUID
    logical_document_id: UUID
    original_text: str
    normalized_value: str
    adopted_value: str | None = None
    unit: str | None = None
    review_status: Literal["unreviewed", "confirmed", "unknown", "revised", "candidate"]
    issue_id: UUID | None = None
    issues: list[FieldIssueView]
    confirm_blocked_reason: str | None = None
    confidence_note: str | None = None
    target: TargetView
    history: list[FieldHistoryView]


class DispositionView(PublishedView):
    by: str
    at: AwareDatetime
    reason: str
    basis: str | None = None
    value: str | None = None


class IssueActionView(PublishedView):
    action: Literal["revise-translation", "ignore", "request-revision", "apply-recommendation", "supplement-upload"]
    enabled: bool
    reason: str | None = None


class IssueGuidanceView(PublishedView):
    recommended_action: Literal["revise-translation", "ignore", "supplement-upload", "human-review"] | None = None
    agent_source_segment_ids: list[UUID] = Field(default_factory=list)
    skill_version: str
    category_label: str
    explanation: str
    impact: str
    recommendation: str
    replacement_text: str | None = None
    target_kind: Literal["translation","field","material"]
    can_apply: bool
    target: TargetView | None = None
    actions: list[IssueActionView] = Field(default_factory=list)
    agent_status: Literal["queued", "succeeded", "failed", "needs_reconcile"] | None = None
    agent_response: str | None = None
    agent_request_no: int = 0


class IssueRepairView(PublishedView):
    run_id: UUID
    issue_id: UUID
    status: str
    translation_revision_id: UUID | None = None
    message: str


class IssueView(PublishedView):
    issue_id: UUID
    severity: Literal["blocking", "warning", "undetermined", "not_covered"]
    category: str
    rule_name: str
    rule_version: str
    conclusion: str
    blocking_delivery: bool
    status: Literal["open", "accepted", "adjudicated", "unresolved", "not_applicable", "ignored", "revised"]
    a: SourceValueView | None = None
    b: SourceValueView | None = None
    difference: str | None = None
    disposition: DispositionView | None = None
    issue_version: int
    sources: list[SourceValueView] | None = None
    guidance: IssueGuidanceView | None = None


class IssuePageView(PageView[IssueView]):
    counts: IssueCountsView
    rules_status: Literal["run", "not_run"]


class ReadingSegmentView(PublishedView):
    source_segment_id: UUID
    section_id: UUID
    logical_document_id: UUID
    structure: str
    text: str
    table_row: int | None = None
    table_col: int | None = None
    page_no: int | None = None
    anchor_precision: Literal["rect", "page_only", "paragraph_only", "cell", "none"]
    bbox: tuple[float, float, float, float] | None = None


class ReadingView(PageView[ReadingSegmentView]):
    parse_artifact_id: UUID
    document_id: UUID
    document_version: int
    is_synthetic: bool
    quality: str
    quality_flags: list[str]


class LineDetailView(PublishedView):
    line_id: UUID
    label: str
    target: TargetView


class LineMatchView(PublishedView):
    left: str
    right: str
    state: str
    relation: str
    basis: str | None = None
    by: str | None = None
    at: AwareDatetime
    left_detail: LineDetailView | None = None
    right_detail: LineDetailView | None = None


class LineItemView(PublishedView):
    line_id: UUID
    line_no: int
    level: str
    hs_code: str | None = None
    unit_price: str | None = None
    currency: str | None = None
    package_count: str | None = None
    package_unit: str | None = None
    net_weight: str | None = None
    gross_weight: str | None = None
    weight_unit: str | None = None
    matches: list[LineMatchView]
    description: str
    quantity: str
    unit: str
    amount: str | None = None
    match_status: Literal["matched", "partial", "unmatched"]
    matched_with: str | None = None
    target: TargetView


class RuleSourceView(PublishedView):
    label: str
    target: TargetView


class RuleCheckView(PublishedView):
    check_id: UUID
    rule_name: str
    rule_version: str
    status: Literal["passed", "failed", "not_applicable", "not_covered", "undetermined", "not_run"]
    executed_at: AwareDatetime
    rule_pack_version: str
    condition: str
    inputs: str
    basis: str
    source_segment_ids: list[UUID]
    sources: list[RuleSourceView]


class ChecksPageView(PageView[RuleCheckView]):
    rules_status: Literal["run", "not_run"]


class TranslationSegmentView(PublishedView):
    translation_segment_id: UUID
    source_segment_id: UUID | None = None
    source_segment_ids: list[UUID]
    document_id: UUID
    section_id: UUID | None = None
    section_title: str
    align_group: str
    source_text: str
    translated_text: str
    qa_status: Literal["passed", "warning", "stale", "not_translated"]
    terms: list[str]
    revision_no: int
    target: TargetView


class TranslationsPageView(PageView[TranslationSegmentView]):
    not_applicable: bool
    reason: str | None = None


class PreviewRepresentationView(PublishedView):
    kind: Literal["reading_html", "draft_pdf"]
    mime: str
    size_bytes: int | None = None
    available: bool
    disabled_reason: str | None = None


class SegmentMappingView(PublishedView):
    source_segment_ids: list[UUID]
    translation_segment_id: UUID


class PreviewManifestView(PublishedView):
    preview_id: UUID
    run_id: UUID
    source_version: SourceView
    preview_version: int
    translation_scope: Literal["all", "goods_description", "selected_sections"]
    status: Literal["valid", "stale", "historical"]
    business_revision_id: UUID
    translation_revision_id: UUID
    compatibility_id: UUID | None = None
    target_language: str
    resolved_scope_id: UUID
    render_config_version: str
    selected_section_refs: list[TargetView]
    untranslated_section_refs: list[TargetView]
    segment_mappings: list[SegmentMappingView]
    representations: list[PreviewRepresentationView]
    allowed_actions: list[str]


class ChangeImpactView(PublishedView):
    affected_rules: list[str]
    affected_segments: int
    affected_previews: int
    affected_summaries: int
    affected_runs: list[UUID]
    cost_level: Literal["none", "low", "medium", "unknown"]
    conflict: str | None = None


class FieldCommandView(PublishedView):
    business_revision_no: int
    affected: ChangeImpactView | None = None


class RunAcceptedView(PublishedView):
    accepted: Literal[True]
    run_id: UUID
    execution_epoch: int


class LineMatchCommandView(PublishedView):
    line_match_id: UUID
    line_match_ids: list[UUID]
    line_match_group_id: UUID
    business_revision_no: int
    business_revision_id: UUID
    affected_run_ids: list[UUID]
    review_state_version: int


class ChecklistGapView(PublishedView):
    item: str
    detail: str
    target: TargetView | None = None


class ChecklistItemView(PublishedView):
    label: str
    state: Literal["ok", "gap", "note"]
    detail: str


class ReviewChecklistView(PublishedView):
    run_id: UUID
    confirmable: bool
    review_fingerprint: str
    output_formats: list[str]
    source_versions: list[SourceView]
    pairing_label: str
    gaps: list[ChecklistGapView]
    items: list[ChecklistItemView]
    not_covered_note: str
    current_review_confirmation_id: UUID | None = None
    confirmation_status: Literal["none", "valid", "stale"]


class PairingView(PublishedView):
    document_id: UUID
    document_version: int
    parse_artifact_id: UUID
    resolved_scope_id: UUID | None = None
    resolved_scope_fingerprint: str | None = None
    translation_revision_id: UUID | None = None
    compatibility_id: UUID | None = None
    preview_id: UUID | None = None
    preview_version: int | None = None


class ConfirmationView(PublishedView):
    confirmation_id: UUID
    run_id: UUID
    task_spec_hash: str
    review_fingerprint: str
    source_versions: list[SourceView]
    business_revision_id: UUID
    translation_revision_id: UUID
    resolved_scope_refs: list[PairingView]
    output_formats: list[str]
    confirmed_by: UUID
    confirmed_at: AwareDatetime
    valid_for_current_working: bool


class ExportStepView(PublishedView):
    step_id: UUID
    label: str
    kind: Literal["docx", "pdf", "source_pdf", "report"]
    source_filename: str | None = None
    status: Literal["pending", "rendering", "checking", "succeeded", "failed"]


class ExportAttemptView(PublishedView):
    attempt_id: UUID
    run_id: UUID
    confirmation_id: UUID
    review_fingerprint: str
    status: Literal["queued", "rendering", "checking", "failed_retryable", "failed_final", "succeeded", "paused_reconcile"]
    artifact_steps: list[ExportStepView]
    error_summary: str | None = None
    retryable_step_ids: list[UUID]
    snapshot_id: UUID | None = None
    updated_at: AwareDatetime


class ExportArtifactView(PublishedView):
    artifact_id: UUID
    kind: Literal["docx", "pdf", "source_pdf", "report"]
    filename: str
    size_bytes: int


class SourceArtifactsView(PublishedView):
    source: SourceView
    artifacts: list[ExportArtifactView]
    untranslated_note: str | None = None


class SnapshotView(PublishedView):
    snapshot_id: UUID
    run_id: UUID
    case_id: UUID
    case_name: str
    target_language: str
    translation_scope: str
    exported_at: AwareDatetime
    exported_by: str
    business_revision_no: int
    translation_revision_no: int
    confirmation_id: UUID
    rule_pack_version: str | None = None
    jurisdiction_inputs: JurisdictionInputs | None = None
    source_artifacts: list[SourceArtifactsView]
    case_report_artifact: ExportArtifactView | None = None
    open_warnings: list[str]


class MatchedSourceView(PublishedView):
    condition: str
    filename: str
    document_version: int
    logical_document: str
    field_role: str | None = None
    state: Literal["candidate", "confirmed"]
    target: TargetView


class ManagedRunView(PublishedView):
    run_id: UUID
    case_id: UUID
    case_name: str
    case_short_code: str
    workflow_type: str
    target_language: LanguageCode | None = None
    source_count: int
    source_types: list[str]
    initiated_by: str
    initiated_at: AwareDatetime
    reviewed_by: str | None = None
    exported_by: str | None = None
    current_work_status: str
    current_stage: str | None = None
    has_successful_snapshot: bool
    displayed_snapshot_id: UUID | None = None
    displayed_snapshot_at: AwareDatetime | None = None
    displayed_snapshot_is_latest: bool | None = None
    working_version_differs: bool | None = None
    latest_export_attempt_id: UUID | None = None
    latest_export_attempt_status: str | None = None
    latest_export_attempt_error_summary: str | None = None
    issue_counts: IssueCountsView
    rules_status: Literal["run", "not_run"]
    matched_sources: list[MatchedSourceView]
    summary_status: Literal["ready", "stale", "failed"]
    last_activity_at: AwareDatetime
    allowed_actions: list[str]
    disabled_reasons: dict[str, str]
    ops_reconcile_step_id: UUID | None = None
    awaiting_external_reconcile: bool | None = None


class RunFacetsView(PublishedView):
    status_counts: dict[str, int]
    languages: list[str]
    timezone: str


class SearchSuggestionView(PublishedView):
    run_id: UUID
    case_id: UUID
    title: str
    language: str | None = None
    status: str
    snapshot_id: UUID | None = None
    activity_at: AwareDatetime
    match_explanation: str
    matched_sources: list[MatchedSourceView]


class ActorView(PublishedView):
    id: UUID
    name: str


class SummarySectionView(PublishedView):
    section_id: UUID
    title: str
    page_range: str
    target: TargetView


class SummaryLogicalView(PublishedView):
    generated_at: AwareDatetime
    state: Literal["ready", "stale", "failed"]
    dependency_fingerprint: str
    logical_document_id: UUID
    label: str
    doc_type: str
    summary: str
    target: TargetView
    page_range: str
    sections: list[SummarySectionView]


class SummaryDocumentView(PublishedView):
    source: SourceView
    generated_at: AwareDatetime
    dependency_fingerprint: str
    summary_scope: Literal["snapshot_document", "run_document"]
    parse_artifact_id: UUID | None = None
    short_summary: str
    state: Literal["ready", "stale", "failed"]
    logical_documents: list[SummaryLogicalView]
    source_target: TargetView


class SummaryIssueView(PublishedView):
    issue_id: UUID
    severity: Literal["blocking", "warning", "undetermined", "not_covered"]
    title: str
    disposition: str
    target: TargetView


class SummarySnapshotView(PublishedView):
    summary: str
    generated_at: AwareDatetime
    state: Literal["ready", "stale", "failed"]
    dependency_fingerprint: str
    snapshot_id: UUID
    exported_at: AwareDatetime
    exported_by: str
    is_latest: bool


class RunSummaryView(PublishedView):
    run_id: UUID
    generated_at: AwareDatetime
    dependency_fingerprint: str
    dependencies: dict[str, JsonValue]
    state: Literal["ready", "stale", "failed"]
    summary: str
    documents: list[SummaryDocumentView]
    issues: list[SummaryIssueView]
    snapshots: list[SummarySnapshotView]


class CaseView(PublishedView):
    case_id: UUID
    case_name: str
    short_code: str
    owner: str
    case_version: int
    retention_expires_at: AwareDatetime
    last_material_event_at: AwareDatetime
    current_business_revision_id: UUID | None = None
    business_revision_no: int | None = None
    allowed_actions: list[str]


class ParseLogicalView(PublishedView):
    summary: str
    generated_at: AwareDatetime
    state: Literal["ready", "stale", "failed"]
    dependency_fingerprint: str
    logical_document_id: UUID
    title: str
    page_range: str
    sections: list[SummarySectionView]


class ParseHistoryView(PublishedView):
    summary: str
    generated_at: AwareDatetime
    state: Literal["ready", "stale", "failed"]
    dependency_fingerprint: str
    parse_artifact_id: UUID
    created_at: AwareDatetime
    quality: str
    page_count: int | None = None
    parser_name: str
    parser_version: str
    target: TargetView
    logical_documents: list[ParseLogicalView]


class SourceHistoryView(PublishedView):
    summary: str
    generated_at: AwareDatetime
    state: Literal["ready", "stale", "failed"]
    dependency_fingerprint: str
    source: SourceView
    parses: list[ParseHistoryView]


class OverviewRunView(PublishedView):
    run_id: UUID
    workflow_type: str
    status: str
    initiated_at: AwareDatetime
    target_language: LanguageCode | None = None


class OverviewSnapshotView(PublishedView):
    snapshot_id: UUID
    run_id: UUID
    exported_at: AwareDatetime
    business_revision_no: int | None = None
    translation_revision_no: int | None = None
    target_language: LanguageCode | None = None


class CaseOverviewView(PublishedView):
    summary: str
    generated_at: AwareDatetime
    state: Literal["ready", "stale", "failed"]
    dependency_fingerprint: str
    source_history: list[SourceHistoryView]
    case: CaseView
    sources: list[SourceView]
    runs: list[OverviewRunView]
    issues: IssueCountsView
    snapshots: list[OverviewSnapshotView]
    last_material_event_at: AwareDatetime


class TimelineEventView(PublishedView):
    seq: int
    occurred_at: AwareDatetime
    kind: Literal["process", "human", "failure", "export", "ops"]
    actor: str
    message: str
    ops_step_id: UUID | None = None


class DeletionPreviewView(PublishedView):
    case_id: UUID
    case_version: int
    short_code: str
    retention_expires_at: AwareDatetime
    source_count: int
    run_count: int
    snapshot_count: int
    allowed: bool
    disabled_reason: str | None = None


class DeletionView(PublishedView):
    deletion_id: UUID
    status: Literal["queued", "purging", "completed", "failed"]
    requested_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    last_error_class: str | None = None


class CycleMetricView(PublishedView):
    category: Literal["publish", "recovery", "retention", "monitor"]
    active: bool
    last_success_age_seconds: float | None = None
    successes: int
    failures: int
    last_duration_seconds: float


class WorkerMetricView(PublishedView):
    queue: Literal["documents", "conversation", "maintenance"]
    active: bool | None
    state: Literal["active", "inactive", "unknown"]


class OperationalCountersView(PublishedView):
    outbox_pending: int
    outbox_failed: int
    outbox_due_count: int
    outbox_oldest_age_seconds: float
    run_paused_reconcile: int
    step_retryable_failures: int
    turn_expired_leases: int
    deletion_overdue_24h: int


class OperationalAlertView(PublishedView):
    code: Literal["CYCLE_STALE", "WORKER_UNAVAILABLE", "OUTBOX_DELAY", "DELETION_SLA"]
    component: str
    action: str


class ModelMeteringView(PublishedView):
    window_hours: Literal[24]
    bounded_per_tenant: Literal[True]
    model_call_count: int
    model_tokens: int
    missing_usage_count: int
    known_model_cost_cny: str
    unknown_cost_count: int
    model_duration_p50_ms: int | None
    model_duration_p95_ms: int | None
    cost_scope: Literal["model_only"]
    ocr_cost_known: Literal[False]


class OperationalMetricsView(PublishedView):
    status: Literal["attention", "ok"]
    probed_at: AwareDatetime
    cycles: list[CycleMetricView]
    workers: list[WorkerMetricView]
    metrics: OperationalCountersView
    model_metering: ModelMeteringView
    alerts: list[OperationalAlertView]
    scope: Literal["current_tenant", "deployment_aggregate"]


class TurnEventView(PublishedView):
    preview_id: UUID | None = None
    revision: int | None = None
    offset: int | None = None
    verification: Literal["pending"] | None = None
    reason: str | None = None
    seq: int
    occurred_at: AwareDatetime
    event_type: Literal["answer_preview_started", "answer_preview_delta", "answer_preview_reset", "tool_started", "tool_finished", "message_delta", "evidence_card", "message_final", "message_error", "continuation_pending", "continuation_status", "continuation_final"]
    message_id: UUID | None = None
    status: str | None = None
    stage: str | None = None
    message: str | MessageView | None = None
    text: str | None = None
    name: str | None = None
    cards: list[EvidenceCardView] | None = None
    run_id: UUID | None = None
    continuation_id: UUID | None = None
    next_action: str | None = None
    error_class: str | None = None


class TurnEventsView(PublishedView):
    items: list[TurnEventView]
    last_seq: int


class LineBasisView(PublishedView):
    same_goods: bool
    complete: bool
    scope_key: str
    role: str
    packaging_level: str


class RunEventPayloadView(PublishedView):
    issue_id: UUID | None = None
    agent_request_no: int | None = None
    business_revision_id: UUID | None = None
    batch_id: UUID | None = None
    scope_revision_id: UUID | None = None
    recompute_run_id: UUID | None = None
    previous_comparison_binding_id: UUID | None = None
    required_action: Literal["new_run"] | None = None
    red: int | None = None
    yellow: int | None = None
    unknown: int | None = None
    files: int | None = None
    checks: int | None = None
    pending_relations: int | None = None
    rules_status: str | None = None
    translation_revision_id: UUID | None = None
    item_id: UUID | None = None
    snapshot_id: UUID | None = None
    reason: str | None = None
    parse_artifact_ids: list[UUID] | None = None
    input_request_id: UUID | None = None
    step_execution_id: UUID | None = Field(default=None, validation_alias=AliasChoices("step_execution_id", "step_id"))
    decision: str | None = None
    reparse_document_id: UUID | None = None
    only_quality_preview: bool | None = None
    attempt_id: UUID | None = None
    line_match_group_id: UUID | None = None
    line_match_ids: list[UUID] | None = None
    left_line_ids: list[UUID] | None = None
    right_line_ids: list[UUID] | None = None
    rejected_edge_ids: list[UUID] | None = None
    relation: str | None = None
    basis: LineBasisView | None = None


class RunEventView(PublishedView):
    seq: int
    occurred_at: AwareDatetime
    status: str
    stage: str | None = None
    message: str
    event_type: str
    kind: str
    completed: int | None = None
    total: int | None = None
    unit: str | None = None
    payload: RunEventPayloadView | None = None


class RunEventsView(PublishedView):
    items: list[RunEventView]
    last_seq: int
    next_cursor: int | None = None


class InputOptionView(PublishedView):
    option_id: str
    label: str
    detail: str | None = None
    source_ref: TargetView | None = None
    excerpt: str | None = None


class InputSourceView(PublishedView):
    section_id: UUID | None = None
    source_segment_id: UUID | None = None
    document_id: UUID | None = None
    document_version: int | None = None
    parse_artifact_id: UUID | None = None
    case_id: UUID | None = None


class InputRequestView(PublishedView):
    input_request_id: UUID
    run_id: UUID
    kind: Literal["document_boundary", "source_language", "translation_scope"]
    schema_version: int
    request_version: int
    source_refs: list[InputSourceView]
    question: str
    options: list[InputOptionView]
    suggested_value: str | None = None
    expected_parse_artifact_id: UUID
    expected_business_revision_id: UUID | None = None
    status: Literal["active", "resolved", "superseded", "cancelled"]
    created_at: AwareDatetime


class OpsStepView(PublishedView):
    step_execution_id: UUID
    run_id: UUID
    step_name: str
    status: str
    request_key_summary: str
    external_task_summary: str
    step_version: int
    submitted_at: AwareDatetime
    allowed_decisions: list[Literal["verified_executed", "confirmed_not_executed", "still_unknown", "terminate_run", "known_response_local_failure"]]


class BindingSummaryView(PublishedView):
    role: Literal["workflow"]
    fingerprint: str
    label: str


class MigrationOptionView(PublishedView):
    fingerprint: str
    label: str
    capability_check: str
    invalidated_steps: list[str]
    cost_level: str
    disabled_reason: str | None = None


class MigrationOptionsView(PublishedView):
    run_id: UUID
    execution_epoch: int
    current: BindingSummaryView
    options: list[MigrationOptionView]
    safe_now: bool
    unsafe_reason: str | None = None


class MigrationAcceptedView(PublishedView):
    accepted: Literal[True]
    execution_epoch: int


class ComponentHealthView(PublishedView):
    ok: bool
    reason: str | None = None
    migration_current: bool | None = None
    public_registry_ready: bool | None = None
    credential_store_ready: bool | None = None
    status: int | None = None
    inference_probed: bool | None = None
    paid_parse_probed: bool | None = None
    enforced_limits: list[Literal["calls", "tokens", "cost_cny"]] | None = None
    cost_cny_enforced: bool | None = None
    cost_scope: Literal["model_only"] | None = None
    ocr_cost_known: bool | None = None


class HealthComponentsView(PublishedView):
    postgres: ComponentHealthView
    checkpointer: ComponentHealthView
    prompts: ComponentHealthView
    mcp_templates: ComponentHealthView
    redis: ComponentHealthView
    object_store: ComponentHealthView
    chat: ComponentHealthView
    extract: ComponentHealthView
    translate: ComponentHealthView
    vision: ComponentHealthView
    ocr: ComponentHealthView
    model_budget: ComponentHealthView


class HealthReadyView(PublishedView):
    status: Literal["ready", "degraded"]
    probed_at: AwareDatetime
    paid_requests: Literal[0]


class OpsHealthView(HealthReadyView):
    components: HealthComponentsView


class LiveView(PublishedView):
    status: Literal["ok"]
    phase: Literal["runtime"]


from app.review.rule_inputs import PartyMappingInput, PackingTableInput, TransportDateInput


class PartyMappingResultView(PartyMappingInput):
    field_assertion_ids: list[UUID]
    source_segment_ids: list[UUID]


class PackingTableResultView(PackingTableInput):
    parse_artifact_id: UUID
    source_segment_ids: list[UUID]


class TransportDateResultView(TransportDateInput):
    field_assertion_ids: list[UUID]
    source_segment_ids: list[UUID]


class RuleInputDecisionView(PublishedView):
    decision_id: UUID
    kind: Literal["party_mapping", "packing_table", "date_transport"]
    scope_key: str
    payload: PartyMappingResultView | PackingTableResultView | TransportDateResultView
    basis: str
    reason: str
    decided_by: UUID
    created_at: AwareDatetime
    business_revision_id: UUID


class RuleInputsView(PublishedView):
    business_revision_id: UUID
    review_state_version: int
    items: list[RuleInputDecisionView]


class RuleInputAcceptedView(PublishedView):
    decision_id: UUID
    business_revision_id: UUID
    business_revision_no: int
    review_state_version: int
    affected_runs: list[UUID]
