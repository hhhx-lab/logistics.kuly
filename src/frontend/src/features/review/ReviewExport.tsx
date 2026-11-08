import { languageName } from "../../i18n/languages";
// 复核确认与整组导出。两个命令流程分开：打开清单必定重取 R13，
// 导出以本次清单的 review_fingerprint 为基线；任一产物失败不形成快照。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { downloadContent } from "../../api/content";
import { errorText, isApiError, newIdempotencyKey } from "../../api/client";
import { contentUrls, reviewApi } from "../../api/review";
import type { EvidenceTarget, ExportAttemptView, ExportSnapshotView, ReviewChecklist, ReviewView, RunView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, Loading, StatusPill, formatBytes, formatDateTime } from "../../components/Status";
import { useChecklist, useExportAttempt, useSnapshot } from "./hooks";
import { FrozenJurisdictionInputs } from "../conversation/JurisdictionInputsForm";

// ---- 复核清单 ----
export function ChecklistDialog({ view, run, onClose, onGoToTarget }: { view: ReviewView; run: RunView; onClose: () => void; onGoToTarget: (t: EvidenceTarget) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useChecklist(run.run_id, true);
  const [key] = useState(() => newIdempotencyKey());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);

  const cl = q.data;

  const confirm = async () => {
    if (!cl) return;
    if (!view.business_revision_id || !view.translation_revision_id) {
      setError("业务版或译文版尚未形成，请刷新后再确认");
      return;
    }
    setPending(true);
    setError(null);
    setStale(false);
    try {
      await reviewApi.confirm(
        run.run_id,
        {
          expected_review_fingerprint: cl.review_fingerprint,
          expected_business_revision_id: view.business_revision_id,
          expected_translation_revision_id: view.translation_revision_id,
        },
        key,
      );
      await qc.invalidateQueries();
      toast({ tone: "ok", text: "已完成复核确认，可以导出" });
      onClose();
    } catch (e) {
      if (isApiError(e) && ["REVIEW_FINGERPRINT_STALE", "REVIEW_STALE"].includes(e.code)) {
        setStale(true);
        await q.refetch();
      } else {
        setError(errorText(e));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="复核清单"
      description={`${view.case_name}｜确认只登记本次复核结论，不会立即导出。`}
      width={720}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            返回
          </button>
          <button type="button" className="btn btn-primary" disabled={!cl?.confirmable || q.isFetching || q.isError || pending} onClick={confirm} title={cl?.confirmable ? undefined : "还有红项或文件生成条件未满足"}>
            {pending ? "正在确认" : "确认复核"}
          </button>
        </>
      }
    >
      {q.isPending && <Loading text="正在读取门禁清单" />}
      {q.error && <ErrorBar text={errorText(q.error)} action={{ label: "重新读取", onSelect: () => void q.refetch() }} />}
      {cl && (
        <>
          {stale && <div className="mb-3"><ErrorBar tone="warn" text="你确认前复核内容发生了变化，清单已重新读取，请再核对一次。" /></div>}
          <dl className="grid gap-x-4 gap-y-1 rounded-ctl bg-page px-3 py-2.5 text-[12px] sm:grid-cols-[auto_1fr]">
            <dt className="text-ink-3">目标语言</dt>
            <dd className="text-ink">{languageName(view.target_language)}</dd>
            <dt className="text-ink-3">参与原件</dt>
            <dd className="text-ink">{cl.source_versions.map((s) => s.filename).join("、")}</dd>
            <dt className="text-ink-3">输出配对</dt>
            <dd className="text-ink">{cl.pairing_label}</dd>
            <dt className="text-ink-3">版本</dt>
            <dd className="num text-ink">
              业务版 {view.business_revision_no} · 译文版 {view.translation_revision_no}
            </dd>
          </dl>

          <ul className="mt-3 space-y-1.5">
            {cl.items.map((it, index) => (
              <li key={`${it.label}:${index}`} className="flex items-start gap-2 rounded-ctl border border-line px-3 py-2">
                <StatusPill tone={it.state === "ok" ? "ok" : it.state === "gap" ? "risk" : "warn"} icon={it.state === "ok" ? "check" : it.state === "gap" ? "alert" : "warn"}>
                  {it.state === "ok" ? "已满足" : it.state === "gap" ? "缺项" : "留待说明"}
                </StatusPill>
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] text-ink">{it.label}</span>
                  <span className="block text-[12px] text-ink-2">{it.detail}</span>
                </span>
              </li>
            ))}
          </ul>

          {cl.gaps.length > 0 && (
            <div className="mt-3 rounded-ctl border border-risk-line bg-risk-bg p-3">
              <p className="text-[13px] font-medium text-risk">需要先处理的缺项</p>
              <ul className="mt-1.5 space-y-1.5">
                {cl.gaps.map((g, i) => (
                  <li key={i} className="flex items-start gap-2 text-[12px] text-risk">
                    <Icon name="alert" size={14} className="mt-px shrink-0" />
                    <span className="flex-1">
                      {g.item}：{g.detail}
                    </span>
                    {g.target && (
                      <button
                        type="button"
                        className="shrink-0 font-medium underline underline-offset-2"
                        onClick={() => {
                          onGoToTarget(g.target!);
                          onClose();
                        }}
                      >
                        前往问题
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <p className="mt-3 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">{cl.not_covered_note}未覆盖的规则不计为通过，审核记录会保留。</p>
          {error && <div className="mt-3"><ErrorBar text={error} /></div>}
        </>
      )}
    </Dialog>
  );
}

// ---- 导出确认层 ----
export function ExportDialog({ view, run, onClose, onStarted }: { view: ReviewView; run: RunView; onClose: () => void; onStarted: (attemptId: string) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useChecklist(run.run_id, true);
  const [key] = useState(() => newIdempotencyKey());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fmts = run.output_formats ?? ["docx", "pdf"];
  const [includeSourcePdf, setIncludeSourcePdf] = useState(true);

  const start = async () => {
    const cl = q.data;
    if (!cl) return;
    if (!run.current_review_confirmation_id || !view.business_revision_id || !view.translation_revision_id) {
      setError("确认记录或版本配对缺失，请刷新并重新复核");
      return;
    }
    setPending(true);
    setError(null);
    try {
      const attempt = await reviewApi.startExport(
        run.run_id,
        {
          confirmation_id: run.current_review_confirmation_id,
          expected_review_fingerprint: cl.review_fingerprint,
          expected_business_revision_id: view.business_revision_id,
          expected_translation_revision_id: view.translation_revision_id,
          include_source_pdf: includeSourcePdf,
        },
        key,
      );
      await qc.invalidateQueries();
      onStarted(attempt.attempt_id);
      onClose();
    } catch (e) {
      if (isApiError(e) && e.code === "EXPORT_ATTEMPT_RETRY_REQUIRED") {
        setError("上次导出还有失败的产物，请先在导出进度中重试那一次，不要重新开始。");
      } else if (isApiError(e) && ["REVIEW_FINGERPRINT_STALE", "REVIEW_STALE"].includes(e.code)) {
        setError("复核内容已变化，请重新完成复核确认后再导出。");
        await q.refetch();
      } else {
        setError(errorText(e));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="导出单据"
      description={`导出本次已确认的${languageName(run.target_language)}译本，可同时下载识别稿 PDF。其他语言译本在对应任务中独立导出。`}
      width={640}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            返回
          </button>
          <button type="button" className="btn btn-primary" onClick={start} disabled={pending || q.isFetching || q.isError || !q.data?.confirmable || q.data?.confirmation_status !== "valid"}>
            {pending ? "正在提交" : "导出所选文件"}
          </button>
        </>
      }
    >
      <div className="space-y-2">
        {run.source_versions.map((s) => (
          <div key={s.document_id} className="rounded-ctl border border-line p-2.5">
            <p className="truncate text-[13px] font-medium text-ink">{s.filename}</p>
            <p className="mt-0.5 text-[11px] text-ink-3">
              {languageName(run.target_language)}译本
            </p>
            <ul className="mt-1.5 space-y-0.5 text-[12px] text-ink-2">
              {fmts.map((f) => (
                <li key={f}>
                  <Icon name={f === "docx" ? "file-word" : "file-pdf"} size={14} className="mr-1 inline align-[-2px]" />
                  {s.filename.replace(/\.[^.]+$/, "")}_{run.target_language}_译本.{f}
                </li>
              ))}
              {includeSourcePdf && <li><Icon name="file-pdf" size={14} className="mr-1 inline align-[-2px]" />{s.filename.replace(/\.[^.]+$/, "")}_识别稿.pdf</li>}
            </ul>
          </div>
        ))}
        <label className="flex items-center gap-2 rounded-ctl border border-line bg-page p-2.5 text-[13px]"><input type="checkbox" checked={includeSourcePdf} onChange={e => setIncludeSourcePdf(e.target.checked)} disabled={pending} />同时导出识别稿 PDF</label>
        <p className="text-[12px] text-ink-3">识别稿由已保存的解析内容生成，保留已识别的表格、横线与分页结构。</p>
      </div>
      {run.translation_scope && run.translation_scope !== "all" && (
        <p className="mt-3 rounded-ctl bg-hist-bg px-3 py-2 text-[12px] text-hist">
          译本仅包含本次指定范围；识别稿 PDF 包含原件全部已识别内容。
        </p>
      )}
      {error && <div className="mt-3"><ErrorBar text={error} /></div>}
      {q.error && <ErrorBar text={errorText(q.error)} action={{ label: "重新读取", onSelect: () => void q.refetch() }} />}
    </Dialog>
  );
}

// ---- 导出进度 ----
const STEP_TEXT: Record<string, { text: string; tone: "info" | "brand" | "ok" | "risk"; busy?: boolean }> = {
  pending: { text: "等待", tone: "info" },
  rendering: { text: "生成中", tone: "brand", busy: true },
  checking: { text: "回检中", tone: "brand", busy: true },
  succeeded: { text: "已完成", tone: "ok" },
  failed: { text: "失败", tone: "risk" },
};

export function ExportProgress({ run, attemptId, onClose }: { run: RunView; attemptId: string; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useExportAttempt(run.run_id, attemptId);
  const [retrying, setRetrying] = useState(false);
  const attempt = q.data;

  useEffect(() => {
    if (!attempt) return;
    if (attempt.status === "succeeded" && attempt.snapshot_id) toast({ tone: "ok", text: "导出完成，交付文件已生成" });
  }, [attempt?.status, attempt?.snapshot_id, attempt, toast]);

  const retry = async () => {
    if (!attempt) return;
    setRetrying(true);
    try {
      const cl = await reviewApi.checklist(run.run_id);
      if (!run.current_business_revision_id || !run.current_translation_revision_id) {
        throw new Error("当前版本配对缺失，请刷新后再重试导出");
      }
      await reviewApi.retryExport(run.run_id, attempt.attempt_id, { expected_review_fingerprint: cl.review_fingerprint,
        confirmation_id: cl.current_review_confirmation_id ?? undefined,
        expected_business_revision_id: run.current_business_revision_id,
        expected_translation_revision_id: run.current_translation_revision_id }, newIdempotencyKey());
      await qc.invalidateQueries();
    } catch (e) {
      toast({ tone: "risk", text: errorText(e) });
    } finally {
      setRetrying(false);
    }
  };

  return (
    <Dialog open onClose={onClose} title="导出进度" description="逐份文件的真实生成与回检状态；全部通过后才形成交付快照。" width={600} footer={<button type="button" className="btn btn-secondary" onClick={onClose}>关闭</button>}>
      {q.isPending && <Loading />}
      {attempt && (
        <>
          <ul className="space-y-1.5">
            {attempt.artifact_steps.map((s) => {
              const st = STEP_TEXT[s.status] ?? STEP_TEXT.pending;
              return (
                <li key={s.step_id} className="flex items-center gap-2 rounded-ctl border border-line px-3 py-2">
                  <Icon name={s.kind === "docx" ? "file-word" : "file-pdf"} size={16} className="shrink-0 text-ink-2" />
                  <span className="min-w-0 flex-1 truncate text-[13px] text-ink">{s.label}</span>
                  <StatusPill tone={st.tone} busy={st.busy}>
                    {st.text}
                  </StatusPill>
                </li>
              );
            })}
          </ul>
          {attempt.status === "failed_retryable" && (
            <div className="mt-3">
              <ErrorBar text={attempt.error_summary ?? "有产物生成失败。已成功的文件被保留，可只重试失败项。"} action={{ label: retrying ? "正在重试" : "只重试失败项", onSelect: () => void retry() }} />
              <p className="mt-2 text-[12px] text-ink-2">失败的暂存文件不会进入交付列表；此前已成功的交付快照仍然可以下载。</p>
            </div>
          )}
          {attempt.status === "succeeded" && attempt.snapshot_id && <SnapshotCard snapshotId={attempt.snapshot_id} />}
        </>
      )}
    </Dialog>
  );
}

// ---- 交付快照 ----
export function SnapshotCard({ snapshotId }: { snapshotId: string }) {
  const q = useSnapshot(snapshotId);
  const snap = q.data;
  if (q.isPending) return <div className="mt-3"><Loading text="正在读取交付文件" /></div>;
  if (!snap) return null;
  return (
    <div className="mt-3">
      <SnapshotFiles snapshot={snap} />
    </div>
  );
}

export function SnapshotFiles({ snapshot }: { snapshot: ExportSnapshotView }) {
  const toast = useToast();
  const download = async (artifactId: string, filename: string) => {
    try {
      await downloadContent(contentUrls.artifact(artifactId), filename);
    } catch {
      toast({ tone: "risk", text: "下载中断，可重试下载同一份文件" });
    }
  };
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <StatusPill tone="ok" icon="check">
          已导出
        </StatusPill>
        <span className="text-[12px] text-ink-2">
          {formatDateTime(snapshot.exported_at)} · {snapshot.exported_by} · 业务版 <span className="num">{snapshot.business_revision_no}</span> / 译文版 <span className="num">{snapshot.translation_revision_no}</span>
        </span>
      </div>
      {snapshot.jurisdiction_inputs && <FrozenJurisdictionInputs value={snapshot.jurisdiction_inputs} sources={snapshot.source_artifacts.map(group => group.source)} packVersion={snapshot.rule_pack_version ?? undefined} />}
      <div className="space-y-2">
        {snapshot.source_artifacts.map((g) => (
          <div key={g.source.document_id} className="rounded-ctl border border-line p-2.5">
            <p className="truncate text-[13px] font-medium text-ink">{g.source.filename}</p>
            <p className="mt-0.5 text-[11px] text-ink-3">原件 v{g.source.document_version}</p>
            {g.untranslated_note && <p className="mt-1 text-[11px] text-hist">{g.untranslated_note}</p>}
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {g.artifacts.map((a) => (
                <button key={a.artifact_id} type="button" className="btn btn-secondary btn-sm" onClick={() => void download(a.artifact_id, a.filename)}>
                  <Icon name={a.kind === "docx" ? "file-word" : "file-pdf"} size={14} />
                  {a.kind === "source_pdf" ? "识别稿 PDF" : `${languageName(snapshot.target_language)}译本 ${a.kind.toUpperCase()}`}
                  <span className="num text-ink-3">{formatBytes(a.size_bytes)}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>

    </div>
  );
}

export type { ExportAttemptView, ReviewChecklist };
