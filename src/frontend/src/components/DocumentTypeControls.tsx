import { useState } from "react";
import { groupedDocumentTypes, type CargoProfileEntry, type DocumentTypeEntry } from "../api/documentCatalog";

export function DocumentTypeSelect({ items, value, onChange, label = "单据类型", placeholder = "请选择", disabled = false }: {
  items: DocumentTypeEntry[]; value: string; onChange: (code: string) => void; label?: string; placeholder?: string; disabled?: boolean;
}) {
  return <select className="input" aria-label={label} value={value} onChange={event => onChange(event.target.value)} disabled={disabled}>
    <option value="">{placeholder}</option>
    {groupedDocumentTypes(items).map(group => <optgroup key={group.group} label={group.group}>
      {group.items.map(item => <option key={item.code} value={item.code}>{item.label}</option>)}
    </optgroup>)}
  </select>;
}

export function CargoProfileSelect({ items, value, onChange, disabled = false }: {
  items: CargoProfileEntry[]; value: string[]; onChange: (codes: string[]) => void; disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const visible = items.filter(item => item.label.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()) || item.code.includes(query.trim().toLowerCase()));
  const toggle = (code: string) => onChange(value.includes(code) ? value.filter(item => item !== code) : [...value, code]);
  return <details className="mt-3 rounded-ctl border border-line p-2.5">
    <summary className="cursor-pointer text-[13px] font-medium">货物领域（可选{value.length ? ` · 已选 ${value.length}` : ""}）</summary>
    <input className="input mt-2" aria-label="搜索货物领域" placeholder="搜索货物领域" value={query} onChange={event => setQuery(event.target.value)} disabled={disabled} />
    <div className="mt-2 grid max-h-52 grid-cols-2 gap-1.5 overflow-y-auto scroll-thin">
      {visible.map(item => <label key={item.code} className={`flex cursor-pointer items-start gap-2 rounded-ctl border p-2 text-[12px] ${value.includes(item.code) ? "opt-on" : "border-line"}`} title={item.review_focus}>
        <input type="checkbox" checked={value.includes(item.code)} disabled={disabled || (!value.includes(item.code) && value.length >= 16)} onChange={() => toggle(item.code)} />
        <span>{item.label}</span>
      </label>)}
      {!visible.length && <p className="col-span-2 text-[12px] text-ink-3">没有匹配领域</p>}
    </div>
  </details>;
}
