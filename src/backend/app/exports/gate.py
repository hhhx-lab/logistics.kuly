"""确认、导出前检和快照提交共用的同步门禁。异步 API 通过 run_sync 调用。"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID
from sqlalchemy import select,or_
from app.documents import evidence_models as em
from app.documents.models import DocumentVersion, Case
from app.exports.models import ReviewConfirmation
from app.review import models as rm
from app.workflows.models import Run,RunDocument,StepExecution,StepStatus

from app.exports.render import TEMPLATE_VERSION
from app.review.issue_state import issue_blocks_delivery, issue_resolved


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str,separators=(",",":")).encode()).hexdigest()


def review_gate(db,run:Run,*,can_confirm=True):
    gaps=[];items=[];warnings=[];unconfirmed_fields=0;blocking_issues=0
    from app.config import get_settings
    from app.review.publications import publication_export_state
    publication_state = publication_export_state(run.task_spec.get("reviewed_rule_publications"), get_settings())
    for publication in publication_state:
        if publication["status"] in {"withdrawn", "expired"}:
            gaps.append({"item": "规则依据已失效", "detail": "本任务引用的已审核规则版本已撤回或到期，请用当前依据创建新任务后交付"})
    pending_external=db.scalar(select(StepExecution.id).where(StepExecution.run_id==run.id,
        StepExecution.status.in_([StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE])).limit(1))
    if pending_external:gaps.append({"item":"外部请求","detail":"存在尚未核查的外部请求，必须核对原回执后才能确认或导出"})
    source_versions=[];pairings=[];field_ids=[];review_ids=[];issue_ids=[];disposition_ids=[];check_ids=[];field_values=[]
    documents=list(db.scalars(select(RunDocument).where(RunDocument.run_id==run.id).order_by(RunDocument.ordinal)))
    revision=db.get(rm.TranslationRevision,run.current_translation_revision_id) if run.current_translation_revision_id else None
    if run.workflow_type!="translate_review":gaps.append({"item":"任务类型","detail":"只读识别不提供翻译确认或导出"})
    if not revision:gaps.append({"item":"译文","detail":"尚无当前译文版"})
    elif revision.qa_status not in {"passed","warning"} or revision.qa_summary.get("semantic_status") not in {"passed", "evaluated_candidates_only"}:gaps.append({"item":"翻译质量","detail":"译文检查尚未完成"})
    if revision:
        if revision.is_synthetic:gaps.append({"item":"翻译质量","detail":"测试模型结果不可正式导出"})
        if set(revision.qa_summary.get("expected_source_ids",[])) != set(revision.qa_summary.get("actual_source_ids",[])):gaps.append({"item":"内容完整性","detail":"确认范围仍有未生成的译段"})
        segments=list(db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]))))
        if any(not segment.translated_text.strip() for segment in segments):gaps.append({"item":"内容完整性","detail":"存在空译段，请先修订"})
    if not can_confirm:gaps.append({"item":"权限","detail":"当前账号没有复核确认权限"})
    from app.consignment.service import review_checks, review_issues, bound_batch
    case = db.get(Case, run.case_id)
    checks = review_checks(db, run, case)
    check_ids=[str(x.id) for x in checks]
    if not checks:gaps.append({"item":"单据检查","detail":"规则尚未运行"})
    issues = review_issues(db, run, case)
    batch = bound_batch(db, run, case)
    if run.workflow_type == "translate_review" and run.workflow_version >= 2 and batch is None:
        gaps.append({"item": "同票核对", "detail": "当前核对缺失或已过期，请完成当前材料核对后重新确认"})
    issue_rows=[]
    for issue in issues:
        disposition=db.scalar(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id==issue.id).order_by(rm.IssueDisposition.review_sequence.desc(),rm.IssueDisposition.created_at.desc(),rm.IssueDisposition.id.desc()).limit(1))
        issue_ids.append(str(issue.id))
        if disposition:disposition_ids.append(str(disposition.id))
        resolved=issue_resolved(issue,disposition)
        target=next((s.get("target",s) for s in issue.sources if s.get("document_id") or s.get("target",{}).get("document_id")),None)
        blocked=issue_blocks_delivery(issue,disposition)
        if blocked:
            blocking_issues+=1
            gaps.append({"item":issue.title,"detail":issue.description,**({"target":target} if target else {})})
        elif not resolved:
            warnings.append({"label":issue.title,"state":"note","detail":issue.description})
        issue_rows.append({"id":str(issue.id),"severity":issue.severity,"blocks_export":blocked,"resolved":resolved,
            "description":issue.description,"source_values":issue.sources,"disposition_id":str(disposition.id) if disposition else None})
    for doc in documents:
        version=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==doc.document_id,DocumentVersion.version==doc.document_version))
        source={"document_id":str(doc.document_id),"document_version":doc.document_version,"case_id":str(run.case_id),
            "filename":version.filename,"mime":version.mime,"size_bytes":version.size_bytes,"uploaded_by":str(version.uploaded_by),"uploaded_at":version.created_at.isoformat()}
        target={"case_id":str(run.case_id),"run_id":str(run.id),"document_id":str(doc.document_id),"document_version":doc.document_version}
        parse_ids=[UUID(x) for x in revision.parse_artifact_ids] if revision else []
        parse=db.scalar(select(em.ParseArtifact).where(em.ParseArtifact.document_id==doc.document_id,
            em.ParseArtifact.document_version==doc.document_version,em.ParseArtifact.id.in_(parse_ids)).limit(1)) if parse_ids else None
        latest=db.scalar(select(em.ParseArtifact).where(em.ParseArtifact.document_id==doc.document_id,
            em.ParseArtifact.document_version==doc.document_version,em.ParseArtifact.quality!=em.ParseQuality.FAILED).order_by(em.ParseArtifact.created_at.desc()).limit(1))
        adopted_parses = set(db.scalars(select(em.FieldAssertion.parse_artifact_id).join(em.BusinessRevisionField,
            em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).join(em.ParseArtifact,
            em.ParseArtifact.id==em.FieldAssertion.parse_artifact_id).where(
            em.BusinessRevisionField.business_revision_id==run.current_business_revision_id,
            em.ParseArtifact.case_id==run.case_id,em.ParseArtifact.document_id==doc.document_id,
            em.ParseArtifact.document_version==doc.document_version).distinct()))
        valid_pair = bool(parse and parse.quality!=em.ParseQuality.FAILED and (
            adopted_parses=={parse.id} if adopted_parses else latest and parse.id==latest.id))
        if not valid_pair:gaps.append({"item":version.filename,"detail":"译文解析配对过期或尚未形成","target":target})
        source["parse_artifact_id"]=str(parse.id) if parse else None
        source_versions.append(source)
        if not parse:continue
        target["parse_artifact_id"]=str(parse.id)
        fields=list(db.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id)
            .where(em.BusinessRevisionField.business_revision_id==run.current_business_revision_id,em.FieldAssertion.parse_artifact_id==parse.id).order_by(em.FieldAssertion.id)))
        for field in fields:
            field_ids.append(str(field.id))
            review=db.scalar(select(em.FieldReviewEvent).where(em.FieldReviewEvent.field_assertion_id==field.id,
                em.FieldReviewEvent.business_revision_id==run.current_business_revision_id).order_by(em.FieldReviewEvent.review_sequence.desc(),em.FieldReviewEvent.created_at.desc(),em.FieldReviewEvent.id.desc()).limit(1))
            if review:review_ids.append(str(review.id))
            field_values.append({"id":str(field.id),"value":field.normalized_value or field.candidate_value,"unit":field.unit,"role":field.role,"human":field.is_human,"basis":field.basis,"source":field.source_text,"review_status":review.action if review else "candidate"})
            if not field.is_human and (not review or review.action!="confirmed"):
                unconfirmed_fields+=1
        scope=db.scalar(select(rm.ResolvedTranslationScope).where(rm.ResolvedTranslationScope.id.in_([UUID(x) for x in revision.resolved_scope_ids]),rm.ResolvedTranslationScope.document_id==doc.document_id).limit(1)) if revision else None
        compatibility=db.scalar(select(rm.TranslationCompatibility).where(rm.TranslationCompatibility.run_id==run.id,
            rm.TranslationCompatibility.business_revision_id==run.current_business_revision_id,rm.TranslationCompatibility.translation_revision_id==revision.id).limit(1)) if revision and revision.business_revision_id!=run.current_business_revision_id else None
        preview=db.scalar(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id==run.id,rm.PreviewArtifact.document_id==doc.document_id,
            rm.PreviewArtifact.document_version==doc.document_version,rm.PreviewArtifact.business_revision_id==run.current_business_revision_id,
            rm.PreviewArtifact.translation_revision_id==revision.id,rm.PreviewArtifact.status=="valid").order_by(rm.PreviewArtifact.created_at.desc()).limit(1)) if revision else None
        if revision and revision.business_revision_id!=run.current_business_revision_id and not compatibility:gaps.append({"item":"版本配对","detail":"业务版与译文版不一致且没有有效兼容记录","target":target})
        if not scope:gaps.append({"item":"翻译范围","detail":"原件尚无冻结翻译范围","target":target})
        if not preview or not preview.reading_html or not preview.segment_mappings:gaps.append({"item":"译文预览","detail":"每份原件必须有当前配对的有效译文 HTML 预览","target":target})
        pairings.append({"document_id":str(doc.document_id),"document_version":doc.document_version,"parse_artifact_id":str(parse.id),
            "resolved_scope_id":str(scope.id) if scope else None,"resolved_scope_fingerprint":scope.fingerprint if scope else None,
            "translation_revision_id":str(revision.id) if revision else None,"compatibility_id":str(compatibility.id) if compatibility else None,
            "preview_id":str(preview.id) if preview else None,"preview_version":preview.preview_version if preview else None})
    assignments=list(db.scalars(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.case_id==run.case_id,
        em.SectionAssignmentDecision.business_revision_id==run.current_business_revision_id).order_by(em.SectionAssignmentDecision.id)))
    rule_inputs=list(db.scalars(select(rm.RuleInputDecision).where(rm.RuleInputDecision.case_id==run.case_id,rm.RuleInputDecision.business_revision_id==run.current_business_revision_id).order_by(rm.RuleInputDecision.id)))
    frozen_parse_ids=[UUID(s["parse_artifact_id"]) for s in source_versions if s.get("parse_artifact_id")]
    line_ids=list(db.scalars(select(em.LineItem.id).where(em.LineItem.parse_artifact_id.in_(frozen_parse_ids))))
    line_matches=list(db.scalars(select(em.LineMatchEdge).where(em.LineMatchEdge.case_id==run.case_id,
        em.LineMatchEdge.left_line_id.in_(line_ids),em.LineMatchEdge.right_line_id.in_(line_ids),
        em.LineMatchEdge.state!="rejected").order_by(em.LineMatchEdge.id)))
    manifest={"task_spec_hash":run.task_spec_hash,"source_versions":source_versions,
        "business_revision_id":str(run.current_business_revision_id) if run.current_business_revision_id else None,
        "translation_revision_id":str(revision.id) if revision else None,"pairings":pairings,
        "field_assertion_ids":sorted(field_ids),"field_review_event_ids":sorted(review_ids),
        "field_values":field_values,"rule_input_decision_ids":[str(x.id) for x in rule_inputs],"section_assignment_ids":[str(x.id) for x in assignments],
        "line_match_ids":[str(x.id) for x in line_matches],
        "line_matches":[{"id":str(x.id),"left":str(x.left_line_id),"right":str(x.right_line_id),"state":x.state,"relation":x.relation,"basis":x.basis,"by":str(x.decided_by) if x.decided_by else None,"at":x.created_at.isoformat()} for x in line_matches],
        "rule_check_ids":check_ids,"rule_results":[{"id":str(c.id),"status":c.status,"fingerprint":c.input_fingerprint} for c in checks],
        "issue_ids":issue_ids,"issue_disposition_ids":disposition_ids,"issue_states":issue_rows,
        "resolved_scope_ids":revision.resolved_scope_ids if revision else [],"preview_ids":[p["preview_id"] for p in pairings if p["preview_id"]],
        "qa_status":revision.qa_status if revision else None,"qa_summary":revision.qa_summary if revision else None,
        "target_language":run.target_language,"translation_scope":run.task_spec.get("translation_scope"),
        "output_formats":run.task_spec.get("output_formats",[]),"template_version":TEMPLATE_VERSION,
        "selected_section_refs":run.task_spec.get("selected_section_refs",[]),"rule_pack_version":run.task_spec.get("rule_pack_version"),
        "glossary_version":run.task_spec.get("glossary_version")}
    if run.task_spec.get("jurisdiction_inputs") is not None:
        manifest["jurisdiction_inputs"]=run.task_spec["jurisdiction_inputs"]
    if publication_state:
        manifest["reviewed_rule_publications"] = run.task_spec["reviewed_rule_publications"]
        manifest["publication_export_state"] = publication_state
    if batch:
        from app.consignment.service import dispositions_for
        comparison_issues=list(db.scalars(select(rm.Issue).where(rm.Issue.comparison_batch_id==batch.id).order_by(rm.Issue.id)))
        comparison_decisions=dispositions_for(db,comparison_issues)
        manifest["comparison"] = {"batch_id":str(batch.id), "scope_revision_id":str(batch.scope_revision_id),
            "binding_id":str(run.current_comparison_binding_id), "input_fingerprint":batch.input_fingerprint,
            "manifest":batch.manifest,"issue_ids":[str(issue.id) for issue in comparison_issues],
            "issue_disposition_ids":sorted(str(decision.id) for decision in comparison_decisions.values())}
    fp=fingerprint(manifest)
    confirmation=db.scalar(select(ReviewConfirmation).where(ReviewConfirmation.run_id==run.id,ReviewConfirmation.review_fingerprint==fp))
    note="；".join(c.explanation for c in checks if c.status=="not_covered") or "当前规则包未评估全部目的国法规或证书要求。"
    items=[{"label":"任务与来源配对","state":"gap" if gaps else "ok","detail":f"固定 {len(documents)} 份原件，业务与译文版本按逐源校验"},
           {"label":"未解决红项","state":"gap" if blocking_issues else "ok","detail":f"{blocking_issues} 条未解决红项；其他疑点仅提示，不阻断导出"},
           *warnings]
    if unconfirmed_fields:
        items.append({"label":"候选字段提示","state":"note","detail":f"{unconfirmed_fields} 个字段尚未逐项人工确认，保留候选标记；不要求逐项确认才能导出"})
    items.append({"label":"规则未覆盖说明","state":"note","detail":note})
    return {"run_id":str(run.id),"confirmable":not gaps,"review_fingerprint":fp,"output_formats":run.task_spec.get("output_formats",[]),
        "source_versions":source_versions,"pairing_label":f"业务版 {run.current_business_revision_id} ／译文版 {revision.revision_no if revision else '未形成'}",
        "gaps":gaps,"items":items,"not_covered_note":note,"current_review_confirmation_id":str(confirmation.id) if confirmation and not gaps else None,
        "confirmation_status":"valid" if confirmation and not gaps else "none","_manifest":manifest}
