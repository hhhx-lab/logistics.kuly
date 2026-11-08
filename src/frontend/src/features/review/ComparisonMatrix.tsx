import { Fragment, useMemo, useState } from "react";
import type { ComparisonRow, ComparisonSource } from "../../api/consignment";
import type { ReviewView } from "../../api/types";
import { UiText } from "../../i18n/runtime";
import { Icon } from "../../components/Icon";
import { StatusPill } from "../../components/Status";

export const comparisonLabels: Record<string, string> = {
  blocking: "红项", red: "红项", warning: "黄项", yellow: "黄项", undetermined: "无法判断", unknown: "无法判断",
  passed: "一致", matched: "一致", normal: "正常差异", different: "存在差异", failed: "存在差异", conflict: "存在差异", skipped: "核对未执行", not_applicable: "不适用", pending: "待关联", awaiting_relation: "待关联", resolved: "已处理", ignored: "已忽略",
  equivalent: "一致", scope_insufficient: "关联不明", "goods.quantity": "货物数量", "goods.package_count": "包装件数", "goods.net_weight": "净重", "goods.gross_weight": "毛重", "goods.chargeable_weight": "计费重", "document.reference": "单据参考号", "party.tax_id": "主体税号", "party.address": "主体地址", "party.name": "主体名称", left_total: "左侧合计", right_total: "右侧合计", delta: "差值", tolerance: "允许误差", unit: "单位", kind: "核对项目",
  numeric_mismatch: "数值差异", party_mismatch: "主体差异", role_difference: "主体角色差异", stage_difference: "运输阶段差异", source: "原件值", adopted: "采用值", quantity: "数量", gross_weight: "毛重", net_weight: "净重", packages: "包装件数", amount: "金额", container_no: "集装箱号", document_number: "单据编号", port_of_loading: "装货港", port_of_discharge: "卸货港", buyer: "买方", seller: "卖方", shipper: "发货人", consignee: "收货人", transport_reference: "运输参考号", cross_document: "跨单证核对", identical: "一致", needs_review: "待核对",
};
const EXPLANATIONS: Record<string, string> = {
  "Cargo association awaits confirmation": "货物对应关系尚未确认",
  "Cargo or transport stage is unknown": "货批或运输阶段尚未明确",
  "Comparison material coverage is not confirmed complete": "尚未确认双方材料覆盖完整核对范围",
  "Measurement value or unit is missing": "计量数值或单位缺失",
  "Packaging level is unknown": "包装层级尚未明确",
  "Measurement units are not equivalent": "两侧计量单位无法可靠换算",
  "Weight precision and tolerance have not been verified": "重量精度与允许误差尚未确认",
};
export function comparisonDescription(row: ComparisonRow) {
  if (EXPLANATIONS[row.description]) return EXPLANATIONS[row.description];
  if (typeof row.calculation.left_total === "string" && typeof row.calculation.right_total === "string") return `左侧合计 ${row.calculation.left_total}，右侧合计 ${row.calculation.right_total}，差值 ${row.calculation.delta ?? "—"}，允许误差 ${row.calculation.tolerance ?? "—"}`;
  if (["passed","failed"].includes(row.status)) return row.status === "passed" ? "两份单据的此项信息一致" : "两份单据的此项信息存在差异，请结合来源核对";
  return row.description;
}
export function ComparisonState({ row }: { row: ComparisonRow }) {
  const closed = !!row.disposition;
  const red = ["blocking", "red"].includes(row.severity);
  const yellow = ["warning", "yellow"].includes(row.severity);
  const neutral = ["passed", "normal", "matched", "not_applicable"].includes(row.status);
  return <StatusPill tone={closed || neutral ? "info" : red ? "risk" : yellow ? "warn" : "info"}><UiText value={closed ? "已处理" : comparisonLabels[row.status] ?? comparisonLabels[row.severity] ?? "无法判断"} /></StatusPill>;
}
export function ComparisonMatrix({ rows, view, width, selectedRowId, onSelectEvidence, onOpenIssue, canOpenIssue = true }: { rows: ComparisonRow[]; view: ReviewView; width: number; selectedRowId: string | null; onSelectEvidence: (row: ComparisonRow, source: ComparisonSource) => void; onOpenIssue: (id: string) => void; canOpenIssue?: boolean }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const limit = width < 920 ? 2 : 4;
  const columns = useMemo(() => {
    const map = new Map<string, { id: string; title: string; documentId: string | null; logicalId: string | null }>();
    rows.forEach(row => row.sources.forEach(source => {
      const target = source.target;
      const id = target.logical_document_id ?? target.document_id ?? source.side;
      const file = view.files.find(file => file.source.document_id === target.document_id);
      const logical = file?.logical_documents.find(item => item.logical_document_id === target.logical_document_id);
      if (!map.has(id)) map.set(id, { id, title: [file?.source.filename, logical?.label].filter(Boolean).join(" · ") || source.side, documentId: target.document_id ?? null, logicalId: target.logical_document_id ?? null });
    }));
    return [...map.values()];
  }, [rows, view.files]);
  const visibleColumns = (selectedColumns.length ? columns.filter(column => selectedColumns.includes(column.id)) : columns).slice(0, limit);
  const chooseColumn = (index: number, id: string) => {
    const chosen = visibleColumns.map(item => item.id); chosen[index] = id;
    setSelectedColumns([...new Set(chosen)]);
  };
  return <div className="flex min-h-0 flex-1 flex-col">
    {columns.length > limit && <div className="flex shrink-0 flex-wrap gap-2 border-b border-line p-2" role="group" aria-label="比较文件选择">
      {visibleColumns.map((column, index) => <select key={index} className="input min-w-0 max-w-[240px] flex-1 text-[12px]" value={column.id} onChange={event => chooseColumn(index, event.target.value)} aria-label={`比较文件 ${index + 1}`}>{columns.map(item => <option key={item.id} value={item.id} disabled={item.id !== column.id && visibleColumns.some(value => value.id === item.id)}>{item.title}</option>)}</select>)}
    </div>}
    <div className="min-h-0 flex-1 overflow-auto scroll-thin">
      <table className="w-full border-separate border-spacing-0 text-left text-[12px]" style={{ minWidth: visibleColumns.length ? 200 + visibleColumns.length * 190 : 400 }}>
        <caption className="sr-only">同票多单证比较矩阵</caption>
        <thead className="sticky top-0 z-[2] bg-page"><tr>
          <th className="sticky left-0 z-[3] w-[200px] border-b border-r border-line bg-page px-3 py-2 font-medium">核对项目</th>
          {visibleColumns.map(column => <th key={column.id} className="min-w-[190px] border-b border-r border-line px-3 py-2 font-normal"><span className="line-clamp-2" title={column.title}>{column.title}</span></th>)}
          {!visibleColumns.length && <th className="border-b border-line px-3 py-2 font-normal">核对说明</th>}
        </tr></thead>
        <tbody>{rows.map(row => <Fragment key={row.row_id}>
          <tr className={selectedRowId === row.row_id ? "bg-sel-soft" : "hover:bg-page"}>
            <th className={`sticky left-0 z-[1] border-b border-r border-line p-3 align-top font-normal ${selectedRowId === row.row_id ? "bg-sel-soft" : "bg-card"}`}>
              <button type="button" className="flex w-full items-start gap-1 text-left" aria-expanded={!!expanded[row.row_id]} onClick={() => setExpanded(value => ({ ...value, [row.row_id]: !value[row.row_id] }))}>
                <Icon name={expanded[row.row_id] ? "chevron-down" : "chevron-right"} size={14} className="mt-0.5 shrink-0 text-ink-3" />
                <span><span className="mb-1 block font-medium"><UiText value={comparisonLabels[row.rule_id] ?? comparisonLabels[row.difference_kind] ?? "核对项目"} /></span><ComparisonState row={row} /><span className="mt-1 block text-[11px] text-ink-3"><UiText value={comparisonLabels[row.comparison_basis] ?? row.comparison_basis} /></span></span>
              </button>
            </th>
            {visibleColumns.map(column => {
              const sources = row.sources.filter(source => column.logicalId ? source.target.logical_document_id === column.logicalId : source.target.document_id === column.documentId);
              const sides = new Set(sources.map(source => source.side));
              const members = Object.entries(row.members).flatMap(([side, members]) => members.filter(member => member.logical_id === column.logicalId || member.document_id === column.documentId || sides.has(side)));
              return <td key={column.id} className="border-b border-r border-line p-3 align-top">
                {members.length ? members.map((member, index) => <div key={member.line_id ?? index} className="mb-1.5 last:mb-0"><p className="break-words">{member.source ?? member.value ?? "—"}{member.unit ? ` ${member.unit}` : ""}</p>{member.adopted && member.adopted !== (member.source ?? member.value) && <p className="mt-0.5 break-words text-ink-3">采用值：{member.adopted}</p>}</div>) : sources.length ? <p className="line-clamp-3 break-words">{sources[0].excerpt || "—"}</p> : <span className="text-ink-3">不参与此项</span>}
                {sources.map((source, index) => <button key={index} type="button" className="btn btn-link btn-sm mt-1 px-0" onClick={() => onSelectEvidence(row, source)}>定位来源{sources.length > 1 ? ` ${index + 1}` : ""}</button>)}
              </td>;
            })}
            {!visibleColumns.length && <td className="border-b border-line p-3 align-top"><UiText value={comparisonDescription(row)} /></td>}
          </tr>
          {expanded[row.row_id] && <tr className="bg-page"><td colSpan={visibleColumns.length + 1} className="border-b border-line p-3">
            <p className="whitespace-pre-wrap text-ink-2"><UiText value={comparisonDescription(row)} /></p>
            <p className="mt-1 text-ink-3">比较口径：<UiText value={comparisonLabels[row.comparison_basis] ?? row.comparison_basis} /></p>
            {!!Object.keys(row.calculation).length && <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-ink-2">{Object.entries(row.calculation).filter(([, value]) => typeof value === "string" || typeof value === "number").map(([key, value]) => <div key={key}><dt className="inline"><UiText value={comparisonLabels[key] ?? key} />：</dt><dd className="inline num">{String(value)}</dd></div>)}</dl>}
            {row.issue_id && <button type="button" className="btn btn-secondary btn-sm mt-2" disabled={!canOpenIssue} onClick={() => onOpenIssue(row.issue_id!)}>处理此疑点</button>}
          </td></tr>}
        </Fragment>)}</tbody>
      </table>
    </div>
  </div>;
}
