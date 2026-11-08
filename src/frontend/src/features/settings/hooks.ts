// 设置与我的记忆的数据 hook。三块各自独立保留未保存草稿；
// 切换身份后查询键的 generation 变化，旧用户的偏好、记忆与建议不会被沿用。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
import { newIdempotencyKey } from "../../api/client";
import { preferencesApi } from "../../api/preferences";
import { useSession } from "../../stores/session";

export function usePreferences() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["preferences", gen], queryFn: () => preferencesApi.get(), retry: false });
}

export function useTimezones() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["timezones", gen], queryFn: () => preferencesApi.timezones(), retry: false, staleTime: 10 * 60_000 });
}

export function useMemories() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["memories", gen], queryFn: () => preferencesApi.memories(), retry: false });
}

export function useSuggestions() {
  const gen = useSession((s) => s.generation);
  return useQuery({ queryKey: ["memory-suggestions", gen], queryFn: () => preferencesApi.suggestions(), retry: false });
}

/**
 * 写命令：幂等键按「一次意图」生成，成功后换新键；
 * 失败保留原键，网络结果未知时可用同键安全重试而不会重复写入。
 */
export function useSettingsCommand<TArgs, TResult>(fn: (args: TArgs, key: string) => Promise<TResult>, invalidate: string[]) {
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

export { preferencesApi };
