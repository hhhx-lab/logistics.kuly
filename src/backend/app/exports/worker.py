"""同步导出 Worker：全组回检成功后才原子登记快照。"""
from __future__ import annotations
import hashlib
import copy
from datetime import UTC,datetime,timedelta
from uuid import UUID
from sqlalchemy import select,text,String
from app.documents import evidence_models as em
from app.documents.models import StoredObject,StoredObjectCategory,Case,CaseStatus,DocumentVersion
from app.documents.retention import touch_material_event
from app.exports import render
from app.exports.gate import review_gate
from app.exports.models import ReviewConfirmation,ExportAttempt,ExportAttemptItem,ExportArtifact,ExportSnapshot
from app.review import models as rm
from app.workflows.models import RunStatus,Run,QueueOutbox

ITEM_BUDGET_SECONDS=240  # soffice120 + document verify/build + bounded S3 read/write
REUSE_BUDGET_SECONDS=45
FINALIZE_BUDGET_SECONDS=600  # frozen source set: at most 11 HEAD/hash reads plus preflight


def freeze_export_item(db,run,attempt_id,item):
    """Copy metadata only; S3 reads must happen after this transaction commits."""
    obj=db.get(StoredObject,item.object_id) if item.object_id else None
    if not (item.tenant_id==run.tenant_id and item.case_id==run.case_id and item.attempt_id==attempt_id
        and item.status=='succeeded' and item.verify_manifest and obj
        and obj.tenant_id==run.tenant_id and obj.case_id==run.case_id
        and obj.category==StoredObjectCategory.EXPORT and not obj.is_visible
        and obj.finalizing_attempt_id==attempt_id and obj.sha256==item.sha256 and obj.size_bytes==item.size_bytes):
        return None
    return {"item":{column.name:copy.deepcopy(getattr(item,column.name)) for column in item.__table__.columns},
        "object":{column.name:copy.deepcopy(getattr(obj,column.name)) for column in obj.__table__.columns}}


def export_object_matches(worker,frozen):
    worker.ensure_time(REUSE_BUDGET_SECONDS)
    meta=frozen['object'];expected=frozen['item']
    head=worker.store.head(meta['object_key'])
    if head is None or head.size_bytes!=expected['size_bytes']:return False
    digest=hashlib.sha256();size=0
    chunks=worker.store.stream(meta['object_key'])
    try:
        for chunk in chunks:
            worker.ensure_time(REUSE_BUDGET_SECONDS)
            size+=len(chunk);digest.update(chunk)
            if size>expected['size_bytes']:return False
    finally:
        chunks.close()
    return size==expected['size_bytes'] and digest.hexdigest()==expected['sha256']


def recheck_export_item(db,run,attempt_id,frozen):
    item=db.scalar(select(ExportAttemptItem).where(ExportAttemptItem.id==frozen['item']['id'])
        .with_for_update().execution_options(populate_existing=True))
    if item and item.object_id:
        db.scalar(select(StoredObject).where(StoredObject.id==item.object_id)
            .with_for_update().execution_options(populate_existing=True))
    if item is None or freeze_export_item(db,run,attempt_id,item)!=frozen:
        raise RuntimeError('正式产物元数据在实际回检期间变化，未登记快照')
    return item


def export_advisory_key(run_id,epoch):
    key=int(hashlib.sha256(f"export:{run_id}:{epoch}".encode()).hexdigest()[:16],16)
    return key-2**64 if key>=2**63 else key


def recover_expired_exports(db,moment,batch_size=50)->int:
    """Called in the recovery tenant transaction; local rendering can be safely retried."""
    from app.config import get_settings
    from app.workflows.worker import append_event
    expiry=moment-timedelta(seconds=get_settings().workflow_task_hard_limit_seconds+60)
    pending=select(QueueOutbox.id).where(QueueOutbox.topic=='run',QueueOutbox.status.in_(['pending','failed']),
        QueueOutbox.payload['run_id'].astext==Run.id.cast(String),QueueOutbox.payload['epoch'].astext==Run.current_epoch.cast(String)).exists()
    candidates=list(db.execute(select(ExportAttempt.id,ExportAttempt.case_id,ExportAttempt.run_id,ExportAttempt.execution_epoch)
        .join(Run,Run.id==ExportAttempt.run_id).join(Case,Case.id==Run.case_id)
        .where(ExportAttempt.status.in_(['queued','rendering','checking']),ExportAttempt.updated_at<expiry,
            ExportAttempt.execution_epoch==Run.current_epoch,Run.status==RunStatus.EXPORTING,
            Case.status==CaseStatus.ACTIVE,Case.retention_expires_at>moment,~pending)
        .order_by(ExportAttempt.updated_at,ExportAttempt.id).limit(batch_size)))
    recovered=0
    for attempt_id,case_id,run_id,epoch in candidates:
        case=db.scalar(select(Case).where(Case.id==case_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if case is None or case.status!=CaseStatus.ACTIVE or case.retention_expires_at<=moment:continue
        run=db.scalar(select(Run).where(Run.id==run_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if run is None or run.status!=RunStatus.EXPORTING or run.current_epoch!=epoch:continue
        settings=get_settings()
        if settings.deletion_ledger_path or settings.deletion_ledger_key:
            from app.management.ledger import DeletionLedger
            if DeletionLedger(settings).is_denied(run.tenant_id,case.id):continue
        attempt=db.scalar(select(ExportAttempt).where(ExportAttempt.id==attempt_id).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if attempt is None or attempt.status not in {'queued','rendering','checking'} or attempt.updated_at>=expiry:continue
        if db.scalar(select(QueueOutbox.id).where(QueueOutbox.topic=='run',QueueOutbox.status.in_(['pending','failed']),
            QueueOutbox.payload['run_id'].astext==str(run.id),QueueOutbox.payload['epoch'].astext==str(epoch)).limit(1)):continue
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),{'key':export_advisory_key(run.id,epoch)}):continue
        run.execution_fence+=1;run.updated_at=moment;run.current_stage='恢复原导出尝试'
        attempt.status='queued';attempt.updated_at=moment;attempt.error_summary=None
        db.add(QueueOutbox(tenant_id=run.tenant_id,topic='run',dedupe_key=f'export-recovery:{attempt.id}:{epoch}:{run.execution_fence}',
            payload={'run_id':str(run.id),'epoch':epoch}))
        append_event(db,run,'export_step','检测到导出执行中断，已安排同一尝试安全续跑',{'attempt_id':str(attempt.id)})
        recovered+=1
    return recovered


def yield_export(worker,attempt_id):
    from app.workflows.worker import append_event
    with worker.transaction() as db:
        run=worker.locked(db)
        attempt=db.scalar(select(ExportAttempt).where(ExportAttempt.id==attempt_id).with_for_update())
        if attempt is None or attempt.execution_epoch!=worker.epoch or attempt.status=='succeeded':return
        run.execution_fence+=1;run.status=RunStatus.EXPORTING;run.current_stage='等待同一导出尝试续跑';run.updated_at=datetime.now(UTC)
        attempt.status='queued';attempt.updated_at=run.updated_at;attempt.error_summary=None
        db.add(QueueOutbox(tenant_id=run.tenant_id,topic='run',dedupe_key=f'export-yield:{attempt.id}:{worker.epoch}:{run.execution_fence}',
            payload={'run_id':str(run.id),'epoch':worker.epoch},next_attempt_at=run.updated_at+timedelta(seconds=1)))
        append_event(db,run,'export_step','已保存整项回检产物，稍后继续同一导出尝试；已完成项复核后复用',{'attempt_id':str(attempt.id)})


class FrozenExportNodes:
    def __init__(self,worker,attempt_id):
        self.worker=worker
        self.attempt_id=attempt_id

    def preflight(self,state):
        from app.workflows.worker import StaleExecution
        worker=self.worker
        with worker.transaction() as db:
            run=worker.locked(db);worker.fence=run.execution_fence
            attempt=db.scalar(select(ExportAttempt).where(ExportAttempt.id==self.attempt_id,ExportAttempt.run_id==run.id,ExportAttempt.execution_epoch==worker.epoch).with_for_update())
            if attempt is None:raise StaleExecution("导出尝试不存在")
            if attempt.status=="succeeded":raise StaleExecution("导出尝试已成功")
            attempt_id=attempt.id
            confirmation=db.get(ReviewConfirmation,attempt.confirmation_id)
            gate=review_gate(db,run)
            if not gate["confirmable"] or gate["review_fingerprint"]!=confirmation.review_fingerprint:raise RuntimeError("导出前复核配对已变化")
            attempt.status="rendering";attempt.updated_at=datetime.now(UTC);attempt.error_summary=None
            run.status=RunStatus.EXPORTING;run.current_stage="生成确认稿";run.updated_at=attempt.updated_at
            worker.case_id=run.case_id
            manifest=confirmation.review_manifest
            case=db.get(Case,run.case_id)
            case_name=case.name
            translation=db.get(rm.TranslationRevision,confirmation.translation_revision_id)
            business=db.get(em.BusinessRevision,confirmation.business_revision_id)
            manifest={**manifest,"business_revision_no":business.revision_no,"translation_revision_no":translation.revision_no}
            fields=list(db.scalars(select(em.FieldAssertion).where(em.FieldAssertion.id.in_([UUID(x) for x in manifest["field_assertion_ids"]]))))
            frozen_fields={f["id"]:f for f in manifest.get("field_values",[])}
            field_rows=[{"name":f.field_key,"role":f.role,"source":f.source_text or "","value":f.normalized_value or f.candidate_value or "","parse_id":str(f.parse_artifact_id),"reason":f.reason,"basis":f.basis,
                "review_status":"人工采用值" if f.is_human else "已人工确认" if frozen_fields.get(str(f.id),{}).get("review_status")=="confirmed" else "机器候选，未逐项人工确认"} for f in fields]
            checks=[{"rule_id":c.rule_id,"version":c.rule_version,"status":c.status,"explanation":c.explanation,"calculation":c.calculation} for c in db.scalars(select(rm.RuleCheck).where(rm.RuleCheck.id.in_([UUID(x) for x in manifest["rule_check_ids"]])))]
            issue_rows=[]
            for issue in db.scalars(select(rm.Issue).where(rm.Issue.id.in_([UUID(x) for x in manifest["issue_ids"]]))):
                disposition=db.scalar(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id==issue.id,
                    rm.IssueDisposition.id.in_([UUID(x) for x in manifest["issue_disposition_ids"]])).order_by(
                    rm.IssueDisposition.review_sequence.desc(),rm.IssueDisposition.created_at.desc(),rm.IssueDisposition.id.desc()).limit(1))
                issue_rows.append({"title":issue.title,"description":issue.description,"sources":issue.sources,
                    "disposition":{"action":disposition.action,"reason":disposition.reason,"basis":disposition.basis,"by":str(disposition.decided_by)} if disposition else None})
            item_ids=list(db.scalars(select(ExportAttemptItem.id).where(ExportAttemptItem.attempt_id==attempt.id).order_by(ExportAttemptItem.ordinal)))
            attempt_id=attempt.id
        self.manifest=manifest
        self.case_name=case_name
        self.field_rows=field_rows
        self.checks=checks
        self.issue_rows=issue_rows
        self.item_ids=item_ids
        self.translation_segment_ids=list(translation.segment_ids)
        self.is_synthetic=translation.is_synthetic
        return {"task_spec_hash":run.task_spec_hash}

    def render(self,state):
        from app.workflows.worker import append_event
        worker,attempt_id=self.worker,self.attempt_id
        manifest,case_name=self.manifest,self.case_name
        field_rows,checks,issue_rows=self.field_rows,self.checks,self.issue_rows
        item_ids=self.item_ids
        cache={}
        for item_id in item_ids:
            worker.ensure_time(REUSE_BUDGET_SECONDS)
            with worker.transaction() as db:
                run=worker.locked(db)
                item=db.get(ExportAttemptItem,item_id)
                frozen=freeze_export_item(db,run,attempt_id,item)
            reusable=frozen is not None and export_object_matches(worker,frozen)
            with worker.transaction() as db:
                run=worker.locked(db)
                item=recheck_export_item(db,run,attempt_id,frozen) if frozen else db.get(ExportAttemptItem,item_id)
                if reusable:
                    confirmation=db.get(ReviewConfirmation,db.get(ExportAttempt,attempt_id).confirmation_id)
                    gate=review_gate(db,run)
                    if not gate['confirmable'] or gate['review_fingerprint']!=confirmation.review_fingerprint:
                        raise RuntimeError('成功项复用前复核配对已变化')
                    db.get(ExportAttempt,attempt_id).updated_at=datetime.now(UTC);run.updated_at=datetime.now(UTC)
                    continue
                worker.ensure_time(ITEM_BUDGET_SECONDS)
                item.status="rendering"
                db.get(ExportAttempt,attempt_id).updated_at=datetime.now(UTC);run.updated_at=datetime.now(UTC)
                append_event(db,run,"export_step",f"正在生成 {item.filename}",{"attempt_id":str(attempt_id),"item_id":str(item.id)})
                kind,document_id=item.kind,item.document_id
            if kind=="report":
                docx=render.build_report(case_name,manifest,checks,issue_rows,field_rows)
                data=render.to_pdf(docx);expected=render.report_expected(case_name,manifest,checks,issue_rows,field_rows)
            else:
                mode="source" if kind=="source_pdf" else "translation"
                cache_key=(document_id,mode)
                if cache_key not in cache:
                    pairing=next(p for p in manifest["pairings"] if p["document_id"]==str(document_id))
                    source=next(s for s in manifest["source_versions"] if s["document_id"]==str(document_id))
                    with worker.transaction() as db:
                        worker.locked(db)
                        segments=list(db.scalars(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(x) for x in self.translation_segment_ids]),rm.TranslationSegment.document_id==document_id).order_by(rm.TranslationSegment.ordinal,rm.TranslationSegment.id)))
                        parse=db.get(em.ParseArtifact,UUID(pairing["parse_artifact_id"]))
                        dv=db.scalar(select(DocumentVersion).where(DocumentVersion.document_id==document_id,DocumentVersion.version==source["document_version"]))
                        obj=db.get(StoredObject,dv.object_id)
                        raw=worker.store.read_all(obj.object_key)
                        from app.review.layout import document_layout
                        ocr=db.get(StoredObject,parse.raw_response_object_id) if parse.raw_response_object_id else None
                        ocr_raw=worker.store.read_all(ocr.object_key) if ocr and ocr.is_visible and ocr.mime=="application/zip" and not parse.layout_manifest else None
                        layout,originals=document_layout(db,parse,raw,dv.mime,ocr_raw)
                        source_text={str(s.id):s.text for s in originals}
                    cache[cache_key]=render.build_layout_translation(layout,source_text,segments,target_language=manifest["target_language"],mode=mode)
                docx,expected=cache[cache_key]
                data=docx if kind=="docx" else render.to_pdf(docx)
            verification=render.verify(data,"pdf" if kind=="source_pdf" else kind,expected)
            key=worker.store.build_key(worker.tenant,worker.case_id,StoredObjectCategory.EXPORT)
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document" if kind=="docx" else "application/pdf"
            with worker.transaction() as db:
                worker.locked(db)
            worker.store._internal.put_object(Bucket=worker.store._bucket,Key=key,Body=data,ContentType=mime)
            with worker.transaction() as db:
                run=worker.locked(db);item=db.get(ExportAttemptItem,item_id)
                obj=StoredObject(tenant_id=worker.tenant,case_id=run.case_id,object_key=key,category=StoredObjectCategory.EXPORT,
                    mime=mime,size_bytes=len(data),sha256=verification["sha256"],finalizing_attempt_id=attempt_id,
                    lease_until=datetime.now(UTC)+timedelta(hours=24),is_visible=False)
                db.add(obj);db.flush()
                item.object_id,item.size_bytes,item.sha256,item.verify_manifest,item.status=obj.id,len(data),verification["sha256"],verification,"succeeded"
                db.get(ExportAttempt,attempt_id).updated_at=datetime.now(UTC);run.updated_at=datetime.now(UTC)
                append_event(db,run,"export_step",f"{item.filename} 已回检",{"attempt_id":str(attempt_id),"item_id":str(item.id)})
        return {}

    def verify(self,state):
        worker,attempt_id=self.worker,self.attempt_id
        worker.ensure_time(FINALIZE_BUDGET_SECONDS)
        with worker.transaction() as db:
            run=worker.locked(db)
            attempt=db.get(ExportAttempt,attempt_id)
            confirmation=db.get(ReviewConfirmation,attempt.confirmation_id)
            gate=review_gate(db,run)
            if not gate["confirmable"] or gate["review_fingerprint"]!=confirmation.review_fingerprint:raise RuntimeError("产物生成后复核配对已变化，未登记快照")
            items=list(db.scalars(select(ExportAttemptItem).where(ExportAttemptItem.attempt_id==attempt.id).order_by(ExportAttemptItem.ordinal)))
            if not all(item.status=="succeeded" and item.object_id and item.verify_manifest for item in items):raise RuntimeError("整组必需产物尚未全部回检")
            frozen_items=[freeze_export_item(db,run,attempt_id,item) for item in items]
            if any(item is None for item in frozen_items):raise RuntimeError('正式产物归属或冻结元数据不符')
        for frozen in frozen_items:
            if not export_object_matches(worker,frozen):raise RuntimeError('正式产物缺失或实际哈希变化，未登记快照')
        self.frozen_items=frozen_items
        return {}

    def commit_snapshot(self,state):
        from app.workflows.worker import append_event
        worker,attempt_id=self.worker,self.attempt_id
        manifest,checks,frozen_items=self.manifest,self.checks,self.frozen_items
        with worker.transaction() as db:
            run=worker.locked(db)
            attempt=db.scalar(select(ExportAttempt).where(ExportAttempt.id==attempt_id).with_for_update())
            confirmation=db.get(ReviewConfirmation,attempt.confirmation_id)
            gate=review_gate(db,run)
            if not gate['confirmable'] or gate['review_fingerprint']!=confirmation.review_fingerprint:raise RuntimeError('产物实际回检后复核配对已变化，未登记快照')
            items=list(db.scalars(select(ExportAttemptItem).where(ExportAttemptItem.attempt_id==attempt.id).order_by(ExportAttemptItem.ordinal)))
            if [item.id for item in items]!=[frozen['item']['id'] for frozen in frozen_items]:raise RuntimeError('整组产物清单在回检期间变化')
            items=[recheck_export_item(db,run,attempt_id,frozen) for frozen in frozen_items]
            existing=db.scalar(select(ExportSnapshot).where(ExportSnapshot.attempt_id==attempt.id))
            if existing:return {"artifact_ids":[str(existing.id)]}
            manifest["not_covered_notes"]=[c["explanation"] for c in checks if c["status"]=="not_covered"]
            manifest["confirmed_by"]=str(confirmation.confirmed_by)
            manifest["export_items"]=[{"document_id":str(item.document_id) if item.document_id else None,"kind":item.kind,"filename":item.filename} for item in items]
            snapshot=ExportSnapshot(tenant_id=run.tenant_id,case_id=run.case_id,run_id=run.id,confirmation_id=confirmation.id,
                attempt_id=attempt.id,manifest=manifest,exported_by=attempt.created_by)
            db.add(snapshot);db.flush()
            for item in items:
                obj=db.get(StoredObject,item.object_id);obj.is_visible=True;obj.finalizing_attempt_id=None;obj.lease_until=None
                db.add(ExportArtifact(tenant_id=run.tenant_id,case_id=run.case_id,snapshot_id=snapshot.id,
                    document_id=item.document_id,document_version=item.document_version,kind=item.kind,object_id=item.object_id,
                    filename=item.filename,size_bytes=item.size_bytes,sha256=item.sha256))
            from app.management.index import refresh_search_projection
            refresh_search_projection(db,run,snapshot)
            attempt.status,attempt.snapshot_id,attempt.updated_at="succeeded",snapshot.id,datetime.now(UTC)
            run.status,run.current_stage=RunStatus.EXPORTED,"整组确认稿已导出"
            touch_material_event(db.get(Case,run.case_id),worker.settings)
            append_event(db,run,"run_status","所选译本及识别稿已回检，成功快照已登记",{"snapshot_id":str(snapshot.id)})
        return {"artifact_ids":[str(snapshot.id)]}


def execute_export(run_id:str,epoch:int):
    from app.workflows.worker import DocumentWorker,StaleExecution,YieldExecution,append_event
    worker=DocumentWorker(UUID(run_id),epoch)
    advisory=worker.engine.connect()
    advisory_key=export_advisory_key(run_id,epoch)
    acquired=advisory.execute(text("SELECT pg_try_advisory_lock(:key)"),{"key":advisory_key}).scalar_one()
    if not acquired:
        advisory.close();worker.close();return
    try:
        with worker.transaction() as db:
            run=worker.locked(db)
            attempt=db.scalar(select(ExportAttempt).where(ExportAttempt.run_id==run.id,ExportAttempt.execution_epoch==epoch).with_for_update())
            if attempt is None:raise StaleExecution("导出尝试不存在")
            if attempt.status=="succeeded":return
            attempt_id=attempt.id
        from app.infra.checkpoints import open_checkpointer,checkpoint_database_url
        from app.workflows.graphs import build_export_graph
        with open_checkpointer(checkpoint_database_url(worker.settings)) as saver:
            graph=build_export_graph(FrozenExportNodes(worker,attempt_id)).compile(checkpointer=saver)
            graph.invoke({"run_id":run_id,"execution_epoch":epoch,"workflow_version":1},
                {"configurable":{"thread_id":f"run:{run_id}:epoch:{epoch}"}},durability="sync")
    except YieldExecution:
        try:yield_export(worker,attempt_id)
        except StaleExecution:pass
        return
    except StaleExecution:
        return
    except Exception:
        with worker.transaction() as db:
            try:
                run=worker.locked(db)
                attempt=db.scalar(select(ExportAttempt).where(ExportAttempt.run_id==run.id,ExportAttempt.execution_epoch==epoch))
                if attempt and attempt.status=="succeeded":
                    # 快照领域事务先于最终检查点，检查点故障不能撤销已提交交付。
                    return
                if attempt:
                    attempt.status="failed_retryable";attempt.error_summary="确认稿生成或回检失败，请检查原导出尝试并安全重试"
                    attempt.updated_at=datetime.now(UTC)
                    for item in db.scalars(select(ExportAttemptItem).where(ExportAttemptItem.attempt_id==attempt.id,ExportAttemptItem.status!="succeeded")):item.status="failed"
                current=review_gate(db,run)
                run.status,run.current_stage=(RunStatus.READY_TO_EXPORT,"上次导出失败") if current["confirmable"] else (RunStatus.AWAITING_REVIEW,"复核配对已变化，需重新核对")
                append_event(db,run,"export_step","导出失败，尚无本次成功快照；可重试原尝试")
            except StaleExecution:pass
        raise
    finally:
        try:advisory.execute(text("SELECT pg_advisory_unlock(:key)"),{"key":advisory_key})
        finally:advisory.close()
        worker.close()
