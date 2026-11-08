import type { ApiResponses } from "../../api/generated/request-contracts";
import type { EvidenceCardData } from "../../api/types";

type TurnEvent = ApiResponses["GET /api/v1/conversations/{conversation_id}/turns/{turn_id}/events"]["200"]["items"][number];
export type AnswerPreview = { id: string; turnId: string; revision: number; text: string; cards: EvidenceCardData[]; status: "generating" | "interrupted"; seq: number };
export type PreviewEvent = { type: string; data: Partial<TurnEvent> };

export function applyPreviewEvent(current: Record<string, AnswerPreview>, turnId: string, event: PreviewEvent): Record<string, AnswerPreview> {
  const data = event.data, id = data.preview_id ?? turnId;
  const old = current[id];
  if (old && data.seq != null && data.seq <= old.seq) return current;
  if (event.type === "evidence_card" && data.cards) {
    return { ...current, [id]: { id, turnId, revision: old?.revision ?? 1, text: old?.text ?? "", cards: data.cards as EvidenceCardData[], status: old?.status ?? "generating", seq: data.seq ?? 0 } };
  }
  if (event.type === "answer_preview_started") {
    return { ...current, [id]: { id, turnId, revision: data.revision ?? 1, text: "", cards: old?.cards ?? [], status: "generating", seq: data.seq ?? 0 } };
  }
  if (event.type === "message_final") {
    const next = { ...current }; delete next[id]; return next;
  }
  if (!old || (data.revision != null && data.revision !== old.revision)) return current;
  if (event.type === "answer_preview_reset") {
    const next = { ...current }; delete next[id]; return next;
  }
  if (event.type === "message_error") return { ...current, [id]: { ...old, status: "interrupted", seq: data.seq ?? old.seq } };
  if (event.type !== "answer_preview_delta") return current;
  if (data.offset !== [...old.text].length || typeof data.text !== "string") throw new Error("增量正文游标不一致");
  return { ...current, [id]: { ...old, text: old.text + data.text, seq: data.seq ?? old.seq } };
}
