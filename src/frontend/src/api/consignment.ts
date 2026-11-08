import { api, type RequestOptions } from "./client";
import type * as Published from "./generated/request-contracts";
import type { ApiRequests, ApiResponses } from "./generated/request-contracts";
import type { EvidenceTarget } from "./types";

export type ConsignmentMaterial = Required<Published.Material>;
export type ScopeParty = Required<Published.ScopeParty>;
export type ScopeLeg = Required<Published.ScopeLeg>;
export type ScopeCargo = Required<Published.ScopeCargo>;
export type ConsignmentScopePayload = Omit<Published.ScopePayload, "schema_version" | "materials" | "parties" | "legs" | "cargo" | "relation_ids" | "match_group_ids" | "completeness"> & {
  schema_version: 1; materials: ConsignmentMaterial[]; parties: ScopeParty[]; legs: ScopeLeg[]; cargo: ScopeCargo[]; relation_ids: string[]; match_group_ids: string[]; completeness: "complete" | "partial" | "unknown";
};
export type ConsignmentScope = Omit<Published.ScopeView, "payload"> & { payload: ConsignmentScopePayload };
export type ComparisonBatch = Published.BatchView;
export interface ComparisonSource { side: string; target: EvidenceTarget; excerpt?: string; precision?: string }
export interface ComparisonMember { line_id?: string; field_id?: string; logical_id?: string; source?: string; adopted?: string; value?: string; unit?: string; document_id?: string; label?: string }
export type ComparisonRow = Omit<Published.MatrixRow, "members" | "sources"> & { members: Record<string, ComparisonMember[]>; sources: ComparisonSource[] };
export type RelationCandidate = Published.RelationView;
export type ConsignmentMutation = Omit<Published.MutationView, "run_id"> & { run_id: string | null };
export type ConsignmentPreconditions = Pick<Published.ScopeCommand, "expected_case_version" | "expected_scope_revision_id" | "expected_business_revision_id" | "expected_review_state_version">;
export const scopePreconditions = (scope: ConsignmentScope): ConsignmentPreconditions => ({ expected_case_version: scope.case_version, expected_scope_revision_id: scope.scope_revision_id, expected_business_revision_id: scope.business_revision_id, expected_review_state_version: scope.review_state_version });
const normalizeScope = (view: Published.ScopeView): ConsignmentScope => ({ ...view, payload: { ...view.payload, schema_version: 1,
  materials: view.payload.materials.map(material => ({ ...material, purpose: material.purpose ?? "active" })),
  parties: (view.payload.parties ?? []).map(item => ({ ...item, logical_document_ids: item.logical_document_ids ?? [], evidence_ids: item.evidence_ids ?? [] })),
  legs: (view.payload.legs ?? []).map(item => ({ ...item, mode: item.mode ?? "unknown", origin: item.origin ?? null, destination: item.destination ?? null, logical_document_ids: item.logical_document_ids ?? [], evidence_ids: item.evidence_ids ?? [] })),
  cargo: (view.payload.cargo ?? []).map(item => ({ ...item, logical_document_ids: item.logical_document_ids ?? [], evidence_ids: item.evidence_ids ?? [] })),
  relation_ids: view.payload.relation_ids ?? [], match_group_ids: view.payload.match_group_ids ?? [], completeness: view.payload.completeness ?? "unknown" } });
const normalizeMutation = (view: Published.MutationView): ConsignmentMutation => ({ ...view, run_id: view.run_id ?? null });
const record = (value: unknown): Record<string, unknown> | null => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const text = (value: unknown): string | undefined => typeof value === "string" ? value : typeof value === "number" ? String(value) : undefined;
const normalizeSources = (sources: Record<string, unknown>[]): ComparisonSource[] => sources.flatMap(source => {
  const target = record(source.target);
  if (!target || typeof target.case_id !== "string") return [];
  return [{ side: text(source.side) ?? "", target: target as EvidenceTarget, excerpt: text(source.excerpt), precision: text(source.precision) }];
});
const normalizeRow = (row: Published.MatrixRow): ComparisonRow => ({ ...row, members: Object.fromEntries(Object.entries(row.members).map(([side, value]) => [side, Array.isArray(value) ? value.flatMap(item => {
  const member = record(item); return member ? [{ line_id: text(member.line_id), field_id: text(member.field_id), logical_id: text(member.logical_id), source: text(member.source), adopted: text(member.adopted), value: text(member.value), unit: text(member.unit), document_id: text(member.document_id), label: text(member.label) }] : [];
}) : []])), sources: normalizeSources(row.sources) });
type Query = RequestOptions["query"];
const base = (caseId: string) => `/cases/${caseId}`;
export const consignmentApi = {
  scope: (caseId: string, scopeRevisionId?: string | null, signal?: AbortSignal) => api<ApiResponses["GET /api/v1/cases/{case_id}/consignment-scope"]["200"]>(`${base(caseId)}/consignment-scope`, { query: { scope_revision_id: scopeRevisionId }, signal }).then(normalizeScope),
  reviseScope: (caseId: string, body: ApiRequests["POST /api/v1/cases/{case_id}/consignment-scope/revisions"]["body"], key: string) => api<ApiResponses["POST /api/v1/cases/{case_id}/consignment-scope/revisions"]["200"]>(`${base(caseId)}/consignment-scope/revisions`, { method: "POST", body, idempotencyKey: key }).then(normalizeMutation),
  batches: (caseId: string, query?: Query, signal?: AbortSignal) => api<ApiResponses["GET /api/v1/cases/{case_id}/comparisons"]["200"]>(`${base(caseId)}/comparisons`, { query, signal }),
  batch: (caseId: string, id: string, signal?: AbortSignal, snapshotId?: string | null) => api<ApiResponses["GET /api/v1/cases/{case_id}/comparisons/{batch_id}"]["200"]>(`${base(caseId)}/comparisons/${id}`, { query: { snapshot_id: snapshotId }, signal }),
  rows: (caseId: string, id: string, query?: Query, signal?: AbortSignal) => api<ApiResponses["GET /api/v1/cases/{case_id}/comparisons/{batch_id}/rows"]["200"]>(`${base(caseId)}/comparisons/${id}/rows`, { query, signal }).then(page => ({ ...page, items: page.items.map(normalizeRow) })),
  evidence: (caseId: string, batchId: string, rowId: string, signal?: AbortSignal) => api<ApiResponses["GET /api/v1/cases/{case_id}/comparisons/{batch_id}/rows/{row_id}/evidence"]["200"]>(`${base(caseId)}/comparisons/${batchId}/rows/${rowId}/evidence`, { signal }).then(view => ({ ...view, sides: Object.fromEntries(Object.entries(view.sides).map(([side, sources]) => [side, normalizeSources(sources)])) })),
  relations: (caseId: string, query?: Query, signal?: AbortSignal) => api<ApiResponses["GET /api/v1/cases/{case_id}/relations"]["200"]>(`${base(caseId)}/relations`, { query, signal }),
  decideRelation: (caseId: string, body: ApiRequests["POST /api/v1/cases/{case_id}/relation-decisions"]["body"], key: string) => api<ApiResponses["POST /api/v1/cases/{case_id}/relation-decisions"]["200"]>(`${base(caseId)}/relation-decisions`, { method: "POST", body, idempotencyKey: key }).then(normalizeMutation),
  decideCargo: (caseId: string, body: ApiRequests["POST /api/v1/cases/{case_id}/cargo-match-decisions"]["body"], key: string) => api<ApiResponses["POST /api/v1/cases/{case_id}/cargo-match-decisions"]["200"]>(`${base(caseId)}/cargo-match-decisions`, { method: "POST", body, idempotencyKey: key }).then(normalizeMutation),
  recompute: (caseId: string, body: ApiRequests["POST /api/v1/cases/{case_id}/comparisons/recompute"]["body"], key: string) => api<ApiResponses["POST /api/v1/cases/{case_id}/comparisons/recompute"]["200"]>(`${base(caseId)}/comparisons/recompute`, { method: "POST", body, idempotencyKey: key }).then(normalizeMutation),
};
