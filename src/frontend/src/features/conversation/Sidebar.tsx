import { useNavigationPreference } from "../../stores/navigation";
import { UiText } from "../../i18n/runtime";
// 左侧会话导航与顶栏。会话列表只搜索本人有权的标题与时间，
// 不搜索单据正文，也不把历史会话摘要带进新会话。
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { errorText } from "../../api/client";
import type { ContextView, ConversationView, RunView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, Menu, Popover, useToast } from "../../components/Overlay";
import { ErrorBar, RunStatusPill, StatusPill, formatTime, shortId } from "../../components/Status";
import { useSession } from "../../stores/session";
import { authApi } from "../../api/auth";
import { conversationApi, newKey, useCommand, useContextView, useConversations } from "./hooks";
import { useQueryClient } from "@tanstack/react-query";

export function ConversationSidebar({
  currentId,
  hasDraft,
  onNewConversation,
  onOpen,
  onRename,
  onContinueInNew,
}: {
  currentId: string | null;
  hasDraft: boolean;
  onNewConversation: () => void;
  onOpen: (id: string) => void;
  onRename: (id: string) => void;
  onContinueInNew: (caseId?: string | null) => void;
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const identity = useSession((s) => s.identity);
  const setAnonymous = useSession((s) => s.setAnonymous);
  const { collapsed, toggle } = useNavigationPreference();
  const [searchOpen, setSearchOpen] = useState(false);
  const [q, setQ] = useState("");
  const [askNew, setAskNew] = useState(false);
  const [pendingOpen, setPendingOpen] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const avatarRef = useRef<HTMLButtonElement>(null);
  const list = useConversations(q);

  const startNew = () => {
    // 当前有未发送草稿时先确认；保留时草稿只留在旧会话，不复制到新会话
    if (hasDraft) return setAskNew(true);
    onNewConversation();
  };

  return (
    <aside data-app-nav className={`flex shrink-0 flex-col border-r border-line bg-nav ${collapsed ? "w-navc" : "w-nav"}`}>
      <div className={collapsed ? "flex flex-col items-center gap-1.5 py-3" : "flex h-bar items-center gap-2 px-3"}>
        <span className="brand-tile flex h-8 w-8 shrink-0 items-center justify-center rounded-ctl">
          <Icon name="logo" size={20} />
        </span>
        {!collapsed && <span className="min-w-0 flex-1 truncate text-[14px] font-semibold text-ink">物流单证智能体</span>}
        <button type="button" className="btn btn-icon btn-ghost h-8 w-8" onClick={toggle} aria-label={collapsed ? "展开导航" : "收起导航"}>
          <Icon name={collapsed ? "expand" : "collapse"} size={16} />
        </button>
      </div>

      <div className="px-2 pb-2">
        <button type="button" className="btn btn-secondary h-10 w-full" onClick={startNew} aria-label="新对话" title={hasDraft ? "当前会话有未发送草稿，会先询问" : undefined}>
          <Icon name="plus" size={16} />
          {!collapsed && "新对话"}
        </button>
      </div>

      {collapsed && (
        <nav className="space-y-0.5 px-2" aria-label="其他页面">
          {([["/documents", "单据管理台", "docs"], ["/extensions", "Skill / MCP", "puzzle"]] as const).map(([to, label, icon]) => (
            <button key={to} type="button" className="flex h-10 w-full items-center justify-center rounded-ctl text-ink-4 hover:bg-fill hover:text-ink-2" onClick={() => navigate(to)} aria-label={label} title={label}>
              <Icon name={icon} size={18} />
            </button>
          ))}
        </nav>
      )}

      {!collapsed && (
        <div className="flex min-h-0 flex-1 flex-col px-2">
          <div className="flex h-8 items-center gap-1">
            {searchOpen ? (
              <input
                className="input h-8 text-[12px]"
                autoFocus
                placeholder="按标题或时间搜索"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    setQ("");
                    setSearchOpen(false);
                  }
                }}
                aria-label="搜索最近会话"
              />
            ) : (
              <>
                <span className="flex-1 text-[11px] text-ink-3">最近会话</span>
                <button type="button" className="btn btn-icon btn-ghost h-8 w-8" onClick={() => setSearchOpen(true)} aria-label="搜索最近会话">
                  <Icon name="search" size={14} />
                </button>
              </>
            )}
          </div>

          <ul className="min-h-0 flex-1 space-y-0.5 overflow-auto scroll-thin">
            {list.isPending && [0, 1, 2].map((i) => <li key={i} className="h-12 animate-pulse rounded-ctl bg-page" aria-hidden />)}
            {list.isError && <li><ErrorBar text={errorText(list.error)} action={{ label: "重试", onSelect: () => void list.refetch() }} /></li>}
            {!list.isPending && !list.data?.items.length && <li className="px-2 py-3 text-[12px] text-ink-3">{q ? "没有匹配的会话。" : "还没有会话。点「新对话」开始。"}</li>}
            {(list.data?.items ?? []).map((c) =>
              c.restricted ? (
                <li key={c.conversation_id} className="rounded-ctl bg-page px-2 py-2 text-[12px] text-ink-3">
                  这个会话已不在你的权限范围内，内容不再显示。
                </li>
              ) : (
              <li key={c.conversation_id} className="group relative">
                <button
                  type="button"
                  className={`w-full rounded-ctl px-2 py-1.5 pr-9 text-left ${currentId === c.conversation_id ? "bg-fill-strong" : "hover:bg-fill"}`}
                  onClick={() => (hasDraft && c.conversation_id !== currentId ? setPendingOpen(c.conversation_id) : onOpen(c.conversation_id))}
                  aria-current={currentId === c.conversation_id ? "true" : undefined}
                >
                  <span className="block truncate text-[13px] text-ink" title={c.title}>
                    {c.title === "新对话" ? <UiText value={c.title} /> : c.title}
                  </span>
                  <span className="mt-0.5 flex items-center gap-1.5 text-[11px] text-ink-3">
                    <span className="shrink-0 whitespace-nowrap num">{formatTime(c.updated_at)}</span>
                    {c.case_name && <span className="min-w-0 truncate">{c.case_name}{c.case_status_label ? ` · ${c.case_status_label}` : ""}</span>}
                    {c.status === "ended" && <span className="shrink-0">已结束</span>}
                  </span>
                </button>
                <RowMenu conv={c} onOpen={onOpen} onRename={onRename} onContinue={onContinueInNew} onArchived={() => { if (currentId === c.conversation_id) onNewConversation(); }} />
              </li>
              ),
            )}
            {list.hasNextPage && <li><button type="button" className="btn btn-ghost btn-sm w-full" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage({ cancelRefetch: false })}>
              {list.isFetchingNextPage ? "正在读取更多会话" : "加载更多会话"}
            </button></li>}
          </ul>

          <nav className="space-y-0.5 border-t border-line py-2" aria-label="其他页面">
            {([["/documents", "单据管理台", "docs"], ["/extensions", "Skill / MCP", "puzzle"]] as const).map(([to, label, icon]) => (
              <button key={to} type="button" className="group flex h-9 w-full items-center gap-2.5 rounded-ctl px-2 text-left text-[13px] text-ink-2 hover:bg-fill hover:text-ink" onClick={() => navigate(to)}>
                <Icon name={icon} size={18} className="text-ink-4 group-hover:text-ink-2" />
                {label}
              </button>
            ))}
          </nav>
        </div>
      )}

      <div className="border-t border-line p-2">
        <button ref={avatarRef} type="button" className="flex h-10 w-full items-center gap-2 rounded-ctl px-2 text-left hover:bg-fill" onClick={() => setMenuOpen(true)} aria-label="账号菜单">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-ink text-[12px] font-medium text-white">{identity?.avatar_initial}</span>
          {!collapsed && <span className="min-w-0 flex-1 truncate text-[13px] text-ink">{identity?.display_name}</span>}
        </button>
        <Menu
          open={menuOpen}
          onClose={() => setMenuOpen(false)}
          anchor={avatarRef}
          label="账号菜单"
          align="start"
          items={[
            { label: "设置", icon: "settings", onSelect: () => navigate("/settings") },
            { label: "我的记忆", icon: "memory", onSelect: () => navigate("/settings/memory") },
            {
              label: "退出登录",
              icon: "logout",
              separatorBefore: true,
              onSelect: async () => {
                await authApi.logout().catch(() => undefined);
                qc.clear();
                setAnonymous();
              },
            },
          ]}
        />
      </div>

      <Dialog
        open={pendingOpen !== null}
        onClose={() => setPendingOpen(null)}
        title="切换会话前先处理未发送的内容"
        width={460}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setPendingOpen(null)}>
              留在当前会话
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                const id = pendingOpen!;
                setPendingOpen(null);
                onOpen(id);
              }}
            >
              切换过去
            </button>
          </>
        }
      >
        <p className="text-[12px] leading-relaxed text-ink-2">草稿只保留在当前会话里，不会被带到另一个会话。</p>
      </Dialog>

      <Dialog
        open={askNew}
        onClose={() => setAskNew(false)}
        title="当前会话还有未发送的内容"
        width={460}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setAskNew(false)}>
              留在旧会话
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setAskNew(false);
                onNewConversation();
              }}
            >
              新建会话
            </button>
          </>
        }
      >
        <p className="text-[12px] leading-relaxed text-ink-2">
          选择留下时，草稿只保留在当前会话里。新建的会话不会复制这段文字、附件、单票或引用标签；已经在后台执行的处理任务继续运行。
        </p>
      </Dialog>
    </aside>
  );
}

/** 会话行尾更多菜单：按状态给出可用动作，不提供「把历史消息加入新上下文」。 */
function RowMenu({
  conv,
  onOpen,
  onRename,
  onContinue,
  onArchived,
}: {
  conv: import("../../api/types").ConversationSummary;
  onOpen: (id: string) => void;
  onRename: (id: string) => void;
  onContinue: (caseId?: string | null) => void;
  onArchived: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [askArchive, setAskArchive] = useState(false);
  const toast = useToast();
  const archive = useCommand((_args: void, key: string) => conversationApi.archive(conv.conversation_id, conv.conversation_version, key), ["c-list", "c-conv"]);
  const ref = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button
        ref={ref}
        type="button"
        className="absolute right-1 top-1 flex h-8 w-8 items-center justify-center rounded-ctl text-ink-3 opacity-0 focus:opacity-100 group-hover:opacity-100"
        onClick={() => setOpen(true)}
        aria-label={`${conv.title} 更多操作`}
      >
        <Icon name="more" size={14} />
      </button>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        anchor={ref}
        label="会话操作"
        items={[
          ...(conv.status === "ended"
            ? [{ label: "在新对话继续", icon: "plus" as const, onSelect: () => onContinue(conv.case_id ?? null) }]
            : [
                { label: "打开", icon: "doc" as const, onSelect: () => onOpen(conv.conversation_id) },
                { label: "重命名", icon: "pencil" as const, onSelect: () => onRename(conv.conversation_id) },
              ]),
          { label: "归档", icon: "docs" as const, separatorBefore:true, onSelect: () => setAskArchive(true) },
        ]}
      />
      <Dialog open={askArchive} onClose={() => { if (!archive.isPending) setAskArchive(false); }} title="归档这个对话？"
        description="从最近会话隐藏，原记录与未发送草稿保留。可以在设置的归档对话中恢复或删除；单据处理任务不受影响。"
        footer={<><button type="button" className="btn btn-secondary" disabled={archive.isPending} onClick={() => setAskArchive(false)}>取消</button>
          <button type="button" className="btn btn-primary" disabled={archive.isPending} onClick={async () => {
            try { await archive.mutateAsync(); setAskArchive(false); onArchived(); toast({tone:"ok",text:"对话已归档，可在设置中管理。"}); }
            catch (e) { toast({tone:"risk",text:errorText(e)}); }
          }}>{archive.isPending ? "正在归档" : "归档对话"}</button></>} />
    </>
  );
}

export function ConversationHeader({
  conv,
  run,
  reviewEnabled,
  reviewDisabledReason,
  reviewOpen,
  onToggleReview,
  onEndConversation,
  onContinueInNew,
  renameSignal,
}: {
  conv: ConversationView | null;
  run: RunView | null;
  reviewEnabled: boolean;
  reviewDisabledReason?: string;
  reviewOpen: boolean;
  onToggleReview: () => void;
  onEndConversation: () => void;
  onContinueInNew: () => void;
  renameSignal?: number;
}) {
  const navigate = useNavigate();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(conv?.title ?? "");
  const [ctxOpen, setCtxOpen] = useState(false);
  const [caseOpen, setCaseOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const ctxRef = useRef<HTMLButtonElement>(null);
  const caseRef = useRef<HTMLButtonElement>(null);
  const runRef = useRef<HTMLButtonElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const ctx = useContextView(conv?.conversation_id ?? null, ctxOpen);

  useEffect(() => {
    if (!renameSignal || !conv) return;
    setTitle(conv.title);
    setEditing(true);
  }, [renameSignal, conv]);

  const rename = useCommand<string, ConversationView>((t, key) => conversationApi.rename(conv!.conversation_id, t, conv!.conversation_version, key), ["c-conv", "c-list"]);

  return (
    <header className="flex h-bar shrink-0 items-center gap-2 border-b border-line bg-card px-4">
      {editing ? (
        <input
          className="input h-9 max-w-[280px]"
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              rename.mutate(title, { onSuccess: () => { setEditing(false); toast({ tone: "ok", text: "已重命名" }); } });
            } else if (e.key === "Escape") {
              setTitle(conv?.title ?? "");
              setEditing(false);
            }
          }}
          aria-label="会话标题"
        />
      ) : (
        <>
          <h1 className="min-w-0 max-w-[280px] truncate text-[16px] font-medium text-ink" title={conv?.title}>
            {conv?.title ?? "新对话"}
          </h1>
          {conv && !conv.archived_at && (
            <button
              type="button"
              className="btn btn-icon btn-ghost h-8 w-8"
              onClick={() => {
                setTitle(conv.title);
                setEditing(true);
              }}
              aria-label="重命名会话"
            >
              <Icon name="pencil" size={14} />
            </button>
          )}
        </>
      )}

      {/* 一个会话只能绑定一票 */}
      {conv?.case_id ? (
        <>
          <button ref={caseRef} type="button" className="tag tag-ref text-[12px]" onClick={() => setCaseOpen(true)}>
            {conv.case_name ?? "当前单票"}
            {conv.case_business_revision_no ? <span className="num ml-1 text-ink-3">业务版 v{conv.case_business_revision_no}</span> : null}
          </button>
          <Menu
            open={caseOpen}
            onClose={() => setCaseOpen(false)}
            anchor={caseRef}
            label="单票操作"
            items={[
              { label: "单票概览", icon: "docs", onSelect: () => navigate(`/documents/cases/${conv.case_id}`) },
              { label: "查看管理记录", icon: "history", onSelect: () => navigate("/documents") },
            ]}
          />
        </>
      ) : (
        <span className="tag border-line bg-page text-[12px] text-ink-3">未关联单票</span>
      )}

      {run && (
        <>
          <button ref={runRef} type="button" onClick={() => setRunOpen(true)} aria-label="查看任务状态">
            <RunStatusPill status={run.status} />
          </button>
          <Popover open={runOpen} onClose={() => setRunOpen(false)} anchor={runRef} label="任务状态" width={320}>
            <div className="p-3">
              <p className="num text-[11px] text-ink-3">Run {shortId(run.run_id)}</p>
              <p className="mt-1 text-[13px] text-ink">当前阶段：{run.current_stage}</p>
              {run.has_successful_snapshot && <p className="mt-1 text-[12px] text-ink-2">已有成功交付快照；当前工作版状态与它分开显示。</p>}
              <p className="mt-1 text-[11px] text-ink-3">更新于 {formatTime(run.updated_at)}</p>
              <button type="button" className="btn btn-secondary btn-sm mt-2" onClick={() => navigate("/documents")}>
                在管理台查看时间线
              </button>
            </div>
          </Popover>
        </>
      )}

      <span className="flex-1" />
      {conv?.archived_at && <button type="button" className="tag border-line bg-page text-[12px] text-ink-2" onClick={() => navigate("/settings/conversations")}>已归档 · 在设置中管理</button>}

      <button ref={ctxRef} type="button" className="btn btn-ghost btn-sm" onClick={() => setCtxOpen(true)}>
        查看本轮上下文
      </button>
      <Popover open={ctxOpen} onClose={() => setCtxOpen(false)} anchor={ctxRef} label="本轮上下文来源" width={380}>
        <ContextPanel ctx={ctx.data ?? null} loading={ctx.isPending} error={ctx.error ? errorText(ctx.error) : null} />
      </Popover>

      <button type="button" className="btn btn-secondary btn-sm" onClick={onToggleReview} disabled={!reviewEnabled} title={reviewEnabled ? undefined : reviewDisabledReason}>
        <Icon name="panel" size={14} />
        {reviewOpen ? "收起校验区" : "查看校验"}
      </button>

      <button ref={moreRef} type="button" className="btn btn-icon btn-ghost" onClick={() => setMoreOpen(true)} aria-label="会话更多操作">
        <Icon name="more" size={16} />
      </button>
      <Menu
        open={moreOpen}
        onClose={() => setMoreOpen(false)}
        anchor={moreRef}
        label="会话操作"
        items={[
          ...(conv && conv.status !== "ended" && !conv.archived_at
            ? [
                { label: "重命名", icon: "pencil" as const, onSelect: () => { setTitle(conv.title); setEditing(true); } },
                { label: "结束对话", icon: "close" as const, onSelect: onEndConversation },
              ]
            : []),
          ...(conv?.status === "ended" ? [{ label: "在新对话继续", icon: "plus" as const, onSelect: onContinueInNew }] : []),
          {
            label: "复制会话链接",
            icon: "copy",
            separatorBefore: true,
            onSelect: () => void navigator.clipboard.writeText(window.location.href).then(() => toast({ tone: "ok", text: "已复制这个会话的链接" })),
          },
        ]}
      />
    </header>
  );
}

/** 只解释来源：不展示系统提示词全文、密钥、原件全文或别人的记忆。 */
function ContextPanel({ ctx, loading, error }: { ctx: ContextView | null; loading: boolean; error: string | null }) {
  if (error) return <div className="p-3"><ErrorBar text={error} /></div>;
  if (loading || !ctx) return <div className="m-3 h-24 animate-pulse rounded-ctl bg-page" aria-hidden />;
  return (
    <div className="p-3">
      <p className="text-[13px] font-medium text-ink">本轮上下文来源</p>
      <dl className="mt-2 grid gap-x-3 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr]">
        <dt className="text-ink-3">系统策略</dt>
        <dd className="num text-ink">{ctx.policy_version}</dd>
        <dt className="text-ink-3">我的已确认偏好</dt>
        <dd className="text-ink">
          <span className="num">{ctx.confirmed_preference_count}</span> 条（记忆版本 v{ctx.memory_version}）
        </dd>
        <dt className="text-ink-3">当前单票</dt>
        <dd className="text-ink">{ctx.case_label ?? "未关联"}</dd>
        <dt className="text-ink-3">上传状态</dt>
        <dd className="text-ink">{ctx.upload_status}</dd>
        <dt className="text-ink-3">进入模型的轮次</dt>
        <dd className="text-ink">
          {ctx.turn_range}（共 <span className="num">{ctx.total_turns}</span> 轮）
        </dd>
        <dt className="text-ink-3">本会话旧轮摘要</dt>
        <dd className="text-ink">{ctx.summary_in_use ? "已使用（只属于本会话）" : "未使用"}</dd>
      </dl>
      {ctx.frozen_references.length > 0 && (
        <>
          <p className="mt-2 text-[11px] text-ink-3">本轮固定的引用</p>
          <ul className="mt-1 space-y-0.5">
            {ctx.frozen_references.map((r) => (
              <li key={r.id + r.kind} className="text-[12px] text-ink">
                {r.label} <span className="text-ink-3">· {r.selection_mode === "fixed" ? "固定历史版本" : "当前指针"}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="mt-2 text-[11px] leading-relaxed text-ink-3">{ctx.token_budget_note}</p>
      <p className="mt-1 text-[11px] text-ink-3">这里只解释来源，不显示系统提示词全文、密钥、原件全文或其他用户的记忆。</p>
    </div>
  );
}

export function ExampleButtons({ hasCase, onPick }: { hasCase: boolean; onPick: (text: string) => void }) {
  const examples = hasCase
    ? ["翻译这票发票和箱单", "解释提单中的 Shipper", "查看待确认问题"]
    : ["翻译这票发票和箱单", "解释提单中的 Shipper", "常见的提单与发票核对口径有哪些"];
  return (
    <div className="mt-4 flex flex-wrap gap-2">
      {examples.map((e) => (
        <button key={e} type="button" className="tag border-line bg-card text-[12px] text-ink-2 hover:bg-page" onClick={() => onPick(e)}>
          <UiText value={e} />
        </button>
      ))}
    </div>
  );
}

export { StatusPill };
