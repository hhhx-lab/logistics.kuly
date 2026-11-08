"""Run解析集合独立于同票其他Run；首次发布前可读已封存的当前批次解析。"""
from uuid import UUID
from sqlalchemy import select
from app.documents import evidence_models as em
from app.review.models import TranslationRevision
from app.workflows.models import RunEvent, ExecutionEpoch, RunDocument, StepExecution, StepStatus


def run_parse_ids(db, run):
    if run.current_translation_revision_id:
        revision=db.get(TranslationRevision,run.current_translation_revision_id)
        if revision:return [UUID(x) for x in revision.parse_artifact_ids]
    events=db.scalars(select(RunEvent).where(RunEvent.run_id==run.id).order_by(RunEvent.seq.desc()))
    for event in events:
        if event.payload and event.payload.get("parse_artifact_ids"):
            return [UUID(x) for x in event.payload["parse_artifact_ids"]]
    epoch=db.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id,ExecutionEpoch.epoch==run.current_epoch))
    candidates = list(epoch.input_parse_artifact_ids) if epoch else []
    if epoch and not candidates:
        prior_epochs = db.scalars(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id,
            ExecutionEpoch.epoch < run.current_epoch).order_by(ExecutionEpoch.epoch.desc()))
        candidates = next((list(prior.input_parse_artifact_ids) for prior in prior_epochs
            if prior.input_parse_artifact_ids), [])
    steps = db.scalars(select(StepExecution).where(StepExecution.run_id == run.id,
        StepExecution.epoch == run.current_epoch, StepExecution.status == StepStatus.SUCCEEDED,
        StepExecution.step_name.startswith("解析:")).order_by(StepExecution.created_at, StepExecution.id))
    for step in steps:
        candidates.extend(step.artifact_ids)
    frozen = {(item.document_id, item.document_version) for item in db.scalars(select(RunDocument).where(RunDocument.run_id == run.id))}
    selected = {}
    for value in candidates:
        try:
            parse = db.get(em.ParseArtifact, UUID(str(value)))
        except (ValueError, TypeError):
            continue
        if (parse and parse.tenant_id == run.tenant_id and parse.case_id == run.case_id
            and parse.quality != em.ParseQuality.FAILED and (parse.document_id, parse.document_version) in frozen):
            selected[(parse.document_id, parse.document_version)] = parse.id
    return list(selected.values())


def run_parse(db,run,document_id,version):
    ids=run_parse_ids(db,run)
    return db.scalar(select(em.ParseArtifact).where(em.ParseArtifact.id.in_(ids),em.ParseArtifact.document_id==document_id,
        em.ParseArtifact.document_version==version)) if ids else None
