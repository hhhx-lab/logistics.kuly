import { useState } from "react";
import { consignmentApi, scopePreconditions, type ComparisonRow, type ComparisonSource, type ConsignmentScope } from "../../api/consignment";
import { errorText } from "../../api/client";
import { UiText } from "../../i18n/runtime";
import { ErrorBar } from "../../components/Status";
import { useCommand } from "../conversation/hooks";

export function CargoMatchReview({ row, scope, editable, onChanged, onLocate }: { row: ComparisonRow; scope?: ConsignmentScope; editable: boolean; onChanged: (runId: string | null) => void; onLocate: (row: ComparisonRow, source: ComparisonSource) => void }) {
  const frozen = (row.calculation.scope ?? {}) as Record<string, unknown>;
  const [cargoId, setCargoId] = useState(typeof frozen.cargo_id === "string" ? frozen.cargo_id : "");
  const [legId, setLegId] = useState(typeof frozen.leg_id === "string" ? frozen.leg_id : "");
  const [completeness, setCompleteness] = useState<"complete" | "partial" | "unknown">(frozen.completeness === "complete" || frozen.completeness === "partial" ? frozen.completeness : "unknown");
  const [quantityKind, setQuantityKind] = useState(typeof frozen.quantity_kind === "string" ? frozen.quantity_kind : "quantity");
  const [packageLevel, setPackageLevel] = useState("");
  const [tolerance, setTolerance] = useState("");
  const [basis, setBasis] = useState("");
  const left = [...new Set((row.members.left ?? []).map(item => item.line_id).filter((value): value is string => !!value))];
  const right = [...new Set((row.members.right ?? []).map(item => item.line_id).filter((value): value is string => !!value))];
  const command = useCommand<void, { run_id: string | null }>((_args, key) => {
    if (!scope) return Promise.reject(new Error("The current case scope could not be loaded."));
    return consignmentApi.decideCargo(scope.case_id, { ...scopePreconditions(scope), left_line_ids: left, right_line_ids: right,
      scope: { cargo_id: cargoId, leg_id: legId, quantity_kind: quantityKind, package_level: packageLevel || null, completeness, absolute_tolerance: tolerance || null }, basis, supersedes_id: row.group_id }, key);
  }, ["consignment", "review-view", "issues", "w-run", "m-runs"]);
  const supported = left.length && right.length && (left.length === 1 || right.length === 1);
  return <article className="rounded-ctl border border-line p-3 text-[12px]">
    <h3 className="font-medium">确认货物明细对应关系</h3>
    <p className="mt-1 text-ink-3">左侧 {left.length} 条明细 · 右侧 {right.length} 条明细</p>
    <div className="mt-2 flex flex-wrap gap-1.5">{row.sources.slice(0, 6).map((source, index) => <button key={index} type="button" className="btn btn-link btn-sm px-0" onClick={() => onLocate(row, source)}><UiText value={source.side === "left" ? "左侧来源" : "右侧来源"} /> {index + 1}</button>)}</div>
    <div className="mt-2 grid gap-2 sm:grid-cols-2">
      <label>所属货批<select className="input mt-1 w-full" value={cargoId} onChange={event => { command.renewKey(); setCargoId(event.target.value); }}><option value="">选择货批</option>{scope?.payload.cargo.map(cargo => <option key={cargo.id} value={cargo.id}>{cargo.label || cargo.id}</option>)}</select></label>
      <label>运输阶段<select className="input mt-1 w-full" value={legId} onChange={event => { command.renewKey(); setLegId(event.target.value); }}><option value="">选择运输阶段</option>{scope?.payload.legs.map(leg => <option key={leg.id} value={leg.id}>{[leg.origin, leg.destination].filter(Boolean).join(" → ") || leg.id}</option>)}</select></label>
      <label>核对量<select className="input mt-1 w-full" value={quantityKind} onChange={event => { command.renewKey(); setQuantityKind(event.target.value); }}><option value="quantity">数量</option><option value="package_count">包装件数</option><option value="gross_weight">毛重</option><option value="net_weight">净重</option><option value="chargeable_weight">计费重</option></select></label>
      <label>对应范围<select className="input mt-1 w-full" value={completeness} onChange={event => { command.renewKey(); setCompleteness(event.target.value as typeof completeness); }}><option value="unknown">尚不能确认</option><option value="partial">仅为部分货物</option><option value="complete">双方都覆盖所选货批与阶段的全部明细</option></select></label>
      {quantityKind === "package_count" && <label>包装层级<input className="input mt-1 w-full" value={packageLevel} onChange={event => { command.renewKey(); setPackageLevel(event.target.value); }} placeholder="例如箱、托盘" maxLength={40} /></label>}
      {["gross_weight", "net_weight", "chargeable_weight"].includes(quantityKind) && <label>允许误差<input className="input mt-1 w-full" inputMode="decimal" value={tolerance} onChange={event => { command.renewKey(); setTolerance(event.target.value); }} placeholder="使用明细中相同的计量单位" maxLength={30} /></label>}
    </div>
    <label className="mt-2 block">确认依据<textarea className="input mt-1 min-h-[64px] w-full" value={basis} onChange={event => { command.renewKey(); setBasis(event.target.value); }} placeholder="说明两侧为何是同批货物以及数量范围" maxLength={2000} /></label>
    {(!scope?.payload.cargo.length || !scope?.payload.legs.length) && <p className="mt-1 text-warn">请先在材料与核对范围中确认货批和运输阶段</p>}
    {!supported && <p className="mt-1 text-warn">多对多明细需要先拆成明确的对应组</p>}
    <button type="button" className="btn btn-secondary btn-sm mt-2" disabled={!editable || !supported || !cargoId || !legId || !basis.trim() || command.isPending || quantityKind === "package_count" && !packageLevel.trim() || ["gross_weight", "net_weight", "chargeable_weight"].includes(quantityKind) && !/^\d+(\.\d+)?$/.test(tolerance)} onClick={() => command.mutate(undefined, { onSuccess: result => onChanged(result.run_id) })}>{command.isPending ? "重核对中" : "确认对应并重新核对"}</button>
    {command.error && <div className="mt-2"><ErrorBar text={errorText(command.error)} /></div>}
  </article>;
}
