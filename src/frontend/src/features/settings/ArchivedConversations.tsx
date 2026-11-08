import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { errorText } from "../../api/client";
import type { ConversationView } from "../../api/types";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, formatDateTime } from "../../components/Status";
import { useSession } from "../../stores/session";
import { conversationApi, useCommand, useConversations } from "../conversation/hooks";

export function ArchivedConversations({ enabled }: { enabled: boolean }) {
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<ConversationView | null>(null);
  const list = useConversations(search, true, enabled);
  const navigate = useNavigate();
  const toast = useToast();
  const userId = useSession(s => s.identity?.user_id);
  const restore = useCommand((row: ConversationView, key: string) => conversationApi.restore(row.conversation_id, row.conversation_version, key), ["c-list", "c-conv"]);
  const remove = useCommand((row: ConversationView, key: string) => conversationApi.remove(row.conversation_id, row.conversation_version, key), ["c-list", "c-conv", "c-msgs"]);
  const busy = restore.isPending || remove.isPending;
  return <section aria-labelledby="archived-heading" className="space-y-4">
    <div><h2 id="archived-heading" className="text-[18px] font-semibold text-ink">归档对话</h2>
      <p className="mt-1 text-[13px] text-ink-2">归档仅隐藏最近会话。恢复保留原状态；删除对话不删除单据和已确认的用户记忆。</p></div>
    <input aria-label="搜索归档对话" className="input max-w-[420px]" placeholder="按对话标题搜索" value={search} onChange={e => setSearch(e.target.value)} />
    {list.isPending && <p role="status" className="text-[13px] text-ink-2">正在读取归档对话</p>}
    {list.isError && <ErrorBar text={errorText(list.error)} action={{label:"重试读取",onSelect:() => void list.refetch()}} />}
    {!list.isPending && !list.isError && !list.data?.items.length && <p className="rounded-card border border-line bg-card p-5 text-[13px] text-ink-3">{search ? "没有匹配的归档对话。" : "暂无归档对话，可从最近会话的更多菜单归档。"}</p>}
    <ul className="space-y-2">{list.data?.items.map(row => <li key={row.conversation_id} className="flex flex-wrap items-center gap-3 rounded-card border border-line bg-card p-4">
      <div className="min-w-0 flex-1"><p className="break-words text-[14px] font-medium text-ink">{row.title}</p>
        <p className="mt-1 text-[12px] text-ink-3">归档于 {row.archived_at ? formatDateTime(row.archived_at) : "—"} · {row.status === "ended" ? "服务已结束" : "服务未结束"}</p></div>
      <div className="flex shrink-0 gap-2"><button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate(`/conversation/${row.conversation_id}`)}>查看</button>
        <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={async () => {
          try { await restore.mutateAsync(row); toast({tone:"ok",text:"对话已恢复到最近会话。"}); }
          catch (e) { toast({tone:"risk",text:errorText(e)}); void list.refetch(); }
        }}>恢复</button><button type="button" className="btn btn-ghost btn-sm text-risk" disabled={busy} onClick={() => { remove.renewKey(); setDeleting(row); }}>永久删除</button></div>
    </li>)}</ul>
    {list.hasNextPage && <button type="button" className="btn btn-secondary" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>加载更多归档对话</button>}
    <Dialog open={!!deleting} onClose={() => {if (!remove.isPending) setDeleting(null);}} title="永久删除这个对话？"
      description={`将删除“${deleting?.title ?? ""}”的聊天、摘要与引用，无法恢复。单据、处理任务、已导出译本和已确认用户记忆保留。`}
      footer={<><button type="button" className="btn btn-secondary" disabled={remove.isPending} onClick={() => setDeleting(null)}>取消</button>
        <button type="button" className="btn btn-primary" disabled={remove.isPending} onClick={async () => {
          if (!deleting) return;
          try { await remove.mutateAsync(deleting); try { sessionStorage.removeItem(`conversation-draft:${userId}:${deleting.conversation_id}`); } catch { /* 不影响服务端删除 */ }
            setDeleting(null); toast({tone:"ok",text:"对话已删除，单据仍保留在管理台。"}); }
          catch (e) { toast({tone:"risk",text:errorText(e)}); void list.refetch(); }
        }}>{remove.isPending ? "正在删除" : "确认永久删除"}</button></>} />
  </section>;
}
