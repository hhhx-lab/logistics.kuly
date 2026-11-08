import { UiText } from "../../i18n/runtime";
import { useState } from "react";
import { consignmentApi, scopePreconditions, type ConsignmentScope, type ConsignmentScopePayload } from "../../api/consignment";
import type { ReviewView } from "../../api/types";
import { errorText } from "../../api/client";
import { ErrorBar } from "../../components/Status";
import { useCommand } from "../conversation/hooks";

export function ComparisonScopeEditor({ scope, view, onClose, onSaved }: { scope: ConsignmentScope; view: ReviewView; onClose: () => void; onSaved: () => void }) {
  const [baseScope] = useState(scope);
  const [payload, setPayload] = useState<ConsignmentScopePayload>(scope.payload);
  const excludedLogicalIds = new Set(view.files.filter(file => payload.materials.some(material => material.document_id === file.source.document_id && material.purpose === "excluded")).flatMap(file => file.logical_documents.map(logical => logical.logical_document_id)));
  const materialsChanged = payload.materials.some(material => baseScope.payload.materials.find(previous => previous.document_id === material.document_id)?.purpose !== material.purpose);
  const command = useCommand<void, unknown>((_args, key) => consignmentApi.reviseScope(view.case_id, { ...scopePreconditions(baseScope), payload: { ...payload, relation_ids: materialsChanged ? [] : payload.relation_ids, match_group_ids: materialsChanged ? [] : payload.match_group_ids, parties: payload.parties.map(item => ({...item,logical_document_ids:item.logical_document_ids.filter(id=>!excludedLogicalIds.has(id))})), legs: payload.legs.map(item => ({...item,logical_document_ids:item.logical_document_ids.filter(id=>!excludedLogicalIds.has(id))})), cargo: payload.cargo.map(item => ({...item,logical_document_ids:item.logical_document_ids.filter(id=>!excludedLogicalIds.has(id))})) } }, key), ["consignment", "review-view", "m-runs"]);
  const activeCount = payload.materials.filter(item => item.purpose !== "excluded").length;
  const logicals = view.files.filter(file => payload.materials.some(material => material.document_id === file.source.document_id && material.purpose !== "excluded")).flatMap(file => file.logical_documents.map(logical => ({id: logical.logical_document_id, label: `${file.source.filename} · ${logical.label}`})));
  const toggleLogical = (ids: string[], id: string) => ids.includes(id) ? ids.filter(value => value !== id) : [...ids, id];
  const partyRoles = [["seller","卖方"],["buyer","买方"],["shipper","发货人"],["consignee","收货人"],["notify_party","通知方"],["carrier","承运人"],["forwarder","货运代理"]] as const;
  return <fieldset disabled={command.isPending} className="mt-2 space-y-2 border-t border-line pt-2">
    <fieldset className="space-y-1"><legend className="mb-1 font-medium">参与核对的原件</legend>{payload.materials.map(material => <label key={material.document_id} className="flex items-start gap-2"><input type="checkbox" checked={material.purpose !== "excluded"} disabled={command.isPending} onChange={event => { command.renewKey(); setPayload(value => ({ ...value, materials: value.materials.map(item => item.document_id === material.document_id ? { ...item, purpose: event.target.checked ? "active" : "excluded" } : item) })); }} /><span className="break-words">{view.files.find(file => file.source.document_id === material.document_id)?.source.filename ?? "未加载的关联单据"} · v{material.document_version}</span></label>)}</fieldset>
    <label className="block">材料完整性<select className="input mt-1 w-full text-[12px]" value={payload.completeness} disabled={command.isPending} onChange={event => { command.renewKey(); setPayload(value => ({ ...value, completeness: event.target.value as ConsignmentScopePayload["completeness"] })); }}><option value="unknown">尚不能确认</option><option value="partial">仅为部分材料</option><option value="complete">已包含本次核对范围的全部材料</option></select></label>
    <details><summary className="cursor-pointer font-medium">货批与运输阶段</summary>
      <div className="mt-2 space-y-2">
        {payload.cargo.map((cargo, index) => <div key={cargo.id} className="rounded-ctl border border-line p-2">
          <label className="block">货批名称<input className="input mt-1 w-full" value={cargo.label} maxLength={300} onChange={event => { command.renewKey(); setPayload(value => ({...value,cargo: value.cargo.map(item => item.id === cargo.id ? {...item,label:event.target.value}:item)})); }} /></label>
          <fieldset className="mt-2 space-y-1"><legend>所属单据</legend>{logicals.map(logical => <label key={logical.id} className="flex gap-2"><input type="checkbox" checked={cargo.logical_document_ids.includes(logical.id)} onChange={() => { command.renewKey(); setPayload(value => ({...value,cargo:value.cargo.map(item => item.id === cargo.id ? {...item,logical_document_ids:toggleLogical(item.logical_document_ids,logical.id)}:item)})); }} /><span>{logical.label}</span></label>)}</fieldset>
          <button type="button" className="btn btn-ghost btn-sm mt-1 text-risk" onClick={() => {command.renewKey();setPayload(value=>({...value,cargo:value.cargo.filter((_,i)=>i!==index)}));}}>移除货批</button>
        </div>)}
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => {command.renewKey();setPayload(value=>({...value,cargo:[...value.cargo,{id:crypto.randomUUID(),label:"",logical_document_ids:[],evidence_ids:[]}]}));}}>添加货批</button>
        {payload.legs.map((leg,index) => <div key={leg.id} className="space-y-2 rounded-ctl border border-line p-2">
          <label className="block">运输方式<select className="input mt-1 w-full" value={leg.mode} onChange={event=>{command.renewKey();setPayload(value=>({...value,legs:value.legs.map(item=>item.id===leg.id?{...item,mode:event.target.value as typeof leg.mode}:item)}));}}>{([["unknown","尚不能确认"],["sea","海运"],["air","空运"],["road","公路"],["rail","铁路"],["courier","快递"]] as const).map(([id,label])=><option key={id} value={id}><UiText value={label}/></option>)}</select></label>
          <label className="block">阶段起点<input className="input mt-1 w-full" value={leg.origin??""} maxLength={300} onChange={event=>{command.renewKey();setPayload(value=>({...value,legs:value.legs.map(item=>item.id===leg.id?{...item,origin:event.target.value||null}:item)}));}}/></label>
          <label className="block">阶段终点<input className="input mt-1 w-full" value={leg.destination??""} maxLength={300} onChange={event=>{command.renewKey();setPayload(value=>({...value,legs:value.legs.map(item=>item.id===leg.id?{...item,destination:event.target.value||null}:item)}));}}/></label>
          <fieldset className="space-y-1"><legend>所属单据</legend>{logicals.map(logical=><label key={logical.id} className="flex gap-2"><input type="checkbox" checked={leg.logical_document_ids.includes(logical.id)} onChange={()=>{command.renewKey();setPayload(value=>({...value,legs:value.legs.map(item=>item.id===leg.id?{...item,logical_document_ids:toggleLogical(item.logical_document_ids,logical.id)}:item)}));}}/><span>{logical.label}</span></label>)}</fieldset>
          <button type="button" className="btn btn-ghost btn-sm text-risk" onClick={()=>{command.renewKey();setPayload(value=>({...value,legs:value.legs.filter((_,i)=>i!==index)}));}}>移除阶段</button>
        </div>)}
        <button type="button" className="btn btn-ghost btn-sm" onClick={()=>{command.renewKey();setPayload(value=>({...value,legs:[...value.legs,{id:crypto.randomUUID(),mode:"unknown",origin:null,destination:null,logical_document_ids:[],evidence_ids:[]}]}));}}>添加运输阶段</button>
      </div>
    </details>
    <details><summary className="cursor-pointer font-medium">同一角色主体</summary>
      <p className="mt-2 text-[12px] text-ink-2">仅选择已核对为同一主体、同一角色的单据。卖方、发货人和承运人分别声明。</p>
      <div className="mt-2 space-y-2">{payload.parties.map(party => <div key={party.id} className="space-y-2 rounded-ctl border border-line p-2">
        <label className="block">主体名称<input className="input mt-1 w-full" value={party.label} maxLength={300} onChange={event=>{command.renewKey();setPayload(value=>({...value,parties:value.parties.map(item=>item.id===party.id?{...item,label:event.target.value}:item)}));}}/></label>
        <label className="block">主体角色<select className="input mt-1 w-full" value={party.role} onChange={event=>{command.renewKey();setPayload(value=>({...value,parties:value.parties.map(item=>item.id===party.id?{...item,role:event.target.value}:item)}));}}>{partyRoles.map(([role,label])=><option key={role} value={role}><UiText value={label}/></option>)}</select></label>
        <fieldset className="space-y-1"><legend>同一主体对应单据</legend>{logicals.map(logical=><label key={logical.id} className="flex gap-2"><input type="checkbox" checked={party.logical_document_ids.includes(logical.id)} onChange={()=>{command.renewKey();setPayload(value=>({...value,parties:value.parties.map(item=>item.id===party.id?{...item,logical_document_ids:toggleLogical(item.logical_document_ids,logical.id)}:item)}));}}/><span>{logical.label}</span></label>)}</fieldset>
        <button type="button" className="btn btn-ghost btn-sm text-risk" onClick={()=>{command.renewKey();setPayload(value=>({...value,parties:value.parties.filter(item=>item.id!==party.id)}));}}>移除主体声明</button>
      </div>)}</div>
      <button type="button" className="btn btn-ghost btn-sm mt-2" onClick={()=>{command.renewKey();setPayload(value=>({...value,parties:[...value.parties,{id:crypto.randomUUID(),label:"",role:"consignee",logical_document_ids:[],evidence_ids:[]}]}));}}>添加主体声明</button>
    </details>
    <div className="flex gap-2"><button type="button" className="btn btn-secondary btn-sm" disabled={command.isPending || activeCount < 2 && logicals.length < 2 || payload.parties.some(party=>!party.label.trim() || party.logical_document_ids.length<2)} onClick={() => command.mutate(undefined, { onSuccess: () => { onSaved(); onClose(); } })}>{command.isPending ? "正在保存" : "保存范围"}</button><button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>取消</button></div>
    {activeCount < 2 && logicals.length < 2 && <p className="text-warn">至少选择两份同票单证才能比较</p>}
    {command.error && <ErrorBar text={errorText(command.error)} />}
  </fieldset>;
}
