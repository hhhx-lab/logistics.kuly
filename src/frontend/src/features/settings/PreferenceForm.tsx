import { LANGUAGES, languageNativeName, languageName } from "../../i18n/languages";
import { useInterfaceText } from "./useInterfaceText";
// 界面偏好：语言、时区、快速定位侧板宽度与动效。
// 拖动只预览，必须点「保存界面设置」才写入账户；保存不改历史 Run、译本或已导出文件。
import { useEffect, useMemo, useRef, useState } from "react";
import { errorText } from "../../api/client";
import type { PreferencesView } from "../../api/types";
import { useToast } from "../../components/Overlay";
import { ErrorBar, formatDateTime } from "../../components/Status";
import { preferencesApi, usePreferences, useSettingsCommand, useTimezones } from "./hooks";

type Draft = Omit<PreferencesView, "version" | "saved_at">;

const MOTION = [
  { id: "system", label: "跟随系统" },
  { id: "reduce", label: "始终减少" },
  { id: "standard", label: "使用标准过渡" },
] as const;

const MIN_W = 520;
const MAX_W = 900;
const DEFAULT_W = 620;

export function PreferenceForm({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) {
  const uiText = useInterfaceText();
  const toast = useToast();
  const pref = usePreferences();
  const tz = useTimezones();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [conflict, setConflict] = useState<PreferencesView | null>(null);
  const [tzQuery, setTzQuery] = useState("");
  const savedTimezone = useRef<string | null>(null);

  // 时区口径变化会影响管理台按本地日的查询，保存后要连同这些查询一起失效
  const save = useSettingsCommand<Draft, PreferencesView>((body, key) => preferencesApi.put(body, pref.data!.version, key), ["preferences", "m-runs", "m-facets", "m-timeline", "m-summaries"]);

  // 服务端值到达前不显示任何值，避免沿用前一个身份的本地偏好
  useEffect(() => {
    if (!pref.data) return;
    savedTimezone.current = savedTimezone.current ?? pref.data.timezone;
    setDraft((d) => d ?? { interface_language: pref.data!.interface_language, timezone: pref.data!.timezone, panel_width: pref.data!.panel_width, motion: pref.data!.motion });
  }, [pref.data]);

  const dirty = useMemo(() => {
    if (!draft || !pref.data) return false;
    return draft.interface_language !== pref.data.interface_language || draft.timezone !== pref.data.timezone || draft.panel_width !== pref.data.panel_width || draft.motion !== pref.data.motion;
  }, [draft, pref.data]);

  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => {
    setDraft((p) => (p ? { ...p, [k]: v } : p));
    setConflict(null);
    save.renewKey();
  };

  if (pref.isError) return <ErrorBar text={uiText(errorText(pref.error), "Unable to read interface preferences. Retry to refresh.")} action={{ label: "重试", onSelect: () => void pref.refetch() }} />;
  if (!draft || pref.isPending) {
    return (
      <div className="space-y-3" aria-busy>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-16 animate-pulse rounded-card bg-page" aria-hidden />
        ))}
        <p className="sr-only">正在读取界面偏好</p>
      </div>
    );
  }

  const tzItems = (tz.data?.items ?? []).filter((t) => !tzQuery.trim() || t.label.includes(tzQuery.trim()) || t.id.toLowerCase().includes(tzQuery.trim().toLowerCase()));
  const timezoneChanged = draft.timezone !== pref.data!.timezone;

  return (
    <div className="card max-w-[800px] space-y-6 p-5">
      <Row label="界面语言" hint={uiText("影响页面辅助文案和之后回合的表达偏好。它不是翻译目标语言，也不会替你决定任务译成哪种语言。", "Changes page guidance and the default language for future explanations. Translation language must be confirmed for each task.")}>
        <select className="input h-9 w-full max-w-[320px]" value={draft.interface_language} onChange={(e) => set("interface_language", e.target.value as Draft["interface_language"])} aria-label="界面语言">
          {LANGUAGES.map(item=><option key={item.code} value={item.code}>{languageNativeName(item.code)}</option>)}
        </select>
      </Row>

      <Row label="时区" hint={uiText("列表、事件和导出时间按这个时区显示；数据库仍以 UTC 保存。日期筛选按所选本地日的左闭右开区间由服务端换算。", "Lists, events and exports use this timezone. The database stores UTC; the server converts date filters to the selected local-day boundaries.")}>
        <div className="w-full max-w-[320px]">
          <input className="input h-9" placeholder="搜索时区" value={tzQuery} onChange={(e) => setTzQuery(e.target.value)} aria-label="搜索时区" />
          <select className="input mt-2 h-9" size={1} value={draft.timezone} onChange={(e) => set("timezone", e.target.value)} aria-label="时区">
            {tzItems.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}（{t.offset}）
              </option>
            ))}
            {!tzItems.some((t) => t.id === draft.timezone) && <option value={draft.timezone}>{draft.timezone}</option>}
          </select>
          {timezoneChanged && (
            <p className="mt-1.5 rounded-ctl bg-warn-bg px-2.5 py-1.5 text-[12px] text-warn">
              保存后时间显示将改为该时区，单据管理台的筛选区间会按新时区重新计算。历史记录与已导出文件不变。
            </p>
          )}
        </div>
      </Row>

      <Row label="复核面板宽度" hint={uiText("控制来源快速定位侧板。完整双语复核至少需要 832 px，空间不够时会临时全屏，不会改写这里保存的宽度。", "Controls the source-location panel. Full bilingual review needs at least 832 px and temporarily uses full screen when space is limited; the saved width is preserved.")}>
        <div className="w-full max-w-[320px]">
          <div className="flex items-center gap-3">
            <input
              type="range"
              className="w-[280px] shrink-0 accent-brand"
              min={MIN_W}
              max={MAX_W}
              step={1}
              value={draft.panel_width}
              aria-label="复核面板宽度"
              aria-valuetext={`${draft.panel_width} 像素`}
              onChange={(e) => set("panel_width", Number(e.target.value))}
              // 键盘每次 32 px，与侧板拖拽柄一致
              onKeyDown={(e) => {
                const up = e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "PageUp";
                const down = e.key === "ArrowLeft" || e.key === "ArrowDown" || e.key === "PageDown";
                if (!up && !down) return;
                e.preventDefault();
                const next = draft.panel_width + (up ? 32 : -32);
                set("panel_width", Math.min(MAX_W, Math.max(MIN_W, next)));
              }}
            />
            <span className="num shrink-0 whitespace-nowrap text-[13px] font-medium text-ink">{draft.panel_width} px</span>
          </div>
          {/* 刻度按 (值-520)/(900-520) 的真实比例定位，620 不能摆在滑轨正中 */}
          <div className="relative mt-1 h-4 w-[280px] text-[11px] text-ink-3">
            {[MIN_W, DEFAULT_W, MAX_W].map((v) => (
              <span key={v} className="num absolute -translate-x-1/2" style={{ left: `${((v - MIN_W) / (MAX_W - MIN_W)) * 100}%` }}>
                {v}
              </span>
            ))}
          </div>
          {/* 拖动即时预览：主区至少 560 px，右侧为当前草稿宽度 */}
          <div className="mt-2 w-[280px]" aria-hidden>
            <div className="flex h-9 gap-1 overflow-hidden rounded-ctl border border-line">
              <div className="flex flex-1 items-center justify-center bg-page text-[11px] text-ink-3">主区</div>
              <div className="flex shrink-0 items-center justify-center bg-brand-soft text-[11px] text-brand-ink" style={{ width: `${(draft.panel_width / (560 + draft.panel_width)) * 100}%` }}>
                侧板
              </div>
            </div>
            <p className="mt-1 text-[11px] text-ink-3">{uiText("示意：左侧主区保留至少 560 px，右侧为快速定位侧板。", "Layout preview: the main area keeps at least 560 px; the right area is the quick-location panel.")}</p>
          </div>
          <button type="button" className="btn btn-secondary btn-sm mt-2" onClick={() => set("panel_width", DEFAULT_W)} disabled={draft.panel_width === DEFAULT_W}>
            恢复默认 620 px
          </button>
          <p className="mt-1 text-[11px] text-ink-3">{uiText("拖动只即时预览布局，改变宽度不会重新识别原件，也不会清空表单或丢失选中字段。", "Dragging previews the layout. Changing the width does not reprocess sources, clear forms or lose the selected field.")}</p>
        </div>
      </Row>

      <Row label="减少动态效果" hint={uiText("影响面板展开与滚动定位动画。选择减少时直接定位，状态文字不会跟着减少。", "Controls panel and scroll animations. Reduced motion jumps directly to the target; status text stays visible.")}>
        <fieldset className="flex w-full max-w-[320px] flex-wrap gap-1.5">
          <legend className="sr-only">减少动态效果</legend>
          {MOTION.map((m) => (
            <label key={m.id} className={`tag cursor-pointer focus-within:outline focus-within:outline-2 focus-within:outline-focus ${draft.motion === m.id ? "tag-on" : "border-line"}`}>
              <input type="radio" name="motion" className="sr-only" checked={draft.motion === m.id} onChange={() => set("motion", m.id)} />
              {m.label}
            </label>
          ))}
        </fieldset>
      </Row>

      {conflict && (
        <div className="rounded-card border border-warn-line bg-warn-bg p-4">
          <p className="text-[13px] font-medium text-warn">{uiText("这份偏好已在别处更新", "These preferences were updated elsewhere.")}</p>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            <div className="rounded-ctl border border-line bg-card p-2.5 text-[12px]">
              <p className="mb-1 text-[11px] text-ink-3">服务端当前版本 v{conflict.version}</p>
              <p className="text-ink">时区 {conflict.timezone}</p>
              <p className="text-ink">语言 {languageName(conflict.interface_language)}</p>
              <p className="num text-ink">宽度 {conflict.panel_width} px</p>
              <p className="text-ink">动效 {MOTION.find((m) => m.id === conflict.motion)?.label ?? conflict.motion}</p>
            </div>
            <div className="rounded-ctl border border-brand-line bg-card p-2.5 text-[12px]">
              <p className="mb-1 text-[11px] text-ink-3">{uiText("你未保存的草稿", "Your unsaved draft")}</p>
              <p className="text-ink">时区 {draft.timezone}</p>
              <p className="text-ink">语言 {languageName(draft.interface_language)}</p>
              <p className="num text-ink">宽度 {draft.panel_width} px</p>
              <p className="text-ink">动效 {MOTION.find((m) => m.id === draft.motion)?.label ?? draft.motion}</p>
            </div>
          </div>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                void pref.refetch().then((r) => {
                  if (r.data) setDraft({ interface_language: r.data.interface_language, timezone: r.data.timezone, panel_width: r.data.panel_width, motion: r.data.motion });
                  setConflict(null);
                });
              }}
            >
              重新加载服务端版本
            </button>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => void navigator.clipboard.writeText(JSON.stringify(draft, null, 2)).then(() => toast({ tone: "ok", text: uiText("草稿已复制到剪贴板", "Draft copied to clipboard.") }))}>
              复制我的草稿
            </button>
          </div>
        </div>
      )}

      {save.error && <ErrorBar text={uiText(errorText(save.error), "Unable to save interface preferences. Reload the latest version or retry.")} action={{ label: "重试保存", onSelect: () => save.mutate(draft) }} />}

      <div className="flex items-center gap-2 border-t border-line pt-4">
        <span className="flex-1 text-[12px] text-ink-3">{pref.data!.saved_at ? `上次保存：${formatDateTime(pref.data!.saved_at)}` : "尚未保存过界面设置"}</span>
        <button
          type="button"
          className="btn btn-ghost"
          disabled={!dirty}
          onClick={() => {
            const d = pref.data!;
            setDraft({ interface_language: d.interface_language, timezone: d.timezone, panel_width: d.panel_width, motion: d.motion });
            setConflict(null);
          }}
        >
          取消修改
        </button>
        <button
          type="button"
          className="btn btn-primary btn-lg"
          disabled={!dirty || save.isPending}
          onClick={() =>
            save.mutate(draft, {
              onSuccess: () => toast({ tone: "ok", text: "界面设置已保存。当前任务内容与历史快照不受影响" }),
              onError: (e) => {
                const cur = (e as { current?: PreferencesView | null }).current;
                if (cur) setConflict(cur);
              },
            })
          }
        >
          {save.isPending ? "正在保存" : "保存界面设置"}
        </button>
      </div>
    </div>
  );
}

function Row({ label, hint, children }: { label: string; hint: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 min-[900px]:flex-row min-[900px]:gap-6">
      <div className="w-full shrink-0 min-[900px]:w-[220px]">
        <p className="text-[13px] font-medium text-ink">{label}</p>
        <p className="mt-1 text-[12px] leading-relaxed text-ink-3">{hint}</p>
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
