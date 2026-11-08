import { CaseComparisonSummary } from "./CaseComparisonSummary";
import { languageName } from "../../i18n/languages";
// 单票概览：原件目录、全部处理时间线、疑点与成功快照三栏。
// 当前工作版卡与历史快照卡分开，新字段不放进旧译本卡；删除入口只在「更多」内。
import { useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { newIdempotencyKey } from "../../api/client";
import { conversationApi } from "../../api/conversation";
import type { ReferenceSelection } from "../../api/types";
import { fileIcon, Icon } from "../../components/Icon";
import { Menu, useToast } from "../../components/Overlay";
import { ErrorBar, IssueCounts, Loading, StatusPill, formatBytes, formatDateTime, formatTime, runTone , shortId } from "../../components/Status";
import { buildReviewSearch } from "../review/ReviewPage";
import { AppShell } from "../shell/AppShell";
import { CaseDeletionDialog, DeletionStatusCard } from "./CaseDeletionDialog";
import { DeliveryGroup } from "./RunDetails";
import { useCaseOverview, useDeletionStatus } from "./hooks";

export function CaseDetailPage() {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const q = useCaseOverview(caseId);
  const [menuOpen, setMenuOpen] = useState(false);
  const moreRef = useRef<HTMLButtonElement>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deletionId, setDeletionId] = useState<string | null>(null);
  const deletion = useDeletionStatus(deletionId);

  const ask = async (refs: ReferenceSelection[], note: string) => {
    try {
      const conv = await conversationApi.create({ initial_reference_selections: refs, entry_source: "case_overview" }, newIdempotencyKey());
      navigate(`/conversation/${conv.conversation_id}`);
      toast({ tone: "ok", text: note });
    } catch {
      toast({ tone: "risk", text: "新建会话失败，请稍后重试" });
    }
  };

  if (q.isPending) {
    return (
      <AppShell>
        <div className="flex flex-1 items-center justify-center">
          <Loading text="正在读取单票概览" />
        </div>
      </AppShell>
    );
  }
  if (q.error || !q.data) {
    return (
      <AppShell>
        <div className="p-6">
          <ErrorBar text="无法读取这票：可能已被删除或你没有权限。" action={{ label: "返回列表", onSelect: () => navigate("/documents") }} />
        </div>
      </AppShell>
    );
  }

  const o = q.data;
  const canDelete = o.case.allowed_actions.includes("delete_case");

  return (
    <AppShell>
      <div className="flex min-h-0 flex-1 flex-col">
        <header className="shrink-0 border-b border-line px-8 py-3">
          <button type="button" className="btn btn-ghost btn-sm -ml-2 mb-1" onClick={() => navigate("/documents")}>
            <Icon name="chevron-left" size={14} />
            返回列表
          </button>
          <div className="flex flex-wrap items-start gap-3">
            <div className="min-w-0 flex-1">
              <h1 className="truncate text-title font-semibold text-ink">{o.case.case_name}</h1>
              <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-2">
                <span>{o.case.short_code}</span>
                <span>
                  业务版 <span className="num">{o.case.current_business_revision_id ? o.case.business_revision_no ?? "—" : "尚未建立"}</span>
                </span>
                <span>
                  <span className="num">{o.sources.length}</span> 份原件
                </span>
                <IssueCounts blocking={o.issues.blocking} warning={o.issues.warning} undetermined={o.issues.undetermined} />
                <span>最近活动 {formatTime(o.last_material_event_at)}</span>
                <span className="text-ink-3">保留至 {formatDateTime(o.case.retention_expires_at)}</span>
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => void ask([{ kind: "case", id: o.case.case_id, label: o.case.case_name, selection_mode: "current", case_id: o.case.case_id }], "已新建会话并关联这票的当前版本")}
              >
                在新对话询问
              </button>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate(`/conversation?case_id=${o.case.case_id}&reprocess=1`)}>
                重新处理
              </button>
              <button ref={moreRef} type="button" className="btn btn-icon btn-ghost" onClick={() => setMenuOpen(true)} aria-label="更多操作">
                <Icon name="more" size={16} />
              </button>
              <Menu
                open={menuOpen}
                onClose={() => setMenuOpen(false)}
                anchor={moreRef}
                label="单票操作"
                items={canDelete ? [{ label: "删除这票", icon: "trash", tone: "risk", onSelect: () => setDeleteOpen(true) }] : [{ label: "没有可用的其他操作", icon: "info", onSelect: () => undefined, disabled: true }]}
              />
            </div>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-auto scroll-thin px-8 py-4">
          {deletion.data && <DeletionStatusCard status={deletion.data.status} onDismiss={() => setDeletionId(null)} />}
          <div className="grid gap-4 lg:grid-cols-3">
            <section className="card p-3">
              {o.summary && <details className="mb-3 text-[12px] text-ink-2"><summary className="cursor-pointer">单票摘要 · 展开全文</summary><p className="mt-1 whitespace-pre-wrap">{o.summary}</p></details>}
              <h2 className="mb-2 text-[14px] font-medium text-ink">原件目录</h2>
              <ul className="space-y-1.5">
                {o.sources.map(s => <li key={s.document_id}>
                  <details className="rounded-ctl border border-line p-2">
                    <summary className="cursor-pointer text-[13px] text-ink"><span className="break-words">{s.filename}</span><span className="block text-[11px] text-ink-3">当前 v{s.document_version} · {formatBytes(s.size_bytes)} · 展开版本与解析历史</span></summary>
                    {(o.source_history?.filter(history => history.source.document_id === s.document_id) ?? [{ source: s, parses: [] }]).map(history => <div key={history.source.document_version} className="mt-2 border-t border-line pt-2">
                      <p className="break-words text-[12px] text-ink">{history.source.filename} · 原件 v{history.source.document_version}{history.source.document_version !== s.document_version ? " · 历史，只读" : ""}</p>
                      <p className="text-[11px] text-ink-3">{formatTime(history.source.uploaded_at)} · {formatBytes(history.source.size_bytes)}</p>
                      <button type="button" className="btn btn-link btn-sm px-0" onClick={() => navigate(`/review${buildReviewSearch({ caseId: o.case.case_id, target: { case_id: o.case.case_id, document_id: s.document_id, document_version: history.source.document_version }, intent: "quick" })}`)}>查看固定原件</button>
                      <ul className="space-y-1 pl-2">{history.parses.map(parse => <li key={parse.parse_artifact_id} className="border-l border-line pl-2">
                        <button type="button" className="text-left text-[12px] text-brand-ink hover:underline" onClick={() => navigate(`/review${buildReviewSearch({ caseId: o.case.case_id, target: parse.target, intent: "quick" })}`)}>解析 {shortId(parse.parse_artifact_id)} · {parse.quality} · {parse.page_count ?? "未知"} 页 · 只读</button>
                        <p className="text-[11px] text-ink-3">{formatTime(parse.created_at)}</p>
                        {parse.logical_documents?.map(logical => <details key={logical.logical_document_id} className="mt-1 text-[11px] text-ink-2"><summary className="cursor-pointer">{logical.title} · 页 {logical.page_range}</summary><ul>{logical.sections.map(section => <li key={section.section_id}><button type="button" className="py-1 text-brand-ink hover:underline" onClick={() => navigate(`/review${buildReviewSearch({ caseId: o.case.case_id, target: section.target, intent: "quick" })}`)}>{section.title} · 页 {section.page_range}</button></li>)}</ul></details>)}
                      </li>)}</ul>
                      {!history.parses.length && <p className="text-[11px] text-ink-3">这份原件尚无解析记录。</p>}
                    </div>)}
                  </details>
                </li>)}
              </ul>
            </section>

            <section className="card p-3">
              <h2 className="mb-2 text-[14px] font-medium text-ink">这票的全部处理</h2>
              <ol className="space-y-1.5">
                {o.runs.map((r) => {
                  const tone = runTone(r.status);
                  return (
                    <li key={r.run_id} className="rounded-ctl border border-line p-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[13px] text-ink">{r.workflow_type === "read_index" ? "识别与索引" : r.workflow_type === "consignment_check" ? "同票核对" : "翻译与校验"}</span>
                        <span className="text-[11px] text-ink-3">{r.workflow_type !== "translate_review" ? "无译文" : languageName(r.target_language)}</span>
                        <StatusPill tone={tone.tone} icon={tone.icon} busy={tone.busy}>
                          {tone.text}
                        </StatusPill>
                      </div>
                      <p className="mt-0.5 text-[11px] text-ink-3">{formatDateTime(r.initiated_at)}</p>
                      <button type="button" className="btn btn-link btn-sm mt-1 px-0" onClick={() => navigate(`/review${buildReviewSearch({ caseId: o.case.case_id, runId: r.run_id, intent: "full" })}`)}>
                        打开这次处理
                      </button>
                    </li>
                  );
                })}
                {!o.runs.length && <li className="text-[13px] text-ink-2">这票还没有发起过处理。</li>}
              </ol>
            </section>

            <section className="space-y-4">
              <CaseComparisonSummary caseId={o.case.case_id} onOpenRun={runId => navigate(`/review${buildReviewSearch({ caseId: o.case.case_id, runId, intent: "full" })}`)} onUpload={() => navigate(`/conversation?case_id=${o.case.case_id}&upload=1`)} />
              <div className="card p-3">
                <h2 className="mb-2 text-[14px] font-medium text-ink">当前工作版</h2>
                <p className="text-[13px] text-ink-2">
                  业务版 <span className="num">{o.case.current_business_revision_id ? o.case.business_revision_no ?? "—" : "尚未建立"}</span>
                </p>
                <div className="mt-1.5">
                  <IssueCounts blocking={o.issues.blocking} warning={o.issues.warning} undetermined={o.issues.undetermined} size="md" />
                </div>
              </div>

              <div className="card p-3">
                <h2 className="mb-2 text-[14px] font-medium text-ink">成功交付快照</h2>
                {o.snapshots.length ? (
                  <ul className="space-y-1.5">
                    {o.snapshots.map((s, i) => (
                      <li key={s.snapshot_id} className="rounded-ctl border border-line p-2">
                        <p className="text-[13px] text-ink">
                          快照 {shortId(s.snapshot_id)}
                          {i > 0 && <span className="ml-1.5 rounded bg-hist-bg px-1.5 py-0.5 text-[11px] text-hist">历史，只读</span>}
                        </p>
                        <p className="mt-0.5 text-[11px] text-ink-3">{formatDateTime(s.exported_at)} · {s.target_language ?? "无译文"} · 业务 v{s.business_revision_no ?? "未知"} / 译文 v{s.translation_revision_no ?? "未知"}</p>
                        <details className="mt-2"><summary className="cursor-pointer text-[12px] text-brand-ink">查看冻结配对与下载</summary><DeliveryGroup snapshotId={s.snapshot_id} /></details>
                        <div className="mt-1 flex gap-1.5">
                          <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate(`/review${buildReviewSearch({ caseId: o.case.case_id, runId: s.run_id, snapshotId: s.snapshot_id, intent: "full" })}`)}>
                            查看快照
                          </button>
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm"
                            onClick={() => void ask([{ kind: "snapshot", id: s.snapshot_id, label: `交付快照 ${shortId(s.snapshot_id)}`, selection_mode: "fixed", case_id: o.case.case_id, snapshot_id: s.snapshot_id, run_id: s.run_id }], "已新建会话并固定这份交付快照")}
                          >
                            询问这版
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-[13px] text-ink-2">这票还没有成功的交付快照。</p>
                )}
              </div>
            </section>
          </div>
        </div>
      </div>

      {deleteOpen && caseId && <CaseDeletionDialog caseId={caseId} caseName={o.case.case_name} onClose={() => setDeleteOpen(false)} onQueued={(id) => {
            setDeleteOpen(false);
            // 离开概览后由列表页继续显示清理状态；只传不含内容的作业 ID
            sessionStorage.setItem("pendingDeletion", id);
            navigate("/documents");
          }} />}
    </AppShell>
  );
}
