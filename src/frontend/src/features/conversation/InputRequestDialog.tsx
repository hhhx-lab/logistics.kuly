// 待输入确认（W05）：文件类型与边界、混排源语言、货描范围三类。
// 这是「处理必要输入」，与 awaiting_review 的人工复核是两回事，不复用同一个按钮。
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { errorText, isApiError } from "../../api/client";
import { useDocumentCatalog } from "../../api/documentCatalog";
import { CargoProfileSelect, DocumentTypeSelect } from "../../components/DocumentTypeControls";
import type { EvidenceTarget, InputRequestView } from "../../api/types";
import { Dialog, useToast } from "../../components/Overlay";
import { ErrorBar } from "../../components/Status";
import { useCommand, workflowApi } from "./hooks";
import { InputRequestEvidence } from "./InputRequestEvidence";
import type { SourceVersion } from "../../api/types";

const TITLE: Record<InputRequestView["kind"], { title: string; description: string }> = {
  document_boundary: {
    title: "确认单据类型与边界",
    description: "这份原件里有无法自动判定归属的区段。确认后会以新的执行轮次继续处理。",
  },
  source_language: {
    title: "确认混排源语言",
    description: "检测到多种源语言。检测结果不代表你的指定；如果确认结果会改变已冻结的翻译范围，需要另建一个新任务。",
  },
  translation_scope: {
    title: "确认货描范围",
    description: "只能在有来源的货描候选中选择本次必译片段。要翻译其他区段时，请新建任务，不会在这次处理里悄悄扩大范围。",
  },
};

export function InputRequestDialog({
  request,
  sources,
  onClose,
  onLocate,
}: {
  request: InputRequestView;
  sources: SourceVersion[];
  onClose: () => void;
  onLocate: (t: EvidenceTarget) => void;
}) {
  const toast = useToast();
  const qc = useQueryClient();
  const [picked, setPicked] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [cargoProfiles, setCargoProfiles] = useState<string[]>([]);
  const catalog = useDocumentCatalog();
  const t = TITLE[request.kind];
  const multi = request.kind === "translation_scope";

  const resolve = useCommand<void, unknown>((_a, key) =>
    workflowApi.resolveInput(
      request.input_request_id,
      {
        selected_option_ids: picked,
        ...(request.kind === "document_boundary" ? { cargo_profile_ids: cargoProfiles } : {}),
        reason: reason.trim() || undefined,
        // 版本与解析基线一并提交，服务端据此核对并发与解析是否变化
        expected_request_version: request.request_version,
        expected_parse_artifact_id: request.expected_parse_artifact_id,
        expected_business_revision_id: request.expected_business_revision_id ?? null,
      },
      key,
    ),
  ["w-run", "w-inputs", "input-requests", "review-view", "run", "c-conv"]);

  const toggle = (id: string) => {
    setPicked((p) => multi ? (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]) : [id]);
    resolve.renewKey();
  };

  const noCandidate = request.options.length === 0;

  return (
    <Dialog
      open
      onClose={() => { if (!resolve.isPending) onClose(); }}
      title={t.title}
      description={t.description}
      width={620}
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={resolve.isPending} onClick={onClose}>
            稍后处理
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!picked.length || resolve.isPending || noCandidate}
            title={noCandidate ? "当前没有可选候选" : !picked.length ? "请至少选择一项" : undefined}
            onClick={() =>
              resolve.mutate(undefined, {
                onError: (error) => {
                  if (isApiError(error) && error.status === 409) {
                    void qc.invalidateQueries({queryKey:["w-inputs"]});
                    void qc.invalidateQueries({queryKey:["w-run"]});
                  }
                },
                onSuccess: () => {
                  toast({ tone: "ok", text: "已提交确认，处理将以新的执行轮次继续" });
                  onClose();
                },
              })
            }
          >
            {resolve.isPending ? "正在提交" : "提交确认"}
          </button>
        </>
      }
    >
      <p className="text-[13px] leading-relaxed text-ink">{request.question}</p>
      <InputRequestEvidence request={request} sources={sources} onLocate={onLocate} />
      <p className="mt-1 text-[11px] text-ink-3">
        请求版本 v{request.request_version} · 答案结构 v{request.schema_version}。提交时会带上你所见的解析版本，服务端会核对期间是否有变化。
      </p>

      {noCandidate ? (
        <div className="mt-3">
          <ErrorBar
            tone="warn"
            text="这次没有找到有来源的候选。可以重新识别原件、新建一个任务，或者取消这次处理；不会生成空译本。"
          />
        </div>
      ) : request.kind === "document_boundary" && catalog.data ? (
        <div className="mt-3">
          <label className="label">单据用途</label>
          <DocumentTypeSelect items={catalog.data.items.filter(item => request.options.some(option => option.option_id === item.code))} value={picked[0] ?? ""} onChange={code => toggle(code)} disabled={resolve.isPending} />
          {request.options.filter(option => !catalog.data.items.some(item => item.code === option.option_id)).map(option => <label key={option.option_id} className="mt-2 flex items-center gap-2 text-[13px]"><input type="radio" checked={picked.includes(option.option_id)} onChange={() => toggle(option.option_id)} disabled={resolve.isPending} />{option.label}</label>)}
          <CargoProfileSelect items={catalog.data.cargo_profiles} value={cargoProfiles} onChange={codes => { setCargoProfiles(codes); resolve.renewKey(); }} disabled={resolve.isPending} />
        </div>
      ) : (
        <ul className="mt-3 space-y-1.5">
          {request.options.map((o) => (
            <li key={o.option_id} className={`rounded-ctl border p-2.5 ${picked.includes(o.option_id) ? "opt-on" : "border-line"}`}>
              <label className="flex cursor-pointer items-start gap-2">
                <input type={multi ? "checkbox" : "radio"} name="input-request-choice" disabled={resolve.isPending} className="mt-1" checked={picked.includes(o.option_id)} onChange={() => toggle(o.option_id)} />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] text-ink">{o.label}</span>
                  {o.detail && <span className="mt-0.5 block text-[12px] text-ink-2">{o.detail}</span>}
                  {o.excerpt && <span className="mt-1 block rounded-ctl bg-page px-2 py-1.5 text-[12px] leading-relaxed text-ink-2">{o.excerpt}</span>}
                </span>
              </label>
              {o.source_ref && (
                <button type="button" className="btn btn-ghost btn-sm mt-1" onClick={() => onLocate(o.source_ref!)}>
                  在原件中定位
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      <label className="mt-3 block">
        <span className="label">处理理由（可选）</span>
        <input className="input mt-1" value={reason} disabled={resolve.isPending} onChange={(e) => { setReason(e.target.value); resolve.renewKey(); }} placeholder="便于后续复核时理解这次选择" />
      </label>

      {resolve.error && <div className="mt-3"><ErrorBar text={errorText(resolve.error)} /></div>}
    </Dialog>
  );
}
