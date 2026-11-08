import type { RunView } from "../../api/types";

export type ProgressEvent = {
  seq: number;
  at: string;
  text: string;
  stage?: string | null;
  type: string;
};

const PHASES = [
  ["关联", "关联单证"], ["同票核对", "同票核对"], ["解析", "解析"], ["抽取", "抽取"], ["翻译", "翻译"],
  ["语义QA", "译文校验"], ["视觉复读", "识别复核"], ["疑点局部修订", "疑点修订"],
] as const;

export function phaseLabel(stage: string) {
  return PHASES.find(([prefix]) => stage.startsWith(prefix))?.[1] ?? stage;
}

/** 批次事件保留在后台，聊天只显示一行业务阶段。 */
export function summarizeProgress(events: ProgressEvent[], run: RunView) {
  const rows = new Map<string, { at: string; text: string; phase?: string; status?: string }>();
  const phaseEvents = events.filter((event) => event.type === "stage");
  const lastPhase = phaseEvents.at(-1);
  const stopped = !["queued", "processing", "exporting"].includes(run.status);
  const finished = ["checked", "indexed", "awaiting_review", "ready_to_export", "exporting", "exported"].includes(run.status);
  for (const event of events) {
    if (event.type !== "stage") {
      // 人工请求、失败、重试仍可见；相同的排队续查通知只更新一次。
      const key = `${event.type}:${event.text}`;
      if (!rows.has(key)) rows.set(key, { at: event.at, text: event.text });
      continue;
    }
    const label = phaseLabel(event.stage || event.text.replace(/^正在/, "").replace(/完成$/, ""));
    const lastLabel = lastPhase && phaseLabel(lastPhase.stage || "");
    const complete = label !== lastLabel || finished;
    const status = complete ? "完成" : run.status.startsWith("failed") ? "失败" : run.status === "cancelled" ? "已停止" : stopped ? "已暂停" : "中";
    const key = `phase:${label}`;
    rows.set(key, { at: rows.get(key)?.at ?? event.at, text: `${label}${status}`, phase: label, status });
  }
  return [...rows.values()];
}
