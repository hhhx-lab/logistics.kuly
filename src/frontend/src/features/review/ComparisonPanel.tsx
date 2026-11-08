import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { consignmentApi, scopePreconditions, type ComparisonRow, type ComparisonSource } from "../../api/consignment";
import { errorText, isApiError } from "../../api/client";
import { workflowApi } from "../../api/workflow";
import { reviewApi } from "../../api/review";
import type { ReviewView, RunView } from "../../api/types";
import { useSession } from "../../stores/session";
import { UiText } from "../../i18n/runtime";
import { ErrorBar, Loading, StatusPill, formatTime } from "../../components/Status";
import { useCommand } from "../conversation/hooks";
import { SourceViewer, type AnchorBox } from "./SourceViewer";
import { ComparisonMatrix } from "./ComparisonMatrix";
import { CargoMatchReview } from "./CargoMatchReview";
import { ComparisonScopeEditor } from "./ComparisonScopeEditor";
import { RelationReview } from "./RelationReview";

const STATES: Record<string, string> = { building: "正在核对", processing: "正在核对", pending: "核对未执行", associating: "正在关联单证", awaiting_relation: "关联不明", completed: "核对完成", succeeded: "核对完成", checked: "核对完成", failed: "当前批次失败", cancelled: "已取消" };
const FILTERS = [["open", "未解决差异"], ["all", "全部"], ["normal", "正常差异"], ["relations", "待关联"], ["done", "历史已处理"]] as const;

export function ComparisonPanel({ view, run, width, onOpenIssue, onUploadMore, onOpenRun }: { view: ReviewView; run?: RunView; width: number; onOpenIssue: (id: string) => void; onUploadMore?: () => void; onOpenRun?: (runId: string) => void }) {
  const generation = useSession(state => state.generation);
  const canRevise = useSession(state => !!state.identity?.permissions.includes("review_revise"));
  const qc = useQueryClient();
  const history = view.mode === "historical";
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("open");
  const [severity, setSeverity] = useState("");
  const [selected, setSelected] = useState<{ row: ComparisonRow; source: ComparisonSource; token: number } | null>(null);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [scopeEditing, setScopeEditing] = useState(false);
  const [queuedRunId, setQueuedRunId] = useState<string | null>(null);
  const active = ["queued", "processing", "awaiting_input"].includes(run?.status ?? "");
  const locked = ["queued", "processing", "awaiting_input", "paused_reconcile", "exporting"].includes(run?.status ?? view.run_status ?? "");
  const baseKey = ["consignment", generation, view.case_id];
  const pendingRun = useQuery({ queryKey: ["w-run", generation, queuedRunId], queryFn: () => workflowApi.run(queuedRunId!), enabled: !!queuedRunId, retry: false, refetchInterval: query => ["queued", "processing", "awaiting_input"].includes(query.state.data?.status ?? "queued") ? 2500 : false });
  useEffect(() => {
    if (!pendingRun.data || ["queued", "processing", "awaiting_input"].includes(pendingRun.data.status)) return;
    if (run?.workflow_type === "consignment_check" && onOpenRun && pendingRun.data.status === "checked") {
      onOpenRun(pendingRun.data.run_id);
    }
    setQueuedRunId(null);
    void Promise.all([
      qc.invalidateQueries({ queryKey: baseKey }),
      qc.invalidateQueries({ queryKey: ["review-view", generation, view.case_id] }),
      qc.invalidateQueries({ queryKey: ["issues", generation, view.case_id] }),
      qc.invalidateQueries({ queryKey: ["w-run", generation, view.run_id] }),
      qc.invalidateQueries({ queryKey: ["run", generation, view.run_id] }),
    ]);
  }, [pendingRun.data?.status, run?.workflow_type, onOpenRun, qc, generation, view.case_id, view.run_id]);
  const batches = useInfiniteQuery({ queryKey: [...baseKey, "batches", view.run_id, view.snapshot_id, view.review_state_version], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => consignmentApi.batches(view.case_id, { run_id: view.run_id, snapshot_id: view.snapshot_id, cursor: pageParam }, signal),
    getNextPageParam: page => page.next_cursor ?? undefined, retry: false, refetchInterval: !history && (active || queuedRunId) ? 2500 : false });
  const batchItems = (batches.data?.pages ?? []).flatMap(page => page.items);
  const batch = batchItems.find(item => item.batch_id === selectedBatchId) ?? batchItems[0];
  const scope = useQuery({ queryKey: [...baseKey, "scope", history ? batch?.scope_revision_id : "current", view.review_state_version], queryFn: ({ signal }) => consignmentApi.scope(view.case_id, history ? batch?.scope_revision_id : null, signal), enabled: !history || !!batch, retry: false });
  const rowsKey = [...baseKey, "rows", batch?.batch_id, view.snapshot_id, filter, severity, view.snapshot_id ? null : scope.data?.review_state_version ?? view.review_state_version];
  const rows = useInfiniteQuery({ queryKey: rowsKey, initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => consignmentApi.rows(view.case_id, batch!.batch_id, { status: filter === "relations" ? "pending_relation" : filter, severity: filter === "relations" ? undefined : severity || undefined, cursor: pageParam, snapshot_id: view.snapshot_id ?? undefined, review_state_version: view.snapshot_id ? undefined : scope.data?.review_state_version ?? view.review_state_version }, signal),
    getNextPageParam: page => page.next_cursor ?? undefined, enabled: !!batch && ["completed", "succeeded", "checked"].includes(batch.status), retry: false });
  const relations = useInfiniteQuery({ queryKey: [...baseKey, "relations", scope.data?.scope_revision_id, batch?.batch_id, view.review_state_version], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => consignmentApi.relations(view.case_id, { scope_revision_id: scope.data?.scope_revision_id, batch_id: batch?.batch_id, state: "candidate", cursor: pageParam }, signal),
    getNextPageParam: page => page.next_cursor ?? undefined, enabled: !!scope.data && (!history || !!batch), retry: false });
  const recompute = useCommand<void, { run_id: string | null }>((_args, key) => {
    if (!scope.data) return Promise.reject(new Error("The current case scope could not be loaded."));
    return consignmentApi.recompute(view.case_id, { ...scopePreconditions(scope.data), previous_batch_id: batch?.batch_id ?? null }, key);
  }, ["consignment", "review-view", "issues", "w-run", "c-conv", "m-runs"]);
  const staleHandled = useRef(false);
  useEffect(() => {
    if (!isApiError(rows.error, "PAGE_STALE")) { staleHandled.current = false; return; }
    if (staleHandled.current) return; staleHandled.current = true;
    void qc.invalidateQueries({ queryKey: [...baseKey, "scope"] });
    void qc.invalidateQueries({ queryKey: ["review-view", generation, view.case_id] });
  }, [rows.error, qc, generation, view.case_id]);
  useEffect(() => { setSelected(null); setSelectedBatchId(null); setQueuedRunId(null); }, [generation, view.case_id, view.run_id, view.snapshot_id]);
  useEffect(() => { if (queuedRunId && batchItems.some(item => item.run_id === queuedRunId && ["completed", "failed", "cancelled"].includes(item.status))) setQueuedRunId(null); }, [queuedRunId, batchItems]);
  const rowItems = (rows.data?.pages ?? []).flatMap(page => page.items);
  const pendingRelations = (relations.data?.pages ?? []).flatMap(page => page.items);
  const cargoCandidates = [...new Map(rowItems.filter(row => row.group_id && row.status === "undetermined").map(row => [row.group_id, row])).values()];
  const editable = canRevise && !locked && !history && (!batch || batch.current) && !batch?.stale;
  const pickEvidence = (row: ComparisonRow, source: ComparisonSource) => { setSelected(value => ({ row, source, token: (value?.token ?? 0) + 1 })); setEvidenceOpen(true); };
  const onChanged = (runId: string | null) => {
    if (runId && run?.workflow_type === "consignment_check" && onOpenRun) { onOpenRun(runId); return; }
    setQueuedRunId(runId); setSelectedBatchId(null); void qc.invalidateQueries({ queryKey: baseKey });
  };
  useEffect(() => { setSelected(null); }, [batch?.batch_id]);
  useEffect(() => { if (locked) setScopeEditing(false); }, [locked]);
  const noComparableObjects = batch?.summary.rules_status === "no_comparable_objects";
  const completed = !!batch && ["completed", "succeeded", "checked"].includes(batch.status);
  const stale = !!batch?.stale || !!scope.data?.stale;
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line p-2 text-[12px]">
      <StatusPill tone={stale ? "warn" : batch?.status === "failed" ? "risk" : "info"} busy={!!batch && ["building", "processing", "associating"].includes(batch.status)}><UiText value={queuedRunId ? "重核对中" : stale ? "新版本待重算" : noComparableObjects ? "无可比较对象" : batch ? STATES[batch.status] ?? "核对未执行" : active ? "解析中" : "核对未执行"} /></StatusPill>
      {batch && <span className="text-ink-3">{formatTime(batch.created_at)}</span>}
      <span className="flex-1" />
      {onUploadMore && !history && <button type="button" className="btn btn-ghost btn-sm" disabled={locked} onClick={onUploadMore}>上传补充材料</button>}
      {!history && <button type="button" className="btn btn-secondary btn-sm" disabled={!canRevise || !scope.data || recompute.isPending || locked || !!queuedRunId} onClick={() => recompute.mutate(undefined, { onSuccess: result => onChanged(result.run_id) })}>{recompute.isPending || queuedRunId ? "重核对中" : "重新核对"}</button>}
    </div>
    {stale && <div className="shrink-0 border-b border-line px-3 py-2 text-[12px] text-warn">核对依据已变化，上次结果仅供参考</div>}
    {(scope.error || batches.error || recompute.error || pendingRun.error) && <div className="shrink-0 p-3"><ErrorBar text={errorText(scope.error ?? batches.error ?? recompute.error ?? pendingRun.error)} action={{ label: "重新加载", onSelect: () => { void scope.refetch(); void batches.refetch(); if (queuedRunId) void pendingRun.refetch(); } }} /></div>}
    {batchItems.length > 1 && <select className="input m-2 max-w-[320px] shrink-0" aria-label="比较批次" value={batch?.batch_id} onChange={event => { setSelectedBatchId(event.target.value); setSelected(null); }}>{batchItems.map(item => <option key={item.batch_id} value={item.batch_id}>{formatTime(item.created_at)} · {STATES[item.status] ?? "核对未执行"}{item.current ? " · 当前" : " · 历史"}</option>)}</select>}
    {batches.hasNextPage && <button type="button" className="btn btn-ghost btn-sm shrink-0" onClick={() => void batches.fetchNextPage()} disabled={batches.isFetchingNextPage}>加载更多批次</button>}
    <details className="shrink-0 border-b border-line px-3 py-2 text-[12px] text-ink-2"><summary className="cursor-pointer">材料与核对范围</summary><div className="max-h-40 overflow-y-auto pt-2 scroll-thin">
      {scope.data?.payload.materials.map(material => <p key={material.document_id} className="break-words">{view.files.find(file => file.source.document_id === material.document_id)?.source.filename ?? "未加载的关联单据"} · v{material.document_version}{material.purpose === "excluded" ? " · 已排除" : ""}</p>)}
      <p className="mt-2"><UiText value={scope.data?.payload.completeness === "complete" ? "完整材料集合" : scope.data?.payload.completeness === "partial" ? "部分材料集合" : "材料完整性待确认"} /></p>
      {!scope.data && <p>正在读取材料范围</p>}
      {!history && scope.data && (scopeEditing ? <ComparisonScopeEditor scope={scope.data} view={view} onClose={() => setScopeEditing(false)} onSaved={() => { void qc.invalidateQueries({ queryKey: baseKey }); }} /> : <button type="button" className="btn btn-ghost btn-sm mt-2" disabled={!canRevise || locked} onClick={() => setScopeEditing(true)}>修改核对范围</button>)}
    </div></details>
    <div className="flex shrink-0 flex-wrap items-center gap-1 border-b border-line p-2">{FILTERS.map(([key, label]) => <button key={key} type="button" className={`btn btn-sm ${filter === key ? "btn-secondary" : "btn-ghost"}`} aria-pressed={filter === key} onClick={() => setFilter(key)}><UiText value={label} />{key === "relations" && pendingRelations.length ? ` (${pendingRelations.length})` : ""}</button>)}<select className="input ml-auto max-w-[150px] text-[12px]" aria-label="差异风险筛选" value={severity} onChange={event => setSeverity(event.target.value)}><option value="">全部风险等级</option><option value="red">红项</option><option value="yellow">黄项</option><option value="unknown">无法判断</option></select></div>
    <div className={`flex min-h-0 flex-1 ${width < 920 ? "flex-col" : "flex-row"}`}>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {filter === "relations" ? <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">{relations.isPending ? <Loading /> : relations.error ? <ErrorBar text={errorText(relations.error)} /> : <><RelationReview items={pendingRelations} view={view} scope={scope.data} editable={editable} onChanged={onChanged} />{cargoCandidates.length > 0 && <div className="space-y-2 p-3 pt-0">{cargoCandidates.map(row => <CargoMatchReview key={row.group_id} row={row} scope={scope.data} editable={editable} onChanged={onChanged} onLocate={pickEvidence} />)}</div>}{rows.error && <div className="p-3"><ErrorBar text={errorText(rows.error)} /></div>}{rows.hasNextPage && <button type="button" className="btn btn-secondary btn-sm m-3" disabled={rows.isFetchingNextPage} onClick={() => void rows.fetchNextPage()}>加载更多比较项</button>}</>}{relations.hasNextPage && <button type="button" className="btn btn-secondary btn-sm m-3" disabled={relations.isFetchingNextPage} onClick={() => void relations.fetchNextPage()}>加载更多关联</button>}</div> : completed ? <>
          {rows.error ? <div className="p-3"><ErrorBar text={errorText(rows.error)} action={{ label: "重新加载", onSelect: () => { void scope.refetch(); void rows.refetch(); } }} /></div> : rows.isPending ? <Loading /> : rowItems.length ? <ComparisonMatrix rows={rowItems} view={view} width={width} selectedRowId={selected?.row.row_id ?? null} onSelectEvidence={pickEvidence} onOpenIssue={onOpenIssue} canOpenIssue={history || !!batch?.current} /> : <div className="p-4 text-[13px] text-ink-2"><UiText value={noComparableObjects ? "没有可比较的字段或明细，请补充同票单证或确认对应范围" : filter === "open" ? "当前筛选下没有未解决差异" : "当前筛选下没有比较结果"} /></div>}
          {rows.hasNextPage && <button type="button" className="btn btn-secondary btn-sm m-2 self-start" disabled={rows.isFetchingNextPage} onClick={() => void rows.fetchNextPage()}>加载更多比较项</button>}
        </> : <div className="p-4 text-[13px] text-ink-2"><UiText value={batches.isPending ? "正在读取核对结果" : batch?.status === "failed" ? "当前核对失败，请在处理记录查看原因" : batch ? "结果将在本次核对完成后展示" : history ? "这份历史版本没有冻结的同票核对结果" : (scope.data?.payload.materials.filter(item => item.purpose === "active").length ?? 0) < 2 ? "无可比较对象，请上传同票的另一份单证" : "核对尚未执行，请明确范围后启动同票核对"} /></div>}
      </div>
      {selected && evidenceOpen && <div className={`flex min-h-0 min-w-0 flex-col border-line ${width < 920 ? "h-[50%] shrink-0 border-t" : "w-[43%] border-l"}`}><ComparisonEvidence key={`${selected.source.target.document_id}:${selected.source.target.parse_artifact_id}`} view={view} selection={selected} onClose={() => setEvidenceOpen(false)} /></div>}
    </div>
  </div>;
}
function ComparisonEvidence({ view, selection, onClose }: { view: ReviewView; selection: { row: ComparisonRow; source: ComparisonSource; token: number }; onClose: () => void }) {
  const generation = useSession(state => state.generation);
  const source = selection.source;
  const resolution = useQuery({ queryKey: ["evidence", generation, source.target], queryFn: () => reviewApi.resolve(source.target), retry: false });
  const inViewFile = view.files.find(file => file.source.document_id === source.target.document_id && file.source.document_version === source.target.document_version && (!source.target.parse_artifact_id || file.parse_artifact_id === source.target.parse_artifact_id));
  const evidenceView = useQuery({ queryKey: ["comparison-evidence-view", generation, source.target], queryFn: () => reviewApi.view(view.case_id, { document_id: source.target.document_id ?? undefined, parse_artifact_id: source.target.parse_artifact_id ?? undefined, business_revision_id: source.target.business_revision_id ?? undefined }), enabled: resolution.isSuccess && !inViewFile, retry: false });
  const file = inViewFile ?? evidenceView.data?.files.find(file => file.source.document_id === source.target.document_id && file.source.document_version === source.target.document_version && (!source.target.parse_artifact_id || file.parse_artifact_id === source.target.parse_artifact_id));
  const [flash, setFlash] = useState(true);
  useEffect(() => { setFlash(true); if (!resolution.data) return; const timer = window.setTimeout(() => setFlash(false), 2000); return () => window.clearTimeout(timer); }, [selection.token, resolution.data]);
  const id = resolution.data?.normalized_target.source_segment_id ?? resolution.data?.normalized_target.source_anchor_id ?? source.target.source_segment_id ?? source.target.source_anchor_id ?? "comparison-evidence";
  const anchors = useMemo<AnchorBox[]>(() => resolution.data && flash ? [{ id, page_no: resolution.data.page_no ?? 1, ...(resolution.data.anchor_precision === "rect" && resolution.data.coord_system === "page_normalized_top_left" && resolution.data.bbox ? { rect: resolution.data.bbox as [number, number, number, number] } : {}), tone: "brand", label: "来源位置" }] : [], [resolution.data, flash, id]);
  return <>
    <header className="flex shrink-0 items-center gap-2 border-b border-line p-2"><span className="min-w-0 flex-1 truncate text-[12px]">{file?.source.filename}</span><button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>收起证据</button></header>
    {resolution.isPending || resolution.isSuccess && !inViewFile && evidenceView.isPending ? <Loading text="正在定位来源" /> : resolution.error || evidenceView.error ? <div className="p-3"><ErrorBar text={errorText(resolution.error ?? evidenceView.error)} /></div> : !file ? <div className="p-3"><ErrorBar text="The evidence file is outside the frozen review scope. Reload the matching review version." /></div> : <SourceViewer file={file} resolution={resolution.data} targetPage={resolution.data?.page_no} navigationToken={selection.token} anchors={anchors} selectedAnchorId={flash ? id : null} highlightDuration={2000} compact />}
  </>;
}
