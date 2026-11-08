import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";
import type { ApiRequests, PrepareBody, PatchBody } from "./generated/request-contracts";

export const workflowApi = {
  workflows: () => api<ApiResponses["GET /api/v1/workflows"]["200"]>("/workflows"),
  createDraft: (body: PrepareBody, key: string) => api<ApiResponses["POST /api/v1/task-drafts"]["200"]>("/task-drafts", { method: "POST", body, idempotencyKey: key }),
  getDraft: (id: string) => api<ApiResponses["GET /api/v1/task-drafts/{draft_id}"]["200"]>(`/task-drafts/${id}`),
  patchDraft: (id: string, body: Omit<PatchBody, "expected_draft_version">, expected_draft_version: number, key: string) =>
    api<ApiResponses["PATCH /api/v1/task-drafts/{draft_id}"]["200"]>(`/task-drafts/${id}`, { method: "PATCH", body: { ...body, expected_draft_version }, idempotencyKey: key }),
  deleteDraft: (id: string, expected_draft_version: number, key: string) =>
    api<void>(`/task-drafts/${id}`, { method: "DELETE", body: { expected_draft_version }, idempotencyKey: key }),
  startRun: (draft_task_id: string, expected_draft_version: number, key: string) =>
    api<ApiResponses["POST /api/v1/runs"]["200"]>("/runs", { method: "POST", body: { draft_task_id, expected_draft_version } satisfies ApiRequests["POST /api/v1/runs"]["body"], idempotencyKey: key }),
  run: (id: string) => api<ApiResponses["GET /api/v1/runs/{run_id}"]["200"]>(`/runs/${id}`),
  inputRequests: (runId: string) => api<ApiResponses["GET /api/v1/runs/{run_id}/input-requests"]["200"]>(`/runs/${runId}/input-requests`),
  resolveInput: (id: string, body: ApiRequests["POST /api/v1/input-requests/{request_id}/resolve"]["body"], key: string) =>
    api<ApiResponses["POST /api/v1/input-requests/{request_id}/resolve"]["200"]>(`/input-requests/${id}/resolve`, { method: "POST", body, idempotencyKey: key }),
  cancelRun: (id: string, key: string) => api<ApiResponses["POST /api/v1/runs/{run_id}/cancel"]["200"]>(`/runs/${id}/cancel`, { method: "POST", idempotencyKey: key }),
  retryRun: (id: string, key: string) => api<ApiResponses["POST /api/v1/runs/{run_id}/retry"]["200"]>(`/runs/${id}/retry`, { method: "POST", idempotencyKey: key }),
};
