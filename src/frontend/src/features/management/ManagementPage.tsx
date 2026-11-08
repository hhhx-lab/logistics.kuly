// 单据管理台。标题、页签、筛选、表头固定，列表独立滚动；
// 筛选、展开行、所选快照与滚动位置保存在当前身份内存，从复核区返回可恢复。
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { newIdempotencyKey } from "../../api/client";
import { conversationApi } from "../../api/conversation";
import type { EvidenceTarget, ManagedRunRow, ReferenceSelection } from "../../api/types";
import { workflowApi } from "../../api/workflow";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, Loading, shortId } from "../../components/Status";
import { ReviewHost } from "../review/ReviewHost";
import type { ReviewTargetSpec } from "../review/ReviewWorkspace";
import { AppShell } from "../shell/AppShell";
import { CaseDeletionDialog, DeletionStatusCard } from "./CaseDeletionDialog";
import { BulkCaseDeletionDialog } from "./BulkCaseDeletionDialog";
import { useCaseSelection, type SelectableCase } from "./CaseSelection";
import { FilterBar, StatusTabs } from "./FilterBar";
import { OpsRunDrawer } from "./OpsRunDrawer";
import { RunDetails, type RunDetailsState } from "./RunDetails";
import { RunList } from "./RunList";
import { UploadDrafts } from "./UploadDrafts";
import { DEFAULT_FILTER, activeChips, useDeletionStatus, useFacets, usePermissions, useRunList, type ManagementFilter } from "./hooks";
import { useSession } from "../../stores/session";

type ReturnFocus = { key?: string; label: string; text: string };
const pagePositions=new Map<number,{filter:ManagementFilter;expandedId:string|null;deliveryFor:string|null;scrollTop:number;details:Map<string,RunDetailsState>;focus?:ReturnFocus}>();
useSession.subscribe((state, previous) => { if (state.generation !== previous.generation) pagePositions.clear(); });

export function ManagementPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedScope = { case_id: searchParams.get("case_id") ?? "", run_id: searchParams.get("run_id") ?? "", snapshot_id: searchParams.get("snapshot_id") ?? "" };
  const hasLinkedScope = !!(linkedScope.case_id || linkedScope.run_id || linkedScope.snapshot_id);
  const linkedKey = JSON.stringify(linkedScope);
  const toast = useToast();
  const qc = useQueryClient();
  const generation=useSession(s=>s.generation);
  pagePositions.forEach((_value,key)=>{if(key!==generation)pagePositions.delete(key);});
  const restored=useRef(hasLinkedScope ? undefined : pagePositions.get(generation));
  const { canReconcile, canMigrate } = usePermissions();

  const [filter, setFilter] = useState<ManagementFilter>(()=>hasLinkedScope ? {...DEFAULT_FILTER,...linkedScope,tab:"all",date_type:"activity",sort:"activity_desc"} : restored.current?.filter??DEFAULT_FILTER);
  const [expandedId, setExpandedId] = useState<string | null>(()=>hasLinkedScope ? linkedScope.run_id || null : restored.current?.expandedId??null);
  const [deliveryFor, setDeliveryFor] = useState<string | null>(()=>hasLinkedScope && linkedScope.snapshot_id ? linkedScope.run_id || null : restored.current?.deliveryFor??null);
  const lastLinkedKey = useRef(linkedKey);
  useEffect(() => {
    if (lastLinkedKey.current === linkedKey) return;
    lastLinkedKey.current = linkedKey;
    if (!hasLinkedScope) return;
    restored.current = undefined;
    setFilter({...DEFAULT_FILTER,...linkedScope,tab:"all",date_type:"activity",sort:"activity_desc"});
    setExpandedId(linkedScope.run_id || null);
    setDeliveryFor(linkedScope.snapshot_id ? linkedScope.run_id || null : null);
  }, [linkedKey, hasLinkedScope]);
  const changeFilter = (next: ManagementFilter) => {
    setFilter(next);
    lastLinkedKey.current = JSON.stringify({case_id:next.case_id,run_id:next.run_id,snapshot_id:next.snapshot_id});
    const params = new URLSearchParams(searchParams);
    for (const key of ["case_id", "run_id", "snapshot_id"] as const) next[key] ? params.set(key,next[key]) : params.delete(key);
    if (params.toString() !== searchParams.toString()) setSearchParams(params, {replace:true});
  };
  const [batchMode, setBatchMode] = useState(false);
  const selection = useCaseSelection(JSON.stringify([generation, filter]), batchMode);
  useEffect(() => setBatchMode(false), [generation, filter]);
  const [deleteCase, setDeleteCase] = useState<SelectableCase | null>(null);
  const [bulkCases, setBulkCases] = useState<SelectableCase[] | null>(null);
  const [receipts, setReceipts] = useState<string[]>([]);
  const onDeleted = (caseId: string, id: string) => {
    setReceipts(old => [...new Set([...old, id])]);
    setReviewSpec(old => old?.caseId === caseId ? null : old);
    selection.clear();
    useSession.getState().clearEvidence();
  };
  const [ops, setOps] = useState<{ runId: string; stepId: string | null; tab: "reconcile" | "migrate" } | null>(null);
  const [deletionId, setDeletionId] = useState<string | null>(() => {
    const id = sessionStorage.getItem("pendingDeletion");
    if (id) sessionStorage.removeItem("pendingDeletion");
    return id;
  });
  const [reviewSpec, setReviewSpec] = useState<ReviewTargetSpec | null>(null);
  const evidenceRequest = useSession(s => s.evidenceRequest);
  useEffect(() => { if (!evidenceRequest) return; const target=evidenceRequest.target; setReviewSpec({caseId:target.case_id,runId:target.run_id??null,snapshotId:target.export_snapshot_id??null,target,intent:evidenceRequest.intent==="review"?"full":"quick"}); useSession.getState().clearEvidence(); }, [evidenceRequest]);
  const [cancelRow, setCancelRow] = useState<ManagedRunRow | null>(null);

  const listRef = useRef<HTMLDivElement>(null);
  const position=useRef({filter,expandedId,deliveryFor,scrollTop:restored.current?.scrollTop??0,details:restored.current?.details??new Map<string,RunDetailsState>(),focus:restored.current?.focus});
  position.current={...position.current,filter,expandedId,deliveryFor};
  useEffect(()=>()=>{if(generation===useSession.getState().generation)pagePositions.set(generation,position.current);},[generation]);
  const [narrow, setNarrow] = useState(false);
  // 用实测可用宽度决定表格还是卡片，不按设备名猜断点
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => setNarrow(entries[0].contentRect.width < 1144));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const list = useRunList(filter);
  const restorePosition = useCallback(() => {
    const saved = restored.current, root = listRef.current;
    if (!saved || !root) return;
    const target = saved.focus && [...root.querySelectorAll<HTMLElement>("button,a")].find(el => saved.focus?.key
      ? el.dataset.managementFocus === saved.focus.key
      : (el.getAttribute("aria-label") ?? "") === saved.focus?.label && (el.textContent ?? "").trim() === saved.focus?.text);
    root.scrollTop = saved.scrollTop;
    if (saved.focus && !target) return;
    target?.focus({ preventScroll: true });
    restored.current = undefined;
  }, []);
  useLayoutEffect(()=>{if(list.items.length)restorePosition();},[list.items.length,restorePosition]);
  const rememberReturn = useCallback(() => {
    const active = document.activeElement;
    position.current.scrollTop = listRef.current?.scrollTop ?? position.current.scrollTop;
    position.current.focus = active instanceof HTMLElement ? { key: active.dataset.managementFocus, label: active.getAttribute("aria-label") ?? "", text: (active.textContent ?? "").trim() } : undefined;
  }, []);
  const facets = useFacets(filter);
  const deletion = useDeletionStatus(deletionId);

  const isDrafts = filter.tab === "drafts";
  const hasCondition = useMemo(
    () => filter.case_id || filter.run_id || filter.snapshot_id || filter.q.trim() || filter.local_date_from || filter.local_date_to || filter.origin_value || filter.recipient_value || filter.actor_value || filter.doc_types.length || filter.formats.length || filter.languages.length || filter.risks.length || filter.only_confirmed,
    [filter],
  );

  /** 打开校验区：历史行锁定该行实际展示的快照，未导出行用当前工作版。 */
  const openReview = useCallback(
    (row: ManagedRunRow, useWorking = false) => {
      const snapshotId = useWorking ? null : row.displayed_snapshot_id ?? null;
      rememberReturn();
      setReviewSpec({ caseId: row.case_id, runId: row.run_id, snapshotId, intent: "full" });
    },
    [rememberReturn],
  );

  const openEvidence = useCallback(
    (target: EvidenceTarget, snapshotId?: string | null) => {
      rememberReturn();
      setReviewSpec({ caseId: target.case_id, runId: target.run_id ?? null, snapshotId: snapshotId ?? target.export_snapshot_id ?? null, target, intent: "quick" });
    },
    [rememberReturn],
  );

  /** 新会话并预置受控引用：已导出固定快照，未导出固定当前处理。 */
  const askAssistant = useCallback(
    async (refs: ReferenceSelection[], note: string) => {
      try {
        const conv = await conversationApi.create({ initial_reference_selections: refs, entry_source: "management" }, newIdempotencyKey());
        navigate(`/conversation/${conv.conversation_id}`);
        toast({ tone: "ok", text: note });
      } catch {
        toast({ tone: "risk", text: "新建会话失败，请稍后重试" });
      }
    },
    [navigate, toast],
  );

  const actions = {
    onDeleteCase: (row: ManagedRunRow) => setDeleteCase({caseId: row.case_id, name: row.case_name}),
    onOpenReview: openReview,
    onOpenCase: (caseId: string) => navigate(`/documents/cases/${caseId}`),
    onViewDelivery: (row: ManagedRunRow) => {
      setExpandedId(row.run_id);
      setDeliveryFor(row.run_id);
    },
    onRetryExport: (row: ManagedRunRow) => openReview(row, true),
    onReprocess: (row: ManagedRunRow) => {
      // 重新处理只预选单票与文件，目标语言与范围回到对话重新确认
      navigate(`/conversation?case_id=${row.case_id}&reprocess=1`);
    },
    onCancel: (row: ManagedRunRow) => setCancelRow(row),
    onAskAssistant: (row: ManagedRunRow) => {
      const refs: ReferenceSelection[] = row.displayed_snapshot_id
        ? [{ kind: "snapshot", id: row.displayed_snapshot_id, label: `交付快照 ${shortId(row.displayed_snapshot_id)}`, selection_mode: "fixed", case_id: row.case_id, snapshot_id: row.displayed_snapshot_id, run_id: row.run_id }]
        : [{ kind: "run", id: row.run_id, label: `本次处理 ${shortId(row.run_id)}`, selection_mode: "current", case_id: row.case_id, run_id: row.run_id }];
      void askAssistant(refs, row.displayed_snapshot_id ? "已新建会话并固定这份交付快照" : "已新建会话并关联这次处理");
    },
  };

  const askAboutDocument = (row: ManagedRunRow, documentId: string, documentVersion: number) => {
    const refs: ReferenceSelection[] = [];
    if (row.displayed_snapshot_id) {
      refs.push({ kind: "snapshot", id: row.displayed_snapshot_id, label: `交付快照 ${shortId(row.displayed_snapshot_id)}`, selection_mode: "fixed", case_id: row.case_id, snapshot_id: row.displayed_snapshot_id, run_id: row.run_id });
    } else {
      refs.push({ kind: "run", id: row.run_id, label: `本次处理 ${shortId(row.run_id)}`, selection_mode: "current", case_id: row.case_id, run_id: row.run_id });
    }
    refs.push({ kind: "document", id: documentId, label: "这张原件", selection_mode: "fixed", case_id: row.case_id, document_id: documentId, document_version: documentVersion });
    void askAssistant(refs, row.displayed_snapshot_id ? "已新建会话，并固定这份历史快照与其中这张原件" : "已新建会话并固定这张原件");
  };

  return (
    <AppShell>
      <div className="flex min-h-0 flex-1 flex-col">
        <header className="flex min-h-16 shrink-0 items-center gap-3 px-8">
          <div className="min-w-0 flex-1">
            <h1 className="text-title font-semibold text-ink">单据管理台</h1>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => {
              // 只失效当前可见资源，保留筛选、游标与展开状态
              ["m-runs", "m-facets", "m-uploads", "m-summary", "m-timeline", "m-snapshots"].forEach((k) => void qc.invalidateQueries({ queryKey: [k] }));
            }}
          >
            <Icon name="retry" size={14} />
            刷新
          </button>
          <button type="button" className={`btn ${batchMode ? "btn-secondary" : "btn-ghost"}`} aria-pressed={batchMode}
            onClick={() => { selection.clear(); setBatchMode((current) => !current); }}>
            {batchMode ? "退出批量处理" : "批量处理"}
          </button>
          <button type="button" className="btn btn-primary btn-lg" onClick={() => navigate("/conversation?upload=1")}>
            <Icon name="plus" size={16} />
            新建处理
          </button>
        </header>

        <div className="px-8">
          <StatusTabs tab={filter.tab} counts={facets.data?.status_counts} onTab={(tab) => { setFilter({ ...filter, tab }); setExpandedId(null); }} />
        </div>

        {!isDrafts && (
          <div className="px-8">
            <FilterBar filter={filter} onChange={(f) => { changeFilter(f); setExpandedId(null); }} />
          </div>
        )}

        <div ref={listRef} onScroll={e=>{position.current.scrollTop=e.currentTarget.scrollTop;}} className="min-h-0 flex-1 overflow-auto scroll-thin px-8 py-3">
          <div className="mx-auto max-w-manage">
            {receipts.map(id => <DeletionReceipt key={id} id={id} onDismiss={() => setReceipts(old => old.filter(value => value !== id))} />)}
            {deletion.data && <DeletionStatusCard status={deletion.data.status} onDismiss={() => setDeletionId(null)} />}
            {list.error && <ErrorBar text="读取记录失败，可重试这一页。" action={{ label: "重试", onSelect: () => void list.refetch() }} />}

            {isDrafts ? (
              <UploadDrafts selection={selection} onDelete={setDeleteCase} onBulkDelete={() => setBulkCases([...selection.selected.values()])} onStartRun={(caseId) => navigate(`/conversation?case_id=${caseId}`)} />
            ) : (
              <RunList
                selection={selection}
                onBulkDelete={() => setBulkCases([...selection.selected.values()])}
                items={list.items}
                dateType={filter.date_type}
                narrow={narrow}
                sort={filter.sort}
                onSort={(sort) => setFilter({ ...filter, sort })}
                isPending={list.isPending}
                hasNextPage={!!list.hasNextPage}
                isFetchingNextPage={list.isFetchingNextPage}
                onLoadMore={() => void list.fetchNextPage()}
                expandedId={expandedId}
                onExpand={(id) => {
                  setExpandedId(id);
                  if (!id) setDeliveryFor(null);
                }}
                actions={actions}
                emptyKind={list.summaryStale ? "stale_index" : hasCondition ? "filtered" : "never"}
                onClearFilter={() => changeFilter({ ...DEFAULT_FILTER, tab: filter.tab })}
                chips={activeChips(filter)}
                onNewRun={() => navigate("/conversation?upload=1")}
                renderDetails={(row) => (
                  <RunDetails
                    key={`${row.run_id}:${row.displayed_snapshot_id ?? "working"}`}
                    row={row}
                    savedState={position.current.details.get(`${row.run_id}:${row.displayed_snapshot_id ?? "working"}`)}
                    onStateChange={state => position.current.details.set(`${row.run_id}:${row.displayed_snapshot_id ?? "working"}`,state)}
                    onLayoutReady={restorePosition}
                    openDelivery={deliveryFor === row.run_id}
                    onOpenEvidence={openEvidence}
                    onAskAboutDocument={askAboutDocument}
                    canReconcile={canReconcile}
                    canMigrate={canMigrate}
                    onOpenOps={(stepId, runId) => setOps({ runId, stepId, tab: "reconcile" })}
                    onOpenMigration={(runId) => setOps({ runId, stepId: null, tab: "migrate" })}
                  />
                )}
              />
            )}
          </div>
        </div>
      </div>

      {deleteCase && <CaseDeletionDialog caseId={deleteCase.caseId} caseName={deleteCase.name} onClose={() => setDeleteCase(null)} onQueued={id => { onDeleted(deleteCase.caseId, id); setDeleteCase(null); }} />}
      {bulkCases && <BulkCaseDeletionDialog cases={bulkCases} onClose={() => setBulkCases(null)} onQueued={onDeleted} />}
      {reviewSpec && <ReviewHost spec={reviewSpec} onClose={() => setReviewSpec(null)} />}
      {ops && <OpsRunDrawer runId={ops.runId} stepId={ops.stepId} canReconcile={canReconcile} canMigrate={canMigrate} initialTab={ops.tab} onClose={() => setOps(null)} />}
      {cancelRow && <CancelDialog row={cancelRow} onClose={() => setCancelRow(null)} />}
    </AppShell>
  );
}

/** 取消处理：确认层说明不删除原件与历史快照。 */
function CancelDialog({ row, onClose }: { row: ManagedRunRow; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [pending, setPending] = useState(false);
  const queued = row.allowed_actions.includes("cancel");
  return (
    <Dialog
      open
      onClose={onClose}
      title={queued ? "取消这次处理" : "请求取消这次处理"}
      description={queued ? "这次处理还在排队，可以直接取消。" : "这次处理正在运行，会在当前步骤安全停止后取消。"}
      tone="risk"
      width={480}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            返回
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={pending}
            onClick={async () => {
              setPending(true);
              try {
                await workflowApi.cancelRun(row.run_id, newIdempotencyKey());
                await qc.invalidateQueries();
                toast({ tone: "ok", text: queued ? "已取消这次处理" : "已提交取消请求" });
                onClose();
              } catch {
                toast({ tone: "risk", text: "取消未成功，请稍后重试" });
              } finally {
                setPending(false);
              }
            }}
          >
            {pending ? "正在提交" : queued ? "取消处理" : "请求取消"}
          </button>
        </>
      }
    >
      <ul className="space-y-1 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
        <li>· 不会删除已上传的原件。</li>
        <li>· 不会影响此前已成功的交付快照。</li>
        <li>· 如果外部请求的受理状态暂时不明，会显示为待核查而不是直接写成已取消。</li>
      </ul>
    </Dialog>
  );
}

export { Loading };

function DeletionReceipt({ id, onDismiss }: { id: string; onDismiss: () => void }) {
  const q = useDeletionStatus(id);
  if (q.isError) return <ErrorBar text="删除回执暂时无法读取" action={{label:"重新读取",onSelect:()=>void q.refetch()}} />;
  return <DeletionStatusCard status={q.data?.status ?? "queued"} onDismiss={onDismiss} />;
}
