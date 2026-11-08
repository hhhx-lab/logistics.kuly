import { UiText } from "../../i18n/runtime";
import { LANGUAGES, languageCode, languageNativeName, languageName } from "../../i18n/languages";
import { useQuery } from "@tanstack/react-query";
import { managementApi } from "../../api/management";
import { useDocumentCatalog, groupedDocumentTypes } from "../../api/documentCatalog";
import { usePreferences } from "../settings/hooks";
import { useSession } from "../../stores/session";
// 状态页签与筛选条。条件之间 AND、同维多选 OR；日期只提交本地字符串，
// 由服务端按本人时区换算；任何条件变化清游标重查。
import { useEffect, useRef, useState } from "react";
import type { RunFacets } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, Popover, useEscLayer } from "../../components/Overlay";
import { DEFAULT_FILTER, activeChips, naturalFilter, type ActorRole, type DateType, type ManagementFilter, type OriginRole, type RecipientRole, type StatusTab, toQuery } from "./hooks";

const TABS: { key: StatusTab; label: string }[] = [
  { key: "exported", label: "已导出" },
  { key: "awaiting", label: "待复核与可导出" },
  { key: "processing", label: "处理中" },
  { key: "failed", label: "失败与取消" },
  { key: "indexed", label: "识别与索引" },
  { key: "checked", label: "同票核对" },
  { key: "drafts", label: "上传草稿" },
  { key: "all", label: "全部" },
];

const DATE_TYPES: { key: DateType; label: string }[] = [
  { key: "exported", label: "导出时间" },
  { key: "uploaded", label: "上传时间" },
  { key: "initiated", label: "处理发起时间" },
  { key: "activity", label: "最近活动时间" },
  { key: "reviewed", label: "复核时间" },
];

const ORIGIN_ROLES: { key: OriginRole; label: string }[] = [
  { key: "origin", label: "起运地" },
  { key: "receipt", label: "承运人接货地" },
  { key: "pol", label: "装货港" },
  { key: "country", label: "原产国／地区" },
];

const RECIPIENT_ROLES: { key: RecipientRole; label: string }[] = [
  { key: "consignee", label: "收货人" },
  { key: "buyer", label: "买方" },
  { key: "notify", label: "通知方" },
];

const ACTOR_ROLES: { key: ActorRole; label: string }[] = [
  { key: "initiator", label: "处理发起人" },
  { key: "reviewer", label: "复核人" },
  { key: "exporter", label: "导出确认人" },
];

const FORMATS = [
  { id: "pdf", label: "PDF" },
  { id: "image", label: "图片" },
  { id: "docx", label: "DOCX" },
];

const RISKS = [
  { id: "blocking", label: "红项" },
  { id: "warning", label: "黄项" },
  { id: "undetermined", label: "无法判断" },
];

export function StatusTabs({ tab, counts, onTab }: { tab: StatusTab; counts?: RunFacets["status_counts"]; onTab: (t: StatusTab) => void }) {
  const countOf = (k: StatusTab) => {
    // 服务端按页签的实际筛选定义计数，不能再次按旧Run状态键拼接。
    return counts?.[k];
  };
  return (
    <div className="flex h-11 shrink-0 items-center gap-0.5 overflow-x-auto scroll-thin border-b border-line" role="tablist" aria-label="记录状态">
      {TABS.map((t) => {
        const n = countOf(t.key);
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`relative shrink-0 px-3 py-2.5 text-[13px] ${tab === t.key ? "font-medium text-ink" : "text-ink-2 hover:text-ink"}`}
            onClick={() => onTab(t.key)}
          >
            {t.label}
            {n !== undefined && <span className="num ml-1 text-ink-3">{n}</span>}
            {tab === t.key && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded bg-brand" />}
          </button>
        );
      })}
    </div>
  );
}

export function FilterBar({ filter, onChange }: { filter: ManagementFilter; onChange: (f: ManagementFilter) => void }) {
  const catalog = useDocumentCatalog();
  const gen = useSession(state => state.generation);
  const preferences = usePreferences();
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [debouncedQ, setDebouncedQ] = useState(filter.q);
  const [conditionDraft, setConditionDraft] = useState<ManagementFilter | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const previewedText = useRef<string | null>(null);
  const [narrow, setNarrow] = useState(() => window.innerWidth < 960);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 959px)");
    const change = () => setNarrow(query.matches);
    query.addEventListener("change", change); return () => query.removeEventListener("change", change);
  }, []);
  const suggestionFilter = { ...filter, q: debouncedQ };
  const suggestions = useQuery({ queryKey: ["management-suggestions", gen, suggestionFilter], queryFn: () => managementApi.suggestions(toQuery(suggestionFilter)), enabled: !!debouncedQ && suggestOpen && !conditionDraft, retry: false });
  const actors = useQuery({ queryKey:["management-actors",gen],queryFn:managementApi.actors,retry:false });
  const [draftQ, setDraftQ] = useState(filter.q);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLButtonElement>(null);
  const chips = activeChips(filter);

  useEffect(() => {
    if (draftQ === filter.q) { setDebouncedQ(filter.q); return; }
    const t = window.setTimeout(() => {
      if (previewedText.current === draftQ) return;
      const proposal = naturalFilter(draftQ, filter, preferences.data?.timezone ?? "Asia/Shanghai");
      if (proposal) previewedText.current = draftQ;
      setConditionDraft(proposal);
      if (!proposal) { setDebouncedQ(draftQ); onChange({ ...filter, q: draftQ }); }
    }, 320);
    return () => window.clearTimeout(t);
  }, [draftQ, filter, onChange, preferences.data?.timezone]);

  useEffect(() => setDraftQ(filter.q), [filter.q]);

  const set = <K extends keyof ManagementFilter>(k: K, v: ManagementFilter[K]) => onChange({ ...filter, [k]: v });
  const toggle = (k: "doc_types" | "formats" | "languages" | "risks", id: string) => {
    const cur = filter[k];
    set(k, cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  };

  const clearChip = (key: string) => {
    if (key === "date") return onChange({ ...filter, local_date_from: "", local_date_to: "" });
    if (key === "doc_types" || key === "formats" || key === "languages" || key === "risks") return set(key, []);
    if (key === "version_scope") return set("version_scope", "current");
    if (key === "match_scope") return set("match_scope", "same_logical_document");
    if (key === "only_confirmed") return set("only_confirmed", false);
    return onChange({ ...filter, [key]: "" } as ManagementFilter);
  };

  const quickRange = (days: number) => {
    const to = new Date().toLocaleDateString("sv-SE", { timeZone: preferences.data?.timezone ?? "Asia/Shanghai" });
    const from = new Date(`${to}T12:00:00Z`);
    from.setUTCDate(from.getUTCDate() - days + 1);
    onChange({ ...filter, local_date_from: from.toISOString().slice(0, 10), local_date_to: to });
  };

  const filterFields = (
        <div className="space-y-3 p-3">
          {narrow && <Field label="日期范围">
            <select className="input h-8" value={filter.date_type} onChange={e => set("date_type", e.target.value as DateType)} aria-label="日期类型">{DATE_TYPES.map(d => <option key={d.key} value={d.key}>{d.label}</option>)}</select>
            <div className="mt-2 flex gap-2"><input className="input min-w-0" type="date" value={filter.local_date_from} onChange={e => set("local_date_from", e.target.value)} aria-label="起始日期" /><input className="input min-w-0" type="date" value={filter.local_date_to} onChange={e => set("local_date_to", e.target.value)} aria-label="结束日期" /></div>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => quickRange(7)}>近 7 天</button><button type="button" className="btn btn-ghost btn-sm" onClick={() => quickRange(30)}>近 30 天</button>
          </Field>}
          <Field label="来源地">
            <div className="flex gap-1.5">
              <select className="input h-8 w-[150px] text-[13px]" value={filter.origin_role} onChange={(e) => set("origin_role", e.target.value as OriginRole)} aria-label="来源地角色">
                {ORIGIN_ROLES.map((r) => (
                  <option key={r.key} value={r.key}>
                    {r.label}
                  </option>
                ))}
              </select>
              <input className="input h-8 flex-1 text-[13px]" placeholder="填写地点值" value={filter.origin_value} onChange={(e) => set("origin_value", e.target.value)} />
            </div>
            <p className="mt-1 text-[11px] text-ink-3">各角色独立匹配，不会从发货人地址推断起运地。</p>
          </Field>

          <Field label="收件人">
            <div className="flex gap-1.5">
              <select className="input h-8 w-[150px] text-[13px]" value={filter.recipient_role} onChange={(e) => set("recipient_role", e.target.value as RecipientRole)} aria-label="收件人角色">
                {RECIPIENT_ROLES.map((r) => (
                  <option key={r.key} value={r.key}>
                    {r.label}
                  </option>
                ))}
              </select>
              <input className="input h-8 flex-1 text-[13px]" placeholder="填写名称" value={filter.recipient_value} onChange={(e) => set("recipient_value", e.target.value)} />
            </div>
          </Field>

          <Field label="处理人">
            <div className="flex gap-1.5">
              <select className="input h-8 w-[150px] text-[13px]" value={filter.actor_role} onChange={(e) => set("actor_role", e.target.value as ActorRole)} aria-label="处理人角色">
                {ACTOR_ROLES.map((r) => (
                  <option key={r.key} value={r.key}>
                    {r.label}
                  </option>
                ))}
              </select>
              <select className="input h-8 min-w-0 flex-1 text-[13px]" value={filter.actor_value} onChange={e => set("actor_value", e.target.value)} aria-label="授权处理人">
                <option value="">不限处理人</option>
                {filter.actor_value && !actors.data?.items.some(actor => actor.name === filter.actor_value) && <option value={filter.actor_value}>{filter.actor_value}</option>}
                {actors.data?.items.map(actor => <option key={actor.id} value={actor.name}>{actor.name}</option>)}
              </select>
            </div>
            <p className="mt-1 text-[11px] text-ink-3">只匹配系统登录身份，不检索单证正文里的人名。</p>
          </Field>

          <Field label="单据类型">
            <div className="max-h-52 space-y-2 overflow-y-auto scroll-thin">
              {groupedDocumentTypes(catalog.data?.items ?? []).map(group => <details key={group.group} open={group.items.some(item => filter.doc_types.includes(item.code))}>
                <summary className="cursor-pointer text-[12px] text-ink-2">{group.group}</summary>
                <div className="mt-1.5"><ChipSet options={group.items.map(item => ({ id: item.code === "supporting" ? "other" : item.code === "unknown" ? "unclassified" : item.code, label: item.label }))} selected={filter.doc_types} onToggle={id => toggle("doc_types", id)} /></div>
              </details>)}
              {catalog.error && <button type="button" className="btn btn-ghost btn-sm" onClick={() => void catalog.refetch()}>重新加载单据类型</button>}
            </div>
          </Field>
          <Field label="物理格式">
            <ChipSet options={FORMATS} selected={filter.formats} onToggle={(id) => toggle("formats", id)} />
          </Field>
          <Field label="目标语言">
            <ChipSet options={LANGUAGES.map(item=>({id:item.code,label:languageNativeName(item.code)}))} selected={filter.languages} onToggle={(id) => toggle("languages", id)} />
          </Field>
          <Field label="风险状态">
            <ChipSet options={RISKS} selected={filter.risks} onToggle={(id) => toggle("risks", id)} />
          </Field>

          <Field label="版本范围">
            <div className="flex gap-1.5">
              {([["current", "仅当前结果"], ["include_history", "包含历史快照"]] as const).map(([id, label]) => (
                <label key={id} className={`tag cursor-pointer ${filter.version_scope === id ? "tag-on" : "border-line"}`}>
                  <input type="radio" name="version-scope" className="sr-only" checked={filter.version_scope === id} onChange={() => set("version_scope", id)} />
                  {label}
                </label>
              ))}
            </div>
            <p className="mt-1 text-[11px] text-ink-3">包含历史快照后，命中行会显示实际命中的那份快照，打开复核与询问都沿用它。</p>
          </Field>

          <Field label="匹配范围">
            <div className="flex gap-1.5">
              {([["same_logical_document", "同一逻辑单据"], ["any_in_case", "同一批次任意文件"]] as const).map(([id, label]) => (
                <label key={id} className={`tag cursor-pointer ${filter.match_scope === id ? "tag-on" : "border-line"}`}>
                  <input type="radio" name="match-scope" className="sr-only" checked={filter.match_scope === id} onChange={() => set("match_scope", id)} />
                  {label}
                </label>
              ))}
            </div>
          </Field>

          <label className="flex items-center gap-2 text-[13px] text-ink">
            <input type="checkbox" checked={filter.only_confirmed} onChange={(e) => set("only_confirmed", e.target.checked)} />
            仅匹配已确认值
            <span className="text-[11px] text-ink-3">关闭时识别候选也会命中，并标注「候选／待复核」</span>
          </label>
        </div>
  );

  return (
    <div className="shrink-0 border-b border-line bg-card px-4 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative w-[280px] max-w-full">
          <Icon name="search" size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-3" />
          <input ref={searchRef} onFocus={() => setSuggestOpen(true)} onKeyDown={e => { if (e.key === "Escape") setSuggestOpen(false); }} className="input h-9 pl-8 pr-8" placeholder="搜索单票、文件、单号，或近7天英文结果" value={draftQ} onChange={(e) => {previewedText.current = null; setDraftQ(e.target.value);setSuggestOpen(true);}} aria-label="搜索单票、文件或单号" />
          {draftQ && (
            <button type="button" className="absolute right-1.5 top-1/2 -translate-y-1/2 p-1 text-ink-3 hover:text-ink" onClick={() => setDraftQ("")} aria-label="清除搜索">
              <Icon name="close" size={14} />
            </button>
          )}
        </label>

        <div className="hidden items-center gap-2 min-[960px]:flex"><select className="input h-9 w-[132px]" value={filter.date_type} onChange={(e) => set("date_type", e.target.value as DateType)} aria-label="日期类型">
          {DATE_TYPES.map((d) => (
            <option key={d.key} value={d.key}>
              {d.label}
            </option>
          ))}
        </select>
        <input className="input h-9 w-[132px]" type="date" value={filter.local_date_from} onChange={(e) => set("local_date_from", e.target.value)} aria-label="起始日期" />
        <span className="text-[12px] text-ink-3">至</span>
        <input className="input h-9 w-[132px]" type="date" value={filter.local_date_to} onChange={(e) => set("local_date_to", e.target.value)} aria-label="结束日期" />
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => quickRange(7)}>
          近 7 天
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => quickRange(30)}>
          近 30 天
        </button></div>

        <button ref={moreRef} type="button" className="btn btn-secondary btn-sm" onClick={() => setMoreOpen(true)} aria-expanded={moreOpen}>
          {narrow ? "筛选" : "更多筛选"}
          <Icon name="chevron-down" size={14} />
        </button>
        <span className="flex-1" />
        {chips.length > 0 && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => onChange({ ...DEFAULT_FILTER })}>
            重置筛选
          </button>
        )}
      </div>

      {chips.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {chips.map((c) => (
            <span key={String(c.key)} className="tag border-line bg-page text-[12px] text-ink-2">
              {c.label}
              <button type="button" className="ml-0.5 flex h-5 w-5 items-center justify-center rounded text-ink-3 hover:text-ink" onClick={() => clearChip(String(c.key))} aria-label={`清除条件 ${c.label}`}>
                <Icon name="close" size={12} />
              </button>
            </span>
          ))}
        </div>
      )}

      <Popover open={suggestOpen && !!draftQ && !conditionDraft} onClose={() => setSuggestOpen(false)} anchor={searchRef} width={280} maxHeight={420} label="搜索建议">
        <div className="p-2 text-[12px]">
          {draftQ !== debouncedQ || suggestions.isFetching ? <p className="p-2 text-ink-3">正在更新建议…</p> : suggestions.isError ? <p className="p-2 text-risk">建议读取失败，可查看分页结果或重新输入。</p> : <>
            {suggestions.data?.items.map(item => <button key={item.run_id} type="button" className="block w-full rounded-ctl p-2 text-left hover:bg-page" onClick={() => { setSuggestOpen(false); window.location.assign(`/documents/cases/${item.case_id}`); }}>
              <span className="block truncate font-medium text-ink" title={item.title}>{item.title}</span>
              <span className="block text-ink-3">{item.language ?? "无译文"} · {item.status} · {item.snapshot_id ? "历史／交付快照" : "当前工作版"}</span>
              <span className="block text-ink-3">{item.match_explanation} · {item.activity_at ? new Date(item.activity_at).toLocaleString() : ""}</span>
              {item.matched_sources.map((hit, index) => <span key={index} className="block text-ink-2">{hit.filename} v{hit.document_version} · {hit.field_role} · {hit.state === "confirmed" ? "已确认" : "候选／待复核"}</span>)}
            </button>)}
            {!suggestions.data?.items.length && <p className="p-2 text-ink-3">当前条件没有建议。</p>}
          </>}
          <button type="button" className="btn btn-link btn-sm w-full" onClick={() => { setSuggestOpen(false); setDebouncedQ(draftQ); onChange({ ...filter, q: draftQ }); }}>查看全部匹配</button>
        </div>
      </Popover>
      {narrow ? <FilterDrawer open={moreOpen} onClose={() => setMoreOpen(false)}>{filterFields}</FilterDrawer> : <Popover open={moreOpen} onClose={() => setMoreOpen(false)} anchor={moreRef} width={420} maxHeight={520} label="更多筛选" align="start">{filterFields}</Popover>}
      <Dialog open={!!conditionDraft} onClose={() => { setConditionDraft(null); setDraftQ(filter.q); }} title="确认搜索条件" description="识别出的条件尚未生效。可编辑后应用，未识别的文字保留为关键词。" width={520} footer={<>
        <button type="button" className="btn btn-secondary" onClick={() => { setConditionDraft(null); setDebouncedQ(draftQ); onChange({ ...filter, q: draftQ }); }}>按原文搜索</button>
        <button type="button" className="btn btn-primary" onClick={() => { if (!conditionDraft) return; onChange(conditionDraft); setDraftQ(conditionDraft.q); setDebouncedQ(conditionDraft.q); setConditionDraft(null); setSuggestOpen(false); }}>应用条件</button>
      </>}>
        {conditionDraft && <div className="space-y-2">
          <div className="flex flex-wrap gap-1">{activeChips(conditionDraft).map(chip => <span key={chip.key} className="tag border-line">{chip.label}</span>)}</div>
          <label className="block">关键词<input className="input" value={conditionDraft.q} onChange={e => setConditionDraft({ ...conditionDraft, q: e.target.value })} /></label>
          <label className="block">状态<select className="input" value={conditionDraft.tab} onChange={e => setConditionDraft({ ...conditionDraft, tab: e.target.value as StatusTab })}>{TABS.map(tab => <option key={tab.key} value={tab.key}>{tab.label}</option>)}</select></label>
          <label className="block">日期口径<select className="input" value={conditionDraft.date_type} onChange={e => setConditionDraft({ ...conditionDraft, date_type: e.target.value as DateType })}>{DATE_TYPES.map(date => <option key={date.key} value={date.key}>{date.label}</option>)}</select></label>
          <div className="flex gap-2"><input className="input min-w-0" type="date" aria-label="草案起始日期" value={conditionDraft.local_date_from} onChange={e => setConditionDraft({ ...conditionDraft, local_date_from: e.target.value })} /><input className="input min-w-0" type="date" aria-label="草案结束日期" value={conditionDraft.local_date_to} onChange={e => setConditionDraft({ ...conditionDraft, local_date_to: e.target.value })} /></div>
          <label className="block">目标语言<select className="input" value={conditionDraft.languages[0] ?? ""} onChange={e => setConditionDraft({ ...conditionDraft, languages: e.target.value ? [e.target.value] : [] })}><option value="">不限</option>{LANGUAGES.map(item=><option key={item.code} value={item.code}>{languageNativeName(item.code)}</option>)}</select></label>
          <label className="block">来源地角色<select className="input" value={conditionDraft.origin_role} onChange={e => setConditionDraft({ ...conditionDraft, origin_role: e.target.value as OriginRole })}>{ORIGIN_ROLES.map(role => <option key={role.key} value={role.key}>{role.label}</option>)}</select></label>
          <label className="block">来源地<input className="input" value={conditionDraft.origin_value} onChange={e => setConditionDraft({ ...conditionDraft, origin_value: e.target.value })} /></label>
          <label className="block">收件人角色<select className="input" value={conditionDraft.recipient_role} onChange={e => setConditionDraft({ ...conditionDraft, recipient_role: e.target.value as RecipientRole })}>{RECIPIENT_ROLES.map(role => <option key={role.key} value={role.key}>{role.label}</option>)}</select></label>
          <label className="block">收件人<input className="input" value={conditionDraft.recipient_value} onChange={e => setConditionDraft({ ...conditionDraft, recipient_value: e.target.value })} /></label>
        </div>}
      </Dialog>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-1 text-[12px] font-medium text-ink">{label}</p>
      {children}
    </div>
  );
}

function ChipSet({ options, selected, onToggle }: { options: { id: string; label: string }[]; selected: string[]; onToggle: (id: string) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((o) => (
        <label key={o.id} className={`tag cursor-pointer ${selected.includes(o.id) ? "tag-on" : "border-line"}`}>
          <input type="checkbox" className="sr-only" checked={selected.includes(o.id)} onChange={() => onToggle(o.id)} />
          {languageCode(o.id) ? <span lang={o.id}>{languageNativeName(o.id)}</span> : <UiText value={o.label} />}
        </label>
      ))}
    </div>
  );
}

function FilterDrawer({ open, onClose, children }: { open: boolean; onClose: () => void; children: React.ReactNode }) {
  const panel = useRef<HTMLElement>(null);
  useEscLayer(open, onClose);
  useEffect(() => {
    if (!open) return;
    const trigger = document.activeElement as HTMLElement | null;
    panel.current?.querySelector<HTMLElement>("button,input,select")?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const controls = Array.from(panel.current?.querySelectorAll<HTMLElement>("button:not(:disabled),input,select") ?? []);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    panel.current?.addEventListener("keydown", trap);
    const root = panel.current;
    return () => { root?.removeEventListener("keydown", trap); trigger?.focus(); };
  }, [open]);
  if (!open) return null;
  return <div className="fixed inset-0 z-modal"><div className="absolute inset-0 bg-[rgba(17,20,24,.28)]" onClick={onClose} aria-hidden /><aside ref={panel} role="dialog" aria-modal="true" aria-label="筛选" className="absolute inset-y-0 right-0 flex w-[360px] max-w-full flex-col bg-card shadow-panel"><header className="flex items-center justify-between border-b border-line p-3"><span>筛选</span><button type="button" className="btn btn-icon" onClick={onClose} aria-label="关闭筛选"><Icon name="close" size={16} /></button></header><div className="min-h-0 flex-1 overflow-auto">{children}</div><footer className="border-t border-line p-3"><button type="button" className="btn btn-primary w-full" onClick={onClose}>完成筛选</button></footer></aside></div>;
}
