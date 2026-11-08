import type { ApiResponses } from "./generated/request-contracts";
import type { PreferencesView } from "./generated/request-contracts";
import { api } from "./client";

export const preferencesApi = {
  get: () => api<ApiResponses["GET /api/v1/me/preferences"]["200"]>("/me/preferences"),
  timezones: () => api<ApiResponses["GET /api/v1/me/timezones"]["200"]>("/me/timezones"),
  put: (body: Omit<PreferencesView, "version" | "saved_at">, expected_version: number, key: string) =>
    api<ApiResponses["PUT /api/v1/me/preferences"]["200"]>("/me/preferences", { method: "PUT", body: { ...body, expected_version }, idempotencyKey: key }),
  memories: () => api<ApiResponses["GET /api/v1/me/memories"]["200"]>("/me/memories"),
  previewMemory: (body: { category: string; content: string; applies_when?: string }) => api<ApiResponses["POST /api/v1/me/memories/preview"]["200"]>("/me/memories/preview", { method: "POST", body }),
  createMemory: (body: { category: string; content: string; applies_when?: string }, key: string) => api<ApiResponses["POST /api/v1/me/memories"]["201"]>("/me/memories", { method: "POST", body, idempotencyKey: key }),
  editMemory: (id: string, body: { category: string; content: string; applies_when?: string; expected_version: number }, key: string) =>
    api<ApiResponses["PATCH /api/v1/me/memories/{memory_id}"]["200"]>(`/me/memories/${id}`, { method: "PATCH", body, idempotencyKey: key }),
  deleteMemory: (id: string, expected_version: number, key: string) =>
    api<ApiResponses["POST /api/v1/me/memories/{memory_id}/delete"]["200"]>(`/me/memories/${id}/delete`, { method: "POST", body: { expected_version }, idempotencyKey: key }),
  suggestions: () => api<ApiResponses["GET /api/v1/me/memory-suggestions"]["200"]>("/me/memory-suggestions"),
  confirmSuggestion: (id: string, key: string) => api<ApiResponses["POST /api/v1/me/memory-suggestions/{suggestion_id}/confirm"]["200"]>(`/me/memory-suggestions/${id}/confirm`, { method: "POST", idempotencyKey: key }),
  ignoreSuggestion: (id: string, key: string) => api<ApiResponses["POST /api/v1/me/memory-suggestions/{suggestion_id}/ignore"]["200"]>(`/me/memory-suggestions/${id}/ignore`, { method: "POST", idempotencyKey: key }),
};
