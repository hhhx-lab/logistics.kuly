from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx
from sqlalchemy import select, text

from app.api.errors import InvalidRequest, NotFoundOrForbidden
from app.api.idempotency import request_digest
from app.config import ModelRole
from app.documents import evidence_models as em
from app.infra.model_gateway import build_model_gateway, capabilities_for, successful_step_model_calls
from app.workflows import commands as wc
from app.workflows.models import ExecutionEpoch, ModelMigration, RunStatus, StepExecution, StepReconciliation, StepStatus


def known_response_local_failure_allowed(step):
    # 只开放已定位的本地元数据错误；网络、账本及任意运行时失败不能借此解除未知受理。
    return step.error_class in {"NoReferencedTableError", "NoReferencedColumnError"} and bool(successful_step_model_calls(step,require_real=True))


async def load_step(db,principal,settings,step_id,*,write=False):
    step=await db.get(StepExecution,step_id)
    if step is None:raise NotFoundOrForbidden()
    run,case=await wc.load_run(db,principal,settings,step.run_id,write=write)
    if write:
        step=(await db.execute(select(StepExecution).where(StepExecution.id==step_id).with_for_update())).scalar_one()
    return step,run,case


async def step_view(db,step):
    count=(await db.execute(select(StepReconciliation).where(StepReconciliation.step_execution_id==step.id))).scalars().all()
    decisions=["verified_executed","confirmed_not_executed","still_unknown","terminate_run"] if step.status in {StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE} else []
    if decisions and known_response_local_failure_allowed(step):decisions.append("known_response_local_failure")
    return {"step_execution_id":str(step.id),"run_id":str(step.run_id),"step_name":step.step_name,"status":step.status.value,
        "request_key_summary":request_digest(step.provider_request_key or "")[:12],"external_task_summary":("已持久化任务 "+request_digest(step.external_task_id)[:12]) if step.external_task_id else "供应商未提供可查询任务ID",
        "step_version":len(count)+1,"submitted_at":step.created_at,"allowed_decisions":decisions}


async def reconcile_step(db,principal,settings,step_id,body):
    wc.require_permission(principal,"ops_reconcile")
    step,run,case=await load_step(db,principal,settings,step_id,write=True)
    current=await step_view(db,step)
    if body.expected_step_version!=current["step_version"]:raise wc.conflict("STEP_STALE","核查记录已更新",step_version=current["step_version"])
    if step.status not in {StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE}:raise wc.conflict("STEP_RECONCILE_NOT_ALLOWED","当前步骤无需外部核查")
    if step.lease_until and step.lease_until>datetime.now(UTC) and run.status is RunStatus.PROCESSING:
        raise wc.conflict("STEP_BUSY","请求仍由Worker持有，请等待响应或租约到期")
    if body.decision not in {"verified_executed","confirmed_not_executed","still_unknown","terminate_run","known_response_local_failure"}:raise InvalidRequest("核查结论不合法")
    if not body.reason.strip():raise InvalidRequest("核查必须提供原因")
    if body.decision in {"verified_executed","confirmed_not_executed","known_response_local_failure"} and not body.evidence_reference.strip():raise InvalidRequest("确定外部受理结论必须附可复核凭据")
    if body.decision=="verified_executed":
        # 有供应商已执行凭据但没有已回取产物，仍不能制造成功或授权再提交。
        if not step.artifact_ids:
            raise wc.conflict("RESULT_RETRIEVAL_REQUIRED","确认已执行后须先回取校验结果，当前不能标成功或重投")
        for value in step.artifact_ids:
            if step.step_name.startswith("解析:嵌图:"):
                from app.documents.models import StoredObject
                artifact=await db.get(StoredObject,UUID(value))
                if artifact and (artifact.mime!="application/json" or not artifact.is_visible):raise InvalidRequest("嵌图OCR结果对象类型尚未校验")
            elif "解析" in step.step_name:
                artifact=await db.get(em.ParseArtifact,UUID(value))
            elif "翻译" in step.step_name:
                from app.review.models import TranslationSegment
                artifact=await db.get(TranslationSegment,UUID(value))
            else:
                artifact=await db.get(em.FieldAssertion,UUID(value))
            if artifact is None or artifact.case_id!=case.id:raise InvalidRequest("步骤结果对象尚未校验归属")
        step.status=StepStatus.SUCCEEDED
    elif body.decision=="confirmed_not_executed":
        step.status=StepStatus.FAILED;step.error_class="confirmed_not_executed";step.lease_until=None
    elif body.decision=="known_response_local_failure":
        if not known_response_local_failure_allowed(step):
            raise wc.conflict("KNOWN_RESPONSE_PROOF_REQUIRED","仅真实成功调用账本及已定位本地元数据错误允许恢复，未知受理仍须核查")
        step.status=StepStatus.FAILED;step.lease_until=None
        step.error_detail="已核查成功模型回执，本地持久化失败；未产生可复用业务产物"
    elif body.decision=="terminate_run":
        run.status=RunStatus.CANCELLED;run.execution_fence+=1
        step.status=StepStatus.FAILED;step.error_class="terminated_after_reconciliation"
    else:
        run.status=RunStatus.PAUSED_RECONCILE;step.status=StepStatus.NEEDS_RECONCILE
    db.add(StepReconciliation(tenant_id=principal.tenant_id,step_execution_id=step.id,decision=body.decision,evidence_reference=body.evidence_reference,
        reason=body.reason,decided_by=principal.user_id))
    await db.flush()
    unknown=(await db.execute(select(StepExecution.id).where(StepExecution.run_id==run.id,StepExecution.status.in_([StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE])).limit(1))).scalar_one_or_none()
    if body.decision in {"verified_executed","confirmed_not_executed","known_response_local_failure"} and unknown is None:
        run.current_epoch+=1;run.execution_fence+=1;run.status=RunStatus.QUEUED;run.current_stage="核查后恢复";run.failure_reason=None
        prior=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id).order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one()
        purpose=prior.purpose if prior.purpose in {"issue_repair","issue_assessment","quality_preview","layout_refresh"} else "retry"
        resumed=await wc.enqueue_epoch(db,run,purpose=purpose,parse_artifact_ids=prior.input_parse_artifact_ids)
        if purpose!="retry":resumed.repair_request=dict(prior.repair_request or {})
    await wc.append_run_event(db,run,message="已保存外部请求核查结论",kind="ops",payload={"step_execution_id":str(step.id),"decision":body.decision})
    return await step_view(db,step)


async def migration_options(db,principal,settings,run_id):
    wc.require_permission(principal,"ops_migrate_model")
    run,case=await wc.load_run(db,principal,settings,run_id)
    epoch=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id).order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one()
    unknown=(await db.execute(select(StepExecution.id).where(StepExecution.run_id==run.id,StepExecution.status.in_([StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE])).limit(1))).scalar_one_or_none()
    safe=run.status not in {RunStatus.PROCESSING,RunStatus.EXPORTING,RunStatus.AWAITING_INPUT,RunStatus.CANCELLED,RunStatus.INDEXED,RunStatus.EXPORTED} and unknown is None
    local_issue = await local_assessment_migration_issue(db, run, case, epoch)
    safe = safe or local_issue is not None
    gateway=build_model_gateway(settings)
    try:
        candidates=[];invalid=[]
        for frozen in epoch.model_bindings:
            role=ModelRole(frozen["role"]);candidate=gateway.binding_for(role)
            if candidate is None:invalid.append(role.value);continue
            ready,_=gateway.role_ready(role,needs_image=role is ModelRole.VISION)
            if not ready:invalid.append(role.value)
            candidates.append(candidate.model_dump(mode="json"))
    finally:await gateway.aclose()
    fingerprint=request_digest(candidates)
    old_fingerprint=request_digest(epoch.model_bindings)
    options=[{"fingerprint":fingerprint,"label":"当前已配置模型绑定（仅恢复本次疑点分析）" if local_issue else "当前已配置模型绑定","capability_check":"缺失能力："+",".join(invalid) if invalid else "满足冻结角色与协议能力",
        "invalidated_steps":["assess_issue"] if local_issue else ["extract_fields","validate_documents","translate_segments","quality_and_preview"],"cost_level":"unknown",
        "disabled_reason":"模型未完整配置或能力未验证" if invalid else "当前与冻结配置相同" if fingerprint==old_fingerprint else None}]
    return {"run_id":str(run.id),"execution_epoch":run.current_epoch,"current":{"role":"workflow","fingerprint":old_fingerprint,"label":"任务当前冻结模型绑定"},
        "options":options,"safe_now":safe,"unsafe_reason":None if safe else "执行中、终态或存在未决外部请求，当前不可迁移"},candidates


def assessment_binding_failed_without_send(step, issue_id):
    """只认绑定检查在创建外发尝试前失败，空账本本身不能证明未外发。"""
    usage = {} if step.usage is None else step.usage
    if not isinstance(usage, dict):
        return False
    if any(key in usage and usage[key] != [] for key in ("model_calls", "calls")):
        return False
    if any(key in usage and usage[key] != {} for key in ("model_attempt", "model_response")):
        return False
    return (step.status == StepStatus.FAILED and step.error_class == "binding_mismatch" and step.finished_at is not None
        and step.step_name == "疑点材料分析:" + str(issue_id) and not step.external_task_id and not step.artifact_ids
        and isinstance(usage, dict))


async def local_assessment_migration_issue(db, run, case, epoch):
    """已导出Run仅允许恢复最新、已知零外发的材料分析，不重算已完成交付。"""
    if (run.status != RunStatus.EXPORTED or epoch.epoch != run.current_epoch or epoch.purpose != "issue_assessment"
            or epoch.graph_version != f"{run.workflow_type}@{run.workflow_version}"
            or epoch.input_business_revision_id != run.current_business_revision_id
            or run.current_business_revision_id != case.current_business_revision_id):
        return None
    request = epoch.repair_request or {}
    if (request.get("prompt_key") != "document.issue_assess" or not request.get("instruction")
            or request.get("return_status") != RunStatus.EXPORTED.value
            or request.get("translation_revision_id") != (str(run.current_translation_revision_id) if run.current_translation_revision_id else None)):
        return None
    from app.review import commands as review_commands, models as review_models
    from app.prompts.registry import get_prompt_registry, PromptUnavailable
    from app.workflows.registry import get_workflow
    try:
        issue = await db.get(review_models.Issue, UUID(request["issue_id"]))
        ref = request["prompt"]
        get_prompt_registry().get_frozen("document.issue_assess", ref["version"], ref["sha256"])
        get_workflow(run.workflow_type, run.workflow_version)
    except (KeyError, TypeError, ValueError, PromptUnavailable):
        return None
    if (issue is None or issue.case_id != case.id or issue.run_id != run.id
            or issue.business_revision_id != case.current_business_revision_id):
        return None
    view = await review_commands.issue_view(db, issue)
    if (view["status"] != "open" or view["guidance"].get("agent_status") != "failed"
            or request.get("agent_request_no") != view["guidance"].get("agent_request_no")):
        return None
    if issue.comparison_batch_id:
        from app.consignment.models import ComparisonBatch
        from app.consignment.service import batch_current
        batch = await db.get(ComparisonBatch, issue.comparison_batch_id)
        if not batch or not await db.run_sync(lambda sync: batch_current(sync, case, batch)):
            return None
    if run.current_translation_revision_id:
        revision = await db.get(review_models.TranslationRevision, run.current_translation_revision_id)
        if revision is None or set(epoch.input_parse_artifact_ids) != set(revision.parse_artifact_ids):
            return None
    if not epoch.input_parse_artifact_ids:
        return None
    for value in epoch.input_parse_artifact_ids:
        try:
            parse = await db.get(em.ParseArtifact, UUID(value))
        except (TypeError, ValueError):
            return None
        if parse is None or parse.case_id != case.id:
            return None
    steps = (await db.execute(select(StepExecution).where(StepExecution.run_id == run.id, StepExecution.epoch == epoch.epoch))).scalars().all()
    active = (await db.execute(select(StepExecution.id).where(StepExecution.run_id == run.id,
        StepExecution.status.in_([StepStatus.RUNNING, StepStatus.SUBMITTING, StepStatus.NEEDS_RECONCILE])).limit(1))).scalar_one_or_none()
    return issue if steps and active is None and all(assessment_binding_failed_without_send(step, issue.id) for step in steps) else None


async def migrate_model(db,principal,settings,run_id,body):
    wc.require_permission(principal,"ops_migrate_model")
    run,case=await wc.load_run(db,principal,settings,run_id,write=True)
    options,candidates=await migration_options(db,principal,settings,run_id)
    if body.expected_execution_epoch!=run.current_epoch:raise wc.conflict("EPOCH_STALE","执行批次已变化",execution_epoch=run.current_epoch)
    if not options["safe_now"]:raise wc.conflict("MODEL_MIGRATION_UNSAFE",options["unsafe_reason"])
    selected=next((o for o in options["options"] if o["fingerprint"]==body.candidate_config_fingerprint),None)
    if selected is None or selected["disabled_reason"]:raise InvalidRequest("模型候选未配置、能力不符合或无变化")
    if not body.reason.strip():raise InvalidRequest("模型迁移必须填写原因")
    previous = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id).order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one()
    local_issue = await local_assessment_migration_issue(db, run, case, previous)
    if local_issue:
        old = run.current_epoch; run.current_epoch += 1; run.execution_fence += 1
        db.add(ModelMigration(tenant_id=principal.tenant_id, run_id=run.id, from_epoch=old, to_epoch=run.current_epoch,
            old_binding_fingerprint=options["current"]["fingerprint"], new_binding_fingerprint=selected["fingerprint"],
            invalidated_steps=selected["invalidated_steps"], reason=body.reason, decided_by=principal.user_id))
        run.status = RunStatus.QUEUED; run.current_stage = "模型迁移后继续疑点分析"; run.failure_reason = None
        epoch = await wc.enqueue_epoch(db, run, purpose=previous.purpose, model_bindings=candidates,
            parse_artifact_ids=list(previous.input_parse_artifact_ids))
        epoch.repair_request = {**previous.repair_request, "agent_status": "queued", "agent_response": None}
        if not local_issue.comparison_batch_id:
            local_issue.guidance = {**(local_issue.guidance or {}), "agent_status": "queued", "agent_response": None}
        case.review_state_version += 1
        await wc.append_run_event(db, run, message="已批准本次疑点分析的受控模型迁移，原译文及交付预览保留", kind="ops",
            payload={"issue_id": str(local_issue.id), "agent_request_no": epoch.repair_request["agent_request_no"]})
        return {"accepted": True, "execution_epoch": run.current_epoch}
    parse_ids=await wc.input_resume_parse_ids(db,run)
    old=run.current_epoch;run.current_epoch+=1;run.execution_fence+=1
    db.add(ModelMigration(tenant_id=principal.tenant_id,run_id=run.id,from_epoch=old,to_epoch=run.current_epoch,
        old_binding_fingerprint=options["current"]["fingerprint"],new_binding_fingerprint=selected["fingerprint"],invalidated_steps=selected["invalidated_steps"],reason=body.reason,decided_by=principal.user_id))
    from app.review.models import TranslationSegment,PreviewArtifact,RuleCheck
    affected_ids=[]
    for model in (TranslationSegment,PreviewArtifact,RuleCheck):
        affected_ids.extend((await db.execute(select(model.id).where(model.run_id==run.id))).scalars().all())
    dependencies=(await db.execute(select(em.ArtifactDependency).where(em.ArtifactDependency.case_id==case.id,
        em.ArtifactDependency.artifact_id.in_(affected_ids)).with_for_update())).scalars().all()
    for dependency in dependencies:dependency.stale=True;dependency.stale_since=datetime.now(UTC)
    previews=(await db.execute(select(PreviewArtifact).where(PreviewArtifact.run_id==run.id).with_for_update())).scalars().all()
    for preview in previews:preview.status="stale"
    run.status=RunStatus.QUEUED;run.current_stage="模型迁移后重算";run.failure_reason=None
    await wc.enqueue_epoch(db,run,purpose="model_migration",model_bindings=candidates,parse_artifact_ids=parse_ids)
    await wc.append_run_event(db,run,message="已批准受控模型迁移，保留原任务提示词版本",kind="ops")
    return {"accepted":True,"execution_epoch":run.current_epoch}


async def health_probe(db,principal,settings):
    wc.require_permission(principal,"ops_health")
    return await readiness_probe(db,settings)


async def readiness_probe(db,settings):
    budget_ready,budget_reason=settings.model_budget_ready()
    cost_enforced=settings.run_max_model_cost_cny is not None and budget_ready
    result={"model_budget":{"ok":budget_ready,"enforced_limits":["calls","tokens"]+(["cost_cny"] if cost_enforced else []),
        "cost_cny_enforced":cost_enforced,"cost_scope":"model_only","ocr_cost_known":False}}
    if not budget_ready:result["model_budget"]["reason"]=budget_reason
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from pathlib import Path
        root=Path(__file__).resolve().parents[2]
        config=Config(str(root/"alembic.ini"));config.set_main_option("script_location",str(root/"alembic"))
        expected=set(ScriptDirectory.from_config(config).get_heads())
        async with db.begin_nested():
            current=set((await db.execute(text("SELECT version_num FROM public.alembic_version"))).scalars())
        result["postgres"]={"ok":current==expected,"migration_current":current==expected}
        if current!=expected:result["postgres"]["reason"]="数据库迁移版本与当前代码不一致"
    except Exception:result["postgres"]={"ok":False,"reason":"数据库迁移版本不可读"}
    checkpoint_engine=None
    try:
        from app.infra.database import create_database_engine
        from sqlalchemy.engine import make_url
        checkpoint_url=settings.checkpoint_database_url
        if not checkpoint_url:raise ValueError("CHECKPOINT_DATABASE_URL未配置")
        async_url=make_url(checkpoint_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        checkpoint_engine=create_database_engine(settings.model_copy(update={"database_url":async_url}))
        async with checkpoint_engine.connect() as connection:
            tables=(await connection.execute(text("SELECT to_regclass('checkpoints'),to_regclass('checkpoint_blobs'),to_regclass('checkpoint_writes')"))).one()
            result["checkpointer"]={"ok":all(tables)}
    except Exception:result["checkpointer"]={"ok":False,"reason":"检查点存储未就绪"}
    finally:
        if checkpoint_engine is not None:await checkpoint_engine.dispose()
    from app.prompts.registry import PromptRegistry
    registry=PromptRegistry.load()
    result["prompts"]={"ok":not registry.check.issues}
    try:
        from app.extensions.service import templates
        catalog=templates(settings)
        registry_ready=bool(catalog["items"]) and all(item.get("public_registry_ready") is True for item in catalog["items"])
        result["mcp_templates"]={"ok":registry_ready,"public_registry_ready":registry_ready,"credential_store_ready":all(item.get("credential_store_ready") for item in catalog["items"])}
        if not registry_ready:result["mcp_templates"]["reason"]="MCP公开工具注册配置无效，调用禁用"
    except Exception:result["mcp_templates"]={"ok":False,"reason":"MCP模板未就绪"}
    if settings.redis_url:
        from redis.asyncio import Redis
        client=Redis.from_url(settings.redis_url,socket_connect_timeout=3,socket_timeout=3)
        try:result["redis"]={"ok":bool(await client.ping())}
        except Exception:result["redis"]={"ok":False,"reason":"Redis不可读"}
        finally:await client.aclose()
    else:result["redis"]={"ok":False,"reason":"未配置"}
    from app.infra.object_store import ObjectStore
    store=None
    try:
        store=ObjectStore(settings);await asyncio.to_thread(store._internal.head_bucket,Bucket=store._bucket)
        result["object_store"]={"ok":True}
    except Exception:result["object_store"]={"ok":False,"reason":"对象存储不可读或未配置"}
    finally:
        if store is not None:
            for name in ("_internal","_public"):
                value=getattr(store,name,None)
                if value is not None:value.close()
    async with httpx.AsyncClient(timeout=5) as client:
        for role in ModelRole:
            try:profile=settings.model_profile(role)
            except ValueError:profile=None
            if profile is None:result[role.value]={"ok":False,"reason":"未配置"};continue
            try:
                response=await client.get(profile.base_url.rstrip('/')+'/v1/models',headers={"Authorization":"Bearer "+profile.api_key.get_secret_value()})
                result[role.value]={"ok":response.status_code==200,"status":response.status_code,"inference_probed":False}
            except Exception:result[role.value]={"ok":False,"reason":"模型只读端点不可达"}
        if settings.mineru_base_url and settings.mineru_api_key:
            from app.documents.parsing.mineru import MinerUAdapter
            adapter=None
            try:
                adapter=MinerUAdapter(settings,timeout_seconds=5,sleep=lambda _:None)
                status=await asyncio.to_thread(adapter.get_status,"logistics-health-nonexistent")
                result["ocr"]={"ok":status.state in {"not_found","unknown"},"paid_parse_probed":False}
            except Exception:result["ocr"]={"ok":False,"reason":"OCR只读查询不可达"}
            finally:
                if adapter:adapter.close()
        else:result["ocr"]={"ok":False,"reason":"未配置OCR"}
    return {"status":"ready" if all(component["ok"] for component in result.values()) else "degraded","components":result,"probed_at":datetime.now(UTC),"paid_requests":0}
