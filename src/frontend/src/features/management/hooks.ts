import { languageName, languageCode, languagePreference } from "../../i18n/languages";
// 管理台数据 hook。筛选变化即清游标、取消旧请求；查询键带身份世代与精确筛选，
// 慢响应不会覆盖新筛选的结果。日期只发本地字符串，由服务端按本人时区换算。
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import { isApiError } from "../../api/client";
import { managementApi, type RunQuery } from "../../api/management";
import { opsApi } from "../../api/ops";
import { reviewApi } from "../../api/review";
import { uploadApi } from "../../api/upload";
import { useSession } from "../../stores/session";

export type StatusTab = "exported" | "awaiting" | "processing" | "failed" | "indexed" | "checked" | "drafts" | "all";

export type DateType = "exported" | "uploaded" | "initiated" | "activity" | "reviewed";
export type OriginRole = "origin" | "receipt" | "pol" | "country";
export type RecipientRole = "consignee" | "buyer" | "notify";
export type ActorRole = "initiator" | "reviewer" | "exporter";

export interface ManagementFilter {
  case_id: string;
  run_id: string;
  snapshot_id: string;
  tab: StatusTab;
  q: string;
  date_type: DateType;
  local_date_from: string;
  local_date_to: string;
  origin_role: OriginRole;
  origin_value: string;
  recipient_role: RecipientRole;
  recipient_value: string;
  actor_role: ActorRole;
  actor_value: string;
  doc_types: string[];
  formats: string[];
  languages: string[];
  risks: string[];
  version_scope: "current" | "include_history";
  match_scope: "same_logical_document" | "any_in_case";
  only_confirmed: boolean;
  sort: string;
}

export const DEFAULT_FILTER: ManagementFilter = {
  case_id: "",
  run_id: "",
  snapshot_id: "",
  tab: "exported",
  q: "",
  date_type: "exported",
  local_date_from: "",
  local_date_to: "",
  origin_role: "origin",
  origin_value: "",
  recipient_role: "consignee",
  recipient_value: "",
  actor_role: "initiator",
  actor_value: "",
  doc_types: [],
  formats: [],
  languages: [],
  risks: [],
  version_scope: "current",
  match_scope: "same_logical_document",
  only_confirmed: false,
  sort: "exported_desc",
};

/** 仅解析明确支持的条件语法，交由用户编辑确认后才应用。 */
export function naturalFilter(text: string, current: ManagementFilter, timezone: string): ManagementFilter | null {
  let rest = text.trim();
  const next = { ...current, q: "" };
  let recognized = false;
  const take = (pattern: RegExp, apply: (match: RegExpMatchArray) => void) => {
    const match = rest.match(pattern);
    if (!match) return;
    recognized = true; apply(match); rest = rest.replace(match[0], " ");
  };
  take(/近\s*(7|30)\s*天/, m => {
    const today = new Date().toLocaleDateString("sv-SE", { timeZone: timezone });
    const from = new Date(`${today}T12:00:00Z`);
    from.setUTCDate(from.getUTCDate() - Number(m[1]) + 1);
    next.local_date_from = from.toISOString().slice(0, 10); next.local_date_to = today;
  });
  take(/(上传|导出|发起|复核|活动)时间/, m => { next.date_type = ({ 上传: "uploaded", 导出: "exported", 发起: "initiated", 复核: "reviewed", 活动: "activity" } as const)[m[1] as "上传"]; });
  take(/(已导出|待复核|处理中|失败记录|全部记录)/, m => { next.tab = ({ 已导出: "exported", 待复核: "awaiting", 处理中: "processing", 失败记录: "failed", 全部记录: "all" } as const)[m[1] as "已导出"]; });
  take(/(英文|中文|法语|法文|西班牙语|西语|俄语|俄文)(?:译本|结果)/, m => { next.languages = [languagePreference(m[1])!]; });
  take(/(起运地|装货港|原产国|承运人接货地)[:：]\s*([^，,；;\s]+)/, m => { next.origin_role = ({ 起运地: "origin", 装货港: "pol", 原产国: "country", 承运人接货地: "receipt" } as const)[m[1] as "起运地"]; next.origin_value = m[2]; });
  take(/(收货人|买方|通知方)[:：]\s*([^，,；;\s]+)/, m => { next.recipient_role = ({ 收货人: "consignee", 买方: "buyer", 通知方: "notify" } as const)[m[1] as "收货人"]; next.recipient_value = m[2]; });
  next.q = rest.replace(/[，,；;]/g, " ").replace(/\s+/g, " ").trim();
  return recognized ? next : null;
}

/** 规范化为服务端查询参数：维度间 AND、同维多选 OR。 */
export function toQuery(f: ManagementFilter): RunQuery {
  const q: RunQuery = {
    case_id: f.case_id || undefined,
    run_id: f.run_id || undefined,
    snapshot_id: f.snapshot_id || undefined,
    tab: f.tab === "all" ? undefined : f.tab,
    q: f.q.trim() || undefined,
    date_type: f.date_type,
    local_date_from: f.local_date_from || undefined,
    local_date_to: f.local_date_to || undefined,
    version_scope: f.version_scope,
    match_scope: f.match_scope,
    sort: f.sort,
  };
  if (f.origin_value.trim()) {
    q.origin_role = f.origin_role;
    q.origin_value = f.origin_value.trim();
  }
  if (f.recipient_value.trim()) {
    q.recipient_role = f.recipient_role;
    q.recipient_value = f.recipient_value.trim();
  }
  if (f.actor_value.trim()) {
    q.actor_role = f.actor_role;
    q.actor_value = f.actor_value.trim();
  }
  if (f.doc_types.length) q.doc_type = f.doc_types;
  if (f.formats.length) q.format = f.formats;
  if (f.languages.length) q.language = f.languages;
  if (f.risks.length) q.risk = f.risks;
  if (f.only_confirmed) q.only_confirmed = "true";
  return q;
}

/** 筛选中实际生效的条件标签，供逐项删除。 */
export function activeChips(f: ManagementFilter): { key: keyof ManagementFilter | "date"; label: string }[] {
  const chips: { key: keyof ManagementFilter | "date"; label: string }[] = [];
  const dateLabel: Record<DateType, string> = { exported: "导出时间", uploaded: "上传时间", initiated: "处理发起时间", activity: "最近活动时间", reviewed: "复核时间" };
  if (f.q.trim()) chips.push({ key: "q", label: `搜索「${f.q.trim()}」` });
  if (f.case_id) chips.push({ key: "case_id", label: `指定单票 ${f.case_id.slice(0,8)}` });
  if (f.run_id) chips.push({ key: "run_id", label: `指定处理 ${f.run_id.slice(0,8)}` });
  if (f.snapshot_id) chips.push({ key: "snapshot_id", label: `固定快照 ${f.snapshot_id.slice(0,8)}` });
  if (f.local_date_from || f.local_date_to) {
    chips.push({ key: "date", label: `按${dateLabel[f.date_type]} ${f.local_date_from || "不限"} 至 ${f.local_date_to || "不限"}` });
  }
  const originLabel: Record<OriginRole, string> = { origin: "起运地", receipt: "承运人接货地", pol: "装货港", country: "原产国／地区" };
  if (f.origin_value.trim()) chips.push({ key: "origin_value", label: `${originLabel[f.origin_role]}：${f.origin_value.trim()}` });
  const recipientLabel: Record<RecipientRole, string> = { consignee: "收货人", buyer: "买方", notify: "通知方" };
  if (f.recipient_value.trim()) chips.push({ key: "recipient_value", label: `${recipientLabel[f.recipient_role]}：${f.recipient_value.trim()}` });
  const actorLabel: Record<ActorRole, string> = { initiator: "发起人", reviewer: "复核人", exporter: "导出确认人" };
  if (f.actor_value.trim()) chips.push({ key: "actor_value", label: `${actorLabel[f.actor_role]}：${f.actor_value.trim()}` });
  if (f.doc_types.length) chips.push({ key: "doc_types", label: `单据类型：${f.doc_types.length} 项` });
  if (f.formats.length) chips.push({ key: "formats", label: `格式：${f.formats.join("／")}` });
  if (f.languages.length) chips.push({ key: "languages", label: `目标语言：${f.languages.map(languageName).join("／")}` });
  if (f.risks.length) chips.push({ key: "risks", label: `风险：${f.risks.length} 项` });
  if (f.version_scope === "include_history") chips.push({ key: "version_scope", label: "包含历史快照" });
  if (f.match_scope === "any_in_case") chips.push({ key: "match_scope", label: "同一批次任意文件" });
  if (f.only_confirmed) chips.push({ key: "only_confirmed", label: "仅已确认值" });
  return chips;
}

export function useRunList(filter: ManagementFilter) {
  const gen = useSession((s) => s.generation);
  const qc = useQueryClient();
  const recovering = useRef(false);
  const query = useMemo(() => toQuery(filter), [filter]);
  const q = useInfiniteQuery({
    queryKey: ["m-runs", gen, query],
    enabled: filter.tab !== "drafts",
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => managementApi.runs({ ...query, cursor: pageParam }, signal),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: false,
  });
  useEffect(() => {
    if (!isApiError(q.error, "PAGE_STALE")) {
      recovering.current = false;
      return;
    }
    if (recovering.current) return;
    recovering.current = true;
    // 水位已变化时丢弃整串旧页，从相同筛选第一页恢复。
    void qc.resetQueries({ queryKey: ["m-runs", gen, query], exact: true });
    void qc.invalidateQueries({ queryKey: ["m-facets", gen] });
  }, [q.error, qc, gen, query]);
  const items = (q.data?.pages ?? []).flatMap((p) => p.items);
  return { ...q, items, asOf: q.data?.pages[0]?.as_of, summaryStale: items.some((r) => r.summary_status === "stale") };
}

export function useFacets(filter: ManagementFilter) {
  const gen = useSession((s) => s.generation);
  const query = useMemo(() => toQuery({ ...filter, tab: "all" }), [filter]);
  return useQuery({ queryKey: ["m-facets", gen, query], queryFn: () => managementApi.facets(query), retry: false });
}

export function useUploadDrafts(enabled: boolean) {
  const gen = useSession((s) => s.generation);
  const q = useInfiniteQuery({
    queryKey: ["m-uploads", gen],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => uploadApi.sessions({ status: "drafts", cursor: pageParam }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled, retry: false,
  });
  return { ...q, items: (q.data?.pages ?? []).flatMap((page) => page.items) };
}

export function useCaseOverview(caseId: string | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["m-case", gen, caseId], queryFn: () => managementApi.overview(caseId!), enabled: !!caseId, retry: false });
}

export function useRunSummary(runId: string | null, snapshotId?: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["m-summary", gen, runId, snapshotId ?? null], queryFn: () => managementApi.summary(runId!, snapshotId), enabled: !!runId, retry: false });
}

export function useRunTimeline(runId: string | null) {
  const gen = useSession((s) => s.generation);
  const q = useInfiniteQuery({
    queryKey: ["m-timeline", gen, runId],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => managementApi.timeline(runId!, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!runId,
    retry: false,
  });
  return { ...q, items: (q.data?.pages ?? []).flatMap((p) => p.items) };
}

export function useSnapshots(runId: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["m-snapshots", gen, runId], queryFn: () => reviewApi.snapshots(runId!), enabled: !!runId, retry: false });
}

export function useSnapshot(snapshotId: string | null | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["m-snapshot", gen, snapshotId], queryFn: () => reviewApi.snapshot(snapshotId!), enabled: !!snapshotId, retry: false });
}

export function useDeletionPreview(caseId: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["m-del-preview", gen, caseId], queryFn: () => managementApi.deletionPreview(caseId!), enabled: !!caseId, retry: false, staleTime: 0, gcTime: 0 });
}

export function useDeletionStatus(deletionId: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["m-deletion", gen, deletionId],
    queryFn: () => managementApi.deletion(deletionId!),
    enabled: !!deletionId,
    refetchInterval: (q) => (q.state.data && ["queued", "purging"].includes(q.state.data.status) ? 1200 : false),
    retry: false,
  });
}

/** O01：仅具备 ops_reconcile 权限且拿到受控步骤 ID 时才读取。 */
export function useOpsStep(stepId: string | null, allowed: boolean) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["ops-step", gen, stepId], queryFn: () => opsApi.step(stepId!), enabled: !!stepId && allowed, retry: false, staleTime: 0 });
}

/** O02：仅具备 ops_migrate_model 权限时读取迁移选项。 */
export function useMigrationOptions(runId: string | null, allowed: boolean) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["ops-migrate", gen, runId], queryFn: () => opsApi.migrationOptions(runId!), enabled: !!runId && allowed, retry: false, staleTime: 0 });
}

export function usePermissions() {
  const identity = useSession((s) => s.identity);
  return {
    canReconcile: !!identity?.permissions.includes("ops_reconcile"),
    canMigrate: !!identity?.permissions.includes("ops_migrate_model"),
  };
}
