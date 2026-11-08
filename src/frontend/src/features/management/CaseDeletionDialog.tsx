// 删除单票：打开时才取 M06 预览，必须输入短号才可提交；
// 版本变化保留弹层、清空输入并刷新数量，要求重新核对。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { errorText, isApiError, newIdempotencyKey } from "../../api/client";
import { managementApi } from "../../api/management";
import { Icon } from "../../components/Icon";
import { Dialog } from "../../components/Overlay";
import { ErrorBar, Loading, formatDateTime } from "../../components/Status";
import { useDeletionPreview } from "./hooks";

export function CaseDeletionDialog({ caseId, caseName, onClose, onQueued }: { caseId: string; caseName?: string; onClose: () => void; onQueued: (deletionId: string) => void }) {
  const q = useDeletionPreview(caseId);
  const qc = useQueryClient();
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const keyRef = useRef(newIdempotencyKey());
  const p = q.data;

  // 版本在核对期间变化：清空输入并要求重新确认
  useEffect(() => {
    if (!p) return;
    setInput("");
  }, [p?.case_version]);

  const canSubmit = !!p?.allowed && input.trim() === p?.short_code && !pending;

  const submit = async () => {
    if (!p) return;
    setPending(true);
    setError(null);
    try {
      const rec = await managementApi.deleteCase(caseId, { expected_case_version: p.case_version, confirm_short_code: input.trim() }, keyRef.current);
      qc.removeQueries({predicate: query => JSON.stringify(query.queryKey).includes(caseId)});
      await qc.invalidateQueries();
      onQueued(rec.deletion_id);
    } catch (e) {
      if (isApiError(e) && e.status === 409) {
        setError("这票在你确认前发生了变化，数量已刷新，请重新核对后再输入短号。");
        setInput("");
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
      onClose={() => { if (!pending) onClose(); }}
      title="删除这票"
      tone="risk"
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={pending} onClick={onClose}>
            返回
          </button>
          <button type="button" className="btn btn-danger" disabled={!canSubmit} onClick={() => void submit()}>
            {pending ? "正在提交" : "确认删除这票"}
          </button>
        </>
      }
    >
      {q.isPending && <Loading text="正在读取这票的内容清单" />}
      {q.isError && <ErrorBar text={errorText(q.error)} action={{ label: "重试读取", onSelect: () => void q.refetch() }} />}
      {p && (
        <>
          <dl className="grid gap-x-4 gap-y-1 rounded-ctl bg-page px-3 py-2.5 text-[12px] sm:grid-cols-[auto_1fr]">
            <dt className="text-ink-3">单票名称</dt>
            <dd className="text-ink">{caseName ?? "—"}</dd>
            <dt className="text-ink-3">短号</dt>
            <dd className="mono text-ink">{p.short_code}</dd>
            <dt className="text-ink-3">原件版本数</dt>
            <dd className="num text-ink">{p.source_count}</dd>
            <dt className="text-ink-3">处理次数</dt>
            <dd className="num text-ink">{p.run_count}</dd>
            <dt className="text-ink-3">成功快照数</dt>
            <dd className="num text-ink">{p.snapshot_count}</dd>
            <dt className="text-ink-3">预计到期</dt>
            <dd className="text-ink">{formatDateTime(p.retention_expires_at)}</dd>
          </dl>

          <p className="mt-3 rounded-ctl border border-risk-line bg-risk-bg px-3 py-2 text-[13px] font-medium leading-relaxed text-risk">
            <Icon name="alert" size={14} className="mr-1 inline align-[-2px]" />
            将删除本票全部历史记录与交付文件，无法恢复；不会删除其他单票。
          </p>

          <ul className="mt-2 space-y-1 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
            <li>· 提交后立即不可访问。</li>
            <li>· 主存清理目标 24 小时内完成。</li>
            <li>· 加密备份最长 7 天后轮换删除。</li>
            <li>· 此前已下载或外发的副本无法由系统远程收回。</li>
            <li>· 第三方服务的保留口径见上传前展示的数据处理说明，本系统不能立刻擦除对方数据。</li>
          </ul>

          {!p.allowed && (
            <div className="mt-3">
              <ErrorBar tone="warn" text={p.disabled_reason ?? "当前不能删除这票。"} />
            </div>
          )}

          <label className="mt-3 block">
            <span className="label">
              请输入单票短号 <span className="mono font-medium text-ink">{p.short_code}</span> 以确认
            </span>
            <input className="input mt-1" value={input} onChange={(e) => setInput(e.target.value)} disabled={!p.allowed} data-autofocus autoComplete="off" />
          </label>

          {error && <div className="mt-3"><ErrorBar text={error} /></div>}
        </>
      )}
    </Dialog>
  );
}

/** 提交后的清理状态卡：不含任何票据内容。 */
export function DeletionStatusCard({ status, onDismiss }: { status: "queued" | "purging" | "completed" | "failed"; onDismiss: () => void }) {
  const text: Record<typeof status, string> = {
    queued: "删除请求已提交，正在排队清理。",
    purging: "正在清理这票的原件、解析结果、译文与交付文件。",
    completed: "这票已删除，相关记录不再可访问。",
    failed: "清理未完成。请稍后查看状态，或联系管理员处理。",
  };
  return (
    <div className={`mb-3 flex items-start gap-2 rounded-card border px-3 py-2.5 text-[13px] ${status === "failed" ? "border-risk-line bg-risk-bg text-risk" : status === "completed" ? "border-line bg-ok-bg text-ok" : "border-line bg-page text-ink-2"}`} role="status">
      <Icon name={status === "failed" ? "alert" : status === "completed" ? "check" : "clock"} size={16} className="mt-px shrink-0" />
      <span className="flex-1">{text[status]}</span>
      {(status === "completed" || status === "failed") && (
        <button type="button" className="shrink-0 underline underline-offset-2" onClick={onDismiss}>
          知道了
        </button>
      )}
    </div>
  );
}
