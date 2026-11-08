"""人工复核命令：原件／机器候选只读，所有修订追加版本与可核验处置。"""
from __future__ import annotations

from datetime import UTC, datetime
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID,uuid4

from sqlalchemy import select,func,or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.api.idempotency import request_digest
from app.documents import evidence_models as em
from app.documents.models import Case
from app.documents.retention import load_case_for_write, touch_material_event
from app.review import models as rm
from app.review.issue_state import issue_blocks_delivery, issue_resolved
from app.workflows import commands as wc
from app.workflows.models import InputRequest, InputRequestStatus, Run, RunDocument, RunStatus, StepExecution, StepStatus


async def check_business(db, principal, settings, case_id, *, expected_business_revision_id,
                         expected_business_revision_no=None) -> Case:
    wc.require_permission(principal, "review_revise")
    case = await load_case_for_write(db, case_id, user_id=principal.user_id, settings=settings)
    if case.current_business_revision_id is None:
        raise wc.conflict("BUSINESS_REVISION_REQUIRED", "尚未形成业务版，请等待识别完成")
    current_revision=await db.get(em.BusinessRevision,case.current_business_revision_id)
    if current_revision is None or current_revision.case_id!=case.id:raise NotFoundOrForbidden()
    case.business_revision_no=current_revision.revision_no
    if expected_business_revision_id != case.current_business_revision_id or (expected_business_revision_no is not None and expected_business_revision_no != case.business_revision_no):
        raise VersionConflict(current={"business_revision_id": str(case.current_business_revision_id),
                                       "business_revision_no": case.business_revision_no})
    return case


async def load_current_field(db, case, field_id, parse_id):
    field = await db.get(em.FieldAssertion, field_id)
    if field is None or field.case_id != case.id or field.parse_artifact_id != parse_id:
        raise NotFoundOrForbidden()
    member = (await db.execute(select(em.BusinessRevisionField).where(
        em.BusinessRevisionField.business_revision_id == case.current_business_revision_id,
        em.BusinessRevisionField.field_assertion_id == field_id))).scalar_one_or_none()
    if member is None:
        raise wc.conflict("FIELD_STALE", "该字段已被新的采用值替代，请重新加载")
    parse = await db.get(em.ParseArtifact, parse_id)
    latest = await wc.latest_parse(db, parse.document_id, parse.document_version)
    if latest is None or latest.id != parse_id:
        raise wc.conflict("PARSE_STALE", "该字段引用较早解析，请重新加载当前识别结果")
    return field


async def new_business_revision(db, case, actor, *, reason, replacement=None):
    latest_no=(await db.execute(select(func.max(em.BusinessRevision.revision_no)).where(em.BusinessRevision.case_id==case.id))).scalar() or 0
    revision = em.BusinessRevision(tenant_id=case.tenant_id, case_id=case.id,
        revision_no=latest_no + 1, parent_revision_id=case.current_business_revision_id,
        reason=reason, created_by=actor)
    db.add(revision)
    await db.flush()
    members = (await db.execute(select(em.BusinessRevisionField).where(
        em.BusinessRevisionField.business_revision_id == case.current_business_revision_id))).scalars().all()
    for member in members:
        field_id = replacement[1] if replacement and member.field_assertion_id == replacement[0] else member.field_assertion_id
        db.add(em.BusinessRevisionField(tenant_id=case.tenant_id, business_revision_id=revision.id, field_assertion_id=field_id))
    # 新业务版继承未改变区段的人工决定，原审核身份及时间保持可追溯。
    decisions=(await db.execute(select(em.SectionAssignmentDecision).where(
        em.SectionAssignmentDecision.business_revision_id==case.current_business_revision_id))).scalars().all()
    for decision in decisions:
        db.add(em.SectionAssignmentDecision(tenant_id=case.tenant_id,case_id=case.id,business_revision_id=revision.id,
            section_id=decision.section_id,logical_document_type=decision.logical_document_type,
            target_logical_document_id=decision.target_logical_document_id,confirmed_language=decision.confirmed_language,
            cargo_profile_ids=decision.cargo_profile_ids,decided_by=decision.decided_by,reason=decision.reason,created_at=decision.created_at))
    if reason != "rule_input":
        rule_decisions = (await db.execute(select(rm.RuleInputDecision).where(
            rm.RuleInputDecision.business_revision_id == case.current_business_revision_id))).scalars().all()
        for decision in rule_decisions:
            db.add(rm.RuleInputDecision(tenant_id=case.tenant_id,case_id=case.id,business_revision_id=revision.id,
                kind=decision.kind,scope_key=decision.scope_key,payload=decision.payload,basis=decision.basis,reason=decision.reason,
                decided_by=decision.decided_by,created_at=decision.created_at))
    case.current_business_revision_id, case.business_revision_no = revision.id, revision.revision_no
    return revision


async def affected_runs(db, case_id, *, document_id=None, run_id=None):
    query = select(Run).where(Run.case_id == case_id)
    if run_id:
        query = query.where(Run.id == run_id)
    elif document_id:
        query = query.join(RunDocument, RunDocument.run_id == Run.id).where(RunDocument.document_id == document_id)
    return list((await db.execute(query.order_by(Run.id).with_for_update())).scalars())


async def change_impact(db, case, target: dict) -> dict:
    document_id = UUID(target["document_id"]) if target.get("document_id") else None
    run_id = UUID(target["run_id"]) if target.get("run_id") else None
    query = select(Run).where(Run.case_id == case.id)
    if run_id:
        query = query.where(Run.id == run_id)
    elif document_id:
        query = query.join(RunDocument, RunDocument.run_id == Run.id).where(RunDocument.document_id == document_id)
    runs = list((await db.execute(query)).scalars())
    ids = [run.id for run in runs]
    checks = list((await db.execute(select(rm.RuleCheck).where(rm.RuleCheck.run_id.in_(ids),
        rm.RuleCheck.business_revision_id == case.current_business_revision_id))).scalars()) if ids else []
    segments = list((await db.execute(select(rm.TranslationSegment.id).where(rm.TranslationSegment.run_id.in_(ids)))).scalars()) if ids else []
    previews = list((await db.execute(select(rm.PreviewArtifact.id).where(rm.PreviewArtifact.run_id.in_(ids),
        rm.PreviewArtifact.status == "valid"))).scalars()) if ids else []
    # 只统计当前译文全视图，历史段不纳入费用估计。
    current_ids = set()
    for run in runs:
        if run.current_translation_revision_id:
            revision = await db.get(rm.TranslationRevision, run.current_translation_revision_id)
            if revision:
                current_ids.update(revision.segment_ids)
    target_segment = target.get("translation_segment_id")
    count = 1 if target_segment and target_segment in current_ids else len(current_ids)
    return {"affected_rules": sorted({check.rule_id for check in checks}), "affected_segments": count,
            "affected_previews": len(previews), "affected_summaries": len(ids),
            "affected_runs": [str(x) for x in ids], "cost_level": "unknown" if count else "none", "conflict": None}


async def require_settled_runs(db, runs):
    pending = (await db.execute(select(StepExecution.run_id).where(
        StepExecution.run_id.in_([run.id for run in runs]),
        StepExecution.status.in_([StepStatus.SUBMITTING, StepStatus.NEEDS_RECONCILE])).limit(1))).scalar_one_or_none()
    if pending:
        raise wc.conflict("RECONCILE_REQUIRED", "相关任务仍有外部请求待核查，请先完成核查再修订或确认", run_id=str(pending))


async def invalidate_and_enqueue(db, runs, case, *, message, principal, settings, reparse_document_id=None, only_preview=False):
    """复用解析，冻结新依赖；运行中/受理不明批次先拒绝，避免并发收费。"""
    if not only_preview and case.current_scope_revision_id:
        consumers=list((await db.execute(select(Run).where(Run.case_id==case.id,Run.workflow_type=="translate_review",
            Run.workflow_version>=2,Run.status!=RunStatus.CANCELLED).order_by(Run.id).with_for_update())).scalars())
        known={r.id for r in runs}
        runs.extend(r for r in consumers if r.id not in known)
    await require_settled_runs(db, runs)
    for run in runs:
        if run.status in {RunStatus.PROCESSING, RunStatus.QUEUED, RunStatus.EXPORTING, RunStatus.PAUSED_RECONCILE}:
            raise wc.conflict("RUN_BUSY", "相关任务正在执行或待核查，请结束当前阶段后再修订", run_id=str(run.id))
    from app.management.index import invalidate_search
    for run in runs:await db.run_sync(lambda sync:invalidate_search(sync,run))
    comparison_runs=[run for run in runs if run.workflow_type=="consignment_check" or run.workflow_type=="translate_review" and run.workflow_version>=2]
    new_comparison=None
    if comparison_runs and not only_preview:
        from app.consignment.service import recomparison_for_change
        new_comparison=await recomparison_for_change(db,principal,settings,case,reparse_document_id=reparse_document_id)
    for run in runs:
        previews = (await db.execute(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id == run.id,
            rm.PreviewArtifact.status == "valid"))).scalars().all()
        for preview in previews:
            preview.status = "stale"
        dependencies = (await db.execute(select(em.ArtifactDependency).where(em.ArtifactDependency.case_id == case.id))).scalars().all()
        for dependency in dependencies:
            deps = dependency.depends_on
            if only_preview and deps.get("comparison_batch_id"):continue
            if deps.get("run_id") == str(run.id) or deps.get("business_revision_id") == str(run.current_business_revision_id):
                dependency.stale, dependency.stale_since = True, datetime.now(UTC)
        if not only_preview and run in comparison_runs:
            if run.workflow_type=="translate_review":
                run.current_business_revision_id=case.current_business_revision_id
                run.status=RunStatus.AWAITING_REVIEW;run.current_stage="等待新批次核对后重新核验译文"
            await wc.append_run_event(db,run,message=message,event_type="comparison.invalidated",kind="human",payload={
                "recompute_run_id":str(new_comparison.id),"scope_revision_id":new_comparison.task_spec["scope_revision_id"],
                "previous_comparison_binding_id":str(run.current_comparison_binding_id) if run.current_comparison_binding_id else None})
            continue
        if not only_preview:run.current_business_revision_id = case.current_business_revision_id
        run.current_epoch += 1
        run.execution_fence += 1
        run.status, run.current_stage = RunStatus.QUEUED, "等待局部重算"
        documents = (await db.execute(select(RunDocument).where(RunDocument.run_id == run.id).order_by(RunDocument.ordinal))).scalars().all()
        parse_ids = []
        for document in documents:
            if document.document_id == reparse_document_id:
                continue
            parse = await wc.latest_parse(db, document.document_id, document.document_version)
            if parse:
                parse_ids.append(str(parse.id))
        await wc.enqueue_epoch(db, run, purpose="quality_preview" if only_preview else "reparse" if reparse_document_id else "recompute", parse_artifact_ids=parse_ids)
        await wc.append_run_event(db, run, message=message, kind="human", payload={
            "reparse_document_id": str(reparse_document_id) if reparse_document_id else None,
            "only_quality_preview": only_preview})
    return new_comparison


def issue_is_resolved(issue, disposition):
    return issue_resolved(issue, disposition)


async def field_issue_links(db, case_id, business_id, fields, *, snapshot=None, run_id=None):
    """只关联明确字段事实问题；全页、全票与翻译疑点不能泛化到所有字段。"""
    links = {str(field.id): [] for field in fields}
    if not fields:
        return links
    query = select(rm.Issue).where(rm.Issue.case_id == case_id, rm.Issue.business_revision_id == business_id)
    if snapshot:
        query = query.where(rm.Issue.id.in_([UUID(x) for x in snapshot.manifest["issue_ids"]]))
    else:
        query = query.join(Run, Run.id == rm.Issue.run_id).where(
            or_(rm.Issue.translation_revision_id.is_(None), rm.Issue.translation_revision_id == Run.current_translation_revision_id))
        case=await db.get(Case,case_id)
        batch_id=case.current_comparison_batch_id if case else None
        if run_id:
            from app.consignment.service import bound_batch
            run=await db.get(Run,run_id)
            if run is None or run.case_id!=case_id:raise NotFoundOrForbidden()
            batch=await db.run_sync(lambda s:bound_batch(s,run,case,strict_current=False))
            batch_id=batch.id if batch else None
            query=query.where(or_(rm.Issue.run_id==run_id,rm.Issue.comparison_batch_id==batch_id)
                if batch_id else rm.Issue.run_id==run_id)
        query=query.where(or_(rm.Issue.comparison_batch_id.is_(None),rm.Issue.comparison_batch_id==batch_id)
            if batch_id else rm.Issue.comparison_batch_id.is_(None))
    issues = list((await db.execute(query.order_by(rm.Issue.created_at, rm.Issue.id))).scalars())
    dispositions = list((await db.execute(select(rm.IssueDisposition).where(
        rm.IssueDisposition.issue_id.in_([issue.id for issue in issues])).order_by(
        rm.IssueDisposition.review_sequence, rm.IssueDisposition.created_at, rm.IssueDisposition.id))).scalars()) if issues else []
    if snapshot:
        allowed = set(snapshot.manifest["issue_disposition_ids"])
        dispositions = [d for d in dispositions if str(d.id) in allowed]
    latest = {d.issue_id: d for d in dispositions}
    check_ids = [issue.rule_check_id for issue in issues if issue.rule_check_id]
    checks = {check.id: check for check in (await db.execute(select(rm.RuleCheck).where(
        rm.RuleCheck.id.in_(check_ids)))).scalars()} if check_ids else {}
    from app.review.issue_skill import field_problem, source_targets
    for issue in issues:
        disposition = latest.get(issue.id)
        if issue_is_resolved(issue, disposition):
            continue
        check = checks.get(issue.rule_check_id)
        if check and not snapshot:
            from app.consignment.service import comparison_owned_rule
            owner=await db.get(Run,issue.run_id)
            if owner and comparison_owned_rule(owner,check.rule_id):continue
        if not field_problem(issue, check):
            continue
        explicit = {str(t["field_id"]) for t in source_targets(issue) if t.get("field_id")}
        if check:
            explicit.update(str(value) for value in check.input_field_ids if value)
        for field in fields:
            if str(field.id) in explicit:
                links[str(field.id)].append({"issue_id": str(issue.id), "title": issue.title,
                    "severity": "blocking" if issue.severity == "red" else "warning" if issue.severity == "yellow" else "undetermined",
                    "resolved": False, "blocks_confirmation": issue.severity == "red"})
    return links


async def field_action(db, principal, settings, *, case_id, field_id, action, body):
    case = await check_business(db, principal, settings, case_id,
        expected_business_revision_id=body.expected_business_revision_id,
        expected_business_revision_no=body.expected_business_revision_no)
    field = await load_current_field(db, case, field_id, body.expected_parse_artifact_id)
    parse = await db.get(em.ParseArtifact, field.parse_artifact_id)
    runs = await affected_runs(db, case.id, document_id=parse.document_id)
    impact = await change_impact(db, case, {"document_id": str(parse.document_id)})
    if action == "confirm":
        await require_settled_runs(db, runs)
        source_ids = [UUID(value) for value in field.source_segment_ids]
        sources = list((await db.execute(select(em.SourceSegment).where(
            em.SourceSegment.id.in_(source_ids), em.SourceSegment.parse_artifact_id == parse.id))).scalars())
        if not sources or len(sources) != len(set(source_ids)):
            raise wc.conflict("FIELD_SOURCE_REQUIRED", "该字段没有完整可核验的原文来源，请补充识别或修订依据")
        if not (field.normalized_value or field.candidate_value or "").strip():
            raise wc.conflict("FIELD_VALUE_REQUIRED", "该字段为空，请先核对原件并补充可靠信息")
        related = await field_issue_links(db, case.id, case.current_business_revision_id, [field])
        if any(issue["blocks_confirmation"] for issue in related[str(field.id)]):
            raise wc.conflict("FIELD_ISSUE_OPEN", "该字段仍有未解决疑点，请先完成处置")
    if action == "revise":
        if not body.value or not body.value.strip() or not body.reason or not body.reason.strip():
            raise InvalidRequest("采用值和修订原因均不能为空")
        value = body.value.strip()
        unit = body.unit if body.unit is not None else field.unit
        if value == (field.normalized_value or field.candidate_value) and unit == field.unit:
            return {"business_revision_no": case.business_revision_no, "affected": {**impact,"cost_level":"none"}}
        numeric = None
        if field.numeric_value is not None:
            try:
                numeric = Decimal(value)
                if not numeric.is_finite():
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise InvalidRequest("数字字段必须提交有限十进制数") from exc
        adopted = em.FieldAssertion(tenant_id=case.tenant_id, case_id=case.id, parse_artifact_id=field.parse_artifact_id,
            logical_document_id=field.logical_document_id, section_id=field.section_id, field_key=field.field_key,
            role=field.role, group=field.group, source_text=field.source_text, candidate_value=value,
            normalized_value=value, numeric_value=numeric, unit=unit, source_segment_ids=field.source_segment_ids,
            state="confirmed", supersedes_assertion_id=field.id, is_human=True, decided_by=principal.user_id,
            reason=body.reason, basis=body.basis)
        db.add(adopted)
        await db.flush()
        await new_business_revision(db, case, principal.user_id, reason="field_revision", replacement=(field.id,adopted.id))
        await invalidate_and_enqueue(db, runs, case, principal=principal,settings=settings,message="已保存字段采用值，相关规则、译文及预览等待重算")
    else:
        if action == "mark-unknown" and not (body.reason or "").strip():
            raise InvalidRequest("无法判断必须填写原因")
        db.add(em.FieldReviewEvent(tenant_id=case.tenant_id, case_id=case.id, field_assertion_id=field.id,
            business_revision_id=case.current_business_revision_id, action="confirmed" if action == "confirm" else "undetermined",
            review_sequence=case.review_state_version+1,
            reason=body.reason or "与原件一致", required_materials=body.required_materials, decided_by=principal.user_id))
        if action == "mark-unknown":
            for run in runs:
                db.add(rm.Issue(tenant_id=case.tenant_id, case_id=case.id, run_id=run.id,
                    business_revision_id=case.current_business_revision_id, category="source_uncertain", severity="undetermined",
                    blocks_export=False, title="字段无法判断", description=body.reason,
                    sources=[{"case_id":str(case.id),"document_id":str(parse.document_id),"document_version":parse.document_version,
                        "parse_artifact_id":str(parse.id),"logical_document_id":str(field.logical_document_id),
                        "section_id":str(field.section_id) if field.section_id else None,"field_id":str(field.id),
                        "value":field.normalized_value or field.candidate_value or ""}]))
        for run in runs:
            if run.status in {RunStatus.READY_TO_EXPORT, RunStatus.EXPORTED}:
                run.status=RunStatus.AWAITING_REVIEW
            await wc.append_run_event(db, run, message="已记录字段审核" if action=="confirm" else "已记录字段无法判断，交付仍需补充依据", kind="human")
    case.review_state_version += 1
    touch_material_event(case,settings)
    await db.flush()
    from app.management.index import refresh_run_summaries
    for run in runs:await db.run_sync(lambda sync:refresh_run_summaries(sync,run))
    return {"business_revision_no":case.business_revision_no,"affected":impact}


async def confirm_line_match(db,principal,settings,*,case_id,body):
    case=await check_business(db,principal,settings,case_id,expected_business_revision_id=body.expected_business_revision_id,
        expected_business_revision_no=body.expected_business_revision_no)
    if case.review_state_version!=body.expected_review_state_version:raise VersionConflict("复核状态已变化，请重新加载",code="PAGE_STALE")
    left_ids=body.left_line_ids or ([body.left_line_id] if body.left_line_id else [])
    right_ids=body.right_line_ids or ([body.right_line_id] if body.right_line_id else [])
    valid_shape=(body.relation=="one_to_one" and len(left_ids)==len(right_ids)==1 or
        body.relation=="one_to_many" and len(left_ids)==1 and len(right_ids)>1 or
        body.relation=="many_to_one" and len(left_ids)>1 and len(right_ids)==1)
    if not valid_shape or len(set(left_ids+right_ids))!=len(left_ids+right_ids):raise InvalidRequest("行关系两端数量与所选关系不一致，或有重复明细")
    if not body.reason.strip() or not body.basis.same_goods or not body.basis.complete or body.basis.role!="goods":
        raise InvalidRequest("须确认同货品与完整范围并填写依据；复杂关系只留人工审核，不自动判数值通过")
    run,_=await wc.load_run(db,principal,settings,body.run_id,write=True)
    if run.case_id!=case.id or run.workflow_type!="translate_review" or run.current_business_revision_id!=case.current_business_revision_id:raise NotFoundOrForbidden()
    revision=await db.get(rm.TranslationRevision,run.current_translation_revision_id) if run.current_translation_revision_id else None
    if revision is None:raise InvalidRequest("请等待翻译及明细识别完成")
    lines=[];parses=[]
    for line_id in left_ids+right_ids:
        line=await db.get(em.LineItem,line_id)
        if line is None or line.case_id!=case.id or str(line.parse_artifact_id) not in revision.parse_artifact_ids:raise NotFoundOrForbidden()
        parse=await db.get(em.ParseArtifact,line.parse_artifact_id)
        member=(await db.execute(select(RunDocument).where(RunDocument.run_id==run.id,RunDocument.document_id==parse.document_id,
            RunDocument.document_version==parse.document_version))).scalar_one_or_none()
        logical=await db.get(em.LogicalDocument,line.logical_document_id)
        source_ids=list((await db.execute(select(em.SourceSegment.id).where(em.SourceSegment.id.in_([UUID(x) for x in line.source_segment_ids]),
            em.SourceSegment.parse_artifact_id==parse.id,em.SourceSegment.logical_document_id==line.logical_document_id))).scalars())
        latest=await wc.latest_parse(db,parse.document_id,parse.document_version)
        if member is None or logical is None or logical.parse_artifact_id!=parse.id or not line.source_segment_ids or len(source_ids)!=len(set(line.source_segment_ids)):raise NotFoundOrForbidden()
        if latest is None or latest.id!=parse.id:raise wc.conflict("PARSE_STALE","行项目解析版已变化，请重新选择")
        lines.append(line);parses.append(parse)
    left_lines=lines[:len(left_ids)];right_lines=lines[len(left_ids):]
    if {x.logical_document_id for x in left_lines}&{x.logical_document_id for x in right_lines}:raise InvalidRequest("左右须来自不同逻辑单据")
    candidates=await affected_runs(db,case.id)
    runs=[]
    for candidate in candidates:
        if candidate.workflow_type!="translate_review" or not candidate.current_translation_revision_id:continue
        current=await db.get(rm.TranslationRevision,candidate.current_translation_revision_id)
        if current and {str(p.id) for p in parses}.issubset(current.parse_artifact_ids):runs.append(candidate)
    if any(r.status in {RunStatus.PROCESSING,RunStatus.QUEUED,RunStatus.EXPORTING,RunStatus.PAUSED_RECONCILE} for r in runs):raise wc.conflict("RUN_BUSY","相关任务运行中或待核查，不能修改行关系")
    old=list((await db.execute(select(em.LineMatchEdge).where(em.LineMatchEdge.case_id==case.id,
        em.LineMatchEdge.state.in_(["confirmed","candidate"]),or_(em.LineMatchEdge.left_line_id.in_([x.id for x in lines]),
            em.LineMatchEdge.right_line_id.in_([x.id for x in lines]))).with_for_update())).scalars())
    for edge in old:edge.state="rejected"
    group_id=uuid4();created=[]
    for left in left_lines:
        for right in right_lines:
            new=em.LineMatchEdge(tenant_id=case.tenant_id,case_id=case.id,left_line_id=left.id,right_line_id=right.id,
                relation=body.relation,basis=json.dumps({**body.basis.model_dump(),"reason":body.reason.strip(),"group_id":str(group_id),
                    "left_line_ids":[str(x) for x in left_ids],"right_line_ids":[str(x) for x in right_ids]},ensure_ascii=False),
                state="confirmed",decided_by=principal.user_id)
            db.add(new);created.append(new)
    await db.flush()
    await new_business_revision(db,case,principal.user_id,reason="line_match")
    await invalidate_and_enqueue(db,runs,case,principal=principal,settings=settings,message="人工确认明细行关系，规则与预览等待复用解析重算")
    case.review_state_version+=1;touch_material_event(case,settings)
    for target in runs:
        await wc.append_run_event(db,target,message=body.reason.strip(),kind="human",payload={"line_match_group_id":str(group_id),"line_match_ids":[str(e.id) for e in created],
            "left_line_ids":[str(x) for x in left_ids],"right_line_ids":[str(x) for x in right_ids],"relation":body.relation,"rejected_edge_ids":[str(e.id) for e in old],"basis":body.basis.model_dump()})
    return {"line_match_id":str(created[0].id),"line_match_ids":[str(e.id) for e in created],"line_match_group_id":str(group_id),"business_revision_no":case.business_revision_no,"business_revision_id":str(case.current_business_revision_id),
        "affected_run_ids":[str(r.id) for r in runs],"review_state_version":case.review_state_version}


async def _issue_source_location(db, issue, target):
    """只读取疑点保存的来源版本，不把最新解析坐标借给旧来源。"""
    from math import isfinite
    try:
        parse_id = UUID(str(target.get("parse_artifact_id")))
        segment_id = UUID(str(target.get("source_segment_id")))
        document_id = UUID(str(target.get("document_id")))
        anchor_id = UUID(str(target["source_anchor_id"])) if target.get("source_anchor_id") else None
    except (ValueError, TypeError, AttributeError):
        return None
    parse = await db.get(em.ParseArtifact, parse_id)
    source = await db.get(em.SourceSegment, segment_id)
    if (parse is None or source is None or parse.case_id != issue.case_id or source.case_id != issue.case_id
        or parse.tenant_id != issue.tenant_id or source.tenant_id != issue.tenant_id
        or source.parse_artifact_id != parse.id or parse.document_id != document_id
        or parse.document_version != target.get("document_version")
        or target.get("logical_document_id") and str(source.logical_document_id) != str(target["logical_document_id"])
        or target.get("section_id") and str(source.section_id) != str(target["section_id"])):
        return None
    query = select(em.SourceAnchor).where(em.SourceAnchor.tenant_id == issue.tenant_id, em.SourceAnchor.case_id == issue.case_id,
        em.SourceAnchor.parse_artifact_id == parse.id, em.SourceAnchor.source_segment_id == source.id)
    if anchor_id:
        query = query.where(em.SourceAnchor.id == anchor_id)
    anchor = (await db.execute(query.order_by(em.SourceAnchor.id).limit(1))).scalar_one_or_none()
    if anchor is None:
        return None
    page = anchor.page_no if anchor.page_no and anchor.page_no > 0 and (not parse.page_count or anchor.page_no <= parse.page_count) else None
    precision = anchor.precision.value
    bbox = anchor.bbox if precision == "rect" else None
    valid = (isinstance(bbox, list) and len(bbox) == 4
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) for value in bbox))
    valid = bool(valid and bbox[0] < bbox[2] and bbox[1] < bbox[3])
    if valid and anchor.coord_system == "page_normalized_top_left":
        valid = all(0 <= value <= 1 for value in bbox)
    if precision == "rect" and (not valid or page is None):
        precision = "page_only" if page else "none"
    return {"source_anchor_id":str(anchor.id), "precision":precision, "page_no":page,
        "bbox":bbox if precision == "rect" else None, "coord_system":anchor.coord_system if precision == "rect" else None,
        "render_version":anchor.render_version}


async def issue_view(db, issue, frozen_disposition_ids=None, *, permissions=None):
    dispositions=list((await db.execute(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id==issue.id)
        .order_by(rm.IssueDisposition.review_sequence,rm.IssueDisposition.created_at,rm.IssueDisposition.id))).scalars())
    if frozen_disposition_ids is not None:
        dispositions=[d for d in dispositions if str(d.id) in frozen_disposition_ids]
    last=dispositions[-1] if dispositions else None
    sources=[]
    for source in issue.sources:
        target=dict(source["target"]) if source.get("target") else {k:v for k,v in source.items() if k not in {"value","label","text","target"}}
        target.update(case_id=str(issue.case_id),run_id=str(issue.run_id),issue_id=str(issue.id),business_revision_id=str(issue.business_revision_id))
        sources.append({"label":source.get("label") or "原文来源","value":str(source.get("value",source.get("text",source.get("excerpt","")))),"target":target,
            "location":await _issue_source_location(db, issue, target)})
    if issue.comparison_batch_id:
        # 同一侧可能有多个证据片段；A/B必须分别来自比较两侧。
        left=next((view for raw,view in zip(issue.sources,sources) if raw.get("side")=="left"),None)
        right=next((view for raw,view in zip(issue.sources,sources) if raw.get("side")=="right"),None)
        sources=[left,right]
    if not sources:
        docs=(await db.execute(select(RunDocument).where(RunDocument.run_id==issue.run_id).order_by(RunDocument.ordinal))).scalars().all()
        if docs:
            sources=[{"label":"任务原件","value":"","target":{"case_id":str(issue.case_id),"run_id":str(issue.run_id),"document_id":str(docs[0].document_id),"document_version":docs[0].document_version}}]
    severity="blocking" if issue.severity=="red" else "warning" if issue.severity=="yellow" else "undetermined"
    state={"accept":"accepted","adjudicate":"adjudicated","mark-unresolved":"unresolved","not-applicable":"not_applicable","ignore":"ignored","revise":"revised"}.get(last.action,"open") if last else "open"
    check=await db.get(rm.RuleCheck,issue.rule_check_id) if issue.rule_check_id else None
    from app.review.issue_skill import guidance_for, guidance_actions_for_access
    guidance = guidance_for(issue)
    if issue.comparison_batch_id and frozen_disposition_ids is None:
        from app.workflows.models import ExecutionEpoch
        analysis = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == issue.run_id,
            ExecutionEpoch.repair_request["issue_id"].astext == str(issue.id)).order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one_or_none()
        if analysis:
            guidance = guidance_for(issue, {k: v for k, v in analysis.repair_request.items() if k in {
                "agent_status", "agent_response", "agent_request_no", "recommendation", "replacement_text", "agent_source_segment_ids", "recommended_action"}})
    if guidance["target"]:
        guidance["target"] = {**guidance["target"], "case_id": str(issue.case_id), "run_id": str(issue.run_id), "issue_id": str(issue.id), "business_revision_id": str(issue.business_revision_id)}
    if permissions is not None or frozen_disposition_ids is not None:
        guidance_actions_for_access(guidance, permissions or (), historical=frozen_disposition_ids is not None)
    if frozen_disposition_ids is not None:
        guidance.update(agent_status=None, agent_response=None, agent_request_no=0)
    return {"issue_id":str(issue.id),"severity":severity,"category":issue.category,
        "rule_name":check.rule_id if check else issue.title,"rule_version":str(check.rule_version) if check else "1",
        "conclusion":issue.description,"blocking_delivery":issue_blocks_delivery(issue,last),
        "status":state,"a":sources[0] if sources else None,"b":sources[1] if len(sources)>1 else None,
        "difference":None,"disposition":{"by":str(last.decided_by),"at":last.created_at,"reason":last.reason,"basis":last.basis} if last else None,
        "issue_version":len(dispositions)+1+int(guidance.get("agent_request_no", 0)) if frozen_disposition_ids is None else len(dispositions)+1, "guidance":guidance}


async def issue_action(db,principal,settings,*,case_id,issue_id,action,body):
    case=await check_business(db,principal,settings,case_id,expected_business_revision_id=body.expected_business_revision_id,
        expected_business_revision_no=body.expected_business_revision_no)
    issue=await db.get(rm.Issue,issue_id)
    if issue is None or issue.case_id!=case.id:
        raise NotFoundOrForbidden()
    if issue.business_revision_id!=case.current_business_revision_id:
        raise wc.conflict("ISSUE_STALE","疑点所属业务版已更新，请处理当前版本疑点")
    if issue.translation_revision_id:
        source_run=await db.get(Run,issue.run_id)
        if source_run is None or source_run.current_translation_revision_id!=issue.translation_revision_id:
            raise wc.conflict("ISSUE_STALE","疑点所属译文版已更新，请处理当前版本疑点")
    if issue.comparison_batch_id:
        from app.consignment.models import ComparisonBatch
        from app.consignment.service import batch_current
        batch = await db.get(ComparisonBatch, issue.comparison_batch_id)
        if not batch or not await db.run_sync(lambda s: batch_current(s, case, batch)):
            raise wc.conflict("COMPARISON_STALE", "核对依据已更新，请对当前材料重新核对后处理")
    runs=await affected_runs(db,case.id,run_id=issue.run_id)
    await require_settled_runs(db, runs)
    if any(run.status in {RunStatus.QUEUED, RunStatus.PROCESSING, RunStatus.EXPORTING} for run in runs):
        raise wc.conflict("RUN_BUSY", "任务正在处理，请等待本轮完成后处置疑点")
    current=await issue_view(db,issue)
    if current["issue_version"]!=body.expected_issue_version:
        raise VersionConflict(current=current)
    if action != "ignore" and (not body.reason or not body.reason.strip()):
        raise InvalidRequest("处置原因不能为空")
    if action=="accept" and issue.severity!="yellow":
        raise InvalidRequest("只有不阻断的黄色提示可以接受；红项必须裁定或补充材料")
    adopted=None
    if action in {"adjudicate","not-applicable"}:
        wc.require_permission(principal,"review_adjudicate")
        if not body.basis or not body.basis.strip():
            raise InvalidRequest("裁定或不适用必须提供可核验依据")
    if action=="adjudicate":
        if not body.value or not body.value.strip():
            raise InvalidRequest("事实裁定必须填写采用值")
        from app.review.issue_skill import source_targets
        source=next((s for s in source_targets(issue) if s.get("field_id")),None)
        if source is None:
            raise InvalidRequest("该疑点没有可裁定字段，请先补充材料或修订具体译文")
        field=await load_current_field(db,case,UUID(source["field_id"]),UUID(source["parse_artifact_id"]))
        if body.value.strip()!=(field.normalized_value or field.candidate_value):
            numeric=None
            if field.numeric_value is not None:
                try:
                    numeric=Decimal(body.value.strip())
                    if not numeric.is_finite():raise InvalidOperation
                except InvalidOperation as exc:raise InvalidRequest("采用值须为有限十进制数") from exc
            adopted=em.FieldAssertion(tenant_id=case.tenant_id,case_id=case.id,parse_artifact_id=field.parse_artifact_id,
                logical_document_id=field.logical_document_id,section_id=field.section_id,field_key=field.field_key,role=field.role,
                group=field.group,source_text=field.source_text,candidate_value=body.value.strip(),normalized_value=body.value.strip(),
                numeric_value=numeric,unit=field.unit,source_segment_ids=field.source_segment_ids,state="confirmed",
                supersedes_assertion_id=field.id,is_human=True,decided_by=principal.user_id,reason=body.reason,basis=body.basis)
            db.add(adopted);await db.flush()
            await new_business_revision(db,case,principal.user_id,reason="issue_adjudication",replacement=(field.id,adopted.id))
            await invalidate_and_enqueue(db,runs,case,principal=principal,settings=settings,message="已记录事实裁定，相关依赖等待重算")
    db.add(rm.IssueDisposition(tenant_id=case.tenant_id,case_id=case.id,issue_id=issue.id,
        business_revision_id=case.current_business_revision_id,action=action,reason=(body.reason or "").strip(),basis=body.basis,
        review_sequence=case.review_state_version+1,
        adopted_assertion_id=adopted.id if adopted else None,decided_by=principal.user_id))
    case.review_state_version+=1
    for run in runs:
        if run.status in {RunStatus.EXPORTED,RunStatus.READY_TO_EXPORT}:run.status=RunStatus.AWAITING_REVIEW
        await wc.append_run_event(db,run,message="已记录疑点人工处置，原检查结论继续保留",kind="human")
    touch_material_event(case,settings);await db.flush()
    return await issue_view(db,issue)


async def section_action(db,principal,settings,*,case_id,section_id,action,body):
    case=await check_business(db,principal,settings,case_id,expected_business_revision_id=body.expected_business_revision_id,
        expected_business_revision_no=body.expected_business_revision_no)
    section=await db.get(em.DocumentSection,section_id)
    if section is None or section.case_id!=case.id or section.parse_artifact_id!=body.expected_parse_artifact_id:raise NotFoundOrForbidden()
    parse=await db.get(em.ParseArtifact,section.parse_artifact_id)
    latest=await wc.latest_parse(db,parse.document_id,parse.document_version)
    if not latest or latest.id!=parse.id:raise wc.conflict("PARSE_STALE","区段已重识别，请重新加载")
    runs=await affected_runs(db,case.id,document_id=parse.document_id)
    active=(await db.execute(select(InputRequest).where(InputRequest.run_id.in_([r.id for r in runs]),
        InputRequest.document_id==parse.document_id,InputRequest.status==InputRequestStatus.ACTIVE))).scalar_one_or_none()
    if active:raise wc.conflict("INPUT_REQUEST_ACTIVE","请通过任务待确认卡回答此问题",input_request_id=str(active.id))
    if not body.reason or not body.reason.strip():raise InvalidRequest("区段或语言修订必须填写原因")
    values=body.sections or {str(section_id):body.source_language}
    if action=="assign":values={str(section_id):None}
    targets=[]
    for sid,language in values.items():
        target=await db.get(em.DocumentSection,UUID(sid))
        if target is None or target.parse_artifact_id!=parse.id:raise InvalidRequest("批量语言确认只能属于同一份原件的当前解析")
        if action=="confirm-language" and language not in {"zh-CN","en","fr","es","ru","mixed","already_target"}:raise InvalidRequest("语言候选不合法")
        targets.append((target,language))
    from app.documents.type_catalog import normalize_type_code, is_document_type_code, normalize_cargo_profiles
    doc_type=normalize_type_code(body.doc_type) if action=="assign" else body.doc_type
    if action=="assign" and not is_document_type_code(doc_type):raise InvalidRequest("单据类型不合法")
    try:
        cargo_profiles=normalize_cargo_profiles(body.cargo_profile_ids or [])
    except ValueError as exc:
        raise InvalidRequest("货物领域类型不合法") from exc
    revision=await new_business_revision(db,case,principal.user_id,reason="section_assignment")
    for target,language in targets:
        db.add(em.SectionAssignmentDecision(tenant_id=case.tenant_id,case_id=case.id,business_revision_id=revision.id,
            section_id=target.id,logical_document_type=doc_type if action=="assign" else None,confirmed_language=language,
            cargo_profile_ids=cargo_profiles,
            decided_by=principal.user_id,reason=body.reason))
    await invalidate_and_enqueue(db,runs,case,principal=principal,settings=settings,message="已保存区段或源语言人工决定，相关依赖等待重算")
    case.review_state_version+=1;touch_material_event(case,settings);await db.flush()
    return {"business_revision_no":case.business_revision_no}


async def revise_segment(db,principal,settings,*,run_id,segment_id,body):
    wc.require_permission(principal,"review_revise")
    run,case=await wc.load_run(db,principal,settings,run_id,write=True)
    if run.current_translation_revision_id!=body.expected_translation_revision_id:
        raise VersionConflict(current={"translation_revision_id":str(run.current_translation_revision_id) if run.current_translation_revision_id else None})
    await require_settled_runs(db,[run])
    if run.status not in {RunStatus.AWAITING_REVIEW,RunStatus.READY_TO_EXPORT,RunStatus.EXPORTED}:raise InvalidRequest("请等待当前任务完成后再修订")
    revision=await db.get(rm.TranslationRevision,body.expected_translation_revision_id)
    segment=await db.get(rm.TranslationSegment,segment_id)
    if revision is None or segment is None or str(segment.id) not in revision.segment_ids or segment.run_id!=run.id:raise NotFoundOrForbidden()
    if body.target_language!=run.target_language or body.target_language!=segment.target_language:raise InvalidRequest("译文语言与当前任务不符")
    if not body.translated_text.strip() or not body.reason.strip():raise InvalidRequest("译文和修订原因不能为空")
    edited=rm.TranslationSegment(tenant_id=case.tenant_id,case_id=case.id,run_id=run.id,document_id=segment.document_id,
        document_version=segment.document_version,parse_artifact_id=segment.parse_artifact_id,section_id=segment.section_id,
        target_language=segment.target_language,ordinal=segment.ordinal,source_segment_ids=segment.source_segment_ids,
        translated_text=body.translated_text.strip(),protected_tokens=segment.protected_tokens,state="human_revised",
        supersedes_segment_id=segment.id,created_by=principal.user_id,reason=body.reason,
        dependency_fingerprint=request_digest({"previous":segment.dependency_fingerprint,"text":body.translated_text.strip()}))
    db.add(edited);await db.flush()
    new=rm.TranslationRevision(tenant_id=case.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=revision.business_revision_id,
        target_language=revision.target_language,revision_no=revision.revision_no+1,parent_revision_id=revision.id,
        parse_artifact_ids=revision.parse_artifact_ids,resolved_scope_ids=revision.resolved_scope_ids,
        segment_ids=[str(edited.id) if sid==str(segment.id) else sid for sid in revision.segment_ids],
        prompt_versions=revision.prompt_versions,model_bindings=revision.model_bindings,glossary_version=revision.glossary_version,
        qa_status="not_run",qa_summary={},dependency_fingerprint=request_digest({"revision":str(revision.id),"edited":str(edited.id)}),
        is_synthetic=revision.is_synthetic,created_by=principal.user_id)
    db.add(new);await db.flush()
    for sid in segment.source_segment_ids:db.add(rm.SourceTranslationLink(tenant_id=case.tenant_id,source_segment_id=UUID(sid),translation_segment_id=edited.id))
    matching=list((await db.execute(select(rm.Issue).where(rm.Issue.run_id==run.id,rm.Issue.translation_revision_id==revision.id))).scalars())
    from app.review.issue_skill import TRANSLATION_CATEGORIES
    for issue in matching:
        if issue.category in TRANSLATION_CATEGORIES and any(s.get("target",s).get("translation_segment_id")==str(segment.id) for s in issue.sources):
            case.review_state_version+=1
            db.add(rm.IssueDisposition(tenant_id=case.tenant_id,case_id=case.id,issue_id=issue.id,business_revision_id=case.current_business_revision_id,action="revise",reason=f"译文v{revision.revision_no}修订为v{new.revision_no}：{body.reason}",review_sequence=case.review_state_version,decided_by=principal.user_id))
    run.current_translation_revision_id=new.id
    await invalidate_and_enqueue(db,[run],case,principal=principal,settings=settings,message="已保存本语言译文修订，QA及预览等待重算",only_preview=True)
    case.review_state_version+=1;touch_material_event(case,settings);await db.flush()
    return await segment_view(db,edited,new)


async def segment_view(db,segment,revision):
    source=(await db.execute(select(em.SourceSegment).where(em.SourceSegment.id.in_([UUID(x) for x in segment.source_segment_ids]))
        .order_by(em.SourceSegment.ordinal))).scalars().all()
    section=await db.get(em.DocumentSection,segment.section_id)
    return {"translation_segment_id":str(segment.id),"source_segment_id":segment.source_segment_ids[0] if segment.source_segment_ids else None,
        "source_segment_ids":segment.source_segment_ids,"document_id":str(segment.document_id),"section_id":str(segment.section_id),
        "section_title":section.title or "区段" if section else "区段","align_group":str(segment.id),
        "source_text":"\n".join(x.text for x in source),"translated_text":segment.translated_text,
        "qa_status":"not_translated" if segment.state in {"not_translated","retained"} else "passed" if revision.qa_status=="passed" else "stale" if revision.qa_status=="not_run" else "warning",
        "terms":[],"revision_no":revision.revision_no,"target":{"case_id":str(segment.case_id),"run_id":str(segment.run_id),
            "document_id":str(segment.document_id),"document_version":segment.document_version,"parse_artifact_id":str(segment.parse_artifact_id),
            "section_id":str(segment.section_id),"source_segment_id":segment.source_segment_ids[0] if segment.source_segment_ids else None,
            "translation_segment_id":str(segment.id),"translation_revision_id":str(revision.id),"business_revision_id":str(revision.business_revision_id)}}


async def reparse(db,principal,settings,*,document_id,version,body):
    wc.require_permission(principal,"review_revise")
    run,case=await wc.load_run(db,principal,settings,body.run_id,write=True)
    document=(await db.execute(select(RunDocument).where(RunDocument.run_id==run.id,RunDocument.document_id==document_id,RunDocument.document_version==version))).scalar_one_or_none()
    if not document:raise NotFoundOrForbidden()
    parse=await wc.latest_parse(db,document_id,version)
    if parse is None or parse.id!=body.expected_parse_artifact_id:raise wc.conflict("PARSE_STALE","识别版本已变化，请读取当前版本")
    if not body.reason.strip():raise InvalidRequest("申请重识别必须填写原因")
    active=(await db.execute(select(InputRequest).where(InputRequest.run_id==run.id,InputRequest.document_id==document_id,
        InputRequest.status==InputRequestStatus.ACTIVE).with_for_update())).scalars().all()
    for request in active:
        request.status=InputRequestStatus.SUPERSEDED;request.request_version+=1
    replacement=await invalidate_and_enqueue(db,[run],case,principal=principal,settings=settings,message="已申请按原件重新识别，旧解析和旧来源继续只读保留",reparse_document_id=document_id)
    case.review_state_version+=1;touch_material_event(case,settings);await db.flush()
    target=replacement or run
    return {"accepted":True,"run_id":str(target.id),"execution_epoch":target.current_epoch}
