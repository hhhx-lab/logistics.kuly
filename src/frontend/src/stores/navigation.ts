import { create } from "zustand";
import { useCallback } from "react";
import { useSession } from "./session";

interface NavigationState {
  saved: Record<string, boolean>;
  temporary: Record<string, boolean | null>;
  setSaved: (key: string, value: boolean) => void;
  setTemporary: (key: string, value: boolean | null) => void;
}

const useNavigation = create<NavigationState>(set => ({
  saved: {}, temporary: {},
  setSaved: (key, value) => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 无持久存储时保留本次选择 */ }
    set(state => ({ saved: { ...state.saved, [key]: value } }));
  },
  setTemporary: (key, value) => set(state => ({ temporary: { ...state.temporary, [key]: value } })),
}));

export function useNavigationPreference() {
  const identity = useSession(state => state.identity);
  const key = `navigation:${identity?.tenant_id ?? "anonymous"}:${identity?.user_id ?? "anonymous"}`;
  const stored = useNavigation(state => state.saved[key]);
  const temporary = useNavigation(state => state.temporary[key]);
  const setSaved = useNavigation(state => state.setSaved);
  const setTemporary = useNavigation(state => state.setTemporary);
  let saved = stored;
  if (saved === undefined) {
    try { saved = localStorage.getItem(key) === "true"; } catch { saved = false; }
  }
  return { collapsed: temporary ?? saved, savedCollapsed: saved,
    toggle: () => setSaved(key, !saved), setTemporary: useCallback((value: boolean | null) => setTemporary(key, value), [key, setTemporary]) };
}
