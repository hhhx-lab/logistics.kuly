"""意见冻结为执行批次；局部修订与材料分析分开，未知受理不重投。"""
from uuid import UUID
from types import SimpleNamespace
from sqlalchemy import select
from app.api.errors import InvalidRequest, VersionConflict, NotFoundOrForbidden
from app.review import commands, models as rm
from app.review.issue_skill import recommendation_requires_assessment, TRANSLATION_CATEGORIES
from app.conversation.domain import is_instruction_attack
from app.conversation.safety import requests_internal_information, contains_internal_information
from app.prompts.registry import get_prompt_registry
from app.workflows import commands as wc
from app.workflows.models import RunStatus


async def prepare_repair(db, principal, settings, case_id, issue_id, body, apply=False):
    wc.require_permission(principal, "review_revise")
    case = await commands.check_business(db, principal, settings, case_id,
        expected_business_revision_id=body.expected_business_revision_id)
    issue = await db.get(rm.Issue, issue_id)
    if issue is None or issue.case_id != case.id:
        raise NotFoundOrForbidden()
    view = await commands.issue_view(db, issue)
    if view["issue_version"] != body.expected_issue_version:
        raise VersionConflict(current=view)
    run, case = await wc.load_run(db, principal, settings, issue.run_id, write=True)
    await commands.require_settled_runs(db, [run])
    if run.status not in {RunStatus.AWAITING_REVIEW, RunStatus.READY_TO_EXPORT, RunStatus.EXPORTED, RunStatus.INDEXED, RunStatus.CHECKED}:
        raise InvalidRequest("当前任务正在执行，请等待本轮处理完成")
    if run.current_business_revision_id != case.current_business_revision_id:
        raise VersionConflict("当前任务业务版已更新，请重新打开")
    if issue.comparison_batch_id:
        from app.consignment.models import ComparisonBatch
        from app.consignment.service import batch_current
        batch = await db.get(ComparisonBatch, issue.comparison_batch_id)
        if not batch or not await db.run_sync(lambda s: batch_current(s, case, batch)):
            raise wc.conflict("COMPARISON_STALE", "核对依据已更新，请重新核对当前材料")
    elif body.expected_translation_revision_id != run.current_translation_revision_id:
        raise VersionConflict(current={"translation_revision_id": str(run.current_translation_revision_id) if run.current_translation_revision_id else None})
    if view["guidance"].get("agent_status") == "needs_reconcile":
        raise wc.conflict("RECONCILE_REQUIRED", "上次 Agent 请求仍待核查，不能重复提交")
    guide = view["guidance"]
    target = guide["target"]
    segment = None
    revision = await db.get(rm.TranslationRevision, run.current_translation_revision_id) if run.current_translation_revision_id else None
    if target and target.get("translation_segment_id") and issue.category in TRANSLATION_CATEGORIES:
        candidate = await db.get(rm.TranslationSegment, UUID(target["translation_segment_id"]))
        if candidate and revision and str(candidate.id) in revision.segment_ids and candidate.run_id == run.id and candidate.case_id == case.id:
            segment = candidate
        else:
            raise VersionConflict("目标译段已更新，请重新打开疑点")
    if apply and not guide["can_apply"]:
        raise InvalidRequest("暂无推荐处理，请先提交 Agent 意见")
    replacement = guide.get("replacement_text")
    assess_recommendation = apply and recommendation_requires_assessment(guide)
    if apply and segment and isinstance(replacement, str) and replacement.strip() == segment.translated_text.strip():
        assess_recommendation = True
    if apply and segment and not assess_recommendation and isinstance(replacement, str) and replacement.strip():
        if contains_internal_information(replacement):
            raise InvalidRequest("推荐内容包含内部信息，不予应用")
        if any(value not in replacement for value in segment.protected_tokens.values()):
            raise InvalidRequest("推荐内容改变了单号、金额等保护值，请人工核验")
        result = await commands.revise_segment(db, principal, settings, run_id=run.id, segment_id=segment.id,
            body=SimpleNamespace(expected_translation_revision_id=revision.id, target_language=run.target_language,
                translated_text=replacement, reason="应用疑点建议：" + issue.description[:1500]))
        return {"run_id": str(run.id), "issue_id": str(issue.id), "status": "queued",
            "translation_revision_id": result["target"]["translation_revision_id"], "message": "已应用建议，正在更新本段校验与预览"}
    instruction = guide["recommendation"].strip() if apply else body.instruction.strip()
    if not instruction:
        raise InvalidRequest("请填写具体处理意见")
    if is_instruction_attack(instruction) or requests_internal_information(instruction):
        raise InvalidRequest("处理意见只能针对单证内容，不能要求内部信息或越权操作")
    # 推荐是本次冻结的用户执行意见；材料核验和补件不能被解释成译文修改。
    if assess_recommendation:
        segment = None
    purpose = "issue_repair" if segment else "issue_assessment"
    prompt_key = "document.revise" if segment else "document.issue_assess"
    registry = get_prompt_registry()
    if not registry.check.capability_ready("document_translate" if segment else "issue_assessment"):
        raise InvalidRequest("内部处理提示词不可用，请检查已发布版本", code="CAPABILITY_UNAVAILABLE")
    prompt = registry.freeze([prompt_key])[prompt_key]
    from app.workflows.models import ExecutionEpoch
    previous_epoch = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id)
        .order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one_or_none()
    required_role = "translate" if segment else "extract"
    if previous_epoch is None or not any(binding.get("role") == required_role for binding in previous_epoch.model_bindings):
        raise wc.conflict("MODEL_BINDING_REQUIRED", "当前历史任务没有冻结所需模型，请用原件新建处理；不会偷偷换模型")
    from app.config import ModelRole
    from app.infra.model_gateway import build_model_gateway
    gateway = build_model_gateway(settings)
    try:
        current = gateway.binding_for(ModelRole(required_role))
        frozen = next(binding for binding in previous_epoch.model_bindings if binding.get("role") == required_role)
        if current is None:
            raise InvalidRequest("当前处理模型未完整配置", code="CAPABILITY_UNAVAILABLE")
        if any(frozen.get(key) != getattr(current, key) for key in ("base_url", "model_name", "protocol", "adapter_version")):
            raise wc.conflict("MODEL_MIGRATION_REQUIRED", "当前模型配置与任务冻结绑定不同，请由有权限的账号执行受控模型迁移后再处理；本次未排队或外发")
    finally:
        await gateway.aclose()
    parse_ids = revision.parse_artifact_ids if revision else []
    if revision is None:
        from app.management.versions import run_parse_ids
        parse_ids = [str(value) for value in await db.run_sync(lambda sync: run_parse_ids(sync, run))]
    run.current_epoch += 1
    run.execution_fence += 1
    previous_status = run.status.value
    run.status = RunStatus.QUEUED
    run.failure_reason = None
    run.current_stage = "等待局部译文修改" if segment else "等待疑点分析"
    epoch = await wc.enqueue_epoch(db, run, purpose=purpose, parse_artifact_ids=parse_ids)
    request_no = int(view["guidance"].get("agent_request_no", 0)) + 1
    epoch.repair_request = {"issue_id": str(issue.id), "segment_id": str(segment.id) if segment else None,
        "translation_revision_id": str(revision.id) if revision else None, "instruction": instruction,
        "actor_id": str(principal.user_id), "prompt": prompt, "prompt_key": prompt_key,
        "request_action": "apply-recommendation" if apply else "request-revision",
        "recommendation": guide["recommendation"], "replacement_text": guide.get("replacement_text"),
        "recommended_action": guide.get("recommended_action"),
        "return_status": previous_status, "agent_request_no": request_no, "agent_status": "queued", "agent_response": None}
    if not issue.comparison_batch_id:
        issue.guidance = {**(issue.guidance or {}), "agent_status": "queued", "agent_response": None,
            "agent_request_no": request_no, "agent_prompt": prompt, "skill_version": guide["skill_version"]}
    case.review_state_version += 1
    await wc.append_run_event(db, run, message=("已收到推荐处理，仅修改对应译段" if segment else "已收到推荐处理，将依据现有材料核验并说明补件要求，疑点保持待处理") if apply else ("已收到处理意见，仅修改对应译段" if segment else "已收到处理意见，将依据现有材料分析，不自动补造缺失信息"), kind="human",
        payload={"issue_id": str(issue.id), "agent_request_no": request_no})
    return {"run_id": str(run.id), "issue_id": str(issue.id), "status": "queued",
        "translation_revision_id": str(revision.id) if revision else None,
        "message": ("已受理推荐，正在生成并核验对应译段的修改" if segment else "已受理推荐，正在分析现有材料并说明核验或补件要求；疑点保持待处理") if apply else ("已受理修改意见，正在处理对应译段" if segment else "已受理处理意见，正在核对材料并生成建议；问题保持待处理")}
