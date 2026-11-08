import { useState } from "react";
import type { JurisdictionInputs, SourceVersion } from "../../api/types";
import type { CertificateInputs } from "../../api/generated/request-contracts";
import { ConsumerMaterialInputsForm, FrozenConsumerMaterials } from "./ConsumerMaterialInputsForm";

const COMMODITIES = [["fresh_apples", "鲜苹果"], ["bananas", "香蕉（五果例外）"], ["pineapples", "菠萝（五果例外）"], ["coconuts", "椰子（五果例外）"], ["durians", "榴莲（五果例外）"], ["dates", "椰枣（五果例外）"], ["other", "其他货物"]] as const;
const LABELS: Record<string, string> = { origin_country: "原产国", dispatch_country: "发运国", destination_country: "进口目的国", eu_plant_health_territory: "欧盟植物卫生适用领土", movement: "运输用途", commodity: "货物", stated_code: "原件注明 HS/CN", import_date: "进口日期", material_set_complete: "本任务材料已完整", certificate_source: "证书原件版本", certificate_same_consignment: "证书属于同票", declaration_reason: "确认依据" };

export function FrozenJurisdictionInputs({ value, sources, packVersion }: { value: JurisdictionInputs; sources: SourceVersion[]; packVersion?: string }) {
  if ("kind" in value) return <FrozenConsumerMaterials value={value} sources={sources} packVersion={packVersion} />;
  return <details className="mt-2 text-[12px] text-ink-2"><summary className="cursor-pointer text-brand-ink">查看已冻结的证书检查条件（试行）</summary>
    <p className="my-2">只判断材料提供情况；法律审定、证书真实性与海关合规未评估。</p>
    {packVersion && <p className="mb-2 break-words">冻结规则版本：{packVersion}</p>}
    <dl className="grid gap-1 sm:grid-cols-[160px_1fr]">{Object.entries(LABELS).map(([key, label]) => {
      const raw = value[key as keyof CertificateInputs];
      const display = key === "certificate_source" && value.certificate_source ? `${sources.find(s => s.document_id === value.certificate_source?.document_id)?.filename ?? value.certificate_source.document_id} v${value.certificate_source.document_version}` : key === "commodity" ? COMMODITIES.find(c => c[0] === raw)?.[1] ?? "未确认" : key === "movement" ? raw === "ordinary_import" ? "普通商业进口" : raw === "transit_or_special" ? "过境／特殊用途（未覆盖）" : "未确认" : typeof raw === "boolean" ? raw ? "已确认" : "否" : raw == null || raw === "" ? "未确认" : String(raw);
      return <div className="contents" key={key}><dt>{label}</dt><dd className="break-words">{display}</dd></div>;
    })}</dl></details>;
}

export function JurisdictionInputsForm({ value, sources, disabled, onSave, packVersion }: { value?: JurisdictionInputs | null; sources: SourceVersion[]; disabled: boolean; onSave: (value: JurisdictionInputs) => void; packVersion?: string }) {
  if (packVersion?.startsWith("cn-eu-consumer-materials@")) return <ConsumerMaterialInputsForm value={value && "kind" in value ? value : undefined} sources={sources} disabled={disabled} onSave={onSave} />;
  return <CertificateInputsForm value={value && !("kind" in value) ? value : undefined} sources={sources} disabled={disabled} onSave={onSave} />;
}

function CertificateInputsForm({ value, sources, disabled, onSave }: { value?: CertificateInputs; sources: SourceVersion[]; disabled: boolean; onSave: (value: JurisdictionInputs) => void }) {
  const [facts, setFacts] = useState<CertificateInputs>(value ?? {});
  const field = (key: keyof CertificateInputs, v: unknown) => setFacts(p => ({ ...p, [key]: v }));
  const boolField = (key: "eu_plant_health_territory" | "material_set_complete" | "certificate_same_consignment", label: string) => <label className="space-y-1">{label}<select className="input w-full" value={facts[key] == null ? "" : String(facts[key])} disabled={disabled} onChange={e => field(key, e.target.value === "" ? null : e.target.value === "true")}><option value="">未确认</option><option value="true">是</option><option value="false">否</option></select></label>;
  return <div className="mt-2 space-y-3 rounded-ctl border border-line p-3 text-[12px]">
    <p>试行范围：中国鲜苹果普通商业进口欧盟；进口日 2026-07-06 至 2026-10-14。只检查证书材料，法律审定待完成。</p>
    <div className="grid gap-3 sm:grid-cols-2">
      {(["origin_country", "dispatch_country", "destination_country"] as const).map(key => <label className="space-y-1" key={key}>{LABELS[key]}（ISO 二字码）<input className="input w-full" maxLength={2} value={facts[key] ?? ""} disabled={disabled} placeholder={key === "destination_country" ? "如 DE" : "如 CN"} onChange={e => field(key, e.target.value.toUpperCase() || null)} /></label>)}
      {boolField("eu_plant_health_territory", "目的地属于欧盟植物卫生适用领土（海外领土须另核）")}
      <label className="space-y-1">运输用途<select className="input w-full" value={facts.movement ?? ""} disabled={disabled} onChange={e => field("movement", e.target.value || null)}><option value="">未确认</option><option value="ordinary_import">普通商业进口</option><option value="transit_or_special">过境／科研等特殊用途</option></select></label>
      <label className="space-y-1">货物<select className="input w-full" value={facts.commodity ?? ""} disabled={disabled} onChange={e => field("commodity", e.target.value || null)}><option value="">未确认</option>{COMMODITIES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      <label className="space-y-1">原件注明 HS/CN（不自动归类）<input className="input w-full" maxLength={10} value={facts.stated_code ?? ""} disabled={disabled} placeholder="080810 / 08081010 / 08081080" onChange={e => field("stated_code", e.target.value || null)} /></label>
      <label className="space-y-1">预计／实际进口日期<input type="date" className="input w-full" value={facts.import_date ?? ""} disabled={disabled} onChange={e => field("import_date", e.target.value || null)} /></label>
      {boolField("material_set_complete", "本任务已选择全部本票证书材料（否则不判缺证）")}
      <label className="space-y-1">植物检疫证书材料（仅本任务所选原件）<select className="input w-full" value={facts.certificate_source?.document_id ?? ""} disabled={disabled} onChange={e => { const source = sources.find(s => s.document_id === e.target.value); field("certificate_source", source ? { document_id: source.document_id, document_version: source.document_version } : null); }}><option value="">未选／本次材料无证书</option>{sources.map(s => <option key={s.document_id} value={s.document_id}>{s.filename} v{s.document_version}</option>)}</select></label>
      {boolField("certificate_same_consignment", "已核对证书属于这票鲜苹果")}
    </div>
    <label className="block space-y-1">确认线路／货物／完整材料或证书同票的依据<textarea className="input w-full" maxLength={1000} value={facts.declaration_reason ?? ""} disabled={disabled} onChange={e => field("declaration_reason", e.target.value)} /></label>
    <button type="button" className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => onSave(facts)}>保存本次证书检查条件</button>
    <p className="text-ink-3">缺项将显示无法判断；五果例外只针对植物检疫证书要求；选证书后仍须实际解析到证书标题。不能据此判断真实性或通关。</p>
  </div>;
}
