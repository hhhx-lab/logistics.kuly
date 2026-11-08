import { englishError, getInterfaceLocale, translateUi } from "../i18n/runtime";
// 状态展示：一律「图标 + 文字」，不靠颜色单独表意。
import type { ReactNode } from "react";
import { Icon, Spinner, type IconName } from "./Icon";

export type Tone = "brand" | "risk" | "warn" | "ok" | "hist" | "dis" | "info";

const toneClass: Record<Tone, string> = {
  // 进行中、识别索引等信息类状态用蓝色；品牌橙不表达业务状态
  brand: "bg-sel-soft text-sel",
  risk: "bg-risk-bg text-risk",
  warn: "bg-warn-bg text-warn",
  ok: "bg-ok-bg text-ok",
  hist: "bg-hist-bg text-hist",
  dis: "bg-dis-bg text-dis",
  info: "bg-page text-ink-2",
};

const toneIcon: Record<Tone, IconName> = {
  brand: "info",
  risk: "alert",
  warn: "warn",
  ok: "check",
  hist: "history",
  dis: "info",
  info: "info",
};

export function StatusPill({ tone, children, icon, busy = false, title }: { tone: Tone; children: ReactNode; icon?: IconName; busy?: boolean; title?: string }) {
  return (
    <span className={`pill ${toneClass[tone]}`} title={title}>
      {busy ? <Spinner size={12} /> : <Icon name={icon ?? toneIcon[tone]} size={12} strokeWidth={1.8} />}
      {children}
    </span>
  );
}

/** Run 状态 → 色调与图标。未知状态统一降级为「状态待支持」。 */
export const RUN_TONE: Record<string, { tone: Tone; icon: IconName; text: string; busy?: boolean }> = {
  queued: { tone: "info", icon: "clock", text: "等待处理" },
  processing: { tone: "brand", icon: "spinner", text: "处理中", busy: true },
  awaiting_input: { tone: "warn", icon: "question", text: "待补确认" },
  checked: { tone: "ok", icon: "check", text: "核对完成" },
  indexed: { tone: "ok", icon: "check", text: "识别完成" },
  awaiting_review: { tone: "warn", icon: "eye", text: "待复核" },
  ready_to_export: { tone: "brand", icon: "check", text: "可导出" },
  exporting: { tone: "brand", icon: "spinner", text: "导出中", busy: true },
  exported: { tone: "ok", icon: "check", text: "已导出" },
  paused_reconcile: { tone: "warn", icon: "shield", text: "外部请求待核查" },
  failed_retryable: { tone: "risk", icon: "alert", text: "处理失败" },
  failed_final: { tone: "risk", icon: "alert", text: "处理失败" },
  cancelled: { tone: "dis", icon: "close", text: "已取消" },
};

export function runTone(status: string) {
  return RUN_TONE[status] ?? { tone: "dis" as Tone, icon: "question" as IconName, text: "状态待支持" };
}

export function RunStatusPill({ status }: { status: string }) {
  const t = runTone(status);
  return (
    <StatusPill tone={t.tone} icon={t.icon} busy={t.busy}>
      {t.text}
    </StatusPill>
  );
}

/** 风险计数：红／黄／无法判断，零值不显示。 */
export function IssueCounts({ blocking, warning, undetermined, size = "sm", rulesStatus }: { blocking: number; warning: number; undetermined: number; size?: "sm" | "md"; rulesStatus?: "run" | "not_run" | null }) {
  const cls = size === "md" ? "text-[13px]" : "text-[12px]";
  if (rulesStatus === "not_run" && !blocking && !warning && !undetermined) {
    return <span className={`inline-flex items-center gap-1 ${cls} text-ink-2`}><Icon name="clock" size={14} />疑点检查尚未完成</span>;
  }
  if (!blocking && !warning && !undetermined) {
    // 零疑点只是「本版没有待处理项」，不代表已核验通过，因此不用成功绿
    return (
      <span className={`inline-flex items-center gap-1 ${cls} text-ink-2`}>
        <Icon name="info" size={14} strokeWidth={1.8} />
        无待处理项
      </span>
    );
  }
  return (
    <span className={`inline-flex items-center gap-2.5 ${cls}`}>
      {blocking > 0 && (
        <span className="inline-flex items-center gap-1 text-risk">
          <Icon name="alert" size={14} strokeWidth={1.8} />
          红项 <span className="num">{blocking}</span>
        </span>
      )}
      {warning > 0 && (
        <span className="inline-flex items-center gap-1 text-warn">
          <Icon name="warn" size={14} strokeWidth={1.8} />
          黄项 <span className="num">{warning}</span>
        </span>
      )}
      {undetermined > 0 && (
        <span className="inline-flex items-center gap-1 text-ink-2">
          <Icon name="question" size={14} strokeWidth={1.8} />
          无法判断 <span className="num">{undetermined}</span>
        </span>
      )}
    </span>
  );
}

export function EmptyState({ icon = "docs", title, description, action }: { icon?: IconName; title: string; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <span className="mb-3 text-ink-3 opacity-60">
        <Icon name={icon} size={32} strokeWidth={1.3} />
      </span>
      <p className="text-[14px] font-medium text-ink">{title}</p>
      {description && <p className="mt-1.5 max-w-[420px] text-[13px] leading-relaxed text-ink-2">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />;
}

/** 行内加载：文字与动效成对，供列表与流式区域复用。 */
export function Loading({ text = "正在加载", className = "" }: { text?: string; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 text-[13px] text-ink-2 ${className}`} role="status">
      <Spinner size={14} />
      {text}
    </span>
  );
}

/** 错误条：说明发生了什么与下一步动作。 */
export function ErrorBar({ text, action, tone = "risk" }: { text: string; action?: { label: string; onSelect: () => void }; tone?: "risk" | "warn" }) {
  const cls = tone === "risk" ? "border-risk-line bg-risk-bg text-risk" : "border-warn-line bg-warn-bg text-warn";
  return (
    <div className={`flex items-start gap-2 rounded-ctl border px-3 py-2 text-[13px] ${cls}`} role="alert">
      <Icon name={tone === "risk" ? "alert" : "warn"} size={16} strokeWidth={1.8} className="mt-px shrink-0" />
      <span lang={tone === "risk" ? "en" : undefined} className="flex-1 leading-relaxed">{tone === "risk" ? englishError(text) : text}</span>
      {action && (
        <button type="button" className="shrink-0 font-medium underline underline-offset-2" onClick={action.onSelect}>
          {action.label}
        </button>
      )}
    </div>
  );
}

export function formatBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 展示时区：由设置页 P01 保存的 IANA 时区驱动，未取到时用浏览器时区。
 * 只影响「显示」，不参与查询边界换算——日期筛选一律把本地日交给服务端解释。
 */
let displayTimezone: string | undefined;

export function setDisplayTimezone(tz: string | undefined) {
  displayTimezone = tz;
}

const tzOpt = () => (displayTimezone ? { timeZone: displayTimezone } : {});

/** 按展示时区取年月日，用于判断今天／昨天，避免跨时区错判。 */
function dayKey(d: Date) {
  return d.toLocaleDateString("en-CA", tzOpt());
}

export function formatTime(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const mins = Math.round((now.getTime() - d.getTime()) / 60000);
  if (mins < 1) return translateUi("刚刚");
  if (mins < 60) return new Intl.RelativeTimeFormat(getInterfaceLocale(),{numeric:"auto"}).format(-mins,"minute");
  const hm = d.toLocaleTimeString(getInterfaceLocale(), { hour: "2-digit", minute: "2-digit", hour12: false, ...tzOpt() });
  const today = dayKey(now);
  if (dayKey(d) === today) return `${translateUi("今天")} ${hm}`;
  if (dayKey(d) === dayKey(new Date(now.getTime() - 86400000))) return `${translateUi("昨天")} ${hm}`;
  const [y, m, day] = dayKey(d).split("-");
  if (y === today.slice(0, 4)) return `${d.toLocaleDateString(getInterfaceLocale(), {month:"short",day:"numeric",...tzOpt()})} ${hm}`;
  return d.toLocaleDateString(getInterfaceLocale(), tzOpt());
}

/** 展示用短号：去掉类型前缀，避免出现「-25xe7」这种前导连字符。 */
export function shortId(id: string, len = 6) {
  return id.replace(/^[a-z]+-/, "").replace(/-/g, "").slice(-len);
}

export function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString(getInterfaceLocale(), { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false, ...tzOpt() });
}
