import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";

export type RunQuery = Record<string, string | string[] | boolean | undefined>;

export const managementApi = {
  suggestions: (q: RunQuery) => api<ApiResponses["GET /api/v1/runs/search-suggestions"]["200"]>("/runs/search-suggestions", { query: q }),
  actors: () => api<ApiResponses["GET /api/v1/runs/actors"]["200"]>("/runs/actors"),
  runs: (q: RunQuery, signal?: AbortSignal) => api<ApiResponses["GET /api/v1/runs"]["200"]>("/runs", { query: q, signal }),
  facets: (q: RunQuery) => api<ApiResponses["GET /api/v1/runs/facets"]["200"]>("/runs/facets", { query: q }),
  overview: (caseId: string) => api<ApiResponses["GET /api/v1/cases/{case_id}/overview"]["200"]>(`/cases/${caseId}/overview`),
  summary: (runId: string, snapshotId?: string | null) => api<ApiResponses["GET /api/v1/runs/{run_id}/summary"]["200"]>(`/runs/${runId}/summary`, { query: { snapshot_id: snapshotId ?? undefined } }),
  timeline: (runId: string, cursor?: string) => api<ApiResponses["GET /api/v1/runs/{run_id}/timeline"]["200"]>(`/runs/${runId}/timeline`, { query: { cursor } }),
  deletionPreview: (caseId: string) => api<ApiResponses["GET /api/v1/cases/{case_id}/deletion-preview"]["200"]>(`/cases/${caseId}/deletion-preview`),
  deleteCase: (caseId: string, body: { expected_case_version: number; confirm_short_code: string }, key: string) =>
    api<ApiResponses["POST /api/v1/cases/{case_id}/delete"]["202"]>(`/cases/${caseId}/delete`, { method: "POST", body, idempotencyKey: key }),
  deletion: (id: string) => api<ApiResponses["GET /api/v1/case-deletions/{deletion_id}"]["200"]>(`/case-deletions/${id}`),
};
