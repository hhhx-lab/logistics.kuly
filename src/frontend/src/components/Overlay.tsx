// 浮层基础：层级顺序为 页面 → 固定栏 → 悬浮层 → 复核区 → 模态 → 提示；
// Esc 只关闭最上层，焦点回到触发元素；动画 160–220ms 且遵循减少动效。
import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";
import { englishError } from "../i18n/runtime";

// ---- Esc 栈：后进先出，只有栈顶响应 ----
const escStack: { id: string; close: () => void }[] = [];

function useEscLayer(active: boolean, close: () => void) {
  const id = useId();
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!active) return;
    const entry = { id, close: () => closeRef.current() };
    escStack.push(entry);
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (escStack[escStack.length - 1]?.id !== id) return;
      e.stopPropagation();
      entry.close();
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      const i = escStack.findIndex((x) => x.id === id);
      if (i >= 0) escStack.splice(i, 1);
    };
  }, [active, id]);
}

/** 关闭后把焦点还给触发元素。 */
export function useReturnFocus(active: boolean) {
  const trigger = useRef<Element | null>(null);
  useEffect(() => {
    if (active) {
      trigger.current = document.activeElement;
      return;
    }
    const el = trigger.current;
    if (el instanceof HTMLElement && document.body.contains(el)) el.focus({ preventScroll: true });
    trigger.current = null;
  }, [active]);
}

export function useFocusTrap(active: boolean, ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!active || !ref.current) return;
    const root = ref.current;
    const first = root.querySelector<HTMLElement>("[data-autofocus]") ?? root.querySelector<HTMLElement>('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])');
    first?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = [...root.querySelectorAll<HTMLElement>('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')].filter((el) => !el.hasAttribute("disabled") && el.offsetParent !== null);
      if (!items.length) return;
      const idx = items.indexOf(document.activeElement as HTMLElement);
      if (e.shiftKey && idx <= 0) {
        e.preventDefault();
        items[items.length - 1].focus();
      } else if (!e.shiftKey && idx === items.length - 1) {
        e.preventDefault();
        items[0].focus();
      }
    };
    root.addEventListener("keydown", onKey);
    return () => root.removeEventListener("keydown", onKey);
  }, [active, ref]);
}

// ---- 模态对话框 ----
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  width = 480,
  tone = "default",
  closeLabel = "关闭",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  width?: number;
  tone?: "default" | "risk";
  closeLabel?: string;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useEscLayer(open, onClose);
  useReturnFocus(open);
  useFocusTrap(open, panel);
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-modal flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-[rgba(17,20,24,.28)] anim-fade" onClick={onClose} aria-hidden="true" />
      <div ref={panel} role="dialog" aria-modal="true" aria-labelledby="dlg-title" className="relative w-full rounded-card bg-card shadow-panel anim-rise" style={{ maxWidth: width }}>
        <div className="flex items-start gap-3 border-b border-line px-5 py-4">
          {tone === "risk" && <span className="mt-0.5 text-risk"><Icon name="alert" size={18} /></span>}
          <div className="min-w-0 flex-1">
            <h2 id="dlg-title" className="text-[15px] font-medium text-ink">{title}</h2>
            {description && <div className="mt-1 text-[13px] leading-relaxed text-ink-2">{description}</div>}
          </div>
          <button type="button" className="btn-icon -mr-1 -mt-1" onClick={onClose} aria-label={closeLabel}>
            <Icon name="close" size={16} />
          </button>
        </div>
        {children && <div className="max-h-[60vh] overflow-y-auto scroll-thin px-5 py-4 text-[13px] text-ink">{children}</div>}
        {footer && <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

// ---- 悬浮层（菜单、候选列表） ----
type Align = "start" | "end" | "center";

export function Popover({
  open,
  onClose,
  anchor,
  children,
  width,
  align = "start",
  label,
  maxHeight = 360,
  role = "dialog",
}: {
  open: boolean;
  onClose: () => void;
  anchor: RefObject<HTMLElement | null>;
  children: ReactNode;
  width?: number;
  align?: Align;
  label: string;
  maxHeight?: number;
  role?: "dialog" | "listbox" | "menu";
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number; width: number; maxHeight: number; zIndex: number } | null>(null);
  useEscLayer(open, onClose);
  useReturnFocus(open);

  const place = useCallback(() => {
    const a = anchor.current;
    const p = panel.current;
    if (!a || !p) return;
    const r = a.getBoundingClientRect();
    let zIndex = 30;
    for (let node: HTMLElement | null = a; node; node = node.parentElement) {
      const layer = Number.parseInt(getComputedStyle(node).zIndex, 10);
      if (Number.isFinite(layer)) zIndex = Math.max(zIndex, layer + 1);
    }
    const w = Math.max(0, Math.min(width ?? r.width, window.innerWidth - 24));
    const h = Math.min(p.scrollHeight, maxHeight, window.innerHeight - 24);
    const below = Math.max(0, window.innerHeight - r.bottom - 20);
    const flipped = below < h && r.top > below;
    const available = flipped ? Math.max(0, r.top - 20) : below;
    const height = Math.min(maxHeight, available || window.innerHeight - 24);
    let left = align === "end" ? r.right - w : align === "center" ? r.left + r.width / 2 - w / 2 : r.left;
    left = Math.max(12, Math.min(left, window.innerWidth - w - 12));
    const top = flipped ? r.top - Math.min(h, height) - 8 : r.bottom + 8;
    setPos({ top: Math.max(12, Math.min(top, window.innerHeight - Math.min(h, height) - 12)), left, width: w, maxHeight: height, zIndex });
  }, [anchor, align, width, maxHeight]);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    place();
    const onScroll = (event: Event) => {
      if (event.target instanceof Node && panel.current?.contains(event.target)) return;
      place();
    };
    const observer = new ResizeObserver(() => place());
    if (panel.current?.firstElementChild) observer.observe(panel.current.firstElementChild);
    window.addEventListener("resize", onScroll);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("resize", onScroll);
      window.removeEventListener("scroll", onScroll, true);
      observer.disconnect();
    };
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (panel.current?.contains(t) || anchor.current?.contains(t)) return;
      onClose();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, onClose, anchor]);

  if (!open) return null;
  return createPortal(
    <div
      ref={panel}
      role={role}
      aria-label={label}
      className="popover fixed z-popover overflow-x-hidden overflow-y-auto overscroll-contain scroll-thin anim-rise"
      style={{ top: pos?.top ?? -9999, left: pos?.left ?? -9999, zIndex: pos?.zIndex, width: pos?.width ?? width ?? anchor.current?.offsetWidth, maxHeight: pos?.maxHeight ?? maxHeight, visibility: pos ? "visible" : "hidden" }}
    >
      {children}
    </div>,
    document.body,
  );
}

export interface MenuItemSpec {
  label: string;
  onSelect: () => void;
  icon?: Parameters<typeof Icon>[0]["name"];
  tone?: "default" | "risk";
  disabled?: boolean;
  disabledReason?: string;
  separatorBefore?: boolean;
}

/** 更多菜单：上下键移动，Enter 触发。 */
export function Menu({ open, onClose, anchor, items, label, width = 200, align = "end" }: { open: boolean; onClose: () => void; anchor: RefObject<HTMLElement | null>; items: MenuItemSpec[]; label: string; width?: number; align?: Align }) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const t = window.setTimeout(() => listRef.current?.querySelector<HTMLElement>("button:not([disabled])")?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open]);
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const btns = [...(listRef.current?.querySelectorAll<HTMLElement>("button:not([disabled])") ?? [])];
    const i = btns.indexOf(document.activeElement as HTMLElement);
    const next = e.key === "ArrowDown" ? (i + 1) % btns.length : (i - 1 + btns.length) % btns.length;
    btns[next]?.focus();
  };
  return (
    <Popover open={open} onClose={onClose} anchor={anchor} width={width} align={align} label={label} role="menu">
      <div ref={listRef} className="py-1" onKeyDown={onKeyDown}>
        {items.map((it, i) => (
          <div key={it.label}>
            {it.separatorBefore && i > 0 && <div className="my-1 h-px bg-line" />}
            <button
              type="button"
              role="menuitem"
              className={`menu-item ${it.tone === "risk" ? "menu-item-risk" : ""}`}
              disabled={it.disabled}
              title={it.disabled ? it.disabledReason : undefined}
              onClick={() => {
                onClose();
                it.onSelect();
              }}
            >
              {it.icon && <Icon name={it.icon} size={16} className="shrink-0 opacity-70" />}
              <span className="truncate">{it.label}</span>
            </button>
            {it.disabled && it.disabledReason && <p className="px-3 pb-1 text-[11px] text-ink-3">{it.disabledReason}</p>}
          </div>
        ))}
      </div>
    </Popover>
  );
}

// ---- 轻提示 ----
export interface Toast {
  id: number;
  text: string;
  tone: "ok" | "risk" | "info";
  action?: { label: string; onSelect: () => void };
}

const ToastCtx = createContext<(t: Omit<Toast, "id">) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev.slice(-2), { ...t, id }]);
    window.setTimeout(() => setItems((prev) => prev.filter((x) => x.id !== id)), 5000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-6 left-1/2 z-toast flex -translate-x-1/2 flex-col items-center gap-2" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className="pointer-events-auto flex max-w-[520px] items-start gap-2 rounded-control bg-ink px-3.5 py-2.5 text-[13px] text-white shadow-panel anim-rise">
            <Icon name={t.tone === "ok" ? "check" : t.tone === "risk" ? "alert" : "info"} size={16} className={t.tone === "risk" ? "text-[#ff8f86]" : t.tone === "ok" ? "text-[#5fd39a]" : "opacity-80"} />
            <span lang={t.tone === "risk" ? "en" : undefined} className="flex-1">{t.tone === "risk" ? englishError(t.text) : t.text}</span>
            {t.action && (
              <button type="button" className="shrink-0 font-medium text-brand-soft underline decoration-[rgba(255,241,234,.45)] underline-offset-2" onClick={() => { t.action!.onSelect(); setItems((prev) => prev.filter((x) => x.id !== t.id)); }}>
                {t.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export { useEscLayer };
