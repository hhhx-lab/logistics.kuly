// 扩展仓库数据 hook。启停、授权与 schema 变化后必须失效 C09 候选，
// 页面不把本地开关当成「可调用」。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
import { newIdempotencyKey } from "../../api/client";
import { extensionsApi } from "../../api/extensions";
import type { ExtensionRowState } from "../../api/types";
import { useSession } from "../../stores/session";

export type KindFilter = "all" | "skill" | "mcp";
export type StatusFilter = "all" | "available" | "inactive" | "abnormal";
export type SortKey = "grouped" | "updated";

/** 状态下拉到 row_state 的映射：可用＝已启用；未启用含草稿、验证中、待启用、已停用。 */
const STATUS_STATES: Record<Exclude<StatusFilter, "all">, ExtensionRowState[]> = {
  available: ["enabled", "syncing"],
  inactive: ["draft", "validating", "ready_to_enable", "disabled", "needs_retest"],
  abnormal: ["validation_failed", "connect_failed", "needs_recheck"],
};

export function useExtensionList(kind: KindFilter, status: StatusFilter, q: string) {
  const gen = useSession((s) => s.generation);
  const params = {
    // Skill 在契约里是 builtin_skill 与 user_skill 两个 kind，按多值交给服务端过滤
    kind: kind === "all" ? undefined : kind === "skill" ? "builtin_skill,user_skill" : "mcp",
    row_state: status === "all" ? undefined : STATUS_STATES[status].join(","),
    q: q.trim() || undefined,
  };
  const list = useQuery({ queryKey: ["ext-list", gen, params], queryFn: () => extensionsApi.list(params), retry: false });
  return { ...list, items: list.data?.items ?? [] };
}

export function useSkillActions() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["ext-actions", gen], queryFn: () => extensionsApi.actions(), retry: false });
}

export function useMcpTemplates() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["ext-templates", gen], queryFn: () => extensionsApi.templates(), retry: false });
}

export function useCalls(extensionId?: string) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["ext-calls", gen, extensionId ?? null], queryFn: () => extensionsApi.calls(extensionId), retry: false });
}

export function useSkill(skillId: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["ext-skill", gen, skillId], queryFn: () => extensionsApi.getSkill(skillId!), enabled: !!skillId, retry: false });
}

export function useMcp(connectionId: string | null) {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["ext-mcp", gen, connectionId], queryFn: () => extensionsApi.getMcp(connectionId!), enabled: !!connectionId, retry: false });
}

/**
 * 写命令：成功后同时失效列表与对话能力候选（C09）。
 * 幂等键按「一次意图」生成：成功后换新键，失败保留原键以便安全重试同一次提交。
 */
export function useExtensionCommand<TArgs, TResult>(fn: (args: TArgs, key: string) => Promise<TResult>) {
  const keyRef = useRef(newIdempotencyKey());
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: (args: TArgs) => fn(args, keyRef.current),
    onSuccess: async () => {
      keyRef.current = newIdempotencyKey();
      await qc.invalidateQueries({ queryKey: ["ext-list"] });
      await qc.invalidateQueries({ queryKey: ["ext-skill"] });
      await qc.invalidateQueries({ queryKey: ["ext-mcp"] });
      // 对话 / 候选来自服务端注册表，启停后必须重取
      await qc.invalidateQueries({ queryKey: ["capabilities"] });
      useSession.getState().refreshCapabilities();
    },
  });
  /** 表单内容变化后换键，避免第二次提交被幂等缓存当成重放而静默丢弃。 */
  const renewKey = useCallback(() => {
    keyRef.current = newIdempotencyKey();
  }, []);
  return { ...m, renewKey };
}

export function newKey() {
  return newIdempotencyKey();
}

export { extensionsApi };
