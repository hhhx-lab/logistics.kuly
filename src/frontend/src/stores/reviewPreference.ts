import { useCallback, useEffect, useState, type SetStateAction } from "react";
import { useSession } from "./session";

/** 只保存界面选择，不保存单据正文；身份和版本共同限定恢复范围。 */
export function useReviewPreference<T>(scope: string, initial: T) {
  const identity = useSession(state => state.identity);
  const key = `review-ui:${identity?.tenant_id}:${identity?.user_id}:${scope}`;
  const read = () => {
    try { const stored = sessionStorage.getItem(key); return stored ? JSON.parse(stored) as T : initial; } catch { return initial; }
  };
  const [entry, setEntry] = useState(() => ({ key, value: read() }));
  const value = entry.key === key ? entry.value : read();
  if (entry.key !== key) setEntry({ key, value });
  const setValue = useCallback((next: SetStateAction<T>) => setEntry(previous => {
    const current = previous.key === key ? previous.value : initial;
    return { key, value: typeof next === "function" ? (next as (value: T) => T)(current) : next };
  }), [key]);
  useEffect(() => {
    if (!identity || entry.key !== key) return;
    try { sessionStorage.setItem(key, JSON.stringify(entry.value)); } catch { /* 存储不可用不阻断复核 */ }
  }, [key, entry, identity]);
  return [value, setValue] as const;
}
