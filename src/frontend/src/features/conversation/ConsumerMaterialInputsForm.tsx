import { useState } from "react";
import type { ConsumerMaterialInputs, ConsignmentMaterial } from "../../api/generated/request-contracts";
import type { SourceVersion } from "../../api/types";

const KINDS = [["commercial_invoice", "商业发票"], ["packing_list", "装箱单"], ["transport_document", "运输文件（提单／运单）"]] as const;
type Kind = ConsignmentMaterial["kind"];
const BOOLS = [["eu_customs_territory", "目的地属于欧盟海关适用领土（海外领土需另核）"], ["general_consumer_goods", "货物属于一般消费品"], ["non_dangerous", "本票货物不属于危险品"], ["material_set_complete", "本任务所选原件已包含全部本票材料"]] as const;

function sourceLabel(ref: ConsignmentMaterial["source"], sources: SourceVersion[]) {
  return `${sources.find(source => source.document_id === ref.document_id && source.document_version === ref.document_version)?.filename ?? ref.document_id} v${ref.document_version}`;
}

export function FrozenConsumerMaterials({ value, sources, packVersion }: { value: ConsumerMaterialInputs; sources: SourceVersion[]; packVersion?: string }) {
  return <details className="mt-2 text-[12px] text-ink-2">
    <summary className="cursor-pointer text-brand-ink">查看已冻结的本票候选材料检查条件（试行）</summary>
    <p className="my-2">法律审定待完成；真实性、HS、专项证书与海关合规未评估。只核本次材料提供与用户明确业务要求。</p>
    <p className="break-words">版本：{packVersion}；发运国 {value.dispatch_country ?? "未确认"} → 进口目的国 {value.destination_country ?? "未确认"}</p>
    <dl className="mt-2 space-y-1">{BOOLS.map(([key, label]) => <div key={key}><dt className="inline">{label}：</dt><dd className="inline">{value[key] == null ? "未确认" : value[key] ? "是" : "否"}</dd></div>)}</dl>
    <p className="mt-2 whitespace-pre-wrap break-words">线路与货物确认依据：{value.declaration_reason || "未填写"}</p>
    <ul className="mt-2 space-y-2">{KINDS.map(([kind, label]) => {
      const material = value.materials?.find(item => item.kind === kind);
      return <li className="rounded-ctl border border-line p-2" key={kind}><p>{label}：{material ? sourceLabel(material.source, sources) : "未选择"}</p>
        <p>同票确认：{material?.same_consignment == null ? "未确认" : material.same_consignment ? "是" : "否"}；依据：{material?.basis || "未填写"}</p>
        <p>用户声明本票业务必需：{value.required_materials?.includes(kind) ? "是" : "否／未声明"}</p></li>;
    })}</ul>
    <p className="mt-2 whitespace-pre-wrap break-words">业务必需清单依据：{value.required_materials_basis || "未填写；不产生缺材料阻断"}</p>
  </details>;
}

export function ConsumerMaterialInputsForm({ value, sources, disabled, onSave }: { value?: ConsumerMaterialInputs; sources: SourceVersion[]; disabled: boolean; onSave: (value: ConsumerMaterialInputs) => void }) {
  const [facts, setFacts] = useState<ConsumerMaterialInputs>(value ?? { kind: "consumer_material_check" });
  const updateMaterial = (kind: Kind, patch: Partial<ConsignmentMaterial>) => setFacts(current => ({ ...current, materials: (current.materials ?? []).map(item => item.kind === kind ? { ...item, ...patch } : item) }));
  return <details className="mt-2 rounded-ctl border border-line p-3 text-[12px]" open>
    <summary className="cursor-pointer font-medium text-ink">本票候选材料核对条件（可折叠）</summary>
    <p className="mt-2 text-ink-3">一般翻译无需启用本包。不明确的输入保持“未确认”，不会因未填法规条件阻止翻译启动。发运国与原产国不同，本包不推断原产地。</p>
    <div className="mt-3 grid gap-3 sm:grid-cols-2">
      {([["dispatch_country", "发运国", "如 CN"], ["destination_country", "进口目的国", "如 DE"]] as const).map(([key, label, placeholder]) => <label key={key} className="space-y-1">{label}（ISO 二字码）<input className="input w-full" aria-label={label} maxLength={2} disabled={disabled} value={facts[key] ?? ""} placeholder={placeholder} onChange={event => setFacts(current => ({ ...current, [key]: event.target.value.toUpperCase() || null }))} /></label>)}
      {BOOLS.map(([key, label]) => <label className="space-y-1" key={key}>{label}<select className="input w-full" aria-label={label} disabled={disabled} value={facts[key] == null ? "" : String(facts[key])} onChange={event => setFacts(current => ({ ...current, [key]: event.target.value === "" ? null : event.target.value === "true" }))}><option value="">未确认</option><option value="true">是</option><option value="false">否</option></select></label>)}
    </div>
    <label className="mt-3 block space-y-1">确认线路、一般消费品与非危险属性的依据<textarea className="input w-full" disabled={disabled} maxLength={1000} value={facts.declaration_reason ?? ""} onChange={event => setFacts(current => ({ ...current, declaration_reason: event.target.value }))} placeholder="依据本票委托、货物说明等；不填写法律审定结论" /></label>
    <div className="mt-3 space-y-3">{KINDS.map(([kind, label]) => {
      const material = facts.materials?.find(item => item.kind === kind);
      return <fieldset className="space-y-2 rounded-ctl border border-line p-2" key={kind} disabled={disabled}>
        <legend className="px-1 font-medium">{label}</legend>
        <label className="block space-y-1">当前任务所选原件版本<select className="input w-full" aria-label={`${label}原件`} value={material ? `${material.source.document_id}:${material.source.document_version}` : ""} onChange={event => {
          const source = sources.find(item => `${item.document_id}:${item.document_version}` === event.target.value);
          setFacts(current => ({ ...current, materials: [...(current.materials ?? []).filter(item => item.kind !== kind), ...(source ? [{ kind, source: { document_id: source.document_id, document_version: source.document_version }, same_consignment: null, basis: "" }] : [])] }));
        }}><option value="">未选／本次材料无此文件</option>{sources.map(source => <option key={source.document_id} value={`${source.document_id}:${source.document_version}`}>{source.filename} v{source.document_version}</option>)}</select></label>
        {material && <>
          <label className="block space-y-1">已核对属于本票<select className="input w-full" value={material.same_consignment == null ? "" : String(material.same_consignment)} onChange={event => updateMaterial(kind, { same_consignment: event.target.value === "" ? null : event.target.value === "true" })}><option value="">未确认</option><option value="true">是</option><option value="false">否</option></select></label>
          <label className="block space-y-1">同票核对依据<textarea className="input w-full" maxLength={1000} value={material.basis ?? ""} onChange={event => updateMaterial(kind, { basis: event.target.value })} placeholder="写明本票编号、关联单号或已核对的业务依据；不要只填文件名" /></label>
        </>}
        <label className="flex items-center gap-2"><input type="checkbox" checked={facts.required_materials?.includes(kind) ?? false} onChange={event => setFacts(current => ({ ...current, required_materials: event.target.checked ? [...(current.required_materials ?? []), kind] : (current.required_materials ?? []).filter(item => item !== kind) }))} />我明确本票业务要求必须提供此材料（不代表法律义务）</label>
      </fieldset>;
    })}</div>
    <label className="mt-3 block space-y-1">本票业务必需材料清单依据<textarea className="input w-full" disabled={disabled} maxLength={1000} value={facts.required_materials_basis ?? ""} onChange={event => setFacts(current => ({ ...current, required_materials_basis: event.target.value }))} placeholder="仅勾选本票必需时填写合同、客户委托或业务约定；无依据时缺件仍为黄色提示" /></label>
    <button type="button" className="btn btn-secondary btn-sm mt-3" disabled={disabled} onClick={() => onSave(facts)}>保存本票材料检查条件</button>
    <p className="mt-2 text-ink-3">解析后须有直接文件标题来源及同票确认才能视为已提供。默认缺件提示不阻断；只有已声明完整、可靠解析且有依据的本票必需材料缺失才阻断。货类专证、HS、CE与通关结论未覆盖。</p>
  </details>;
}
