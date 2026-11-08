"""同步单证 Worker：领域事务先落账，图检查点仅保存资源 ID。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import ModelRole, get_settings
from app.documents import evidence_models as em
from app.documents.type_catalog import PARTY_ROLES
from app.documents.extraction import bounded_extraction_batches, field_schema, normalize_numeric, source_value_present
from app.documents.models import Case, CaseAccessGrant, CaseStatus, DocumentVersion, StoredObject, StoredObjectCategory
from app.documents.parsing.mineru import MinerUAdapter, MinerUError, MinerUOptions
from app.documents.parsing.selector import merge_outcomes, ocr_input, parse_local_routes, require_ready, select_parser, prepared_ocr_input, attach_preprocessing
from app.documents.preprocessing import PREPROCESSING_VERSION, DOCUMENT_GROUPING_VERSION
from app.infra.database import create_sync_engine, create_sync_session_factory, sync_tenant_transaction
from app.infra.model_gateway import CallBudget, Failed, NeedsReconcile, Succeeded, build_model_gateway, model_call_timeout
from app.infra.object_store import ObjectStore
from app.infra.task_queue import create_task_queue
from app.prompts.registry import PromptRegistry
from app.prompts.schemas import ExtractionResult
from app.workflows.contracts import ModelBinding
from app.workflows.registry import WorkflowVersionUnavailable, get_workflow
from app.workflows.models import ExecutionEpoch, QueueOutbox, Run, RunDocument, RunEvent, RunStatus, StepExecution, StepStatus
from app.consignment import models as consignment_models  # noqa: F401

queue = create_task_queue(get_settings())
queue.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
                  task_serializer="json", accept_content=["json"], broker_connection_retry_on_startup=True,
                  task_soft_time_limit=get_settings().workflow_task_soft_limit_seconds,
                  task_time_limit=get_settings().workflow_task_hard_limit_seconds)


class StaleExecution(RuntimeError):
    pass


class AwaitExternal(RuntimeError):
    pass


class YieldExecution(RuntimeError):
    """A known safe boundary; persisted continuation replaces the current message."""
    def __init__(self,reason="message_time_budget",*,delay_seconds=1):
        super().__init__(reason);self.reason=reason;self.delay_seconds=delay_seconds


class ModelFailure(RuntimeError):
    def __init__(self, kind: str, detail: str, *, unknown: bool = False):
        super().__init__(detail)
        self.kind, self.unknown = kind, unknown


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def append_event(db: Session, run: Run, event_type: str, message: str, payload=None) -> None:
    run.next_event_seq += 1
    db.add(RunEvent(tenant_id=run.tenant_id, run_id=run.id, seq=run.next_event_seq,
                   event_type=event_type, status=run.status.value, stage=run.current_stage, message=message, payload=payload))


class DocumentWorker:
    def __init__(self, run_id: UUID, epoch: int):
        self.settings = get_settings()
        self.engine = create_sync_engine(self.settings)
        self.factory = create_sync_session_factory(self.engine)
        self.run_id, self.epoch = run_id, epoch
        with self.engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE logistics_worker"))
            self.tenant = conn.execute(text("SELECT worker_tenant_of_run(:id)"), {"id": run_id}).scalar_one_or_none()
        if self.tenant is None:
            self.engine.dispose()
            raise StaleExecution("任务已删除")
        self.runner = asyncio.Runner()
        self.gateway = build_model_gateway(self.settings)
        self.registry = PromptRegistry.load()
        self.store = ObjectStore(self.settings)
        self.fence = None
        self.lease = uuid4()
        self.message_deadline=time.monotonic()+self.settings.workflow_task_soft_limit_seconds

    @contextmanager
    def transaction(self):
        with self.factory() as db, sync_tenant_transaction(db, self.tenant):
            db.execute(text("SET LOCAL ROLE logistics_worker"))
            yield db

    def locked(self, db: Session) -> Run:
        case_id = db.scalar(select(Run.case_id).where(Run.id == self.run_id))
        if case_id is None:
            raise StaleExecution("任务已删除")
        db.scalar(select(Case).where(Case.id == case_id).with_for_update())
        run = db.scalar(select(Run).where(Run.id == self.run_id).with_for_update())
        if run is None or run.current_epoch != self.epoch or run.status is RunStatus.CANCELLED:
            raise StaleExecution("任务已取消或执行批次过期")
        case = db.get(Case, run.case_id)
        if case and (self.settings.deletion_ledger_path or self.settings.deletion_ledger_key):
            from app.management.ledger import DeletionLedger,LedgerUnavailable
            try:denied=DeletionLedger(self.settings).is_denied(self.tenant,case.id)
            except (LedgerUnavailable,OSError):raise StaleExecution("删除账本不可校验，停止单票处理") from None
            if denied:raise StaleExecution("单票已撤权")
        if case is None or case.status is not CaseStatus.ACTIVE or case.retention_expires_at <= datetime.now(UTC):
            raise StaleExecution("单票不可访问")
        if self.fence is not None and run.execution_fence != self.fence:
            raise StaleExecution("执行栅栏已变更")
        return run

    def step(self, name: str, inputs, execute):
        fingerprint = digest({"task": self.spec_hash, "step": name, "inputs": inputs,
                              "prompts": self.prompt_versions, "models": self.bindings})
        with self.transaction() as db:
            run = self.locked(db)
            pending = list(db.scalars(select(StepExecution).where(StepExecution.run_id == run.id,
                StepExecution.status.in_([StepStatus.SUBMITTING, StepStatus.NEEDS_RECONCILE]))))
            child_prefix = "解析:嵌图:" + name.removeprefix("解析:")[:8] + ":" if name.startswith("解析:") and not name.startswith("解析:嵌图:") else None
            allowed = [item.id for item in pending if item.epoch == self.epoch and item.status == StepStatus.SUBMITTING
                and item.external_task_id and ((item.step_name == name and item.input_fingerprint == fingerprint)
                or child_prefix and item.step_name.startswith(child_prefix))]
            previous = db.scalar(select(StepExecution).where(StepExecution.run_id == run.id,
                StepExecution.step_name == name, StepExecution.input_fingerprint == fingerprint,
                StepExecution.status == StepStatus.SUCCEEDED))
            if previous:
                # 多文件续跑先回读已成功产物，再抵达正在等待结果的同批次 OCR。
                waiting_ocr = [item.id for item in pending if item.epoch == self.epoch
                    and item.status == StepStatus.SUBMITTING and item.external_task_id
                    and item.step_name.startswith("解析:") and item.error_class == "ocr_poll_pending"
                    and (item.usage or {}).get("ocr_upload_confirmed") is True]
                self.require_settled_steps(db, allowed_ids=waiting_ocr)
                return previous.artifact_ids
            self.require_settled_steps(db, allowed_ids=allowed)
            self.ensure_time(90)
            active = db.scalar(select(StepExecution).where(StepExecution.run_id == run.id,
                StepExecution.step_name == name, StepExecution.input_fingerprint == fingerprint,
                StepExecution.status.in_([StepStatus.RUNNING, StepStatus.SUBMITTING, StepStatus.NEEDS_RECONCILE]))
                .order_by(StepExecution.created_at.desc()).limit(1))
            if active:
                if active.lease_until and active.lease_until > datetime.now(UTC):
                    raise StaleExecution("步骤仍由其他 Worker 持有")
                resumable_ocr=(name.startswith("解析:") and active.external_task_id
                    and active.status==StepStatus.SUBMITTING and active.epoch==self.epoch)
                resumable_local_parse=(name.startswith("解析:") and not active.external_task_id
                    and active.epoch==self.epoch and active.status==StepStatus.RUNNING
                    and active.error_class=="safe_yield_waiting_child")
                if active.status in (StepStatus.SUBMITTING, StepStatus.NEEDS_RECONCILE) and not resumable_ocr:
                    raise ModelFailure("acceptance_unknown", "外部受理状态待核查", unknown=True)
                if resumable_ocr or resumable_local_parse:
                    step=active;step.lease_token=self.lease;step.lease_until=datetime.now(UTC)+timedelta(minutes=15)
                    if resumable_local_parse:step.error_class=None
                else:
                    active.status = StepStatus.FAILED
                    active.error_class = "lease_expired_before_submit"
                    step=None
            else:step=None
            if step is None:
                step = StepExecution(tenant_id=self.tenant, run_id=run.id, epoch=self.epoch, step_name=name,
                    input_fingerprint=fingerprint, status=StepStatus.RUNNING, lease_token=self.lease,
                    lease_until=datetime.now(UTC)+timedelta(minutes=15), provider_request_key=f"step:{uuid4()}",
                    usage={"preprocessing_policy":inputs["preprocessing_policy"]} if isinstance(inputs,dict) and inputs.get("preprocessing_policy") else None)
                db.add(step)
            db.flush()
            step_id = step.id
            run.current_stage = name.split(":", 1)[0]
            append_event(db, run, "stage", f"正在{run.current_stage}")
        try:
            ids = execute(step_id)
            with self.transaction() as db:
                run = self.locked(db)
                step = db.get(StepExecution, step_id)
                if step.status != StepStatus.SUCCEEDED:
                    self.complete_step(db, step_id, ids)
            return [str(x) for x in ids]
        except StaleExecution:
            raise
        except YieldExecution as exc:
            if not getattr(exc,"persisted",False):self.schedule_continuation(exc,step_id=step_id)
            raise
        except Exception as exc:
            unknown = bool(getattr(exc, "unknown", False)) or isinstance(exc, MinerUError) and exc.kind == "acceptance_unknown"
            with self.transaction() as db:
                run = self.locked(db)
                step = db.get(StepExecution, step_id)
                if step.status == StepStatus.SUCCEEDED:
                    return step.artifact_ids
                # Unexpected interruption after durable submission intent cannot
                # prove that an external service rejected the request.
                if step.status==StepStatus.SUBMITTING and not isinstance(exc,(ModelFailure,MinerUError)):
                    unknown=True
                step.status = StepStatus.NEEDS_RECONCILE if unknown else StepStatus.FAILED
                step.error_class = getattr(exc, "kind", None) or getattr(exc, "code", None) or type(exc).__name__
                step.error_detail = "外部步骤受理待核查" if unknown else "步骤执行失败，可查看运行核查记录"
                step.finished_at = datetime.now(UTC)
            raise

    def ensure_time(self,required_seconds):
        if required_seconds+45>=self.settings.workflow_task_soft_limit_seconds:
            raise ModelFailure("unavailable","单次外部调用总时限超过Worker消息预算，请调整部署时限")
        if self.message_deadline-time.monotonic()<required_seconds+45:
            raise YieldExecution()

    def schedule_continuation(self,reason,*,step_id=None):
        with self.transaction() as db:
            run=self.locked(db)
            waiting_for_ocr=False
            if step_id:
                step=db.get(StepExecution,step_id)
                if step.status!=StepStatus.SUCCEEDED:
                    if step.external_task_id and step.step_name.startswith("解析:"):
                        step.status=StepStatus.SUBMITTING;step.error_class="ocr_poll_pending"
                        waiting_for_ocr=reason.reason.startswith("ocr_")
                    elif step.status==StepStatus.RUNNING:
                        step.status=StepStatus.FAILED;step.error_class="safe_yield_before_submit"
                    else:raise ModelFailure("acceptance_unknown","受理状态未明，不能安全续跑",unknown=True)
                    step.lease_until=None
            # Preserve the local parent's identity while its known OCR child
            # waits; only this explicit safe boundary may reuse a running Step.
            for parent in db.scalars(select(StepExecution).where(StepExecution.run_id==run.id,StepExecution.epoch==self.epoch,
                StepExecution.status==StepStatus.RUNNING,StepExecution.lease_token==self.lease)):
                if waiting_for_ocr and parent.step_name.startswith("解析:") and not parent.external_task_id:
                    parent.error_class="safe_yield_waiting_child"
                else:
                    parent.status=StepStatus.FAILED;parent.error_class="safe_yield_before_submit"
                parent.lease_until=None
            run.execution_fence+=1;run.status=RunStatus.QUEUED
            run.current_stage="OCR处理等待结果" if reason.reason.startswith("ocr_") else "等待安全续跑"
            key=f"run-yield:{run.id}:{run.current_epoch}:{run.execution_fence}"
            db.add(QueueOutbox(tenant_id=self.tenant,topic="run",dedupe_key=key,payload={"run_id":str(run.id),"epoch":run.current_epoch},
                next_attempt_at=datetime.now(UTC)+timedelta(seconds=reason.delay_seconds)))
            append_event(db,run,"run_status","OCR任务已提交，稍后读取同一任务结果" if reason.reason.startswith("ocr_") else "已保存安全续跑点，已完成步骤将直接复用",{"reason":reason.reason})
            # Prevent the outer consumer from scheduling the same yield twice.
            reason.persisted=True

    def complete_step(self, db: Session, step_id: UUID, artifact_ids):
        """与产物同一事务提交成功记录，崩溃恢复只复用已封存的结果。"""
        run = self.locked(db)
        step = db.get(StepExecution, step_id)
        if step is None or step.lease_token != self.lease or step.epoch != self.epoch:
            raise StaleExecution("步骤租约已变更")
        self.require_settled_steps(db, allowed_ids=[step_id])
        step.status = StepStatus.SUCCEEDED
        step.artifact_ids = [str(value) for value in artifact_ids]
        step.finished_at = datetime.now(UTC)
        append_event(db, run, "stage", f"{step.step_name.split(':', 1)[0]}完成")

    def submit_intent(self, step_id: UUID, external_id: str | None = None):
        with self.transaction() as db:
            self.locked(db)
            self.require_settled_steps(db, allowed_ids=[step_id])
            step = db.get(StepExecution, step_id)
            step.status = StepStatus.SUBMITTING
            if external_id:
                step.external_task_id = external_id

    def record_model_response(self, step_id, result):
        """成功回执不是业务产物成功；落库失败仍按未知受理核查。"""
        from app.infra.model_gateway import successful_step_model_calls
        try:
            with self.transaction() as db:
                self.locked(db)
                step=db.scalar(select(StepExecution).where(StepExecution.id==step_id).with_for_update().execution_options(populate_existing=True))
                if step is None or step.run_id!=self.run_id or step.epoch!=self.epoch or step.lease_token!=self.lease:
                    raise StaleExecution("模型响应对应步骤租约已变化")
                known=successful_step_model_calls(step)
                returned={record.call_id for record in result.records if record.outcome=="succeeded"}
                if step.status!=StepStatus.SUBMITTING or not returned or not returned.issubset(set(known)):
                    raise ModelFailure("acceptance_unknown","模型响应账本无法确认，保留请求待核查",unknown=True)
                step.usage={**(step.usage or {}),"model_response":{"call_ids":sorted(returned),"received_at":datetime.now(UTC).isoformat()}}
                step.status=StepStatus.RUNNING
        except (StaleExecution,ModelFailure):raise
        except Exception as exc:
            raise ModelFailure("acceptance_unknown","模型响应凭据保存失败，保留请求待核查",unknown=True) from exc

    def require_settled_steps(self, db: Session, *, allowed_ids=()):
        """已知同批次 OCR 可续查；其他未决受理必须先核查，不能靠新批次绕过。"""
        pending = db.scalar(select(StepExecution.id).where(StepExecution.run_id == self.run_id,
            StepExecution.status.in_([StepStatus.SUBMITTING, StepStatus.NEEDS_RECONCILE]),
            StepExecution.id.not_in(allowed_ids)).limit(1))
        if pending:
            raise ModelFailure("acceptance_unknown", "本任务仍有外部请求待核查，不能继续推进", unknown=True)

    def model_budget(self, binding):
        from app.workflows.costs import ModelCostBudget
        with self.transaction() as db:
            self.locked(db)
            records = [record for usage in db.scalars(select(StepExecution.usage).where(StepExecution.run_id == self.run_id))
                       if usage for record in usage.get("model_calls", usage.get("calls", []))]
        return ModelCostBudget(settings=self.settings, records=records, binding=binding)

    def request_key(self, step_id):
        with self.transaction() as db:
            self.locked(db)
            return db.get(StepExecution, step_id).provider_request_key

    def load_spec(self, state):
        with self.transaction() as db:
            run = self.locked(db)
            self.fence = run.execution_fence
            if run.status not in (RunStatus.QUEUED, RunStatus.PROCESSING):
                raise StaleExecution("当前状态不能执行")
            known_ocr = list(db.scalars(select(StepExecution.id).where(StepExecution.run_id == run.id,
                StepExecution.epoch == self.epoch, StepExecution.status == StepStatus.SUBMITTING,
                StepExecution.step_name.startswith("解析:"), StepExecution.external_task_id.is_not(None))))
            self.require_settled_steps(db, allowed_ids=known_ocr)
            run.status = RunStatus.PROCESSING
            run.current_stage = "加载任务"
            epoch = db.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id, ExecutionEpoch.epoch == self.epoch))
            self.spec, self.spec_hash, self.prompt_versions = run.task_spec, run.task_spec_hash, run.prompt_versions
            self.bindings = epoch.model_bindings
            self.purpose = epoch.purpose
            self.input_parse_ids = epoch.input_parse_artifact_ids
            if self.purpose == "retry" and not self.input_parse_ids:
                prior_epochs = db.scalars(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id,
                    ExecutionEpoch.epoch < self.epoch).order_by(ExecutionEpoch.epoch.desc()))
                self.input_parse_ids = next((list(prior.input_parse_artifact_ids) for prior in prior_epochs
                    if prior.input_parse_artifact_ids), [])
            self.input_business_id = epoch.input_business_revision_id
            self.case_id = run.case_id
            self.documents = [(d.document_id, d.document_version) for d in db.scalars(select(RunDocument)
                              .where(RunDocument.run_id == run.id).order_by(RunDocument.ordinal))]
            append_event(db, run, "run_status", "已开始单证处理")
        return {"task_spec_hash": self.spec_hash}

    def inspect_sources(self, state):
        sources=[]
        with self.transaction() as db:
            self.locked(db)
            for doc_id, version in self.documents:
                dv = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc_id, DocumentVersion.version == version))
                if dv is None or dv.case_id != self.case_id:
                    raise StaleExecution("来源版本失效")
                obj=db.get(StoredObject,dv.object_id)
                sources.append((dv.mime,dv.sha256,dv.size_bytes,obj.object_key))
        total_pages=total_bytes=0
        for mime,sha,size,key in sources:
            raw=self.store.read_all(key)
            if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=sha:
                raise ModelFailure("source_hash_mismatch","原件字节与封存哈希不一致")
            total_bytes+=len(raw)
            plan=select_parser(mime,self.settings,data=raw)
            total_pages+=plan.page_count or 0
            if total_pages>self.settings.run_max_total_pages or total_bytes>self.settings.run_max_total_bytes:
                raise ModelFailure("source_limit_exceeded","全部原件实际页数或总字节超过Run处理上限，未提交OCR")
        return {}

    def put_object(self, db: Session, data: bytes, mime: str):
        key = self.store.build_key(self.tenant, self.case_id, StoredObjectCategory.PARSE)
        self.store._internal.put_object(Bucket=self.store._bucket, Key=key, Body=data, ContentType=mime)
        obj = StoredObject(tenant_id=self.tenant, case_id=self.case_id, object_key=key,
            category=StoredObjectCategory.PARSE, mime=mime, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), is_visible=True)
        db.add(obj)
        db.flush()
        return obj.id

    def ocr_result(self,step_id,data,filename,*,page_map=(1,),page_rotations=None,source_page_count=None):
        """One known vendor task per persistent Step; read-only resume never resubmits."""
        with self.transaction() as db:
            run = self.locked(db)
            step=db.get(StepExecution,step_id)
            task_id=step.external_task_id
            stored_options=(step.usage or {}).get('ocr_options')
            frozen_binding = run.task_spec.get('ocr_binding')
            if frozen_binding and frozen_binding.get('base_url') != (self.settings.mineru_base_url or '').rstrip('/'):
                raise ModelFailure('binding_mismatch','OCR端点与冻结任务不一致，不能自动切换供应商')
            options=(MinerUOptions(**stored_options) if stored_options else
                     MinerUOptions(model_version='pipeline',language='auto') if task_id else
                     MinerUOptions(**frozen_binding['options']) if frozen_binding else
                     MinerUOptions.from_settings(self.settings))
            if not stored_options and not task_id:
                step.usage={**(step.usage or {}),'ocr_options':asdict(options)}
            if 'ocr_cost' not in (step.usage or {}):
                from app.workflows.costs import estimate_ocr_request
                # 只冻结本次原件选路需要的页；旧任务不以当前环境报价回填。
                binding = frozen_binding if not task_id else None
                estimate = estimate_ocr_request(binding, pages=len(page_map) if page_map else None)
                estimate['source_page_count'] = source_page_count
                step.usage={**(step.usage or {}),'ocr_cost':estimate}
            allocated=db.scalar(select(func.count(StepExecution.id)).where(StepExecution.run_id==self.run_id,StepExecution.external_task_id.is_not(None)))
        adapter=MinerUAdapter(self.settings,options=options,timeout_seconds=20,upload_timeout_seconds=120)
        try:
            if not task_id:
                if allocated>=self.settings.run_max_ocr_tasks:raise ModelFailure('budget_exhausted','本Run已达到OCR任务总上限')
                self.ensure_time(480);self.submit_intent(step_id)
                task_id=adapter.submit(data,filename,on_task_id=lambda value:self.submit_intent(step_id,value))
                with self.transaction() as db:
                    self.locked(db);step=db.get(StepExecution,step_id);step.usage={**(step.usage or {}),'ocr_upload_confirmed':True}
            self.ensure_time(180)
            with self.transaction() as db:
                self.locked(db);step=db.get(StepExecution,step_id);usage=step.usage or {}
                if usage.get('ocr_poll_count',0)>=self.settings.run_max_ocr_polls:
                    raise ModelFailure('acceptance_unknown','OCR续查次数已达上限，保留原任务等待运营核查',unknown=True)
                step.usage={**usage,'ocr_poll_count':usage.get('ocr_poll_count',0)+1}
            status=adapter.get_status(task_id)
            if status.state=='failed':raise ModelFailure('permanent','OCR任务失败，结果未产生')
            if status.state in {'not_found','unknown'}:raise ModelFailure('acceptance_unknown','供应商任务状态不可确认',unknown=True)
            with self.transaction() as db:
                self.locked(db);confirmed=(db.get(StepExecution,step_id).usage or {}).get('ocr_upload_confirmed')
            if status.state=='waiting-file' and not confirmed:raise ModelFailure('acceptance_unknown','原件上传完成状态未知，不能重传',unknown=True)
            if status.state!='done':raise YieldExecution('ocr_poll_pending',delay_seconds=30)
            self.ensure_time(420)
            return adapter.normalize(adapter.fetch_result(status),page_map=page_map,page_rotations=page_rotations)
        except MinerUError as exc:
            if task_id and exc.kind in {'not_sent','rejected'}:raise YieldExecution('ocr_read_retry',delay_seconds=min(300,max(30,exc.retry_after or 30))) from None
            if task_id and exc.kind in {'protocol','auth'}:raise ModelFailure('acceptance_unknown','已受理OCR结果无法读取，需核查',unknown=True) from None
            raise
        finally:adapter.close()

    def enrich_docx_images(self,local,doc_id,version,*,preprocessing_policy=None):
        from app.documents.parsing.docx_ocr import bounded_embedded_images,attach_embedded_ocr
        from app.documents.parsing.dto import ParseOutcome
        images=bounded_embedded_images(local,self.settings)
        if not images:return local
        if not self.settings.parser_ready():raise ModelFailure('unavailable','DOCX嵌图需要已配置的OCR服务')
        results=[]
        for image in images:
            def recognize(step_id):
                if preprocessing_policy:
                    from app.documents.preprocessing import prepare_image
                    from app.documents.parsing.dto import QualityFlag
                    with self.transaction() as db:
                        self.locked(db);step=db.get(StepExecution,step_id);stored=(step.usage or {}).get("recognition_input")
                    if not stored:
                        prepared=prepare_image(image.data,mime=image.content_type)
                        with self.transaction() as db:
                            self.locked(db);step=db.get(StepExecution,step_id)
                            if step.external_task_id:raise ModelFailure("acceptance_unknown","已受理嵌图缺少派生输入记录，不能重投",unknown=True)
                            stored={"object_id":str(self.put_object(db,prepared.data,prepared.mime)),
                                "manifest_object_id":str(self.put_object(db,json.dumps(prepared.manifest,ensure_ascii=False).encode(),"application/json")),
                                "manifest":prepared.manifest,"quality_flags":[flag.model_dump() for flag in prepared.quality_flags]}
                            step.usage={**(step.usage or {}),"recognition_input":stored}
                    with self.transaction() as db:
                        self.locked(db);obj=db.get(StoredObject,UUID(stored["object_id"]));recognition=self.store.read_all(obj.object_key)
                        if obj.case_id!=self.case_id or len(recognition)!=obj.size_bytes or hashlib.sha256(recognition).hexdigest()!=obj.sha256:
                            raise ModelFailure("source_hash_mismatch","嵌图派生输入哈希或归属不符")
                    result=self.ocr_result(step_id,recognition,"embedded.png",page_rotations={1:0},source_page_count=1)
                    result=attach_preprocessing(result,stored["manifest"],[QualityFlag.model_validate(flag) for flag in stored["quality_flags"]])
                else:
                    result=self.ocr_result(step_id,image.data,'embedded.png' if image.content_type=='image/png' else 'embedded.jpg',source_page_count=1)
                if not result.segments:raise ModelFailure('empty_parse','DOCX嵌图未取得可读文字')
                with self.transaction() as db:
                    self.locked(db);object_id=self.put_object(db,result.model_dump_json().encode(),'application/json')
                    self.complete_step(db,step_id,[object_id]);return [object_id]
            parent=image.anchor.model_dump()
            if not preprocessing_policy:parent.pop('preprocessing_transform',None)
            inputs={'document_id':str(doc_id),'version':version,'image_key':image.key,'sha256':image.sha256,'parent':parent,'parser':'docx-embedded-ocr@2' if preprocessing_policy else 'docx-embedded-ocr@1'}
            if preprocessing_policy:inputs['preprocessing_policy']=preprocessing_policy
            ids=self.step('解析:嵌图:'+str(doc_id)[:8]+':'+image.key,inputs,recognize)
            with self.transaction() as db:
                self.locked(db);obj=db.get(StoredObject,UUID(ids[0]));raw=self.store.read_all(obj.object_key)
                if len(raw)!=obj.size_bytes or hashlib.sha256(raw).hexdigest()!=obj.sha256:raise ModelFailure('source_hash_mismatch','嵌图OCR结果封存哈希不符')
            results.append((image,ParseOutcome.model_validate_json(raw)))
        return attach_embedded_ocr(local,results)

    def parse_documents(self, state):
        artifact_ids=[]
        for doc_id, version in self.documents:
            with self.transaction() as db:
                preserved = db.scalar(select(em.ParseArtifact).where(em.ParseArtifact.id.in_([UUID(x) for x in self.input_parse_ids]),
                    em.ParseArtifact.document_id == doc_id, em.ParseArtifact.document_version == version)) if self.input_parse_ids else None
                if preserved is None and self.purpose == "retry":
                    succeeded = db.scalars(select(StepExecution).where(StepExecution.run_id == self.run_id,
                        StepExecution.step_name == f"解析:{doc_id}", StepExecution.status == StepStatus.SUCCEEDED)
                        .order_by(StepExecution.created_at.desc()))
                    for prior in succeeded:
                        preserved = db.scalar(select(em.ParseArtifact).where(em.ParseArtifact.id.in_(prior.artifact_ids),
                            em.ParseArtifact.case_id == self.case_id, em.ParseArtifact.document_id == doc_id,
                            em.ParseArtifact.document_version == version))
                        if preserved is not None: break
                old_step=db.scalar(select(StepExecution).where(StepExecution.run_id==self.run_id,StepExecution.epoch==self.epoch,
                    StepExecution.step_name==f"解析:{doc_id}").order_by(StepExecution.created_at.desc()).limit(1))
                # 旧已受理步骤保持原指纹/原输入，绝不因上线新预处理重新提交。
                policy=(old_step.usage or {}).get("preprocessing_policy") if old_step else PREPROCESSING_VERSION
            if preserved:
                artifact_ids.append(str(preserved.id))
                continue
            def parse(step_id):
                with self.transaction() as db:
                    self.locked(db)
                    if policy:
                        step=db.get(StepExecution,step_id)
                        step.usage={**(step.usage or {}),"preprocessing_policy":policy,"document_grouping_version":DOCUMENT_GROUPING_VERSION}
                    dv=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==doc_id, DocumentVersion.version==version))
                    obj=db.get(StoredObject,dv.object_id)
                    raw=self.store.read_all(obj.object_key)
                    mime=dv.mime
                    if hashlib.sha256(raw).hexdigest() != dv.sha256 or len(raw) != dv.size_bytes:
                        raise ModelFailure("source_hash_mismatch", "原件字节与封存哈希不一致")
                plan=select_parser(mime,self.settings,data=raw)
                require_ready(plan)
                local=parse_local_routes(plan,raw)
                parts=[local] if local else []
                if local and local.embedded_images:
                    local=self.enrich_docx_images(local,doc_id,version,preprocessing_policy=policy)
                    parts=[local]
                if plan.needs_external_ocr:
                    with self.transaction() as db:
                        self.locked(db);step=db.get(StepExecution,step_id);stored=(step.usage or {}).get("recognition_input")
                    if policy and not stored:
                        prepared=prepared_ocr_input(plan,raw)
                        with self.transaction() as db:
                            self.locked(db);step=db.get(StepExecution,step_id)
                            if step.external_task_id:
                                raise ModelFailure("acceptance_unknown","已受理OCR缺少派生输入记录，不能重新制作外发输入",unknown=True)
                            derived_id=self.put_object(db,prepared.data,prepared.mime)
                            manifest_id=self.put_object(db,json.dumps(prepared.manifest,ensure_ascii=False).encode(),"application/json")
                            stored={"object_id":str(derived_id),"manifest_object_id":str(manifest_id),"mime":prepared.mime,
                                "manifest":prepared.manifest,"quality_flags":[flag.model_dump() for flag in prepared.quality_flags]}
                            step.usage={**(step.usage or {}),"preprocessing_policy":policy,"recognition_input":stored}
                    if stored:
                        with self.transaction() as db:
                            self.locked(db);obj=db.get(StoredObject,UUID(stored["object_id"]))
                            if not obj or obj.case_id!=self.case_id:raise ModelFailure("source_hash_mismatch","派生识别图归属不符")
                            ocr_bytes=self.store.read_all(obj.object_key)
                            if len(ocr_bytes)!=obj.size_bytes or hashlib.sha256(ocr_bytes).hexdigest()!=obj.sha256:
                                raise ModelFailure("source_hash_mismatch","派生识别图封存哈希不符")
                        file_name="source.png" if stored["mime"]=="image/png" else "source.pdf"
                        page_map=plan.ocr_pages;rotations={page:0 for page in page_map}
                    else:
                        from dataclasses import replace
                        ocr_bytes,file_name,page_map=ocr_input(replace(plan,preprocessing_version=None),raw)
                        rotations=plan.page_rotations
                    recognized=self.ocr_result(step_id,ocr_bytes,file_name,page_map=page_map,page_rotations=rotations,source_page_count=plan.page_count)
                    if stored:
                        from app.documents.parsing.dto import QualityFlag
                        recognized=attach_preprocessing(recognized,stored["manifest"],[QualityFlag.model_validate(flag) for flag in stored["quality_flags"]])
                    parts.append(recognized)
                outcome=merge_outcomes(parts,plan=plan)
                if not outcome.segments:
                    raise ModelFailure("empty_parse","原件未取得可核验文本")
                with self.transaction() as db:
                    self.locked(db)
                    pa=em.ParseArtifact(tenant_id=self.tenant,case_id=self.case_id,document_id=doc_id,document_version=version,
                        parser_name=outcome.parser[:40],parser_version=outcome.parser_version[:40],parser_config_hash=outcome.config_fingerprint,
                        page_count=outcome.page_count,quality=em.ParseQuality.DEGRADED if outcome.quality_flags else em.ParseQuality.VALID,
                        quality_flags=[x.code for x in outcome.quality_flags],external_task_id=outcome.provider_task_id,is_synthetic=outcome.is_fake)
                    db.add(pa);db.flush()
                    md=outcome.markdown or "\n\n".join(s.text for s in outcome.segments)
                    pa.markdown_object_id=self.put_object(db,md.encode(),"text/markdown")
                    if outcome.raw_payload:
                        pa.raw_response_object_id=self.put_object(db,outcome.raw_payload,"application/zip")
                    from app.documents.preprocessing import split_document_segments
                    segment_ids = {}
                    logical_profiles = {}
                    for index, group in enumerate(split_document_segments(outcome.segments)):
                        pages = [segment.anchor.page_no for segment in group["segments"] if segment.anchor.page_no]
                        ld = em.LogicalDocument(tenant_id=self.tenant,case_id=self.case_id,parse_artifact_id=pa.id,ordinal=index,
                            type_candidate=em.LogicalDocumentType(group["type_candidate"]),type_confidence="candidate",
                            page_start=min(pages) if pages else None,page_end=max(pages) if pages else None,
                            title=group["title"])
                        db.add(ld);db.flush()
                        from app.documents.type_catalog import cargo_candidates, CATALOG_VERSION
                        logical_profiles[str(ld.id)]={"document_type":group["document_type"], "catalog_version":CATALOG_VERSION, "cargo_candidates":[]}
                        logical_source_ids=[]
                        for section_no, part in enumerate(group["sections"]):
                            section_pages = [segment.anchor.page_no for segment in part["segments"] if segment.anchor.page_no]
                            section = em.DocumentSection(tenant_id=self.tenant,case_id=self.case_id,parse_artifact_id=pa.id,
                                logical_document_id=ld.id,ordinal=section_no,kind="document",title=part["title"],
                                page_start=min(section_pages) if section_pages else None,page_end=max(section_pages) if section_pages else None,
                                detected_language=part["language"].language,language_uncertain=part["language"].uncertain)
                            db.add(section);db.flush()
                            for ordinal, segment in enumerate(part["segments"]):
                                ss=em.SourceSegment(tenant_id=self.tenant,case_id=self.case_id,parse_artifact_id=pa.id,
                                    logical_document_id=ld.id,section_id=section.id,ordinal=ordinal,structure=segment.kind,text=segment.text,
                                    table_row=segment.table_cell.row_index if segment.table_cell else None,
                                    table_col=segment.table_cell.col_index if segment.table_cell else None)
                                db.add(ss);db.flush();anchor=segment.anchor
                                segment_ids[segment.key] = str(ss.id)
                                logical_source_ids.append({"id":str(ss.id),"text":ss.text})
                                db.add(em.SourceAnchor(tenant_id=self.tenant,case_id=self.case_id,parse_artifact_id=pa.id,source_segment_id=ss.id,
                                    precision=em.AnchorPrecision(anchor.precision),page_no=anchor.page_no,
                                    bbox=[anchor.rect.x0,anchor.rect.y0,anchor.rect.x1,anchor.rect.y1] if anchor.rect else None,
                                    coord_system=anchor.coordinate_space,transform={"rotation":anchor.page_rotation,
                                        "docx":anchor.docx.model_dump() if anchor.docx else None,
                                        "table_cell":anchor.table_cell.model_dump() if anchor.table_cell else None,
                                        "embedded_image":anchor.embedded_image,"preprocessing":anchor.preprocessing_transform},render_version=anchor.locator_version))
                        logical_profiles[str(ld.id)]["cargo_candidates"]=cargo_candidates(logical_source_ids)
                    from app.review.layout import manifest_from_outcome
                    pa.layout_manifest = {**manifest_from_outcome(outcome, segment_ids),"logical_document_profiles":logical_profiles,
                        "document_grouping_version":DOCUMENT_GROUPING_VERSION}
                    self.complete_step(db, step_id, [pa.id])
                    return [pa.id]
            inputs={"document_id":str(doc_id),"version":version,"reparse_epoch":self.epoch if self.purpose == "reparse" else None}
            if not old_step or (old_step.usage or {}).get("document_grouping_version"):
                inputs["document_grouping_version"]=(old_step.usage or {}).get("document_grouping_version",DOCUMENT_GROUPING_VERSION) if old_step else DOCUMENT_GROUPING_VERSION
            if policy:inputs["preprocessing_policy"]=policy
            artifact_ids.extend(self.step(f"解析:{doc_id}",inputs,parse))
        self.parse_ids=[UUID(x) for x in artifact_ids]
        return {"artifact_ids":artifact_ids}

    @staticmethod
    def detect_type(value):
        from app.documents.type_catalog import detect_document_type, base_type
        return em.LogicalDocumentType(base_type(detect_document_type(value)))

    def classify_sections(self,state):
        self.parse_ids=[UUID(x) for x in state["artifact_ids"]]
        from app.workflows.translation import TranslationNodes
        from app.documents.type_catalog import document_type_choices
        with self.transaction() as db:
            run=self.locked(db)
            logicals=list(db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id.in_(self.parse_ids)).order_by(em.LogicalDocument.ordinal)))
            for logical in logicals:
                declared = self.type_input(db, logical)
                mixed_input = declared and not self.single_type_input(db, logical) and not self.boundary_answer(db, logical)
                if not mixed_input and self.confirmed_type(db,logical)!=em.LogicalDocumentType.UNKNOWN.value:continue
                sections=list(db.scalars(select(em.DocumentSection).where(em.DocumentSection.logical_document_id==logical.id)))
                section_ids=[str(section.id) for section in sections]
                TranslationNodes(self)._input(db,run,db.get(em.ParseArtifact,logical.parse_artifact_id),'document_boundary',
                    '该文件包含多个逻辑单据，请分别确认当前部分的类型与货物领域' if mixed_input else '该原件单据类型或边界无法可靠确认，请确认单据用途后继续识别',
                    document_type_choices(section_ids),
                    [{'section_id':value} for value in section_ids])
                return {'branch_decision':'awaiting_input'}
        return {'branch_decision':'continue'}

    def type_input(self, db, logical):
        run = db.get(Run, self.run_id)
        parse = db.get(em.ParseArtifact, logical.parse_artifact_id)
        if not parse:return None
        return next((value for value in (run.task_spec if run else {}).get("document_type_inputs", [])
            if value["document_id"] == str(parse.document_id) and value["document_version"] == parse.document_version
            and (value.get("document_type") or value.get("cargo_profile_ids"))), None)

    def single_type_input(self, db, logical):
        value = self.type_input(db, logical)
        if value and db.scalar(select(func.count(em.LogicalDocument.id)).where(em.LogicalDocument.parse_artifact_id == logical.parse_artifact_id)) == 1:
            return value
        return None

    def boundary_answer(self, db, logical):
        from app.workflows.models import InputRequest, InputRequestStatus
        sections = {str(value) for value in db.scalars(select(em.DocumentSection.id).where(em.DocumentSection.logical_document_id == logical.id))}
        requests = db.scalars(select(InputRequest).where(InputRequest.run_id == self.run_id,
            InputRequest.expected_parse_artifact_id == logical.parse_artifact_id, InputRequest.kind == "document_boundary",
            InputRequest.status == InputRequestStatus.RESOLVED).order_by(InputRequest.resolved_at.desc()))
        return next((request for request in requests if any(set(option.get("section_ids", [])) & sections
            for option in (request.answer or {}).get("selected_options", []))), None)

    def confirmed_type(self,db,logical):
        from app.workflows.models import InputRequest,InputRequestStatus
        sections=list(db.scalars(select(em.DocumentSection.id).where(em.DocumentSection.logical_document_id==logical.id)))
        run=db.get(Run,self.run_id)
        revision_id=run.current_business_revision_id if run else getattr(self,"input_business_id",None)
        declared = self.single_type_input(db, logical)
        decision=db.scalar(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.section_id.in_(sections),
            em.SectionAssignmentDecision.business_revision_id==revision_id,em.SectionAssignmentDecision.logical_document_type.is_not(None)).order_by(em.SectionAssignmentDecision.created_at.desc(),em.SectionAssignmentDecision.id.desc()).limit(1)) if revision_id else None
        if declared and declared.get("document_type") and (not decision or decision.created_at < run.created_at):
            return declared["document_type"]
        if decision:return decision.logical_document_type
        requests=list(db.scalars(select(InputRequest).where(InputRequest.run_id==self.run_id,InputRequest.expected_parse_artifact_id==logical.parse_artifact_id,InputRequest.status==InputRequestStatus.RESOLVED,InputRequest.kind=='document_boundary').order_by(InputRequest.resolved_at.desc())))
        for request in requests:
            for option in (request.answer or {}).get('selected_options',[]):
                if option.get('logical_document_type') and set(option.get('section_ids',[]))&{str(value) for value in sections}:return option['logical_document_type']
        if declared and declared.get("document_type"):return declared["document_type"]
        from app.documents.type_catalog import candidate_document_type
        return candidate_document_type(logical,db.get(em.ParseArtifact,logical.parse_artifact_id))

    def confirmed_cargo_profiles(self,db,logical):
        from app.documents.type_catalog import candidate_profile
        from app.workflows.models import InputRequest,InputRequestStatus
        sections=list(db.scalars(select(em.DocumentSection.id).where(em.DocumentSection.logical_document_id==logical.id)))
        run=db.get(Run,self.run_id)
        revision_id=run.current_business_revision_id if run else getattr(self,"input_business_id",None)
        declared = self.single_type_input(db, logical)
        decision=db.scalar(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.section_id.in_(sections),
            em.SectionAssignmentDecision.business_revision_id==revision_id,em.SectionAssignmentDecision.logical_document_type.is_not(None)).order_by(em.SectionAssignmentDecision.created_at.desc(),em.SectionAssignmentDecision.id.desc()).limit(1)) if revision_id else None
        if declared and (not decision or decision.created_at < run.created_at):return declared.get("cargo_profile_ids", [])
        if decision:return decision.cargo_profile_ids or []
        requests=list(db.scalars(select(InputRequest).where(InputRequest.run_id==self.run_id,InputRequest.expected_parse_artifact_id==logical.parse_artifact_id,
            InputRequest.kind=="document_boundary",InputRequest.status==InputRequestStatus.RESOLVED).order_by(InputRequest.resolved_at.desc())))
        for request in requests:
            if any(set(option.get("section_ids",[])) & {str(s) for s in sections} for option in (request.answer or {}).get("selected_options",[])):
                return (request.answer or {}).get("cargo_profile_ids",[])
        if declared:return declared.get("cargo_profile_ids", [])
        return [p["code"] for p in candidate_profile(logical,db.get(em.ParseArtifact,logical.parse_artifact_id)).get("cargo_candidates",[])]

    def run_awaiting_input(self,state):
        with self.transaction() as db:return self.locked(db).status==RunStatus.AWAITING_INPUT

    def extract_fields(self,state):
        from app.documents.extraction import annotate_extraction_structure, build_line_values, line_evidence_fingerprint, PARTY_FIELD_KEYS, resolve_party_role
        with self.transaction() as db:
            logical_ids=list(db.scalars(select(em.LogicalDocument.id).where(em.LogicalDocument.parse_artifact_id.in_(self.parse_ids))))
        type_logicals=set()
        if self.purpose in ("initial", "resume_input"):
            with self.transaction() as db:
                type_logicals = {value.id for value in db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.id.in_(logical_ids)))
                    if self.single_type_input(db, value)}
        if self.purpose=='recompute' and self.input_business_id:
            with self.transaction() as db:
                revision=db.get(em.BusinessRevision,self.input_business_id)
                if revision and revision.reason=='section_assignment':
                    current=list(db.scalars(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.business_revision_id==revision.id,
                        em.SectionAssignmentDecision.logical_document_type.is_not(None))))
                    previous=list(db.scalars(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.business_revision_id==revision.parent_revision_id,
                        em.SectionAssignmentDecision.logical_document_type.is_not(None)))) if revision.parent_revision_id else []
                    prior={(decision.section_id,decision.logical_document_type,tuple(decision.cargo_profile_ids or [])) for decision in previous}
                    changed={decision.section_id for decision in current if (decision.section_id,decision.logical_document_type,tuple(decision.cargo_profile_ids or [])) not in prior}
                    type_logicals=set(db.scalars(select(em.DocumentSection.logical_document_id).where(em.DocumentSection.id.in_(changed),em.DocumentSection.parse_artifact_id.in_(self.parse_ids))))
        if self.purpose=='resume_input':
            from app.workflows.models import InputRequest,InputRequestStatus
            with self.transaction() as db:
                requests=db.scalars(select(InputRequest).where(InputRequest.run_id==self.run_id,InputRequest.status==InputRequestStatus.RESOLVED,InputRequest.kind=='document_boundary'))
                section_ids={UUID(value) for request in requests for option in (request.answer or {}).get('selected_options',[]) for value in option.get('section_ids',[]) if option.get('logical_document_type')}
                type_logicals.update(db.scalars(select(em.DocumentSection.logical_document_id).where(em.DocumentSection.id.in_(section_ids),em.DocumentSection.parse_artifact_id.in_(self.parse_ids))))
        if self.purpose in ("recompute", "resume_input") and self.input_business_id and not type_logicals:
            with self.transaction() as db:
                self.assertion_ids = [str(x) for x in db.scalars(select(em.BusinessRevisionField.field_assertion_id).where(
                    em.BusinessRevisionField.business_revision_id == self.input_business_id))]
            return {}
        assertions=[]
        preserved_logicals=set()
        preserved_fields=set()
        preserved_line_logicals=set()
        if type_logicals and self.input_business_id:
            with self.transaction() as db:
                # 类型/货类不改变原件明细结构；复用已有解析行，不让模型重新分组追加旧行。
                # 修改源明细须重识别产生新Parse，旧行及其人工匹配/历史引用不覆盖。
                preserved_line_logicals=set(db.scalars(select(em.LineItem.logical_document_id).where(
                    em.LineItem.logical_document_id.in_(type_logicals),em.LineItem.parse_artifact_id.in_(self.parse_ids)).distinct()))
                inherited=list(db.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField,em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).where(em.BusinessRevisionField.business_revision_id==self.input_business_id)))
                for field in inherited:
                    logical=db.get(em.LogicalDocument,field.logical_document_id)
                    allowed={(item['field_key'],item['role']) for item in field_schema(self.confirmed_type(db,logical),self.confirmed_cargo_profiles(db,logical))['fields']} if logical else set()
                    if field.logical_document_id not in type_logicals or (field.field_key,field.role) in allowed and field.is_human:
                        assertions.append(str(field.id));preserved_fields.add((field.logical_document_id,field.field_key,field.role))
                preserved_logicals={value for value in logical_ids if value not in type_logicals}
        if self.purpose == "initial" and self.input_business_id and self.input_parse_ids and not type_logicals:
            with self.transaction() as db:
                inherited=list(db.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField,
                    em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).where(
                    em.BusinessRevisionField.business_revision_id==self.input_business_id,
                    em.FieldAssertion.parse_artifact_id.in_([UUID(value) for value in self.input_parse_ids]))))
                assertions.extend(str(field.id) for field in inherited)
                preserved_logicals={field.logical_document_id for field in inherited}
        for logical_id in logical_ids:
            if logical_id in preserved_logicals:
                continue
            with self.transaction() as db:
                ld = db.get(em.LogicalDocument, logical_id)
                ld_type = self.confirmed_type(db,ld)
                source_rows = list(db.scalars(select(em.SourceSegment).join(em.DocumentSection,
                    em.DocumentSection.id == em.SourceSegment.section_id).where(
                    em.SourceSegment.logical_document_id == logical_id).order_by(em.DocumentSection.ordinal, em.SourceSegment.ordinal)))
                anchors = {anchor.source_segment_id: anchor for anchor in db.scalars(select(em.SourceAnchor).where(
                        em.SourceAnchor.source_segment_id.in_([row.id for row in source_rows])))}
                material = []
                for source in source_rows:
                    anchor = anchors.get(source.id)
                    cell = ((anchor.transform or {}).get("table_cell") or {}) if anchor else {}
                    material.append({"id": str(source.id), "text": source.text, "structure": source.structure,
                        "section_id": str(source.section_id), "table_row": source.table_row, "table_col": source.table_col,
                        "page_no": anchor.page_no if anchor else None, "table_key": cell.get("table_key")})
                material = annotate_extraction_structure(material)
                schema = field_schema(ld_type,self.confirmed_cargo_profiles(db,ld))
                allowed_fields = {(row["field_key"], row["role"]) for row in schema["fields"]}
            from app.documents.extraction import compact_extraction_input, structured_table_lines, merge_table_candidates
            from app.infra.model_gateway import strict_json_schema
            from app.prompts.registry import estimate_tokens, PromptBudgetExceeded
            import json
            material = compact_extraction_input(material)
            table_lines = structured_table_lines(material)
            line_source_map = {source["id"]: source["text"] for source in material}
            # 模型已收到原表头，无需重复发送所有五语列名别名及校验规则清单。
            prompt_schema = {key: schema[key] for key in ("fields", "line_columns")}
            binding = next((ModelBinding.model_validate(b) for b in self.bindings if b["role"] == "extract"), None)
            if binding is None:
                raise ModelFailure("unavailable", "抽取模型未冻结")
            spec = self.prompt_versions["document.extract"]
            empty = self.registry.get_frozen("document.extract", spec["version"], spec["sha256"]).render(
                document_type=ld_type, field_schema=prompt_schema, segments=[])
            overhead = empty.estimated_input_tokens + estimate_tokens(json.dumps(strict_json_schema(ExtractionResult), ensure_ascii=False)) + 256
            available = binding.context_window_tokens - min(binding.max_output_tokens, empty.max_output_tokens) - overhead
            if available < 500:
                raise PromptBudgetExceeded("冻结模型上下文不足以容纳字段定义和输出结构，请调整已验证的上下文或拆分单据类型")
            for batch_index, material_batch in bounded_extraction_batches(material, max_input_tokens=min(6500, int(available * .8))):
                def extract(step_id):
                    spec=self.prompt_versions["document.extract"]
                    prompt=self.registry.get_frozen("document.extract",spec["version"],spec["sha256"]).render(
                        document_type=ld_type,field_schema=prompt_schema,segments=material_batch)
                    binding=next((ModelBinding.model_validate(b) for b in self.bindings if b["role"]=="extract"),None)
                    if binding is None:raise ModelFailure("unavailable","抽取模型未冻结")
                    self.ensure_time(model_call_timeout(self.settings, binding))
                    self.submit_intent(step_id)
                    from app.infra.model_gateway import StepExecutionCallLedger
                    from app.workflows.costs import PricedLedger
                    budget = self.model_budget(binding)
                    gateway = build_model_gateway(self.settings, ledger=PricedLedger(StepExecutionCallLedger(
                        self.factory, tenant_id=self.tenant, step_execution_id=step_id,
                        lease_token=self.lease, execution_fence=self.fence), binding, budget))
                    try:
                        result=self.runner.run(gateway.structured_generate(binding,[prompt],ExtractionResult,
                            self.request_key(step_id), budget=budget))
                        if isinstance(result,Succeeded):self.record_model_response(step_id,result)
                    finally:
                        self.runner.run(gateway.aclose())
                    if isinstance(result,NeedsReconcile):raise ModelFailure("acceptance_unknown","模型受理待核查",unknown=True)
                    if isinstance(result,Failed):raise ModelFailure(result.kind,result.message)
                    source_map={row["id"]:row["text"] for row in material_batch}
                    party_material_by_id={row["id"]:row for row in material_batch}
                    section_map={str(row.id):row.section_id for row in source_rows}
                    with self.transaction() as db:
                        self.locked(db)
                        ids=[]
                        for field in result.value.fields:
                            if field.field_key in PARTY_FIELD_KEYS:
                                role = resolve_party_role(field.role, field.raw_value, field.source_segment_ids,
                                    party_material_by_id,
                                    {role for key, role in allowed_fields if key == field.field_key})
                                if role is None:continue
                                field = field.model_copy(update={"role": role})
                            if (logical_id,field.field_key,field.role) in preserved_fields:continue
                            if (field.field_key, field.role) not in allowed_fields:
                                continue
                            if not source_value_present(field.raw_value, field.source_segment_ids, source_map):
                                continue
                            fa=em.FieldAssertion(tenant_id=self.tenant,case_id=self.case_id,parse_artifact_id=ld.parse_artifact_id,
                                logical_document_id=logical_id,field_key=field.field_key,role=field.role,
                                group="parties" if field.role in PARTY_ROLES
                                    else field.role if field.role in {"transport","goods","amounts"} else "identifiers",
                                section_id=section_map[field.source_segment_ids[0]],numeric_value=normalize_numeric(field.raw_value),
                                source_text=field.raw_value,candidate_value=field.raw_value,normalized_value=field.raw_value,
                                source_segment_ids=field.source_segment_ids,state="candidate",ambiguity=field.ambiguity)
                            db.add(fa);db.flush();ids.append(fa.id)
                        line_no = db.scalar(select(func.max(em.LineItem.line_no)).where(
                            em.LineItem.logical_document_id == logical_id)) or 0
                        seen_line_candidates = set()
                        batch_ids = set(source_map)
                        proven_rows = [row for row in table_lines if set(row.row_source_segment_ids).issubset(batch_ids)]
                        for row in ([] if logical_id in preserved_line_logicals else merge_table_candidates(result.value.line_items, proven_rows)):
                            if type_logicals and db.scalar(select(em.LineItem.id).where(em.LineItem.logical_document_id==logical_id,em.LineItem.source_segment_ids==row.row_source_segment_ids).limit(1)):continue
                            values = build_line_values(row.cells, row.row_source_segment_ids, line_source_map,
                                {source["id"]: source for source in material})
                            if values is None:
                                continue
                            if row in proven_rows:
                                for measurement in values["measurements"]:
                                    measurement["extraction_method"] = "structured_table/1"
                            fingerprint = line_evidence_fingerprint({cell.column_key: cell.raw_value for cell in row.cells},
                                values["cell_sources"], line_source_map)
                            if fingerprint is not None and fingerprint in seen_line_candidates:
                                continue
                            if fingerprint is not None:
                                seen_line_candidates.add(fingerprint)
                            pages = [anchors[UUID(key)].page_no for key in row.row_source_segment_ids
                                if UUID(key) in anchors and anchors[UUID(key)].page_no is not None]
                            line_no += 1
                            db.add(em.LineItem(tenant_id=self.tenant,case_id=self.case_id,parse_artifact_id=ld.parse_artifact_id,
                                logical_document_id=logical_id,line_no=line_no,source_segment_ids=row.row_source_segment_ids,
                                page_start=min(pages) if pages else None,page_end=max(pages) if pages else None,**values))
                        self.complete_step(db, step_id, ids)
                        return ids
                assertions.extend(self.step(f"抽取:{logical_id}:{batch_index}",
                    {"logical_id":str(logical_id),"segments":material_batch,"document_type":ld_type,"schema":prompt_schema,"input_budget":available},extract))
        self.assertion_ids=assertions
        return {}

    def verify_sources(self,state):
        with self.transaction() as db:
            run=self.locked(db)
            case=db.scalar(select(Case).where(Case.id==self.case_id).with_for_update())
            declared_sections = {}
            for logical in db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id.in_(self.parse_ids))):
                if self.single_type_input(db, logical):
                    for section in db.scalars(select(em.DocumentSection).where(em.DocumentSection.logical_document_id == logical.id)):
                        declared_sections[section.id] = (self.confirmed_type(db, logical), self.confirmed_cargo_profiles(db, logical))
            if run.current_business_revision_id:
                existing = db.get(em.BusinessRevision, run.current_business_revision_id)
                adopted = set(db.scalars(select(em.BusinessRevisionField.field_assertion_id).where(
                    em.BusinessRevisionField.business_revision_id == existing.id)))
                saved = {value.section_id: (value.logical_document_type, value.cargo_profile_ids) for value in db.scalars(
                    select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.business_revision_id == existing.id,
                        em.SectionAssignmentDecision.logical_document_type.is_not(None)))}
                if adopted == {UUID(value) for value in self.assertion_ids} and all(saved.get(key) == value for key, value in declared_sections.items()):
                    return {}
            revision_no=(db.scalar(select(func.max(em.BusinessRevision.revision_no)).where(em.BusinessRevision.case_id==self.case_id)) or 0)+1
            revision=em.BusinessRevision(tenant_id=self.tenant,case_id=self.case_id,revision_no=revision_no,reason="baseline_extraction",parent_revision_id=run.current_business_revision_id)
            db.add(revision);db.flush()
            for field_id in self.assertion_ids:
                db.add(em.BusinessRevisionField(tenant_id=self.tenant,business_revision_id=revision.id,field_assertion_id=UUID(field_id)))
            prior_decisions=list(db.scalars(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.business_revision_id==run.current_business_revision_id))) if run.current_business_revision_id else []
            copied_sections=set()
            for old in prior_decisions:
                if old.section_id in declared_sections:continue
                db.add(em.SectionAssignmentDecision(tenant_id=self.tenant,case_id=self.case_id,business_revision_id=revision.id,section_id=old.section_id,
                    logical_document_type=old.logical_document_type,cargo_profile_ids=old.cargo_profile_ids,target_logical_document_id=old.target_logical_document_id,confirmed_language=old.confirmed_language,
                    decided_by=old.decided_by,reason=old.reason,created_at=old.created_at));copied_sections.add(old.section_id)
            for section_id, (kind, cargo) in declared_sections.items():
                db.add(em.SectionAssignmentDecision(tenant_id=self.tenant, case_id=self.case_id, business_revision_id=revision.id,
                    section_id=section_id, logical_document_type=kind, cargo_profile_ids=cargo,
                    decided_by=run.initiated_by, reason="任务启动前明确选择单据类型与货物领域"))
                copied_sections.add(section_id)
            from app.workflows.models import InputRequest,InputRequestStatus
            for logical in db.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id.in_(self.parse_ids))):
                confirmed=self.confirmed_type(db,logical)
                cargo_profiles=self.confirmed_cargo_profiles(db,logical)
                request=self.boundary_answer(db, logical)
                if confirmed==logical.type_candidate.value and not cargo_profiles:continue
                if not request:continue
                for section in db.scalars(select(em.DocumentSection).where(em.DocumentSection.logical_document_id==logical.id)):
                    if section.id in copied_sections:continue
                    old=db.scalar(select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.section_id==section.id,em.SectionAssignmentDecision.business_revision_id==run.current_business_revision_id).order_by(em.SectionAssignmentDecision.created_at.desc()).limit(1)) if run.current_business_revision_id else None
                    db.add(em.SectionAssignmentDecision(tenant_id=self.tenant,case_id=self.case_id,business_revision_id=revision.id,section_id=section.id,
                        logical_document_type=confirmed,cargo_profile_ids=cargo_profiles,confirmed_language=old.confirmed_language if old else None,
                        decided_by=old.decided_by if old else request.answered_by,reason=old.reason if old else '用户确认输入类型'))
            run.current_business_revision_id=revision.id
            case.current_business_revision_id=revision.id
            case.business_revision_no=revision_no
        return {}

    def publish_index(self,state):
        with self.transaction() as db:
            run=self.locked(db)
            self.require_settled_steps(db)
            run.status=RunStatus.INDEXED;run.current_stage="识别已完成"
            append_event(db,run,"run_status","识别与来源核验完成，可对原件提问", {"parse_artifact_ids":[str(x) for x in self.parse_ids],
                "business_revision_id": str(run.current_business_revision_id)})
            from app.conversation.deferred import enqueue_run_continuations
            enqueue_run_continuations(db,run)
            from app.management.index import refresh_run_summaries
            refresh_run_summaries(db, run)
        return {}

    def close(self):
        if hasattr(self.gateway,"aclose"):
            self.runner.run(self.gateway.aclose())
        self.runner.close()
        self.engine.dispose()


@queue.task(name="document_agent.run")
def execute_run(run_id: str, epoch: int):
    worker=None
    try:
        worker=DocumentWorker(UUID(run_id),epoch)
        with worker.transaction() as db:
            run=worker.locked(db)
            workflow_type=run.workflow_type
            workflow_version=run.workflow_version
            active_epoch=db.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id, ExecutionEpoch.epoch==epoch))
            purpose=active_epoch.purpose
        if purpose == "export":
            from app.exports.worker import execute_export
            worker.close()
            worker=None
            execute_export(run_id,epoch)
            return
        definition=get_workflow(workflow_type, workflow_version)
        from app.infra.checkpoints import open_checkpointer,checkpoint_database_url
        url=checkpoint_database_url(worker.settings)
        with open_checkpointer(url) as saver:
            if purpose in {"quality_preview","issue_repair","issue_assessment","layout_refresh"}:
                from app.workflows.translation import TranslationNodes
                from langgraph.graph import StateGraph,START,END
                from app.workflows.state import RunGraphState
                nodes=TranslationNodes(worker);builder=StateGraph(RunGraphState)
                builder.add_node("load_review",nodes.load_review);builder.add_node("quality_and_preview",nodes.quality_and_preview);builder.add_node("publish_review",nodes.publish_review)
                builder.add_edge(START,"load_review")
                if purpose=="issue_assessment":
                    builder = StateGraph(RunGraphState)
                    builder.add_node("load_review", nodes.load_review); builder.add_node("assess_issue", nodes.assess_issue)
                    builder.add_edge(START, "load_review"); builder.add_edge("load_review", "assess_issue"); builder.add_edge("assess_issue", END)
                elif purpose=="issue_repair":builder.add_node("repair_local",nodes.repair_local);builder.add_edge("load_review","repair_local");builder.add_edge("repair_local","quality_and_preview")
                else:builder.add_edge("load_review","quality_and_preview")
                if purpose != "issue_assessment":
                    builder.add_edge("quality_and_preview","publish_review");builder.add_edge("publish_review",END)
                graph=builder.compile(checkpointer=saver)
            else:graph=definition.build_graph(worker, checkpointer=saver)
            graph.invoke({"run_id":run_id,"execution_epoch":epoch,"workflow_version":workflow_version},
                         {"configurable":{"thread_id":f"run:{run_id}:epoch:{epoch}"}}, durability="sync")
    except YieldExecution as exc:
        if worker and not getattr(exc,"persisted",False):worker.schedule_continuation(exc)
        return
    except StaleExecution:
        return
    except Exception as exc:
        if worker:
            try:
                with worker.transaction() as db:
                    run=worker.locked(db)
                    unknown=bool(getattr(exc,"unknown",False)) or isinstance(exc,MinerUError) and exc.kind=="acceptance_unknown"
                    unknown=unknown or db.scalar(select(StepExecution.id).where(StepExecution.run_id==run.id,
                        StepExecution.status==StepStatus.NEEDS_RECONCILE).limit(1)) is not None
                    unavailable=isinstance(exc, WorkflowVersionUnavailable)
                    run.status=RunStatus.AWAITING_INPUT if isinstance(exc, AwaitExternal) else RunStatus.PAUSED_RECONCILE if unknown or unavailable else RunStatus.FAILED_RETRYABLE
                    run.failure_reason="固定工作流版本未部署，请恢复该版本或人工迁移，不能自动更换图" if unavailable else "外部受理状态待核查" if unknown else "处理失败，请检查配置或安全重试"
                    if getattr(exc, "kind", None) == "budget_exhausted" and not unknown:
                        run.failure_reason = "Model processing budget exhausted. Completed results are preserved. Increase the processing budget or reduce the document range before retrying."
                    from app.consignment.models import ComparisonBatch
                    batch = db.scalar(select(ComparisonBatch).where(ComparisonBatch.run_id == run.id,
                        ComparisonBatch.epoch == epoch, ComparisonBatch.status == "building"))
                    if batch:
                        batch.status = "failed"
                        append_event(db, run, "comparison.failed", "本次核对未完成，原批次留存，重试会建立新的尝试批次", {"batch_id": str(batch.id)})
                    active = db.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id, ExecutionEpoch.epoch == epoch))
                    if active and active.purpose in {"issue_repair", "issue_assessment"} and active.repair_request.get("issue_id"):
                        from app.review.models import Issue
                        issue = db.get(Issue, UUID(active.repair_request["issue_id"]))
                        if issue and issue.case_id == run.case_id:
                            failure_guidance = {"agent_status": "needs_reconcile" if unknown else "failed",
                                "agent_response": "模型外部受理状态待核查，尚未完成处理。" if unknown else "本次 Agent 处理失败，原内容和未决疑点保留。"}
                            unchanged = getattr(exc, "kind", None) == "revision_unchanged" and not unknown
                            if unchanged:
                                failure_guidance["agent_response"] = str(exc)
                                run.failure_reason = "Agent 未产生译文修改，疑点保持待处理"
                            if issue.comparison_batch_id:
                                active.repair_request = {**active.repair_request, **failure_guidance}
                            else:
                                issue.guidance = {**(issue.guidance or {}), **failure_guidance}
                            case = db.get(Case, run.case_id); case.review_state_version += 1
                            if (active.purpose == "issue_assessment" and not unknown) or unchanged:
                                run.status = RunStatus(active.repair_request["return_status"])
                                run.current_stage = "Agent 分析失败，原内容保留待复核"
                    append_event(db,run,"run_status",run.failure_reason)
                    from app.conversation.deferred import enqueue_run_continuations
                    enqueue_run_continuations(db,run)
            except StaleExecution:
                pass
        raise
    finally:
        if worker:worker.close()


@queue.task(name="document_agent.continuation")
def execute_continuation(continuation_id: str):
    from app.conversation.deferred import answer_continuation
    from app.infra.database import create_database_engine, create_session_factory
    settings=get_settings()
    engine=create_sync_engine(settings)
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE logistics_worker"))
            tenant=conn.execute(text("SELECT worker_tenant_of_continuation(:id)"),{"id":UUID(continuation_id)}).scalar_one_or_none()
        if tenant is None:return
        async def execute():
            async_engine=create_database_engine(settings)
            try:
                await answer_continuation(create_session_factory(async_engine),settings,UUID(continuation_id),tenant)
            finally:
                await async_engine.dispose()
        with asyncio.Runner() as runner:runner.run(execute())
    finally:
        engine.dispose()


# 注册对话回合消费者，与单证任务共用已配置队列。
from app.conversation import worker as conversation_worker


@queue.task(name="document_agent.purge")
def execute_case_purge(deletion_id: str):
    from app.management.purge import execute_purge
    return execute_purge(deletion_id)
