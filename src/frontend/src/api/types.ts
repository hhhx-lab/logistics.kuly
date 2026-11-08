import type * as Published from "./generated/request-contracts";
// 前后端契约 DTO。字段名与《前后端接口与状态契约》保持一致；
// 所有业务成功响应使用实际 OpenAPI 生成类型；UI 状态仅补本地交互字段。
import type { WorkflowView as PublishedWorkflowView, TaskDraftView as PublishedTaskDraftView, RunView as PublishedRunView, WorkflowSourceVersion, SelectedSectionRef as PublishedSelectedSectionRef } from "./generated/request-contracts";

export type UUID = string;
export type ISODate = string;
export type Decimal = string;

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
  as_of: ISODate;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
    trace_id?: string;
    current?: Record<string, unknown>;
  };
}

// ---- 身份与系统 ----
export type IdentityView = Published.IdentityView;

export type DisclosureService = Published.DisclosureService;

export type DisclosureView = Published.DisclosureView;

// ---- 工作流 ----
export type WorkflowType = PublishedWorkflowView["workflow_type"];

export type WorkflowView = PublishedWorkflowView;

export type SourceVersion = WorkflowSourceVersion & { parse_artifact_id?: UUID | null };

export type SelectedSectionRef = PublishedSelectedSectionRef;

export type TargetLanguage = NonNullable<Published.TargetView["target_language"]>;
export type TranslationScope = "all" | "goods_description" | "selected_sections";

export type JurisdictionInputs = Published.ConsumerMaterialInputs | Published.CertificateInputs;

export type DocumentTypeInput = Required<Published.DocumentTypeInput>;

export interface TranslateDraftParameters {
  target_language: TargetLanguage | null;
  translation_scope: TranslationScope;
  selected_section_refs: SelectedSectionRef[];
  scope_selector_version?: string;
  output_formats: ("docx" | "pdf")[];
  glossary_version: string | null;
  rule_pack_version: string;
  jurisdiction_inputs?: JurisdictionInputs | null;
  document_type_inputs?: DocumentTypeInput[];
}

export interface ReadDraftParameters {
  index_scope: "selected_documents" | "case";
  document_type_inputs?: DocumentTypeInput[];
}

export type TaskDraftView = PublishedTaskDraftView;

export type RunStatus =
  | "queued"
  | "processing"
  | "awaiting_input"
  | "indexed"
  | "checked"
  | "awaiting_review"
  | "ready_to_export"
  | "exporting"
  | "exported"
  | "paused_reconcile"
  | "failed_retryable"
  | "failed_final"
  | "cancelled";

export type RunView = PublishedRunView;

export interface RunEventData {
  seq: number;
  occurred_at: ISODate;
  status: string;
  stage: string;
  message: string;
  completed?: number;
  total?: number;
  unit?: string;
  file?: string;
}

export type InputRequestOption = Published.InputOptionView;

export type InputRequestView = Published.InputRequestView;

// ---- 上传 ----
export type UploadFileStatus = "pending" | "uploading" | "received" | "verified" | "failed" | "cancelled";

export type UploadFileView = Published.UploadFileView;

export type UploadSessionView = Published.UploadSessionView;

export type CaseView = Published.app__api__routers__uploads__CaseView;

// ---- 对话 ----
export type ReferenceSelection = Published.ReferenceView & { detail?: string };

export interface CapabilitySelection {
  kind: "builtin_skill" | "user_skill" | "mcp_tool";
  extension_id: UUID;
  extension_version: number;
  tool_id?: string | null;
  tool_schema_hash?: string | null;
  user_parameters?: Record<string, string | boolean>;
  label: string;
  unavailable_reason?: string | null;
}

export type ConversationSummary = Published.ConversationView;

export type ConversationView = Published.ConversationView;

export type EvidenceTarget = Published.TargetView;

export type EvidenceCardData = Published.EvidenceCardView;

export type MessageCard = Published.MessageView["cards"][number];

export type MessageView = Published.MessageView & { historical_version_notice?: string | null };

export type TurnView = Published.TurnView;

export type ContextView = Published.ContextView;

export type CapabilityCandidate = Published.CapabilityView;

export type ReferenceCandidate = Published.ReferenceCandidateView;

export type McpChallengeView = Published.McpChallengeView;

// ---- 复核 ----
export type EvidenceResolution = Published.EvidenceResolution;

export type LogicalDocNode = Published.LogicalDocumentView;

export type ReviewFileNode = Published.ReviewFileView;

export type ReviewView = Published.ReviewView;

export type FieldView = Published.FieldView;

export type IssueView = Published.IssueView;

export type LineItemView = Published.LineItemView;

export type RuleCheckView = Published.RuleCheckView;

export type TranslationSegmentView = Published.TranslationSegmentView;

export type PreviewManifest = Published.PreviewManifestView;

export type ChangeImpact = Published.ChangeImpactView;

export type ReviewChecklist = Published.ReviewChecklistView;

export type ExportAttemptStatus = "queued" | "rendering" | "checking" | "failed_retryable" | "failed_final" | "succeeded" | "paused_reconcile";

export type ExportAttemptView = Published.ExportAttemptView;

export type ExportArtifact = Published.ExportArtifactView;

export type ExportSnapshotView = Published.SnapshotView;

// ---- 管理台 ----
export type MatchedSource = Published.MatchedSourceView;

export type ManagedRunRow = Published.ManagedRunView;

export type RunFacets = Published.RunFacetsView;

export type RunSummaryView = Published.RunSummaryView;

export type TimelineEvent = Published.TimelineEventView;

export type CaseOverview = Published.CaseOverviewView;

export type DeletionPreview = Published.DeletionPreviewView;

export type CaseDeletionView = Published.DeletionView;

export type OpsStepView = Published.OpsStepView;

export type ModelMigrationOptions = Published.MigrationOptionsView;

// ---- 扩展 ----
export type ExtensionStatus = "draft" | "validating" | "available" | "failed" | "disabled" | "archived";

export type SkillParam = Published.SkillParameterView;

/** 校验错误绑定字段键，界面在对应输入下就地显示并聚焦首个错误。 */
export type SkillFieldError = Published.FieldErrorView;

/** 可注册业务动作候选由服务端下发，前端不自行维护白名单。 */
export type SkillActionOption = Published.SkillActionView;

export type SkillVersion = Published.SkillVersionView;

/** 工具权限类别：只读、写入、外发。首版仅 read_only 可授权。 */
export type McpToolCategory = "read_only" | "write" | "egress";

export type McpTool = Published.McpToolView;

export type McpTestStage = Published.McpTestStageView;

export type McpVersion = Published.McpVersionView;

/** 部署能力：支持的认证方式与个人凭证存储是否就绪，由服务端下发。 */
export type McpTemplateCapability = Published.McpTemplateView;

/** 行级派生状态：区分「已校验待启用」「待测试」「待复核」等启用意图。 */
export type ExtensionRowState =
  | "enabled"
  | "ready_to_enable"
  | "draft"
  | "validating"
  | "disabled"
  | "archived"
  | "validation_failed"
  | "connect_failed"
  | "needs_retest"
  | "needs_recheck"
  | "syncing";

export type ExtensionRow = Published.ExtensionRowView;

export type ExtensionCall = Published.ExtensionCallView;

export type ImportDiffRow = Published.ImportDiffView;

// ---- 设置 ----
export type PreferencesView = Published.PreferencesView;

export type MemoryCategory = "answer_language" | "target_language" | "presentation" | "terminology";

/** 时区候选由服务端下发，避免界面与服务端对同一 IANA ID 的解释不一致。 */
export type TimezoneOption = Published.TimezoneView;

export type MemoryView = Published.MemoryView;

export type MemorySuggestion = Published.MemorySuggestionView;

export type MemoryPreview = Published.MemoryPreviewView;
