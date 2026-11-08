import { useEffect, useMemo, useRef, useState } from "react";

export type SelectableCase = { caseId: string; name: string };
export type CaseSelection = { enabled: boolean; selected: Map<string, SelectableCase>; toggle: (item: SelectableCase) => void; clear: () => void };

export function useCaseSelection(scope: string, enabled: boolean): CaseSelection {
  const [selected, setSelected] = useState(new Map<string, SelectableCase>());
  useEffect(() => setSelected(new Map()), [scope, enabled]);
  return { enabled, selected, clear: () => setSelected(new Map()), toggle: (item) => setSelected(old => {
    if (!enabled) return old;
    const next = new Map(old);
    next.has(item.caseId) ? next.delete(item.caseId) : next.set(item.caseId, item);
    return next;
  }) };
}

export function CaseCheckbox({ item, selection }: { item: SelectableCase; selection: CaseSelection }) {
  if (!selection.enabled) return null;
  return <input type="checkbox" className="h-4 w-4 shrink-0 accent-brand" aria-label={`选择整票 ${item.name}`} checked={selection.selected.has(item.caseId)} onChange={() => selection.toggle(item)} />;
}

export function SelectionBar({ items, selection, onDelete }: { items: SelectableCase[]; selection: CaseSelection; onDelete: () => void }) {
  const unique = useMemo(() => [...new Map(items.map(item => [item.caseId, item])).values()], [items]);
  const count = unique.filter(item => selection.selected.has(item.caseId)).length;
  const all = unique.length > 0 && count === unique.length;
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = count > 0 && !all; }, [count, all]);
  if (!selection.enabled || !unique.length) return null;
  return <div className="mb-3 flex flex-wrap items-center gap-3 rounded-ctl border border-line bg-page px-3 py-2 text-[12px] text-ink-2">
    <label className="flex items-center gap-2"><input ref={ref} type="checkbox" className="h-4 w-4 accent-brand" checked={all} onChange={() => unique.forEach(item => { if (all || !selection.selected.has(item.caseId)) selection.toggle(item); })} />全选已加载</label>
    <span>已选 {selection.selected.size} 票（同票记录联动）</span>
    {selection.selected.size > 0 && <button type="button" className="text-brand-ink underline underline-offset-2" onClick={selection.clear}>清空选择</button>}
    <button type="button" className="btn btn-danger btn-sm ml-auto" disabled={!selection.selected.size} onClick={onDelete}>删除所选整票</button>
  </div>;
}
