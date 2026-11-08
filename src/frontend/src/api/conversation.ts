import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";
import type { ApiRequests, TurnBody as PublishedTurnBody } from "./generated/request-contracts";
import type {
  CapabilityCandidate,
  CapabilitySelection,
  ContextView,
  ConversationSummary,
  ConversationView,
  McpChallengeView,
  MessageView,
  Page,
  ReferenceCandidate,
  ReferenceSelection,
  TurnView,
} from "./types";

export interface TurnBody extends Pick<PublishedTurnBody, "text" | "task_draft_id"> {
  references: ReferenceSelection[];
  capabilities: CapabilitySelection[];
  upload_file_ids: string[];
}

export const conversationApi = {
  list: (q?: string, cursor?: string, archived = false) => api<ApiResponses["GET /api/v1/conversations"]["200"]>("/conversations", { query: { q, cursor, limit: 30, archived } }),
  archive: (id: string, expected_conversation_version: number, key: string) => api<ApiResponses["POST /api/v1/conversations/{conversation_id}/archive"]["200"]>(`/conversations/${id}/archive`, { method: "POST", body: {expected_conversation_version}, idempotencyKey:key }),
  restore: (id: string, expected_conversation_version: number, key: string) => api<ApiResponses["POST /api/v1/conversations/{conversation_id}/restore"]["200"]>(`/conversations/${id}/restore`, { method: "POST", body: {expected_conversation_version}, idempotencyKey:key }),
  remove: (id: string, expected_conversation_version: number, key: string) => api<void>(`/conversations/${id}`, { method: "DELETE", body: {expected_conversation_version}, idempotencyKey:key }),
  create: (body: { initial_reference_selections?: ReferenceSelection[]; entry_source?: string }, key: string) =>
    api<ApiResponses["POST /api/v1/conversations"]["201"]>("/conversations", { method: "POST", body: {
      ...body, initial_reference_selections: body.initial_reference_selections?.map(reference => ({ ...reference })),
    } satisfies ApiRequests["POST /api/v1/conversations"]["body"], idempotencyKey: key }),
  get: (id: string) => api<ApiResponses["GET /api/v1/conversations/{conversation_id}"]["200"]>(`/conversations/${id}`),
  rename: (id: string, title: string, expected_conversation_version: number, key: string) =>
    api<ApiResponses["PATCH /api/v1/conversations/{conversation_id}"]["200"]>(`/conversations/${id}`, { method: "PATCH", body: { title, expected_conversation_version }, idempotencyKey: key }),
  messages: (id: string, cursor?: string, beforeSeq?: number) => api<ApiResponses["GET /api/v1/conversations/{conversation_id}/messages"]["200"]>(`/conversations/${id}/messages`, { query: { cursor, before_seq: beforeSeq, limit: 50 } }),
  sendTurn: (id: string, body: TurnBody, key: string) => api<ApiResponses["POST /api/v1/conversations/{conversation_id}/turns"]["202"]>(`/conversations/${id}/turns`, { method: "POST", body: {
    ...body, references: body.references.map(reference => ({ ...reference })), capabilities: body.capabilities.map(({ kind, extension_id, extension_version, tool_id, tool_schema_hash, user_parameters }) =>
      ({ kind, extension_id, extension_version, tool_id, tool_schema_hash, user_parameters })),
  } satisfies PublishedTurnBody, idempotencyKey: key }),
  turn: (id: string, turnId: string) => api<ApiResponses["GET /api/v1/conversations/{conversation_id}/turns/{turn_id}"]["200"]>(`/conversations/${id}/turns/${turnId}`),
  context: (id: string) => api<ApiResponses["GET /api/v1/conversations/{conversation_id}/context"]["200"]>(`/conversations/${id}/context`),
  end: (id: string, key: string) => api<ApiResponses["POST /api/v1/conversations/{conversation_id}/end"]["200"]>(`/conversations/${id}/end`, { method: "POST", idempotencyKey: key }),
  searchReferences: (params: Record<string, string | undefined>, signal?: AbortSignal) =>
    api<ApiResponses["GET /api/v1/search/references"]["200"]>("/search/references", { query: params, signal }),
  capabilities: (q: string, signal?: AbortSignal) => api<ApiResponses["GET /api/v1/extensions/available"]["200"]>("/extensions/available", { query: { q }, signal }),
  challenge: (id: string) => api<ApiResponses["GET /api/v1/mcp-challenges/{challenge_id}"]["200"]>(`/mcp-challenges/${id}`),
  approveChallenge: (id: string, key: string) => api<ApiResponses["POST /api/v1/mcp-challenges/{challenge_id}/approve"]["200"]>(`/mcp-challenges/${id}/approve`, { method: "POST", idempotencyKey: key }),
  rejectChallenge: (id: string, key: string) => api<ApiResponses["POST /api/v1/mcp-challenges/{challenge_id}/reject"]["200"]>(`/mcp-challenges/${id}/reject`, { method: "POST", idempotencyKey: key }),
};
