// 疑点区与检查清单。来源按钮负责定位，处置才打开表单；
// 已裁定的冲突同时保留原冲突与门禁解除，不合成一个绿色「通过」。
import { useEffect, useMemo, useRef, useState } from "react";
import type { EvidenceTarget, IssueView, ReviewView, RuleCheckView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { EmptyState, Loading, StatusPill, formatDateTime } from "../../components/Status";
import { useChecks, useIssues } from "./hooks";
import { errorText } from "../../api/client";
import { ErrorBar } from "../../components/Status";
import { RuleInputsPanel } from "./RuleInputsPanel";
import { useSession } from "../../stores/session";

export type IssueDisposition = "accept" | "adjudicate" | "mark-unresolved" | "not-applicable" | "ignore" | "apply-recommendation" | "request-revision";

const SEV: Record<string, { label: string; tone: "risk" | "warn" | "info" | "dis"; icon: "alert" | "warn" | "question" | "info"; bar: string }> = {
  blocking: { label: "阻断红项", tone: "risk", icon: "alert", bar: "bg-risk-line" },
  warning: { label: "待确认黄项", tone: "warn", icon: "warn", bar: "bg-warn-line" },
  undetermined: { label: "无法判断", tone: "info", icon: "question", bar: "bg-ink-3" },
  not_covered: { label: "规则未覆盖", tone: "dis", icon: "info", bar: "bg-dis" },
};

const STATUS_TEXT: Record<IssueView["status"], string> = {
  ignored: "已人工忽略",
  revised: "已修订，原问题留档",
  open: "待处置",
  accepted: "已接受并记录",
  adjudicated: "已裁定事实",
  unresolved: "已标记无法判断",
  not_applicable: "已标记不适用",
};

type Filter = "open" | "all" | "blocking" | "warning" | "undetermined" | "done";

export function IssuesPanel({
  view,
  editable,
  onLocate,
  onDispose,
  onEdit,
  onSupplement,
  canSupplement,
  selectedIssueId,
  documentFilter,
  onDocumentFilter,
  filter: externalFilter,
  onFilter,
  restoreScroll,
}: {
  view: ReviewView;
  editable: boolean;
  onLocate: (target: EvidenceTarget, issueId: string) => void;
  onDispose: (issue: IssueView, kind: IssueDisposition) => void;
  onEdit: (issue: IssueView, kind: "field" | "translation") => void;
  onSupplement: (issue: IssueView) => void;
  canSupplement: boolean;
  selectedIssueId?: string | null;
  filter?: Filter;
  onFilter?: (value: Filter) => void;
  documentFilter?: string | null;
  onDocumentFilter?: (id: string | null) => void;
  restoreScroll?: { token: number; issueId: string; top: number };
}) {
  const [localFilter, setLocalFilter] = useState<Filter>("open");
  const scrollRef = useRef<HTMLDivElement>(null);
  const locatedIssueRef = useRef<string | null>(null);
  const restoredTokenRef = useRef<number | null>(null);
  const filter = externalFilter ?? localFilter;
  const setFilter = onFilter ?? setLocalFilter;
  const q = useIssues(view, filter === "all" ? undefined : filter, documentFilter ?? undefined);
  const all = q.items;
  useEffect(() => {
    if (filter === "all" && selectedIssueId && !all.some(i => i.issue_id === selectedIssueId) && q.hasNextPage && !q.isFetching && !q.error) void q.fetchNextPage();
  }, [filter, selectedIssueId, all, q.hasNextPage, q.isFetching, q.error, q.fetchNextPage]);

  const items = useMemo(() => {
    const open = all.filter((i) => (i.status === "open" || i.status === "unresolved"));
    const done = all.filter((i) => (i.status !== "open" && i.status !== "unresolved"));
    const order = (i: IssueView) => (i.severity === "blocking" ? 0 : i.severity === "warning" ? 1 : i.severity === "undetermined" ? 2 : 3);
    const sorted = [...open.sort((a, b) => order(a) - order(b)), ...done];
    if (filter === "all") return sorted;
    if (filter === "open") return open.sort((a, b) => order(a) - order(b));
    if (filter === "done") return done;
    return open.filter((i) => i.severity === filter || filter === "undetermined" && i.status === "unresolved");
  }, [all, filter]);

  const localCounts = useMemo(
    () => ({
      blocking: all.filter((i) => i.severity === "blocking" && (i.status === "open" || i.status === "unresolved")).length,
      warning: all.filter((i) => i.severity === "warning" && (i.status === "open" || i.status === "unresolved")).length,
      undetermined: all.filter((i) => i.severity === "undetermined" && (i.status === "open" || i.status === "unresolved")).length,
      done: all.filter((i) => (i.status !== "open" && i.status !== "unresolved")).length,
    }),
    [all],
  );

  useEffect(() => {
    if (!selectedIssueId) { locatedIssueRef.current = null; return; }
    if (locatedIssueRef.current === selectedIssueId) return;
    const row = scrollRef.current?.querySelector(`[data-issue-id="${selectedIssueId}"]`);
    if (row) { row.scrollIntoView({ block: "nearest" }); locatedIssueRef.current = selectedIssueId; }
  }, [selectedIssueId, items]);
  useEffect(() => {
    if (!restoreScroll || q.isPending || restoredTokenRef.current === restoreScroll.token || !items.some(issue => issue.issue_id === restoreScroll.issueId)) return;
    const scroller = scrollRef.current;
    if (!scroller) return;
    const frame = window.requestAnimationFrame(() => {
      scroller.scrollTop = restoreScroll.top;
      restoredTokenRef.current = restoreScroll.token;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [restoreScroll, q.isPending, items]);
  const counts = q.data?.pages[0]?.counts ?? localCounts;
  const tabs: { key: Filter; label: string; n?: number }[] = [
    { key: "open", label: "未解决", n: q.data?.pages[0]?.counts ? (q.data.pages[0].counts.all ?? 0) - (q.data.pages[0].counts.done ?? 0) : undefined },
    { key: "blocking", label: "阻断红项", n: q.data ? counts.blocking : undefined },
    { key: "warning", label: "待确认黄项", n: q.data ? counts.warning : undefined },
    { key: "undetermined", label: "无法判断", n: q.data ? counts.undetermined : undefined },
    { key: "done", label: "已处置", n: q.data ? counts.done ?? undefined : undefined },
    { key: "all", label: "全部记录", n: q.data?.pages[0]?.counts?.all ?? undefined },
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-1 border-b border-line bg-card px-3 py-2">
        {tabs.map((t) => (
          <button key={t.key} type="button" className={`btn btn-sm ${filter === t.key ? "btn-secondary" : "btn-ghost"}`} onClick={() => setFilter(t.key)} aria-pressed={filter === t.key}>
            {t.label}
            {t.n !== undefined && <span className="num ml-0.5 text-ink-3">{t.n}</span>}
          </button>
        ))}
        {documentFilter && onDocumentFilter && (
          <button type="button" className="btn btn-ghost btn-sm ml-auto" onClick={() => onDocumentFilter(null)}>
            <Icon name="close" size={14} />
            取消文件筛选
          </button>
        )}
      </div>

      <div className="shrink-0 border-b border-line bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
        默认只显示未解决疑点，只有红项阻断导出。黄项和其他提示可留待处理，也可应用建议、编辑或忽略；已处置问题保存在记录中。
      </div>
      <div ref={scrollRef} data-review-issues-scroll className="min-h-0 flex-1 overflow-auto scroll-thin p-3">
        {q.isPending && <Loading text="正在读取疑点" />}
        {q.error && <ErrorBar text={errorText(q.error)} action={{ label: "重新读取", onSelect: () => void q.refetch() }} />}
        {!q.isPending && !q.error && !items.length && (
          <EmptyState
            icon="shield"
            title={all.length ? "该筛选下没有疑点" : "本版没有待处理疑点"}
            description={all.length ? "可切回“全部”查看其他疑点。" : "下方检查清单列出了实际执行了哪些规则、哪些未被覆盖，以及规则包版本与检查时间。"}
          />
        )}
        {items.map((i) => (
          <div key={i.issue_id} data-issue-id={i.issue_id}><IssueCard issue={i} editable={editable} canSupplement={canSupplement} selected={selectedIssueId === i.issue_id} onLocate={onLocate} onDispose={onDispose} onEdit={onEdit} onSupplement={onSupplement} /></div>
        ))}
        {q.hasNextPage && (
          <div className="py-2 text-center">
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => void q.fetchNextPage()} disabled={q.isFetchingNextPage}>
              {q.isFetchingNextPage ? "正在加载" : "加载更多疑点"}
            </button>
          </div>
        )}
        <RuleInputsPanel view={view} />
        <ChecksList view={view} />
      </div>
    </div>
  );
}

function IssueCard({ issue, editable, canSupplement, selected, onLocate, onDispose, onEdit, onSupplement }: { issue: IssueView; editable: boolean; canSupplement: boolean; selected: boolean; onLocate: (t: EvidenceTarget, id: string) => void; onDispose: (i: IssueView, k: IssueDisposition) => void; onEdit: (i: IssueView, k: "field" | "translation") => void; onSupplement: (i: IssueView) => void }) {
  const sev = SEV[issue.severity];
  const open = issue.status === "open" || issue.status === "unresolved";
  const translationTarget = issue.guidance?.target?.translation_segment_id ?? issue.a?.target.translation_segment_id ?? issue.b?.target.translation_segment_id;
  const baseReason = !open ? "此疑点已处理，可在处理记录查看" : !editable ? "当前版本或账号只允许查看" : null;
  const agentBusy = issue.guidance?.agent_status === "queued" || issue.guidance?.agent_status === "needs_reconcile";
  const agentBusyReason = agentBusy ? issue.guidance?.agent_status === "needs_reconcile" ? "上次外部请求待核查，暂不能重复处理" : "Agent正在处理本项，请等待结果" : null;
  const explanation = issue.guidance?.explanation.trim() || issue.conclusion;
  const recommendation = issue.guidance?.recommendation.trim();
  const replacement = issue.guidance?.replacement_text?.trim();
  const actionReason = (action: string, fallback: string | null) => {
    if (baseReason) return baseReason;
    const capability = issue.guidance?.actions?.find(item => item.action === action);
    return capability ? capability.enabled ? null : capability.reason || "当前状态不支持此操作" : fallback;
  };
  const buttons = [
    { id: "revise-translation", label: "修订译文", reason: !translationTarget ? "此疑点没有可修订的对应译段" : actionReason("revise-translation", null), run: () => onEdit(issue, "translation") },
    { id: "ignore", label: "忽略并记录", reason: actionReason("ignore", null), run: () => onDispose(issue, "ignore") },
    { id: "request-revision", label: "Agent处理", reason: baseReason ?? agentBusyReason ?? actionReason("request-revision", null), run: () => onDispose(issue, "request-revision") },
    { id: "apply-recommendation", label: "应用推荐修改", reason: baseReason ?? agentBusyReason ?? actionReason("apply-recommendation", !recommendation ? "当前没有可执行的推荐处理" : null), run: () => onDispose(issue, "apply-recommendation") },
    { id: "supplement-upload", label: "补充上传", reason: !canSupplement ? "请从对话页打开本票上传入口" : actionReason("supplement-upload", null), run: () => onSupplement(issue) },
  ];
  return (
    <article className={`card relative mb-2.5 min-h-[104px] overflow-hidden p-3 pl-4 ${selected ? "ring-2 ring-sel" : ""}`} aria-current={selected || undefined}>
      <span className={`absolute left-0 top-0 h-full w-1 ${sev.bar}`} aria-hidden="true" />
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill tone={sev.tone} icon={sev.icon}>
          {sev.label}
        </StatusPill>
        <h3 className="text-[13px] font-medium text-ink">{issue.guidance?.category_label ?? issue.category}</h3>
      </div>

      <dl className="mt-3 space-y-3 rounded-ctl bg-page p-3 text-[13px] leading-7 text-ink-2">
        <IssueDescription label="问题说明" text={explanation} />
        {issue.guidance?.impact.trim() && <IssueDescription label="可能影响" text={issue.guidance.impact} />}
        {recommendation && <IssueDescription label="推荐处理" text={recommendation} />}
        {replacement && replacement !== recommendation && <div className="rounded-ctl border border-line bg-card p-2.5">
          <IssueDescription label="建议替换为" text={replacement} />
        </div>}
      </dl>
      {issue.guidance?.agent_status && <div className="mt-2 rounded-ctl border border-line bg-card p-2 text-[12px] leading-relaxed" role="status">
        <p className="font-medium">{issue.guidance.agent_status === "queued" ? "Agent正在分析该问题" : issue.guidance.agent_status === "succeeded" ? "Agent处理结果" : issue.guidance.agent_status === "needs_reconcile" ? "Agent外部请求待核查" : "Agent本次处理失败"}</p>
        {issue.guidance.agent_response && <p className="mt-1 whitespace-pre-wrap text-ink-2">{issue.guidance.agent_response}</p>}
        {issue.guidance.agent_status === "succeeded" && open && <p className="mt-1 text-ink-3">请根据结果核对或补充材料；本条仍在未解决列表中。</p>}
      </div>}
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        {issue.a ? <SourceSide label="A" side={issue.a} onLocate={() => issue.a && onLocate(issue.a.target, issue.issue_id)} /> : <p className="text-[12px] text-ink-3">当前规则没有可定位来源，待补充材料</p>}
        {issue.b && <SourceSide label="B" side={issue.b} onLocate={() => onLocate(issue.b!.target, issue.issue_id)} />}
      </div>
      {issue.difference && (
        <p className="num mt-2 text-[12px] text-ink-2">
          差值 <span className="font-medium text-ink">{issue.difference}</span>
        </p>
      )}

      <div className="mt-2.5 flex flex-wrap items-center gap-2 border-t border-line pt-2.5">
        <span className="inline-flex items-center gap-1 text-[12px] text-ink-2">
          <Icon name={open ? "clock" : "check"} size={14} />
          {STATUS_TEXT[issue.status]}
        </span>
        {issue.disposition && (
          <span className="text-[12px] text-ink-3">
            {issue.disposition.by} {formatDateTime(issue.disposition.at)}：{issue.disposition.reason}
          </span>
        )}
        {issue.blocking_delivery && open && <StatusPill tone="risk" icon="alert">处置前不能导出</StatusPill>}
        <span className="flex-1" />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
        {buttons.map(button => <button key={button.id} type="button" className="btn btn-secondary btn-sm min-w-0 w-full" disabled={!!button.reason} title={button.reason ?? button.label} onClick={button.run}>{button.label}</button>)}
      </div>
    </article>
  );
}

function IssueDescription({ label, text }: { label: string; text: string }) {
  const paragraphs = text.trim().split(/\n+/).filter(Boolean);
  return (
    <div>
      <dt className="font-medium text-ink">{label}</dt>
      <dd className="mt-0.5 space-y-1.5 break-words">
        {paragraphs.map((paragraph, index) => <p key={index} className="whitespace-pre-wrap">{paragraph}</p>)}
      </dd>
    </div>
  );
}

function SourceSide({ label, side, onLocate }: { label: string; side: { label: string; value: string; target: EvidenceTarget }; onLocate: () => void }) {
  return (
    <div className="rounded-ctl border border-line bg-page p-2">
      <p className="text-[11px] text-ink-3">
        {label} · {side.label}
      </p>
      <p className="num mt-0.5 break-words-all text-[13px] text-ink">{side.value}</p>
      <button type="button" className="btn btn-link btn-sm mt-1 px-0" onClick={onLocate}>
        查看来源 {label}
      </button>
    </div>
  );
}

const CHECK_STATE: Record<RuleCheckView["status"], { text: string; tone: "ok" | "risk" | "warn" | "info" | "dis"; icon: "check" | "alert" | "warn" | "question" | "info" | "clock" }> = {
  passed: { text: "已核验通过", tone: "ok", icon: "check" },
  failed: { text: "未通过", tone: "risk", icon: "alert" },
  undetermined: { text: "无法判断", tone: "warn", icon: "question" },
  not_applicable: { text: "不适用", tone: "info", icon: "info" },
  not_covered: { text: "规则未覆盖", tone: "dis", icon: "info" },
  not_run: { text: "未运行", tone: "dis", icon: "clock" },
};

export function ChecksList({ view }: { view: ReviewView }) {
  const openEvidence = useSession(state => state.openEvidence);
  const q = useChecks(view);
  const [open, setOpen] = useState(false);
  const rows = q.items;
  if (!view.run_id) return null;
  return (
    <section className="card mt-1 overflow-hidden">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-page" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={14} className="text-ink-3" />
        <span className="text-[13px] font-medium text-ink">检查清单</span>
        <span className="num text-[12px] text-ink-3">{rows.length} 条</span>
        <span className="text-[12px] text-ink-3">执行了哪些规则、哪些未覆盖</span>
      </button>
      {open && (
        <div className="border-t border-line">
          {q.isPending && <div className="p-3"><Loading /></div>}
          {rows.map((c) => {
            const st = CHECK_STATE[c.status];
            return (
              <div key={c.check_id} className="border-b border-[#eceef1] px-3 py-2.5 last:border-b-0">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill tone={st.tone} icon={st.icon}>
                    {st.text}
                  </StatusPill>
                  <span className="text-[13px] text-ink">{c.rule_name}</span>
                  <span className="text-[11px] text-ink-3">{c.rule_version}</span>
                </div>
                <dl className="mt-1 grid gap-x-4 gap-y-0.5 text-[12px] text-ink-2 sm:grid-cols-[auto_1fr]">
                  <dt className="text-ink-3">适用条件</dt>
                  <dd>{c.condition}</dd>
                  <dt className="text-ink-3">输入来源</dt>
                  <dd className="num break-words-all">{c.inputs}</dd>
                  <dt className="text-ink-3">比较口径</dt>
                  <dd>{c.basis}</dd><dt className="text-ink-3">执行时间</dt><dd>{c.executed_at ? formatDateTime(c.executed_at) : "未提供"} · {c.rule_pack_version}</dd>
                  <dt className="text-ink-3">原文证据</dt><dd>{c.sources?.map((source,index) => <button key={index} className="btn btn-ghost btn-sm" onClick={() => openEvidence(source.target,"view_source")}>{source.label} {index+1}</button>)}</dd>
                </dl>
              </div>
            );
          })}
          {q.hasNextPage && <div className="p-3"><button type="button" className="btn btn-secondary" disabled={q.isFetchingNextPage} onClick={()=>void q.fetchNextPage()}>加载更多检查</button></div>}
        </div>
      )}
    </section>
  );
}
