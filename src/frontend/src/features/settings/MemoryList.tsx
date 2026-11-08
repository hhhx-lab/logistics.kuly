import { useInterfaceText } from "./useInterfaceText";
// 我的记忆：已确认的长期偏好、待确认建议与添加／编辑表单。
// 只保存本人可复用的表达偏好；单票金额、收发货人、地址、单号与密钥一律拒绝。
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { errorText } from "../../api/client";
import type { MemoryCategory, MemoryPreview, MemorySuggestion, MemoryView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { EmptyState, ErrorBar, StatusPill, formatDateTime } from "../../components/Status";
import { useSession } from "../../stores/session";
import { preferencesApi, useMemories, useSettingsCommand, useSuggestions } from "./hooks";

const CATEGORY_LABEL: Record<MemoryCategory, string> = {
  answer_language: "回答语言与术语解释",
  target_language: "常用目标语言",
  presentation: "输出呈现",
  terminology: "获确认术语偏好",
};

const CATEGORY_HINT: Record<MemoryCategory, string> = {
  answer_language: "例如「解释物流术语时优先用简体中文」。只影响表达，不会把英文目标语言的任务改成中文。",
  target_language: "例如「常用英文译本」。对未说明目标语言的翻译只给一键建议，你点击确认后才会启动任务。",
  presentation: "例如「先看风险清单」或常用的 DOCX＋PDF 展示方式。不会跳过复核或改变已冻结的任务格式。",
  terminology: "个人常用称呼及说明。与领域事实、审核术语或当轮明确要求冲突时，不覆盖后者。",
};

const CATEGORY_HINT_EN: Record<MemoryCategory, string> = {
  "answer_language": "Explain logistics terms in Simplified Chinese; this affects expression, not the task target language.",
  "target_language": "Prefer English translations when no language is specified. A task starts only after you confirm the suggestion.",
  "presentation": "Show risks first, or prefer DOCX and PDF. This does not bypass review or change frozen task formats.",
  "terminology": "Personal terms do not override domain facts, review terminology or explicit instructions."
};

const ORIGIN_LABEL: Record<MemorySuggestion["origin"], string> = {
  explicit_request: "来自本次明确请求",
  repeated_setting: "重复设置建议",
};

export function MemorySection() {
  const uiText = useInterfaceText();
  const memories = useMemories();
  const suggestions = useSuggestions();
  const [editing, setEditing] = useState<{ memory: MemoryView | null } | null>(null);
  // 记录打开编辑器的按钮，关闭后把焦点还回去
  const triggerRef = useRef<HTMLElement | null>(null);
  const [categoryFilter, setCategoryFilter] = useState<MemoryCategory | "all">("all");
  const addRef = useRef<HTMLButtonElement>(null);

  const items = (memories.data?.items ?? []).filter((m) => categoryFilter === "all" || m.category === categoryFilter).sort((a, b) => b.confirmed_at.localeCompare(a.confirmed_at));

  return (
    <div className="max-w-[800px]">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-[16px] font-semibold text-ink">我的记忆</h2>
        </div>
        <button
          ref={addRef}
          type="button"
          className="btn btn-primary h-10"
          onClick={(e) => {
            triggerRef.current = e.currentTarget;
            setEditing({ memory: null });
          }}
        >
          <Icon name="plus" size={16} />
          添加偏好
        </button>
      </div>

      <MemorySuggestionList />

      <div className="mt-5 flex flex-wrap items-center gap-1.5">
        <span className="text-[12px] text-ink-3">按类别筛选</span>
        {(["all", "answer_language", "target_language", "presentation", "terminology"] as const).map((c) => (
          <button key={c} type="button" className={`tag ${categoryFilter === c ? "tag-on" : "border-line"}`} onClick={() => setCategoryFilter(c)} aria-pressed={categoryFilter === c}>
            {c === "all" ? "全部" : CATEGORY_LABEL[c]}
          </button>
        ))}
      </div>

      <div className="mt-3 space-y-2">
        {memories.isPending && [0, 1].map((i) => <div key={i} className="h-[88px] animate-pulse rounded-card bg-page" aria-hidden />)}
        {memories.isError && <ErrorBar text={uiText(errorText(memories.error), "Unable to load preferences. Retry to refresh your account data.")} action={{ label: "重试", onSelect: () => void memories.refetch() }} />}
        {!memories.isPending && !memories.isError && !items.length && (
          <EmptyState
            icon="memory"
            title={categoryFilter === "all" ? "尚未保存长期偏好" : "这个类别下还没有偏好"}
            description={categoryFilter === "all" ? uiText("新的会话仍可正常使用；需要引用历史单据时，在对话里用 @ 关联具体单票。", "New conversations work without saved preferences. Use @ to link a specific historical case.") : uiText("可以切换到全部类别查看其他偏好。", "Select all categories to see other preferences.")}
            action={
              <button type="button" className="btn btn-secondary" onClick={(e) => { triggerRef.current = e.currentTarget; return categoryFilter === "all" ? setEditing({ memory: null }) : setCategoryFilter("all"); }}>
                {categoryFilter === "all" ? "添加偏好" : "查看全部"}
              </button>
            }
          />
        )}
        {items.map((m) => (
          <MemoryRow key={m.memory_id} memory={m} onEdit={() => setEditing({ memory: m })} returnFocus={() => addRef.current?.focus({ preventScroll: true })} />
        ))}
      </div>

      {editing && (
        <MemoryEditor
          key={editing.memory?.memory_id ?? "new"}
          memory={editing.memory}
          onClose={() => {
            setEditing(null);
            const t = triggerRef.current;
            requestAnimationFrame(() => (document.body.contains(t) ? t : addRef.current)?.focus({ preventScroll: true }));
          }}
        />
      )}
    </div>
  );
}

function MemorySuggestionList() {
  const uiText = useInterfaceText();
  const toast = useToast();
  const suggestions = useSuggestions();
  // 逐条记录提交中的建议与错误，一条失败不影响其他条
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<Record<string, string>>({});
  const confirm = useSettingsCommand<string, unknown>((id, key) => preferencesApi.confirmSuggestion(id, key), ["memories", "memory-suggestions", "c-ctx"]);
  const ignore = useSettingsCommand<string, unknown>((id, key) => preferencesApi.ignoreSuggestion(id, key), ["memory-suggestions"]);
  const items = suggestions.data?.items ?? [];
  if (suggestions.isError)
    return (
      <div className="mt-4">
        <ErrorBar text={uiText(errorText(suggestions.error), "Unable to load pending suggestions. Retry to refresh.")} action={{ label: "重试", onSelect: () => void suggestions.refetch() }} />
      </div>
    );
  if (!items.length) return null;

  return (
    <section className="mt-4 rounded-card border border-brand-line bg-brand-soft p-4" aria-label="待确认的记忆建议">
      <p className="text-[13px] font-medium text-ink">待确认建议</p>
      <p className="mt-0.5 text-[12px] text-ink-2">{uiText("尚未保存。未确认的建议不会进入任何新会话的上下文。", "Not saved. Unconfirmed suggestions are never loaded into new conversations.")}</p>
      <ul className="mt-2 space-y-2">
        {items.map((s) => (
          <li key={s.suggestion_id} className="rounded-ctl border border-line bg-card p-3">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="tag border-line bg-page text-[12px] text-ink-2">{CATEGORY_LABEL[s.category]}</span>
              <StatusPill tone="info" icon="info">
                尚未保存
              </StatusPill>
            </div>
            <p className="mt-1.5 text-[13px] leading-relaxed text-ink">{s.content}</p>
            {s.applies_when && <p className="mt-1 text-[12px] text-ink-2">适用条件：{s.applies_when}</p>}
            <p className="mt-1 text-[11px] text-ink-3">
              {ORIGIN_LABEL[s.origin]} · {formatDateTime(s.created_at)}
              {!s.source_restricted && s.from_conversation ? ` · 来自会话「${s.from_conversation}」` : ""}
            </p>
            {s.source_restricted && <p className="mt-0.5 text-[11px] text-ink-3">{uiText("来源会话已不可访问，这里只显示建议本身，不展示那段聊天原文。", "The source conversation is unavailable. Only this suggestion is displayed, without the original chat.")}</p>}
            <div className="mt-2 flex gap-2">
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={busyId === s.suggestion_id}
                onClick={() => {
                  setBusyId(s.suggestion_id);
                  confirm.mutate(s.suggestion_id, {
                    onSuccess: () => { setBusyId(null); setRowError((p) => ({ ...p, [s.suggestion_id]: "" })); toast({ tone: "ok", text: uiText("已确认，之后的新回合会读取这条偏好", "Confirmed. Future turns will read this preference.") }); },
                    onError: (e) => { setBusyId(null); setRowError((p) => ({ ...p, [s.suggestion_id]: uiText(errorText(e), "The suggestion request failed. Retry or reload the latest version.") })); },
                  });
                }}
              >
                确认保存
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busyId === s.suggestion_id}
                onClick={() => {
                  setBusyId(s.suggestion_id);
                  ignore.mutate(s.suggestion_id, {
                    onSuccess: () => { setBusyId(null); toast({ tone: "ok", text: uiText("已忽略这条建议，没有保存任何内容", "Suggestion ignored; nothing was saved.") }); },
                    onError: (e) => { setBusyId(null); setRowError((p) => ({ ...p, [s.suggestion_id]: uiText(errorText(e), "The suggestion request failed. Retry or reload the latest version.") })); },
                  });
                }}
              >
                这次不保存
              </button>
            </div>
            {rowError[s.suggestion_id] && <div className="mt-2"><ErrorBar text={rowError[s.suggestion_id]} /></div>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function MemoryRow({ memory, onEdit, returnFocus }: { memory: MemoryView; onEdit: (trigger: HTMLElement) => void; returnFocus: () => void }) {
  const uiText = useInterfaceText();
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const del = useSettingsCommand<void, unknown>((_a, key) => preferencesApi.deleteMemory(memory.memory_id, memory.version, key), ["memories", "c-ctx"]);
  // 长文本折叠不能把适用条件截掉，否则偏好含义会变
  const long = memory.content.length > 52;

  return (
    <>
      <div className="card card-hover flex min-h-[88px] flex-col gap-2 p-5 sm:flex-row sm:items-start">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="tag border-line bg-page text-[12px] text-ink-2">{CATEGORY_LABEL[memory.category]}</span>
            <StatusPill tone="ok" icon="check">
              已确认
            </StatusPill>
            <span className="text-[11px] text-ink-3">{memory.source === "manual" ? "我手动添加" : "我确认的建议"}</span>
          </div>
          <p className={`mt-1.5 text-[13px] leading-relaxed text-ink ${expanded || !long ? "" : "line-clamp-2"}`}>{memory.content}</p>
          {/* 适用条件始终可见：截掉它会改变偏好含义 */}
          {memory.applies_when && <p className="mt-1 text-[12px] text-ink-2">适用条件：{memory.applies_when}</p>}
          {long && (
            <button type="button" className="mt-1 text-[12px] text-brand-ink underline underline-offset-2" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
              {expanded ? "收起内容" : "展开内容"}
            </button>
          )}
          <button type="button" className="mt-1 flex items-center gap-1 text-[11px] text-ink-3" onClick={() => setShowSource((v) => !v)} aria-expanded={showSource}>
            <Icon name={showSource ? "chevron-down" : "chevron-right"} size={12} />
            查看来源
          </button>
          {showSource && (
            <div className="mt-1 rounded-ctl bg-page px-2.5 py-2 text-[11px] leading-relaxed text-ink-2">
              <p>确认时间：{formatDateTime(memory.confirmed_at)}</p>
              <p>来源类型：{memory.source === "manual" ? "我手动添加" : "我确认的建议"}</p>
              <p className="num">版本：v{memory.version}</p>
              {memory.source_restricted ? <p className="mt-0.5">来源会话已不可访问，这里不显示那段聊天原文。</p> : memory.source_label ? <p className="mt-0.5">{memory.source_label}</p> : null}
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2 sm:flex-col sm:items-end">
          <span className="text-[11px] text-ink-3">
            更新于 {formatDateTime(memory.confirmed_at)} · <span className="num">v{memory.version}</span>
          </span>
          <div className="flex gap-2">
            <button type="button" className="btn btn-ghost btn-sm" onClick={(e) => onEdit(e.currentTarget)}>
              编辑
            </button>
            <button type="button" className="btn btn-ghost btn-sm text-risk" onClick={() => setConfirmDelete(true)}>
              删除
            </button>
          </div>
        </div>
      </div>

      <Dialog
        open={confirmDelete}
        onClose={() => { setConfirmDelete(false); returnFocus(); }}
        title="删除这条偏好"
        width={480}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setConfirmDelete(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={del.isPending}
              onClick={() =>
                del.mutate(undefined, {
                  onSuccess: () => {
                    setConfirmDelete(false);
                    toast({ tone: "ok", text: uiText("已删除，之后的新回合不再读取这条偏好", "Deleted. Future turns will no longer read this preference.") });
                  },
                })
              }
            >
              确认删除
            </button>
          </>
        }
      >
        <p className="text-[12px] text-ink-3">
          {CATEGORY_LABEL[memory.category]} · <span className="num">v{memory.version}</span>
        </p>
        <p className="mt-1 rounded-ctl bg-page px-3 py-2 text-[13px] leading-relaxed text-ink">{memory.content}</p>
        {memory.applies_when && <p className="mt-1 text-[12px] text-ink-2">适用条件：{memory.applies_when}</p>}
        <p className="mt-2 text-[12px] leading-relaxed text-ink-2">
          {uiText("删除后今后的回合不再读取这条偏好；已经完成的回答、正在运行的任务和已导出的文件都不会改变，历史审计按保留规则处理，其他用户不可见。", "Future turns will stop reading this preference. Completed answers, running tasks and exported files keep their versions. Audit records follow retention rules and remain private.")}
        </p>
        {del.error && <div className="mt-2"><ErrorBar text={uiText(errorText(del.error), "Unable to delete this preference. Retry or refresh its latest version.")} /></div>}
      </Dialog>
    </>
  );
}

/**
 * 拒绝提示：写明原因，并对单票事实给出「到管理台查看」「在新对话 @ 该票」两个入口，
 * 不让用户只看到一句「不能保存」。
 */
function RejectNote({ text }: { text: string }) {
  const uiText = useInterfaceText();
  const navigate = useNavigate();
  const isCaseFact = /单票业务资料/.test(text);
  return (
    <span className="mt-1 block text-[12px] text-risk">
      {uiText(text, isCaseFact ? "Case facts cannot be stored as preferences. Open Document Management or link the case with @ in a new conversation." : "This preference could not be accepted. Check the selected category and content.")}
      {isCaseFact && (
        <span className="mt-1 flex flex-wrap gap-2">
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate("/documents")}>
            打开单据管理台
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate("/conversation?hint=mention")}>
            在新对话中 @ 该票
          </button>
        </span>
      )}
    </span>
  );
}

function MemoryEditor({ memory, onClose }: { memory: MemoryView | null; onClose: () => void }) {
  const uiText = useInterfaceText();
  const toast = useToast();
  const identity = useSession((st) => st.identity);
  const [category, setCategory] = useState<MemoryCategory>(memory?.category ?? "answer_language");
  const [content, setContent] = useState(memory?.content ?? "");
  const [appliesWhen, setAppliesWhen] = useState(memory?.applies_when ?? "");
  const [preview, setPreview] = useState<MemoryPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [confirmSave, setConfirmSave] = useState(false);
  const [askClose, setAskClose] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 服务端 422 指出的字段，用于把错误显示在对应输入下方
  const [serverField, setServerField] = useState<string | null>(null);
  const [conflictWith, setConflictWith] = useState<MemoryView | null>(null);
  const [expectedVersion, setExpectedVersion] = useState(memory?.version ?? 0);
  const baseline = useRef(memory);
  const panel = useRef<HTMLElement>(null);

  const save = useSettingsCommand<void, MemoryView>((_a, key) =>
    memory
      ? preferencesApi.editMemory(memory.memory_id, { category, content: content.trim(), applies_when: appliesWhen.trim() || undefined, expected_version: expectedVersion }, key)
      : preferencesApi.createMemory({ category, content: content.trim(), applies_when: appliesWhen.trim() || undefined }, key),
  ["memories", "c-ctx"]);

  const dirty = content.trim() !== (baseline.current?.content ?? "") || category !== (baseline.current?.category ?? "answer_language") || appliesWhen.trim() !== (baseline.current?.applies_when ?? "");
  const closeGuarded = () => (dirty ? setAskClose(true) : onClose());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      e.stopPropagation();
      closeGuarded();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  });

  useEffect(() => {
    panel.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus({ preventScroll: true });
  }, []);

  const change = (fn: () => void) => {
    fn();
    setPreview(null);
    setError(null);
    setServerField(null);
    setConflictWith(null);
    save.renewKey();
  };

  /** 保存失败时把错误落到对应字段，并关闭确认层，避免反馈被遮住。 */
  const onSaveError = (e: unknown) => {
    const err = e as { status?: number; details?: { field?: string }; current?: MemoryView | null };
    setConfirmSave(false);
    setError(uiText(errorText(e), "The preference request failed. Check its content or reload the latest version and retry."));
    if (err.status === 422 && err.details?.field) {
      setServerField(err.details.field);
      requestAnimationFrame(() => panel.current?.querySelector<HTMLElement>(`[data-field="${err.details!.field}"]`)?.focus({ preventScroll: true }));
    }
    if (err.status === 409 && err.current) setConflictWith(err.current);
  };

  const doPreview = async () => {
    setPreviewing(true);
    setError(null);
    try {
      // 预览只读：不写库、不调用模型
      setPreview(await preferencesApi.previewMemory({ category, content: content.trim(), applies_when: appliesWhen.trim() || undefined }));
    } catch (e) {
      setError(uiText(errorText(e), "The preference request failed. Check its content or reload the latest version and retry."));
    } finally {
      setPreviewing(false);
    }
  };

  const tooLong = content.trim().length > 120;
  // 前端只做即时反馈；敏感内容与单票事实最终由服务端拒绝
  const looksLikeSecret = /\b(sk|pk|api[-_]?key|token|bearer)[-_ ]?[A-Za-z0-9]{8,}/i.test(content) || /-----BEGIN/.test(content);
  const rejectField = preview?.rejected_field ?? serverField ?? "content";
  const rejectText = preview?.rejected_reason ?? (serverField ? error : null);
  const rejectFor = (field: string) => (rejectText && rejectField === field ? rejectText : null);
  const blocked = !content.trim() || tooLong || looksLikeSecret || !!preview?.rejected_reason;
  const blockReason = !content.trim()
    ? uiText("请填写偏好内容", "Enter preference content.")
    : tooLong
      ? uiText(`内容 ${content.trim().length} 字，超过 120 字上限`, `${content.trim().length} characters; the limit is 120.`)
      : looksLikeSecret
        ? uiText("内容里出现了疑似密钥。密钥只能通过部署配置管理，不能保存为长期偏好", "This content appears to contain a secret. Manage secrets through deployment configuration; they cannot be stored as preferences.")
        : (preview?.rejected_reason ?? null);

  return (
    <>
      <aside ref={panel} className="fixed inset-y-0 right-0 z-panel flex w-[560px] max-w-full flex-col border-l border-line bg-card shadow-panel anim-slide-in" role="dialog" aria-label={memory ? "编辑偏好" : "添加偏好"}>
        <header className="flex h-bar shrink-0 items-center gap-2 border-b border-line px-4">
          <div className="min-w-0 flex-1">
            <p className="text-[14px] font-medium text-ink">{memory ? "编辑偏好" : "添加偏好"}</p>
            {memory && <p className="num text-[11px] text-ink-3">当前 v{expectedVersion}，保存后形成新版本</p>}
          </div>
          <button type="button" className="btn btn-icon btn-ghost" onClick={closeGuarded} aria-label="关闭">
            <Icon name="close" size={16} />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-auto scroll-thin p-4">
          <fieldset>
            <legend className="label mb-1.5">类别</legend>
            <div className="flex flex-wrap gap-1.5">
              {(Object.keys(CATEGORY_LABEL) as MemoryCategory[]).map((c) => (
                <label key={c} className={`tag cursor-pointer ${category === c ? "tag-on" : "border-line"}`}>
                  <input type="radio" name="memory-category" className="sr-only" checked={category === c} onChange={() => change(() => setCategory(c))} />
                  {CATEGORY_LABEL[c]}
                </label>
              ))}
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-ink-3">{uiText(CATEGORY_HINT[category], CATEGORY_HINT_EN[category])}</p>
            {rejectFor("category") && <RejectNote text={rejectFor("category")!} />}
          </fieldset>

          <label className="mt-4 block">
            <span className="label">偏好内容</span>
            <textarea className="input mt-1" rows={3} data-autofocus data-field="content" value={content} aria-invalid={!!blockReason} onChange={(e) => change(() => setContent(e.target.value))} />
            <span className={`mt-1 block text-[11px] ${tooLong ? "text-risk" : "text-ink-3"}`}>
              <span className="num">{content.trim().length}</span> / 120 字
            </span>
            {rejectFor("content") && <RejectNote text={rejectFor("content")!} />}
          </label>

          <label className="mt-3 block">
            <span className="label">适用条件（可选）</span>
            <input className="input mt-1" data-field="applies_when" value={appliesWhen} aria-invalid={!!rejectFor("applies_when")} onChange={(e) => change(() => setAppliesWhen(e.target.value))} placeholder={uiText("例如：仅在我没有明确说目标语言时", "For example: only when I have not specified a target language.")} />
            {rejectFor("applies_when") && <RejectNote text={rejectFor("applies_when")!} />}
          </label>

          <p className="mt-3 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
            {uiText("只保存可复用的表达偏好。某票的金额、收发货人、地址、单号与原件全文属于单票业务资料，请到单据管理台查看，或在新会话中用 @ 关联该票；密钥只能通过部署配置管理。", "Save reusable expression preferences only. Amounts, parties, addresses, document numbers and source text belong to cases; view them in Document Management or link a case with @. Manage secrets through deployment configuration.")}
          </p>

          {preview && !preview.rejected_reason && (
            <div className="mt-3 rounded-card border border-line p-3">
              <p className="text-[13px] font-medium text-ink">预览影响</p>
              <p className="mt-1.5 text-[11px] text-ink-3">{uiText("未来的新回合可能这样表达", "Future turns may use these expressions.")}</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[12px] text-ink">
                {preview.affects.map((a, i) => (
                  <li key={i}>{uiText(a, a)}</li>
                ))}
              </ul>
              <p className="mt-2 text-[11px] text-ink-3">{uiText("不会改变", "Does not change")}</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[12px] text-ink-2">
                {preview.not_affects.map((a, i) => (
                  <li key={i}>{uiText(a, a)}</li>
                ))}
              </ul>
              <p className="mt-2 text-[11px] text-ink-3">{uiText("预览只读，不会调用模型，也不会保存任何内容。", "Preview is read-only. It calls no model and saves no content.")}</p>
            </div>
          )}

          {conflictWith && (
            <div className="mt-3 rounded-card border border-warn-line bg-warn-bg p-3">
              <p className="text-[13px] font-medium text-warn">{uiText("这条偏好已在别处更新", "This preference was updated elsewhere.")}</p>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <div className="rounded-ctl border border-line bg-card p-2.5 text-[12px]">
                  <p className="mb-1 text-[11px] text-ink-3">服务端当前 v{conflictWith.version}</p>
                  <p className="text-ink">{conflictWith.content}</p>
                  {conflictWith.applies_when && <p className="mt-0.5 text-ink-2">适用条件：{conflictWith.applies_when}</p>}
                </div>
                <div className="rounded-ctl border border-brand-line bg-card p-2.5 text-[12px]">
                  <p className="mb-1 text-[11px] text-ink-3">你的草稿</p>
                  <p className="text-ink">{content.trim()}</p>
                  {appliesWhen.trim() && <p className="mt-0.5 text-ink-2">适用条件：{appliesWhen.trim()}</p>}
                </div>
              </div>
              <div className="mt-2 flex gap-2">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => {
                  setContent(conflictWith.content); setAppliesWhen(conflictWith.applies_when ?? ""); setCategory(conflictWith.category);
                  setExpectedVersion(conflictWith.version); baseline.current = conflictWith;
                  setConflictWith(null); setError(null); setServerField(null); setPreview(null); save.renewKey();
                }}>
                  重新加载服务端版本
                </button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => void navigator.clipboard.writeText(content.trim()).then(() => toast({ tone: "ok", text: uiText("草稿已复制到剪贴板", "Draft copied to clipboard.") }))}>
                  复制我的草稿
                </button>
              </div>
            </div>
          )}
          {error && !conflictWith && (
            <div className="mt-3">
              <ErrorBar text={error} action={{ label: "重试保存", onSelect: () => save.mutate(undefined, { onSuccess: () => { toast({ tone: "ok", text: uiText("已保存", "Saved.") }); onClose(); }, onError: onSaveError }) }} />
            </div>
          )}
        </div>

        <footer className="flex shrink-0 items-center gap-2 border-t border-line px-4 py-3">
          <span className="flex-1 text-[11px] text-ink-3">{blockReason ?? uiText("可以先预览影响，再保存并确认", "Preview the effect before saving and confirming.")}</span>
          <button type="button" className="btn btn-secondary" onClick={closeGuarded}>
            取消
          </button>
          <button type="button" className="btn btn-outline" onClick={() => void doPreview()} disabled={!content.trim() || previewing}>
            {previewing ? "正在预览" : "预览影响"}
          </button>
          <button type="button" className="btn btn-primary" disabled={blocked} onClick={() => setConfirmSave(true)} title={blockReason ?? undefined}>
            保存并确认
          </button>
        </footer>
      </aside>

      <Dialog
        open={confirmSave}
        onClose={() => setConfirmSave(false)}
        title="保存并确认这条偏好"
        description={uiText("确认后它会写入你本人的记忆版本，之后的新回合才开始读取。", "Confirmation saves a version of your memory for future turns.")}
        width={480}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setConfirmSave(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-primary"
              data-autofocus
              disabled={save.isPending}
              onClick={() =>
                save.mutate(undefined, {
                  onSuccess: () => {
                    setConfirmSave(false);
                    toast({ tone: "ok", text: memory ? uiText("已保存为新版本，之后的新回合生效", "Saved as a new version for future turns.") : uiText("已确认保存，之后的新回合生效", "Confirmed and saved for future turns.") });
                    onClose();
                  },
                  onError: onSaveError,
                })
              }
            >
              确认保存
            </button>
          </>
        }
      >
        <dl className="grid gap-x-3 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr]">
          <dt className="text-ink-3">写入账户</dt>
          <dd className="text-ink">{identity?.display_name ?? "当前登录用户"}</dd>
          <dt className="text-ink-3">类别</dt>
          <dd className="text-ink">{CATEGORY_LABEL[category]}</dd>
          <dt className="text-ink-3">完整内容</dt>
          <dd className="leading-relaxed text-ink">{content.trim()}</dd>
          {appliesWhen.trim() && (
            <>
              <dt className="text-ink-3">适用条件</dt>
              <dd className="text-ink">{appliesWhen.trim()}</dd>
            </>
          )}
        </dl>
        <p className="mt-2 text-[12px] leading-relaxed text-ink-2">
          {uiText("影响范围：仅对你本人的新回合生效。正在运行的回合、已完成的回答、已冻结的任务要求和已导出的文件都不会改变。", "Applies to your future turns only. Running turns, completed answers, frozen tasks and exported files keep their existing versions.")}
        </p>
      </Dialog>

      <Dialog
        open={askClose}
        onClose={() => setAskClose(false)}
        title="还有未保存的内容"
        description={uiText("尚未保存的偏好内容会被丢弃。", "Unsaved preference changes will be discarded.")}
        width={420}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setAskClose(false)}>
              继续编辑
            </button>
            <button type="button" className="btn btn-danger" onClick={onClose}>
              丢弃
            </button>
          </>
        }
      />
    </>
  );
}
