"""LangGraph节点调用适配，不自行建立Worker事务或绕过执行栅栏。"""
from uuid import UUID
from sqlalchemy import select
from app.consignment import service, models
from app.documents.models import Case
from app.documents.evidence_models import FieldAssertion,BusinessRevisionField
from app.workflows.models import RunStatus, RunEvent


def prepare_comparison(db, run, settings, parse_ids):
    case=db.get(Case,run.case_id)
    scope_id=run.task_spec.get("scope_revision_id")
    if not scope_id:
        scope=service.ensure_scope(db,case,run.initiated_by)
        scope_id=scope.id
    assertion_ids=list(db.scalars(select(FieldAssertion.id).join(BusinessRevisionField,
        BusinessRevisionField.field_assertion_id==FieldAssertion.id).where(FieldAssertion.case_id==case.id,
        BusinessRevisionField.business_revision_id==run.current_business_revision_id,
        FieldAssertion.parse_artifact_id.in_(parse_ids))))
    previous = db.scalar(select(models.ComparisonBatch).where(models.ComparisonBatch.run_id == run.id,
        models.ComparisonBatch.epoch < run.current_epoch).order_by(models.ComparisonBatch.epoch.desc()).limit(1))
    parent = previous.id if previous else run.task_spec.get("previous_batch_id")
    batch=service.prepare_comparison(db,run,case,scope_revision_id=scope_id,parse_ids=parse_ids,
        assertion_ids=assertion_ids,parent_batch_id=parent)
    return batch.id


def publish_comparison(db,run,settings,batch_id):
    case=db.get(Case,run.case_id)
    batch=service.owned(db.get(models.ComparisonBatch,UUID(str(batch_id))),case)
    if batch.status=="completed":return {"batch_id":str(batch.id),**batch.summary}
    result=service.publish_comparison(db,run,case,batch)
    result = {**result, "parse_artifact_ids": [p["id"] for p in batch.manifest["parses"]],
        "business_revision_id": str(batch.business_revision_id)}
    if service.batch_current(db,case,batch):service.bind_comparison(db,run,case,batch)
    service.try_rebind_compatible(db,case,batch)
    if run.workflow_type=="consignment_check":
        run.status=RunStatus.CHECKED;run.current_stage="核对完成"
    run.next_event_seq+=1
    db.add(RunEvent(tenant_id=run.tenant_id,run_id=run.id,seq=run.next_event_seq,event_type="comparison.completed",
        status=run.status.value,stage=run.current_stage,message="同票材料核对完成",payload=result))
    from app.management.index import refresh_run_summaries
    refresh_run_summaries(db, run)
    if run.workflow_type=="consignment_check":
        from app.conversation.deferred import enqueue_run_continuations
        enqueue_run_continuations(db,run)
    return result
