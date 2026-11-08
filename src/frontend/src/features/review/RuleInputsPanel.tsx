import type { ApiResponses } from "../../api/generated/request-contracts";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { PackingTableInput, PartyMappingInput, RuleInputBody, TransportDateInput } from "../../api/generated/request-contracts";
import type { ReviewView } from "../../api/types";
import { api, errorText } from "../../api/client";
import { Dialog } from "../../components/Overlay";
import { ErrorBar, formatDateTime } from "../../components/Status";
import { useFields, useReviewCommand } from "./hooks";
import { useSession } from "../../stores/session";

const roles = [["seller","卖方 Seller"],["buyer","买方 Buyer"],["shipper","发货人 Shipper"],["consignee","收货人 Consignee"],["notify_party","通知方 Notify Party"]];
export function RuleInputsPanel({view}:{view:ReviewView}) {
  const generation=useSession(s=>s.generation);
  const [kind,setKind]=useState<RuleInputBody["kind"]|null>(null);
  const q=useQuery({queryKey:["rule-inputs",generation,view.case_id,view.business_revision_id,view.snapshot_id],queryFn:()=>api<ApiResponses["GET /api/v1/cases/{case_id}/rule-inputs"]["200"]>(`/cases/${view.case_id}/rule-inputs`,{query:{business_revision_id:view.business_revision_id??undefined,snapshot_id:view.snapshot_id??undefined}}),enabled:!!view.business_revision_id,retry:false});
  return <section className="card my-2 p-3 text-[12px]">
    <p className="font-medium text-ink">规则适用事实与人工依据</p><p className="mt-1 text-ink-2">主体映射、完整同层包装表及同一运输事件需要明确确认。保存会形成业务新版，相关检查和译稿等待重算；旧规则版本不能添加新算法。</p>
    {view.mode==="working"&&view.run_id&&view.allowed_actions.some(action=>["revise_field", "field_revise"].includes(action))&&<div className="mt-2 flex flex-wrap gap-2">{([["party_mapping","确认同主体角色"],["packing_table","确认包装表口径"],["date_transport","确认运输日期关系"]] as const).map(([value,label])=><button key={value} className="btn btn-secondary btn-sm" onClick={()=>setKind(value)}>{label}</button>)}</div>}
    {q.error&&<ErrorBar text={errorText(q.error)} action={{label:"重读",onSelect:()=>void q.refetch()}}/>}
    {q.data?.items.map(d=><details key={d.decision_id} className="mt-2"><summary>{d.scope_key} · {d.kind} · {formatDateTime(d.created_at)}</summary><p>确认人：{d.decided_by}</p><p>依据：{d.basis}</p><p>理由：{d.reason}</p></details>)}
    {kind&&<RuleInputForm view={view} kind={kind} onClose={()=>setKind(null)}/>}
  </section>;
}
function RuleInputForm({view,kind,onClose}:{view:ReviewView;kind:RuleInputBody["kind"];onClose:()=>void}) {
  const fields=useFields(view);const command=useReviewCommand((body:RuleInputBody,key:string)=>api<ApiResponses["POST /api/v1/cases/{case_id}/rule-inputs"]["200"]>(`/cases/${view.case_id}/rule-inputs`,{method:"POST",body,idempotencyKey:key}));
  const logicals=view.files.flatMap(file=>file.logical_documents.map(logical=>({id:logical.logical_document_id,label:`${file.source.filename} / ${logical.label}`,sections:logical.sections})));
  const [members,setMembers]=useState<PartyMappingInput["members"]>([{logical_document_id:logicals[0]?.id??"",role:"seller"},{logical_document_id:logicals[1]?.id??logicals[0]?.id??"",role:"shipper"}]);
  const [compare,setCompare]=useState<PartyMappingInput["compare_fields"]>(["name"]);
  const [logical,setLogical]=useState(logicals[0]?.id??"");const [section,setSection]=useState("");
  const [description,setDescription]=useState(0);const [count,setCount]=useState(1);const [header,setHeader]=useState(0);const [total,setTotal]=useState(2);const [level,setLevel]=useState<PackingTableInput["packaging_level"]>("carton");
  const [proof,setProof]=useState<{id:string;text:string}[]>([]);const [chosen,setChosen]=useState<string[]>([]);const [complete,setComplete]=useState(false);
  const [departure,setDeparture]=useState("");const [arrival,setArrival]=useState("");const [timezone,setTimezone]=useState("Asia/Shanghai");
  const proofGeneration=useRef(0);
  const [proofLoading,setProofLoading]=useState(false);
  useEffect(()=>{ proofGeneration.current++; setProofLoading(false); setProof([]); setChosen([]); setComplete(false); return ()=>{proofGeneration.current++;}; },[logical,section,view.business_revision_id,view.review_state_version]);
  const [scope,setScope]=useState("");const [basis,setBasis]=useState("");const [reason,setReason]=useState("");const [error,setError]=useState<string|null>(null);
  const loadProof=async()=>{
    const current=++proofGeneration.current;
    const identity=useSession.getState().generation;
    const active=()=>current===proofGeneration.current&&identity===useSession.getState().generation;
    setProofLoading(true); setError(null); setProof([]); setChosen([]); setComplete(false);
    try{
      const file=view.files.find(f=>f.logical_documents.some(l=>l.logical_document_id===logical));
      if(!file?.parse_artifact_id||!section)throw new Error("先选择有解析的包装表区段");
      const rows:{id:string;text:string}[]=[]; let cursor:string|undefined;
      do{
        const result=await api<ApiResponses["GET /api/v1/parse-artifacts/{parse_artifact_id}/reading-view"]["200"]>(`/parse-artifacts/${file.parse_artifact_id}/reading-view`,{query:{section_id:section,limit:500,cursor}});
        if(!active())return;
        rows.push(...result.items.map(row=>({id:row.source_segment_id,text:row.text})));
        cursor=result.next_cursor??undefined;
      }while(cursor);
      setProof(rows);
    }catch(e){if(active())setError(errorText(e));}
    finally{if(active())setProofLoading(false);}
  };
  const save=async()=>{setError(null);let payload:RuleInputBody["payload"];if(kind==="party_mapping")payload={members,compare_fields:compare};else if(kind==="packing_table")payload={logical_document_id:logical,section_id:section,description_column:description,count_column:count,header_row:header,total_row:total,packaging_level:level,scope_source_segment_ids:chosen,complete:true};else payload={departure_field_id:departure,arrival_field_id:arrival,timezone,calendar:"gregorian"} satisfies TransportDateInput;
    try{await command.mutateAsync({run_id:view.run_id!,expected_business_revision_id:view.business_revision_id!,expected_business_revision_no:view.business_revision_no,expected_review_state_version:view.review_state_version!,kind,scope_key:scope.trim(),payload,basis:basis.trim(),reason:reason.trim()});onClose();}catch(e){setError(errorText(e));}};
  const valid=kind==="party_mapping"?members.every(m=>m.logical_document_id)&&compare.length>0:kind==="packing_table"?!!section&&chosen.length>0&&complete:!!departure&&!!arrival&&!!timezone;
  return <Dialog open title="确认规则适用事实" onClose={onClose} width={720} footer={<><button className="btn btn-secondary" onClick={onClose}>取消</button><button className="btn btn-primary" disabled={!valid||!scope.trim()||!basis.trim()||!reason.trim()||command.isPending} onClick={()=>void save()}>保存依据并重算</button></>}><div className="space-y-3 p-5 text-[13px]">
    {kind==="party_mapping"&&<><p>确认下列角色在本次业务中属于同一主体；不同角色不会自动合并。姓名字段必须有唯一原文来源。</p>{members.map((member,index)=><div key={index} className="flex gap-2"><select aria-label={`角色${index+1}所在单据`} className="input min-w-0 flex-1" value={member.logical_document_id} onChange={e=>setMembers(ms=>ms.map((m,i)=>i===index?{...m,logical_document_id:e.target.value}:m))}>{logicals.map(l=><option key={l.id} value={l.id}>{l.label}</option>)}</select><select aria-label={`主体角色${index+1}`} className="input" value={member.role} onChange={e=>setMembers(ms=>ms.map((m,i)=>i===index?{...m,role:e.target.value as typeof member.role}:m))}>{roles.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select><button className="btn btn-ghost" disabled={members.length<=2} onClick={()=>setMembers(ms=>ms.filter((_,i)=>i!==index))}>移除</button></div>)}<button className="btn btn-secondary btn-sm" disabled={members.length>=10} onClick={()=>setMembers(ms=>[...ms,{logical_document_id:logicals[0]?.id??"",role:"consignee"}])}>增加角色</button><div className="flex gap-3">{([["name","名称"],["address","地址"],["company_no","公司编号"]] as const).map(([value,label])=><label key={value}><input type="checkbox" checked={compare.includes(value)} onChange={e=>setCompare(old=>e.target.checked?[...old,value]:old.filter(v=>v!==value))}/>{label}</label>)}</div></>}
    {kind==="packing_table"&&<><p>只核对原文明确完整、连续且同一包装层级的表；混装、多层汇总或缺失来源不能自动通过。下列行列序号从0开始。</p><select aria-label="包装单据" className="input w-full" value={logical} onChange={e=>{proofGeneration.current++;setLogical(e.target.value);setSection("");setProofLoading(false);setProof([]);setChosen([]);setComplete(false);}}>{logicals.map(l=><option key={l.id} value={l.id}>{l.label}</option>)}</select><select aria-label="包装表区段" className="input w-full" value={section} onChange={e=>{proofGeneration.current++;setSection(e.target.value);setProofLoading(false);setProof([]);setChosen([]);setComplete(false);}}><option value="">选择表格区段</option>{logicals.find(l=>l.id===logical)?.sections.map(s=><option key={s.section_id} value={s.section_id}>{s.title}</option>)}</select><div className="grid grid-cols-2 gap-2">{([["货描列",description,setDescription],["数量列",count,setCount],["表头行",header,setHeader],["总计行",total,setTotal]] as const).map(([label,value,set])=><label key={label}>{label}<input className="input" type="number" min={0} value={value} onChange={e=>set(Number(e.target.value))}/></label>)}</div><label>包装层级<select className="input" value={level} onChange={e=>setLevel(e.target.value as typeof level)}><option value="carton">箱 carton</option><option value="pallet">托 pallet</option><option value="piece">件 piece</option></select></label><button className="btn btn-secondary" disabled={!section||proofLoading} onClick={()=>void loadProof()}>{proofLoading?"正在加载原文依据":"加载完整范围的原文依据"}</button><div className="max-h-44 overflow-auto">{proof.map(row=><label key={row.id} className="block"><input type="checkbox" checked={chosen.includes(row.id)} disabled={!chosen.includes(row.id)&&chosen.length>=10} onChange={e=>setChosen(old=>e.target.checked?[...old,row.id]:old.filter(id=>id!==row.id))}/>{row.text}</label>)}</div><label className="block"><input type="checkbox" checked={complete} disabled={proofLoading||!proof.length} onChange={e=>setComplete(e.target.checked)}/>已核对原文明确说明完整本票、同层级包装范围</label></>}
    {kind==="date_transport"&&<><p>确认离港／到港字段属于同一运输事件、同一公历与当地时区；未知用途不会按日期字符串推断。</p>{([["离港日期",departure,setDeparture],["到港日期",arrival,setArrival]] as const).map(([label,value,set])=><label key={label} className="block">{label}<select className="input w-full" value={value} onChange={e=>set(e.target.value)}><option value="">选择对应日期字段</option>{fields.items.filter(field=>field.name===(label==="离港日期"?"离港日期":"到港日期")).map(field=><option key={field.field_id} value={field.field_id}>{field.name}：{field.normalized_value} · {logicals.find(l=>l.id===field.logical_document_id)?.label}</option>)}</select></label>)}{fields.hasNextPage&&<button className="btn btn-secondary" onClick={()=>void fields.fetchNextPage()}>加载更多字段</button>}<label className="block">当地IANA时区<input className="input w-full" value={timezone} onChange={e=>setTimezone(e.target.value)}/></label></>}
    <label className="block">业务比较范围<input className="input w-full" value={scope} onChange={e=>setScope(e.target.value)} placeholder="同一主体／包装表／运输事件名称"/></label><label className="block">可核验依据<textarea className="input w-full" rows={3} value={basis} onChange={e=>setBasis(e.target.value)}/></label><label className="block">确认理由<textarea className="input w-full" value={reason} onChange={e=>setReason(e.target.value)}/></label>{error&&<ErrorBar text={error}/>}
  </div></Dialog>;
}
