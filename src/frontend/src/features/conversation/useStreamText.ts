import { useLayoutEffect, useRef, useState } from "react";
import type { MessageView } from "../../api/types";
import type { AnswerPreview } from "./preview";

/** 只对在途回复渐进显示；历史正文不重播，定稿沿用同一显示游标。 */
export function useStreamText(messages: MessageView[], previews: AnswerPreview[], onReveal?: () => void) {
  const entries = useRef(new Map<string, { text: string; shown: number }>());
  const callback = useRef(onReveal); callback.current = onReveal;
  const [display, setDisplay] = useState<Record<string, string>>({});
  useLayoutEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const finals = new Map(messages.filter(m => m.role === "assistant").map(m => [m.turn_id, m]));
    const active = new Map(previews.map(p => [p.turnId, p]));
    for (const [id, entry] of entries.current) {
      const final = finals.get(id), preview = active.get(id);
      if (final?.restricted || (final && final.status !== "completed") || (!final && !preview)) { entries.current.delete(id); continue; }
      const text = final?.text ?? preview!.text;
      if (!text.startsWith(Array.from(entry.text).slice(0, entry.shown).join(""))) { entries.current.delete(id); continue; }
      entry.text = text;
      if (reduced || preview?.status === "interrupted") entry.shown = Array.from(text).length;
    }
    for (const preview of previews) {
      if (entries.current.has(preview.turnId) || finals.has(preview.turnId) || preview.status !== "generating") continue;
      entries.current.set(preview.turnId, { text: preview.text, shown: reduced ? Array.from(preview.text).length : 0 });
    }
    const publish = () => setDisplay(Object.fromEntries([...entries.current].map(([id, e]) => [id, Array.from(e.text).slice(0, e.shown).join("")])));
    publish();
    let frame = 0, previous = performance.now(), credit = 0;
    const tick = (now: number) => {
      const elapsed = Math.min(now - previous, 50); previous = now;
      let pending = false, changed = false;
      for (const entry of entries.current.values()) {
        const length = Array.from(entry.text).length, remaining = length - entry.shown;
        if (remaining <= 0) continue;
        credit += elapsed * Math.min(180, 60 + remaining / 8) / 1000;
        const count = Math.min(remaining, Math.floor(credit));
        credit -= count; entry.shown += count;
        changed ||= count > 0; pending ||= entry.shown < length;
      }
      if (changed) { publish(); callback.current?.(); }
      if (pending) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [messages, previews]);
  return display;
}
