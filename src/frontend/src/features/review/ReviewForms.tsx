import { UiText } from "../../i18n/runtime";
import { LANGUAGES, languageCode, languageNativeName, languageName } from "../../i18n/languages";
// 修订、裁定与处置表单。壳只管焦点、未保存草稿、版本基线、幂等键与提交反馈；
// 原文来源只读，409 进入差异界面并保留草稿，不做最后写入覆盖。
import { useMemo, useState, type ReactNode } from "react";
import type { ApiRequests } from "../../api/generated/request-contracts";
import { useSession } from "../../stores/session";
import { errorText, isApiError } from "../../api/client";
import { reviewApi } from "../../api/review";
import { workflowApi } from "../../api/workflow";
import { useDocumentCatalog } from "../../api/documentCatalog";
import { CargoProfileSelect, DocumentTypeSelect } from "../../components/DocumentTypeControls";
import type { IssueView, ReviewView, TranslationSegmentView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, Loading, StatusPill , shortId } from "../../components/Status";
import { useReviewCommand } from "./hooks";
import { useCommand } from "../conversation/hooks";
import { fieldDisplayName, type FieldAction } from "./FieldsPanel";
import type { IssueDisposition } from "./IssuesPanel";
import { InputRequestEvidence } from "../conversation/InputRequestEvidence";

const UNKNOWN_REASONS = [
  { id: "unreadable", label: "原件不可读" },
  { id: "multiple", label: "存在多个候选" },
  { id: "no_basis", label: "缺少判断依据" },
  { id: "other", label: "其他" },
];

/** 版本冲突：并排展示服务端当前值与你的草稿，由你决定如何处理，绝不做最后写入覆盖。 */
function ConflictPanel({
  current,
  draft,
  draftLabel = "你的草稿",
  onReload,
  onCopy,
}: {
  current: Record<string, unknown> | null;
  draft?: string;
  draftLabel?: string;
  onReload: () => void;
  onCopy: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const serverText = current
    ? Object.entries(current)
        .map(([k, v]) => `${k}: ${String(v)}`)
        .join("\n")
    : "服务端未给出具体差异字段";
  return (
    <div className="mt-3 rounded-ctl border border-warn-line bg-warn-bg p-3 text-[12px] text-warn">
      <p className="font-medium">你在编辑期间，这里的版本已经变化</p>
      <p className="mt-1 leading-relaxed">为避免覆盖别人的改动，这次没有保存。你的输入仍然保留在下方，可以复制后重新加载再提交。</p>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <div className="rounded-ctl border border-line bg-card p-2">
          <p className="mb-1 text-[11px] font-medium text-ink-2">服务端当前值</p>
          <pre className="mono whitespace-pre-wrap break-words-all text-[11.5px] leading-relaxed text-ink">{serverText}</pre>
        </div>
        <div className="rounded-ctl border border-brand-line bg-card p-2">
          <p className="mb-1 text-[11px] font-medium text-brand-ink">{draftLabel}</p>
          <pre className="mono whitespace-pre-wrap break-words-all text-[11.5px] leading-relaxed text-ink">{draft?.trim() || "（本次没有文本输入）"}</pre>
        </div>
      </div>
      <div className="mt-2 flex gap-2">
        <button type="button" className="btn btn-secondary btn-sm" onClick={onReload}>
          重新读取当前值
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => {
            onCopy();
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2000);
          }}
        >
          {copied ? "已复制草稿" : "复制我的草稿"}
        </button>
      </div>
    </div>
  );
}

function ReadOnlySource({ label, value, extra }: { label: string; value: string; extra?: ReactNode }) {
  return (
    <div className="rounded-ctl border border-line bg-page p-2.5">
      <p className="text-[11px] text-ink-3">{label}</p>
      <p className="num mt-0.5 break-words-all text-[13px] text-ink">{value || "未提取"}</p>
      {extra}
    </div>
  );
}

// ---- 字段命令 ----
export function FieldForm({ action, view, onClose }: { action: FieldAction; view: ReviewView; onClose: () => void }) {
  const { field, kind } = action;
  const toast = useToast();
  const [value, setValue] = useState(field.adopted_value ?? field.normalized_value);
  const [unit, setUnit] = useState(field.unit ?? "");
  const [reason, setReason] = useState("");
  const [basis, setBasis] = useState("");
  const [why, setWhy] = useState(UNKNOWN_REASONS[0].id);
  const [needed, setNeeded] = useState("");
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);
  // 打开表单时冻结所见版本作为提交前置条件
  const [base] = useState(() => ({ business_revision_no: view.business_revision_no, business_revision_id: view.business_revision_id, parse_artifact_id: field.target.parse_artifact_id }));

  const cmd = useReviewCommand<Record<string, unknown>, unknown>((body, key) => reviewApi.fieldAction(view.case_id, field.field_id, kind, body, key));

  const title = kind === "confirm" ? "确认此字段无误" : kind === "revise" ? "修订识别值" : "标记无法判断";
  const permission = kind === "confirm" ? "confirm_field" : kind === "revise" ? "revise_field" : "mark_field_unknown";
  const canSave = view.allowed_actions.includes(permission) && (kind === "confirm" ? !field.confirm_blocked_reason : kind === "revise" ? value.trim().length > 0 && reason.trim().length > 0 : needed.trim().length > 0 || why !== "other");

  const submit = () => {
    setConflict(null);
    const body: Record<string, unknown> = {
      expected_business_revision_no: base.business_revision_no,
      expected_business_revision_id: base.business_revision_id,
      expected_parse_artifact_id: base.parse_artifact_id,
    };
    if (kind === "revise") Object.assign(body, { value: value.trim(), unit: unit.trim() || undefined, reason: reason.trim(), basis: basis.trim() || undefined });
    if (kind === "confirm") Object.assign(body, { reason: reason.trim() || "与原件一致" });
    if (kind === "mark-unknown") Object.assign(body, { reason: `${UNKNOWN_REASONS.find((r) => r.id === why)?.label}：${needed.trim() || "需补充材料"}`, required_materials: needed.trim() });
    cmd.mutate(body, {
      onSuccess: () => {
        toast({ tone: "ok", text: kind === "revise" ? `已修订「${field.name}」，相关译文与预览等待重算` : kind === "confirm" ? `已记录你对「${field.name}」的确认` : `已把「${field.name}」标为无法判断` });
        onClose();
      },
      onError: (e) => {
        if (isApiError(e) && e.status === 409) setConflict(e.current);
      },
    });
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={`${title} · ${fieldDisplayName(field)}`}
      description={`${view.case_name} · 业务版 ${view.business_revision_no ?? "—"}｜原文与来源只读，保存只追加记录，不改写原件。`}
      width={720}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            返回
          </button>
          <button type="button" className="btn btn-primary" onClick={submit} disabled={!canSave || cmd.isPending}>
            {cmd.isPending ? "正在保存" : kind === "confirm" ? "确认此字段" : kind === "revise" ? "保存修订" : "保存状态"}
          </button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <ReadOnlySource label={`原文（${field.role ?? field.name}）`} value={field.original_text} extra={<p className="mt-1 text-[11px] text-ink-3">原件 v{field.target.document_version} · 解析 {field.target.parse_artifact_id ? shortId(field.target.parse_artifact_id) : "—"}</p>} />
        <ReadOnlySource label="当前规范值" value={`${field.normalized_value}${field.unit ? ` ${field.unit}` : ""}`} />
      </div>

      {kind === "revise" && (
        <div className="mt-3 space-y-3">
          <label className="block">
            <span className="label">复核后采用的值</span>
            <input className="input mt-1" value={value} onChange={(e) => setValue(e.target.value)} data-autofocus />
          </label>
          <label className="block">
            <span className="label">单位（可留空）</span>
            <input className="input mt-1" value={unit} onChange={(e) => setUnit(e.target.value)} />
          </label>
          <label className="block">
            <span className="label">修订理由（必填）</span>
            <textarea className="input mt-1" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例如：订舱确认书写明实际收货地" />
          </label>
          <label className="block">
            <span className="label">可核查的补充依据</span>
            <input className="input mt-1" value={basis} onChange={(e) => setBasis(e.target.value)} placeholder="例如：订舱确认书 BK-2409-118" />
          </label>
        </div>
      )}

      {kind === "confirm" && (
        <div className="mt-3">
          <p className="rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
            确认只记录你已核对该字段及所见版本，不改写原文或采用值，也不会解除疑点的交付阻断。
          </p>
          <label className="mt-3 block">
            <span className="label">备注（可留空）</span>
            <input className="input mt-1" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="与原件一致" data-autofocus />
          </label>
        </div>
      )}

      {kind === "mark-unknown" && (
        <div className="mt-3 space-y-3">
          <fieldset>
            <legend className="label mb-1">原因</legend>
            <div className="flex flex-wrap gap-2">
              {UNKNOWN_REASONS.map((r) => (
                <label key={r.id} className={`tag cursor-pointer ${why === r.id ? "tag-on" : "border-line"}`}>
                  <input type="radio" name="unknown-reason" className="sr-only" checked={why === r.id} onChange={() => setWhy(r.id)} />
                  {r.label}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block">
            <span className="label">说明与需要的材料</span>
            <textarea className="input mt-1" rows={2} value={needed} onChange={(e) => setNeeded(e.target.value)} placeholder="例如：提单该处被印章遮挡，需要清晰扫描件" data-autofocus />
          </label>
          <p className="rounded-ctl bg-warn-bg px-3 py-2 text-[12px] text-warn">
            <Icon name="warn" size={14} className="mr-1 inline align-[-2px]" />
            标记无法判断不等于通过；是否阻断交付仍由适用规则与翻译所需信息决定。
          </p>
        </div>
      )}

      {cmd.error && !conflict && <div className="mt-3"><ErrorBar text={errorText(cmd.error)} /></div>}
      {conflict && <ConflictPanel current={conflict} draft={kind === "revise" ? `采用值：${value}${unit ? ` ${unit}` : ""}\n理由：${reason}${basis ? `\n依据：${basis}` : ""}` : reason} draftLabel="你填写的内容" onReload={onClose} onCopy={() => void navigator.clipboard?.writeText(value)} />}
    </Dialog>
  );
}

// ---- 疑点处置 ----
export function IssueForm({ issue, kind, view, onClose, onSaved }: { issue: IssueView; kind: IssueDisposition; view: ReviewView; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const canAdjudicate = useSession(s => s.identity?.permissions.includes("review_adjudicate") ?? false);
  const needsAdjudication = kind === "adjudicate" || kind === "not-applicable";
  const isRepair = kind === "apply-recommendation" || kind === "request-revision";
  const directReplacement = Boolean(issue.guidance?.replacement_text?.trim() && issue.guidance?.target?.translation_segment_id);
  const [reason, setReason] = useState("");
  const [adopted, setAdopted] = useState(issue.a?.value ?? "");
  const [basis, setBasis] = useState("");
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);
  const [base] = useState(() => ({ business_revision_id: view.business_revision_id, issue_version: issue.issue_version, translation_revision_id: view.translation_revision_id }));
  const cmd = useReviewCommand<Record<string, unknown>, unknown>((body, key) => kind === "apply-recommendation" || kind === "request-revision" ? reviewApi.repairIssue(view.case_id, issue.issue_id, kind, body, key) : reviewApi.issueAction(view.case_id, issue.issue_id, kind, body, key));

  const meta: Record<IssueDisposition, { title: string; save: string; hint: string }> = {
    ignore: { title: "忽略这条疑点", save: "忽略并记录", hint: "确认此疑点不影响本次交付后，可记录理由并忽略。不会修改译文，也不把机器结论改写为通过。" },
    "apply-recommendation": { title: "应用推荐修改", save: "应用推荐修改", hint: directReplacement ? "将建议替换文本保存为新译文版本，再校验并更新预览。" : "按当前推荐提交给Agent执行对应处理。结果会回到这条疑点中，完成后仍需核对；缺少材料时会说明需要补充的内容。" },
    "request-revision": { title: "Agent处理", save: "提交给Agent", hint: "说明你希望如何处理这条问题。Agent根据原文、译文及你的意见执行对应处理；缺少证据时会返回补充材料建议，不会凭空补全事实。" },
    accept: { title: "接受这条提示", save: "接受并记录", hint: "适用于已核对并认可现有内容；只登记理由，不改字段、不重新翻译。记录移至“已处置”。译文有误请返回选择“编辑译文”。" },
    adjudicate: { title: "裁定事实", save: "保存裁定", hint: "原始冲突会继续保留。只有依据可核查、采用值确实改变时才产生新业务版；依赖重算完成前不会显示可导出。" },
    "mark-unresolved": { title: "标记无法判断", save: "保存处置", hint: "显示为无法判断，不会写成“自动通过”。" },
    "not-applicable": { title: "标记不适用", save: "保存处置", hint: "需要写明规则前提与不适用依据；规则未覆盖仍单独保留，不能被这个按钮冒充通过。" },
  };
  const m = meta[kind];
  const needBasis = needsAdjudication;
  const canSave = (kind === "apply-recommendation" || kind === "ignore" || reason.trim().length > 0) && (!needBasis || basis.trim().length > 0) && (!needsAdjudication || canAdjudicate);

  return (
    <Dialog
      open
      onClose={onClose}
      title={m.title}
      description={issue.guidance?.category_label ?? issue.category}
      width={720}
      tone={issue.severity === "blocking" ? "risk" : "default"}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            返回
          </button>
          <button type="button" className="btn btn-primary" onClick={() => {
            if (!canSave) return;
            setConflict(null);
            cmd.mutate(isRepair ? { expected_business_revision_id: base.business_revision_id, expected_issue_version: base.issue_version, expected_translation_revision_id: base.translation_revision_id ?? null, instruction: reason.trim() } : { reason: reason.trim(), value: kind === "adjudicate" ? adopted.trim() : undefined, basis: basis.trim() || undefined, expected_issue_version: base.issue_version, expected_business_revision_id: base.business_revision_id }, {
              onSuccess: (result) => {
                const message = result && typeof result === "object" && "message" in result && typeof result.message === "string" ? result.message : null;
                toast({ tone: "ok", text: message ?? (isRepair ? "已受理该疑点处理，请查看结果并核对" : `已记录处置：${m.title}`) });
                onSaved();
              },
              onError: (e) => {
                if (isApiError(e) && e.status === 409) setConflict(e.current);
              },
            });
          }} disabled={!canSave || cmd.isPending}>
            {cmd.isPending ? "正在保存" : m.save}
          </button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <ReadOnlySource label={`A · ${issue.a?.label ?? "来源缺失"}`} value={issue.a?.value ?? ""} />
        {issue.b && <ReadOnlySource label={`B · ${issue.b.label}`} value={issue.b.value} />}
      </div>
      {issue.difference && <p className="num mt-2 text-[12px] text-ink-2">差值 {issue.difference}</p>}
      {isRepair && issue.guidance && <div className="mt-3 space-y-3 rounded-ctl border border-line bg-page p-3 text-[13px] leading-7 text-ink-2">
        <div><p className="font-medium text-ink">推荐处理</p><p className="mt-0.5 whitespace-pre-wrap break-words">{issue.guidance.recommendation}</p></div>
        {issue.guidance.replacement_text?.trim() && issue.guidance.replacement_text.trim() !== issue.guidance.recommendation.trim() && <div><p className="font-medium text-ink">建议替换为</p><p className="mt-0.5 whitespace-pre-wrap break-words">{issue.guidance.replacement_text}</p></div>}
      </div>}

      {kind === "adjudicate" && (
        <label className="mt-3 block">
          <span className="label">裁定后采用的值</span>
          <input className="input mt-1" value={adopted} onChange={(e) => setAdopted(e.target.value)} data-autofocus />
        </label>
      )}
      <label className="mt-3 block">
        <span className="label">{kind === "request-revision" ? "处理意见（必填）" : kind === "apply-recommendation" ? "补充说明（选填）" : kind === "ignore" ? "忽略理由（选填）" : "理由（必填）"}</span>
        <textarea className="input mt-1" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} data-autofocus={kind !== "adjudicate"} />
      </label>
      <label className="mt-3 block">
        <span className="label">可核查依据{needBasis ? "（必填）" : "（可留空）"}</span>
        <input className="input mt-1" value={basis} onChange={(e) => setBasis(e.target.value)} placeholder="例如：承运人更正函 COR-0918" />
      </label>
      <p className="mt-3 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">{m.hint}{kind === "mark-unresolved" && issue.blocking_delivery ? " 这条红项仍阻断交付。" : ""}</p>
      {needsAdjudication && !canAdjudicate && <p className="mt-2 text-[12px] text-warn">当前账号没有事实裁定权限，可以返回查看来源或记录无法判断。</p>}

      {cmd.error && !conflict && <div className="mt-3"><ErrorBar text={errorText(cmd.error)} /></div>}
      {conflict && <ConflictPanel current={conflict} draft={`${kind === "adjudicate" ? `采用值：${adopted}\n` : ""}理由：${reason}${basis ? `\n依据：${basis}` : ""}`} draftLabel="你填写的处置" onReload={onClose} onCopy={() => void navigator.clipboard?.writeText(reason)} />}
    </Dialog>
  );
}

// ---- 译段修订 ----
export function SegmentForm({ segment, view, onClose }: { segment: TranslationSegmentView; view: ReviewView; onClose: () => void }) {
  const toast = useToast();
  const [text, setText] = useState(segment.translated_text);
  const [reason, setReason] = useState("");
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);
  const [base] = useState(() => ({ translation_revision_id: segment.target.translation_revision_id ?? view.translation_revision_id, target_language: segment.target.target_language ?? view.target_language }));
  const cmd = useReviewCommand<Record<string, unknown>, unknown>((body, key) => reviewApi.reviseSegment(view.run_id!, segment.translation_segment_id, body, key));

  return (
    <Dialog
      open
      onClose={onClose}
      title="修订译文"
      description={`${languageName(view.target_language)} · 译文版 ${view.translation_revision_no ?? "—"}｜只影响本处理与本语言，不改业务版。`}
      width={720}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!view.allowed_actions.includes("revise_segment") || !text.trim() || !reason.trim() || cmd.isPending}
            onClick={() => {
              setConflict(null);
              cmd.mutate({ translated_text: text.trim(), reason: reason.trim(), expected_translation_revision_id: base.translation_revision_id, target_language: base.target_language }, {
                onSuccess: () => {
                  toast({ tone: "ok", text: "已保存译文修订，预览等待重算" });
                  onClose();
                },
                onError: (e) => {
                  if (isApiError(e) && e.status === 409) setConflict(e.current);
                },
              });
            }}
          >
            {cmd.isPending ? "正在保存" : "保存译文"}
          </button>
        </>
      }
    >
      <ReadOnlySource label={`原文片段 · ${segment.section_title}`} value={segment.source_text} />
      <label className="mt-3 block">
        <span className="label">译文</span>
        <textarea className="input mt-1" rows={3} value={text} onChange={(e) => setText(e.target.value)} data-autofocus />
      </label>
      <label className="mt-3 block">
        <span className="label">修改理由（必填）</span>
        <textarea className="input mt-1" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例如：按术语表统一为“通知方”" />
      </label>
      {segment.terms.length > 0 && <p className="mt-2 text-[12px] text-ink-2">涉及术语：{segment.terms.join("、")}</p>}
      {cmd.error && !conflict && <div className="mt-3"><ErrorBar text={errorText(cmd.error)} /></div>}
      {conflict && <ConflictPanel current={conflict} draft={`译文：${text}\n理由：${reason}`} draftLabel="你修订的译文" onReload={onClose} onCopy={() => void navigator.clipboard?.writeText(text)} />}
    </Dialog>
  );
}

// ---- 区段归属 ----
export function SectionAssignForm({ view, sectionId, suggestion, onClose }: { view: ReviewView; sectionId: string; suggestion: string; onClose: () => void }) {
  const toast = useToast();
  const current = view.files.flatMap(file => file.logical_documents).find(logical => logical.sections.some(section => section.section_id === sectionId));
  const [docType, setDocType] = useState(current?.doc_type === "other" ? "supporting" : current?.doc_type === "unclassified" ? "unknown" : current?.doc_type ?? "");
  const [reason, setReason] = useState("");
  const [cargoProfiles, setCargoProfiles] = useState<string[]>(current?.cargo_profile_ids ?? []);
  const catalog = useDocumentCatalog();
  const cmd = useReviewCommand<Record<string, unknown>, unknown>((body, key) => reviewApi.sectionAction(view.case_id, sectionId, "assign", body, key));
  return (
    <Dialog
      open
      onClose={onClose}
      title={current?.needs_confirmation ? "确认类型或区段" : "调整类型与货物领域"}
      description={`解析建议：${suggestion}。人工改正会产生新业务版，并让受影响的规则、译文与预览待重算。`}
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!docType || cmd.isPending || !catalog.isSuccess || !catalog.data?.items.some(item => item.code === docType)}
            onClick={() =>
              cmd.mutate({ doc_type: docType, cargo_profile_ids: cargoProfiles, reason: reason.trim() || "人工确认单据用途与货物领域", expected_business_revision_no: view.business_revision_no, expected_business_revision_id: view.business_revision_id, expected_parse_artifact_id: view.files.find(f => f.logical_documents.some(ld => ld.sections.some(s => s.section_id === sectionId)))?.parse_artifact_id }, {
                onSuccess: () => {
                  toast({ tone: "ok", text: "已保存归属，受影响内容等待重算" });
                  onClose();
                },
              })
            }
          >
            {cmd.isPending ? "正在保存" : "保存归属"}
          </button>
        </>
      }
    >
      <label className="label">这一段属于</label>
      <DocumentTypeSelect items={catalog.data?.items ?? []} value={docType} onChange={setDocType} disabled={cmd.isPending || !catalog.data} />
      {catalog.isPending && <p className="mt-2 text-[12px] text-ink-3">正在加载单据类型</p>}
      {catalog.error && <div className="mt-2"><ErrorBar text={errorText(catalog.error)} action={{ label: "重新加载", onSelect: () => void catalog.refetch() }} /></div>}
      {catalog.data && <>
        {docType && <p className="mt-2 text-[12px] text-ink-2">{catalog.data.items.find(item => item.code === docType)?.review_focus.join(" ")}</p>}
        <CargoProfileSelect items={catalog.data.cargo_profiles} value={cargoProfiles} onChange={setCargoProfiles} disabled={cmd.isPending} />
      </>}
      <label className="mt-3 block">
        <span className="label">理由（可留空）</span>
        <input className="input mt-1" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <p className="mt-3 rounded-ctl bg-page px-3 py-2 text-[12px] text-ink-2">边界不清时可以继续标为未分类，不必勉强选一个猜测类型。</p>
      {cmd.error && <div className="mt-3"><ErrorBar text={errorText(cmd.error)} /></div>}
    </Dialog>
  );
}

// ---- 申请重识别 ----
export function ReparseForm({ view, documentId, documentVersion, filename, onClose }: { view: ReviewView; documentId: string; documentVersion: number; filename: string; onClose: () => void }) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const cmd = useReviewCommand<Record<string, unknown>, unknown>((body, key) => reviewApi.reparse(documentId, documentVersion, body, key));
  return (
    <Dialog
      open
      onClose={onClose}
      title="申请重识别"
      description="按整份物理文件重新解析，会产生新的解析产物；原件版本不变，旧区段与历史快照保持只读。"
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            返回
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={cmd.isPending}
            onClick={() =>
              cmd.mutate({ reason: reason.trim(), run_id: view.run_id, expected_parse_artifact_id: view.files.find(f => f.source.document_id === documentId)?.parse_artifact_id }, {
                onSuccess: () => {
                  toast({ tone: "ok", text: "已提交重识别，进度会在处理记录中显示" });
                  onClose();
                },
              })
            }
          >
            {cmd.isPending ? "正在提交" : "发起"}
          </button>
        </>
      }
    >
      <ReadOnlySource label="文件" value={filename} extra={<p className="mt-1 text-[11px] text-ink-3">原件 v{documentVersion}</p>} />
      <div className="mt-3 rounded-ctl border border-warn-line bg-warn-bg px-3 py-2 text-[12px] text-warn">
        <Icon name="warn" size={14} className="mr-1 inline align-[-2px]" />
        重识别会重新调用解析服务，产生额外费用与处理时间。解析版更新后，无法映射到新结果的旧区段会标为待复核。
      </div>
      <label className="mt-3 block">
        <span className="label">理由</span>
        <input className="input mt-1" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例如：首次解析漏读了第 2 页表格" data-autofocus />
      </label>
      {cmd.error && <div className="mt-3"><ErrorBar text={errorText(cmd.error)} /></div>}
    </Dialog>
  );
}

// ---- W05 处理必要确认 ----
export function InputRequestForm({
  request,
  view,
  onClose,
  onLocate,
}: {
  request: import("../../api/types").InputRequestView;
  view: ReviewView;
  onClose: () => void;
  onLocate: (t: import("../../api/types").EvidenceTarget) => void;
}) {
  const toast = useToast();
  const [picked, setPicked] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);
  const cmd = useCommand<ApiRequests["POST /api/v1/input-requests/{request_id}/resolve"]["body"], unknown>((body, key) => workflowApi.resolveInput(request.input_request_id, body, key), ["review-view", "run", "w-run", "w-inputs", "input-requests", "c-conv"]);
  const zeroCandidates = request.options.length === 0;

  const kindText = request.kind === "translation_scope" ? "确认货描范围" : request.kind === "document_boundary" ? "确认单据边界" : "确认源语言";

  return (
    <Dialog
      open
      onClose={() => { if (!cmd.isPending) onClose(); }}
      title={kindText}
      description={request.question}
      width={720}
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={cmd.isPending} onClick={onClose}>
            返回
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={zeroCandidates || !picked.length || cmd.isPending}
            title={zeroCandidates ? "没有可选的候选片段，请改用重识别或新建任务" : undefined}
            onClick={() => {
              setConflict(null);
              cmd.mutate(
                {
                  selected_option_ids: picked,
                  reason: reason.trim(),
                  expected_request_version: request.request_version,
                  expected_parse_artifact_id: request.expected_parse_artifact_id,
                  expected_business_revision_id: request.expected_business_revision_id ?? null,
                },
                {
                  onSuccess: () => {
                    toast({ tone: "ok", text: "已提交确认，处理继续" });
                    onClose();
                  },
                  onError: (e) => {
                    if (isApiError(e) && e.status === 409) setConflict(e.current);
                  },
                },
              );
            }}
          >
            {cmd.isPending ? "正在提交" : "确认范围"}
          </button>
        </>
      }
    >
      <InputRequestEvidence request={request} sources={view.files.map(file => file.source)} onLocate={onLocate} />
      {zeroCandidates ? (
        <div className="space-y-3">
          <ErrorBar
            tone="warn"
            text="解析结果里没有找到可选的货描片段，因此不能在这里勾选。可以先重识别这份文件，或另建一个新任务。"
          />
          <p className="text-[12px] text-ink-2">也可以取消当前处理后重新发起。这些入口在任务卡与文件工具条中。</p>
        </div>
      ) : (
        <>
          <p className="mb-2 text-[12px] text-ink-2">
            {request.kind === "translation_scope" ? "勾选实际属于本次货描的片段。" : "请选择一个符合原件的候选。"}解析版 {shortId(request.expected_parse_artifact_id)}
            {request.expected_business_revision_id ? ` · 业务版 ${shortId(request.expected_business_revision_id)}` : " · 尚无业务版"}
          </p>
          <div className="space-y-2">
            {request.options.map((o) => (
              <label key={o.option_id} className={`flex cursor-pointer items-start gap-2.5 rounded-ctl border p-2.5 ${picked.includes(o.option_id) ? "opt-on" : "border-line"}`}>
                <input
                  type={request.kind === "translation_scope" ? "checkbox" : "radio"}
                  name="review-input-choice"
                  disabled={cmd.isPending}
                  className="mt-0.5"
                  checked={picked.includes(o.option_id)}
                  onChange={(e) => { cmd.renewKey(); setPicked((prev) => request.kind === "translation_scope" ? (e.target.checked ? [...prev, o.option_id] : prev.filter((x) => x !== o.option_id)) : [o.option_id]); }}
                />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium text-ink">{o.label}</span>
                  {o.excerpt && <span className="mt-0.5 block break-words-all text-[12px] text-ink-2">{o.excerpt}</span>}
                  {o.source_ref && (
                    <button
                      type="button"
                      className="btn btn-link btn-sm mt-1 px-0"
                      onClick={(e) => {
                        e.preventDefault();
                        onLocate(o.source_ref!);
                      }}
                    >
                      定位原文
                    </button>
                  )}
                </span>
              </label>
            ))}
          </div>
          <label className="mt-3 block">
            <span className="label">理由（可留空）</span>
            <input className="input mt-1" value={reason} onChange={(e) => { setReason(e.target.value); cmd.renewKey(); }} />
          </label>
        </>
      )}
      {cmd.error && !conflict && <div className="mt-3"><ErrorBar text={errorText(cmd.error)} /></div>}
      {conflict && <ConflictPanel current={conflict} draft={`已选候选：${picked.length} 项\n理由：${reason}`} draftLabel="你的选择" onReload={onClose} onCopy={() => undefined} />}
      <span className="hidden">{view.case_id}</span>
    </Dialog>
  );
}


// ---- R09 确认源语言 ----
export function ConfirmLanguageForm({ view, file, onClose }: { view: ReviewView; file: import("../../api/types").ReviewFileNode; onClose: () => void }) {
  const toast = useToast();
  const sections = useMemo(() => file.logical_documents.flatMap((l) => l.sections.map((s) => ({ ...s, logical: l.label }))), [file]);
  const [lang, setLang] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);
  const picked = Object.entries(lang).filter(([, v]) => v);
  const cmd = useReviewCommand<Record<string, unknown>, unknown>((body, key) =>
    reviewApi.sectionAction(view.case_id, String(body.section_id), "confirm-language", body, key),
  );
  const options = [
    ...LANGUAGES.map(item=>({id:item.code,label:languageNativeName(item.code)})),
    { id: "mixed", label: "多语混排" },
    { id: "already_target", label: "已是目标语言" },
  ];
  return (
    <Dialog
      open
      onClose={onClose}
      title="确认源语言"
      description="逐区段确认原文语言。保存会产生新业务版，并让对应译文与预览重算；超出本次冻结翻译范围时会转为新建任务。"
      width={640}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!picked.length || cmd.isPending}
            onClick={() => {
              setConflict(null);
              // 逐区段提交，任一冲突即停下让用户核对
              const [sectionId, value] = picked[0];
              cmd.mutate(
                { section_id: sectionId, source_language: value, reason: reason.trim(), sections: Object.fromEntries(picked), expected_parse_artifact_id: file.parse_artifact_id, expected_business_revision_no: view.business_revision_no, expected_business_revision_id: view.business_revision_id },
                {
                  onSuccess: () => {
                    toast({ tone: "ok", text: `已确认 ${picked.length} 个区段的源语言，相关译文等待重算` });
                    onClose();
                  },
                  onError: (e) => {
                    if (isApiError(e) && e.status === 409) setConflict(e.current);
                  },
                },
              );
            }}
          >
            {cmd.isPending ? "正在保存" : "保存源语言"}
          </button>
        </>
      }
    >
      <p className="mb-2 text-[12px] text-ink-2">
        {file.source.filename}
        {file.source.language_uncertain ? "：解析判定源语言不确定，请人工确认。" : "：解析已给出推测语言，可在此更正。"}
      </p>
      <div className="space-y-2">
        {sections.map((sec) => (
          <div key={sec.section_id} className="rounded-ctl border border-line p-2.5">
            <p className="text-[12px] text-ink-3">{sec.logical}</p>
            <p className="text-[13px] text-ink">{sec.title}</p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {options.map((o) => (
                <label key={o.id} className={`tag cursor-pointer ${lang[sec.section_id] === o.id ? "tag-on" : "border-line"}`}>
                  <input type="radio" name={`lang-${sec.section_id}`} className="sr-only" checked={lang[sec.section_id] === o.id} onChange={() => setLang((p) => ({ ...p, [sec.section_id]: o.id }))} />
                  {languageCode(o.id) ? <span lang={o.id}>{languageNativeName(o.id)}</span> : <UiText value={o.label} />}
                </label>
              ))}
            </div>
          </div>
        ))}
      </div>
      <label className="mt-3 block">
        <span className="label">理由（可留空）</span>
        <input className="input mt-1" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      {cmd.error && !conflict && <div className="mt-3"><ErrorBar text={errorText(cmd.error)} /></div>}
      {conflict && <ConflictPanel current={conflict} draft={picked.map(([k, v]) => `${k}=${v}`).join(", ")} onReload={onClose} onCopy={() => undefined} />}
    </Dialog>
  );
}

// ---- 替换此原件（U02/U05，产生新 DocumentVersion）----
export function ReplaceDocumentForm({ file, onClose, onGoUpload }: { file: import("../../api/types").ReviewFileNode; onClose: () => void; onGoUpload: (replaceDocumentId: string, expectedVersion: number) => void }) {
  return (
    <Dialog
      open
      onClose={onClose}
      title="替换此原件"
      description="替换会为这份原件产生一个新版本，不等于重识别。旧版本与历史交付快照仍然只读保留。"
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            返回
          </button>
          <button type="button" className="btn btn-primary" onClick={() => onGoUpload(file.source.document_id, file.source.document_version)} data-autofocus>
            去选择替代文件
          </button>
        </>
      }
    >
      <ReadOnlySource label="将被替换的原件" value={file.source.filename} extra={<p className="mt-1 text-[11px] text-ink-3">当前版本 v{file.source.document_version}</p>} />
      <ul className="mt-3 space-y-1 rounded-ctl bg-page px-3 py-2 text-[12px] leading-relaxed text-ink-2">
        <li>· 已启动处理的输入集合不会被改写；要让新版本参与处理，需要另建一次新任务。</li>
        <li>· 依赖旧版本的来源引用会标为待复核，历史锚点仍可查看。</li>
      </ul>
    </Dialog>
  );
}

// ---- 切换到当前工作版 ----
export function SwitchToWorkingDialog({ onClose, onSwitch }: { onClose: () => void; onSwitch: () => void }) {
  return (
    <Dialog
      open
      onClose={onClose}
      title="切换到当前工作版"
      description="当前查看的是历史交付版本。切换会重新按当前版本鉴权并加载，历史快照仍可从记录中打开。"
      width={480}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            继续看历史版
          </button>
          <button type="button" className="btn btn-primary" onClick={onSwitch} data-autofocus>
            切换当前工作版
          </button>
        </>
      }
    >
      <p className="text-[13px] leading-relaxed text-ink-2">
        旧来源锚点不会被投射到新的解析结果上。如果当前版有待重算的内容，切换后会看到「待重算」标识，而不是直接显示为已通过。
      </p>
    </Dialog>
  );
}

/** 关闭含未保存输入的工作台前的确认。 */
export function UnsavedDialog({ open, onKeep, onDiscard }: { open: boolean; onKeep: () => void; onDiscard: () => void }) {
  return (
    <Dialog
      open={open}
      onClose={onKeep}
      title="还有未保存的修改"
      description="关闭后这些输入会丢失。"
      width={420}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onKeep} data-autofocus>
            继续编辑
          </button>
          <button type="button" className="btn btn-danger" onClick={onDiscard}>
            丢弃未保存更改
          </button>
        </>
      }
    />
  );
}

export function LoadingForm() {
  return (
    <div className="flex items-center justify-center p-8">
      <Loading />
    </div>
  );
}

export { StatusPill as FormStatusPill };
