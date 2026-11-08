"""工作流命令。调用者持有事务与租户上下文，提交由 API 或对话入口统一负责。"""

from __future__ import annotations
from app.languages import LANGUAGES, language_name

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError, InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.api.idempotency import request_digest
from app.config import ModelRole, Settings
from app.conversation.models import Conversation
from app.documents.evidence_models import (
    BusinessRevision, BusinessRevisionField, DocumentSection, FieldAssertion, ParseArtifact, ParseQuality,
    SectionAssignmentDecision, SourceSegment,
)
from app.documents.models import Case, DocumentVersion
from app.documents.retention import load_case_for_read, load_case_for_write, touch_material_event
from app.identity.service import Principal
from app.infra.model_gateway import DefaultModelGateway
from app.prompts.registry import PromptUnavailable, get_prompt_registry
from app.router.contracts import SelectedSectionRef, SourceVersion
from app.workflows.models import (
    DraftStatus, ExecutionEpoch, InputRequest, InputRequestStatus, QueueOutbox,
    Run, RunDocument, RunEvent, RunStatus, StepExecution, StepStatus, TaskDraft,
)
from app.workflows.registry import WORKFLOWS, get_workflow
from app.workflows.contracts import ReadParameters, TranslateParameters
from app.review.jurisdiction import CERTIFICATE_PACK, MATERIAL_PACK, CertificateInputs, ConsumerMaterialInputs, published_rule_packs


def conflict(code: str, message: str, **current: Any) -> ApiError:
    return ApiError(message, code=code, status_code=409, current=current or None)


def require_permission(principal: Principal, action: str) -> None:
    if action not in principal.permissions:
        raise NotFoundOrForbidden()


def workflow_capability(settings: Settings, workflow_type: str, version: int = 1) -> tuple[bool, str | None]:
    if (workflow_type, version) not in WORKFLOWS:
        return False, "该工作流尚未发布"
    budget_ready, budget_reason = settings.model_budget_ready(get_workflow(workflow_type, version).model_roles)
    if not budget_ready:
        return False, budget_reason
    if not settings.parser_ready():
        return False, "文档解析服务未配置完整"
    gateway = DefaultModelGateway(settings)
    definition = get_workflow(workflow_type, version)
    roles, keys = definition.model_roles, definition.prompt_keys
    for role in roles:
        ready, reason = gateway.role_ready(role)
        if not ready:
            return False, reason
    try:
        get_prompt_registry().freeze(keys)
    except PromptUnavailable:
        return False, "任务所需提示词未通过版本校验"
    return True, None


def workflows_view(settings: Settings) -> list[dict]:
    result = []
    for definition in WORKFLOWS.values():
        enabled, reason = workflow_capability(settings, definition.workflow_type, definition.version)
        result.append({
            "workflow_type": definition.workflow_type, "workflow_version": definition.version,
            "title": definition.title, "enabled": enabled, "disabled_reason": reason,
            "stages": list(definition.stages),
            "visible_stages": list(definition.stages),
            "accepted_mime": list(definition.input_mime),
            "max_file_bytes": settings.upload_max_bytes,
            "output_types": list(definition.output_types),
            "supported_languages": list(definition.supported_languages),
            "supported_formats": list(definition.supported_formats),
            "required_permissions": list(definition.required_permissions), "graph_version": definition.graph_version,
            "cost_level": definition.cost_level, "ui_contract": definition.ui_contract(), "artifact_policy": definition.artifact_policy(),
            "task_form_schema": definition.parameter_model.model_json_schema(),
            "is_synthetic": settings.using_fake_externals(),
            "published_rule_packs": published_rule_packs() if definition.workflow_type == "translate_review" else [],
        })
    return result


def normalize_parameters(workflow_type: str, supplied: dict | None, *, version: int = 1) -> tuple[dict, list[str], list[str]]:
    if (workflow_type, version) not in WORKFLOWS:
        raise InvalidRequest("工作流尚未发布")
    definition = get_workflow(workflow_type, version)
    try:
        parsed = definition.parameter_model.model_validate(supplied or {})
        if workflow_type in {"read_index", "consignment_check"}:
            if workflow_type == "read_index" and parsed.index_scope not in {"selected_documents", "case"}:
                raise InvalidRequest("识别范围必须是所选原件或本票")
            return parsed.model_dump(mode="json"), [], []
    except ValidationError as exc:
        raise InvalidRequest("任务参数不合法", details={"fields": [str(x["loc"]) for x in exc.errors()]}) from exc
    if parsed.target_language is not None and parsed.target_language not in LANGUAGES:
        raise InvalidRequest("请选择中文、英语、法语、西班牙语或俄语")
    if parsed.translation_scope not in {"all", "goods_description", "selected_sections"}:
        raise InvalidRequest("翻译范围不合法")
    if any(x not in {"docx", "pdf"} for x in parsed.output_formats) or len(set(parsed.output_formats)) != len(parsed.output_formats):
        raise InvalidRequest("输出格式只支持不重复的 DOCX、PDF")
    if parsed.rule_pack_version not in {"core-document@1","core-document@2","core-document@3",CERTIFICATE_PACK,MATERIAL_PACK}:
        raise InvalidRequest("规则包尚未发布，不能冻结不存在的规则版本")
    if parsed.rule_pack_version in {"core-document@1","core-document@2","core-document@3"} and parsed.jurisdiction_inputs is not None:
        raise InvalidRequest("法规输入必须选择对应试行规则包，不能改变通用包行为")
    if parsed.jurisdiction_inputs is not None:
        expected = ConsumerMaterialInputs if parsed.rule_pack_version == MATERIAL_PACK else CertificateInputs
        if not isinstance(parsed.jurisdiction_inputs, expected):
            raise InvalidRequest("材料检查输入与所选规则包版本不一致")
    if parsed.glossary_version is not None:
        raise InvalidRequest("当前尚无已发布的术语表版本")
    if parsed.translation_scope == "goods_description":
        if parsed.scope_selector_version not in {None, "cargo_description@1"}:
            raise InvalidRequest("货描选择器版本未发布")
        parsed.scope_selector_version = "cargo_description@1"
    else:
        parsed.scope_selector_version = None
    if parsed.translation_scope != "selected_sections" and parsed.selected_section_refs:
        raise InvalidRequest("只有指定区段翻译可以提交区段引用")
    missing, questions = [], []
    if parsed.target_language is None:
        missing.append("target_language")
        questions.append("请确认译成中文还是英文？")
    if parsed.translation_scope == "selected_sections" and not parsed.selected_section_refs:
        missing.append("selected_section_refs")
        questions.append("请先识别文件，再选择需要翻译的区段。")
    if not parsed.output_formats:
        missing.append("output_formats")
        questions.append("请选择 DOCX 或 PDF 输出格式。")
    normalized=parsed.model_dump(mode="json")
    if normalized["jurisdiction_inputs"] is None:normalized.pop("jurisdiction_inputs")
    return normalized, missing, questions


async def validate_sources(db: AsyncSession, settings: Settings, case_id: UUID, sources: list) -> list[dict]:
    try:
        values = TypeAdapter(list[SourceVersion]).validate_python(sources)
    except ValidationError as exc:
        raise InvalidRequest("原件版本不合法") from exc
    if not values or len({v.document_id for v in values}) != len(values):
        raise InvalidRequest("至少选择一份原件，同一原件不能重复")
    if len(values) > settings.run_max_files:
        raise InvalidRequest("本次处理文件数量超过限制")
    size, pages, resolved = 0, 0, []
    for ref in values:
        version = (await db.execute(select(DocumentVersion).where(
            DocumentVersion.case_id == case_id, DocumentVersion.document_id == ref.document_id,
            DocumentVersion.version == ref.document_version,
        ))).scalar_one_or_none()
        if version is None:
            raise NotFoundOrForbidden()
        size += version.size_bytes
        pages += version.page_count or 0
        resolved.append({**ref.model_dump(mode="json"), "case_id": str(case_id),
                         "filename": version.filename, "mime": version.mime, "size_bytes": version.size_bytes,
                         "uploaded_by": str(version.uploaded_by), "uploaded_at": version.created_at.isoformat(),
                         "page_count": version.page_count})
    if size > settings.run_max_total_bytes or pages > settings.run_max_total_pages:
        raise InvalidRequest("本次原件大小或已知页数超过处理限制")
    return resolved


async def latest_parse(db: AsyncSession, document_id: UUID, version: int) -> ParseArtifact | None:
    return (await db.execute(select(ParseArtifact).where(
        ParseArtifact.document_id == document_id, ParseArtifact.document_version == version,
        ParseArtifact.quality != ParseQuality.FAILED,
    ).order_by(ParseArtifact.created_at.desc(), ParseArtifact.id.desc()).limit(1))).scalar_one_or_none()


async def section_baseline(db: AsyncSession, case_id: UUID, sources: list[dict], parameters: dict) -> dict | None:
    sources = list({(s["document_id"], s["document_version"]): s for s in sources + parameters.get("comparison_source_versions", [])}.values())
    refs = parameters.get("selected_section_refs", [])
    source_set = {(s["document_id"], s["document_version"]) for s in sources}
    inputs = [(entry["document_id"], entry["document_version"]) for entry in parameters.get("document_type_inputs", [])]
    if len(set(inputs)) != len(inputs) or any(value not in source_set for value in inputs):
        raise InvalidRequest("单据类型声明必须属于本次原件版本且不可重复")
    cert=(parameters.get("jurisdiction_inputs") or {}).get("certificate_source")
    if cert and (cert["document_id"],cert["document_version"]) not in source_set:
        raise InvalidRequest("证书必须属于本任务明确选择的原件版本集合")
    for material in (parameters.get("jurisdiction_inputs") or {}).get("materials", []):
        ref = material["source"]
        if (ref["document_id"], ref["document_version"]) not in source_set:
            raise InvalidRequest("候选材料必须属于本任务明确选择的原件版本集合")
    seen, baseline = set(), {}
    for source in sources:
        artifact=await latest_parse(db,UUID(source["document_id"]),source["document_version"])
        # 新整件任务不复用旧边界策略；明确选择的区段仍冻结原解析，历史任务不改写。
        from app.documents.preprocessing import DOCUMENT_GROUPING_VERSION
        if artifact and not refs and (artifact.layout_manifest or {}).get("document_grouping_version") != DOCUMENT_GROUPING_VERSION:
            continue
        if artifact:baseline[source["document_id"]]=str(artifact.id)
    for raw in refs:
        ref = SelectedSectionRef.model_validate(raw)
        if (str(ref.document_id), ref.document_version) not in source_set or ref.section_id in seen:
            raise InvalidRequest("所选区段必须属于本次原件集合且不可重复")
        seen.add(ref.section_id)
        section = await db.get(DocumentSection, ref.section_id)
        artifact = await latest_parse(db, ref.document_id, ref.document_version)
        if section is None or section.case_id != case_id or section.logical_document_id != ref.logical_document_id or section.parse_artifact_id != ref.parse_artifact_id:
            raise NotFoundOrForbidden()
        if artifact is None or artifact.id != ref.parse_artifact_id:
            raise conflict("TASK_DRAFT_PARSE_STALE", "所选区段已重识别，请重新选择", current_parse_artifact_id=str(artifact.id) if artifact else None)
        baseline[str(ref.document_id)] = str(ref.parse_artifact_id)
    return baseline or None


def draft_view(draft: TaskDraft) -> dict:
    from app.config import get_settings
    from app.workflows.costs import estimate_workflow_cost
    estimate = getattr(draft, "_cost_estimate", None) or estimate_workflow_cost(get_settings(), draft.workflow_type, draft.source_versions, draft.draft_parameters)
    return {
        "draft_task_id": str(draft.id), "case_id": str(draft.case_id),
        "workflow_type": draft.workflow_type, "workflow_version": draft.workflow_version,
        "source_versions": draft.source_versions, "draft_parameters": draft.draft_parameters,
        "status": draft.status.value, "missing_fields": draft.missing_fields,
        "questions": draft.questions, "task_summary": draft.task_summary,
        "expires_at": draft.expires_at, "draft_version": draft.draft_version,
        "started_run_id": str(draft.started_run_id) if draft.started_run_id else None,
        "cost_estimate": estimate,
    }


def refresh_draft(draft: TaskDraft, settings: Settings) -> None:
    parameters, missing, questions = normalize_parameters(draft.workflow_type, draft.draft_parameters, version=draft.workflow_version)
    enabled, reason = workflow_capability(settings, draft.workflow_type, draft.workflow_version)
    from app.workflows.costs import estimate_workflow_cost
    draft._cost_estimate = estimate_workflow_cost(settings, draft.workflow_type, draft.source_versions, parameters)
    draft.draft_parameters, draft.missing_fields, draft.questions = parameters, missing, questions
    draft.status = DraftStatus.UNSUPPORTED if not enabled else DraftStatus.NEEDS_CLARIFICATION if missing else DraftStatus.READY
    if not enabled:
        draft.questions = [reason or "工作流暂不可用", *questions]
    if draft.workflow_type == "read_index":
        draft.task_summary = f"识别并索引 {len(draft.source_versions)} 份原件，不生成译本"
    elif draft.workflow_type == "consignment_check":
        draft.task_summary = f"核对同票 {len(draft.source_versions)} 份原件的主体、路线、货物及差异"
    else:
        language = language_name(parameters["target_language"])
        scope = {"all": "全文", "goods_description": "货物描述", "selected_sections": "所选区段"}[parameters["translation_scope"]]
        draft.task_summary = f"{len(draft.source_versions)} 份原件，翻译{scope}为{language}，输出 {'、'.join(parameters['output_formats']).upper() or '待选格式'}"
        if parameters["rule_pack_version"]==CERTIFICATE_PACK:
            facts=parameters.get("jurisdiction_inputs") or {}
            draft.task_summary += f"；试行证书材料检查 {facts.get('origin_country') or '原产待确认'}→{facts.get('destination_country') or '目的待确认'}，{facts.get('commodity') or '货物待确认'}，进口日 {facts.get('import_date') or '待确认'}（法律审定/真实性/海关合规未评估）"
        if parameters["rule_pack_version"] == MATERIAL_PACK:
            facts = parameters.get("jurisdiction_inputs") or {}
            draft.task_summary += f"；一般非危险消费品候选材料核对 {facts.get('dispatch_country') or '发运待确认'}→{facts.get('destination_country') or '目的待确认'}（本票材料候选；法律审定/真实性/专项合规未评估）"


async def prepare_draft(db: AsyncSession, principal: Principal, settings: Settings, *, case_id: UUID,
                        workflow_type: str, source_versions: list, draft_parameters: dict | None = None,
                        conversation_id: UUID | None = None, workflow_version: int = 1) -> TaskDraft:
    if (workflow_type, workflow_version) not in WORKFLOWS:
        raise InvalidRequest("工作流尚未发布")
    definition = get_workflow(workflow_type, workflow_version)
    for permission in definition.required_permissions:
        require_permission(principal, permission)
    if conversation_id:
        await draft_conversation(db, principal, conversation_id, case_id)
    case = await load_case_for_write(db, case_id, user_id=principal.user_id, settings=settings)
    sources = await validate_sources(db, settings, case_id, source_versions)
    if any(source["mime"] not in definition.input_mime for source in sources):
        raise InvalidRequest("原件格式不在该工作流支持范围")
    parameters, _, _ = normalize_parameters(workflow_type, draft_parameters, version=workflow_version)
    if workflow_type == "consignment_check" or workflow_type == "translate_review" and workflow_version >= 2:
        from app.consignment.service import freeze_task_scope
        parameters = await freeze_task_scope(db, principal, settings, case, sources, parameters)
        if workflow_type == "consignment_check":
            from app.consignment.models import CaseScopeRevision
            scope = await db.get(CaseScopeRevision, UUID(parameters["scope_revision_id"]))
            sources = await validate_sources(db, settings, case_id, [m for m in scope.payload["materials"] if m["purpose"] != "excluded"])
        else:
            await validate_sources(db, settings, case_id, parameters["comparison_source_versions"])
    baseline = await section_baseline(db, case_id, sources, parameters)
    conversation = None
    if conversation_id:
        conversation = await draft_conversation(db, principal, conversation_id, case_id, locked=True)
        if conversation.status != "active":
            raise NotFoundOrForbidden()
    draft = TaskDraft(
        tenant_id=principal.tenant_id, case_id=case_id, conversation_id=conversation_id,
        created_by=principal.user_id, workflow_type=workflow_type, workflow_version=definition.version,
        source_versions=sources, draft_parameters=parameters, parse_baseline=baseline,
        status=DraftStatus.READY, expires_at=min(datetime.now(UTC) + timedelta(hours=24), case.retention_expires_at),
    )
    refresh_draft(draft, settings)
    db.add(draft)
    await db.flush()
    if conversation:
        conversation.case_id = case_id
        conversation.active_task_draft_id = draft.id
        conversation.conversation_version += 1
        conversation.updated_at = datetime.now(UTC)
    touch_material_event(case, settings)
    return draft


async def draft_conversation(db: AsyncSession, principal: Principal, conversation_id: UUID, case_id: UUID,
                             *, locked: bool = False) -> Conversation:
    if not locked:
        from app.conversation.service import lock_conversation_binding
        await lock_conversation_binding(db, principal, conversation_id)
    query = select(Conversation).where(
        Conversation.id == conversation_id, Conversation.tenant_id == principal.tenant_id,
        Conversation.owner_user_id == principal.user_id,
    ).execution_options(populate_existing=True)
    if locked:
        query = query.with_for_update()
    conversation = (await db.execute(query)).scalar_one_or_none()
    if conversation is None:
        raise NotFoundOrForbidden()
    if conversation.case_id is not None and conversation.case_id != case_id:
        raise conflict("CONVERSATION_CASE_CONFLICT", "一个会话只能处理一票，请新建会话")
    return conversation


async def load_draft(db: AsyncSession, principal: Principal, settings: Settings, draft_id: UUID, *, write: bool = False,
                     allow_discarded: bool = False) -> tuple[TaskDraft, Case]:
    draft = await db.get(TaskDraft, draft_id)
    if draft is None or draft.created_by != principal.user_id:
        raise ApiError("任务草稿不存在或无权访问", code="TASK_DRAFT_NOT_FOUND", status_code=404)
    if write and draft.conversation_id:
        await draft_conversation(db, principal, draft.conversation_id, draft.case_id)
    loader = load_case_for_write if write else load_case_for_read
    case = await loader(db, draft.case_id, user_id=principal.user_id, settings=settings)
    if write:
        draft = (await db.execute(select(TaskDraft).where(TaskDraft.id == draft_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    if draft.discarded_at and not allow_discarded:
        raise ApiError("任务草稿已撤销", code="TASK_DRAFT_NOT_FOUND", status_code=404)
    return draft, case


def check_draft_edit(draft: TaskDraft, expected: int) -> None:
    if draft.started_run_id:
        raise conflict("TASK_DRAFT_ALREADY_STARTED", "任务已启动，改变要求需创建新草稿", started_run_id=str(draft.started_run_id))
    if draft.draft_version != expected:
        raise VersionConflict(current=draft_view(draft))
    if draft.expires_at <= datetime.now(UTC):
        raise conflict("TASK_DRAFT_EXPIRED", "草稿已过期，请重新确认处理要求")


async def patch_draft(db: AsyncSession, principal: Principal, settings: Settings, *, draft_id: UUID,
                      expected_draft_version: int, draft_parameters: dict | None = None,
                      source_versions: list | None = None, source_version_document_ids: list[UUID] | None = None) -> TaskDraft:
    require_permission(principal, "run_start")
    draft, case = await load_draft(db, principal, settings, draft_id, write=True)
    check_draft_edit(draft, expected_draft_version)
    if source_versions is not None and source_version_document_ids is not None:
        raise InvalidRequest("不能同时按完整版本和文件 ID 修改原件集合")
    if source_version_document_ids is not None:
        # 只从既有冻结集合删选，不把用户没看到的新上传版本悄悄纳入。
        selected = {str(x) for x in source_version_document_ids}
        source_versions = [s for s in draft.source_versions if s["document_id"] in selected]
        if len(source_versions) != len(selected):
            raise InvalidRequest("新增原件必须提交明确的原件版本")
    if source_versions is not None:
        draft.source_versions = await validate_sources(db, settings, draft.case_id, source_versions)
    draft.draft_parameters = {**draft.draft_parameters, **(draft_parameters or {})}
    if draft.workflow_type == "consignment_check" or draft.workflow_type == "translate_review" and draft.workflow_version >= 2:
        from app.consignment.service import freeze_task_scope
        draft.draft_parameters = await freeze_task_scope(db, principal, settings, case, draft.source_versions, draft.draft_parameters)
        if draft.workflow_type == "translate_review":
            await validate_sources(db, settings, case.id, draft.draft_parameters["comparison_source_versions"])
    if source_versions is not None and "document_type_inputs" not in (draft_parameters or {}):
        selected_keys = {(value["document_id"], value["document_version"]) for value in draft.source_versions}
        draft.draft_parameters["document_type_inputs"] = [value for value in draft.draft_parameters.get("document_type_inputs", [])
            if (value["document_id"], value["document_version"]) in selected_keys]
    refresh_draft(draft, settings)
    draft.parse_baseline = await section_baseline(db, draft.case_id, draft.source_versions, draft.draft_parameters)
    draft.draft_version += 1
    touch_material_event(case, settings)
    await db.flush()
    return draft


async def append_run_event(db: AsyncSession, run: Run, *, message: str, event_type: str = "run_status",
                           kind: str = "process", payload: dict | None = None) -> RunEvent:
    """调用者先锁 Run；序号与领域变化在同一事务提交。"""
    run.next_event_seq += 1
    run.updated_at = datetime.now(UTC)
    event = RunEvent(tenant_id=run.tenant_id, run_id=run.id, seq=run.next_event_seq,
                     event_type=event_type, status=run.status.value, stage=run.current_stage,
                     message=message, kind=kind, payload=payload)
    db.add(event)
    await db.flush()
    return event


async def enqueue_epoch(db: AsyncSession, run: Run, *, purpose: str, model_bindings: list[dict] | None = None,
                        parse_artifact_ids: list[str] | None = None) -> ExecutionEpoch:
    if model_bindings is None:
        prior = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id)
                    .order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one_or_none()
        model_bindings = prior.model_bindings if prior else []
    epoch = ExecutionEpoch(tenant_id=run.tenant_id, run_id=run.id, epoch=run.current_epoch,
                           purpose=purpose, graph_version=f"{run.workflow_type}@{run.workflow_version}",
                           input_business_revision_id=run.current_business_revision_id,
                           input_parse_artifact_ids=parse_artifact_ids or [], model_bindings=model_bindings)
    db.add(epoch)
    db.add(QueueOutbox(tenant_id=run.tenant_id, topic="run", dedupe_key=f"run:{run.id}:{run.current_epoch}",
                       payload={"run_id": str(run.id), "epoch": run.current_epoch}))
    await db.flush()
    return epoch


async def start_run(db: AsyncSession, principal: Principal, settings: Settings, *, draft_task_id: UUID,
                    expected_draft_version: int, idempotency_key: str) -> Run:
    require_permission(principal, "run_start")
    draft, case = await load_draft(db, principal, settings, draft_task_id, write=True, allow_discarded=True)
    conversation = None
    if draft.conversation_id:
        conversation = await draft_conversation(db, principal, draft.conversation_id, draft.case_id, locked=True)
        if conversation.case_id != draft.case_id:
            raise conflict("CONVERSATION_CASE_CONFLICT", "草稿与会话的单票归属不一致，请新建会话")
    if draft.discarded_at:
        raise conflict("TASK_DRAFT_DISCARDED", "任务草稿已撤销，不能启动")
    check_draft_edit(draft, expected_draft_version)
    draft.source_versions = await validate_sources(db, settings, draft.case_id, draft.source_versions)
    refresh_draft(draft, settings)
    if draft.status is not DraftStatus.READY:
        raise conflict("TASK_DRAFT_NOT_READY", "任务要求尚未明确或服务不可用", **draft_view(draft))
    baseline = await section_baseline(db, draft.case_id, draft.source_versions, draft.draft_parameters)
    if baseline != draft.parse_baseline:
        raise conflict("TASK_DRAFT_PARSE_STALE", "区段解析基线已变化，请重新选择", old_parse_baseline=draft.parse_baseline, new_parse_baseline=baseline)
    if draft.workflow_type == "consignment_check" or draft.workflow_type == "translate_review" and draft.workflow_version >= 2:
        from app.consignment.service import validate_task_scope
        await validate_task_scope(db, principal, settings, case, draft.source_versions, draft.draft_parameters)
    raw = dict(case_id=str(draft.case_id), workflow_type=draft.workflow_type, workflow_version=draft.workflow_version,
               source_versions=draft.source_versions, initiated_by=str(principal.user_id), created_at=datetime.now(UTC),
               idempotency_key=idempotency_key, **draft.draft_parameters)
    definition = get_workflow(draft.workflow_type, draft.workflow_version)
    for permission in definition.required_permissions:
        require_permission(principal, permission)
    if any(source["mime"] not in definition.input_mime for source in draft.source_versions):
        raise InvalidRequest("原件格式不在该工作流支持范围")
    spec = definition.task_spec_model.model_validate(raw).model_dump(mode="json")
    from app.review.publications import freeze_publications
    spec["reviewed_rule_publications"] = freeze_publications(settings)
    if spec.get("jurisdiction_inputs") is None:spec.pop("jurisdiction_inputs",None)
    from app.workflows.costs import freeze_ocr_binding
    spec["ocr_binding"] = freeze_ocr_binding(settings)
    keys = list(definition.prompt_keys)
    roles = list(definition.model_roles)
    gateway = DefaultModelGateway(settings)
    if gateway.role_ready(ModelRole.VISION, needs_image=True)[0]:
        keys.append("document.vision_region")
        roles.append(ModelRole.VISION)
    prompts = get_prompt_registry().freeze(keys)
    bindings = [gateway.binding_for(role).model_dump(mode="json") for role in roles]
    budget_ready, budget_reason = settings.model_budget_ready(tuple(roles))
    if not budget_ready:
        raise InvalidRequest(budget_reason)
    run = Run(tenant_id=principal.tenant_id, case_id=draft.case_id, draft_task_id=draft.id,
              workflow_type=draft.workflow_type, workflow_version=draft.workflow_version,
              task_spec=spec, task_spec_hash=request_digest(spec), prompt_versions=prompts,
              target_language=spec.get("target_language"), initiated_by=principal.user_id,
              status=RunStatus.QUEUED, current_stage="等待处理", current_epoch=1,
              current_business_revision_id=case.current_business_revision_id)
    db.add(run)
    await db.flush()
    for ordinal, source in enumerate(draft.source_versions):
        db.add(RunDocument(tenant_id=principal.tenant_id, case_id=case.id, run_id=run.id,
                           ordinal=ordinal, document_id=UUID(source["document_id"]), document_version=source["document_version"]))
    draft.started_run_id = run.id
    if conversation and conversation.status == "active":
        conversation.active_run_id = run.id
        conversation.active_task_draft_id = None
        conversation.conversation_version += 1
    await enqueue_epoch(db, run, purpose="initial", model_bindings=bindings,
                        parse_artifact_ids=list((draft.parse_baseline or {}).values()))
    await append_run_event(db, run, message="任务要求已确认，已进入处理队列")
    from app.management.index import refresh_search_projection
    await db.run_sync(lambda sync:refresh_search_projection(sync,run))
    touch_material_event(case, settings)
    return run


async def load_run(db: AsyncSession, principal: Principal, settings: Settings, run_id: UUID, *, write: bool = False) -> tuple[Run, Case]:
    run = await db.get(Run, run_id)
    if run is None:
        raise NotFoundOrForbidden()
    loader = load_case_for_write if write else load_case_for_read
    case = await loader(db, run.case_id, user_id=principal.user_id, settings=settings)
    if write:
        run = (await db.execute(select(Run).where(Run.id == run_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    return run, case


def public_run_failure(run, steps):
    if run.status not in {RunStatus.FAILED_RETRYABLE, RunStatus.FAILED_FINAL, RunStatus.PAUSED_RECONCILE}:
        return None
    failed = sorted((step for step in steps if step.epoch == run.current_epoch
        and step.status in {StepStatus.FAILED, StepStatus.NEEDS_RECONCILE}), key=lambda step: step.created_at, reverse=True)
    if failed:
        usage = failed[0].usage or {}
        calls = usage.get("model_calls", usage.get("calls", []))
        if getattr(failed[0], "error_class", None) == "binding_mismatch":
            return "The task's saved model configuration differs from the current service configuration. A controlled model migration is required. Uploaded files and completed parsing have been preserved."
        if getattr(failed[0], "error_class", None) == "transient_exhausted":
            return "The model service did not complete the request within the configured retry limits. Completed steps are preserved. You can retry this task."
        if getattr(failed[0], "error_class", None) == "budget_exhausted":
            return "This task reached its model request, token, or cost limit before the next request could be sent. Uploaded files and completed results are preserved. Increase the processing budget or reduce the document range before retrying."
        if getattr(failed[0], "error_class", None) == "expired_lease_acceptance_unknown" and calls and calls[-1].get("outcome")=="interrupted":
            return "Processing paused after a model connection interruption and an expired task lease. Uploaded files and completed results are preserved. A recovery check is required before this batch can resume."
        for record in reversed(calls):
            if record.get("outcome") == "succeeded":
                break
            status = record.get("http_status")
            if status == 524:
                return "The upstream model service timed out (HTTP 524). Uploaded files and completed steps have been preserved."
            if status in {502, 503, 504}:
                return f"The upstream model service is temporarily unavailable (HTTP {status}). Uploaded files and completed steps have been preserved."
            if status in {401, 403}:
                return f"The model service rejected authentication (HTTP {status}). Check the configured credentials."
            if record.get("outcome") == "needs_reconcile" and status is None:
                return "The model request was interrupted before a complete response could be confirmed. Uploaded files and completed steps have been preserved. Verify the request with the model provider before retrying."
    return run.failure_reason


async def run_view(db: AsyncSession, run: Run, case: Case, principal: Principal) -> dict:
    sources = []
    for item in run.task_spec["source_versions"]:
        version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == UUID(item["document_id"]),
                  DocumentVersion.version == item["document_version"]))).scalar_one()
        sources.append({**item, "case_id": str(case.id), "filename": version.filename,
                        "mime": version.mime, "size_bytes": version.size_bytes,
                        "uploaded_by": str(version.uploaded_by), "uploaded_at": version.created_at.isoformat(), "page_count": version.page_count})
    request = (await db.execute(select(InputRequest).where(InputRequest.run_id == run.id,
                  InputRequest.status == InputRequestStatus.ACTIVE))).scalar_one_or_none()
    unknown = (await db.execute(select(StepExecution).where(StepExecution.run_id == run.id,
               StepExecution.status.in_([StepStatus.NEEDS_RECONCILE, StepStatus.SUBMITTING])).limit(1))).scalar_one_or_none()
    actions, reasons = ["view", "open_review"], {}
    editable = "run_start" in principal.permissions
    if editable and run.status in {RunStatus.QUEUED, RunStatus.PROCESSING, RunStatus.AWAITING_INPUT, RunStatus.FAILED_RETRYABLE, RunStatus.PAUSED_RECONCILE}:
        actions.append("cancel")
    else:
        reasons["cancel"] = "当前状态或权限不允许取消"
    if editable and run.status is RunStatus.FAILED_RETRYABLE and unknown is None:
        actions.append("retry")
    else:
        reasons["retry"] = "外部受理未明确时须先运营核查" if unknown or run.status is RunStatus.PAUSED_RECONCILE else "仅可重试失败的处理步骤可重试，导出须重试原导出尝试"
    if request and editable:
        actions.append("resolve_input")
    from app.review import models as rm
    from app.review.commands import issue_view
    from app.consignment.service import review_issues, review_checks
    issues = await db.run_sync(lambda session: review_issues(session, run, case))
    issue_views=[await issue_view(db,x) for x in issues]
    checks = await db.run_sync(lambda session: review_checks(session, run, case))
    from app.exports.models import ExportSnapshot,ExportAttempt
    from app.exports.gate import review_gate
    snapshot=(await db.execute(select(ExportSnapshot).where(ExportSnapshot.run_id==run.id)
        .order_by(ExportSnapshot.exported_at.desc()).limit(1))).scalar_one_or_none()
    attempt=(await db.execute(select(ExportAttempt).where(ExportAttempt.run_id==run.id)
        .order_by(ExportAttempt.created_at.desc()).limit(1))).scalar_one_or_none()
    confirmation=None
    if run.workflow_type=="translate_review" and run.current_translation_revision_id:
        gate=await db.run_sync(lambda session:review_gate(session,run,can_confirm="export_confirm" in principal.permissions))
        if gate["confirmation_status"]=="valid":confirmation=gate["current_review_confirmation_id"]
        if gate["confirmable"] and "export_confirm" in principal.permissions:
            actions.append("confirm_review")
            if confirmation:actions.append("export")
    from app.workflows.costs import workflow_cost_summary
    cost_steps = list((await db.execute(select(StepExecution).where(StepExecution.run_id == run.id))).scalars())
    calls = [record for step in cost_steps if step.usage for record in step.usage.get("model_calls", step.usage.get("calls", []))]
    ocr_steps = [{"step_id": str(step.id), "external_task_id": step.external_task_id, "ocr_cost": (step.usage or {}).get("ocr_cost")}
                 for step in cost_steps if (step.usage or {}).get("ocr_cost") or (step.external_task_id and step.step_name.startswith("解析"))]
    model_steps = [{"status":step.status.value} for step in cost_steps
                   if step.step_name.startswith(("抽取:", "翻译批次:", "语义QA批次:", "视觉复读:"))]
    return {
        "cost_summary": workflow_cost_summary(calls, ocr_steps, model_steps=model_steps, run_status=run.status.value),
        "run_id": str(run.id), "case_id": str(case.id), "case_name": case.name,
        "workflow_type": run.workflow_type, "workflow_version": run.workflow_version,
        "task_spec_hash": run.task_spec_hash, "source_versions": sources,
        "initiated_by": str(run.initiated_by), "initiated_at": run.created_at,
        "target_language": run.target_language, "translation_scope": run.task_spec.get("translation_scope"),
        "output_formats": run.task_spec.get("output_formats", []), "status": run.status.value,
        "rule_pack_version":run.task_spec.get("rule_pack_version"), "jurisdiction_inputs":run.task_spec.get("jurisdiction_inputs"),
        "current_stage": run.current_stage, "execution_epoch": run.current_epoch, "last_seq": run.next_event_seq,
        "has_successful_snapshot": snapshot is not None, "latest_success_snapshot_id": str(snapshot.id) if snapshot else None, "latest_success_snapshot_at": snapshot.exported_at if snapshot else None,
        "current_business_revision_id": str(run.current_business_revision_id) if run.current_business_revision_id else None,
        "current_translation_revision_id": str(run.current_translation_revision_id) if run.current_translation_revision_id else None,
        "current_review_confirmation_id": confirmation, "confirmation_status": "valid" if confirmation else "none", "blocking_issue_count": sum(v["blocking_delivery"] for v in issue_views),
        "warning_issue_count": sum(v["severity"]=="warning" and v["status"] in {"open","unresolved"} for v in issue_views),
        "undetermined_issue_count": sum(v["severity"]=="undetermined" and v["status"]=="open" or v["status"]=="unresolved" for v in issue_views) if checks else None,
        "rules_status": "run" if checks else "not_run",
        "active_input_request_id": str(request.id) if request else None,
        "latest_export_attempt_id": str(attempt.id) if attempt else None, "latest_export_attempt_status": attempt.status if attempt else None, "latest_export_attempt_error_summary": attempt.error_summary if attempt else None,
        "allowed_actions": actions, "disabled_reasons": reasons,
        "ops_reconcile_step_id": str(unknown.id) if unknown and "ops_reconcile" in principal.permissions else None,
        "pause_explanation": run.pause_explanation, "failure_reason": public_run_failure(run, cost_steps), "updated_at": run.updated_at,
    }


def input_view(request: InputRequest) -> dict:
    from app.documents.type_catalog import DOCUMENT_PROFILES
    type_labels = {profile.code:profile.label for profile in DOCUMENT_PROFILES}
    return {"input_request_id": str(request.id), "run_id": str(request.run_id), "kind": request.kind,
            "schema_version": request.schema_version, "request_version": request.request_version,
            "source_refs": request.source_refs, "question": request.question,
            "options": [{**{k: v for k, v in item.items() if k in {"option_id", "label", "detail", "source_ref", "excerpt"}},
                "label": type_labels.get(item["option_id"], item.get("label", item["option_id"])) if request.kind == "document_boundary" else item.get("label", item["option_id"])} for item in request.options],
            "suggested_value": request.suggested_value, "expected_parse_artifact_id": str(request.expected_parse_artifact_id),
            "expected_business_revision_id": str(request.expected_business_revision_id) if request.expected_business_revision_id else None,
            "status": request.status.value, "created_at": request.created_at}


async def input_request_view(db: AsyncSession, request: InputRequest) -> dict:
    view = input_view(request)
    material_cache={}
    for rendered, option in zip(view["options"], request.options):
        section_ids = [UUID(str(value)) for value in option.get("section_ids", [])]
        if not section_ids:
            continue
        cache_key=tuple(section_ids)
        if cache_key in material_cache:
            rendered.update(material_cache[cache_key]);continue
        sections = (await db.execute(select(DocumentSection).where(
            DocumentSection.id.in_(section_ids), DocumentSection.parse_artifact_id == request.expected_parse_artifact_id,
        ).order_by(DocumentSection.ordinal))).scalars().all()
        if not sections:
            continue
        section = sections[0]
        segments = (await db.execute(select(SourceSegment.text).where(
            SourceSegment.section_id == section.id, SourceSegment.parse_artifact_id == request.expected_parse_artifact_id,
        ).order_by(SourceSegment.ordinal).limit(3))).scalars().all()
        material={"detail": "适用区段：" + "、".join(value.title or "未命名区段" for value in sections),
            "excerpt":"\n".join(segments)[:600], "source_ref": {"case_id": str(section.case_id),
            "run_id": str(request.run_id), "document_id": str(request.document_id), "document_version": request.document_version,
            "parse_artifact_id": str(request.expected_parse_artifact_id), "section_id": str(section.id)}}
        material_cache[cache_key]=material;rendered.update(material)
    return view


async def cancel_run(db: AsyncSession, principal: Principal, settings: Settings, run_id: UUID) -> tuple[Run, Case]:
    require_permission(principal, "run_start")
    run, case = await load_run(db, principal, settings, run_id, write=True)
    if run.status is RunStatus.CANCELLED:
        return run, case
    if run.status not in {RunStatus.QUEUED, RunStatus.PROCESSING, RunStatus.AWAITING_INPUT, RunStatus.FAILED_RETRYABLE, RunStatus.PAUSED_RECONCILE}:
        raise conflict("RUN_CANCEL_NOT_ALLOWED", "当前状态不可取消，已交付内容不会被删除")
    run.execution_fence += 1
    run.status, run.current_stage = RunStatus.CANCELLED, "已取消"
    from app.consignment.models import ComparisonBatch
    batches = (await db.execute(select(ComparisonBatch).where(ComparisonBatch.run_id == run.id,
        ComparisonBatch.status == "building"))).scalars()
    for batch in batches:
        batch.status = "cancelled"
    requests = (await db.execute(select(InputRequest).where(InputRequest.run_id == run.id,
                   InputRequest.status == InputRequestStatus.ACTIVE).with_for_update())).scalars().all()
    for request in requests:
        request.status = InputRequestStatus.CANCELLED
        request.request_version += 1
    await append_run_event(db, run, message="用户已取消本次处理，已提交外部请求的账本仍保留供核查", kind="human")
    from app.conversation.deferred import notify_run_state
    await notify_run_state(db, run)
    return run, case


async def retry_run(db: AsyncSession, principal: Principal, settings: Settings, run_id: UUID) -> tuple[Run, Case]:
    require_permission(principal, "run_start")
    run, case = await load_run(db, principal, settings, run_id, write=True)
    unknown = (await db.execute(select(StepExecution).where(StepExecution.run_id == run.id,
               StepExecution.status.in_([StepStatus.NEEDS_RECONCILE, StepStatus.SUBMITTING])).limit(1))).scalar_one_or_none()
    if unknown or run.status is RunStatus.PAUSED_RECONCILE:
        raise conflict("RECONCILE_REQUIRED", "外部服务可能已受理，请先运营核查，不可重复提交")
    if run.status is not RunStatus.FAILED_RETRYABLE:
        raise conflict("RUN_RETRY_NOT_ALLOWED", "仅允许重试可恢复的处理失败，导出失败须重试原导出尝试")
    run.current_epoch += 1
    run.execution_fence += 1
    run.status, run.current_stage, run.failure_reason = RunStatus.QUEUED, "等待安全重试", None
    previous = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id).order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one_or_none()
    purpose = previous.purpose if previous and previous.purpose in {"issue_repair", "issue_assessment", "quality_preview", "layout_refresh"} else "retry"
    parse_ids = previous.input_parse_artifact_ids if previous else None
    if purpose == "retry" and previous and previous.purpose == "resume_input":
        parse_ids = await input_resume_parse_ids(db, run)
    epoch = await enqueue_epoch(db, run, purpose=purpose, parse_artifact_ids=parse_ids)
    if purpose != "retry":
        epoch.repair_request = dict(previous.repair_request or {})
    await append_run_event(db, run, message="已安排安全重试，复用本任务已有有效产物", kind="human")
    from app.conversation.deferred import notify_run_state
    await notify_run_state(db, run)
    return run, case


async def input_resume_parse_ids(db: AsyncSession, run: Run, answered_parse: ParseArtifact | None = None) -> list[str]:
    """恢复全票解析；已采用业务来源优先，不能只冻结所回答的一份文件。"""
    from app.management.versions import run_parse_ids
    selected = {}
    allowed = {(str(value["document_id"]), value["document_version"]) for value in
        run.task_spec["source_versions"] + run.task_spec.get("comparison_source_versions", [])}
    candidates = await db.run_sync(lambda session: run_parse_ids(session, run))
    adopted = list((await db.execute(select(ParseArtifact).join(FieldAssertion,
        FieldAssertion.parse_artifact_id == ParseArtifact.id).join(BusinessRevisionField,
        BusinessRevisionField.field_assertion_id == FieldAssertion.id).where(
        BusinessRevisionField.business_revision_id == run.current_business_revision_id,
        ParseArtifact.case_id == run.case_id, ParseArtifact.quality != ParseQuality.FAILED).distinct())).scalars()) if run.current_business_revision_id else []
    for value in candidates:
        parse = await db.get(ParseArtifact, value)
        if parse and parse.case_id == run.case_id and (str(parse.document_id), parse.document_version) in allowed:
            selected[(parse.document_id, parse.document_version)] = parse
    adopted_by_material = {}
    for parse in adopted:
        key = (parse.document_id, parse.document_version)
        if (str(parse.document_id), parse.document_version) not in allowed:
            continue
        if key in adopted_by_material and adopted_by_material[key].id != parse.id:
            raise conflict("PARSE_BASELINE_AMBIGUOUS", "同一原件版本存在多个已采用解析，请先明确解析基线")
        adopted_by_material[key] = parse
    selected.update(adopted_by_material)
    if answered_parse is not None:
        if answered_parse.case_id != run.case_id or (str(answered_parse.document_id), answered_parse.document_version) not in allowed:
            raise NotFoundOrForbidden()
        selected[(answered_parse.document_id, answered_parse.document_version)] = answered_parse
    return [str(parse.id) for parse in selected.values()]


async def resolve_input(db: AsyncSession, principal: Principal, settings: Settings, *, request_id: UUID,
                        selected_option_ids: list[str], expected_request_version: int,
                        expected_parse_artifact_id: UUID, expected_business_revision_id: UUID | None,
                        reason: str | None = None, cargo_profile_ids: list[str] | None = None) -> tuple[Run, Case]:
    require_permission(principal, "run_start")
    request = await db.get(InputRequest, request_id)
    if request is None:
        raise NotFoundOrForbidden()
    run, case = await load_run(db, principal, settings, request.run_id, write=True)
    request = (await db.execute(select(InputRequest).where(InputRequest.id == request_id).with_for_update()
                               .execution_options(populate_existing=True))).scalar_one()
    if request.status is not InputRequestStatus.ACTIVE or run.status is not RunStatus.AWAITING_INPUT:
        raise conflict("INPUT_REQUEST_STALE", "该问题已处理或被新解析替代，请读取最新任务")
    parse = await latest_parse(db, request.document_id, request.document_version)
    if request.request_version != expected_request_version or request.expected_parse_artifact_id != expected_parse_artifact_id or parse is None or parse.id != expected_parse_artifact_id:
        raise conflict("INPUT_REQUEST_STALE", "解析版或问题版本已改变，请重新核对", **input_view(request))
    if request.expected_business_revision_id != expected_business_revision_id or case.current_business_revision_id != expected_business_revision_id:
        raise conflict("INPUT_REQUEST_STALE", "业务版已变化，请重新核对", current_business_revision_id=str(case.current_business_revision_id) if case.current_business_revision_id else None)
    if request.schema_version != 1 or request.kind not in {"document_boundary", "source_language", "translation_scope"}:
        raise InvalidRequest("该输入请求版本尚未支持")
    options = {str(x["option_id"]): x for x in request.options}
    if not selected_option_ids or len(set(selected_option_ids)) != len(selected_option_ids) or any(x not in options for x in selected_option_ids):
        raise InvalidRequest("请选择本问题提供的不重复候选；无候选时需补充材料或申请重识别")
    if request.kind != "translation_scope" and len(selected_option_ids) != 1:
        raise InvalidRequest("该问题只能选择一个候选")
    selected = [options[x] for x in selected_option_ids]
    from app.documents.type_catalog import normalize_cargo_profiles, is_document_type_code
    if cargo_profile_ids is not None and request.kind!="document_boundary":raise InvalidRequest("货物领域只随单据类型确认提交")
    try:cargo_profiles=normalize_cargo_profiles(cargo_profile_ids or [])
    except ValueError as exc:raise InvalidRequest(str(exc)) from exc
    if request.kind=="document_boundary" and any(not is_document_type_code(x.get("logical_document_type")) for x in selected):
        raise InvalidRequest("单据类型不在当前目录中，请重新加载候选")
    if any(x.get("requires_new_run") for x in selected):
        raise conflict("NEW_RUN_REQUIRED", "该选择改变冻结的任务范围，请创建新任务", draft_parameters=run.task_spec)
    for option in selected:
        for segment_id in option.get("source_segment_ids", []):
            segment = await db.get(SourceSegment, UUID(str(segment_id)))
            if segment is None or segment.parse_artifact_id != parse.id:
                raise InvalidRequest("候选来源不属于本问题的解析版本")
    moment = datetime.now(UTC)
    request.answer = {"selected_option_ids": selected_option_ids, "selected_options": selected, "reason": reason or "用户确认候选", "cargo_profile_ids":cargo_profiles}
    request.status, request.answered_by, request.resolved_at = InputRequestStatus.RESOLVED, principal.user_id, moment
    request.request_version += 1
    # 只记录真实业务覆盖，机器候选和源文不原位修改；首次分类无业务版则由恢复抽取形成基线。
    if case.current_business_revision_id and request.kind in {"document_boundary", "source_language"}:
        revision = BusinessRevision(tenant_id=run.tenant_id, case_id=case.id, revision_no=(case.business_revision_no or 0) + 1,
                                    parent_revision_id=case.current_business_revision_id, reason="section_assignment", created_by=principal.user_id)
        db.add(revision)
        await db.flush()
        previous = (await db.execute(select(BusinessRevisionField).where(BusinessRevisionField.business_revision_id == case.current_business_revision_id))).scalars().all()
        for field in previous:
            db.add(BusinessRevisionField(tenant_id=run.tenant_id, business_revision_id=revision.id, field_assertion_id=field.field_assertion_id))
        changed_sections = {UUID(value) for option in selected for value in option.get("section_ids", [])}
        if not changed_sections:
            changed_sections = {UUID(ref["section_id"]) for ref in request.source_refs if ref.get("section_id")}
        prior_decisions = (await db.execute(select(SectionAssignmentDecision).where(
            SectionAssignmentDecision.business_revision_id == case.current_business_revision_id))).scalars().all()
        for old in prior_decisions:
            if old.section_id in changed_sections:continue
            db.add(SectionAssignmentDecision(tenant_id=run.tenant_id, case_id=case.id, business_revision_id=revision.id,
                section_id=old.section_id, logical_document_type=old.logical_document_type, cargo_profile_ids=old.cargo_profile_ids,
                target_logical_document_id=old.target_logical_document_id, confirmed_language=old.confirmed_language,
                decided_by=old.decided_by, reason=old.reason, created_at=old.created_at))
        for option in selected:
            section_ids = option.get("section_ids", [])
            if not section_ids:
                section_ids = [x["section_id"] for x in request.source_refs if x.get("section_id")]
            for section_id in dict.fromkeys(section_ids):
                section = await db.get(DocumentSection, UUID(str(section_id)))
                if section is None or section.parse_artifact_id != parse.id:
                    raise InvalidRequest("候选区段不属于当前问题")
                db.add(SectionAssignmentDecision(tenant_id=run.tenant_id, case_id=case.id, business_revision_id=revision.id,
                    section_id=section.id, logical_document_type=option.get("logical_document_type"), cargo_profile_ids=cargo_profiles,
                    confirmed_language=option.get("confirmed_language"), decided_by=principal.user_id,
                    reason=reason or "用户确认候选"))
        case.current_business_revision_id, case.business_revision_no = revision.id, revision.revision_no
        run.current_business_revision_id = revision.id
        case.review_state_version += 1
    resume_parse_ids = await input_resume_parse_ids(db, run, parse)
    run.current_epoch += 1
    run.execution_fence += 1
    run.status, run.current_stage = RunStatus.QUEUED, "等待继续处理"
    await enqueue_epoch(db, run, purpose="resume_input", parse_artifact_ids=resume_parse_ids)
    await append_run_event(db, run, message="已保存人工输入，继续原任务处理", kind="human", payload={"input_request_id": str(request.id)})
    from app.conversation.deferred import notify_run_state
    await notify_run_state(db, run)
    touch_material_event(case, settings)
    return run, case
