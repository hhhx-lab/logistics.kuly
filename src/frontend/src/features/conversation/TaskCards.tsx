import { consignmentApi } from "../../api/consignment";
import { managementApi } from "../../api/management";
import { LANGUAGES, languageNativeName, languageName, languagePreference } from "../../i18n/languages";
import { UiText, englishError } from "../../i18n/runtime";
// 输入区上方的卡片：空闲时是单据转换主卡，有活动任务时同位替换为任务卡。
// 卡片不是第二个任务状态源：阶段与数值只来自 W04 的 RunView 与持久事件。
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiResponses } from "../../api/generated/request-contracts";
import { phaseLabel, summarizeProgress, type ProgressEvent } from "./progress";
import { useSession } from "../../stores/session";
import { useSnapshot } from "../review/hooks";
import { SnapshotFiles } from "../review/ReviewExport";
import { api, errorText } from "../../api/client";
import type { DocumentTypeInput, RunView, TaskDraftView, TranslateDraftParameters } from "../../api/types";
import { useDocumentCatalog } from "../../api/documentCatalog";
import { CargoProfileSelect, DocumentTypeSelect } from "../../components/DocumentTypeControls";
import { Icon } from "../../components/Icon";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar, RunStatusPill, StatusPill, formatTime, shortId } from "../../components/Status";
import { newKey, useCommand, useInputRequests, useWorkflows, workflowApi } from "./hooks";
import { JurisdictionInputsForm } from "./JurisdictionInputsForm";
import { useMemories } from "../settings/hooks";

const FLOW = ["上传", "识别与核对", "人工复核", "导出"];

/** 缺项字段键到中文说明，界面不直接显示契约字段名。 */
const MISSING_LABEL: Record<string, string> = {
  target_language: "还没有选择目标语言",
  selected_section_refs: "选择了「指定区段」但还没有勾选任何区段",
  output_formats: "至少选择一种译本格式",
  source_versions: "还没有已验收的原件",
};

/** 空会话主卡：只展开说明，不建立任何 Run。 */
export function DocumentMainCard({ onUpload, compact }: { onUpload: () => void; compact?: boolean }) {
  const [flowOpen, setFlowOpen] = useState(false);
  const workflows = useWorkflows();
  const wf = workflows.data?.items.filter((w) => w.workflow_type === "translate_review").sort((a,b) => b.workflow_version-a.workflow_version)[0];
  const blocked = wf && !wf.enabled;

  if (compact) {
    return (
      <button type="button" className="flex h-11 w-full items-center gap-2 rounded-card border border-line bg-card px-3 text-left" onClick={onUpload}>
        <Icon name="doc" size={16} className="text-ink-3" />
        <span className="flex-1 text-[13px] text-ink">上传同票单据，识别、翻译并核对</span>
        <span className="text-[12px] text-brand-ink">上传单据</span>
      </button>
    );
  }

  return (
    <section className="card min-h-[128px] p-5">
      <div className="flex flex-wrap items-start gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-ctl border border-brand-line bg-brand-soft text-brand">
          <Icon name="doc" size={20} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-[16px] font-semibold text-ink">单据翻译与核对</h2>
          <p className="mt-1 text-[13px] leading-relaxed text-ink-2">上传同票材料，识别、翻译并核对；复核后导出。</p>
        </div>
        <button type="button" className="btn btn-primary h-10 shrink-0" onClick={onUpload} disabled={blocked} title={wf?.disabled_reason ?? undefined}>
          <Icon name="paperclip" size={16} />
          上传单据
        </button>
      </div>
      <button type="button" className="mt-4 text-[12px] text-brand-ink underline underline-offset-2" onClick={() => setFlowOpen((v) => !v)} aria-expanded={flowOpen}>
        查看处理流程
      </button>
      {flowOpen && (
        <ol className="mt-2 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-2">
          {FLOW.map((f, i) => (
            <li key={f} className="flex items-center gap-1.5">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-page text-[11px] text-ink-2">{i + 1}</span>
              <UiText value={f} />
              {i < FLOW.length - 1 && <Icon name="chevron-right" size={12} className="text-ink-3" />}
            </li>
          ))}
          <li className="w-full pt-1 text-[11px] text-ink-3">识别与核对由系统完成并给出疑点；必须经人工复核确认后才能导出，不会自动通关。</li>
        </ol>
      )}
      {blocked && <div className="mt-3"><ErrorBar text={wf?.disabled_reason ?? "当前部署尚未开启这个工作流"} tone="warn" /></div>}
    </section>
  );
}

/** 任务要求摘要：尚未启动 Run 时可编辑；启动后改文件／语言／范围必须新建任务。 */
export function TaskDraftCard({
  draft,
  onStarted,
  onDiscarded,
  onUpload,
}: {
  draft: TaskDraftView;
  onStarted: (runId: string) => void;
  onDiscarded: () => void;
  onUpload: () => void;
}) {
  const toast = useToast();
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const workflows = useWorkflows();
  const packs = workflows.data?.items.find(w => w.workflow_type === "translate_review" && w.workflow_version === draft.workflow_version)?.published_rule_packs ?? [];
  const catalog = useDocumentCatalog();
  const isTranslate = draft.workflow_type === "translate_review";
  const isCompare = draft.workflow_type === "consignment_check";
  const memories = useMemories();
  const languagePreferences = (memories.data?.items ?? []).filter(item => item.category === "target_language").map(item => {
    const language=languagePreference(item.content);
    return language ? {...item,language} : null;
  }).filter((item): item is NonNullable<typeof item> => item !== null);
  const languageSuggestion = languagePreferences.length === 1 ? languagePreferences[0] : null;
  const parameters = (draft.draft_parameters ?? {}) as Partial<TranslateDraftParameters> & { scope_revision_id?: string | null; comparison_source_versions?: { document_id: string; document_version: number }[] };
  const generation = useSession(state => state.generation);
  const scope = useQuery({ queryKey: ["consignment", generation, draft.case_id, "scope", parameters.scope_revision_id], queryFn: ({signal}) => consignmentApi.scope(draft.case_id, parameters.scope_revision_id, signal), enabled: !!parameters.scope_revision_id, retry: false });
  const caseOverview = useQuery({queryKey:["draft-case-overview",generation,draft.case_id],queryFn:()=>managementApi.overview(draft.case_id),enabled:!!parameters.scope_revision_id,retry:false});
  const p = { ...parameters, selected_section_refs: parameters.selected_section_refs ?? [], output_formats: parameters.output_formats ?? [] };
  const sourceVersions = draft.source_versions ?? [];
  const missingFields = draft.missing_fields ?? [];
  const questions = draft.questions ?? [];

  const patch = useCommand<Record<string, unknown>, TaskDraftView>((body, key) => workflowApi.patchDraft(draft.draft_task_id, body, draft.draft_version, key), ["w-draft", "c-conv"]);
  const start = useCommand<void, RunView>((_a, key) => workflowApi.startRun(draft.draft_task_id, draft.draft_version, key), ["w-draft", "c-conv", "w-run"]);
  const discard = useCommand<void, void>((_a, key) => workflowApi.deleteDraft(draft.draft_task_id, draft.draft_version, key), ["w-draft", "c-conv"]);

  const ready = draft.status === "ready";
  const typeInputs = parameters.document_type_inputs ?? [];
  const saveTypeInput = (source: typeof sourceVersions[number], change: Partial<DocumentTypeInput>) => {
    const previous = typeInputs.find(item => item.document_id === source.document_id && item.document_version === source.document_version);
    const next = { document_id: source.document_id, document_version: source.document_version, document_type: null, cargo_profile_ids: [], ...previous, ...change };
    const rest = typeInputs.filter(item => item.document_id !== source.document_id);
    patch.renewKey();
    patch.mutate({ draft_parameters: { document_type_inputs: next.document_type || next.cargo_profile_ids.length ? [...rest, next] : rest } });
  };

  return (
    <section className="card max-h-[55dvh] min-h-0 overflow-y-auto p-4 scroll-thin">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <p className="text-[13px] font-medium text-ink">{isTranslate ? "翻译要求" : isCompare ? "同票核对要求" : "识别要求"}</p>
            <StatusPill tone={ready ? "ok" : "warn"} icon={ready ? "check" : "warn"}>
              {ready ? "可以启动" : isTranslate ? "尚未启动翻译" : isCompare ? "尚未启动核对" : "尚未启动识别"}
            </StatusPill>
          </div>
          <p className="mt-1 text-[12px] text-ink-2">{draft.task_summary}</p>
        </div>
        {!draft.started_run_id && (
          <button type="button" className="btn btn-ghost btn-sm text-risk" onClick={() => setConfirmDiscard(true)}>
            删除草稿
          </button>
        )}
      </div>

      <dl className="mt-3 grid gap-x-3 gap-y-1.5 text-[12px] sm:grid-cols-[92px_1fr]">
        <dt className="text-ink-3">任务类型</dt>
        <dd className="text-ink">{isTranslate ? "翻译与校验" : isCompare ? "同票核对（不产生译文）" : "识别与索引（不产生译文）"}</dd>

        <dt className="text-ink-3">参与文件</dt>
        <dd className="text-ink">
          {sourceVersions.length ? (
            <ul className="space-y-0.5">
              {sourceVersions.map((s) => (
                <li key={s.document_id} className="break-words-all">
                  {s.filename} <span className="num text-ink-3">v{s.document_version}</span>
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-warn">还没有已验收的原件</span>
          )}
          <button type="button" className="btn btn-ghost btn-sm mt-1" onClick={onUpload}>
            修改
          </button>
        </dd>

        {sourceVersions.length > 0 && <>
          <dt className="pt-2 text-ink-3">单据类型</dt>
          <dd className="min-w-0 space-y-2">
            {catalog.isError ? <div className="flex items-center gap-2"><ErrorBar text="单据类型目录暂时无法加载" /><button type="button" className="btn btn-ghost btn-sm" onClick={() => catalog.refetch()}>重试</button></div> : sourceVersions.map(source => {
              const choice = typeInputs.find(item => item.document_id === source.document_id && item.document_version === source.document_version);
              const disabled = !!draft.started_run_id || patch.isPending || start.isPending || !catalog.data;
              return <div key={source.document_id} className="rounded-ctl border border-line p-2.5">
                <p className="mb-2 break-words text-ink-2">{source.filename}</p>
                <DocumentTypeSelect items={(catalog.data?.items ?? []).filter(item => item.code !== "unknown")} value={choice?.document_type ?? ""} placeholder={catalog.isPending ? "正在加载单据类型…" : "自动识别"} label={`单据类型：${source.filename}`} disabled={disabled} onChange={code => saveTypeInput(source, { document_type: code || null })} />
                <CargoProfileSelect items={catalog.data?.cargo_profiles ?? []} value={choice?.cargo_profile_ids ?? []} disabled={disabled} onChange={codes => saveTypeInput(source, { cargo_profile_ids: codes })} />
              </div>;
            })}
          </dd>
        </>}

        {(isCompare || isTranslate && draft.workflow_version >= 2) && <>
          <dt className="text-ink-3">核对范围</dt>
          <dd className="text-ink-2">
            <p>核对本票已确认范围内的原件；译本不作为第二份业务凭证。</p>
            {scope.isPending && parameters.scope_revision_id ? <p className="mt-1 text-ink-3">正在读取材料范围</p> : scope.error ? <ErrorBar text={errorText(scope.error)} /> : <ul className="mt-1 space-y-0.5">{scope.data?.payload.materials.filter(material=>material.purpose!=="excluded").map(material=><li key={material.document_id}>{caseOverview.data?.sources.find(source=>source.document_id===material.document_id)?.filename ?? sourceVersions.find(source=>source.document_id===material.document_id)?.filename ?? "未加载的关联单据"} · v{material.document_version}</li>)}</ul>}
          </dd>
        </>}
        {isTranslate && (
          <>
            <dt className="text-ink-3">目标语言</dt>
            <dd>
              <select className="input w-full max-w-[320px]" aria-label="目标语言" value={p.target_language ?? ""} disabled={!!draft.started_run_id || patch.isPending || start.isPending} onChange={e=>{
                patch.renewKey();patch.mutate({draft_parameters:{target_language:e.target.value || null}});
              }}>
                <option value="">选择目标语言</option>
                {LANGUAGES.map(item=><option key={item.code} value={item.code}>{languageNativeName(item.code)}</option>)}
              </select>
              {!p.target_language && <p className="mt-1 text-[11px] text-warn">还没有指定目标语言。长期偏好只作建议，需要你点选后才会成为本次要求。</p>}
              {!p.target_language && !draft.started_run_id && languageSuggestion && (
                <div className="mt-2 rounded-ctl border border-line bg-page p-2 text-[11px]">
                  <p className="text-ink-2">已确认偏好：{languageSuggestion.content}</p>
                  {languageSuggestion.applies_when && <p className="mt-1 text-ink-3">适用条件：{languageSuggestion.applies_when}</p>}
                  <button type="button" className="btn btn-secondary btn-sm mt-1" disabled={patch.isPending} onClick={() => {
                    patch.renewKey(); patch.mutate({ draft_parameters: { target_language: languageSuggestion.language } });
                  }}>本次采用{languageName(languageSuggestion.language)}</button>
                </div>
              )}
            </dd>

            <dt className="text-ink-3">翻译范围</dt>
            <dd className="text-ink">
              {p.translation_scope === "goods_description" ? "只翻货描" : p.translation_scope === "selected_sections" ? "指定区段" : "全文翻译"}
              {p.selected_section_refs.length > 0 && <span className="text-ink-3"> · 已选 {p.selected_section_refs.length} 个区段</span>}
              {!draft.started_run_id && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm ml-1"
                  onClick={() => {
                    patch.renewKey();
                    patch.mutate({ draft_parameters: { translation_scope: p.translation_scope === "goods_description" ? "all" : "goods_description" } });
                  }}
                >
                  修改
                </button>
              )}
            </dd>

            <dt className="text-ink-3">译本格式</dt>
            <dd>
              <div className="flex flex-wrap gap-1.5">
                {([["docx", "DOCX"], ["pdf", "PDF"]] as const).map(([id, label]) => {
                  const on = p.output_formats.includes(id);
                  // 至少保留一种译本格式
                  const lastOne = on && p.output_formats.length === 1;
                  return (
                    <label key={id} className={`tag cursor-pointer ${on ? "tag-on" : "border-line"} ${lastOne ? "opacity-70" : ""}`} title={lastOne ? "至少保留一种译本格式" : undefined}>
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={on}
                        disabled={!!draft.started_run_id || lastOne || patch.isPending}
                        onChange={() => {
                          patch.renewKey();
                          patch.mutate({ draft_parameters: { output_formats: on ? p.output_formats.filter((x) => x !== id) : [...p.output_formats, id] } });
                        }}
                      />
                      {label}
                    </label>
                  );
                })}
              </div>
              <p className="mt-1 text-[11px] text-ink-3">导出时可同时选择识别稿 PDF。</p>
            </dd>

            <dt className="text-ink-3">规则与术语</dt>
            <dd className="text-ink-2">
              <select aria-label="规则包" className="input w-full" value={p.rule_pack_version ?? "core-document@1"} disabled={!!draft.started_run_id || patch.isPending} onChange={e => { patch.renewKey(); patch.mutate({ draft_parameters: { rule_pack_version: e.target.value, jurisdiction_inputs: null } }); }}>
                {packs.length ? packs.map(pack => <option key={pack.version} value={pack.version}>{pack.title}{pack.status === "trial_material_check" ? "（试行）" : ""}</option>) : <option value={p.rule_pack_version ?? "core-document@1"}>{p.rule_pack_version ?? "通用单据核对"}</option>}
              </select>
              {p.glossary_version ? ` · 术语表 ${p.glossary_version}` : " · 未指定术语表"}
              {packs.find(pack => pack.version === p.rule_pack_version)?.notice && <p className="mt-2 text-[11px] text-warn">{packs.find(pack => pack.version === p.rule_pack_version)?.notice}</p>}
              {packs.find(pack => pack.version === p.rule_pack_version)?.parameter_schema && <JurisdictionInputsForm key={draft.draft_version} packVersion={p.rule_pack_version} value={p.jurisdiction_inputs} sources={sourceVersions} disabled={!!draft.started_run_id || patch.isPending} onSave={value => { patch.renewKey(); patch.mutate({ draft_parameters: { jurisdiction_inputs: value } }); }} />}
            </dd>
          </>
        )}
      </dl>

      {missingFields.length > 0 && (
        <div className="mt-3 rounded-ctl bg-warn-bg px-2.5 py-2">
          <p className="text-[12px] font-medium text-warn">还缺这些才能启动</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[12px] text-warn">
            {missingFields.map((f) => (
              <li key={f}>{MISSING_LABEL[f] ?? f}</li>
            ))}
          </ul>
        </div>
      )}
      {questions.length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[12px] text-ink-2">
          {questions.map((q) => (
            <li key={q}>{q}</li>
          ))}
        </ul>
      )}

      {(patch.error || start.error || discard.error) && <div className="mt-2"><ErrorBar text={errorText(patch.error ?? start.error ?? discard.error)} /></div>}

      <div className="mt-3 flex items-center gap-2">
        <span className="flex-1 text-[11px] text-ink-3">{ready ? "启动后这次任务的文件、语言与范围会被冻结" : "补齐缺项后才能启动"}</span>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!ready || patch.isPending || start.isPending || !!draft.started_run_id}
          onClick={() =>
            start.mutate(undefined, {
              onSuccess: (r) => {
                toast({ tone: "ok", text: "已启动处理，进度来自真实事件" });
                onStarted(r.run_id);
              },
            })
          }
        >
          {start.isPending ? "正在启动" : isTranslate ? "开始翻译" : isCompare ? "开始核对" : "开始识别"}
        </button>
      </div>

      <Dialog
        open={confirmDiscard}
        onClose={() => setConfirmDiscard(false)}
        title="删除这份待启动草稿"
        description="只清除尚未启动的任务要求，不会删除已接收的原件或这一票。"
        width={460}
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setConfirmDiscard(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={() =>
                discard.mutate(undefined, {
                  onSuccess: () => {
                    setConfirmDiscard(false);
                    toast({ tone: "ok", text: "已删除草稿；原件仍保留在这一票里" });
                    onDiscarded();
                  },
                })
              }
            >
              删除草稿
            </button>
          </>
        }
      >
        <p className="text-[12px] leading-relaxed text-ink-2">
          将放弃的任务要求：{draft.task_summary}；关联文件 {sourceVersions.length} 份。已验收的原件随整票保留，删除整票的入口在单据管理台的单票概览。
        </p>
      </Dialog>
    </section>
  );
}

/** 活动任务卡：状态映射必须显式，未知状态写「状态待支持」而不归类为成功。 */
export function RunCard({
  run,
  events,
  connection,
  onOpenReview,
  onOpenRecord,
  onEndService,
  onResolveInput,
  onStartTranslate,
}: {
  run: RunView;
  events: ProgressEvent[];
  connection: "connecting" | "open" | "reconnecting" | "closed";
  onOpenReview: () => void;
  onOpenRecord: () => void;
  onEndService: () => void;
  onResolveInput: () => void;
  onStartTranslate?: () => void;
}) {
  const toast = useToast();
  const [timelineOpen, setTimelineOpen] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const inputs = useInputRequests(run.run_id, run.status === "awaiting_input");
  const generation = useSession((s) => s.generation);
  const qc = useQueryClient();
  const timelineKey = ["conversation-timeline", generation, run.run_id];
  const timeline = useQuery({ queryKey: timelineKey,
    queryFn: async ({ signal }) => {
      const items = [...(qc.getQueryData<ProgressEvent[]>(timelineKey) ?? [])];
      let after = items.at(-1)?.seq ?? 0;
      do {
        const page = await api<ApiResponses["GET /api/v1/runs/{run_id}/events"]["200"]>(`/runs/${run.run_id}/events`, { query: { after_seq: after, limit: 200 }, signal });
        items.push(...page.items.map((e) => ({ seq: e.seq, at: e.occurred_at, text: e.message, stage: e.stage, type: e.event_type })));
        if (!page.next_cursor || page.next_cursor <= after) break;
        after = page.next_cursor;
      } while (!signal.aborted);
      return items;
    }, retry: false });
  useEffect(() => {
    void qc.invalidateQueries({ queryKey: ["conversation-timeline", generation, run.run_id] });
  }, [qc, generation, run.run_id, run.last_seq]);
  const orderedEvents = [...new Map([...(timeline.data ?? []), ...events].map((e) => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq);
  const visibleEvents = summarizeProgress(orderedEvents, run);
  const snapshot = useSnapshot(run.status === "exported" ? run.latest_success_snapshot_id : null);
  const cancel = useCommand<void, RunView>((_a, key) => workflowApi.cancelRun(run.run_id, key), ["w-run", "c-conv"]);
  const retry = useCommand<void, RunView>((_a, key) => workflowApi.retryRun(run.run_id, key), ["w-run", "c-conv"]);

  const known = RUN_CARD[run.status];
  if (!known) console.warn("[run-card] 未映射的 Run 状态", run.status);
  const map = known ?? { text: "状态待支持，请查看管理记录" };
  const canCancel = run.allowed_actions.includes("cancel") || run.allowed_actions.includes("request_cancel");
  const cancelBlockedReason = run.disabled_reasons?.cancel ?? run.disabled_reasons?.request_cancel ?? null;
  const canRetry = run.allowed_actions.includes("retry");
  const queued = run.status === "queued";

  return (
    <section className="card p-4">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <RunStatusPill status={run.status} />
            <span className="text-[12px] text-ink-2">{run.case_name}</span>
            <span className="num text-[11px] text-ink-3">Run {shortId(run.run_id)}</span>
            {run.target_language && <span className="text-[11px] text-ink-3">{languageName(run.target_language)}</span>}
          </div>
          <p className="mt-1 text-[13px] text-ink">{map.text}</p>
          {/* 当前状态与历史成功快照分开显示，不写成笼统「已完成」 */}
          {run.has_successful_snapshot && run.status !== "exported" && (
            <p className="mt-1 text-[12px] text-ink-2">已有一份成功交付快照（{run.latest_success_snapshot_at ? formatTime(run.latest_success_snapshot_at) : "时间未知"}），当前工作版仍在处理。</p>
          )}
          {(run.status === "awaiting_review" || run.status === "checked") && (
            <p className="mt-1 text-[12px] text-ink-2">
              红项 <span className="num">{run.blocking_issue_count}</span> · 黄项 <span className="num">{run.warning_issue_count}</span> ·
              无法判断 {run.undetermined_issue_count === null || run.undetermined_issue_count === undefined ? <span className="text-ink-3">未提供</span> : <span className="num">{run.undetermined_issue_count}</span>}
            </p>
          )}
          {["failed_retryable", "failed_final"].includes(run.latest_export_attempt_status ?? "") && (
            <p className="mt-1 text-[12px] text-risk">
              上次导出失败{run.latest_export_attempt_error_summary ? `：${run.latest_export_attempt_error_summary}` : ""}。可在校验区按服务端许可重试这次导出。
            </p>
          )}
          {run.pause_explanation && <p className="mt-1 text-[12px] text-warn">{run.pause_explanation}</p>}
          {run.status === "paused_reconcile" && (
            <p className="mt-1 text-[12px] text-ink-2">这类外部请求需要有核查权限的人员在单据管理台的任务时间线上处理，普通用户不能直接重投。</p>
          )}
          {run.failure_reason && <p lang="en" className="mt-1 text-[12px] text-risk">{englishError(run.failure_reason)}</p>}
        </div>

        <div className="flex shrink-0 flex-wrap gap-1.5">
          {run.status === "awaiting_input" && (
            <button type="button" className="btn btn-primary btn-sm" onClick={onResolveInput}>
              {inputs.data?.items[0] ? INPUT_LABEL[inputs.data.items[0].kind] ?? "补充确认" : "补充确认"}
            </button>
          )}
          {["awaiting_review", "ready_to_export", "exporting", "exported"].includes(run.status) && (
            <button type="button" className="btn btn-primary btn-sm" onClick={onOpenReview}>
              {run.status === "awaiting_review" ? "查看并校验" : run.status === "ready_to_export" ? "打开复核区并导出" : run.status === "exporting" ? "查看导出进度" : "查看交付文件"}
            </button>
          )}
          {(run.status === "indexed" || run.status === "checked") && (
            <>
              <button type="button" className="btn btn-secondary btn-sm" onClick={onOpenReview}>
                {run.status === "checked" ? "查看同票对比" : "查看原文"}
              </button>
              {onStartTranslate && (
                <button type="button" className="btn btn-primary btn-sm" onClick={onStartTranslate}>
                  发起翻译
                </button>
              )}
            </>
          )}
          {["failed_retryable", "failed_final", "cancelled", "paused_reconcile"].includes(run.status) && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={onOpenRecord}>
              查看原因
            </button>
          )}
          {run.status === "exported" && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={onEndService}>
              结束本次服务
            </button>
          )}
          {canRetry && (
            <button type="button" className="btn btn-secondary btn-sm" disabled={retry.isPending} onClick={() => retry.mutate(undefined, { onSuccess: () => toast({ tone: "ok", text: "已请求重试，进度以服务端状态为准" }) })}>
              重试
            </button>
          )}
          <button type="button" className="btn btn-secondary btn-sm" onClick={onOpenRecord}>
            查看管理记录
          </button>
          {canCancel ? (
            <button type="button" className="btn btn-ghost btn-sm text-risk" onClick={() => setConfirmCancel(true)}>
              {queued ? "取消处理" : "请求取消"}
            </button>
          ) : cancelBlockedReason ? (
            <span className="text-[11px] text-ink-3" title={cancelBlockedReason}>
              不可取消：{cancelBlockedReason}
            </span>
          ) : null}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-line pt-2">
        <span className="min-w-0 flex-1 truncate text-[11px] text-ink-3">
          当前阶段：{phaseLabel(run.current_stage)}
        </span>
        {connection === "reconnecting" && <StatusPill tone="warn" icon="clock" busy>正在恢复连接</StatusPill>}
        <button type="button" className="text-[11px] text-brand-ink underline underline-offset-2" onClick={() => setTimelineOpen((v) => !v)} aria-expanded={timelineOpen}>
          {timelineOpen ? "收起进度" : "展开进度"}
        </button>
      </div>
      {timelineOpen && (
        <ul className="mt-1.5 max-h-[240px] space-y-1 overflow-auto scroll-thin">
          {timeline.isError && <li><ErrorBar text={errorText(timeline.error)} /></li>}
          {visibleEvents.length === 0 && <li className="text-[12px] text-ink-2">还没有收到新的事件。阶段与时间来自服务端持久记录。</li>}
          {visibleEvents.map((e, i) => (
            <li key={i} className="flex gap-2 text-[12px]">
              <span className="num shrink-0 text-ink-3">{formatTime(e.at)}</span>
              <span className="min-w-0 flex-1 text-ink-2">{e.phase ? <><UiText value={e.phase} /> · <UiText value={e.status} /></> : e.text}</span>
            </li>
          ))}
        </ul>
      )}
      {run.status === "exported" && snapshot.data && <div className="mt-3"><p className="mb-2 text-[14px] font-medium">本次服务已完成</p><SnapshotFiles snapshot={snapshot.data} /></div>}

      <Dialog
        open={confirmCancel}
        onClose={() => setConfirmCancel(false)}
        title={queued ? "取消这次处理" : "请求取消这次处理"}
        width={480}
        tone="risk"
        footer={
          <>
            <button type="button" className="btn btn-secondary" data-autofocus onClick={() => setConfirmCancel(false)}>
              返回
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={cancel.isPending}
              onClick={() =>
                cancel.mutate(undefined, {
                  onSuccess: (r) => {
                    setConfirmCancel(false);
                    toast({ tone: "ok", text: r.status === "cancelled" ? "已取消这次处理" : "已提交取消请求，最终状态以服务端为准" });
                  },
                })
              }
            >
              {queued ? "取消处理" : "提交取消请求"}
            </button>
          </>
        }
      >
        <dl className="grid gap-x-3 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr]">
          <dt className="text-ink-3">任务</dt>
          <dd className="num text-ink">Run {shortId(run.run_id)}（{run.case_name}）</dd>
          <dt className="text-ink-3">当前阶段</dt>
          <dd className="text-ink">{run.current_stage}</dd>
        </dl>
        <p className="mt-2 text-[12px] leading-relaxed text-ink-2">
          已接收的原件和历史成功快照不会被删除。如果已经送出的外部请求受理状态不明，任务会转为「外部请求待核查」而不会立即标为已取消。
        </p>
        {cancel.error && <div className="mt-2"><ErrorBar text={errorText(cancel.error)} /></div>}
      </Dialog>
    </section>
  );
}

const INPUT_LABEL: Record<string, string> = {
  document_boundary: "确认类型与边界",
  source_language: "确认混排源语言",
  translation_scope: "确认货描范围",
};

const RUN_CARD: Record<string, { text: string }> = {
  queued: { text: "等待处理。排队中不显示百分比。" },
  processing: { text: "正在处理。阶段与时间来自服务端事件。" },
  parsing: { text: "正在识别与索引，不会产生译文。" },
  awaiting_input: { text: "需要你补充确认后才能继续。" },
  checked: { text: "同票核对完成，可查看差异与关联证据。本次不产生译本。" },
  indexed: { text: "识别与索引完成，没有产生译文。可以据此回答文件内容，或另发起翻译。" },
  awaiting_review: { text: "已产出译文草稿，等待人工复核。" },
  ready_to_export: { text: "复核已确认，可以在校验区导出。" },
  exporting: { text: "正在导出并逐文件回检。暂存产物不提供下载。" },
  exported: { text: "已导出成功快照。" },
  paused_reconcile: { text: "外部请求待核查，需要有权人员处理后才能继续。" },
  failed_retryable: { text: "处理失败，按服务端许可可以重试。" },
  failed_final: { text: "处理失败且不可重试，可新建任务。" },
  cancelled: { text: "这次处理已取消。原件与历史快照仍然保留。" },
};
