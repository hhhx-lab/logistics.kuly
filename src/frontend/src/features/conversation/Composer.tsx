import { LANGUAGES, languageNativeName, languageName } from "../../i18n/languages";
import { UiText } from "../../i18n/runtime";
// 输入区：Lexical 富文本 + 精确 `/ ` 与 `@ ` 触发的结构化标签。
// 发送载荷由纯文本、引用选择、能力选择三部分组成，绝不从可见标签名反解析 ID。
import { $createParagraphNode, $createTextNode, $getRoot, $getSelection, $isRangeSelection, COMMAND_PRIORITY_LOW, KEY_DOWN_COMMAND, PASTE_COMMAND, type LexicalEditor } from "lexical";
import { LexicalComposer } from "@lexical/react/LexicalComposer";
import { ContentEditable } from "@lexical/react/LexicalContentEditable";
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext";
import { OnChangePlugin } from "@lexical/react/LexicalOnChangePlugin";
import { RichTextPlugin } from "@lexical/react/LexicalRichTextPlugin";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { errorText } from "../../api/client";
import { useDocumentCatalog } from "../../api/documentCatalog";
import { DocumentTypeSelect } from "../../components/DocumentTypeControls";
import type { CapabilityCandidate, CapabilitySelection, ReferenceCandidate, ReferenceSelection } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, formatBytes } from "../../components/Status";
import { conversationApi } from "./hooks";
import { useQueryScope, useSession } from "../../stores/session";

export interface ComposerAttachment {
  uploadFileId: string;
  filename: string;
  sizeBytes: number;
}

export interface ComposerDraft {
  text: string;
  references: ReferenceSelection[];
  capabilities: CapabilitySelection[];
  attachments: ComposerAttachment[];
}

export const EMPTY_DRAFT: ComposerDraft = { text: "", references: [], capabilities: [], attachments: [] };

type Trigger = { kind: "capability" | "reference"; query: string } | null;

function capabilityKey(c: CapabilitySelection | CapabilityCandidate) {
  return `${c.kind}:${c.extension_id}:${c.extension_version}:${c.tool_id ?? ""}:${c.tool_schema_hash ?? ""}`;
}

function referenceCandidateKey(candidate: ReferenceCandidate) {
  const selection = candidate.selection;
  return [selection.kind, selection.id, selection.document_version, selection.parse_artifact_id, selection.snapshot_id, selection.run_id].join(":");
}

/** 只有正文开头或空白边界的半角 `/ `、`@ ` 才触发；单独一个字符、URL、路径与代码都不触发。 */
function detectTrigger(text: string, caret: number): Trigger {
  const before = text.slice(0, caret);
  const m = before.match(/(^|[\s\n])([/@]) ([^\s\n]*)$/);
  if (!m) return null;
  return { kind: m[2] === "/" ? "capability" : "reference", query: m[3] };
}

export function Composer({
  draft,
  onDraftChange,
  onSend,
  onPickFiles,
  disabled,
  disabledReason,
  sending,
  sendError,
  onRetry,
  caseId,
  readOnly,
  onCrossCaseReference,
}: {
  draft: ComposerDraft;
  onDraftChange: (d: ComposerDraft) => void;
  onSend: () => void;
  onPickFiles: () => void;
  disabled?: boolean;
  disabledReason?: string;
  sending?: boolean;
  sendError?: string | null;
  onRetry?: () => void;
  caseId: string | null;
  readOnly?: boolean;
  onCrossCaseReference: (selection: ReferenceSelection) => Promise<void>;
}) {
  const [trigger, setTrigger] = useState<Trigger>(null);
  const [attachmentsOpen, setAttachmentsOpen] = useState(false);
  const attachmentListId = useId();
  const [parameterCandidate, setParameterCandidate] = useState<CapabilityCandidate | null>(null);
  const [parameterValues, setParameterValues] = useState<Record<string, string | boolean>>({});
  const [crossCaseCandidate, setCrossCaseCandidate] = useState<ReferenceCandidate | null>(null);
  const [creatingConversation, setCreatingConversation] = useState(false);
  const [crossCaseError, setCrossCaseError] = useState<string | null>(null);
  const identityGeneration = useQueryScope();
  const capabilityRevision = useSession(state => state.capabilityRevision);
  const [capabilityDirectory, setCapabilityDirectory] = useState<{ selectionKey: string; generation: number; reasons: Record<string, string> } | null>(null);
  const selectedCapabilityKey = draft.capabilities.map(capabilityKey).sort().join("|");
  useEffect(() => {
    if (!selectedCapabilityKey) { setCapabilityDirectory(null); return; }
    let controller: AbortController | null = null;
    const verify = () => {
      controller?.abort(); controller = new AbortController();
      const request = controller;
      setCapabilityDirectory(null);
      conversationApi.capabilities("", request.signal).then(({ items }) => {
        if (request.signal.aborted) return;
        const reasons = Object.fromEntries(selectedCapabilityKey.split("|").flatMap((key) => {
          const current = items.find((item) => capabilityKey(item) === key);
          const reason = !current ? "能力版本或授权已变化，请移除后重新选择" : current.unavailable_reason || (current.connection_status !== "可用" ? `连接状态：${current.connection_status}` : null);
          return reason ? [[key, reason]] : [];
        }));
        setCapabilityDirectory({ selectionKey: selectedCapabilityKey, generation: identityGeneration, reasons });
      }).catch((e) => {
        if (request.signal.aborted) return;
        setCapabilityDirectory({ selectionKey: selectedCapabilityKey, generation: identityGeneration, reasons: Object.fromEntries(selectedCapabilityKey.split("|").map((key) => [key, `能力状态无法核验：${errorText(e)}`])) });
      });
    };
    const visible = () => { if (document.visibilityState === "visible") verify(); };
    verify(); window.addEventListener("focus", verify); document.addEventListener("visibilitychange", visible);
    return () => { controller?.abort(); window.removeEventListener("focus", verify); document.removeEventListener("visibilitychange", visible); };
  }, [selectedCapabilityKey, identityGeneration, capabilityRevision]);
  const editorRef = useRef<LexicalEditor | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  // 记录编辑器自身最近一次产生的文本；外部（示例按钮、清空草稿）改变 draft.text 时同步回编辑器
  const localText = useRef(draft.text);
  // 记录被 Esc 关掉的触发位置，避免同一处反复弹出
  const dismissedText = useRef<string | null>(null);
  const pasting = useRef(false);
  const closePicker = () => {
    dismissedText.current = draft.text;
    setTrigger(null);
    requestAnimationFrame(() => editorRef.current?.getRootElement()?.focus({ preventScroll: true }));
  };
  useEffect(() => {
    if (draft.text === localText.current) return;
    localText.current = draft.text;
    setEditorText(editorRef.current, draft.text);
  }, [draft.text]);

  const initialConfig = useMemo(
    () => ({
      namespace: "composer",
      // 输入内容一律作为纯文本处理，不把用户输入的 HTML 当可执行页面
      onError: (e: Error) => console.error(e),
      theme: { paragraph: "m-0" },
    }),
    [],
  );

  const insertCapability = (c: CapabilityCandidate, values?: Record<string, string | boolean>) => {
    if (c.unavailable_reason || c.connection_status !== "可用") return;
    if (c.parameters?.length && values === undefined) {
      setParameterValues(Object.fromEntries(c.parameters.filter((p) => p.default_value != null).map((p) => [p.name, p.default_value as string | boolean])));
      setParameterCandidate(c);
      setTrigger(null);
      return;
    }
    const sel: CapabilitySelection = {
      kind: c.kind,
      extension_id: c.extension_id,
      extension_version: c.extension_version,
      tool_id: c.tool_id,
      tool_schema_hash: c.tool_schema_hash,
      label: c.name,
      user_parameters: values,
    };
    onDraftChange({ ...draft, text: stripTrigger(draft.text), capabilities: [...draft.capabilities.filter((x) => !(x.extension_id === c.extension_id && x.tool_id === c.tool_id)), sel] });
    setTrigger(null);
    setParameterCandidate(null);
    setEditorText(editorRef.current, stripTrigger(draft.text));
  };

  const insertReference = (c: ReferenceCandidate) => {
    onDraftChange({ ...draft, text: stripTrigger(draft.text), references: [...draft.references.filter((x) => x.id !== c.selection.id), c.selection] });
    setTrigger(null);
    setEditorText(editorRef.current, stripTrigger(draft.text));
  };

  // 引用或能力已失效时阻止这次调用，但保留用户输入
  const directoryCurrent = capabilityDirectory?.selectionKey === selectedCapabilityKey && capabilityDirectory?.generation === identityGeneration;
  const checkingCapabilities = !!selectedCapabilityKey && !directoryCurrent;
  const capabilityReason = (c: CapabilitySelection) => c.unavailable_reason || (directoryCurrent ? capabilityDirectory?.reasons[capabilityKey(c)] : undefined);
  const unavailable = [...draft.references.filter((x) => x.unavailable_reason), ...draft.capabilities.filter(capabilityReason)];
  const canSend = !disabled && !sending && !checkingCapabilities && !unavailable.length && (draft.text.trim().length > 0 || draft.attachments.length > 0);

  if (readOnly) {
    return (
      <div className="rounded-card border border-line bg-page p-4 text-center">
        <p className="text-[13px] text-ink-2">这个会话已结束或归档，只能阅读。</p>
      </div>
    );
  }

  return (
    <div className="relative">
      {/* 已选结构化标签在输入框上沿 */}
      {(draft.references.length > 0 || draft.capabilities.length > 0) && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {draft.capabilities.map((c) => (
            <TagChip
              key={`${c.extension_id}-${c.tool_id ?? ""}`}
              tone={capabilityReason(c) ? "dis" : "cap"}
              label={c.label}
              detail={`能力 v${c.extension_version}${c.tool_id ? ` · 工具 ${c.tool_id}` : ""}${capabilityReason(c) ? ` · ${capabilityReason(c)}` : ""}`}
              onRemove={() => onDraftChange({ ...draft, capabilities: draft.capabilities.filter((x) => x !== c) })}
            />
          ))}
          {draft.references.map((r) => (
            <TagChip
              key={r.id + r.kind}
              tone={r.unavailable_reason ? "dis" : r.selection_mode === "fixed" ? "fixed" : "case"}
              label={r.label ?? "关联文件"}
              detail={`${kindLabel(r.kind)} · ${r.selection_mode === "fixed" ? "固定历史版本" : "当前指针"}${r.detail ? ` · ${r.detail}` : ""}${r.unavailable_reason ? ` · ${r.unavailable_reason}` : ""}`}
              onRemove={() => onDraftChange({ ...draft, references: draft.references.filter((x) => x !== r) })}
            />
          ))}
        </div>
      )}

      {/* 附件托盘 */}
      {draft.attachments.length > 0 && (
        <div className="mb-2">
        {draft.attachments.length > 2 && <button type="button" className="flex w-full items-center gap-2 rounded-ctl border border-line bg-card px-3 py-2 text-[12px] text-ink-2" aria-expanded={attachmentsOpen} aria-controls={attachmentListId} onClick={() => setAttachmentsOpen(value => !value)}>
          <Icon name="paperclip" size={16} className="shrink-0 text-ink-3" />
          <span className="flex-1 text-left">{draft.attachments.length}份附件</span>
          <span className="num text-ink-3">{formatBytes(draft.attachments.reduce((total, item) => total + item.sizeBytes, 0))}</span>
          <UiText value={attachmentsOpen ? "收起附件" : "展开附件"} />
        </button>}
        {(draft.attachments.length <= 2 || attachmentsOpen) && <ul id={attachmentListId} className="mt-1 max-h-36 space-y-1 overflow-y-auto scroll-thin">
          {draft.attachments.map((a) => (
            <li key={a.uploadFileId} className="flex min-h-[52px] items-center gap-2 rounded-ctl border border-line bg-card px-2.5 py-2">
              <Icon name="paperclip" size={14} className="shrink-0 text-ink-3" />
              <span className="min-w-0 flex-1 break-words-all line-clamp-2 text-[12px] text-ink">{a.filename}</span>
              <span className="num shrink-0 text-[11px] text-ink-3">{formatBytes(a.sizeBytes)}</span>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => onDraftChange({ ...draft, attachments: draft.attachments.filter((x) => x !== a) })} aria-label={`移除附件 ${a.filename}`}>
                移除
              </button>
            </li>
          ))}
        </ul>}
        </div>
      )}

      {sendError && (
        <div className="mb-2">
          <ErrorBar text={sendError} action={onRetry ? { label: "重试发送", onSelect: onRetry } : undefined} />
        </div>
      )}

      <div ref={boxRef} className="rounded-card border border-line bg-card">
        <LexicalComposer initialConfig={initialConfig}>
          <EditorBridge
            editorRef={editorRef}
            onTextChange={(text, caret) => {
              if (text === localText.current) return;
              const grew = text.length > localText.current.length;
              localText.current = text;
              onDraftChange({ ...draft, text });
              if (pasting.current) {
                // 粘贴进来的斜杠、URL、路径不触发选择器
                pasting.current = false;
                setTrigger(null);
                return;
              }
              if (dismissedText.current !== null && text === dismissedText.current) return;
              dismissedText.current = null;
              // 只有用户实际键入内容时才可能打开浮层；纯移动光标不触发
              setTrigger(grew ? detectTrigger(text, caret) : null);
            }}
            onPaste={() => {
              pasting.current = true;
            }}
            onSubmit={() => {
              if (trigger) return false;
              if (canSend) onSend();
              return true;
            }}
            onEscape={() => {
              if (!trigger) return false;
              dismissedText.current = draft.text;
              setTrigger(null);
              return true;
            }}
            hasPopover={!!trigger}
          />
          <div className="relative">
          <RichTextPlugin
            contentEditable={
              <ContentEditable
                className="min-h-[88px] max-h-[240px] overflow-auto scroll-thin px-3 py-2.5 text-[14px] leading-relaxed text-ink outline-none [&:focus-visible]:outline-none"
                aria-label="输入消息"
                aria-describedby="composer-hint"
              />
            }
            placeholder={<p className="pointer-events-none absolute inset-x-3 top-2.5 text-[14px] leading-relaxed text-ink-3">描述要做什么；输入 / 选择能力，@ 关联已有单票</p>}
            ErrorBoundary={({ children }) => <>{children}</>}
          />
          </div>
        </LexicalComposer>

        <div className="flex items-center gap-2 border-t border-line px-2.5 py-2">
          <button type="button" className="btn btn-icon btn-ghost" onClick={onPickFiles} aria-label="添加文件">
            <Icon name="paperclip" size={20} />
          </button>
          {draft.attachments.length > 0 && <span className="num text-[11px] text-ink-3">{draft.attachments.length} 份附件</span>}
          <span id="composer-hint" className="flex-1 text-[11px] text-ink-3">
            {checkingCapabilities ? "正在核验所选能力" : unavailable.length ? `有 ${unavailable.length} 个标签已不可用，请移除或更换后再发送` : disabled && disabledReason ? disabledReason : "Enter 发送，Shift+Enter 换行"}
          </span>
          <span className="num text-[11px] text-ink-3">{draft.text.length}</span>
          <button type="button" className="btn btn-primary h-10" disabled={!canSend} onClick={onSend} title={disabled ? disabledReason : undefined}>
            {sending ? "发送中" : "发送"}
            <Icon name="send" size={16} />
          </button>
        </div>
      </div>

      {trigger?.kind === "capability" && <CapabilityPicker query={trigger.query} onPick={insertCapability} onClose={closePicker} />}
      {trigger?.kind === "reference" && <ReferencePicker query={trigger.query} caseId={caseId} onPick={insertReference}
        onCrossCase={(candidate) => { setCrossCaseCandidate(candidate); setCrossCaseError(null); setTrigger(null); }} onClose={closePicker} />}
      <Dialog open={!!crossCaseCandidate} onClose={() => { if (!creatingConversation) setCrossCaseCandidate(null); }} title="在新会话查看另一票"
        description={`将固定引用“${crossCaseCandidate?.title ?? ""}”。当前会话的消息、草稿和附件不会带入。`}
        footer={<><button type="button" className="btn btn-secondary" disabled={creatingConversation} onClick={() => setCrossCaseCandidate(null)}>取消</button>
          <button type="button" className="btn btn-primary" disabled={creatingConversation} onClick={async () => {
            if (!crossCaseCandidate) return;
            setCreatingConversation(true); setCrossCaseError(null);
            try { await onCrossCaseReference({ ...crossCaseCandidate.selection, selection_mode: "fixed" }); setCrossCaseCandidate(null); }
            catch (e) { setCrossCaseError(errorText(e)); }
            finally { setCreatingConversation(false); }
          }}>{creatingConversation ? "正在创建" : "创建新会话"}</button></>}>
        <p className="text-[13px] text-ink-2">新会话仅关联这次选择，服务端会重新核验访问权限；本人已确认记忆按现有规则使用。</p>
        {crossCaseError && <div className="mt-3"><ErrorBar text={crossCaseError} /></div>}
      </Dialog>
      <Dialog open={!!parameterCandidate} onClose={() => setParameterCandidate(null)} title="能力参数"
        description={`${parameterCandidate?.name ?? ""} · v${parameterCandidate?.extension_version ?? ""}。参数随本轮冻结。`}
        footer={<><button type="button" className="btn btn-secondary" onClick={() => setParameterCandidate(null)}>取消</button><button type="button" className="btn btn-primary"
          disabled={parameterCandidate?.parameters?.some((p) => p.required && (parameterValues[p.name] === undefined || parameterValues[p.name] === ""))}
          onClick={() => parameterCandidate && insertCapability(parameterCandidate, parameterValues)}>添加能力标签</button></>}>
        <div className="space-y-3">{parameterCandidate?.parameters?.map((p) => <label key={p.name} className="block space-y-1">
          <span>{p.name}{p.required ? "（必填）" : "（可选）"}</span>
          {p.description && <span className="block text-[12px] text-ink-3">{p.description}</span>}
          {p.type === "boolean" ? <select aria-label={p.name} className="input w-full" value={parameterValues[p.name] === undefined ? "" : String(parameterValues[p.name])}
            onChange={(e) => setParameterValues((old) => { const next = { ...old }; if (!e.target.value) delete next[p.name]; else next[p.name] = e.target.value === "true"; return next; })}>
            <option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>
          : p.type === "enum" ? <select aria-label={p.name} className="input w-full" value={String(parameterValues[p.name] ?? "")}
            onChange={(e) => setParameterValues((old) => ({ ...old, [p.name]: e.target.value }))}><option value="">请选择</option>{p.options?.map((o) => <option key={o} value={o}>{o}</option>)}</select>
          : <input aria-label={p.name} className="input w-full" value={String(parameterValues[p.name] ?? "")} maxLength={2000}
            onChange={(e) => setParameterValues((old) => ({ ...old, [p.name]: e.target.value }))} />}
        </label>)}</div>
      </Dialog>
    </div>
  );
}

/** 去掉尾部的触发片段（`/ xxx` 或 `@ xxx`），插入标签后不把触发文字留在正文里。 */
function stripTrigger(text: string) {
  return text.replace(/(^|[\s\n])[/@] [^\s\n]*$/, "$1");
}

/** 把编辑器正文整体替换为给定文本，供插入标签后清理触发词与外部填入示例使用。 */
function setEditorText(editor: LexicalEditor | null, text: string) {
  editor?.update(() => {
    const root = $getRoot();
    if (root.getTextContent() === text) return;
    root.clear();
    const para = $createParagraphNode();
    if (text) para.append($createTextNode(text));
    root.append(para);
    para.selectEnd();
  });
}

function EditorBridge({
  editorRef,
  onTextChange,
  onSubmit,
  onEscape,
  onPaste,
  hasPopover,
}: {
  editorRef: React.MutableRefObject<LexicalEditor | null>;
  onTextChange: (text: string, caret: number) => void;
  onSubmit: () => boolean;
  onEscape: () => boolean;
  onPaste: () => void;
  hasPopover: boolean;
}) {
  const [editor] = useLexicalComposerContext();
  editorRef.current = editor;

  useEffect(() => editor.registerCommand(PASTE_COMMAND, () => { onPaste(); return false; }, COMMAND_PRIORITY_LOW), [editor, onPaste]);

  useEffect(() => {
    return editor.registerCommand(
      KEY_DOWN_COMMAND,
      (e: KeyboardEvent) => {
        // 中文输入法组合中不触发发送与候选选择
        if (e.isComposing) return false;
        if (e.key === "Escape") return onEscape();
        if (e.key === "Enter" && !e.shiftKey) {
          if (hasPopover) return true;
          e.preventDefault();
          return onSubmit();
        }
        return false;
      },
      COMMAND_PRIORITY_LOW,
    );
  }, [editor, onSubmit, onEscape, hasPopover]);

  return (
    <OnChangePlugin
      onChange={(state) => {
        state.read(() => {
          const text = $getRoot().getTextContent();
          const sel = $getSelection();
          let caret = text.length;
          if ($isRangeSelection(sel) && sel.isCollapsed()) {
            // 近似光标位置：用锚点前的文本长度
            const anchorText = sel.anchor.getNode().getTextContent();
            const idx = text.lastIndexOf(anchorText);
            caret = idx >= 0 ? idx + sel.anchor.offset : text.length;
          }
          onTextChange(text, caret);
        });
      }}
    />
  );
}

function TagChip({ tone, label, detail, onRemove }: { tone: "case" | "fixed" | "cap" | "dis"; label: string; detail: string; onRemove: () => void }) {
  const [open, setOpen] = useState(false);
  const cls =
    tone === "dis"
      ? "border-line bg-dis-bg text-dis"
      : tone === "fixed"
        ? "border-[#8e93b5] bg-hist-bg text-hist"
        : tone === "cap"
          ? "tag-ref"
          : "tag-ref";
  return (
    <span className="relative">
      <span className={`inline-flex h-7 items-center gap-1 rounded-full border px-2 text-[12px] ${cls}`}>
        <button type="button" className="max-w-[220px] truncate" onClick={() => setOpen((v) => !v)} title={detail} aria-expanded={open}>
          {label}
        </button>
        <button type="button" className="flex h-10 w-10 items-center justify-center" onClick={onRemove} aria-label={`移除 ${label}`}>
          <Icon name="close" size={12} />
        </button>
      </span>
      {open && (
        <span className="absolute bottom-full left-0 z-pop mb-1 block w-[280px] rounded-ctl border border-line bg-card p-2 text-[11px] leading-relaxed text-ink-2 shadow-panel">{detail}</span>
      )}
    </span>
  );
}

function kindLabel(k: ReferenceSelection["kind"]) {
  return k === "case" ? "单票" : k === "run" ? "处理任务" : k === "document" ? "原件" : k === "section" ? "区段" : "成功快照";
}

/** `/ ` 候选：按内置 Skill／已启用 Skill／可用 MCP 工具分组。 */
function CapabilityPicker({ query, onPick, onClose }: { query: string; onPick: (c: CapabilityCandidate) => void; onClose: () => void }) {
  const [items, setItems] = useState<CapabilityCandidate[]>([]);
  const [warnings, setWarnings] = useState<{ code: string; message: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const generation = useQueryScope();
  const capabilityRevision = useSession(state => state.capabilityRevision);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true); setError(null); setWarnings([]); setItems([]); setActive(0);
    const t = setTimeout(() => {
      conversationApi
        .capabilities(query, ctrl.signal)
        .then((r) => { if (!ctrl.signal.aborted) { setItems(r.items); setWarnings(r.warnings ?? []); } })
        .catch((e) => {
          if (!ctrl.signal.aborted && (e as Error).name !== "AbortError") setError(errorText(e));
        })
        .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    }, 180);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [query, generation, retry, capabilityRevision]);

  return (
    <PickerShell label="可用能力" onClose={onClose} count={items.length} active={active} setActive={setActive} onEnter={index => items[index] && onPick(items[index])}>
      {warnings.map((warning) => <p key={warning.code} className="px-3 py-2 text-[12px] text-warn" role="status">{warning.message}</p>)}
      {loading && <p className="px-3 py-3 text-[12px] text-ink-2" role="status">正在读取可用能力…</p>}
      {error && <ErrorBar text={error} action={{label:"重试",onSelect:()=>setRetry(value=>value+1)}} />}
      {!loading && !error && !items.length && <p className="px-3 py-3 text-[12px] text-ink-2">没有匹配的能力。可以在 Skill / MCP 仓库里新建或启用。</p>}
      {(["内置 Skill", "已启用 Skill", "可用 MCP 工具"] as const).map((g) => {
        const rows = items.filter((x) => x.group === g);
        if (!rows.length) return null;
        return (
          <div key={g}>
            <p className="px-3 pt-2 text-[11px] text-ink-3"><UiText value={g} /></p>
            {rows.map((c) => {
              const idx = items.indexOf(c);
              const blocked = !!c.unavailable_reason || c.connection_status !== "可用";
              return (
                <button
                  key={`${c.extension_id}-${c.tool_id ?? ""}`}
                  type="button"
                  role="option"
                  aria-selected={idx === active}
                  data-picker-index={idx}
                  className={`flex min-h-[48px] w-full flex-col items-start gap-0.5 px-3 py-2 text-left ${idx === active ? "bg-fill" : ""} ${blocked ? "opacity-60" : ""}`}
                  disabled={blocked}
                  onFocus={() => setActive(idx)}
                  onPointerMove={event => { if (event.movementX || event.movementY) setActive(idx); }}
                  onClick={() => onPick(c)}
                >
                  <span className="flex w-full items-center gap-1.5">
                    <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">{c.kind === "builtin_skill" ? <UiText value={c.name} /> : c.name}</span>
                    <span className="num shrink-0 text-[11px] text-ink-3">v{c.extension_version}</span>
                    <span className="shrink-0 rounded bg-page px-1.5 text-[11px] text-ink-2"><UiText value={c.permission} /></span>
                  </span>
                  <span className="line-clamp-1 text-[12px] text-ink-2">{c.kind === "builtin_skill" ? <UiText value={c.purpose} /> : c.purpose}</span>
                  {blocked && <span className="text-[11px] text-risk">{c.unavailable_reason ?? `连接状态：${c.connection_status}`}</span>}
                </button>
              );
            })}
          </div>
        );
      })}
    </PickerShell>
  );
}

/** `@ ` 候选：按 Case／Run／原件／区段／成功快照分组；跨票候选不可加入。 */
function ReferencePicker({ query, caseId, onPick, onCrossCase, onClose }: { query: string; caseId: string | null; onPick: (c: ReferenceCandidate) => void; onCrossCase: (c: ReferenceCandidate) => void; onClose: () => void }) {
  const catalog = useDocumentCatalog();
  const toast = useToast();
  const [items, setItems] = useState<ReferenceCandidate[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [active, setActive] = useState(0);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [advanced, setAdvanced] = useState(false);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const updateFilter = (name: string, value: string) => {
    setFilters((previous) => ({ ...previous, [name]: value }));
    setError(null);
    setActive(0);
  };

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true); setError(null); setItems([]); setHasMore(false); setActive(0);
    const t = setTimeout(() => {
      conversationApi
        .searchReferences({ q: query || undefined, case_id: caseId ?? undefined, ...filters }, ctrl.signal)
        .then((r) => {
          if (ctrl.signal.aborted) return;
          setError(null);
          setItems(r.items);
          setHasMore(r.has_more ?? false);
        })
        .catch((e) => {
          if (!ctrl.signal.aborted && (e as Error).name !== "AbortError") setError(errorText(e));
        })
        .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    }, 180);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [query, caseId, filters, retry]);

  const flat = items.flatMap((i) => [i, ...(expanded.includes(referenceCandidateKey(i)) ? (i.children ?? []) : [])]);
  const toggleChildren = (candidate: ReferenceCandidate, open: boolean) => {
    const key = referenceCandidateKey(candidate);
    const index = flat.indexOf(candidate);
    const focused = document.activeElement instanceof HTMLElement && document.activeElement.hasAttribute("data-picker-index");
    setExpanded(previous => open ? [...new Set([...previous, key])] : previous.filter(id => id !== key));
    setActive(index);
    if (focused) requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-reference-picker] [data-picker-index="${index}"]`)?.focus({ preventScroll: true }));
  };
  const expandActive = (open: boolean, index: number) => {
    const current = flat[index];
    const parent = items.find(candidate => candidate.children?.includes(current));
    if (current?.children?.length) toggleChildren(current, open);
    else if (!open && parent) toggleChildren(parent, false);
  };

  const pick = (c: ReferenceCandidate) => {
    if (c.cross_case) {
      onCrossCase(c);
      return;
    }
    if (!c.selectable) {
      toast({ tone: "info", text: c.not_selectable_reason ?? "这个候选暂时不能加入" });
      return;
    }
    onPick(c);
  };

  return (
    <PickerShell label="关联单票、原件或快照" onClose={onClose} count={flat.length} active={active} setActive={setActive} onExpand={expandActive} onEnter={index => flat[index] && pick(flat[index])}>
      <div data-reference-filters className="border-b border-line px-3 py-2">
        <button type="button" className="btn btn-ghost btn-sm" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>高级筛选{Object.values(filters).some(Boolean) ? " · 已生效" : ""}</button>
        {advanced && <div className="mt-2 grid grid-cols-2 gap-2 text-[12px]">
          <label>日期口径<select aria-label="引用日期口径" className="input mt-1" value={filters.date_type ?? "activity"} onChange={event => updateFilter("date_type", event.target.value)}><option value="activity">最近活动</option><option value="uploaded">原件上传</option><option value="initiated">处理发起</option><option value="reviewed">复核</option><option value="exported">成功导出</option></select></label>
          <label>单据类型<div className="mt-1"><DocumentTypeSelect items={(catalog.data?.items ?? []).map(item => ({ ...item, code: item.code === "supporting" ? "other" : item.code === "unknown" ? "unclassified" : item.code }))} label="引用单据类型" placeholder="不限" value={filters.doc_type ?? ""} onChange={code => updateFilter("doc_type", code)} disabled={!catalog.data} /></div></label>
          <label>开始日期<input aria-label="引用开始日期" type="date" className="input mt-1" value={filters.local_date_from ?? ""} onChange={event => updateFilter("local_date_from", event.target.value)} /></label>
          <label>结束日期<input aria-label="引用结束日期" type="date" className="input mt-1" value={filters.local_date_to ?? ""} onChange={event => updateFilter("local_date_to", event.target.value)} /></label>
          <label>来源地角色<select aria-label="引用来源地角色" className="input mt-1" value={filters.origin_role ?? "origin"} onChange={event => updateFilter("origin_role", event.target.value)}><option value="origin">起运地</option><option value="receipt">承运人接货地</option><option value="pol">装货港</option><option value="country">原产国／地区</option></select></label>
          <label>来源地<input aria-label="引用来源地" className="input mt-1" value={filters.origin_value ?? ""} onChange={event => setFilters(previous => ({ ...previous, origin_role: previous.origin_role || "origin", origin_value: event.target.value }))} /></label>
          <label>收件人角色<select aria-label="引用收件人角色" className="input mt-1" value={filters.recipient_role ?? "consignee"} onChange={event => updateFilter("recipient_role", event.target.value)}><option value="consignee">收货人</option><option value="buyer">买方</option><option value="notify">通知方</option></select></label>
          <label>收件人<input aria-label="引用收件人" className="input mt-1" value={filters.recipient_value ?? ""} onChange={event => setFilters(previous => ({ ...previous, recipient_role: previous.recipient_role || "consignee", recipient_value: event.target.value }))} /></label>
          <label>处理人角色<select aria-label="引用处理人角色" className="input mt-1" value={filters.actor_role ?? "initiator"} onChange={event => updateFilter("actor_role", event.target.value)}><option value="initiator">发起人</option><option value="reviewer">复核人</option><option value="exporter">导出确认人</option></select></label>
          <label>处理人<input aria-label="引用处理人" className="input mt-1" value={filters.actor_value ?? ""} onChange={event => setFilters(previous => ({ ...previous, actor_role: previous.actor_role || "initiator", actor_value: event.target.value }))} /></label>
          <label>目标语言<select aria-label="引用目标语言" className="input mt-1" value={filters.language ?? ""} onChange={event => updateFilter("language", event.target.value)}><option value="">不限</option>{LANGUAGES.map(language => <option key={language.code} value={language.code}>{languageNativeName(language.code)}</option>)}</select></label>
          <label>版本范围<select aria-label="引用版本范围" className="input mt-1" value={filters.version_scope ?? "current"} onChange={event => updateFilter("version_scope", event.target.value)}><option value="current">最近成功快照／工作版</option><option value="include_history">包含历史成功快照</option></select></label>
          <label>条件匹配<select aria-label="引用条件匹配" className="input mt-1" value={filters.match_scope ?? "same_logical_document"} onChange={event => updateFilter("match_scope", event.target.value)}><option value="same_logical_document">同一逻辑单据</option><option value="any_in_case">同一批次任意文件</option></select></label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={filters.only_confirmed === "true"} onChange={event => updateFilter("only_confirmed", event.target.checked ? "true" : "")} />仅确认字段值</label>
          <p className="col-span-2 text-ink-3">日期由服务端按账户时区解释；摘要仅用于定位，引用在发送时再次鉴权并冻结版本。</p>
          <button type="button" className="btn btn-ghost btn-sm col-span-2" onClick={() => { setFilters({}); setError(null); }}>清除高级筛选</button>
        </div>}
      </div>
      {loading && <p className="px-3 py-3 text-[12px] text-ink-2" role="status">正在查找有权对象…</p>}
      {error && <ErrorBar text={error} action={{ label: "重试", onSelect: () => setRetry(value => value + 1) }} />}
      {!loading && !error && !flat.length && <p className="px-3 py-3 text-[12px] text-ink-2">没有匹配的有权对象。可以先上传单据，或换个关键字（文件名、单号、日期、类型、收件人、处理人）。</p>}
      {(["Case", "Run", "原件", "区段", "成功快照"] as const).map((g) => {
        const rows = items.filter((x) => x.group === g);
        if (!rows.length) return null;
        return (
          <div key={g}>
            <p className="px-3 pt-2 text-[11px] text-ink-3"><UiText value={g} /></p>
            {rows.map((c) => {
              const idx = flat.indexOf(c);
              const blocked = c.cross_case || !c.selectable;
              const open = expanded.includes(referenceCandidateKey(c));
              return (
                <div key={referenceCandidateKey(c) + c.group}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={idx === active}
                    aria-keyshortcuts={c.children?.length ? "ArrowRight ArrowLeft" : undefined}
                    aria-label={`${c.group} ${c.title} ${c.case_name} ${c.version_label} ${c.status_label}${c.children?.length ? open ? "，子项已展开，左方向键收起" : "，含子项，右方向键展开" : ""}${blocked ? `，${c.not_selectable_reason ?? "属于另一票，选择后确认新建会话"}` : ""}`}
                    data-picker-index={idx}
                    className={`flex min-h-[48px] w-full flex-col items-start gap-0.5 px-3 py-2 text-left ${idx === active ? "bg-fill" : ""} ${blocked ? "opacity-70" : ""}`}
                    onPointerMove={event => { if (event.movementX || event.movementY) setActive(idx); }}
                    onFocus={() => setActive(idx)}
                    onClick={() => pick(c)}
                  >
                    <span className="flex w-full items-center gap-1.5">
                      {c.children?.length ? (
                        <span
                          aria-hidden="true"
                          className="shrink-0 text-ink-3"
                          onClick={(e) => {
                            e.stopPropagation();
                            toggleChildren(c, !open);
                          }}
                        >
                          <Icon name={open ? "chevron-down" : "chevron-right"} size={12} />
                        </span>
                      ) : null}
                      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">{c.title}</span>
                      <span className="shrink-0 text-[11px] text-ink-3">{c.version_label}</span>
                    </span>
                    <span className="flex w-full flex-wrap gap-x-2 text-[11px] text-ink-3">
                      <span>{c.case_name}</span>
                      <span>{c.time_label}</span>
                      <span>{c.status_label}</span>
                      {c.summary_state && <span>{c.summary_state}</span>}
                      {c.hit_source && <span>命中：{c.hit_source}</span>}
                    </span>
                    {c.cross_case && <span className="text-[11px] text-warn">属于另一票，选择后可确认新建会话</span>}
                    {!c.cross_case && !c.selectable && <span className="text-[11px] text-warn">{c.not_selectable_reason}</span>}
                  </button>
                  {open &&
                    (c.children ?? []).map((ch) => {
                      const cidx = flat.indexOf(ch);
                      return (
                        <button
                          key={referenceCandidateKey(ch)}
                          data-picker-index={cidx}
                          type="button"
                          role="option"
                          aria-selected={cidx === active}
                          className={`flex min-h-[48px] w-full flex-col items-start gap-0.5 px-3 py-2 pl-8 text-left ${cidx === active ? "bg-fill" : ""} ${ch.selectable ? "" : "opacity-70"}`}
                          onPointerMove={event => { if (event.movementX || event.movementY) setActive(cidx); }}
                          onFocus={() => setActive(cidx)}
                          onClick={() => pick(ch)}
                        >
                          <span className="truncate text-[13px] text-ink">{ch.title}</span>
                          <span className="text-[11px] text-ink-3">
                            {ch.status_label} · {ch.version_label}
                          </span>
                          {!ch.selectable && <span className="text-[11px] text-warn">{ch.not_selectable_reason}</span>}
                        </button>
                      );
                    })}
                </div>
              );
            })}
          </div>
        );
      })}
      {hasMore && <p className="px-3 py-2 text-[11px] text-ink-3">还有更多结果，继续输入关键字缩小范围。</p>}
    </PickerShell>
  );
}

/** 候选浮层外壳：420 px 宽、最高 360 px，方向键只移动候选焦点。 */
function PickerShell({
  label,
  onClose,
  count,
  active,
  setActive,
  onEnter,
  onExpand,
  children,
}: {
  label: string;
  onClose: () => void;
  count: number;
  active: number;
  setActive: (n: number) => void;
  onEnter: (index: number) => void;
  onExpand?: (open: boolean, index: number) => void;
  children: React.ReactNode;
}) {
  const shell = useRef<HTMLDivElement>(null);
  const pickerId = useId();
  useEffect(() => { setActive(count ? Math.max(0, Math.min(active, count - 1)) : 0); }, [active, count, setActive]);
  useEffect(() => {
    const editor = document.querySelector('[contenteditable="true"]');
    if (!editor) return;
    shell.current?.querySelectorAll<HTMLElement>("[data-picker-index]").forEach(option => {
      option.id = `${pickerId}-option-${option.dataset.pickerIndex}`;
    });
    const option = shell.current?.querySelector<HTMLElement>(`[data-picker-index="${active}"]`);
    editor.setAttribute("aria-controls", pickerId);
    editor.setAttribute("aria-haspopup", "listbox");
    editor.setAttribute("aria-expanded", "true");
    if (option) editor.setAttribute("aria-activedescendant", option.id);
    else editor.removeAttribute("aria-activedescendant");
    return () => {
      for (const name of ["aria-controls", "aria-haspopup", "aria-expanded", "aria-activedescendant"]) editor.removeAttribute(name);
    };
  });
  const [position, setPosition] = useState<{ left: number; top: number; width: number; maxHeight: number } | null>(null);
  useEffect(() => {
    const measure = () => {
      const selection = window.getSelection();
      const editable = document.querySelector('[contenteditable="true"]');
      const rect = selection?.rangeCount && editable?.contains(selection.anchorNode) ? selection.getRangeAt(0).getBoundingClientRect() : null;
      const editor = editable?.getBoundingClientRect();
      const anchor = rect && (rect.width || rect.height) ? rect : editor;
      if (!anchor) return;
      const width = Math.min(420, window.innerWidth - 32);
      const above = anchor.top - 16;
      const below = window.innerHeight - anchor.bottom - 16;
      const maxHeight = Math.max(80, Math.min(360, Math.max(above, below)));
      setPosition({ width, maxHeight, left: Math.max(16, Math.min(window.innerWidth - width - 16, anchor.left)),
        top: above >= below ? Math.max(8, anchor.top - maxHeight - 8) : anchor.bottom + 8 });
    };
    measure();
    window.addEventListener("resize", measure);
    document.addEventListener("scroll", measure, true);
    document.addEventListener("selectionchange", measure);
    return () => { window.removeEventListener("resize", measure); document.removeEventListener("scroll", measure, true); document.removeEventListener("selectionchange", measure); };
  }, []);
  useEffect(() => { shell.current?.querySelector(`[data-picker-index="${active}"]`)?.scrollIntoView({ block: "nearest" }); }, [active]);
  const onKey = useCallback(
    (e: KeyboardEvent) => {
      // 高级筛选中的原生输入/下拉使用自己的键盘，不把Enter误当引用选择。
      if (e.target instanceof HTMLElement && e.target.closest('[data-reference-filters]') && e.key !== "Escape") return;
      if (e.target instanceof HTMLElement && !e.target.closest('[contenteditable="true"], [data-picker-index]') && e.key !== "Escape") return;
      // 中文输入法组词中不选择候选，也不发送
      if (e.isComposing || e.keyCode === 229) return;
      const focusedIndex = e.target instanceof HTMLElement ? e.target.closest<HTMLElement>("[data-picker-index]")?.dataset.pickerIndex : undefined;
      const index = focusedIndex === undefined ? active : Number(focusedIndex);
      const move = (next: number) => {
        const direction = next >= index ? 1 : -1;
        while (next >= 0 && next < count && shell.current?.querySelector(`[data-picker-index="${next}"]`)?.hasAttribute("disabled")) next += direction;
        if (next < 0 || next >= count) return;
        setActive(next);
        if (focusedIndex !== undefined) shell.current?.querySelector<HTMLElement>(`[data-picker-index="${next}"]`)?.focus({ preventScroll: true });
      };
      if (e.key === "ArrowDown") {
        e.preventDefault();
        move(count ? Math.min(count - 1, index + 1) : 0);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        move(Math.max(0, index - 1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        onEnter(index);
      } else if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && onExpand) {
        e.preventDefault();
        onExpand(e.key === "ArrowRight", index);
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    },
    [active, count, onEnter, onClose, onExpand, setActive],
  );

  useEffect(() => {
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onKey]);

  return (
    <div id={pickerId} ref={shell} data-reference-picker={onExpand ? true : undefined} style={position ?? undefined} className={`${position ? "fixed" : "absolute bottom-full left-0 w-[420px] max-h-[360px]"} z-pop max-w-full overflow-auto scroll-thin rounded-card border border-line bg-card shadow-panel`} role="listbox" aria-label={label}>
      {children}
    </div>
  );
}
