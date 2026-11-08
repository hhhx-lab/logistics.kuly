// 上传草稿（U03）：按上传会话列出每份文件的持久状态与已确认字节。
// 刷新丢失本地 File 对象后只显示「请重新选择」，不恢复旧的上传百分比。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { newIdempotencyKey } from "../../api/client";
import { putBytes, uploadApi } from "../../api/upload";
import { CaseCheckbox, SelectionBar, type CaseSelection, type SelectableCase } from "./CaseSelection";
import type { UploadFileStatus, UploadFileView } from "../../api/types";
import { fileIcon, Icon } from "../../components/Icon";
import { useToast } from "../../components/Overlay";
import { EmptyState, ErrorBar, Loading, StatusPill, formatBytes, formatTime , shortId } from "../../components/Status";
import { useUploadDrafts } from "./hooks";

const STATUS: Record<UploadFileStatus, { text: string; tone: "ok" | "warn" | "risk" | "info" | "dis"; icon: "check" | "clock" | "alert" | "warn" | "close" }> = {
  verified: { text: "已验收", tone: "ok", icon: "check" },
  received: { text: "已接收，正在校验", tone: "warn", icon: "clock" },
  uploading: { text: "传输中", tone: "warn", icon: "clock" },
  pending: { text: "待核验／请重新选择", tone: "info", icon: "clock" },
  failed: { text: "失败", tone: "risk", icon: "alert" },
  cancelled: { text: "已取消", tone: "dis", icon: "close" },
};

export function UploadDrafts({ onStartRun, selection, onDelete, onBulkDelete }: { onStartRun: (caseId: string) => void; selection: CaseSelection; onDelete: (item: SelectableCase) => void; onBulkDelete: () => void }) {
  const q = useUploadDrafts(true);
  const sessions = q.items;

  if (q.isPending) return <div className="flex justify-center py-12"><Loading text="正在读取上传会话" /></div>;
  if (q.isError && !sessions.length) return <ErrorBar text={q.error.message || "无法读取上传草稿"} action={{ label: "重试读取", onSelect: () => void q.refetch() }} />;
  if (!sessions.length) {
    return <EmptyState icon="plus" title="没有待处理上传草稿" description="完整上传但尚未发起处理、未完成及失败的上传会话可在这里管理。" />;
  }

  return (
    <div className="space-y-3">
      <SelectionBar items={sessions.map(s => ({caseId: s.case_id, name: s.case_name}))} selection={selection} onDelete={onBulkDelete} />
      {sessions.map((s) => {
        const verified = s.files.filter((f) => f.status === "verified");
        return (
          <section key={s.upload_session_id} className="card overflow-hidden">
            <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2.5">
              <CaseCheckbox item={{caseId: s.case_id, name: s.case_name}} selection={selection} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-[13px] font-medium text-ink">{s.case_name}</p>
                <p className="mt-0.5 text-[11px] text-ink-3">
                  上传会话 {shortId(s.upload_session_id)} · {s.owner} · {formatTime(s.created_at)}
                </p>
              </div>
              <StatusPill tone={s.status === "completed" ? "ok" : s.status === "partial" ? "warn" : s.status === "expired" ? "dis" : "info"} icon={s.status === "completed" ? "check" : s.status === "partial" ? "warn" : "clock"}>
                {s.status === "completed" ? "全部已验收" : s.status === "partial" ? "部分未完成" : s.status === "expired" ? "已过期" : "待处理"}
              </StatusPill>
              <button type="button" className="btn btn-ghost btn-sm text-risk" onClick={() => onDelete({caseId: s.case_id, name: s.case_name})}>删除整票</button>
              {verified.length > 0 && (
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => onStartRun(s.case_id)}>
                  用已验收文件发起处理
                </button>
              )}
            </div>
            <ul>
              {s.files.map((f) => (
                <FileRow key={f.upload_file_id} file={f} />
              ))}
            </ul>
            <p className="border-t border-line bg-page px-3 py-2 text-[11px] text-ink-3">
              这里只是上传记录，不是一次处理。需要开始处理时，请显式选择已验收的文件。
            </p>
          </section>
        );
      })}
      {q.isError && <ErrorBar text={q.error.message || "无法读取更多草稿"} action={{ label: "重试读取", onSelect: () => void q.refetch() }} />}
      {q.hasNextPage && <div className="flex justify-center"><button type="button" className="btn btn-secondary" disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>{q.isFetchingNextPage ? "正在加载" : "加载更多上传草稿"}</button></div>}
    </div>
  );
}

function FileRow({ file }: { file: UploadFileView }) {
  const st = Object.entries(STATUS).find(([key]) => key === file.status)?.[1] ?? { text: "未知状态", tone: "dis" as const, icon: "clock" as const };
  const toast = useToast();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [sentBytes, setSentBytes] = useState(0);
  const picker = useRef<HTMLInputElement>(null);
  const transfer = useRef<AbortController | null>(null);
  useEffect(() => () => transfer.current?.abort(), []);
  const notRetryable = ["FILE_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE", "TOO_MANY_PAGES"].includes(file.error_code ?? "");

  const reupload = async (selected: File) => {
    if (selected.name !== file.filename || selected.size !== file.size_bytes) {
      toast({ tone: "info", text: "请选择与原上传槽文件名、大小相同的原文件。替换文件请在单票中重新上传。" });
      return;
    }
    setBusy(true); setSentBytes(0);
    const controller = new AbortController(); transfer.current = controller;
    try {
      const slot = await uploadApi.retry(file.upload_file_id, newIdempotencyKey());
      if (!slot.put_url) throw new Error("服务端未签发上传地址，请刷新状态后重试");
      const { etag } = await putBytes(slot.put_url, selected, setSentBytes, controller.signal);
      await uploadApi.complete(file.upload_file_id, { etag }, newIdempotencyKey());
      toast({ tone: "ok", text: "文件已重新上传并通过服务端验收" });
    } catch (e) {
      if (!controller.signal.aborted) toast({ tone: "risk", text: (e as Error).message || "重新上传未完成" });
    } finally {
      transfer.current = null; setBusy(false); setSentBytes(0);
      await qc.invalidateQueries({ queryKey: ["m-uploads"] });
    }
  };

  const cancel = async () => {
    setBusy(true);
    try {
      await uploadApi.cancel(file.upload_file_id, newIdempotencyKey());
      toast({ tone: "ok", text: "已取消这个上传槽，不影响已有原件" });
      await qc.invalidateQueries({ queryKey: ["m-uploads"] });
    } catch (e) {
      toast({ tone: "risk", text: (e as Error).message || "操作未完成" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex min-h-[52px] items-start gap-2 border-b border-[#eceef1] px-3 py-2 last:border-b-0">
      <input ref={picker} type="file" className="hidden" accept={file.mime} aria-label={`重新选择 ${file.filename}`} onChange={(event) => { const selected = event.target.files?.[0]; event.target.value = ""; if (selected) void reupload(selected); }} />
      <Icon name={fileIcon(file.mime)} size={16} className="mt-1 shrink-0 text-ink-2" />
      <div className="min-w-0 flex-1">
        <p className="break-words-all text-[13px] text-ink">{file.filename}</p>
        <p className="mt-0.5 text-[11px] text-ink-3">
          {formatBytes(file.size_bytes)}
          {file.status !== "verified" && file.confirmed_bytes > 0 ? ` · 服务端已确认 ${formatBytes(file.confirmed_bytes)}` : ""}
          {file.duplicate_of ? " · 与本票已有文件内容相同" : ""}
          {busy && sentBytes > 0 ? ` · 本次已发送 ${formatBytes(sentBytes)}，等待验收` : ""}
        </p>
        {file.error && <p className="mt-0.5 text-[11px] text-risk">{file.error}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <StatusPill tone={st.tone} icon={st.icon}>
          {st.text}
        </StatusPill>
        {["failed", "pending", "uploading", "cancelled"].includes(file.status) && !notRetryable && (
          <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => picker.current?.click()}>
            {busy ? "正在重新上传" : file.status === "failed" ? "重试" : "重新选择文件"}
          </button>
        )}
        {["pending", "uploading", "failed", "received"].includes(file.status) && (
          <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void cancel()}>
            取消上传
          </button>
        )}
      </div>
    </li>
  );
}
