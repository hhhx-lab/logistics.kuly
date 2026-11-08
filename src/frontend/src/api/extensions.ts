import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";
import type { ExtensionCall, ExtensionRow, ImportDiffRow, McpTemplateCapability, McpVersion, SkillActionOption, SkillVersion } from "./types";

export const extensionsApi = {
  list: (q: { kind?: string; row_state?: string; q?: string }) => api<ApiResponses["GET /api/v1/extensions"]["200"]>("/extensions", { query: q }),
  actions: () => api<ApiResponses["GET /api/v1/skill-actions"]["200"]>("/skill-actions"),
  templates: () => api<ApiResponses["GET /api/v1/mcp-templates"]["200"]>("/mcp-templates"),
  calls: (extensionId?: string) => api<ApiResponses["GET /api/v1/extension-calls"]["200"]>("/extension-calls", { query: { extension_id: extensionId } }),
  importSkillPreview: (text: string) => api<ApiResponses["POST /api/v1/skills/import-preview"]["200"]>("/skills/import-preview", { method: "POST", body: { text } }),
  createSkill: (body: Partial<SkillVersion>, key: string) => api<ApiResponses["POST /api/v1/skills"]["201"]>("/skills", { method: "POST", body, idempotencyKey: key }),
  getSkill: (id: string) => api<ApiResponses["GET /api/v1/skills/{skill_id}"]["200"]>(`/skills/${id}`),
  newSkillVersion: (id: string, key: string) => api<ApiResponses["POST /api/v1/skills/{skill_id}/versions"]["200"]>(`/skills/${id}/versions`, { method: "POST", idempotencyKey: key }),
  patchSkillVersion: (id: string, v: number, body: Partial<SkillVersion>, key: string) =>
    api<ApiResponses["PATCH /api/v1/skills/{skill_id}/versions/{version}"]["200"]>(`/skills/${id}/versions/${v}`, { method: "PATCH", body, idempotencyKey: key }),
  validateSkill: (id: string, v: number, key: string) => api<ApiResponses["POST /api/v1/skills/{skill_id}/versions/{version}/validate"]["200"]>(`/skills/${id}/versions/${v}/validate`, { method: "POST", idempotencyKey: key }),
  enableSkill: (id: string, v: number, key: string) => api<ApiResponses["POST /api/v1/skills/{skill_id}/versions/{version}/enable"]["200"]>(`/skills/${id}/versions/${v}/enable`, { method: "POST", idempotencyKey: key }),
  disableSkill: (id: string, key: string) => api<ApiResponses["POST /api/v1/skills/{skill_id}/disable"]["200"]>(`/skills/${id}/disable`, { method: "POST", idempotencyKey: key }),
  archiveSkill: (id: string, key: string) => api<ApiResponses["POST /api/v1/skills/{skill_id}/archive"]["200"]>(`/skills/${id}/archive`, { method: "POST", idempotencyKey: key }),
  importMcpPreview: (text: string) => api<ApiResponses["POST /api/v1/mcp-connections/import-preview"]["200"]>("/mcp-connections/import-preview", { method: "POST", body: { text } }),
  createMcp: (body: { name: string; endpoint: string; auth_type: string; credential?: string }, key: string) =>
    api<ApiResponses["POST /api/v1/mcp-connections"]["201"]>("/mcp-connections", { method: "POST", body, idempotencyKey: key }),
  getMcp: (id: string) => api<ApiResponses["GET /api/v1/mcp-connections/{connection_id}"]["200"]>(`/mcp-connections/${id}`),
  newMcpVersion: (id: string, key: string) => api<ApiResponses["POST /api/v1/mcp-connections/{connection_id}/versions"]["200"]>(`/mcp-connections/${id}/versions`, { method: "POST", idempotencyKey: key }),
  patchMcpVersion: (id: string, v: number, body: Record<string, unknown>, key: string) =>
    api<ApiResponses["PATCH /api/v1/mcp-connections/{connection_id}/versions/{version}"]["200"]>(`/mcp-connections/${id}/versions/${v}`, { method: "PATCH", body, idempotencyKey: key }),
  testMcp: (id: string, v: number, key: string) => api<ApiResponses["POST /api/v1/mcp-connections/{connection_id}/versions/{version}/test"]["200"]>(`/mcp-connections/${id}/versions/${v}/test`, { method: "POST", idempotencyKey: key }),
  mcpTools: (id: string, v: number) => api<ApiResponses["GET /api/v1/mcp-connections/{connection_id}/versions/{version}/tools"]["200"]>(`/mcp-connections/${id}/versions/${v}/tools`),
  grantTools: (id: string, v: number, tool_ids: string[], key: string) =>
    api<ApiResponses["POST /api/v1/mcp-connections/{connection_id}/versions/{version}/tool-grants"]["200"]>(`/mcp-connections/${id}/versions/${v}/tool-grants`, { method: "POST", body: { tool_ids }, idempotencyKey: key }),
  revokeGrant: (grantId: string, key: string) => api<ApiResponses["POST /api/v1/mcp-tool-grants/{grant_id}/revoke"]["200"]>(`/mcp-tool-grants/${grantId}/revoke`, { method: "POST", idempotencyKey: key }),
  enableMcp: (id: string, v: number, key: string) => api<ApiResponses["POST /api/v1/mcp-connections/{connection_id}/versions/{version}/enable"]["200"]>(`/mcp-connections/${id}/versions/${v}/enable`, { method: "POST", idempotencyKey: key }),
  disableMcp: (id: string, key: string) => api<ApiResponses["POST /api/v1/mcp-connections/{connection_id}/disable"]["200"]>(`/mcp-connections/${id}/disable`, { method: "POST", idempotencyKey: key }),
  archiveMcp: (id: string, key: string) => api<ApiResponses["POST /api/v1/mcp-connections/{connection_id}/archive"]["200"]>(`/mcp-connections/${id}/archive`, { method: "POST", idempotencyKey: key }),
};
