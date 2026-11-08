import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";

export const opsApi = {
  step: (id: string) => api<ApiResponses["GET /api/v1/ops/steps/{step_id}"]["200"]>(`/ops/steps/${id}`),
  reconcile: (id: string, body: { expected_step_version: number; decision: string; evidence_reference: string; reason: string }, key: string) =>
    api<ApiResponses["POST /api/v1/ops/steps/{step_id}/reconcile"]["200"]>(`/ops/steps/${id}/reconcile`, { method: "POST", body, idempotencyKey: key }),
  migrationOptions: (runId: string) => api<ApiResponses["GET /api/v1/ops/runs/{run_id}/model-migration-options"]["200"]>(`/ops/runs/${runId}/model-migration-options`),
  migrate: (runId: string, body: { candidate_config_fingerprint: string; expected_execution_epoch: number; reason: string }, key: string) =>
    api<ApiResponses["POST /api/v1/ops/runs/{run_id}/migrate-model"]["200"]>(`/ops/runs/${runId}/migrate-model`, { method: "POST", body, idempotencyKey: key }),
};
