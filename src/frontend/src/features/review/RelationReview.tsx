import { useState } from "react";
import type { ConsignmentScope, RelationCandidate } from "../../api/consignment";
import { consignmentApi, scopePreconditions } from "../../api/consignment";
import type { ReviewView } from "../../api/types";
import { errorText } from "../../api/client";
import { UiText } from "../../i18n/runtime";
import { useCommand } from "../conversation/hooks";
import { ErrorBar, StatusPill } from "../../components/Status";

const RELATIONS: Record<string, string> = { identical_file: "同一原件", document_revision: "单据修订版本", language_version: "不同语言版本", attachment: "补充附件", explicit_reference: "单据明确引用", master_house: "主单与分单", same_goods: "同批货物", transport_handover: "运输阶段衔接", same_party: "同一主体" };
export function RelationReview({ items, view, scope, editable, onChanged }: { items: RelationCandidate[]; view: ReviewView; scope: ConsignmentScope | undefined; editable: boolean; onChanged: (runId: string | null) => void }) {
  return <div className="space-y-2 p-3">{items.length ? items.map(item => <RelationCard key={item.id} item={item} view={view} scope={scope} editable={editable} onChanged={onChanged} />) : <p className="text-[12px] text-ink-3">没有待确认的关联关系</p>}</div>;
}
function RelationCard({ item, view, scope, editable, onChanged }: { item: RelationCandidate; view: ReviewView; scope: ConsignmentScope | undefined; editable: boolean; onChanged: (runId: string | null) => void }) {
  const [correcting, setCorrecting] = useState(false);
  const [kind, setKind] = useState(item.kind);
  const [reason, setReason] = useState("");
  const [lastIntent, setLastIntent] = useState<string | null>(null);
  const command = useCommand<{ decision: "confirm" | "reject" | "correct"; kind?: string; reason?: string }, { run_id: string | null }>((args, key) => {
    if (!scope) return Promise.reject(new Error("The current case scope could not be loaded."));
    return consignmentApi.decideRelation(view.case_id, { ...scopePreconditions(scope), relation_id: item.id, ...args }, key);
  }, ["consignment", "review-view", "issues", "w-run", "c-conv", "m-runs"]);
  const send = (decision: "confirm" | "reject" | "correct") => {
    const intent = JSON.stringify([decision, kind, reason]);
    if (intent !== lastIntent) command.renewKey();
    setLastIntent(intent);
    command.mutate({ decision, ...(decision === "correct" ? { kind } : {}), reason }, { onSuccess: result => { setCorrecting(false); onChanged(result.run_id); } });
  };
  const logicalName = (id: string) => {
    for (const file of view.files) {
      const logical = file.logical_documents.find(value => value.logical_document_id === id);
      if (logical) return `${file.source.filename} · ${logical.label}`;
    }
    return "未加载的关联单据";
  };
  const evidence = Object.entries(item.basis).filter(([key, value]) => ["description", "reason", "support", "opposition", "supporting_evidence", "conflicting_evidence"].includes(key) && (typeof value === "string" || Array.isArray(value)));
  return <article className="rounded-ctl border border-line p-3 text-[12px]">
    <div className="flex items-center gap-2"><p className="min-w-0 flex-1 font-medium"><UiText value={RELATIONS[item.kind] ?? "关系待确认"} /></p><StatusPill tone="info">待关联</StatusPill></div>
    <p className="mt-2 break-words text-ink-2">{logicalName(item.left_logical_id)}</p><p className="mt-1 break-words text-ink-2">{logicalName(item.right_logical_id)}</p>
    {evidence.map(([key, value]) => <p key={key} className="mt-2 whitespace-pre-wrap text-ink-3"><UiText value={key.includes("opposition") || key.includes("conflicting") ? "反对依据" : "关联依据"} />：{typeof value === "string" ? <UiText value={value} /> : (value as unknown[]).filter(value => typeof value === "string").join("；")}</p>)}
    {correcting && <div className="mt-2 space-y-2"><label className="block">对应关系<select className="input mt-1 w-full" value={kind} onChange={event => setKind(event.target.value)}>{Object.entries(RELATIONS).map(([key, label]) => <option key={key} value={key}><UiText value={label} /></option>)}</select></label><label className="block">范围说明<textarea className="input mt-1 min-h-[72px] w-full" value={reason} onChange={event => setReason(event.target.value)} placeholder="说明这两份单据对应的货物或运输阶段" maxLength={2000} /></label></div>}
    <div className="mt-3 flex flex-wrap gap-1.5">
      <button type="button" className="btn btn-secondary btn-sm" disabled={!editable || !scope || command.isPending} onClick={() => send("confirm")}>确认关系</button>
      <button type="button" className="btn btn-ghost btn-sm" disabled={!editable || !scope || command.isPending} onClick={() => send("reject")}>不是同一对象</button>
      <button type="button" className="btn btn-ghost btn-sm" disabled={!editable || !scope || command.isPending} onClick={() => correcting ? send("correct") : setCorrecting(true)}>{correcting ? "保存对应范围" : "修改对应范围"}</button>
      {correcting && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setCorrecting(false)}>取消</button>}
    </div>
    {command.error && <div className="mt-2"><ErrorBar text={errorText(command.error)} /></div>}
  </article>;
}
