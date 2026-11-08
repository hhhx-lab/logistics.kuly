// 身份与跨页联动状态。身份切换时递增 generation，用于失效全部查询与本地态。
// 不把任何单据内容或凭证写入 localStorage。
import { create } from "zustand";
import type { EvidenceTarget, IdentityView } from "../api/types";
import { abortAllRequests, setCsrfToken } from "../api/client";
import { closeAllStreams } from "../api/events";

export interface EvidenceRequest {
  target: EvidenceTarget;
  intent: "view_source" | "view_translation" | "view_issue" | "view_field" | "review";
  returnFocus?: string;
  at: number;
}

interface SessionState {
  identity: IdentityView | null;
  generation: number;
  capabilityRevision: number;
  status: "loading" | "authenticated" | "anonymous";
  /** 复核区请求：由对话、管理台等处调用，复核区消费后清空。 */
  evidenceRequest: EvidenceRequest | null;
  panelWidth: number;
  setIdentity: (id: IdentityView) => void;
  setAnonymous: () => void;
  resetForIdentityChange: () => void;
  openEvidence: (target: EvidenceTarget, intent: EvidenceRequest["intent"], returnFocus?: string) => void;
  clearEvidence: () => void;
  setPanelWidth: (w: number) => void;
  refreshCapabilities: () => void;
}

export const useSession = create<SessionState>((set, get) => ({
  identity: null,
  generation: 0,
  capabilityRevision: 0,
  status: "loading",
  evidenceRequest: null,
  panelWidth: 620,

  setIdentity: (identity) => {
    const prev = get().identity;
    setCsrfToken(identity.csrf_token);
    if (prev && prev.user_id !== identity.user_id) {
      abortAllRequests();
      closeAllStreams();
      set({ identity, status: "authenticated", generation: get().generation + 1, evidenceRequest: null });
      return;
    }
    set({ identity, status: "authenticated" });
  },

  setAnonymous: () => {
    if (get().status === "anonymous") return;
    setCsrfToken(null);
    abortAllRequests();
    closeAllStreams();
    set({ identity: null, status: "anonymous", evidenceRequest: null, generation: get().generation + 1 });
  },

  resetForIdentityChange: () => {
    abortAllRequests();
    closeAllStreams();
    set({ generation: get().generation + 1, evidenceRequest: null });
  },

  openEvidence: (target, intent, returnFocus) => set({ evidenceRequest: { target, intent, returnFocus, at: Date.now() } }),
  clearEvidence: () => set({ evidenceRequest: null }),
  setPanelWidth: (panelWidth) => set({ panelWidth: Math.max(520, Math.min(900, panelWidth)) }),
  refreshCapabilities: () => set({ capabilityRevision: get().capabilityRevision + 1 }),
}));

/** 查询键统一带身份代际，身份切换后旧缓存自然不再命中。 */
export function useQueryScope() {
  return useSession((s) => s.generation);
}
