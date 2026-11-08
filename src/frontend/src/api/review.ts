import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";
import type { ConfirmBody, ExportBody, RetryBody } from "./generated/request-contracts";
import type {
  ChangeImpact,
  EvidenceResolution,
  EvidenceTarget,
  ExportAttemptView,
  ExportSnapshotView,
  FieldView,
  IssueView,
  LineItemView,
  Page,
  PreviewManifest,
  ReviewChecklist,
  ReviewView,
  RuleCheckView,
  TranslationSegmentView,
} from "./types";

export const reviewApi = {
  resolve: (target: EvidenceTarget) => api<ApiResponses["POST /api/v1/evidence/resolve"]["200"]>("/evidence/resolve", { method: "POST", body: target }),
  view: (caseId: string, params: { run_id?: string; snapshot_id?: string; document_id?: string; parse_artifact_id?: string; business_revision_id?: string }) =>
    api<ApiResponses["GET /api/v1/cases/{case_id}/review-view"]["200"]>(`/cases/${caseId}/review-view`, { query: params }),
  fields: (caseId: string, params: Record<string, string | number | null | undefined>) => api<ApiResponses["GET /api/v1/cases/{case_id}/fields"]["200"]>(`/cases/${caseId}/fields`, { query: params }),
  issues: (caseId: string, params: Record<string, string | number | null | undefined>) => api<ApiResponses["GET /api/v1/cases/{case_id}/issues"]["200"]>(`/cases/${caseId}/issues`, { query: params }),
  lineItems: (caseId: string, params: Record<string, string | number | null | undefined>) => api<ApiResponses["GET /api/v1/cases/{case_id}/line-items"]["200"]>(`/cases/${caseId}/line-items`, { query: params }),
  confirmLineMatch: (caseId: string, body: Record<string, unknown>, key: string) => api<ApiResponses["POST /api/v1/cases/{case_id}/line-matches"]["200"]>(`/cases/${caseId}/line-matches`, { method: "POST", body, idempotencyKey: key }),
  checks: (runId: string, params: Record<string, string | number | null | undefined>) => api<ApiResponses["GET /api/v1/runs/{run_id}/checks"]["200"]>(`/runs/${runId}/checks`, { query: params }),
  translations: (runId: string, params: Record<string, string | number | null | undefined>) =>
    api<ApiResponses["GET /api/v1/runs/{run_id}/translations"]["200"]>(`/runs/${runId}/translations`, { query: params }),
  previewManifest: (previewId: string) => api<ApiResponses["GET /api/v1/previews/{preview_id}/manifest"]["200"]>(`/previews/${previewId}/manifest`),
  changeImpact: (caseId: string, body: Record<string, unknown>) => api<ApiResponses["POST /api/v1/cases/{case_id}/change-impact/preview"]["200"]>(`/cases/${caseId}/change-impact/preview`, { method: "POST", body }),
  fieldAction: (caseId: string, fieldId: string, action: "confirm" | "mark-unknown" | "revise", body: Record<string, unknown>, key: string) =>
    api<ApiResponses["POST /api/v1/cases/{case_id}/fields/{field_id}/confirm"]["200"] | ApiResponses["POST /api/v1/cases/{case_id}/fields/{field_id}/mark-unknown"]["200"] | ApiResponses["POST /api/v1/cases/{case_id}/fields/{field_id}/revise"]["200"]>(`/cases/${caseId}/fields/${fieldId}/${action}`, { method: "POST", body, idempotencyKey: key }),
  sectionAction: (caseId: string, sectionId: string, action: "assign" | "confirm-language", body: Record<string, unknown>, key: string) =>
    api<ApiResponses["POST /api/v1/cases/{case_id}/sections/{section_id}/assign"]["200"] | ApiResponses["POST /api/v1/cases/{case_id}/sections/{section_id}/confirm-language"]["200"]>(`/cases/${caseId}/sections/${sectionId}/${action}`, { method: "POST", body, idempotencyKey: key }),
  repairIssue: (caseId: string, issueId: string, action: "apply-recommendation" | "request-revision", body: Record<string, unknown>, key: string) =>
    api<ApiResponses["POST /api/v1/cases/{case_id}/issues/{issue_id}/request-revision"]["202"]>(`/cases/${caseId}/issues/${issueId}/${action}`, { method: "POST", body, idempotencyKey: key }),
  issueAction: (caseId: string, issueId: string, action: "accept" | "adjudicate" | "mark-unresolved" | "not-applicable" | "ignore", body: Record<string, unknown>, key: string) =>
    api<ApiResponses["POST /api/v1/cases/{case_id}/issues/{issue_id}/accept"]["200"] | ApiResponses["POST /api/v1/cases/{case_id}/issues/{issue_id}/adjudicate"]["200"] | ApiResponses["POST /api/v1/cases/{case_id}/issues/{issue_id}/mark-unresolved"]["200"] | ApiResponses["POST /api/v1/cases/{case_id}/issues/{issue_id}/not-applicable"]["200"]>(`/cases/${caseId}/issues/${issueId}/${action}`, { method: "POST", body, idempotencyKey: key }),
  reparse: (documentId: string, version: number, body: Record<string, unknown>, key: string) =>
    api<ApiResponses["POST /api/v1/documents/{document_id}/versions/{version}/reparse"]["200"]>(`/documents/${documentId}/versions/${version}/reparse`, { method: "POST", body, idempotencyKey: key }),
  reviseSegment: (runId: string, segId: string, body: Record<string, unknown>, key: string) =>
    api<ApiResponses["POST /api/v1/runs/{run_id}/translation-segments/{segment_id}/revise"]["200"]>(`/runs/${runId}/translation-segments/${segId}/revise`, { method: "POST", body, idempotencyKey: key }),
  checklist: (runId: string) => api<ApiResponses["GET /api/v1/runs/{run_id}/review-checklist"]["200"]>(`/runs/${runId}/review-checklist`),
  confirm: (runId: string, body: ConfirmBody, key: string) =>
    api<ApiResponses["POST /api/v1/runs/{run_id}/review-confirmations"]["200"]>(`/runs/${runId}/review-confirmations`, { method: "POST", body, idempotencyKey: key }),
  startExport: (runId: string, body: ExportBody, key: string) =>
    api<ApiResponses["POST /api/v1/runs/{run_id}/exports"]["200"]>(`/runs/${runId}/exports`, { method: "POST", body, idempotencyKey: key }),
  exportAttempt: (runId: string, attemptId: string) => api<ApiResponses["GET /api/v1/runs/{run_id}/export-attempts/{attempt_id}"]["200"]>(`/runs/${runId}/export-attempts/${attemptId}`),
  retryExport: (runId: string, attemptId: string, body: RetryBody, key: string) =>
    api<ApiResponses["POST /api/v1/runs/{run_id}/export-attempts/{attempt_id}/retry"]["200"]>(`/runs/${runId}/export-attempts/${attemptId}/retry`, { method: "POST", body, idempotencyKey: key }),
  snapshots: (runId: string) => api<ApiResponses["GET /api/v1/runs/{run_id}/snapshots"]["200"]>(`/runs/${runId}/snapshots`),
  snapshot: (id: string) => api<ApiResponses["GET /api/v1/snapshots/{snapshot_id}"]["200"]>(`/snapshots/${id}`),
};

/** 字节接口一律走同源 API 代理，每次请求重新鉴权。 */
export const contentUrls = {
  document: (documentId: string, version: number) => `/api/v1/documents/${documentId}/versions/${version}/content`,
  preview: (previewId: string, representation: "reading_html" | "draft_pdf", mode = "translation") => `/api/v1/previews/${previewId}/content?representation=${representation}&view_mode=${mode}`,
  artifact: (artifactId: string) => `/api/v1/export-artifacts/${artifactId}/content`,
};
