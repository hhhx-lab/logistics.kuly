"""已知零外发的材料分析局部O02；真实PG外层回滚，无模型请求。"""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import evidence_models as em
from app.documents.models import Case
from app.exports.models import ExportAttempt, ExportSnapshot, ReviewConfirmation
from app.review import models as rm
from app.workflows.models import ExecutionEpoch, ModelMigration, QueueOutbox, Run, RunStatus, StepExecution, StepStatus
from tests.test_review_field_gate import current_model_bindings, seed_fields
from tests.test_workflow_response_contracts import http_db


async def seed_failed_assessment(conn, principal, created):
    from app.prompts.registry import get_prompt_registry
    ids = await seed_fields(conn, principal, created)
    bindings = await current_model_bindings('extract', 'translate')
    bindings = [{**binding, 'model_name': 'previous-frozen-model'} for binding in bindings]
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        run = await db.get(Run, ids['run_id']); run.current_epoch = 10; run.status = RunStatus.EXPORTED
        scope = rm.ResolvedTranslationScope(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'],
            business_revision_id=ids['revision_id'], selector_version='all@1', source_section_ids=[str(ids['section_id'])],
            source_segment_ids=[str(ids['source_id'])], fingerprint='1'*64)
        db.add(scope); await db.flush()
        revision = rm.TranslationRevision(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            business_revision_id=ids['revision_id'], target_language='en', revision_no=1,
            parse_artifact_ids=[str(ids['parse_id'])], resolved_scope_ids=[str(scope.id)], segment_ids=[],
            prompt_versions={}, model_bindings=bindings, qa_status='passed', qa_summary={}, dependency_fingerprint='2'*64)
        db.add(revision); await db.flush(); run.current_translation_revision_id = revision.id
        preview = rm.PreviewArtifact(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'],
            business_revision_id=ids['revision_id'], translation_revision_id=revision.id, resolved_scope_id=scope.id,
            target_language='en', preview_version=1, render_config_version='test@1', status='valid',
            reading_html='<p>Existing translation</p>', segment_mappings=[])
        db.add(preview); await db.flush()
        dependency = em.ArtifactDependency(tenant_id=principal.tenant_id, case_id=run.case_id, artifact_kind='preview',
            artifact_id=preview.id, depends_on={'translation_revision_id': str(revision.id)}, fingerprint='3'*64, stale=False)
        confirmation = ReviewConfirmation(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            task_spec_hash=run.task_spec_hash, review_fingerprint='4'*64, business_revision_id=ids['revision_id'],
            translation_revision_id=revision.id, pairing_manifest={}, review_manifest={}, output_formats=['pdf'], confirmed_by=principal.user_id)
        db.add_all([dependency, confirmation]); await db.flush()
        attempt = ExportAttempt(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            confirmation_id=confirmation.id, review_fingerprint='4'*64, status='succeeded', execution_epoch=9, created_by=principal.user_id)
        db.add(attempt); await db.flush()
        snapshot = ExportSnapshot(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            confirmation_id=confirmation.id, attempt_id=attempt.id, manifest={'translation_revision_id': str(revision.id)}, exported_by=principal.user_id)
        db.add(snapshot)
        issue = await db.get(rm.Issue, ids['issues'][0]); issue.category = 'undetermined'
        issue.guidance = {'recommendation': '请核对现有材料并说明需要补拍的区域', 'agent_status': 'failed', 'agent_request_no': 1}
        request = {'issue_id': str(issue.id), 'segment_id': None, 'translation_revision_id': str(revision.id),
            'instruction': issue.guidance['recommendation'], 'actor_id': str(principal.user_id),
            'prompt': get_prompt_registry().freeze(['document.issue_assess'])['document.issue_assess'],
            'prompt_key': 'document.issue_assess', 'return_status': 'exported', 'agent_request_no': 1,
            'agent_status': 'queued', 'agent_response': None, 'request_action': 'apply-recommendation'}
        epoch = ExecutionEpoch(tenant_id=principal.tenant_id, run_id=run.id, epoch=10, purpose='issue_assessment',
            graph_version='translate_review@1', input_business_revision_id=ids['revision_id'],
            input_parse_artifact_ids=[str(ids['parse_id'])], model_bindings=bindings, repair_request=request)
        step = StepExecution(tenant_id=principal.tenant_id, run_id=run.id, epoch=10,
            step_name='疑点材料分析:' + str(issue.id), input_fingerprint='5'*64, status=StepStatus.FAILED,
            error_class='binding_mismatch', finished_at=datetime.now(UTC), usage={}, artifact_ids=[])
        db.add_all([epoch, step]); await db.flush()
        return {**ids, 'preview_id': preview.id, 'translation_id': revision.id, 'dependency_id': dependency.id,
            'confirmation_id': confirmation.id, 'snapshot_id': snapshot.id, 'epoch_id': epoch.id, 'step_id': step.id, 'request': request}


@pytest.mark.asyncio
async def test_local_o02_preserves_delivery_and_replays_same_migration(http_db):
    http, conn, principal, created = http_db
    ids = await seed_failed_assessment(conn, principal, created)
    url = f"/api/v1/ops/runs/{ids['run_id']}"
    options = await http.get(url + '/model-migration-options')
    assert options.status_code == 200, options.text
    view = options.json(); assert view['safe_now'] and view['options'][0]['invalidated_steps'] == ['assess_issue']
    assert '仅恢复本次疑点分析' in view['options'][0]['label']
    body = {'candidate_config_fingerprint': view['options'][0]['fingerprint'], 'expected_execution_epoch': 10,
        'reason': '按已授权的新模型仅继续这一次资料分析'}
    stale = await http.post(url + '/migrate-model', headers={'Idempotency-Key': uuid4().hex}, json={**body, 'expected_execution_epoch': 9})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'EPOCH_STALE'
    key = uuid4().hex
    accepted = await http.post(url + '/migrate-model', headers={'Idempotency-Key': key}, json=body)
    assert accepted.status_code == 200 and accepted.json() == {'accepted': True, 'execution_epoch': 11}, accepted.text
    replay = await http.post(url + '/migrate-model', headers={'Idempotency-Key': key}, json=body)
    assert replay.status_code == 200 and replay.json() == accepted.json()
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        run = await db.get(Run, ids['run_id']); assert run.status == RunStatus.QUEUED and run.current_translation_revision_id == ids['translation_id']
        epoch = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == run.id, ExecutionEpoch.epoch == 11))).scalar_one()
        assert epoch.purpose == 'issue_assessment' and epoch.graph_version == 'translate_review@1'
        assert epoch.input_parse_artifact_ids == [str(ids['parse_id'])] and epoch.repair_request == ids['request']
        original = await db.get(ExecutionEpoch, ids['epoch_id']); assert original.model_bindings != epoch.model_bindings
        migration = (await db.execute(select(ModelMigration).where(ModelMigration.run_id == run.id))).scalar_one()
        assert migration.from_epoch == 10 and migration.to_epoch == 11 and migration.decided_by == principal.user_id
        assert migration.invalidated_steps == ['assess_issue'] and migration.old_binding_fingerprint != migration.new_binding_fingerprint
        assert (await db.get(rm.PreviewArtifact, ids['preview_id'])).status == 'valid'
        assert not (await db.get(em.ArtifactDependency, ids['dependency_id'])).stale
        assert (await db.get(ReviewConfirmation, ids['confirmation_id'])).translation_revision_id == ids['translation_id']
        assert (await db.get(ExportSnapshot, ids['snapshot_id'])).manifest == {'translation_revision_id': str(ids['translation_id'])}
        assert (await db.get(rm.Issue, ids['issues'][0])).guidance['agent_status'] == 'queued'


@pytest.mark.asyncio
@pytest.mark.parametrize('unsafe', ['old_pending', 'resolved', 'baseline', 'purpose', 'prompt'])
async def test_exported_local_migration_refuses_changed_or_unsettled_context(http_db, unsafe):
    http, conn, principal, created = http_db
    ids = await seed_failed_assessment(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        epoch = await db.get(ExecutionEpoch, ids['epoch_id'])
        if unsafe == 'old_pending':
            db.add(StepExecution(tenant_id=principal.tenant_id, run_id=ids['run_id'], epoch=9, step_name='old external request',
                input_fingerprint='6'*64, status=StepStatus.SUBMITTING))
        elif unsafe == 'resolved':
            db.add(rm.IssueDisposition(tenant_id=principal.tenant_id, case_id=UUID(ids['case_id']), issue_id=ids['issues'][0],
                business_revision_id=ids['revision_id'], action='ignore', reason='已由用户忽略', review_sequence=99, decided_by=principal.user_id))
        elif unsafe == 'baseline': epoch.input_business_revision_id = None
        elif unsafe == 'purpose': epoch.purpose = 'quality_preview'
        elif unsafe == 'prompt': epoch.repair_request = {**epoch.repair_request, 'prompt': {**epoch.repair_request['prompt'], 'sha256': '0'*64}}
    url = f"/api/v1/ops/runs/{ids['run_id']}"
    options = (await http.get(url + '/model-migration-options')).json(); assert not options['safe_now']
    response = await http.post(url + '/migrate-model', headers={'Idempotency-Key': uuid4().hex},
        json={'candidate_config_fingerprint': options['options'][0]['fingerprint'], 'expected_execution_epoch': 10, 'reason': '拒绝不安全状态'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'MODEL_MIGRATION_UNSAFE'


@pytest.mark.parametrize('change', [
    {'usage': {'model_calls': [{'outcome': 'not_sent'}]}}, {'usage': {'calls': [{'outcome': 'succeeded', 'is_synthetic': True}]}},
    {'usage': {'model_attempt': {'state': 'finished'}}}, {'usage': {'model_response': {'call_ids': ['call']}}},
    {'usage': []}, {'usage': {'model_calls': ''}},
    {'external_task_id': 'task'}, {'artifact_ids': [str(uuid4())]}, {'finished_at': None}, {'error_class': 'transport_error'},
])
def test_empty_model_ledger_is_not_sufficient_for_local_migration(change):
    from app.ops.service import assessment_binding_failed_without_send
    issue_id = uuid4()
    step = SimpleNamespace(status=StepStatus.FAILED, error_class='binding_mismatch', finished_at=datetime.now(UTC),
        step_name='疑点材料分析:' + str(issue_id), external_task_id=None, artifact_ids=[], usage={})
    assert assessment_binding_failed_without_send(step, issue_id)
    for key, value in change.items(): setattr(step, key, value)
    assert not assessment_binding_failed_without_send(step, issue_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['model_name', 'adapter_version'])
async def test_recommendation_binding_mismatch_is_rejected_before_enqueue(http_db, changed):
    http, conn, principal, created = http_db
    ids = await seed_failed_assessment(conn, principal, created)
    if changed == 'adapter_version':
        bindings = await current_model_bindings('extract', 'translate')
        async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
            epoch = await db.get(ExecutionEpoch, ids['epoch_id'])
            epoch.model_bindings = [{**binding, 'adapter_version': 'previous-adapter'} for binding in bindings]
    response = await http.post(f"/api/v1/cases/{ids['case_id']}/issues/{ids['issues'][0]}/apply-recommendation",
        headers={'Idempotency-Key': uuid4().hex}, json={'expected_business_revision_id': str(ids['revision_id']),
            'expected_translation_revision_id': str(ids['translation_id']), 'expected_issue_version': 2})
    assert response.status_code == 409 and response.json()['error']['code'] == 'MODEL_MIGRATION_REQUIRED', response.text
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        assert (await db.get(Run, ids['run_id'])).current_epoch == 10
        assert (await db.get(rm.Issue, ids['issues'][0])).guidance['agent_request_no'] == 1
        assert not (await db.execute(select(QueueOutbox).where(QueueOutbox.dedupe_key == f"run:{ids['run_id']}:11"))).scalars().all()
