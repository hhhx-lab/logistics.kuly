import { languageName, languageCode } from "../../i18n/languages";
// 批次展开区：参与原件、疑点与处置、交付文件、快照历史、任务时间线。
// 主行是历史快照时，仅当摘要版本与所示快照一致才显示该摘要。
import { useLayoutEffect, useState } from "react";
import { downloadContent } from "../../api/content";
import { contentUrls } from "../../api/review";
import type { EvidenceTarget, ManagedRunRow, RunSummaryView } from "../../api/types";
import { fileIcon, Icon } from "../../components/Icon";
import { useToast } from "../../components/Overlay";
import { EmptyState, IssueCounts, Loading, StatusPill, formatBytes, formatDateTime, formatTime, shortId } from "../../components/Status";
import { useRunSummary, useRunTimeline, useSnapshot, useSnapshots } from "./hooks";

type Group = "sources" | "issues" | "delivery" | "snapshots" | "timeline";
export type RunDetailsState = { groups: Record<Group, boolean>; sources: Record<string, boolean> };

export function RunDetails({
  row,
  openDelivery,
  onOpenEvidence,
  onAskAboutDocument,
  onOpenOps,
  onOpenMigration,
  canReconcile,
  canMigrate,
  savedState,
  onStateChange,
  onLayoutReady,
}: {
  row: ManagedRunRow;
  openDelivery?: boolean;
  onOpenEvidence: (target: EvidenceTarget, snapshotId?: string | null) => void;
  onAskAboutDocument: (row: ManagedRunRow, documentId: string, documentVersion: number) => void;
  onOpenOps: (stepId: string, runId: string) => void;
  onOpenMigration: (runId: string) => void;
  canReconcile: boolean;
  canMigrate: boolean;
  savedState?: RunDetailsState;
  onStateChange?: (state: RunDetailsState) => void;
  onLayoutReady?: () => void;
}) {
  const [open, setOpen] = useState<Record<Group, boolean>>(() => savedState?.groups ?? { sources: true, issues: false, delivery: !!openDelivery, snapshots: false, timeline: false });
  const [sources, setSources] = useState<Record<string, boolean>>(() => savedState?.sources ?? {});
  const summary = useRunSummary(row.run_id, row.displayed_snapshot_id);
  useLayoutEffect(() => {
    onStateChange?.({ groups: open, sources });
    if (summary.data) onLayoutReady?.();
  }, [open, sources, summary.data, onStateChange, onLayoutReady]);
  const toggle = (g: Group) => setOpen((p) => ({ ...p, [g]: !p[g] }));

  // 历史行的摘要必须与所示快照同版，否则不能冒充当前事实
  const summaryApplies = true;

  return (
    <div className="space-y-2">
      {summary.data && <details className="card p-3 text-[13px] text-ink-2"><summary className="cursor-pointer">处理摘要 · 展开全文</summary><p className="mt-2 whitespace-pre-wrap">{summary.data.summary}</p></details>}
      <Section label="参与原件" n={row.source_count} open={open.sources} onToggle={() => toggle("sources")}>
        {summary.isPending && <Loading />}
        {summary.data && (
          <SourcesList
            row={row}
            summary={summary.data}
            summaryApplies={summaryApplies}
            onOpenEvidence={onOpenEvidence}
            onAskAboutDocument={onAskAboutDocument}
            expanded={sources}
            onExpandedChange={setSources}
          />
        )}
      </Section>

      <Section label="疑点与处置" n={row.rules_status === "not_run" ? undefined : row.issue_counts.blocking + row.issue_counts.warning + row.issue_counts.undetermined} open={open.issues} onToggle={() => toggle("issues")}>
        {row.rules_status === "not_run" ? <p className="px-3 py-2 text-[13px] text-ink-2">疑点检查尚未完成</p> : summary.data && <IssueList summary={summary.data} snapshotId={row.displayed_snapshot_id} onOpenEvidence={onOpenEvidence} />}
      </Section>

      {row.workflow_type === "translate_review" && <Section label="交付文件" n={row.has_successful_snapshot ? 1 : 0} open={open.delivery} onToggle={() => toggle("delivery")}>
        {row.has_successful_snapshot && row.displayed_snapshot_id ? (
          <DeliveryGroup snapshotId={row.displayed_snapshot_id} />
        ) : (
          <p className="px-3 py-2 text-[13px] text-ink-2">这次处理还没有成功的交付快照，因此没有正式文件。失败或暂存的文件不会出现在这里。</p>
        )}
      </Section>}

      {row.workflow_type === "translate_review" && <Section label="快照历史" open={open.snapshots} onToggle={() => toggle("snapshots")}>
        <SnapshotHistory row={row} onOpenEvidence={onOpenEvidence} />
      </Section>}

      <Section label="任务时间线" open={open.timeline} onToggle={() => toggle("timeline")}>
        <Timeline row={row} canReconcile={canReconcile} canMigrate={canMigrate} onOpenOps={onOpenOps} onOpenMigration={onOpenMigration} />
      </Section>
    </div>
  );
}

function Section({ label, n, open, onToggle, children }: { label: string; n?: number; open: boolean; onToggle: () => void; children: React.ReactNode }) {
  return (
    <section className="card overflow-hidden">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-page" aria-expanded={open} onClick={onToggle}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={14} className="text-ink-3" />
        <span className="text-[13px] font-medium text-ink">{label}</span>
        {n !== undefined && <span className="num text-[12px] text-ink-3">{n}</span>}
      </button>
      {open && <div className="border-t border-line">{children}</div>}
    </section>
  );
}

function SourcesList({
  row,
  summary,
  summaryApplies,
  onOpenEvidence,
  onAskAboutDocument,
  expanded,
  onExpandedChange,
}: {
  row: ManagedRunRow;
  summary: RunSummaryView;
  summaryApplies: boolean;
  onOpenEvidence: (t: EvidenceTarget, snapshotId?: string | null) => void;
  onAskAboutDocument: (row: ManagedRunRow, documentId: string, documentVersion: number) => void;
  expanded: Record<string, boolean>;
  onExpandedChange: (expanded: Record<string, boolean>) => void;
}) {
  const toast = useToast();
  return (
    <div>
      {!summaryApplies && (
        <p className="border-b border-line bg-hist-bg px-3 py-2 text-[12px] text-hist">
          <Icon name="history" size={14} className="mr-1 inline align-[-2px]" />
          当前摘要不适用于这份历史交付，下面只提供该快照冻结版本的只读来源入口。
        </p>
      )}
      {summary.documents.map((d) => {
        const key = d.source.document_id;
        const isOpen = !!expanded[key];
        return (
          <div key={key} className="border-b border-line last:border-b-0">
            <div className="flex min-h-[56px] items-start gap-1 px-2 py-2.5">
              <button type="button" className="btn btn-icon btn-ghost h-9 w-9 shrink-0" aria-expanded={isOpen} aria-label={isOpen ? "收起内部单据" : "展开内部单据"} onClick={() => onExpandedChange({ ...expanded, [key]: !isOpen })}>
                <Icon name={isOpen ? "chevron-down" : "chevron-right"} size={14} />
              </button>
              <Icon name={fileIcon(d.source.mime)} size={16} className="mt-1.5 shrink-0 text-ink-2" />
              <div className="min-w-0 flex-1">
                <button type="button" data-management-focus={`source:${row.run_id}:${row.displayed_snapshot_id ?? "working"}:${d.source.document_id}:${d.source.document_version}`} className="block max-w-full truncate text-left text-[13px] font-medium text-ink underline-offset-2 hover:text-brand-ink hover:underline" title={d.source.filename} onClick={() => onOpenEvidence({ case_id: row.case_id, document_id: d.source.document_id, document_version: d.source.document_version, parse_artifact_id: d.parse_artifact_id ?? undefined, run_id: row.run_id, export_snapshot_id: row.displayed_snapshot_id ?? undefined }, row.displayed_snapshot_id)}>
                  {d.source.filename}
                </button>
                <p className="mt-0.5 text-[11px] text-ink-3">
                  原件 v{d.source.document_version} · {formatBytes(d.source.size_bytes)} · {d.source.uploaded_by} {formatTime(d.source.uploaded_at)}
                  {d.parse_artifact_id ? ` · 解析 ${d.parse_artifact_id.replace(/^pa-/, "")}` : " · 尚未解析"}
                </p>
                {summaryApplies ? (
                  <details className="mt-1 text-[12px] text-ink-2"><summary className="line-clamp-3 cursor-pointer">{d.short_summary}</summary><p className="mt-1 whitespace-pre-wrap">{d.short_summary}</p></details>
                ) : null}
                {summaryApplies && d.state !== "ready" && (
                  <StatusPill tone={d.state === "stale" ? "hist" : "risk"} icon={d.state === "stale" ? "history" : "alert"}>
                    {d.state === "stale" ? "摘要更新中" : "摘要生成失败"}
                  </StatusPill>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-0.5">
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => onAskAboutDocument(row, d.source.document_id, d.source.document_version)}>
                  询问这张单据
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(`${d.source.filename} · v${d.source.document_version} · 处理 ${shortId(row.run_id)}`);
                      toast({ tone: "ok", text: "已复制文件名、版本与处理编号" });
                    } catch {
                      toast({ tone: "info", text: "浏览器未授予剪贴板权限，可手动选择文本复制" });
                    }
                  }}
                >
                  复制文件信息
                </button>
              </div>
            </div>
            {isOpen && (
              <div className="bg-[#f8f9fa] pb-2 pl-12 pr-3">
                {d.logical_documents.map(ld => <details key={ld.logical_document_id} className="border-t border-[#eceef1] py-1.5">
                  <summary className="cursor-pointer text-[12px] text-ink">{ld.label} · 页 {ld.page_range ?? "未知"}</summary>
                  <p className="mt-1 whitespace-pre-wrap text-[11px] text-ink-3">{ld.summary}</p>
                  <button type="button" data-management-focus={`logical:${row.run_id}:${row.displayed_snapshot_id ?? "working"}:${ld.logical_document_id}`} className="btn btn-link btn-sm px-0" onClick={() => onOpenEvidence({ ...ld.target, export_snapshot_id: row.displayed_snapshot_id ?? undefined }, row.displayed_snapshot_id)}>查看这张逻辑单据</button>
                  <ul className="pl-3">{ld.sections?.map(section => <li key={section.section_id}>
                    <button type="button" className="flex w-full items-center gap-2 py-1.5 text-left text-[12px] text-brand-ink hover:underline" onClick={() => onOpenEvidence({ ...section.target, export_snapshot_id: row.displayed_snapshot_id ?? undefined }, row.displayed_snapshot_id)}>
                      <Icon name="docs" size={12} /><span className="min-w-0 flex-1 break-words">{section.title}</span><span className="shrink-0 text-ink-3">页 {section.page_range}</span>
                    </button>
                  </li>)}</ul>
                </details>)}
                {!row.has_successful_snapshot && <p className="border-t border-[#eceef1] pt-2 text-[11px] text-ink-3">尚无正式译本</p>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function IssueList({ summary, snapshotId, onOpenEvidence }: { summary: RunSummaryView; snapshotId?: string | null; onOpenEvidence: (t: EvidenceTarget, s?: string | null) => void }) {
  if (!summary.issues.length) return <p className="px-3 py-2 text-[13px] text-ink-2">这次处理没有记录到疑点。</p>;
  const sevText: Record<string, { text: string; tone: "risk" | "warn" | "info" | "dis" }> = {
    blocking: { text: "红项", tone: "risk" },
    warning: { text: "黄项", tone: "warn" },
    undetermined: { text: "无法判断", tone: "info" },
    not_covered: { text: "规则未覆盖", tone: "dis" },
  };
  return (
    <ul>
      {summary.issues.map((i) => {
        const sv = sevText[i.severity] ?? sevText.not_covered;
        return (
          <li key={i.issue_id} className="border-b border-[#eceef1] px-3 py-2 last:border-b-0">
            <div className="flex flex-wrap items-start gap-2">
              <StatusPill tone={sv.tone} icon={sv.tone === "risk" ? "alert" : sv.tone === "warn" ? "warn" : "question"}>
                {sv.text}
              </StatusPill>
              <button type="button" data-management-focus={`issue:${i.issue_id}:${snapshotId ?? "working"}`} className="min-w-0 flex-1 text-left text-[13px] text-ink hover:underline" onClick={() => onOpenEvidence({ ...i.target, export_snapshot_id: snapshotId ?? undefined }, snapshotId)}>
                {i.title}
              </button>
            </div>
            <p className="mt-0.5 text-[12px] text-ink-2">{i.disposition}</p>
          </li>
        );
      })}
    </ul>
  );
}

/** 交付文件：按物理源文件分卡，列出译本与所选识别稿。 */
export function DeliveryGroup({ snapshotId }: { snapshotId: string }) {
  const q = useSnapshot(snapshotId);
  const toast = useToast();
  const snap = q.data;
  if (q.isPending) return <div className="p-3"><Loading text="正在读取交付文件" /></div>;
  if (!snap) return <p className="px-3 py-2 text-[13px] text-ink-2">无法读取这份交付快照。</p>;

  const download = async (id: string, filename: string) => {
    try {
      await downloadContent(contentUrls.artifact(id), filename);
    } catch {
      toast({ tone: "risk", text: "下载中断，可重试下载同一份文件，不会重新导出" });
    }
  };

  return (
    <div className="space-y-2 p-3">
      <p className="text-[12px] text-ink-2">
        快照 {shortId(snap.snapshot_id)} · {formatDateTime(snap.exported_at)} · {snap.exported_by} · 业务版 <span className="num">{snap.business_revision_no}</span> / 译文版 <span className="num">{snap.translation_revision_no}</span>
      </p>
      {snap.source_artifacts.map((g) => (
        <div key={g.source.document_id} className="rounded-ctl border border-line p-2.5">
          <p className="truncate text-[13px] font-medium text-ink" title={g.source.filename}>
            {g.source.filename}
          </p>
          <p className="mt-0.5 break-words text-[11px] text-ink-3">原件 v{g.source.document_version} · 解析 {g.source.parse_artifact_id ? shortId(g.source.parse_artifact_id) : "冻结解析"} · 业务 v{snap.business_revision_no} / 译文 v{snap.translation_revision_no} · {snap.target_language ?? "无译文"}</p>
          {g.untranslated_note && <p className="mt-1 text-[11px] text-hist">{g.untranslated_note}</p>}
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {g.artifacts.map((a) => (
              <button key={a.artifact_id} type="button" className="btn btn-secondary btn-sm" onClick={() => void download(a.artifact_id, a.filename)}>
                <Icon name={a.kind === "docx" ? "file-word" : "file-pdf"} size={14} />
                {a.kind === "source_pdf" ? "识别稿 PDF" : `${languageName(snap.target_language)}译本 ${a.kind.toUpperCase()}`}
                <span className="num text-ink-3">{formatBytes(a.size_bytes)}</span>
              </button>
            ))}
          </div>
        </div>
      ))}

    </div>
  );
}

function SnapshotHistory({ row, onOpenEvidence }: { row: ManagedRunRow; onOpenEvidence: (t: EvidenceTarget, s?: string | null) => void }) {
  const q = useSnapshots(row.run_id);
  const items = q.data?.items ?? [];
  if (q.isPending) return <div className="p-3"><Loading /></div>;
  if (!items.length) return <p className="px-3 py-2 text-[13px] text-ink-2">这次处理还没有成功的交付快照。</p>;
  return (
    <ul>
      {items.map((s, idx) => {
        const isLatest = idx === 0;
        const isDisplayed = s.snapshot_id === row.displayed_snapshot_id;
        return (
          <li key={s.snapshot_id} className="flex flex-wrap items-center gap-2 border-b border-[#eceef1] px-3 py-2.5 last:border-b-0">
            <div className="min-w-0 flex-1">
              <p className="text-[13px] text-ink">
                快照 {shortId(s.snapshot_id)}
                {isDisplayed && <span className="ml-1.5 rounded bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand-ink">当前行展示</span>}
                {!isLatest && <span className="ml-1.5 rounded bg-hist-bg px-1.5 py-0.5 text-[11px] text-hist">历史，只读</span>}
              </p>
              <p className="mt-0.5 text-[11px] text-ink-3">
                {formatDateTime(s.exported_at)} · {s.exported_by} · 业务版 <span className="num">{s.business_revision_no}</span> / 译文版 <span className="num">{s.translation_revision_no}</span>
              </p>
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() =>
                onOpenEvidence(
                  { case_id: row.case_id, document_id: s.source_artifacts[0]?.source.document_id ?? "", document_version: s.source_artifacts[0]?.source.document_version ?? 1, run_id: row.run_id, export_snapshot_id: s.snapshot_id },
                  s.snapshot_id,
                )
              }
            >
              查看该快照
            </button>
            {!isLatest && items[0] && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => onOpenEvidence({ case_id: row.case_id, document_id: items[0].source_artifacts[0]?.source.document_id ?? "", document_version: items[0].source_artifacts[0]?.source.document_version ?? 1, run_id: row.run_id, export_snapshot_id: items[0].snapshot_id }, items[0].snapshot_id)}>
                最新成功快照
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Timeline({ row, canReconcile, canMigrate, onOpenOps, onOpenMigration }: { row: ManagedRunRow; canReconcile: boolean; canMigrate: boolean; onOpenOps: (stepId: string, runId: string) => void; onOpenMigration: (runId: string) => void }) {
  const q = useRunTimeline(row.run_id);
  return (
    <div className="p-3">
      {canMigrate && (
        <div className="mb-2 flex justify-end">
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenMigration(row.run_id)}>
            <Icon name="settings" size={14} />
            迁移模型
          </button>
        </div>
      )}
      {q.isPending && <Loading />}
      {!q.isPending && !q.items.length && <p className="text-[13px] text-ink-2">暂无事件记录。</p>}
      <ol className="space-y-2">
        {q.items.map((e) => (
          <li key={e.seq} className="flex gap-2.5">
            <span
              className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${e.kind === "failure" ? "bg-risk" : e.kind === "human" ? "bg-brand" : e.kind === "export" ? "bg-ok" : e.kind === "ops" ? "bg-warn" : "bg-line"}`}
              aria-hidden="true"
            />
            <div className="min-w-0 flex-1 border-b border-line pb-2">
              <div className="flex flex-wrap items-center gap-2">
                <p className="min-w-0 flex-1 text-[13px] text-ink">{e.message}</p>
                {/* 步骤 ID 只对有核查权限的人显示，普通用户只看到受控说明 */}
                {e.ops_step_id && canReconcile && (
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenOps(e.ops_step_id!, row.run_id)}>
                    运行核查
                  </button>
                )}
              </div>
              <p className="mt-0.5 text-[11px] text-ink-3">
                {formatTime(e.occurred_at)} · {e.actor}
              </p>
            </div>
          </li>
        ))}
      </ol>
      {q.hasNextPage && (
        <div className="pt-2 text-center">
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void q.fetchNextPage()} disabled={q.isFetchingNextPage}>
            {q.isFetchingNextPage ? "正在加载" : "加载更早事件"}
          </button>
        </div>
      )}
      {row.awaiting_external_reconcile && !canReconcile && (
        <div className="mt-2 rounded-ctl bg-warn-bg px-3 py-2 text-[12px] text-warn">
          <Icon name="shield" size={14} className="mr-1 inline align-[-2px]" />
          这次处理有一个外部请求结果待核查，已交由具备核查权限的人员处理。核查完成前不会重复提交收费请求。
        </div>
      )}
    </div>
  );
}

export { EmptyState };
