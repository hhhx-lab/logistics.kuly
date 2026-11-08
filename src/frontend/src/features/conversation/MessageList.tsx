import { languageName } from "../../i18n/languages";
// 消息流与各类卡片。卡片只渲染服务端给的结构化对象，
// 不从模型输出的 Markdown 猜 EvidenceTarget，也不把外部返回当已核验事实。
import type { AnswerPreview } from "./preview";
import { useStreamText } from "./useStreamText";
import { Fragment, useState } from "react";
import { errorText } from "../../api/client";
import type { EvidenceCardData, EvidenceTarget, MessageCard, MessageView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Markdown } from "../../components/Markdown";
import { useToast } from "../../components/Overlay";
import { ErrorBar, StatusPill, formatDateTime, formatTime, shortId } from "../../components/Status";
import { conversationApi, newKey, useCommand, useMemorySuggestions } from "./hooks";
import { preferencesApi } from "../../api/preferences";

export function MessageList({
  previews = [],
  messages,
  onOpenEvidence,
  onOpenRun,
  onEndService,
  onContinueInNew,
  onReask,
  onReveal,
}: {
  onReveal?: () => void;
  previews?: AnswerPreview[];
  messages: MessageView[];
  onOpenEvidence: (target: EvidenceTarget, intent: "quick" | "full") => void;
  onOpenRun: (runId: string) => void;
  onEndService: () => void;
  onContinueInNew: (caseId?: string | null) => void;
  onReask?: (m: MessageView) => void;
}) {
  const streamed = useStreamText(messages, previews, onReveal);
  const visible = previews.filter(preview => !messages.some(m => m.turn_id === preview.turnId && m.role === "assistant" && m.status === "completed"));
  const lastByTurn = new Map(messages.map(m => [m.turn_id, m.message_id]));
  return (
    <div className="space-y-5">
      {messages.map((m) => (
        <Fragment key={m.message_id}>
          <MessageRow m={m} displayText={m.role === "assistant" ? streamed[m.turn_id] : undefined} onOpenEvidence={onOpenEvidence} onOpenRun={onOpenRun} onEndService={onEndService} onContinueInNew={onContinueInNew} onReask={onReask} />
          {lastByTurn.get(m.turn_id) === m.message_id && visible.filter(p => p.turnId === m.turn_id).map(preview => (
            <PreviewRow key={preview.id} preview={preview} displayText={streamed[preview.turnId]} onOpenEvidence={onOpenEvidence} />
          ))}
        </Fragment>
      ))}
      {visible.filter(p => !lastByTurn.has(p.turnId)).map(preview => <PreviewRow key={preview.id} preview={preview} displayText={streamed[preview.turnId]} onOpenEvidence={onOpenEvidence} />)}
    </div>
  );
}

function PreviewRow({ preview, displayText, onOpenEvidence }: { preview: AnswerPreview; displayText?: string; onOpenEvidence: (target: EvidenceTarget, intent: "quick" | "full") => void }) {
  const text = displayText ?? preview.text;
  if (!text && preview.status === "generating") return null;
  return (
    <div className="flex gap-3" aria-live="off">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full brand-tile" aria-hidden><Icon name="logo" size={18} /></span>
      <div className="min-w-0 max-w-[85%] space-y-1.5">
        <p className="text-[11px] text-ink-3">单证助手{preview.status === "interrupted" && " · 生成已中断"}</p>
        {text && <div className="max-w-[620px] whitespace-pre-wrap break-words rounded-card border border-line bg-card px-3 py-2.5 text-[14px] leading-relaxed">{text}</div>}
        {!!preview.cards.length && <EvidenceCards cards={preview.cards} onOpen={target => onOpenEvidence(target, "quick")} />}
      </div>
    </div>
  );
}

function MessageRow({
  m,
  displayText,
  onOpenEvidence,
  onOpenRun,
  onEndService,
  onContinueInNew,
  onReask,
}: {
  m: MessageView;
  displayText?: string;
  onOpenEvidence: (target: EvidenceTarget, intent: "quick" | "full") => void;
  onOpenRun: (runId: string) => void;
  onEndService: () => void;
  onContinueInNew: (caseId?: string | null) => void;
  onReask?: (m: MessageView) => void;
}) {
  const toast = useToast();
  const mine = m.role === "user";
  const namedReferences = m.references.filter(reference => reference.label?.trim());
  const namedCapabilities = m.capabilities.filter(capability => capability.label?.trim());
  const [expanded, setExpanded] = useState(false);
  const [sourceOpen, setSourceOpen] = useState(false);
  // 长答复折叠，但首段结论与来源入口始终可见
  const text = displayText ?? m.text;
  const long = !mine && text.split("\n").length > 12;

  if (m.restricted) {
    return (
      <div className="flex justify-start">
        <div className="max-w-[620px] rounded-card border border-line bg-page p-3 text-[12px] text-ink-2">这条消息引用的对象已不在你的权限范围内，正文不再显示。</div>
      </div>
    );
  }

  return (
    <div id={`message-${m.message_id}`} tabIndex={-1} className={`flex gap-3 ${mine ? "flex-row-reverse" : ""}`}>
      <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[12px] ${mine ? "bg-ink font-medium text-white" : "brand-tile"}`} aria-hidden>
        {mine ? m.author.slice(0, 1) : <Icon name="logo" size={18} />}
      </span>
      <div className={`min-w-0 max-w-[85%] ${mine ? "items-end text-right" : ""} flex flex-col gap-1.5`}>
        <p className="text-[11px] text-ink-3">
          {mine ? m.author : "单证助手"}
          {m.status === "interrupted" ? " · 生成已中断" : m.status !== "generating" && ` · ${formatTime(m.created_at)}`}
        </p>

        {m.text && !(!mine && m.cards.some(card => card.type === "system_error" && card.text === m.text)) && (
          <div className={`max-w-[620px] rounded-card px-3 py-2.5 text-left ${mine ? "bg-fill-strong" : "border border-line bg-card"}`}>
            <Markdown text={long && !expanded ? text.split("\n").slice(0, 12).join("\n") : text} />
            {long && (
              <button type="button" className="mt-1 text-[12px] text-brand-ink underline underline-offset-2" onClick={() => setExpanded((v) => !v)}>
                {expanded ? "收起" : "展开全文"}
              </button>
            )}
          </div>
        )}

        {m.historical_version_notice && (
          <p className="max-w-[620px] rounded-ctl bg-hist-bg px-2.5 py-1.5 text-left text-[12px] text-hist">{m.historical_version_notice}</p>
        )}

        {/* 只显示具名引用；隐式延续和冻结来源不重复生成附件按钮。 */}
        {mine && (namedReferences.length > 0 || namedCapabilities.length > 0) && (
          <div className="flex max-w-[620px] flex-wrap justify-end gap-1.5">
            {namedCapabilities.map((c) => (
              <span key={c.extension_id + (c.tool_id ?? "")} className="tag tag-ref text-[11px]" title={`能力 v${c.extension_version}`}>
                {c.label}
              </span>
            ))}
            {namedReferences.map((r) => (
              <span key={r.id + r.kind} className={`tag text-[11px] ${r.unavailable_reason ? "border-line bg-dis-bg text-dis" : r.selection_mode === "fixed" ? "border-[#8e93b5] bg-hist-bg text-hist" : "tag-ref"}`} title={r.unavailable_reason ?? (r.selection_mode === "fixed" ? "固定历史版本" : "当前指针")}>
                {r.label}
              </span>
            ))}
          </div>
        )}
        {m.cards.filter(card => card.type !== "evidence").map((card, i) => (
          <div key={i} className="max-w-[620px] text-left">
            <CardView card={card} onOpenEvidence={onOpenEvidence} onOpenRun={onOpenRun} onEndService={onEndService} onContinueInNew={onContinueInNew} />
          </div>
        ))}

        {!!m.cards.some(card => card.type === "evidence") && <EvidenceCards cards={m.cards.flatMap(card => card.type === "evidence" ? card.cards : [])} onOpen={target => onOpenEvidence(target, "quick")} />}

        {/* read_index 的异步续答挂在原问题下，不新建一个用户轮次 */}
        {m.continuation && (
          <div className="max-w-[620px] rounded-card border border-line bg-page p-3 text-left">
            <div className="flex items-center gap-1.5">
              <StatusPill
                tone={m.continuation.status === "completed" ? "ok" : ["failed", "cancelled", "restricted", "conversation_closed"].includes(m.continuation.status) ? "risk" : "info"}
                icon={m.continuation.status === "completed" ? "check" : ["failed", "cancelled", "restricted", "conversation_closed"].includes(m.continuation.status) ? "alert" : "clock"}
                busy={["pending", "running"].includes(m.continuation.status)}
              >
                {CONT_TEXT[m.continuation.status]}
              </StatusPill>
            </div>
            <p className="mt-1.5 text-[13px] leading-relaxed text-ink">{m.continuation.text}</p>
            {m.continuation.next_action && <p className="mt-1 text-[12px] text-ink-2">下一步：{m.continuation.next_action}</p>}
          </div>
        )}

        {!mine && (m.status === "completed" || m.status === "interrupted") && (
          <div className="flex flex-wrap gap-1.5">
            {m.status === "completed" && (
              <>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void navigator.clipboard.writeText(m.text).then(() => toast({ tone: "ok", text: "已复制这条回答的可见文字" }))}
                >
                  <Icon name="copy" size={12} />
                  复制
                </button>
                {!m.cards.some(card => card.type === "evidence") && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setSourceOpen((v) => !v)} aria-expanded={sourceOpen}>
                  <Icon name="source" size={12} />
                  来源／版本
                </button>}
              </>
            )}
            {m.status === "interrupted" && onReask && (
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => onReask(m)}>
                重新提问
              </button>
            )}
          </div>
        )}
        {sourceOpen && (
          <div className="max-w-[620px] rounded-ctl border border-line bg-page p-2.5 text-left text-[11px] leading-relaxed text-ink-2">
            <p>这条回答固定在发送时冻结的版本上；后续修订不会改写它。</p>
            {m.references.length > 0 ? (
              <ul className="mt-1 space-y-0.5">
                {m.references.map((r) => (
                  <li key={r.id + r.kind}>
                    {r.label} · {r.selection_mode === "fixed" ? "固定历史版本" : "当前指针"}
                    {r.unavailable_reason ? ` · ${r.unavailable_reason}` : ""}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1">这条回答没有固定的单票引用。</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

const CONT_TEXT: Record<string, string> = {
  pending: "已登记续答，等待识别完成",
  running: "正在识别，完成后在这里补答",
  awaiting_retry: "识别失败，等待重试",
  awaiting_reconcile: "外部请求待核查",
  completed: "已补答",
  failed: "识别失败，未能补答",
  cancelled: "已取消",
  restricted: "权限已变化，不再显示续答内容",
  conversation_closed: "会话已结束",
};

function CardView({
  card,
  onOpenEvidence,
  onOpenRun,
  onEndService,
  onContinueInNew,
}: {
  card: MessageCard;
  onOpenEvidence: (target: EvidenceTarget, intent: "quick" | "full") => void;
  onOpenRun: (runId: string) => void;
  onEndService: () => void;
  onContinueInNew: (caseId?: string | null) => void;
}) {
  switch (card.type) {
    case "knowledge_source":
      return <a href={card.url} target="_blank" rel="noopener noreferrer" className="block rounded-card border border-line bg-card px-3 py-2 text-ink hover:border-line-strong focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus">
        <span className="text-[11px] text-ink-3">知识来源 · {card.version}</span>
        <p className="mt-1 text-[13px] font-medium">{card.title} ↗</p>
        <p className="mt-1 text-[11px] text-ink-3">{card.scope}</p>
      </a>;
    case "evidence":
      return <EvidenceCards cards={card.cards} onOpen={(t) => onOpenEvidence(t, "quick")} />;

    case "adopted_value":
      return (
        <div className="card p-3">
          <p className="mb-1.5 text-[12px] font-medium text-ink">人工采用值</p>
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="rounded-ctl border border-line p-2">
              <p className="text-[11px] text-ink-3">原文写的是</p>
              <p className="mt-0.5 text-[13px] text-ink">{card.original}</p>
              <p className="mt-1 text-[11px] text-ink-3">原文只证明文件上的写法</p>
            </div>
            <div className="rounded-ctl border border-brand-line bg-brand-soft p-2">
              <p className="text-[11px] text-ink-3">复核后采用</p>
              <p className="mt-0.5 text-[13px] font-medium text-ink">{card.adopted}</p>
            </div>
          </div>
          <p className="mt-1.5 text-[11px] text-ink-3">
            {card.field} · {card.by} · {formatDateTime(card.at)}
          </p>
          <p className="mt-1 text-[12px] text-ink-2">理由：{card.reason}</p>
          {card.basis && <p className="mt-0.5 text-[12px] text-ink-2">补充依据：{card.basis}</p>}
          <button type="button" className="btn btn-secondary btn-sm mt-2" onClick={() => onOpenEvidence(card.target, "full")}>
            查看复核记录
          </button>
        </div>
      );

    case "translation":
      return (
        <div className="card p-3">
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusPill tone={card.state === "valid" ? "ok" : card.state === "draft" ? "warn" : "dis"} icon={card.state === "valid" ? "check" : card.state === "draft" ? "warn" : "history"}>
              {card.state === "valid" ? "有效译文" : card.state === "draft" ? "草稿译文" : "历史译文"}
            </StatusPill>
            <span className="text-[11px] text-ink-3">
              {languageName(card.target_language)} · Run {shortId(card.run_id)} · 译文版 {card.translation_revision} · 预览 {card.preview_version}
            </span>
          </div>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <div className="rounded-ctl bg-page p-2 text-[13px] text-ink">{card.source}</div>
            <div className="rounded-ctl border border-line p-2 text-[13px] text-ink">{card.translated}</div>
          </div>
          <button type="button" className="btn btn-secondary btn-sm mt-2" onClick={() => onOpenEvidence(card.target, "full")}>
            在双语对照中定位
          </button>
        </div>
      );

    case "issue":
      return (
        <div className={`card p-3 ${card.severity === "blocking" ? "border-risk-line" : card.severity === "warning" ? "border-warn-line" : ""}`}>
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusPill tone={card.severity === "blocking" ? "risk" : card.severity === "warning" ? "warn" : "info"} icon={card.severity === "blocking" ? "alert" : card.severity === "warning" ? "warn" : "question"}>
              {card.severity === "blocking" ? "高风险／阻断" : card.severity === "warning" ? "待确认" : "无法判断"}
            </StatusPill>
            <span className="text-[12px] text-ink-2">{card.category}</span>
            <span className="text-[11px] text-ink-3">规则 {card.rule_name}</span>
          </div>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {[card.a, card.b].filter(Boolean).map((side, i) => (
              <button key={i} type="button" className="rounded-ctl border border-line p-2 text-left" onClick={() => onOpenEvidence(side!.target, "quick")}>
                <p className="text-[11px] text-ink-3">{side!.label}</p>
                <p className="num mt-0.5 text-[13px] text-ink">{side!.value}</p>
              </button>
            ))}
          </div>
          {card.difference && <p className="num mt-1.5 text-[12px] text-ink-2">差值：{card.difference}</p>}
          <p className="mt-1.5 text-[12px] text-ink-2">待处理：{card.pending_action}</p>
          <button type="button" className="btn btn-secondary btn-sm mt-2" onClick={() => onOpenEvidence(card.a.target, "full")}>
            查看问题
          </button>
        </div>
      );

    case "tool_call":
      return <ToolCallCard card={card} />;

    case "memory_suggestion":
      return <MemorySuggestionCard card={card} />;

    case "mcp_challenge":
      return <ChallengeCard challengeId={card.challenge_id} />;

    case "system_error":
      return <ErrorBar text={card.text} tone={card.kind === "permission_changed" ? "warn" : "risk"} />;

    case "run_link":
      return (
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenRun(card.run_id)}>
          查看这次处理
        </button>
      );

    case "read_prompt":
      return <p className="rounded-card border border-line bg-page p-3 text-[12px] leading-relaxed text-ink-2">{card.text}</p>;

    case "completion":
      return (
        <div className="card p-4">
          <p className="text-[14px] font-medium text-ink">{card.ended ? "本次会话已结束" : "本次服务状态"}</p>
          <p className="mt-1 text-[13px] text-ink-2">{card.status_text}</p>
          {!card.snapshot_id && <p className="mt-1 text-[12px] text-ink-3">后台任务和已接收原件仍可在单据管理台查看。</p>}
          <div className="mt-2 flex flex-wrap gap-2">
            {card.run_id && <button type="button" className="btn btn-secondary btn-sm" onClick={() => card.run_id && onOpenRun(card.run_id)}>
              查看管理记录
            </button>}
            {!card.ended && (
              <button type="button" className="btn btn-secondary btn-sm" onClick={onEndService}>
                结束本次服务
              </button>
            )}
            {card.ended && (
              <button type="button" className="btn btn-primary btn-sm" onClick={() => onContinueInNew()}>
                在新对话继续
              </button>
            )}
          </div>
        </div>
      );
  }
}

/** 按物理文件和版本归组，引用位置仅在展开后显示。 */
function EvidenceCards({ cards, onOpen }: { cards: EvidenceCardData[]; onOpen: (t: EvidenceTarget) => void }) {
  const [open, setOpen] = useState(false);
  const groups = new Map<string, { translated: boolean; cards: EvidenceCardData[] }>();
  for (const card of cards) {
    const t = card.target, translated = !!(t.translation_revision_id || t.translation_segment_id);
    const key = [t.case_id, t.document_id, t.document_version, translated ? "translation" : "source", translated ? t.translation_revision_id : t.parse_artifact_id].join(":");
    const group = groups.get(key) ?? { translated, cards: [] };
    if (!group.cards.some(c => c.card_id === card.card_id && c.target.field_id === t.field_id && c.target.translation_segment_id === t.translation_segment_id)) group.cards.push(card);
    groups.set(key, group);
  }
  return <div className="w-full max-w-[620px] text-left">
    <button type="button" className="flex items-center gap-1.5 py-1 text-[12px] text-ink-2 hover:text-brand-ink" aria-expanded={open} onClick={() => setOpen(v => !v)}>
      <Icon name="source" size={12} />来源 · {groups.size} 份文件<span className="text-ink-3">{open ? "收起" : "展开"}</span>
    </button>
    {open && <div className="max-h-64 overflow-y-auto scroll-thin rounded-ctl border border-line bg-card">
      {[...groups].map(([key, group]) => <details key={key} className="border-b border-line last:border-0">
        <summary className="cursor-pointer px-3 py-2 text-[12px] text-ink hover:bg-page">
          <span className="mr-2 text-ink-3">{group.translated ? "翻译文件" : "源文件"}</span>
          <span className="break-all">{group.cards[0].filename}</span>
          <span className="ml-2 text-[11px] text-ink-3">{group.cards.length} 处引用</span>
        </summary>
        <ul className="space-y-1 border-t border-line bg-page p-2">
          {group.cards.map((c, index) => <li key={c.card_id + ":" + index}>
            <button type="button" className="w-full rounded-ctl px-2 py-1.5 text-left hover:bg-card" onClick={() => onOpen(c.target)} title={c.versions}>
              <span className="flex gap-2 text-[11px] text-ink-3">{c.location}<span>{c.logical_document}</span><span>{c.status === "conflict" ? "来源有冲突" : c.status === "historical" ? "历史版本" : c.status === "confirmed" ? "已确认" : "待核对"}</span></span>
              <span className="mt-0.5 block line-clamp-2 break-words text-[12px] text-ink-2">{c.excerpt}</span>
            </button>
          </li>)}
        </ul>
      </details>)}
    </div>}
  </div>;
}

function ToolCallCard({ card }: { card: Extract<MessageCard, { type: "tool_call" }> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Icon name="puzzle" size={14} className="text-ink-3" />
        <span className="text-[12px] font-medium text-ink">{card.name}</span>
        <span className="num text-[11px] text-ink-3">v{card.version}</span>
        <StatusPill tone={card.status === "succeeded" ? "ok" : card.status === "failed" ? "risk" : "info"} icon={card.status === "succeeded" ? "check" : card.status === "failed" ? "alert" : "clock"} busy={card.status === "running"}>
          {card.status === "succeeded" ? "成功" : card.status === "failed" ? "失败" : "执行中"}
        </StatusPill>
      </div>
      <p className="mt-1 text-[12px] text-ink-2">{card.purpose}</p>
      {card.external_source && <p className="mt-1 rounded-ctl bg-hist-bg px-2 py-1 text-[11px] text-hist">外部工具返回 · {card.external_source}。外部资料不作为本票已核验事实。</p>}
      {card.error && <p className="mt-1 text-[12px] text-risk">{card.error}</p>}
      <button type="button" className="mt-1.5 text-[11px] text-brand-ink underline underline-offset-2" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        {open ? "收起参数" : "展开脱敏参数"}
      </button>
      {open && <pre className="mono mt-1 whitespace-pre-wrap break-words-all rounded-ctl bg-page p-2 text-[11px] text-ink-2">{card.params_redacted}</pre>}
    </div>
  );
}

/** 记忆建议卡：未确认不进入任何新会话上下文。 */
function MemorySuggestionCard({ card }: { card: Extract<MessageCard, { type: "memory_suggestion" }> }) {
  const toast = useToast();
  const suggestions = useMemorySuggestions();
  const confirm = useCommand<void, unknown>((_a, key) => preferencesApi.confirmSuggestion(card.suggestion_id, key), ["memories", "memory-suggestions", "c-ctx"]);
  const ignore = useCommand<void, unknown>((_a, key) => preferencesApi.ignoreSuggestion(card.suggestion_id, key), ["memory-suggestions"]);
  const still = (suggestions.data?.items ?? []).some((s) => s.suggestion_id === card.suggestion_id);

  return (
    <div className="card border-brand-line bg-brand-soft p-3">
      <p className="text-[12px] font-medium text-ink">记住这个偏好？</p>
      <p className="mt-1 text-[13px] text-ink">{card.content}</p>
      <p className="mt-1 text-[11px] text-ink-3">类别：{card.category}。确认后只对之后的新回合生效；不确认就不会保存。</p>
      {suggestions.isPending ? (
        <p className="mt-2 text-[12px] text-ink-2">正在核对这条建议的当前状态…</p>
      ) : suggestions.isError ? (
        <div className="mt-2"><ErrorBar text={errorText(suggestions.error)} action={{ label: "重新核对", onSelect: () => void suggestions.refetch() }} /></div>
      ) : still ? (
        <div className="mt-2 flex gap-2">
          <button type="button" className="btn btn-primary btn-sm" disabled={confirm.isPending} onClick={() => confirm.mutate(undefined, { onSuccess: () => toast({ tone: "ok", text: "已保存为长期偏好" }) })}>
            确认保存
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={ignore.isPending} onClick={() => ignore.mutate(undefined, { onSuccess: () => toast({ tone: "ok", text: "这次不保存" }) })}>
            这次不保存
          </button>
        </div>
      ) : (
        <p className="mt-2 text-[12px] text-ink-2">这条建议已处理。可以在「设置 → 我的记忆」里查看或修改。</p>
      )}
      {(confirm.error || ignore.error) && <div className="mt-2"><ErrorBar text={errorText(confirm.error ?? ignore.error)} /></div>}
    </div>
  );
}

/** MCP 一次性外发授权卡：用户确认前工具收不到任何数据。 */
function ChallengeCard({ challengeId }: { challengeId: string }) {
  const toast = useToast();
  const [view, setView] = useState<import("../../api/types").McpChallengeView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useState(() => {
    conversationApi
      .challenge(challengeId)
      .then(setView)
      .catch((e) => setError(errorText(e)));
    return null;
  });

  const act = async (approve: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const v = approve ? await conversationApi.approveChallenge(challengeId, newKey()) : await conversationApi.rejectChallenge(challengeId, newKey());
      setView(v);
      toast({ tone: approve ? "ok" : "info", text: approve ? "已同意本次发送" : "已取消这次外部调用，未发送任何数据" });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  if (!view) return error ? <ErrorBar text={error} /> : <div className="h-20 animate-pulse rounded-card bg-page" aria-hidden />;

  const pending = view.status === "pending";
  return (
    <div className="card border-warn-line bg-warn-bg p-3">
      <p className="text-[13px] font-medium text-warn">外部工具数据授权</p>
      <dl className="mt-2 grid gap-x-3 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr]">
        <dt className="text-ink-3">服务名称</dt>
        <dd className="text-ink">{view.service_name}</dd>
        <dt className="text-ink-3">目的地</dt>
        <dd className="mono text-ink">{view.destination}</dd>
        <dt className="text-ink-3">工具</dt>
        <dd className="mono text-ink">{view.tool}</dd>
        <dt className="text-ink-3">用途</dt>
        <dd className="text-ink">{view.purpose}</dd>
        <dt className="text-ink-3">数据级别</dt>
        <dd className="text-ink">{view.data_level}</dd>
        <dt className="text-ink-3">是否保留结果</dt>
        <dd className="text-ink">{view.retains_result ? "对方会保留" : "对方不保留"}</dd>
      </dl>
      <p className="mt-2 text-[11px] text-ink-3">将发送的范围</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[12px] text-ink">
        {view.scope.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ul>
      <p className="mt-1.5 text-[11px] text-ink-3">有效期至 {formatDateTime(view.expires_at)}。同意只覆盖本次列明的范围；服务端仍会再次检查身份、版本与工具权限。</p>
      {pending ? (
        <div className="mt-2 flex gap-2">
          <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => void act(true)}>
            同意本次发送
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void act(false)}>
            取消调用
          </button>
        </div>
      ) : (
        <p className="mt-2 text-[12px] text-ink-2">
          {view.status === "approved" ? "已同意本次发送。" : view.status === "rejected" ? "已取消，未向外部工具发送任何数据。" : "这次授权已过期，请重新提问。"}
        </p>
      )}
      {error && <div className="mt-2"><ErrorBar text={error} /></div>}
    </div>
  );
}
