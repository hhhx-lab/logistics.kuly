// 对话工作台数据 hook。会话、消息、回合事件、任务与上下文都只从服务端读，
// 本地仅保留未发送草稿、浮层开合与滚动锚点。
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
import { newIdempotencyKey } from "../../api/client";
import { conversationApi, type TurnBody } from "../../api/conversation";
import { preferencesApi } from "../../api/preferences";
import { uploadApi } from "../../api/upload";
import { workflowApi } from "../../api/workflow";
import { authApi } from "../../api/auth";
import { useSession } from "../../stores/session";

export function useConversations(q: string, archived = false, enabled = true) {
  const gen = useSession((s) => s.generation);
  return useInfiniteQuery({ queryKey: ["c-list", gen, q.trim(), archived], enabled,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => conversationApi.list(q.trim() || undefined, pageParam, archived),
    getNextPageParam: (page) => page.has_more ? page.next_cursor ?? undefined : undefined,
    select: (data) => ({ ...data, items: Array.from(new Map(data.pages.flatMap((page) => page.items).map((item) => [item.conversation_id, item])).values()) }),
    retry: false });
}

export function useConversation(id: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["c-conv", gen, id], queryFn: () => conversationApi.get(id!), enabled: !!id, retry: false });
}

export function useTurn(conversationId: string | null, turnId: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["c-turn", gen, conversationId, turnId],
    queryFn: () => conversationApi.turn(conversationId!, turnId!), enabled: !!conversationId && !!turnId, retry: false,
    refetchInterval: (query) => ["pending", "running"].includes(query.state.data?.status ?? "") ? 2000 : false });
}

export function useMessages(id: string | null) {
  const gen = useSession((s) => s.generation);
  return useInfiniteQuery({ queryKey: ["c-msgs", gen, id], initialPageParam: 0,
    queryFn: ({ pageParam }) => conversationApi.messages(id!, undefined, pageParam),
    getNextPageParam: (page) => page.has_more && page.next_cursor ? Number(page.next_cursor) : undefined,
    select: (data) => ({ ...data, items: Array.from(new Map([...data.pages].reverse().flatMap((page) => page.items).map((item) => [item.message_id, item])).values())
      .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)) }),
    enabled: !!id, retry: false });
}

export function useContextView(id: string | null, enabled: boolean) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["c-ctx", gen, id], queryFn: () => conversationApi.context(id!), enabled: !!id && enabled, retry: false });
}

export function useWorkflows() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["w-list", gen], queryFn: () => workflowApi.workflows(), retry: false, staleTime: 5 * 60_000 });
}

export function useRun(runId: string | null | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["w-run", gen, runId ?? null], queryFn: () => workflowApi.run(runId!), enabled: !!runId, retry: false,
    refetchInterval: query => ["queued", "processing", "exporting"].includes(query.state.data?.status ?? "") ? 2000 : false });
}

export function useTaskDraft(draftId: string | null | undefined) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["w-draft", gen, draftId ?? null], queryFn: () => workflowApi.getDraft(draftId!), enabled: !!draftId, retry: false });
}

export function useInputRequests(runId: string | null | undefined, enabled: boolean) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["w-inputs", gen, runId ?? null], queryFn: () => workflowApi.inputRequests(runId!), enabled: !!runId && enabled, retry: false });
}

export function useDisclosure() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["s-disclosure", gen], queryFn: () => authApi.disclosure(), retry: false });
}

export function useUploadSession(sessionId: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["u-session", gen, sessionId], queryFn: () => uploadApi.session(sessionId!), enabled: !!sessionId, retry: false });
}

export function useMemorySuggestions() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["memory-suggestions", gen], queryFn: () => preferencesApi.suggestions(), retry: false });
}

/** 写命令：幂等键按一次意图生成；成功后换新键，失败保留原键以便安全重试。 */
export function useCommand<TArgs, TResult>(fn: (args: TArgs, key: string) => Promise<TResult>, invalidate: string[] = []) {
  const keyRef = useRef(newIdempotencyKey());
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: (args: TArgs) => fn(args, keyRef.current),
    onSuccess: async () => {
      keyRef.current = newIdempotencyKey();
      await Promise.all(invalidate.map((k) => qc.invalidateQueries({ queryKey: [k] })));
    },
  });
  const renewKey = useCallback(() => {
    keyRef.current = newIdempotencyKey();
  }, []);
  return { ...m, renewKey };
}

export function newKey() {
  return newIdempotencyKey();
}

export type { TurnBody };
export { conversationApi, uploadApi, workflowApi };
