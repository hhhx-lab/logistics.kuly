import { languageName, languageCode } from "../../i18n/languages";
import { useInterfaceText } from "../settings/useInterfaceText";
// /review 全屏深链。URL 只放契约 EvidenceTarget 的 ID 与版本，
// 不放原件路径、原文或带签名的下载地址。
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useSession } from "../../stores/session";
import type { EvidenceTarget } from "../../api/types";
import { EmptyState } from "../../components/Status";
import { ReviewWorkspace, reviewTargetKey, type ReviewTargetSpec } from "./ReviewWorkspace";

export function buildReviewSearch(spec: { caseId: string; runId?: string | null; snapshotId?: string | null; target?: EvidenceTarget | null; intent?: "full" | "quick" }) {
  const p = new URLSearchParams();
  p.set("case_id", spec.caseId);
  if (spec.runId) p.set("run_id", spec.runId);
  if (spec.snapshotId) p.set("snapshot_id", spec.snapshotId);
  if (spec.intent) p.set("intent", spec.intent);
  const t = spec.target;
  if (t) {
    if (t.document_id) p.set("document_id", t.document_id);
    if (t.document_version) p.set("document_version", String(t.document_version));
    if (t.parse_artifact_id) p.set("parse_artifact_id", t.parse_artifact_id);
    if (t.logical_document_id) p.set("logical_document_id", t.logical_document_id);
    if (t.section_id) p.set("section_id", t.section_id);
    if (t.source_anchor_id) p.set("anchor_id", t.source_anchor_id);
    if (t.issue_id) p.set("issue_id", t.issue_id);
    if (t.field_id) p.set("field_id", t.field_id);
    if (t.source_segment_id) p.set("source_segment_id", t.source_segment_id);
    if (t.business_revision_id) p.set("business_revision_id", t.business_revision_id);
    if (t.translation_revision_id) p.set("translation_revision_id", t.translation_revision_id);
    if (t.translation_segment_id) p.set("translation_segment_id", t.translation_segment_id);
    if (t.preview_version) p.set("preview_version", String(t.preview_version));
    if (t.target_language) p.set("target_language", t.target_language);
    if (t.line_item_id) p.set("line_item_id", t.line_item_id);
    if (t.compatibility_id) p.set("compatibility_id", t.compatibility_id);
  }
  return `?${p.toString()}`;
}

export function ReviewPage() {
  const uiText = useInterfaceText();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const evidenceRequest = useSession(s => s.evidenceRequest);
  useEffect(() => { if (!evidenceRequest) return; const target=evidenceRequest.target; setParams(new URLSearchParams(buildReviewSearch({caseId:target.case_id,runId:target.run_id,snapshotId:target.export_snapshot_id,target,intent:evidenceRequest.intent==="review"?"full":"quick"})), { replace: true }); useSession.getState().clearEvidence(); }, [evidenceRequest, setParams]);
  const [viewport,setViewport]=useState(window.innerWidth);
  useEffect(()=>{const resize=()=>setViewport(window.innerWidth);window.addEventListener("resize",resize);return()=>window.removeEventListener("resize",resize);},[]);

  const spec = useMemo<ReviewTargetSpec | null>(() => {
    const caseId = params.get("case_id");
    if (!caseId) return null;
    const documentId = params.get("document_id");
    const target: EvidenceTarget | null = documentId
      ? {
          case_id: caseId,
          document_id: documentId,
          document_version: Number(params.get("document_version") ?? 1),
          parse_artifact_id: params.get("parse_artifact_id") ?? undefined,
          logical_document_id: params.get("logical_document_id") ?? undefined,
          section_id: params.get("section_id") ?? undefined,
          source_anchor_id: params.get("anchor_id") ?? undefined,
          issue_id: params.get("issue_id") ?? undefined,
          field_id: params.get("field_id") ?? undefined,
          source_segment_id: params.get("source_segment_id") ?? undefined,
          business_revision_id: params.get("business_revision_id") ?? undefined,
          translation_revision_id: params.get("translation_revision_id") ?? undefined,
          translation_segment_id: params.get("translation_segment_id") ?? undefined,
          preview_version: params.has("preview_version") ? Number(params.get("preview_version")) : undefined,
          target_language: languageCode(params.get("target_language")),
          line_item_id: params.get("line_item_id") ?? undefined,
          compatibility_id: params.get("compatibility_id") ?? undefined,
          run_id: params.get("run_id") ?? undefined,
          export_snapshot_id: params.get("snapshot_id") ?? undefined,
        }
      : null;
    return {
      caseId,
      runId: params.get("run_id"),
      snapshotId: params.get("snapshot_id"),
      target,
      intent: params.get("intent") === "quick" ? "quick" : "full",
    };
  }, [params]);

  if (!spec) {
    return (
      <div className="flex h-full items-center justify-center">
        <EmptyState
          icon="shield"
          title="这个校验链接缺少必要信息"
          description="无法确定要打开哪一票材料。可以回到对话或单据管理台，从任务卡、来源卡或记录行重新进入。"
          action={
            <div className="flex gap-2">
              <button type="button" className="btn btn-primary" onClick={() => navigate("/conversation")}>
                返回对话
              </button>
              <button type="button" className="btn btn-secondary" onClick={() => navigate("/documents")}>
                返回单据管理台
              </button>
            </div>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <p className="shrink-0 border-b border-line px-6 py-1 text-[11px] text-ink-3">{uiText("点击字段或疑点查看原文位置；历史交付仅供查阅。", "Select a field or issue to locate its source. Historical exports are read-only.")}</p>
      <ReviewWorkspace
        key={reviewTargetKey(spec)}
        spec={spec}
        width={viewport - 48}
        fullscreen
        onClose={() => navigate(-1)}
        onBack={{ label: "返回", onSelect: () => navigate(-1) }}
        onUploadMore={(caseId, replace) => {
          // 上传统一走对话的上传向导，替换原件时带上被替换的版本
          const q = new URLSearchParams({ case_id: caseId, upload: "1" });
          if (replace?.replaceDocumentId && replace.expectedVersion) {
            q.set("replace_document_id", replace.replaceDocumentId);
            q.set("expected_document_version", String(replace.expectedVersion));
          }
          if (replace?.relatedIssueId && replace.expectedIssueVersion) {
            q.set("related_issue_id", replace.relatedIssueId);
            q.set("expected_issue_version", String(replace.expectedIssueVersion));
          }
          navigate(`/conversation?${q.toString()}`);
        }}
        onOpenRun={runId => setParams(new URLSearchParams(buildReviewSearch({ caseId: spec.caseId, runId, intent: "full" })), { replace: true })}
        onSwitchToWorking={() => {
          // 明确请求当前版：去掉快照并重新鉴权加载
          const next = new URLSearchParams({case_id: spec.caseId, intent: "full"});
          setParams(next, { replace: true });
        }}
      />
    </div>
  );
}
