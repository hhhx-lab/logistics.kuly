import { useNavigationPreference } from "../../stores/navigation";
// 侧板／全屏宿主。用 ResizeObserver 读实测可用宽度决定形态；
// 拖拽只改布局，不重跑解析、不清编辑表单、不改锚点。
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { newIdempotencyKey } from "../../api/client";
import { preferencesApi } from "../../api/preferences";
import { Icon } from "../../components/Icon";
import { useToast } from "../../components/Overlay";
import { useSession } from "../../stores/session";
import { ReviewWorkspace, reviewTargetKey, type ReviewTargetSpec } from "./ReviewWorkspace";

const MIN = 520;
const MAX = 900;
const MAIN_MIN = 560;
const GAP = 48;

export function ReviewHost({ spec, onClose, onBack }: { spec: ReviewTargetSpec | null; onClose: () => void; onBack?: { label: string; onSelect: () => void } }) {
  const navigate = useNavigate();
  const toast = useToast();
  const [runOverride, setRunOverride] = useState<string | null>(null);
  const requestedKey = spec ? reviewTargetKey(spec) : null;
  useEffect(() => { setRunOverride(null); }, [requestedKey]);
  const { savedCollapsed, setTemporary } = useNavigationPreference();
  const savedWidth = useSession((s) => s.panelWidth);
  const setPanelWidth = useSession((s) => s.setPanelWidth);
  const [width, setWidth] = useState(() => (spec?.intent === "full" ? Math.min(MAX, Math.max(832, savedWidth)) : savedWidth));
  const [fullscreen, setFullscreen] = useState(false);
  const [manualFullscreen, setManualFullscreen] = useState(false);
  const [viewport, setViewport] = useState(() => window.innerWidth);
  const dragging = useRef(false);
  const panel = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const dragCleanup = useRef<(() => void) | null>(null);
  useLayoutEffect(() => {
    const active = document.activeElement;
    trigger.current = active instanceof HTMLElement ? active : null;
    return () => {
      dragCleanup.current?.();
      if (trigger.current && document.body.contains(trigger.current)) trigger.current.focus({ preventScroll: true });
    };
  }, []);
  useEffect(() => {
    if (!spec || !fullscreen) return;
    const root = panel.current;
    root?.querySelector<HTMLElement>('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab" || !root) return;
      const nodes = [...root.querySelectorAll<HTMLElement>('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter(el => !el.hasAttribute("disabled") && el.offsetParent !== null);
      if (!nodes.length) return;
      const index = nodes.indexOf(document.activeElement as HTMLElement);
      if (e.shiftKey && index <= 0) {e.preventDefault();nodes[nodes.length - 1].focus();}
      else if (!e.shiftKey && (index < 0 || index === nodes.length - 1)) {e.preventDefault();nodes[0].focus();}
    };
    root?.addEventListener("keydown", onKey);
    return () => root?.removeEventListener("keydown", onKey);
  }, [!!spec, fullscreen]);

  useEffect(() => {
    const observer = new ResizeObserver(() => setViewport(document.documentElement.clientWidth));
    observer.observe(document.documentElement);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    if (spec) setWidth(spec.intent === "full" ? Math.min(MAX, Math.max(832, savedWidth)) : savedWidth);
    setManualFullscreen(false);
  }, [spec?.caseId, spec?.intent]);
  useLayoutEffect(() => {
    if (!spec) return;
    const desired = width;
    const navigationWidth = savedCollapsed ? 64 : 232;
    const fits = viewport - navigationWidth - desired - GAP >= MAIN_MIN;
    const fitsCollapsed = viewport - 64 - desired - GAP >= MAIN_MIN;
    setTemporary(!fits && fitsCollapsed ? true : null);
    setFullscreen(manualFullscreen || !fitsCollapsed);
  }, [spec?.caseId, spec?.intent, width, viewport, savedCollapsed, setTemporary, manualFullscreen]);
  useLayoutEffect(() => {
    document.documentElement.style.setProperty("--review-reserved-width", spec && !fullscreen ? `${width + 24}px` : "0px");
    return () => { document.documentElement.style.removeProperty("--review-reserved-width"); };
  }, [spec, fullscreen, width]);
  useEffect(() => () => setTemporary(null), [setTemporary]);

  const savePref = useMutation({
    mutationFn: async (w: number) => {
      const cur = await preferencesApi.get();
      return preferencesApi.put({ interface_language: cur.interface_language, timezone: cur.timezone, panel_width: w, motion: cur.motion }, cur.version, newIdempotencyKey());
    },
    onError: () => toast({ tone: "info", text: "复核区宽度未能同步到设置，本次仍按你拖动的宽度显示" }),
  });

  /** 只有快速定位的宽度写回偏好；完整复核拖窄只影响本次。 */
  const commitWidth = useCallback(
    (w: number) => {
      const clamped = Math.max(MIN, Math.min(MAX, w));
      setWidth(clamped);
      if (spec?.intent === "quick") {
        setPanelWidth(clamped);
        savePref.mutate(clamped);
      }
    },
    [spec?.intent, setPanelWidth, savePref],
  );

  const onPointerDown = (e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest("button") || e.button !== 0) return;
    dragCleanup.current?.();
    dragging.current = true;
    const startX = e.clientX;
    const startW = width;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      if (!dragging.current) return;
      setWidth(Math.max(MIN, Math.min(MAX, startW + (startX - ev.clientX))));
    };
    const up = (ev: PointerEvent) => {
      dragging.current = false;
      dragCleanup.current?.();
      commitWidth(Math.max(MIN, Math.min(MAX, startW + (startX - ev.clientX))));
    };
    const cleanup = () => {
      dragging.current = false;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cleanup);
      dragCleanup.current = null;
    };
    dragCleanup.current = cleanup;
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cleanup);
  };

  if (!spec) return null;
  const workingSpec: ReviewTargetSpec = runOverride ? { caseId: spec.caseId, runId: runOverride, snapshotId: null, target: null, intent: "full" } : spec;


  return (
    <div ref={panel} role={fullscreen ? "dialog" : "region"} aria-modal={fullscreen || undefined} aria-label="单证复核工作台" className={`fixed z-panel flex min-h-0 overflow-hidden bg-card shadow-panel anim-slide-in ${fullscreen ? "inset-0" : "inset-y-0 right-0"}`} style={fullscreen ? undefined : { width }}>
      <div
        role="separator"
        aria-label="调整复核区宽度"
        aria-orientation="vertical"
        aria-valuenow={width}
        aria-valuemin={MIN}
        aria-valuemax={MAX}
        tabIndex={0}
        className={`group relative w-3 shrink-0 cursor-col-resize bg-page ${fullscreen ? "hidden" : ""}`}
        onPointerDown={onPointerDown}
        onDoubleClick={() => commitWidth(spec.intent === "full" ? 832 : 620)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") {
            e.preventDefault();
            commitWidth(width + 32);
          } else if (e.key === "ArrowRight") {
            e.preventDefault();
            commitWidth(width - 32);
          }
        }}
      >
        <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-line group-hover:bg-focus" aria-hidden="true" />
        <span className="absolute top-1/2 left-1/2 flex -translate-x-1/2 -translate-y-1/2 flex-col gap-1">
          <button type="button" className="btn btn-icon btn-secondary h-7 w-7 rounded-full" onClick={() => commitWidth(width + 32)} aria-label="放大复核区">
            <Icon name="chevron-left" size={12} />
          </button>
          <button type="button" className="btn btn-icon btn-secondary h-7 w-7 rounded-full" onClick={() => commitWidth(width - 32)} aria-label="缩小复核区">
            <Icon name="chevron-right" size={12} />
          </button>
        </span>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col border-l border-line">
        <ReviewWorkspace key={reviewTargetKey(workingSpec)} spec={workingSpec} width={fullscreen ? viewport - 48 : width - 12} fullscreen={fullscreen} onClose={onClose} onExpand={fullscreen ? undefined : () => setManualFullscreen(true)} onBack={onBack} onToggleFullscreen={viewport - 64 - width - GAP >= MAIN_MIN ? () => setManualFullscreen(value => !value) : undefined}
          onOpenRun={runId => setRunOverride(runId)}
          onUploadMore={(caseId, replace) => {
            const q = new URLSearchParams({case_id: caseId, upload: "1"});
            if (replace?.replaceDocumentId && replace.expectedVersion) { q.set("replace_document_id", replace.replaceDocumentId); q.set("expected_document_version", String(replace.expectedVersion)); }
            if (replace?.relatedIssueId && replace.expectedIssueVersion) { q.set("related_issue_id", replace.relatedIssueId); q.set("expected_issue_version", String(replace.expectedIssueVersion)); }
            onClose(); navigate(`/conversation?${q.toString()}`);
          }}
          onSwitchToWorking={() => { onClose(); navigate(`/review?${new URLSearchParams({case_id: spec.caseId, intent: "full"})}`); }}
        />
      </div>
    </div>
  );
}
