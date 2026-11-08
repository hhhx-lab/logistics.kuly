import type { ApiResponses } from "../../api/generated/request-contracts";
// 统一上传向导：主卡、回形针、拖入与管理台「新建处理」共用同一流程。
// 状态严格分为 local_selected → upload_session_created
// → bytes_transferring → server_verifying → verified/failed/cancelled。
// XHR 的 100% 只代表本连接已发送字节，只有 U05 返回 SourceVersion 才算「已验收」。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText } from "../../api/client";
import { useSequencedStream } from "../../api/events";
import type { CaseView, UploadFileView, UploadSessionView } from "../../api/types";
import { putBytes, type UploadFileSpec } from "../../api/upload";
import { fileIcon, Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, StatusPill, formatBytes } from "../../components/Status";
import { newKey, uploadApi, useDisclosure, useWorkflows } from "./hooks";

export interface UploadResult {
  caseId: string;
  caseName: string;
  /** 已验收的原件，可交给 W02 建任务草稿 */
  verified: { documentId: string; documentVersion: number; filename: string; sizeBytes: number }[];
  /** 已验收文件对应的 upload_file_id，供 C04 作附件提交 */
  uploadFileIds: string[];
}

type Slot = {
  localId: string;
  file: File;
  /** 本地即时预检结果，只作反馈；真校验在 U02/U05 */
  localReject?: string;
  serverFile?: UploadFileView;
  sentBytes: number;
  transferring: boolean;
  error?: string;
  keepDuplicate?: boolean;
};

export function UploadWizard({
  open,
  caseId,
  caseName,
  replaceTarget,
  issueSupplement,
  onClose,
  onDone,
}: {
  open: boolean;
  /** 当前会话已绑定的单票；为空时用户需选择新建单票 */
  caseId: string | null;
  caseName: string | null;
  replaceTarget?: { documentId: string; expectedVersion: number } | null;
  issueSupplement?: { related_issue_id: string; expected_issue_version: number } | null;
  onClose: () => void;
  onDone: (r: UploadResult) => void;
}) {
  const toast = useToast();
  const disclosure = useDisclosure();
  const workflows = useWorkflows();
  const [slots, setSlots] = useState<Slot[]>([]);
  const [session, setSession] = useState<UploadSessionView | null>(null);
  const [targetCase, setTargetCase] = useState<{ id: string; name: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const abortMap = useRef(new Map<string, AbortController>());
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const openRef = useRef(open);
  openRef.current = open;
  const refreshing = useRef<Promise<UploadSessionView> | null>(null);
  const refreshController = useRef<AbortController | null>(null);
  const applySession = useCallback((fresh: UploadSessionView) => {
    if (!openRef.current || sessionRef.current?.upload_session_id !== fresh.upload_session_id) return;
    if (sessionRef.current.last_seq > fresh.last_seq) return;
    sessionRef.current = fresh;
    setSession(fresh);
    setSlots((current) => current.map((slot) => {
      const file = fresh.files.find((f) => f.upload_file_id === slot.serverFile?.upload_file_id);
      return file ? { ...slot, serverFile: file } : slot;
    }));
  }, []);
  const refreshSession = useCallback(() => {
    if (refreshing.current) return refreshing.current;
    const id = sessionRef.current?.upload_session_id;
    if (!id || !openRef.current) return;
    const controller = new AbortController();
    refreshController.current = controller;
    const pending = api<ApiResponses["GET /api/v1/upload-sessions/{session_id}"]["200"]>(`/upload-sessions/${id}`, { signal: controller.signal })
      .then((fresh) => { if (!controller.signal.aborted) applySession(fresh); return fresh; })
      .finally(() => {
        if (refreshController.current === controller) refreshController.current = null;
        if (refreshing.current === pending) refreshing.current = null;
      });
    refreshing.current = pending;
    return pending;
  }, [applySession]);
  const uploadStream = useSequencedStream({
    kind: "upload",
    path: open && session?.status === "open" ? `/upload-sessions/${session.upload_session_id}/events/stream` : null,
    snapshotSeq: session?.last_seq,
    onEvent: () => { void refreshSession()?.catch(() => {}); },
    refetch: refreshSession,
  });

  useEffect(() => {
    openRef.current = open;
    return () => {
      openRef.current = false;
      refreshController.current?.abort();
      abortMap.current.forEach((controller) => controller.abort());
      abortMap.current.clear();
    };
  }, [open]);

  // 从 W01 读可接受格式与单文件上限，界面不硬编码
  const wf = workflows.data?.items.find((w) => w.workflow_type === "translate_review") ?? workflows.data?.items[0];
  const acceptedMime = wf?.accepted_mime ?? [];
  const maxBytes = wf?.max_file_bytes ?? 0;

  useEffect(() => {
    if (open) {
      // 每次打开都按当前会话重新初始化归票，避免沿用上一个会话的目标票
      setTargetCase(caseId && caseName ? { id: caseId, name: caseName } : null);
      return;
    }
    setSlots([]);
    setSession(null);
    setError(null);
    setTargetCase(null);
    refreshController.current?.abort();
    abortMap.current.forEach((c) => c.abort());
    abortMap.current.clear();
  }, [open, caseId, caseName]);

  if (!open) return null;

  const localCheck = (f: File): string | undefined => {
    if (acceptedMime.length && !acceptedMime.includes(f.type)) return `不支持此格式。当前支持：${acceptedMime.map(mimeLabel).join("、")}`;
    if (maxBytes && f.size > maxBytes) return `超过单文件上限 ${formatBytes(maxBytes)}`;
    return undefined;
  };

  const addFiles = (files: File[]) => {
    setError(null);
    setSlots((p) => [
      ...p,
      ...files.map((f) => ({
        localId: `${f.name}-${f.size}-${Math.random().toString(16).slice(2, 8)}`,
        file: f,
        localReject: localCheck(f),
        sentBytes: 0,
        transferring: false,
      })),
    ]);
  };

  const usable = slots.filter((s) => !s.localReject);
  const dup = (s: Slot) => usable.filter((x) => x.file.name === s.file.name && x.file.size === s.file.size).length > 1;

  /** 建会话 → 逐槽直传字节 → U05 验收。部分失败逐件保留，不静默跳过。 */
  const startUpload = async () => {
    if (busy || session || !usable.length) return;
    setBusy(true);
    setError(null);
    try {
      const policy = await disclosure.refetch();
      if (!policy.data || policy.error) throw policy.error ?? new Error("暂时无法读取上传配置，请重试");
      if (!policy.data.upload_allowed) throw new Error(policy.data.upload_blocked_reason ?? "当前暂不能上传");
      // 会话已绑票时一律加入该票；另一票必须通过新会话上传，不在同一会话混票
      let target = caseId ? { id: caseId, name: caseName ?? "当前单票" } : targetCase;
      if (caseId && !caseName) {
        const current = await uploadApi.getCase(caseId);
        target = { id: current.case_id, name: current.case_name };
      }
      if (!target) {
        const c: CaseView = await uploadApi.createCase(null, newKey());
        target = { id: c.case_id, name: c.case_name };
        setTargetCase(target);
      }
      setTargetCase(target);
      const specs: UploadFileSpec[] = usable.map((s, i) => ({
        filename: s.file.name,
        mime: s.file.type,
        size_bytes: s.file.size,
        // 替换原件时只对第一份生效，并带上被替换的版本
        ...(replaceTarget && i === 0 ? { replace_document_id: replaceTarget.documentId, expected_document_version: replaceTarget.expectedVersion } : {}),
      }));
      const sess = await uploadApi.createSession(target.id, specs, policy.data.disclosure_version, newKey(), issueSupplement ?? undefined);
      sessionRef.current = sess;
      setSession(sess);
      setSlots((p) => p.map((s) => { const sv = sess.files.find((f) => f.filename === s.file.name && f.size_bytes === s.file.size); return sv ? { ...s, serverFile: sv } : s; }));

      await Promise.all(
        sess.files.map(async (sf) => {
          const slot = usable.find((s) => s.file.name === sf.filename && s.file.size === sf.size_bytes);
          if (!slot || !sf.put_url) return;
          const ctrl = new AbortController();
          abortMap.current.set(sf.upload_file_id, ctrl);
          setSlots((p) => p.map((s) => (s.localId === slot.localId ? { ...s, transferring: true, error: undefined } : s)));
          try {
            const { etag } = await putBytes(sf.put_url, slot.file, (loaded) => setSlots((p) => p.map((s) => (s.localId === slot.localId ? { ...s, sentBytes: loaded } : s))), ctrl.signal);
            // 只有 U05 验收成功才算已验收
            const r = await uploadApi.complete(sf.upload_file_id, { etag }, newKey());
            setSlots((p) => p.map((s) => (s.localId === slot.localId ? { ...s, transferring: false, serverFile: r.file } : s)));
          } catch (e) {
            if ((e as Error).name === "AbortError") {
              setSlots((p) => p.map((s) => (s.localId === slot.localId ? { ...s, transferring: false } : s)));
              return;
            }
            setSlots((p) => p.map((s) => (s.localId === slot.localId ? { ...s, transferring: false, error: errorText(e) } : s)));
          } finally {
            abortMap.current.delete(sf.upload_file_id);
          }
        }),
      );
      const fresh = await uploadApi.session(sess.upload_session_id);
      applySession(fresh);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const cancelSlot = async (s: Slot) => {
    const id = s.serverFile?.upload_file_id;
    if (!id) return setSlots((p) => p.filter((x) => x.localId !== s.localId));
    // 先中止本地 XHR，再让服务端取消这个槽；已验收原件不受影响
    abortMap.current.get(id)?.abort();
    try {
      const f = await uploadApi.cancel(id, newKey());
      setSlots((p) => p.map((x) => (x.localId === s.localId ? { ...x, serverFile: f, transferring: false } : x)));
      await refreshSession();
    } catch (e) {
      setError(errorText(e));
    }
  };

  const retrySlot = async (s: Slot) => {
    const id = s.serverFile?.upload_file_id;
    if (!id) return;
    try {
      const f = await uploadApi.retry(id, newKey());
      setSlots((p) => p.map((x) => (x.localId === s.localId ? { ...x, serverFile: f, error: undefined, sentBytes: 0 } : x)));
      await refreshSession();
      if (!f.put_url) return;
      const ctrl = new AbortController();
      abortMap.current.set(id, ctrl);
      setSlots((p) => p.map((x) => (x.localId === s.localId ? { ...x, transferring: true } : x)));
      const { etag } = await putBytes(f.put_url, s.file, (loaded) => setSlots((p) => p.map((x) => (x.localId === s.localId ? { ...x, sentBytes: loaded } : x))), ctrl.signal);
      const r = await uploadApi.complete(id, { etag }, newKey());
      setSlots((p) => p.map((x) => (x.localId === s.localId ? { ...x, transferring: false, serverFile: r.file } : x)));
      await refreshSession();
    } catch (e) {
      setSlots((p) => p.map((x) => (x.localId === s.localId ? { ...x, transferring: false, error: errorText(e) } : x)));
    }
  };

  const verified = slots.filter((s) => s.serverFile?.status === "verified" && s.serverFile.source_version);
  const failed = slots.filter((s) => s.serverFile?.status === "failed" || s.error);
  const canFinish = verified.length > 0;

  const finish = () => {
    onDone({
      caseId: targetCase!.id,
      caseName: targetCase!.name,
      verified: verified.map((s) => ({ documentId: s.serverFile!.source_version!.document_id, documentVersion: s.serverFile!.source_version!.document_version, filename: s.file.name, sizeBytes: s.file.size })),
      uploadFileIds: verified.map((s) => s.serverFile!.upload_file_id),
    });
  };

  const d = disclosure.data;

  return (
    <Dialog
      open
      onClose={onClose}
      title={replaceTarget ? "替换这份原件" : "上传单据"}
      description={replaceTarget ? "替换会新增一个原件版本，不会删除已有版本；相关规则与译文需要重新计算。" : "上传同票材料。选择文件不等于上传成功，只有服务端验收后才会计入处理原件。"}
      width={640}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {canFinish ? "稍后处理" : "取消"}
          </button>
          {slots.length && !session ? (
            <button type="button" className="btn btn-primary" disabled={busy || !usable.length || (d && !d.upload_allowed)} onClick={() => void startUpload()}>
              {busy ? "正在上传" : "开始上传"}
            </button>
          ) : (
            <button type="button" className="btn btn-primary" disabled={!canFinish} onClick={finish} title={canFinish ? undefined : "至少需要一份已验收的原件"}>
              {failed.length ? `只处理已成功的 ${verified.length} 份` : "完成"}
            </button>
          )}
        </>
      }
    >
      <input
        ref={fileInput}
        type="file"
        multiple
        className="sr-only"
        aria-label="选择要上传的单据文件"
        onChange={(e) => {
          addFiles([...(e.target.files ?? [])]);
          e.target.value = "";
        }}
      />

      {session?.status === "open" && uploadStream.connection === "reconnecting" && (
        <p className="text-xs text-muted" role="status">正在恢复上传状态；字节进度与服务端验收分别确认。</p>
      )}

      {!slots.length && (
        <div
          className="rounded-card border border-dashed border-line bg-page p-6 text-center"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            addFiles([...e.dataTransfer.files]);
          }}
        >
          <Icon name="paperclip" size={24} className="mx-auto text-ink-3" />
          <p className="mt-2 text-[13px] text-ink">把文件拖到这里，或</p>
          <button type="button" className="btn btn-secondary mt-2" onClick={() => fileInput.current?.click()}>
            选择文件
          </button>
          <p className="mt-2 text-[11px] text-ink-3">
            {acceptedMime.length ? `支持 ${acceptedMime.map(mimeLabel).join("、")}` : "正在读取支持的格式"}
            {maxBytes ? ` · 单文件不超过 ${formatBytes(maxBytes)}` : ""}
          </p>
        </div>
      )}

      {disclosure.isError && <ErrorBar text="暂时无法读取上传配置" action={{ label: "重试", onSelect: () => void disclosure.refetch() }} />}
      {d && !d.upload_allowed && <ErrorBar text={d.upload_blocked_reason ?? "当前暂不能上传"} />}

      {slots.length > 0 && (
        <>
          <div className="mb-3 rounded-ctl bg-page p-3">
            <p className="text-[12px] font-medium text-ink">归票</p>
            {caseId ? (
              <div className="mt-1.5">
                <p className="text-[12px] text-ink">将加入当前单票：{caseName ?? targetCase?.name ?? "所选单票"}</p>
                <p className="mt-1 text-[11px] text-ink-3">一个会话只处理一票。要上传另一票的材料，请在新会话中上传。</p>
              </div>
            ) : (
              <p className="mt-1 text-[12px] text-ink-2">将新建一个单票。{targetCase ? `当前：${targetCase.name}` : "如果这几份材料不属于同一票，请分批上传。"}</p>
            )}
            <p className="mt-1 text-[11px] text-ink-3">不确定两份是否同票时请分批上传；一个会话只处理一票，跨票材料不会在同一会话混合。</p>
          </div>

          <ul className="space-y-1.5">
            {slots.map((s) => {
              const sf = s.serverFile;
              const pct = s.file.size ? Math.min(100, Math.round((s.sentBytes / s.file.size) * 100)) : 0;
              return (
                <li key={s.localId} className="min-h-[52px] rounded-ctl border border-line p-2.5">
                  <div className="flex items-start gap-2">
                    <Icon name={fileIcon(s.file.type)} size={16} className="mt-0.5 shrink-0 text-ink-3" />
                    <div className="min-w-0 flex-1">
                      <p className="break-words-all line-clamp-2 text-[13px] text-ink">{s.file.name}</p>
                      <p className="mt-0.5 text-[11px] text-ink-3">
                        {mimeLabel(s.file.type)} · <span className="num">{formatBytes(s.file.size)}</span>
                      </p>
                      {s.localReject && <p className="mt-1 text-[12px] text-risk">{s.localReject}</p>}
                      {!s.localReject && s.transferring && (
                        <p className="mt-1 text-[12px] text-ink-2">
                          正在传输 <span className="num">{pct}%</span>（<span className="num">{formatBytes(s.sentBytes)}</span>／<span className="num">{formatBytes(s.file.size)}</span>）
                        </p>
                      )}
                      {sf && !s.transferring && (
                        <p className="mt-1">
                          <StatusPill
                            tone={sf.status === "verified" ? "ok" : sf.status === "failed" ? "risk" : sf.status === "cancelled" ? "dis" : "warn"}
                            icon={sf.status === "verified" ? "check" : sf.status === "failed" ? "alert" : sf.status === "cancelled" ? "close" : "clock"}
                          >
                            {sf.status === "verified" ? "服务器已验收" : sf.status === "failed" ? "失败" : sf.status === "cancelled" ? "已取消" : sf.status === "received" ? "已接收，正在校验" : "待传输"}
                          </StatusPill>
                        </p>
                      )}
                      {(s.error || sf?.error) && <p className="mt-1 text-[12px] text-risk">{s.error ?? sf?.error}</p>}
                      {dup(s) && !s.keepDuplicate && (
                        <div className="mt-1 rounded-ctl bg-warn-bg px-2 py-1.5 text-[12px] text-warn">
                          这份文件和列表里另一份同名同大小。
                          <button type="button" className="ml-1 underline" onClick={() => setSlots((p) => p.map((x) => (x.localId === s.localId ? { ...x, keepDuplicate: true } : x)))}>
                            仍保留
                          </button>
                          <button type="button" className="ml-2 underline" onClick={() => setSlots((p) => p.filter((x) => x.localId !== s.localId))}>
                            移除此份
                          </button>
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 gap-1">
                      {(s.error || sf?.status === "failed") && (
                        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void retrySlot(s)}>
                          重试
                        </button>
                      )}
                      {s.transferring && (
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void cancelSlot(s)}>
                          取消
                        </button>
                      )}
                      {!s.transferring && sf?.status !== "verified" && (
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void cancelSlot(s)} aria-label={`移除 ${s.file.name}`}>
                          移除
                        </button>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>

          <button type="button" className="btn btn-secondary btn-sm mt-2" onClick={() => fileInput.current?.click()} disabled={!!session}>
            <Icon name="plus" size={14} />
            继续添加文件
          </button>

          {failed.length > 0 && verified.length > 0 && (
            <p className="mt-2 rounded-ctl bg-warn-bg px-2.5 py-2 text-[12px] text-warn">
              只有已验收的 {verified.length} 份会参与处理，被排除的是：{failed.map((s) => s.file.name).join("、")}。
            </p>
          )}
          <p className="mt-2 text-[11px] text-ink-3">上传完成后可选择识别或翻译。翻译草稿可按需选择“中国出口欧盟一般非危险消费品候选材料核对”，确认参与的发票、箱单与运输文件；基本翻译不要求启用此试行检查。不会根据文件名推断同票、货类、证书义务或合规放行。</p>
          <p className="mt-2 text-[11px] text-ink-3">关闭这个面板不会删除已验收的原件；取消上传只影响尚未完成的传输。</p>
        </>
      )}

      {error && <div className="mt-3"><ErrorBar text={error} /></div>}
    </Dialog>
  );
}

function mimeLabel(m: string) {
  if (m === "application/pdf") return "PDF";
  if (m === "image/jpeg") return "JPG";
  if (m === "image/png") return "PNG";
  if (m.includes("wordprocessingml")) return "DOCX";
  if (m.startsWith("image/")) return "图片";
  return m || "未知格式";
}
