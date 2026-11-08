"""识别/复核产物提交事务内保存每Run每原件的短摘要，不依赖外部模型。"""
from uuid import UUID
from sqlalchemy import select,delete,update
from app.api.idempotency import request_digest
from app.documents import evidence_models as em
from app.documents.models import DocumentVersion
from app.management.models import RunDocumentSummary,SearchProjection,SearchFieldValue
from app.review import models as rm
from app.workflows.models import RunDocument
from app.exports.models import ExportSnapshot,ExportAttempt,ReviewConfirmation
from app.documents.extraction import ORIGIN_FIELD_KEYS,field_label


def summary_dependencies(db, run, snapshot=None):
    """引用真实版本与结果元数据；冻结快照只依赖其清单，不读取后来的处置。"""
    from app.management.versions import run_parse_ids
    if snapshot:
        return {"run_id":str(run.id),"snapshot_id":str(snapshot.id),"exported_at":snapshot.exported_at.isoformat(),"manifest":snapshot.manifest}
    parses=run_parse_ids(db,run)
    fields=list(db.scalars(select(em.FieldAssertion.id).join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id)
        .where(em.BusinessRevisionField.business_revision_id==run.current_business_revision_id,em.FieldAssertion.parse_artifact_id.in_(parses)).order_by(em.FieldAssertion.id)))
    reviews=list(db.scalars(select(em.FieldReviewEvent.id).where(em.FieldReviewEvent.business_revision_id==run.current_business_revision_id,
        em.FieldReviewEvent.field_assertion_id.in_(fields)).order_by(em.FieldReviewEvent.id)))
    from app.consignment.service import review_issues, bound_batch
    from app.documents.models import Case
    case = db.get(Case, run.case_id)
    issues = [(issue.id, issue.severity, issue.blocks_export) for issue in review_issues(db, run, case, strict_current=False)]
    batch = bound_batch(db, run, case, strict_current=False)
    dispositions=list(db.execute(select(rm.IssueDisposition.id,rm.IssueDisposition.action,rm.IssueDisposition.review_sequence).where(
        rm.IssueDisposition.issue_id.in_([row[0] for row in issues])).order_by(rm.IssueDisposition.id)))
    scopes=list(db.execute(select(rm.ResolvedTranslationScope.id,rm.ResolvedTranslationScope.fingerprint,rm.ResolvedTranslationScope.confirmed_by).where(
        rm.ResolvedTranslationScope.run_id==run.id,rm.ResolvedTranslationScope.business_revision_id==run.current_business_revision_id).order_by(rm.ResolvedTranslationScope.id)))
    translation=db.get(rm.TranslationRevision,run.current_translation_revision_id) if run.current_translation_revision_id else None
    decisions=list(db.scalars(select(em.SectionAssignmentDecision.id).where(em.SectionAssignmentDecision.business_revision_id==run.current_business_revision_id).order_by(em.SectionAssignmentDecision.id)))
    attempts=list(db.execute(select(ExportAttempt.id,ExportAttempt.status,ExportAttempt.error_summary,ExportAttempt.updated_at).where(ExportAttempt.run_id==run.id).order_by(ExportAttempt.id)))
    snapshots=list(db.execute(select(ExportSnapshot.id,ExportSnapshot.exported_at).where(ExportSnapshot.run_id==run.id).order_by(ExportSnapshot.id)))
    confirmations=list(db.scalars(select(ReviewConfirmation.id).where(ReviewConfirmation.run_id==run.id).order_by(ReviewConfirmation.id)))
    def rows(values):return [[str(value) if value is not None else None for value in row] for row in values]
    return {"run_id":str(run.id),"case_id":str(run.case_id),"target_language":run.target_language,"task_spec_hash":run.task_spec_hash,
        "comparison_batch_id": str(batch.id) if batch else None, "comparison_binding_id": str(run.current_comparison_binding_id) if batch else None,
        "translation_scope":run.task_spec.get("translation_scope"),"parse_artifact_ids":sorted(str(value) for value in parses),
        "business_revision_id":str(run.current_business_revision_id) if run.current_business_revision_id else None,
        "translation_revision_id":str(run.current_translation_revision_id) if run.current_translation_revision_id else None,
        "translation":{ "fingerprint":translation.dependency_fingerprint,"qa_status":translation.qa_status,"segments":translation.segment_ids,"scopes":translation.resolved_scope_ids} if translation else None,
        "fields":[str(value) for value in fields],"field_reviews":[str(value) for value in reviews],"section_decisions":[str(value) for value in decisions],
        "issues":rows(issues),"dispositions":rows(dispositions),"resolved_scopes":rows(scopes),"confirmations":[str(value) for value in confirmations],
        "export_attempts":rows(attempts),"snapshots":rows(snapshots),"status":run.status.value,"stage":run.current_stage,
        "failure_reason":run.failure_reason,"pause_explanation":run.pause_explanation}


def summary_fingerprint(db,run,snapshot=None):
    return request_digest(summary_dependencies(db,run,snapshot))


def fact_summary(fields,reviews):
    def state(field):
        event=reviews.get(field.id)
        return "人工" if field.is_human else "已确认" if event and event.action=="confirmed" else "无法判断" if event else "候选"
    return [f"{field_label(field.field_key,field.role)}：{(field.normalized_value or field.candidate_value or '')[:100]}（{state(field)}）" for field in fields[:12]]


def refresh_run_summaries(db, run):
    dependencies=summary_dependencies(db,run)
    fingerprint=request_digest(dependencies)
    documents=db.scalars(select(RunDocument).where(RunDocument.run_id==run.id).order_by(RunDocument.ordinal))
    for document in documents:
        source=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==document.document_id,DocumentVersion.version==document.document_version))
        from app.management.versions import run_parse
        parse=run_parse(db,run,document.document_id,document.document_version)
        logical=list(db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id==parse.id).order_by(em.LogicalDocument.ordinal))) if parse else []
        fields=list(db.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id)
            .where(em.BusinessRevisionField.business_revision_id==run.current_business_revision_id,em.FieldAssertion.parse_artifact_id==parse.id))) if parse and run.current_business_revision_id else []
        relevant=[f for f in fields if f.field_key in ORIGIN_FIELD_KEYS | {"consignee","shipper","buyer","port_of_loading","bill_number","invoice_number","cargo_description"} or f.role in {"consignee","shipper","buyer","origin","pol"}]
        reviews={event.field_assertion_id:event for event in db.scalars(select(em.FieldReviewEvent).where(
            em.FieldReviewEvent.business_revision_id==run.current_business_revision_id,em.FieldReviewEvent.field_assertion_id.in_([field.id for field in relevant]))
            .order_by(em.FieldReviewEvent.review_sequence,em.FieldReviewEvent.created_at,em.FieldReviewEvent.id))}
        facts=fact_summary(relevant,reviews)
        text="；".join([source.filename,f"原件 v{source.version}","、".join(x.title or x.type_candidate.value for x in logical),
            f"解析 {parse.id}，{parse.page_count or '未知'} 页" if parse else "尚未解析",f"目标 {run.target_language or '无译文'}，范围 {run.task_spec.get('translation_scope','不适用')}",
            f"业务 {run.current_business_revision_id or '无'}，译文 {run.current_translation_revision_id or '无'}",*facts,
            f"状态 {run.status.value}，{run.current_stage}",f"疑点 {len(dependencies['issues'])}，处置 {len(dependencies['dispositions'])}，交付 {len(dependencies['snapshots'])}",run.failure_reason or run.pause_explanation or ""])[:1800]
        saved=db.scalar(select(RunDocumentSummary).where(RunDocumentSummary.run_id==run.id,RunDocumentSummary.document_id==document.document_id,RunDocumentSummary.document_version==document.document_version))
        if saved is None:
            saved=RunDocumentSummary(tenant_id=run.tenant_id,case_id=run.case_id,run_id=run.id,document_id=document.document_id,document_version=document.document_version)
            db.add(saved)
        saved.parse_artifact_id=parse.id if parse else None
        saved.summary=text
        saved.state="ready" if parse else "stale"
        saved.dependency_fingerprint=fingerprint
        from datetime import UTC,datetime
        saved.created_at=datetime.now(UTC)
    refresh_search_projection(db,run)


def refresh_search_projection(db,run,snapshot=None):
    fingerprint=summary_fingerprint(db,run,snapshot)
    from app.management.versions import run_parse_ids
    old=list(db.scalars(select(SearchProjection.id).where(SearchProjection.run_id==run.id,
        SearchProjection.snapshot_id==snapshot.id if snapshot else SearchProjection.snapshot_id.is_(None))))
    if old:
        db.execute(delete(SearchFieldValue).where(SearchFieldValue.projection_id.in_(old)))
        db.execute(delete(SearchProjection).where(SearchProjection.id.in_(old)))
    parses=[UUID(s["parse_artifact_id"]) for s in snapshot.manifest["source_versions"]] if snapshot else run_parse_ids(db,run)
    business_id=UUID(snapshot.manifest["business_revision_id"]) if snapshot else run.current_business_revision_id
    query=select(em.FieldAssertion)
    query=query.where(em.FieldAssertion.id.in_([UUID(x) for x in snapshot.manifest["field_assertion_ids"]])) if snapshot else query.join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).where(em.BusinessRevisionField.business_revision_id==business_id)
    fields=list(db.scalars(query))
    if not snapshot:fields=[f for f in fields if f.parse_artifact_id in parses]
    events=list(db.scalars(select(em.FieldReviewEvent).where(em.FieldReviewEvent.field_assertion_id.in_([f.id for f in fields])).order_by(em.FieldReviewEvent.review_sequence,em.FieldReviewEvent.created_at,em.FieldReviewEvent.id)))
    if snapshot:events=[e for e in events if str(e.id) in snapshot.manifest["field_review_event_ids"]]
    else:events=[e for e in events if e.business_revision_id==business_id]
    reviews={e.field_assertion_id:e for e in events}
    documents=list(db.scalars(select(RunDocument).where(RunDocument.run_id==run.id)))
    for document in documents:
        source=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==document.document_id,DocumentVersion.version==document.document_version))
        parse=db.scalar(select(em.ParseArtifact).where(em.ParseArtifact.id.in_(parses),em.ParseArtifact.document_id==document.document_id,em.ParseArtifact.document_version==document.document_version)) if parses else None
        logical=list(db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id==parse.id))) if parse else []
        for logical_document in logical or [None]:
            local=[f for f in fields if logical_document and f.logical_document_id==logical_document.id]
            from app.documents.type_catalog import candidate_document_type
            doc_type=candidate_document_type(logical_document,parse) if logical_document else "unclassified"
            decisions=list(db.scalars(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.case_id==run.case_id,
                em.SectionAssignmentDecision.business_revision_id==business_id,em.SectionAssignmentDecision.logical_document_type.is_not(None)).order_by(em.SectionAssignmentDecision.created_at)))
            if snapshot:decisions=[d for d in decisions if str(d.id) in snapshot.manifest.get("section_assignment_ids",[])]
            section_ids=set(db.scalars(select(em.DocumentSection.id).where(em.DocumentSection.logical_document_id==logical_document.id))) if logical_document else set()
            typed=[d for d in decisions if d.section_id in section_ids]
            if typed:doc_type=typed[-1].logical_document_type
            projection=SearchProjection(tenant_id=run.tenant_id,case_id=run.case_id,run_id=run.id,snapshot_id=snapshot.id if snapshot else None,
                document_id=document.document_id,document_version=document.document_version,parse_artifact_id=parse.id if parse else None,
                logical_document_id=logical_document.id if logical_document else None,business_revision_id=business_id,filename=source.filename,
                doc_type={"supporting":"other","unknown":"unclassified"}.get(doc_type,doc_type),summary="；".join([logical_document.title or doc_type,f"页 {logical_document.page_start or '?'}–{logical_document.page_end or '?'}",*fact_summary(local,reviews)]) if logical_document else source.filename,
                uploaded_at=source.created_at,uploaded_by=source.uploaded_by,state="ready" if parse else "stale",
                dependency_fingerprint=fingerprint)
            db.add(projection);db.flush()
            for field in local:
                review=reviews.get(field.id)
                db.add(SearchFieldValue(tenant_id=run.tenant_id,projection_id=projection.id,field_assertion_id=field.id,role=field.role,
                    field_key=field.field_key,normalized_value=field.normalized_value or field.candidate_value or "",
                    state="confirmed" if field.is_human or review and review.action=="confirmed" else "unknown" if review else "candidate"))


def invalidate_search(db,run):
    db.execute(update(SearchProjection).where(SearchProjection.run_id==run.id,SearchProjection.snapshot_id.is_(None)).values(state="stale"))
    db.execute(update(RunDocumentSummary).where(RunDocumentSummary.run_id==run.id).values(state="stale"))
