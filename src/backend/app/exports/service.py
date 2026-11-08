"""确认、导出尝试及成功快照视图。"""
from __future__ import annotations
from datetime import UTC,datetime
from uuid import UUID
from sqlalchemy import select
from app.api.errors import InvalidRequest,NotFoundOrForbidden
from app.documents.models import Case,DocumentVersion
from app.documents.retention import touch_material_event
from app.exports.gate import review_gate
from app.exports.models import ReviewConfirmation,ExportAttempt,ExportAttemptItem,ExportSnapshot,ExportArtifact
from app.workflows import commands as wc
from app.workflows.models import RunStatus


async def checklist(db,run,principal):
    return await db.run_sync(lambda session:review_gate(session,run,can_confirm="export_confirm" in principal.permissions))


def public_checklist(value):return {k:v for k,v in value.items() if not k.startswith("_")}


async def confirm(db,principal,settings,run_id,body):
    wc.require_permission(principal,"export_confirm")
    run,case=await wc.load_run(db,principal,settings,run_id,write=True)
    gate=await checklist(db,run,principal)
    if gate["review_fingerprint"]!=body.expected_review_fingerprint or run.current_business_revision_id!=body.expected_business_revision_id or run.current_translation_revision_id!=body.expected_translation_revision_id:
        raise wc.conflict("REVIEW_STALE","复核内容已变化，请重新核对",**public_checklist(gate))
    if not gate["confirmable"]:raise wc.conflict("REVIEW_BLOCKED","复核仍有缺项，不能确认",**public_checklist(gate))
    existing=(await db.execute(select(ReviewConfirmation).where(ReviewConfirmation.run_id==run.id,
        ReviewConfirmation.review_fingerprint==gate["review_fingerprint"]))).scalar_one_or_none()
    if existing:return confirmation_view(existing)
    confirmation=ReviewConfirmation(tenant_id=run.tenant_id,case_id=case.id,run_id=run.id,task_spec_hash=run.task_spec_hash,
        review_fingerprint=gate["review_fingerprint"],business_revision_id=run.current_business_revision_id,
        translation_revision_id=run.current_translation_revision_id,pairing_manifest={"pairings":gate["_manifest"]["pairings"]},
        review_manifest=gate["_manifest"],output_formats=run.task_spec["output_formats"],confirmed_by=principal.user_id)
    db.add(confirmation);await db.flush()
    run.status,run.current_stage=RunStatus.READY_TO_EXPORT,"复核已确认，等待导出"
    await wc.append_run_event(db,run,message="复核确认已登记，当前整组版本可导出",kind="human")
    touch_material_event(case,settings)
    return confirmation_view(confirmation)


def confirmation_view(confirmation):
    return {"confirmation_id":str(confirmation.id),"run_id":str(confirmation.run_id),"task_spec_hash":confirmation.task_spec_hash,
        "review_fingerprint":confirmation.review_fingerprint,"source_versions":confirmation.review_manifest["source_versions"],
        "business_revision_id":str(confirmation.business_revision_id),"translation_revision_id":str(confirmation.translation_revision_id),
        "resolved_scope_refs":confirmation.pairing_manifest["pairings"],"output_formats":confirmation.output_formats,
        "confirmed_by":str(confirmation.confirmed_by),"confirmed_at":confirmation.confirmed_at,"valid_for_current_working":True}


async def check_export_pairing(db,principal,settings,run_id,body):
    wc.require_permission(principal,"export_confirm")
    run,case=await wc.load_run(db,principal,settings,run_id,write=True)
    gate=await checklist(db,run,principal)
    confirmation=await db.get(ReviewConfirmation,body.confirmation_id)
    if confirmation is None or confirmation.run_id!=run.id:raise NotFoundOrForbidden()
    if not gate["confirmable"] or confirmation.review_fingerprint!=gate["review_fingerprint"] or body.expected_review_fingerprint!=gate["review_fingerprint"] or body.expected_business_revision_id!=run.current_business_revision_id or body.expected_translation_revision_id!=run.current_translation_revision_id:
        raise wc.conflict("REVIEW_STALE","当前内容与复核确认不一致，请重新核对",**public_checklist(gate))
    return run,case,confirmation


async def start_export(db,principal,settings,run_id,body):
    run,case,confirmation=await check_export_pairing(db,principal,settings,run_id,body)
    previous=(await db.execute(select(ExportAttempt).where(ExportAttempt.run_id==run.id,
        ExportAttempt.review_fingerprint==confirmation.review_fingerprint).order_by(ExportAttempt.created_at.desc()).limit(1))).scalar_one_or_none()
    if previous:
        kinds=set((await db.execute(select(ExportAttemptItem.kind).where(ExportAttemptItem.attempt_id==previous.id))).scalars())
        same_plan=kinds==(set(confirmation.output_formats)|({"source_pdf"} if body.include_source_pdf else set()))
        if previous.status in {"queued","rendering","checking","succeeded"} and same_plan:return await attempt_view(db,previous)
        if previous.status!="succeeded":raise wc.conflict("EXPORT_ATTEMPT_RETRY_REQUIRED","上次导出尚未结清，请先查看或重试原导出尝试",attempt_id=str(previous.id))
    if run.status not in {RunStatus.READY_TO_EXPORT,RunStatus.EXPORTED}:raise wc.conflict("RUN_BUSY","当前任务不能开始导出")
    run.current_epoch+=1;run.execution_fence+=1
    run.status,run.current_stage=RunStatus.EXPORTING,"生成确认稿"
    attempt=ExportAttempt(tenant_id=run.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,
        review_fingerprint=confirmation.review_fingerprint,status="queued",execution_epoch=run.current_epoch,created_by=principal.user_id)
    db.add(attempt);await db.flush()
    ordinal=0
    for source in confirmation.review_manifest["source_versions"]:
        base=source["filename"].rsplit(".",1)[0]
        for kind in confirmation.output_formats:
            db.add(ExportAttemptItem(tenant_id=run.tenant_id,case_id=case.id,attempt_id=attempt.id,
                document_id=UUID(source["document_id"]),document_version=source["document_version"],ordinal=ordinal,kind=kind,
                filename=f"{base}_{run.target_language}_译本.{kind}",status="pending"));ordinal+=1
        if body.include_source_pdf:
            db.add(ExportAttemptItem(tenant_id=run.tenant_id,case_id=case.id,attempt_id=attempt.id,
                document_id=UUID(source["document_id"]),document_version=source["document_version"],ordinal=ordinal,
                kind="source_pdf",filename=f"{base}_识别稿.pdf",status="pending"));ordinal+=1
    await wc.enqueue_epoch(db,run,purpose="export",parse_artifact_ids=[p["parse_artifact_id"] for p in confirmation.review_manifest["pairings"]])
    await wc.append_run_event(db,run,message="已安排译本及所选识别稿文件生成",event_type="export_step",kind="export",payload={"attempt_id":str(attempt.id)})
    return await attempt_view(db,attempt)


async def retry_export(db,principal,settings,run_id,attempt_id,body):
    attempt=await db.get(ExportAttempt,attempt_id)
    if attempt is None or attempt.run_id!=run_id:raise NotFoundOrForbidden()
    # 重试前置复用同一确认；前端无须重复发送确认 ID。
    body.confirmation_id=attempt.confirmation_id
    run,case,confirmation=await check_export_pairing(db,principal,settings,run_id,body)
    if attempt.status=="succeeded":return await attempt_view(db,attempt)
    if attempt.status!="failed_retryable":raise wc.conflict("EXPORT_RETRY_NOT_ALLOWED","该导出尝试当前不能安全重试")
    items=(await db.execute(select(ExportAttemptItem).where(ExportAttemptItem.attempt_id==attempt.id).order_by(ExportAttemptItem.ordinal))).scalars().all()
    if any(item.kind=="report" for item in items):
        # 旧失败尝试保留身份与失败事件，恢复时升级交付清单；报告暂存交给生命周期清理。
        for item in items:
            if item.kind=="report":await db.delete(item)
            elif item.kind in {"docx","pdf"}:
                base=next(s["filename"].rsplit(".",1)[0] for s in confirmation.review_manifest["source_versions"] if s["document_id"]==str(item.document_id))
                item.filename=f"{base}_{run.target_language}_译本.{item.kind}"
        ordinal=max(item.ordinal for item in items)+1
        for source in confirmation.review_manifest["source_versions"]:
            if not any(item.kind=="source_pdf" and str(item.document_id)==source["document_id"] for item in items):
                db.add(ExportAttemptItem(tenant_id=run.tenant_id,case_id=run.case_id,attempt_id=attempt.id,
                    document_id=UUID(source["document_id"]),document_version=source["document_version"],ordinal=ordinal,
                    kind="source_pdf",filename=f"{source['filename'].rsplit('.',1)[0]}_识别稿.pdf",status="pending"));ordinal+=1
        await db.flush()
    run.current_epoch+=1;run.execution_fence+=1;run.status,run.current_stage=RunStatus.EXPORTING,"重试原确认稿生成"
    attempt.status,attempt.execution_epoch,attempt.error_summary="queued",run.current_epoch,None
    attempt.updated_at=datetime.now(UTC)
    await wc.enqueue_epoch(db,run,purpose="export",parse_artifact_ids=[p["parse_artifact_id"] for p in confirmation.review_manifest["pairings"]])
    await wc.append_run_event(db,run,message="已安排同一导出尝试安全重试，重新核验暂存产物",kind="export",event_type="export_step")
    return await attempt_view(db,attempt)


async def attempt_view(db,attempt):
    items=(await db.execute(select(ExportAttemptItem).where(ExportAttemptItem.attempt_id==attempt.id).order_by(ExportAttemptItem.ordinal))).scalars().all()
    return {"attempt_id":str(attempt.id),"run_id":str(attempt.run_id),"confirmation_id":str(attempt.confirmation_id),
        "review_fingerprint":attempt.review_fingerprint,"status":attempt.status,"artifact_steps":[{"step_id":str(x.id),"label":x.filename,"kind":x.kind,
        "source_filename":x.filename if x.document_id else None,"status":x.status} for x in items],"error_summary":attempt.error_summary,
        "retryable_step_ids":[str(x.id) for x in items if x.status=="failed" and attempt.status=="failed_retryable"],
        "snapshot_id":str(attempt.snapshot_id) if attempt.snapshot_id else None,"updated_at":attempt.updated_at}


async def snapshot_view(db,snapshot):
    manifest=snapshot.manifest
    case=await db.get(Case,snapshot.case_id)
    artifacts=(await db.execute(select(ExportArtifact).where(ExportArtifact.snapshot_id==snapshot.id))).scalars().all()
    def artifact_view(a):return {"artifact_id":str(a.id),"kind":a.kind,"filename":a.filename,"size_bytes":a.size_bytes}
    sources=[]
    for source in manifest["source_versions"]:
        matches=[a for a in artifacts if str(a.document_id)==source["document_id"] and a.document_version==source["document_version"]]
        sources.append({"source":source,"artifacts":[artifact_view(a) for a in matches],
            "untranslated_note":"译本仅包含指定范围；识别稿保留原件全部已识别内容" if manifest["translation_scope"]!="all" else None})
    report=next((a for a in artifacts if a.kind=="report"),None)
    from app.identity.models import User
    exporter=await db.get(User,snapshot.exported_by)
    return {"snapshot_id":str(snapshot.id),"run_id":str(snapshot.run_id),"case_id":str(case.id),"case_name":case.name,
        "target_language":manifest["target_language"],"translation_scope":manifest["translation_scope"],"exported_at":snapshot.exported_at,
        "exported_by":exporter.display_name if exporter else str(snapshot.exported_by),"business_revision_no":manifest["business_revision_no"],
        "translation_revision_no":manifest["translation_revision_no"],"confirmation_id":str(snapshot.confirmation_id),
        "rule_pack_version":manifest.get("rule_pack_version"),"jurisdiction_inputs":manifest.get("jurisdiction_inputs"),
        "source_artifacts":sources,"case_report_artifact":artifact_view(report) if report else None,"open_warnings":manifest.get("not_covered_notes",[])+[
            item["description"] for item in manifest.get("issue_states",[]) if item.get("resolved") is False and not item.get("blocks_export")]}
