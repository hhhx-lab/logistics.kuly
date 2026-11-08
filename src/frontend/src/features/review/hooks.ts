// 校验区数据 hook。查询键统一带身份世代与所选版本；历史快照与当前工作版使用不同键，
// 版本或模式变化立即换键，不把旧配对的字段／译段拼进新版本。
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isApiError, newIdempotencyKey } from "../../api/client";
import { reviewApi } from "../../api/review";
import { workflowApi } from "../../api/workflow";
import { managementApi } from "../../api/management";
import { useSequencedStream } from "../../api/events";
import type { EvidenceTarget, ReviewView } from "../../api/types";
import { useSession } from "../../stores/session";

export type ReviewMode = ReviewView["mode"];

export interface Scope {
  caseId: string;
  runId?: string | null;
  snapshotId?: string | null;
  parseArtifactId?: string | null;
  businessRevisionId?: string | null;
}

/** R01：打开前冻结目标并鉴权。 */
export function useResolveEvidence(target: EvidenceTarget | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["evidence", gen, target],
    queryFn: () => reviewApi.resolve(target!),
    enabled: !!target,
    retry: false,
    staleTime: 60_000,
  });
}

/** R02：轻量抬头、文件树、门禁摘要与版本指针。 */
export function useReviewView(scope: Scope, documentId?: string, gateOpen = true) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["review-view", gen, scope.caseId, scope.snapshotId ?? "working", scope.runId ?? null, documentId ?? null, scope.parseArtifactId ?? null, scope.businessRevisionId ?? null],
    queryFn: () => reviewApi.view(scope.caseId, { run_id: scope.snapshotId ? undefined : scope.runId ?? undefined, snapshot_id: scope.snapshotId ?? undefined, document_id: documentId, parse_artifact_id: scope.parseArtifactId ?? undefined, business_revision_id: scope.businessRevisionId ?? undefined }),
    enabled: !!scope.caseId && gateOpen,
    retry: false,
  });
}

/** 当前工作版的版本三元组，历史版只用 snapshot_id。 */
function versionParams(view: ReviewView | undefined) {
  if (!view) return {};
  if (view.mode === "historical") return view.snapshot_id ? { snapshot_id: view.snapshot_id } : {
    run_id: view.run_id ?? undefined, parse_artifact_id: view.files[0]?.parse_artifact_id ?? undefined,
    business_revision_id: view.business_revision_id ?? undefined
  };
  return {
    run_id: view.run_id ?? undefined,
    business_revision_id: view.business_revision_id ?? undefined,
    review_state_version: view.review_state_version,
  };
}

function versionKey(view: ReviewView | undefined) {
  if (!view) return "none";
  if (view.mode === "historical") return view.snapshot_id ? `snap:${view.snapshot_id}` : `source:${view.files[0]?.parse_artifact_id}:${view.business_revision_id}`;
  return `work:${view.run_id}:${view.business_revision_id}:${view.review_state_version}:${view.translation_revision_id}`;
}

/**
 * R03 字段：游标分页。当前工作版的后续页若复核状态版已变，服务端返回
 * 409 PAGE_STALE；此时丢弃整串页、重取 R02 的状态版，再从第一页恢复，
 * 绝不把新旧处置拼在一起。
 */
export function useFields(view: ReviewView | undefined, logicalDocumentId?: string | null, group?: string, documentId?: string | null) {
  const gen = useSession((s) => s.generation);
  const qc = useQueryClient();
  const key = ["fields", gen, view?.case_id, versionKey(view), logicalDocumentId ?? null, group ?? null, documentId ?? null];
  const q = useInfiniteQuery({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      reviewApi.fields(view!.case_id, { ...versionParams(view), logical_document_id: logicalDocumentId ?? undefined, document_id:documentId ?? undefined, group, cursor: pageParam, limit: 50 }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!view,
    retry: false,
  });
  usePageStaleRecovery(q.error, key, qc, view?.case_id);
  return {
    ...q,
    items: (q.data?.pages ?? []).flatMap((p) => p.items),
  };
}

/** 分页串遇 PAGE_STALE：清掉该串并重取 R02，页面随新状态版从第一页重新加载。 */
function usePageStaleRecovery(error: unknown, key: unknown[], qc: ReturnType<typeof useQueryClient>, caseId: string | undefined) {
  const staleRef = useRef(false);
  useEffect(() => {
    if (!isApiError(error, "PAGE_STALE")) {
      staleRef.current = false;
      return;
    }
    if (staleRef.current) return;
    staleRef.current = true;
    // 保留过期查询的错误态，等待视图返回新版本后自然换键；移除活动查询会重新加载旧版本并形成循环。
    if (caseId) void qc.invalidateQueries({ queryKey: ["review-view", key[1], caseId] });
  }, [error, key, qc, caseId]);
}

export function useIssues(view: ReviewView | undefined, severity?: string, documentId?: string | null) {
  const gen = useSession((s) => s.generation);
  const qc = useQueryClient();
  const key = ["issues", gen, view?.case_id, versionKey(view), severity ?? null, documentId ?? null];
  const q = useInfiniteQuery({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => reviewApi.issues(view!.case_id, { ...versionParams(view), severity, document_id: documentId ?? undefined, cursor: pageParam, limit: 50 }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!view,
    retry: false,
  });
  usePageStaleRecovery(q.error, key, qc, view?.case_id);
  return { ...q, items: (q.data?.pages ?? []).flatMap((p) => p.items) };
}

export function useLineItems(view: ReviewView | undefined, logicalDocumentId?: string | null) {
  const gen = useSession((s) => s.generation);
  const qc = useQueryClient();
  const key = ["line-items", gen, view?.case_id, versionKey(view), logicalDocumentId ?? null];
  const q = useInfiniteQuery({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    queryFn: ({pageParam}) => reviewApi.lineItems(view!.case_id, { ...versionParams(view), run_id:view?.run_id??undefined, logical_document_id: logicalDocumentId ?? undefined, cursor:pageParam, limit: 200 }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!view,
    retry: false,
  });
  usePageStaleRecovery(q.error, key, qc, view?.case_id);
  return {...q, items:(q.data?.pages ?? []).flatMap(page=>page.items)};
}

export function useChecks(view: ReviewView | undefined) {
  const gen = useSession((s) => s.generation);
  const qc = useQueryClient();
  const key = ["checks", gen, view?.run_id, versionKey(view)];
  const q = useInfiniteQuery({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    queryFn: ({pageParam}) => reviewApi.checks(view!.run_id!, { ...versionParams(view), cursor:pageParam, limit: 200 }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!view?.run_id && (view.mode !== "historical" || !!view.snapshot_id),
    retry: false,
  });
  usePageStaleRecovery(q.error, key, qc, view?.case_id);
  return {...q, items:(q.data?.pages ?? []).flatMap(page=>page.items)};
}

/** R05：译段分页；read_index 返回 not_applicable。 */
export function useTranslations(view: ReviewView | undefined, documentId?: string | null, sectionId?: string | null) {
  const gen = useSession((s) => s.generation);
  const qc = useQueryClient();
  const key = ["translations", gen, view?.run_id, versionKey(view), documentId ?? null, sectionId ?? null];
  const q = useInfiniteQuery({
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    queryFn: ({pageParam}) =>
      reviewApi.translations(view!.run_id!, {
        ...(view!.mode === "historical"
          ? { snapshot_id: view!.snapshot_id ?? undefined }
          : { business_revision_id: view!.business_revision_id ?? undefined, translation_revision_id: view!.translation_revision_id ?? undefined }),
        document_id: documentId ?? undefined,
        section_id: sectionId ?? undefined,
        cursor:pageParam,
        limit: 200,
      }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!view?.run_id && (view.mode !== "historical" || !!view.snapshot_id),
    retry: false,
  });
  usePageStaleRecovery(q.error, key, qc, view?.case_id);
  return {...q, items:(q.data?.pages ?? []).flatMap(page=>page.items), notApplicable:q.data?.pages[0]?.not_applicable};
}

export function usePreviewManifest(previewId: string | null | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["preview-manifest", gen, previewId],
    queryFn: () => reviewApi.previewManifest(previewId!),
    enabled: !!previewId,
    retry: false,
  });
}

export function useRun(runId: string | null | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["run", gen, runId],
    queryFn: () => workflowApi.run(runId!),
    enabled: !!runId,
    retry: false,
  });
}

export function useInputRequests(runId: string | null | undefined, enabled: boolean) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["input-requests", gen, runId],
    queryFn: () => workflowApi.inputRequests(runId!),
    enabled: !!runId && enabled,
    retry: false,
  });
}

export function useTimeline(runId: string | null | undefined, enabled: boolean) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["timeline", gen, runId],
    queryFn: () => managementApi.timeline(runId!),
    enabled: !!runId && enabled,
    retry: false,
  });
}

export function useChecklist(runId: string | null | undefined, enabled: boolean) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["checklist", gen, runId],
    queryFn: () => reviewApi.checklist(runId!),
    enabled: !!runId && enabled,
    retry: false,
    // 打开确认层必须重新取，不能用旧门禁判断
    staleTime: 0,
    gcTime: 0,
  });
}

export function useExportAttempt(runId: string | null | undefined, attemptId: string | null | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["export-attempt", gen, runId, attemptId],
    queryFn: () => reviewApi.exportAttempt(runId!, attemptId!),
    enabled: !!runId && !!attemptId,
    retry: false,
    refetchInterval: (query) => query.state.data && ["succeeded", "failed_retryable", "failed_final", "paused_reconcile"].includes(query.state.data.status) ? false : 1500,
  });
}

export function useSnapshot(snapshotId: string | null | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({
    queryKey: ["snapshot", gen, snapshotId],
    queryFn: () => reviewApi.snapshot(snapshotId!),
    enabled: !!snapshotId,
    retry: false,
  });
}

/**
 * 只有当前工作版才订阅本 Run 事件；历史快照与只读原件不订阅，
 * 避免当前事件把冻结页面刷新成新版本。
 */
export function useReviewStream(view: ReviewView | undefined) {
  const qc = useQueryClient();
  const gen = useSession((s) => s.generation);
  const cursor = view?.run_event_cursor ?? null;
  const invalidate = useCallback(async () => {
    if (!view) return;
    const versioned = new Set(["fields", "line-items", "issues", "translations", "checks"]);
    const currentPages = { predicate: (query: { queryKey: readonly unknown[] }) =>
      versioned.has(String(query.queryKey[0])) && query.queryKey[1] === gen &&
      (query.queryKey[2] === view.case_id || query.queryKey[2] === view.run_id) &&
      String(query.queryKey[3]).startsWith("work:") };
    // 先更新轻量版本指针。事件和推荐处理可能连续修改复核版，不能同时重取旧版分页。
    await qc.cancelQueries(currentPages);
    await qc.invalidateQueries({ ...currentPages, refetchType: "none" });
    await qc.invalidateQueries({ queryKey: ["review-view", gen, view.case_id, "working"] });
    const latestVersions = new Set(qc.getQueriesData<ReviewView>({ queryKey: ["review-view", gen, view.case_id, "working"], type: "active" })
      .flatMap(([, latest]) => latest?.mode === "working" && latest.run_id === view.run_id ? [versionKey(latest)] : []));
    // 同一版本内的阶段进展仍需更新，但只重取新指针对应的活动分页。
    await qc.refetchQueries({ predicate: query => currentPages.predicate(query) && latestVersions.has(String(query.queryKey[3])), type: "active" });
    return Promise.all(["run", "checklist", "export-attempt", "timeline"]
      .map((key) => qc.invalidateQueries({ queryKey: [key, gen, view.run_id] })));
  }, [qc, gen, view]);

  return useSequencedStream({
    kind: "run",
    path: cursor && ["queued", "processing", "exporting"].includes(view?.run_status ?? "") ? `/runs/${cursor.run_id}/events/stream` : null,
    snapshotSeq: cursor?.last_seq,
    onEvent: invalidate,
    refetch: invalidate,
  });
}

/** 写命令：同一用户意图使用稳定幂等键，409 交由调用方进入差异界面。 */
export function useReviewCommand<TArgs, TResult>(fn: (args: TArgs, key: string) => Promise<TResult>, onDone?: (r: TResult) => void) {
  const [key] = useState(() => newIdempotencyKey());
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: TArgs) => fn(args, key),
    onSuccess: async (r) => {
      const versioned = ["fields", "line-items", "issues", "translations", "checks"];
      await Promise.all(versioned.map(key => qc.cancelQueries({ queryKey: [key] })));
      // 只标记旧数据过期；先取得新的复核版本，分页 hook 换键后再读取。
      await Promise.all(versioned.map(key => qc.invalidateQueries({ queryKey: [key], refetchType: "none" })));
      await qc.invalidateQueries({ queryKey: ["review-view"] });
      await Promise.all(["rule-inputs", "checklist", "run", "w-run", "m-runs", "m-case", "m-summary"].map(key => qc.invalidateQueries({ queryKey: [key] })));
      onDone?.(r);
    },
  });
}

export function isConflict(e: unknown) {
  return isApiError(e) && e.status === 409;
}

/** 按当前所选文件在树里查出逻辑单据与区段。 */
export function useSelectedFile(view: ReviewView | undefined, documentId: string | null) {
  return useMemo(() => {
    if (!view?.files.length) return null;
    return view.files.find((f) => f.source.document_id === documentId) ?? view.files[0];
  }, [view, documentId]);
}
