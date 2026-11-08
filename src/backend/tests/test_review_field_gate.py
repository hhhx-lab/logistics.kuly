"""G05：生产HTTP与真实PG，整个样本在外层事务回滚；无外部调用。"""
from datetime import UTC, datetime, timedelta
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import evidence_models as em
from app.documents.models import Case, DocumentVersion, StoredObject, StoredObjectCategory
from app.exports.models import ExportArtifact, ExportAttempt, ExportAttemptItem, ExportSnapshot, ReviewConfirmation
from app.review import models as rm
from app.workflows.models import ExecutionEpoch, Run, RunDocument, RunStatus, StepExecution, StepStatus, TaskDraft, RunEvent
from tests.test_workflow_response_contracts import http_db, seed_source


async def current_model_bindings(*roles):
    from app.config import get_settings, ModelRole
    from app.infra.model_gateway import DefaultModelGateway
    gateway = DefaultModelGateway(get_settings())
    try:
        return [gateway.binding_for(ModelRole(role)).model_dump(mode='json') for role in roles]
    finally:
        await gateway.aclose()


async def seed_fields(conn, p, created):
    case_id, document_id = await seed_source(conn, p, created)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            parse=em.ParseArtifact(tenant_id=p.tenant_id,case_id=UUID(case_id),document_id=UUID(document_id),document_version=1,parser_name='fixture',parser_version='1',parser_config_hash='0'*64,quality=em.ParseQuality.VALID)
            db.add(parse);await db.flush()
            logical=em.LogicalDocument(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,ordinal=0,type_candidate=em.LogicalDocumentType.COMMERCIAL_INVOICE)
            db.add(logical);await db.flush()
            section=em.DocumentSection(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,logical_document_id=logical.id,ordinal=0,kind='body')
            db.add(section);await db.flush()
            source=em.SourceSegment(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,logical_document_id=logical.id,section_id=section.id,ordinal=0,text='Invoice 001',structure='paragraph')
            db.add(source);await db.flush()
            fields=[]
            for index in range(2):
                field=em.FieldAssertion(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,logical_document_id=logical.id,section_id=section.id,field_key='invoice_no' if not index else 'goods_description',group='logistics',source_text='001',candidate_value='001',source_segment_ids=[str(source.id)] if not index else [])
                db.add(field);fields.append(field)
            revision=em.BusinessRevision(tenant_id=p.tenant_id,case_id=UUID(case_id),revision_no=1,reason='baseline_extraction',created_by=p.user_id)
            db.add(revision);await db.flush()
            for field in fields:db.add(em.BusinessRevisionField(tenant_id=p.tenant_id,business_revision_id=revision.id,field_assertion_id=field.id))
            case=await db.get(Case,UUID(case_id));case.current_business_revision_id=revision.id;case.business_revision_no=1
            draft=TaskDraft(tenant_id=p.tenant_id,case_id=case.id,created_by=p.user_id,workflow_type='translate_review',workflow_version=1,source_versions=[],draft_parameters={},status='ready',missing_fields=[],questions=[],task_summary='test',expires_at=datetime.now(UTC)+timedelta(hours=1))
            db.add(draft);await db.flush()
            run=Run(draft_task_id=draft.id,tenant_id=p.tenant_id,case_id=case.id,workflow_type='translate_review',workflow_version=1,task_spec={'rule_pack_version':'core-document@3','target_language':'en','translation_scope':'all','output_formats':['docx','pdf'],'source_versions':[{'document_id':document_id,'document_version':1}]},task_spec_hash='0'*64,prompt_versions={},initiated_by=p.user_id,target_language='en',status=RunStatus.AWAITING_REVIEW,current_stage='待复核',current_business_revision_id=revision.id)
            db.add(run);await db.flush();db.add(RunDocument(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,ordinal=0,document_id=UUID(document_id),document_version=1))
            issues=[]
            for index in range(2):
                issue=rm.Issue(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=revision.id,category='conflict' if not index else 'undetermined',severity='red' if not index else 'undetermined',blocks_export=not index,title='红色冲突' if not index else '仍无法判断',description='需有依据确认',sources=[{'target':{'case_id':case_id,'document_id':document_id,'document_version':1,'parse_artifact_id':str(parse.id),'logical_document_id':str(logical.id),'source_segment_id':str(source.id),'field_id':str(fields[0].id)},'label':'原文','value':'001'}])
                db.add(issue);issues.append(issue)
            await db.flush()
            return {'case_id':case_id,'document_id':document_id,'parse_id':parse.id,'logical_id':logical.id,'source_id':source.id,'section_id':section.id,'revision_id':revision.id,'run_id':run.id,'field_id':fields[0].id,'no_source_id':fields[1].id,'issues':[issue.id for issue in issues]}


@pytest.mark.asyncio
async def test_paused_legacy_retry_reads_own_frozen_parse_without_latest_fallback(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        run = await db.get(Run, ids['run_id'])
        run.current_epoch = 3
        run.status = RunStatus.PAUSED_RECONCILE
        section = await db.get(em.DocumentSection, ids['section_id'])
        section.detected_language = 'fr'
        section.language_uncertain = False
        db.add(em.ParseArtifact(tenant_id=p.tenant_id, case_id=run.case_id, document_id=UUID(ids['document_id']),
            document_version=1, parser_name='later-other-run', parser_version='1', parser_config_hash='1'*64,
            quality=em.ParseQuality.VALID))
        for epoch in (1, 2, 3):
            db.add(ExecutionEpoch(tenant_id=p.tenant_id, run_id=run.id, epoch=epoch, purpose='initial' if epoch == 1 else 'retry',
                graph_version='test@1', input_parse_artifact_ids=[str(ids['parse_id'])] if epoch == 1 else []))
        db.add(StepExecution(tenant_id=p.tenant_id, run_id=run.id, epoch=3, step_name='抽取:pending',
            input_fingerprint='0'*64, status=StepStatus.NEEDS_RECONCILE))
    response = await http.get(f"/api/v1/cases/{ids['case_id']}/review-view", params={'run_id':str(ids['run_id'])})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view['mode'] == 'working' and view['allowed_actions'] == ['open_source']
    assert view['files'][0]['parse_artifact_id'] == str(ids['parse_id'])
    assert view['files'][0]['logical_documents'][0]['logical_document_id'] == str(ids['logical_id'])
    assert view['files'][0]['source']['detected_language'] == 'fr'
    assert view['files'][0]['source']['language_uncertain'] is False


def test_single_supported_language_is_not_mixed():
    from types import SimpleNamespace
    from app.api.routers.review import _source_language
    for language in ('zh-CN', 'en', 'fr', 'es', 'ru'):
        assert _source_language([SimpleNamespace(id=uuid4(), detected_language=language, language_uncertain=False)], []) == {
            'detected_language':language, 'language_uncertain':False}


@pytest.mark.asyncio
@pytest.mark.parametrize('include_source_pdf', [True, False])
async def test_only_open_red_blocks_confirmation_and_export(http_db, include_source_pdf):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        db.add(em.ParseArtifact(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),document_id=UUID(ids['document_id']),
            document_version=1,parser_name='unadopted',parser_version='2',parser_config_hash='2'*64,
            quality=em.ParseQuality.VALID,is_synthetic=False,page_count=1))
        scope = rm.ResolvedTranslationScope(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
            document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'],
            business_revision_id=ids['revision_id'], selector_version='all@1', source_section_ids=[str(ids['section_id'])],
            source_segment_ids=[str(ids['source_id'])], fingerprint='0'*64)
        segment = rm.TranslationSegment(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
            document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'], section_id=ids['section_id'],
            target_language='en', ordinal=0, source_segment_ids=[str(ids['source_id'])], translated_text='Invoice 001',
            state='translated', dependency_fingerprint='0'*64)
        db.add_all([scope, segment]); await db.flush()
        revision = rm.TranslationRevision(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
            business_revision_id=ids['revision_id'], target_language='en', revision_no=1,
            parse_artifact_ids=[str(ids['parse_id'])], resolved_scope_ids=[str(scope.id)], segment_ids=[str(segment.id)],
            prompt_versions={}, model_bindings=[], qa_status='passed', qa_summary={'semantic_status':'evaluated_candidates_only', 'expected_source_ids':[str(ids['source_id'])],
                'actual_source_ids':[str(ids['source_id'])]}, dependency_fingerprint='0'*64)
        db.add(revision); await db.flush()
        run = await db.get(Run, ids['run_id']); run.current_translation_revision_id = revision.id
        db.add(rm.PreviewArtifact(tenant_id=p.tenant_id, case_id=run.case_id, run_id=run.id,
            document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'],
            business_revision_id=ids['revision_id'], translation_revision_id=revision.id, resolved_scope_id=scope.id,
            target_language='en', preview_version=1, render_config_version='fixture@1', status='valid',
            reading_html='<p>Invoice 001</p>', segment_mappings=[{'translation_segment_id':str(segment.id)}]))
        db.add(rm.RuleCheck(tenant_id=p.tenant_id, case_id=run.case_id, run_id=run.id, business_revision_id=ids['revision_id'],
            rule_id='fixture', rule_pack_version='core-document@3', status='not_covered', explanation='未覆盖专项规则', input_fingerprint='0'*64))
        db.add(rm.Issue(tenant_id=p.tenant_id, case_id=run.case_id, run_id=run.id, business_revision_id=ids['revision_id'],
            category='quality', severity='yellow', blocks_export=True, title='非阻断黄色提示', description='无需逐条接受才导出', sources=[]))
        translation_id = revision.id
    base = f"/api/v1/runs/{ids['run_id']}"
    blocked = (await http.get(base+'/review-checklist')).json()
    assert not blocked['confirmable'] and [gap['item'] for gap in blocked['gaps']] == ['红色冲突']
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        case = await db.get(Case, UUID(ids['case_id'])); case.review_state_version += 1
        db.add(rm.IssueDisposition(tenant_id=p.tenant_id, case_id=case.id, issue_id=ids['issues'][0],
            business_revision_id=ids['revision_id'], review_sequence=case.review_state_version, action='revise',
            reason='已完成对应修订', decided_by=p.user_id))
    cl = (await http.get(base+'/review-checklist')).json()
    assert cl['confirmable'] and cl['gaps'] == []
    assert any(item['label']=='候选字段提示' and item['state']=='note' for item in cl['items'])
    assert not any(item['label']=='红色冲突' for item in cl['items'])
    pending = (await http.get(f"/api/v1/cases/{ids['case_id']}/issues", params={'run_id':str(ids['run_id']), 'severity':'open'})).json()
    assert pending['counts']['done']==1 and pending['counts']['blocking']==0 and len(pending['items'])==2
    assert all(item['status'] in {'open','unresolved'} and not item['blocking_delivery'] for item in pending['items'])
    fields = (await http.get(f"/api/v1/cases/{ids['case_id']}/fields")).json()['items']
    assert all(link['resolved'] for field in fields for link in field['issues'] if link['issue_id']==str(ids['issues'][0]))
    body = {'expected_review_fingerprint':cl['review_fingerprint'], 'expected_business_revision_id':str(ids['revision_id']),
        'expected_translation_revision_id':str(translation_id)}
    stale = await http.post(base+'/review-confirmations', headers={'Idempotency-Key':uuid4().hex}, json={**body,'expected_review_fingerprint':blocked['review_fingerprint']})
    assert stale.status_code==409 and stale.json()['error']['code']=='REVIEW_STALE'
    confirmed = await http.post(base+'/review-confirmations', headers={'Idempotency-Key':uuid4().hex}, json=body)
    assert confirmed.status_code==200, confirmed.text
    cl = (await http.get(base+'/review-checklist')).json(); assert cl['confirmation_status']=='valid'
    export = await http.post(base+'/exports', headers={'Idempotency-Key':uuid4().hex},
        json={**body,'confirmation_id':confirmed.json()['confirmation_id'],'include_source_pdf':include_source_pdf})
    assert export.status_code==200 and export.json()['status']=='queued', export.text
    steps=export.json()['artifact_steps']
    assert {step['kind'] for step in steps} == ({'docx','pdf','source_pdf'} if include_source_pdf else {'docx','pdf'})
    assert all('_en_译本.' in step['label'] for step in steps if step['kind'] in {'docx','pdf'})
    repeated=await http.post(base+'/exports',headers={'Idempotency-Key':uuid4().hex},
        json={**body,'confirmation_id':confirmed.json()['confirmation_id'],'include_source_pdf':include_source_pdf})
    assert repeated.status_code==200 and repeated.json()['attempt_id']==export.json()['attempt_id']
    different=await http.post(base+'/exports',headers={'Idempotency-Key':uuid4().hex},
        json={**body,'confirmation_id':confirmed.json()['confirmation_id'],'include_source_pdf':not include_source_pdf})
    assert different.status_code==409 and different.json()['error']['code']=='EXPORT_ATTEMPT_RETRY_REQUIRED'
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db, db.begin():
        attempt=await db.get(ExportAttempt,UUID(export.json()['attempt_id']));attempt.status='failed_retryable'
        run=await db.get(Run,ids['run_id']);run.status=RunStatus.READY_TO_EXPORT
        db.add(ExportAttemptItem(tenant_id=p.tenant_id,case_id=run.case_id,attempt_id=attempt.id,
            ordinal=99,kind='report',filename='旧失败报告.pdf',status='failed'))
    retry=await http.post(base+f"/export-attempts/{export.json()['attempt_id']}/retry",
        headers={'Idempotency-Key':uuid4().hex},json=body)
    assert retry.status_code==200, retry.text
    assert retry.json()['attempt_id']==export.json()['attempt_id']
    assert {item['kind'] for item in retry.json()['artifact_steps']}=={'docx','pdf','source_pdf'}


@pytest.mark.asyncio
async def test_field_confirm_refuses_pending_red_unknown_and_no_source_then_adjudicated(http_db):
    http, conn, p, created=http_db
    ids=await seed_fields(conn,p,created)
    base=f"/api/v1/cases/{ids['case_id']}"
    body={'expected_business_revision_id':str(ids['revision_id']),'expected_parse_artifact_id':str(ids['parse_id'])}
    first=await http.get(base+'/fields',params={'limit':1});assert first.status_code==200,first.text
    full=(await http.get(base+'/fields')).json();field=next(row for row in full['items'] if row['field_id']==str(ids['field_id']))
    assert len(field['issues'])==2 and not any(issue['resolved'] for issue in field['issues'])
    blocked=await http.post(base+f"/fields/{ids['field_id']}/confirm",headers={'Idempotency-Key':uuid4().hex},json=body)
    assert blocked.status_code==409 and blocked.json()['error']['code']=='FIELD_ISSUE_OPEN'
    no_source=await http.post(base+f"/fields/{ids['no_source_id']}/confirm",headers={'Idempotency-Key':uuid4().hex},json=body)
    assert no_source.status_code==409 and no_source.json()['error']['code']=='FIELD_SOURCE_REQUIRED'
    page=(await http.get(base+'/issues',params={'limit':1})).json()
    assert page['counts']=={'all':2,'blocking':1,'warning':0,'undetermined':1,'done':0} and page['has_more']
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            case=await db.get(Case,UUID(ids['case_id']))
            case.review_state_version+=1
            db.add(rm.IssueDisposition(tenant_id=p.tenant_id,case_id=case.id,issue_id=ids['issues'][0],business_revision_id=ids['revision_id'],review_sequence=case.review_state_version,action='adjudicate',reason='已核验合同',basis='可核验外部记录（回滚测试）',decided_by=p.user_id))
    still_unknown=await http.post(base+f"/fields/{ids['field_id']}/confirm",headers={'Idempotency-Key':uuid4().hex},json=body)
    assert still_unknown.status_code==200, still_unknown.text
    remaining=(await http.get(base+'/fields')).json()
    row=next(item for item in remaining['items'] if item['field_id']==str(ids['field_id']))
    assert row['issues'] and not any(item['blocks_confirmation'] for item in row['issues'])
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            case=await db.get(Case,UUID(ids['case_id']));case.review_state_version+=1
            db.add(rm.IssueDisposition(tenant_id=p.tenant_id,case_id=case.id,issue_id=ids['issues'][1],business_revision_id=ids['revision_id'],review_sequence=case.review_state_version,action='adjudicate',reason='补齐不确定条件',basis='可核验运输记录（回滚测试）',decided_by=p.user_id))
    confirmed=await http.post(base+f"/fields/{ids['field_id']}/confirm",headers={'Idempotency-Key':uuid4().hex},json=body)
    assert confirmed.status_code==200,confirmed.text
    full=(await http.get(base+'/fields')).json();field=next(row for row in full['items'] if row['field_id']==str(ids['field_id']))
    assert field['review_status']=='confirmed' and field['issues']==[]
    assert any(event['action']=='confirmed' for event in field['history'])
    stale=await http.get(base+'/fields',params={'limit':1,'cursor':first.json()['next_cursor']});assert stale.status_code==409
    done=(await http.get(base+'/issues',params={'limit':1,'severity':'done'})).json()
    assert done['counts']['done']==2 and done['counts']['blocking']==done['counts']['undetermined']==0 and len(done['items'])==1


@pytest.mark.asyncio
async def test_snapshot_fields_and_risk_do_not_use_later_decisions_or_history(http_db):
    http,conn,p,created=http_db
    ids=await seed_fields(conn,p,created)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            case=await db.get(Case,UUID(ids['case_id']))
            now=datetime.now(UTC)
            case.review_state_version+=1
            event=em.FieldReviewEvent(tenant_id=p.tenant_id,case_id=case.id,field_assertion_id=ids['field_id'],business_revision_id=ids['revision_id'],review_sequence=case.review_state_version,action='undetermined',reason='快照时仍待核对',decided_by=p.user_id,created_at=now-timedelta(seconds=4))
            db.add(event);await db.flush()
            confirmation=ReviewConfirmation(tenant_id=p.tenant_id,case_id=case.id,run_id=ids['run_id'],task_spec_hash='0'*64,review_fingerprint='0'*64,business_revision_id=ids['revision_id'],translation_revision_id=uuid4(),pairing_manifest={},review_manifest={},output_formats=[],confirmed_by=p.user_id)
            # 为隔离读取验证创建最小历史链；不是正式交付成功验证。
            translation=rm.TranslationRevision(id=confirmation.translation_revision_id,tenant_id=p.tenant_id,case_id=case.id,run_id=ids['run_id'],business_revision_id=ids['revision_id'],target_language='en',revision_no=1,parse_artifact_ids=[str(ids['parse_id'])],resolved_scope_ids=[],segment_ids=[],qa_status='passed')
            old_segment=rm.TranslationSegment(tenant_id=p.tenant_id,case_id=case.id,run_id=ids['run_id'],document_id=UUID(ids['document_id']),document_version=1,
                parse_artifact_id=ids['parse_id'],section_id=ids['section_id'],target_language='en',ordinal=0,source_segment_ids=[str(ids['source_id'])],
                translated_text='Frozen invoice',state='translated',dependency_fingerprint='0'*64)
            db.add(old_segment);await db.flush()
            await db.execute(text("INSERT INTO translation_revisions (id,tenant_id,case_id,run_id,business_revision_id,target_language,revision_no,parse_artifact_ids,resolved_scope_ids,segment_ids,qa_status,qa_summary,dependency_fingerprint,is_synthetic,prompt_versions,model_bindings) VALUES (:id,:tenant,:case,:run,:revision,'en',1,CAST(:parses AS jsonb),'[]'::jsonb,CAST(:segments AS jsonb),'passed','{}'::jsonb,:fingerprint,false,'{}'::jsonb,'[]'::jsonb)"), {'id':translation.id,'tenant':p.tenant_id,'case':case.id,'run':ids['run_id'],'revision':ids['revision_id'],'fingerprint':'0'*64,'parses':json.dumps([str(ids['parse_id'])]),'segments':json.dumps([str(old_segment.id)])})
            db.add(confirmation);await db.flush()
            attempt=ExportAttempt(tenant_id=p.tenant_id,case_id=case.id,run_id=ids['run_id'],confirmation_id=confirmation.id,review_fingerprint='0'*64,status='succeeded',execution_epoch=1,created_by=p.user_id)
            db.add(attempt);await db.flush()
            version=(await db.execute(select(DocumentVersion).where(DocumentVersion.document_id==UUID(ids['document_id'])))).scalar_one()
            frozen_source={'case_id':str(case.id),'document_id':ids['document_id'],'document_version':1,
                'filename':version.filename,'mime':version.mime,'size_bytes':version.size_bytes,'uploaded_by':str(version.uploaded_by),
                'uploaded_at':version.created_at.isoformat(),'parse_artifact_id':str(ids['parse_id'])}
            old_manifest={'business_revision_id':str(ids['revision_id']),'field_assertion_ids':[str(ids['field_id'])],'field_review_event_ids':[str(event.id)],
                'issue_ids':[str(value) for value in ids['issues']],'issue_disposition_ids':[],'rule_check_ids':[],
                'translation_revision_id':str(translation.id),'business_revision_no':1,'translation_revision_no':1,
                'source_versions':[frozen_source],'target_language':'en','translation_scope':'all','rule_pack_version':'core-document@2',
                'pairings':[{'document_id':ids['document_id'],'preview_id':None}]}
            assert 'rule_input_decision_ids' not in old_manifest
            snapshot=ExportSnapshot(tenant_id=p.tenant_id,case_id=case.id,run_id=ids['run_id'],confirmation_id=confirmation.id,attempt_id=attempt.id,exported_by=p.user_id,
                manifest=old_manifest,created_at=now-timedelta(seconds=2))
            db.add(snapshot);await db.flush();snapshot_id=snapshot.id
            report_object=StoredObject(tenant_id=p.tenant_id,case_id=case.id,object_key=f'old-snapshot-fixture/{uuid4()}',category=StoredObjectCategory.EXPORT)
            db.add(report_object);await db.flush()
            db.add(ExportArtifact(tenant_id=p.tenant_id,case_id=case.id,snapshot_id=snapshot.id,kind='report',object_id=report_object.id,
                filename='历史报告.pdf',size_bytes=128,sha256='0'*64))
            for issue_id in ids['issues']:
                case.review_state_version+=1
                db.add(rm.IssueDisposition(tenant_id=p.tenant_id,case_id=case.id,issue_id=issue_id,business_revision_id=ids['revision_id'],review_sequence=case.review_state_version,action='adjudicate',reason='快照后才补依据',basis='后续依据',decided_by=p.user_id,created_at=now))
            case.review_state_version+=1
            db.add(em.FieldReviewEvent(tenant_id=p.tenant_id,case_id=case.id,field_assertion_id=ids['field_id'],business_revision_id=ids['revision_id'],review_sequence=case.review_state_version,action='confirmed',reason='快照后审核',decided_by=p.user_id,created_at=now))
    base=f"/api/v1/cases/{ids['case_id']}"
    current=(await http.get(base+'/fields')).json();field=next(row for row in current['items'] if row['field_id']==str(ids['field_id']))
    assert field['review_status']=='confirmed' and field['issues']==[]
    old=await http.get(base+'/fields',params={'snapshot_id':str(snapshot_id)})
    assert old.status_code==200,old.text
    field=old.json()['items'][0]
    assert field['review_status']=='unknown' and not any(issue['resolved'] for issue in field['issues'])
    assert [event['reason'] for event in field['history']]==['快照时仍待核对']
    source_fields = await http.get(base+'/fields', params={'snapshot_id':str(snapshot_id), 'run_id':str(ids['run_id']),
        'document_id':ids['document_id'], 'parse_artifact_id':str(ids['parse_id']), 'source_segment_id':str(ids['source_id'])})
    assert source_fields.status_code == 200, source_fields.text
    assert [item['field_id'] for item in source_fields.json()['items']] == [str(ids['field_id'])]
    assert source_fields.json()['items'][0]['target']['export_snapshot_id'] == str(snapshot_id)
    old_translations = await http.get(f"/api/v1/runs/{ids['run_id']}/translations", params={'snapshot_id':str(snapshot_id),
        'document_id':ids['document_id'], 'parse_artifact_id':str(ids['parse_id']), 'source_segment_id':str(ids['source_id'])})
    assert old_translations.status_code == 200, old_translations.text
    assert old_translations.json()['items'][0]['translated_text'] == 'Frozen invoice'
    assert old_translations.json()['items'][0]['target']['export_snapshot_id'] == str(snapshot_id)
    risks=await http.get(base+'/issues',params={'snapshot_id':str(snapshot_id),'limit':1})
    assert risks.status_code==200,risks.text
    assert risks.json()['counts']['blocking']==1 and risks.json()['counts']['done']==0
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            db.add(rm.RuleInputDecision(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), business_revision_id=ids['revision_id'], kind='party_mapping', scope_key='later-only', payload={
                'members':[{'logical_document_id':str(ids['logical_id']),'role':'seller'},{'logical_document_id':str(ids['logical_id']),'role':'shipper'}],
                'compare_fields':['name'],'field_assertion_ids':[str(ids['field_id'])],'source_segment_ids':[]
            }, basis='后续依据', reason='快照后记录', decided_by=p.user_id))
    latest_inputs = await http.get(base+'/rule-inputs',params={'business_revision_id':str(ids['revision_id'])})
    frozen_inputs = await http.get(base+'/rule-inputs',params={'snapshot_id':str(snapshot_id)})
    assert latest_inputs.status_code == frozen_inputs.status_code == 200
    assert len(latest_inputs.json()['items']) == 1 and frozen_inputs.json()['items'] == []
    review=await http.get(base+'/review-view',params={'snapshot_id':str(snapshot_id)})
    assert review.status_code==200,review.text
    assert review.json()['mode']=='historical' and review.json()['review_state_version'] is None
    detail=await http.get(f'/api/v1/snapshots/{snapshot_id}')
    assert detail.status_code==200,detail.text
    assert detail.json()['rule_pack_version']=='core-document@2' and detail.json()['business_revision_no']==1
    listing=await http.get(f"/api/v1/runs/{ids['run_id']}/snapshots")
    assert listing.status_code==200,listing.text
    assert listing.json()['items'][0]['snapshot_id']==str(snapshot_id)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        snapshot=await db.get(ExportSnapshot,snapshot_id)
        assert snapshot.manifest==old_manifest and 'rule_input_decision_ids' not in snapshot.manifest


@pytest.mark.asyncio
async def test_run_fields_read_uses_selected_business_not_new_case_pointer(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            case = await db.get(Case, UUID(ids['case_id']))
            db.add(RunEvent(tenant_id=p.tenant_id, run_id=ids['run_id'], seq=1, event_type='parse_published', status='awaiting_review', message='发布测试解析', payload={'parse_artifact_ids':[str(ids['parse_id'])]}))
            newer = em.BusinessRevision(tenant_id=p.tenant_id, case_id=case.id, revision_no=2,
                parent_revision_id=ids['revision_id'], reason='other_run_revision', created_by=p.user_id)
            db.add(newer); await db.flush()
            case.current_business_revision_id = newer.id; case.business_revision_no = 2
    base = f"/api/v1/cases/{ids['case_id']}"
    current = await http.get(base + '/fields')
    assert current.status_code == 200 and current.json()['items'] == []
    selected = await http.get(base + '/fields', params={'run_id': str(ids['run_id']), 'business_revision_id': str(ids['revision_id'])})
    assert selected.status_code == 200, selected.text
    assert {item['field_id'] for item in selected.json()['items']} == {str(ids['field_id']), str(ids['no_source_id'])}
    assert all(item['target']['business_revision_id'] == str(ids['revision_id']) for item in selected.json()['items'])
    issues = await http.get(base + '/issues', params={'run_id': str(ids['run_id']), 'business_revision_id': str(ids['revision_id'])})
    assert issues.status_code == 200 and issues.json()['counts']['all'] == 2


@pytest.mark.asyncio
async def test_source_box_fields_include_secondary_source_and_all_cursor_pages(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        secondary_section=em.DocumentSection(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), parse_artifact_id=ids['parse_id'],
            logical_document_id=ids['logical_id'], ordinal=1, kind='body')
        db.add(secondary_section);await db.flush()
        secondary = em.SourceSegment(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), parse_artifact_id=ids['parse_id'],
            logical_document_id=ids['logical_id'], section_id=secondary_section.id, ordinal=1, text='Shared source', structure='paragraph')
        db.add(secondary); await db.flush()
        anchor = em.SourceAnchor(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), parse_artifact_id=ids['parse_id'],
            source_segment_id=secondary.id, precision=em.AnchorPrecision.RECT, page_no=2, bbox=[20, 30, 150, 80], coord_system='pdf_points')
        db.add(anchor)
        # 默认第一页在这些字段之前结束；每个匹配字段把点击源段放在第二位。
        selected = []
        for index in range(265):
            sources = [str(ids['source_id'])] + ([str(secondary.id)] if index >= 60 else [])
            field = em.FieldAssertion(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), parse_artifact_id=ids['parse_id'],
                logical_document_id=ids['logical_id'], section_id=ids['section_id'], field_key=f'box_{index:03}', group='goods',
                source_text='Shared source', candidate_value=str(index), source_segment_ids=sources)
            db.add(field); await db.flush()
            db.add(em.BusinessRevisionField(tenant_id=p.tenant_id, business_revision_id=ids['revision_id'], field_assertion_id=field.id))
            if index >= 60: selected.append(str(field.id))
        db.add(RunEvent(tenant_id=p.tenant_id, run_id=ids['run_id'], seq=1, event_type='parse_published', status='awaiting_review',
            message='发布测试解析', payload={'parse_artifact_ids':[str(ids['parse_id'])]}))
        secondary_id, anchor_id = secondary.id, anchor.id
    base = f"/api/v1/cases/{ids['case_id']}/fields"
    common = {'run_id':str(ids['run_id']), 'business_revision_id':str(ids['revision_id']),
        'document_id':ids['document_id'], 'parse_artifact_id':str(ids['parse_id'])}
    first = await http.get(base, params={**common, 'limit':50})
    assert first.status_code == 200 and not set(selected).intersection(item['field_id'] for item in first.json()['items'])
    exact = {**common, 'source_segment_id':str(secondary_id), 'source_anchor_id':str(anchor_id), 'limit':200}
    page = await http.get(base, params=exact)
    assert page.status_code == 200, page.text
    assert page.json()['has_more'] and len(page.json()['items']) == 200
    second = await http.get(base, params={**exact, 'cursor':page.json()['next_cursor']})
    assert second.status_code == 200, second.text
    items = page.json()['items'] + second.json()['items']
    assert not second.json()['has_more'] and len(items) == len(selected) == 205
    assert {item['field_id'] for item in items} == set(selected)
    assert all(item['source_segment_ids'] == [str(ids['source_id']), str(secondary_id)] for item in items)
    assert all(item['target']['source_segment_id'] == str(secondary_id) and item['target']['source_anchor_id'] == str(anchor_id) for item in items)
    resolved=await http.post('/api/v1/evidence/resolve',json=items[0]['target'])
    assert resolved.status_code==200,resolved.text
    assert resolved.json()['normalized_target']['source_segment_id']==str(secondary_id) and resolved.json()['page_no']==2
    anchor_only = await http.get(base, params={**common, 'source_anchor_id':str(anchor_id), 'limit':1})
    assert anchor_only.status_code == 200 and anchor_only.json()['items'][0]['target']['source_segment_id'] == str(secondary_id)
    mismatch = await http.get(base, params={**exact, 'source_segment_id':str(ids['source_id'])})
    assert mismatch.status_code == 404
    changed_cursor = await http.get(base, params={**common, 'source_segment_id':str(ids['source_id']), 'cursor':page.json()['next_cursor']})
    assert changed_cursor.status_code == 422 and changed_cursor.json()['error']['code'] == 'INVALID_REQUEST'
    unknown = await http.get(base, params={**common, 'source_segment_id':str(uuid4())})
    assert unknown.status_code == 404
    working = await http.get(base, params={**exact, 'review_state_version':page.json()['review_state_version']})
    assert working.status_code == 200
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        case=await db.get(Case,UUID(ids['case_id']));case.review_state_version+=1
    stale = await http.get(base, params={**exact, 'review_state_version':page.json()['review_state_version'], 'cursor':working.json()['next_cursor']})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'PAGE_STALE'
    other = await seed_fields(conn, p, created)
    cross_case = await http.get(base, params={**common, 'source_segment_id':str(other['source_id'])})
    cross_document = await http.get(base, params={**exact, 'document_id':other['document_id']})
    assert cross_case.status_code == cross_document.status_code == 404


@pytest.mark.asyncio
async def test_unpublished_run_fields_keep_own_parse_after_newer_parse_same_source(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        run = await db.get(Run, ids['run_id'])
        run.current_business_revision_id = None; run.status = RunStatus.PAUSED_RECONCILE
        db.add(ExecutionEpoch(tenant_id=p.tenant_id, run_id=run.id, epoch=run.current_epoch, purpose='initial', graph_version='test', input_parse_artifact_ids=[]))
        db.add(StepExecution(tenant_id=p.tenant_id, run_id=run.id, epoch=run.current_epoch, step_name='解析:invoice',
            input_fingerprint='1'*64, status=StepStatus.SUCCEEDED, artifact_ids=[str(ids['parse_id'])]))
        newer_parse = em.ParseArtifact(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), document_id=UUID(ids['document_id']),
            document_version=1, parser_name='fixture', parser_version='later', parser_config_hash='2'*64, quality=em.ParseQuality.VALID,
            created_at=datetime.now(UTC)+timedelta(seconds=2))
        db.add(newer_parse); await db.flush()
        newer_business = em.BusinessRevision(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), revision_no=2,
            parent_revision_id=ids['revision_id'], reason='other_run_revision', created_by=p.user_id)
        db.add(newer_business); await db.flush()
        case = await db.get(Case, UUID(ids['case_id'])); case.current_business_revision_id=newer_business.id; case.business_revision_no=2
    selected = await http.get(f"/api/v1/cases/{ids['case_id']}/fields", params={'run_id':str(ids['run_id'])})
    assert selected.status_code == 200, selected.text
    assert {item['field_id'] for item in selected.json()['items']} == {str(ids['field_id']), str(ids['no_source_id'])}
    assert all(item['target']['parse_artifact_id'] == str(ids['parse_id']) for item in selected.json()['items'])
    assert all(item['target']['business_revision_id'] is None and item['issues'] == [] for item in selected.json()['items'])
    source = await http.get(f"/api/v1/cases/{ids['case_id']}/fields", params={'run_id':str(ids['run_id']),
        'source_segment_id':str(ids['source_id']), 'parse_artifact_id':str(ids['parse_id'])})
    assert source.status_code == 200 and [item['field_id'] for item in source.json()['items']] == [str(ids['field_id'])]


@pytest.mark.asyncio
async def test_source_translation_lookup_finds_later_page_secondary_source_and_binds_cursor(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        secondary_section=em.DocumentSection(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), parse_artifact_id=ids['parse_id'],
            logical_document_id=ids['logical_id'], ordinal=1, kind='body')
        db.add(secondary_section);await db.flush()
        secondary = em.SourceSegment(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), parse_artifact_id=ids['parse_id'],
            logical_document_id=ids['logical_id'], section_id=secondary_section.id, ordinal=1, text='Second source', structure='paragraph')
        db.add(secondary); await db.flush()
        anchor = em.SourceAnchor(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), parse_artifact_id=ids['parse_id'],
            source_segment_id=secondary.id, precision=em.AnchorPrecision.RECT, page_no=2, bbox=[20, 30, 150, 80], coord_system='pdf_points')
        db.add(anchor); await db.flush()
        segments = []
        for index in range(62):
            segment = rm.TranslationSegment(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
                document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'], section_id=ids['section_id'],
                target_language='en', ordinal=index, source_segment_ids=[str(ids['source_id'])]+([str(secondary.id)] if index>=60 else []),
                translated_text=f'Translation {index}', state='translated', dependency_fingerprint='0'*64)
            db.add(segment); await db.flush(); segments.append(str(segment.id))
        revision = rm.TranslationRevision(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
            business_revision_id=ids['revision_id'], target_language='en', revision_no=1, parse_artifact_ids=[str(ids['parse_id'])],
            resolved_scope_ids=[], segment_ids=segments, prompt_versions={}, model_bindings=[], qa_status='passed', dependency_fingerprint='0'*64)
        db.add(revision); await db.flush()
        run=await db.get(Run,ids['run_id']);run.current_translation_revision_id=revision.id
        secondary_id, anchor_id, revision_id = secondary.id, anchor.id, revision.id
    base=f"/api/v1/runs/{ids['run_id']}/translations"
    common={'business_revision_id':str(ids['revision_id']), 'translation_revision_id':str(revision_id),
        'parse_artifact_id':str(ids['parse_id']), 'document_id':ids['document_id']}
    default=await http.get(base,params={**common,'limit':50})
    assert default.status_code==200 and not set(segments[60:]).intersection(item['translation_segment_id'] for item in default.json()['items'])
    filters={**common,'source_segment_id':str(secondary_id),'source_anchor_id':str(anchor_id),'limit':1}
    first=await http.get(base,params=filters)
    assert first.status_code==200,first.text
    second=await http.get(base,params={**filters,'cursor':first.json()['next_cursor']})
    assert second.status_code==200 and second.json()['next_cursor'] is None
    items=first.json()['items']+second.json()['items']
    assert [item['translation_segment_id'] for item in items]==segments[60:]
    assert all(item['target']['source_segment_id']==str(secondary_id) and item['target']['source_anchor_id']==str(anchor_id) for item in items)
    assert all(item['source_segment_ids']==[str(ids['source_id']),str(secondary_id)] for item in items)
    resolved=await http.post('/api/v1/evidence/resolve',json=items[0]['target'])
    assert resolved.status_code==200,resolved.text
    assert resolved.json()['normalized_target']['source_segment_id']==str(secondary_id) and resolved.json()['page_no']==2
    anchor_only=await http.get(base,params={**common,'source_anchor_id':str(anchor_id),'limit':1})
    assert anchor_only.status_code==200 and anchor_only.json()['items'][0]['target']['source_segment_id']==str(secondary_id)
    changed=await http.get(base,params={**common,'source_segment_id':str(ids['source_id']),'cursor':first.json()['next_cursor']})
    assert changed.status_code==409 and changed.json()['error']['code']=='PAGE_STALE'
    mismatch=await http.get(base,params={**filters,'source_segment_id':str(ids['source_id'])})
    wrong_parse=await http.get(base,params={**filters,'parse_artifact_id':str(uuid4())})
    assert mismatch.status_code==wrong_parse.status_code==404
    other=await seed_fields(conn,p,created)
    cross_case=await http.get(base,params={**common,'source_segment_id':str(other['source_id'])})
    assert cross_case.status_code==404


@pytest.mark.asyncio
async def test_accept_warning_refreshes_version_without_retranslation(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        issue = await db.get(rm.Issue, ids["issues"][1])
        issue.severity = "yellow"
        run = await db.get(Run, ids["run_id"])
        run.next_event_seq += 1
        db.add(RunEvent(tenant_id=p.tenant_id, run_id=run.id, seq=run.next_event_seq, event_type="run_status", status="awaiting_review", stage="等待人工复核", message="固定解析", payload={"parse_artifact_ids":[str(ids["parse_id"])]}))
    base = f"/api/v1/cases/{ids['case_id']}"
    view = (await http.get(base + "/review-view", params={"run_id": str(ids["run_id"])})).json()
    params = {"run_id": str(ids["run_id"]), "business_revision_id": str(ids["revision_id"]), "review_state_version": view["review_state_version"]}
    before = (await http.get(base + "/issues", params=params)).json()
    issue = next(item for item in before["items"] if item["issue_id"] == str(ids["issues"][1]))
    response = await http.post(base + f"/issues/{issue['issue_id']}/accept", headers={"Idempotency-Key": uuid4().hex},
        json={"expected_business_revision_id": str(ids["revision_id"]), "expected_issue_version": issue["issue_version"], "reason": "已核对原件并认可"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    old = await http.get(base + "/issues", params=params)
    assert old.status_code == 409 and old.json()["error"]["code"] == "PAGE_STALE"
    updated = (await http.get(base + "/review-view", params={"run_id": str(ids["run_id"])})).json()
    assert updated["review_state_version"] == view["review_state_version"] + 1
    assert updated["business_revision_id"] == view["business_revision_id"]
    after = (await http.get(base + "/issues", params={**params, "review_state_version": updated["review_state_version"], "severity": "done"})).json()
    assert after["counts"]["done"] == 1 and after["items"][0]["disposition"]["reason"] == "已核对原件并认可"
    assert after["counts"]["warning"] == 0
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        run = await db.get(Run, ids["run_id"])
        assert run.status == RunStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_ignore_red_issue_and_local_repair_api_are_versioned_and_idempotent(http_db):
    http,conn,p,created=http_db
    ids=await seed_fields(conn,p,created)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        db.add(ExecutionEpoch(tenant_id=p.tenant_id,run_id=ids['run_id'],epoch=1,purpose='initial',graph_version='translate_review@1',model_bindings=await current_model_bindings('extract', 'translate'),input_parse_artifact_ids=[str(ids['parse_id'])]))
    base=f"/api/v1/cases/{ids['case_id']}"
    body={'expected_business_revision_id':str(ids['revision_id']),'expected_issue_version':1}
    ignored=await http.post(base+f"/issues/{ids['issues'][0]}/ignore",headers={'Idempotency-Key':uuid4().hex},json=body)
    assert ignored.status_code==200 and ignored.json()['status']=='ignored' and not ignored.json()['blocking_delivery']
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        segment=rm.TranslationSegment(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),run_id=ids['run_id'],document_id=UUID(ids['document_id']),document_version=1,parse_artifact_id=ids['parse_id'],section_id=ids['section_id'],target_language='en',ordinal=0,source_segment_ids=[str(ids['source_id'])],translated_text='Invoice number 001',state='translated',dependency_fingerprint='0'*64)
        db.add(segment);await db.flush()
        revision=rm.TranslationRevision(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),run_id=ids['run_id'],business_revision_id=ids['revision_id'],target_language='en',revision_no=1,parse_artifact_ids=[str(ids['parse_id'])],resolved_scope_ids=[],segment_ids=[str(segment.id)],prompt_versions={},model_bindings=[],qa_status='passed',qa_summary={'semantic_status':'evaluated_candidates_only'},dependency_fingerprint='0'*64)
        db.add(revision);await db.flush()
        run=await db.get(Run,ids['run_id']);run.current_translation_revision_id=revision.id
        issue=await db.get(rm.Issue,ids['issues'][1]);issue.translation_revision_id=revision.id;issue.category='other';issue.guidance={'replacement_text':'Invoice 001','recommendation':'Use the exact label'};issue.sources=[{'target':{'case_id':ids['case_id'],'document_id':ids['document_id'],'document_version':1,'parse_artifact_id':str(ids['parse_id']),'translation_segment_id':str(segment.id),'translation_revision_id':str(revision.id)}}]
        revision_id=revision.id
    key=uuid4().hex;payload={'expected_business_revision_id':str(ids['revision_id']),'expected_issue_version':1,'expected_translation_revision_id':str(revision_id)}
    url=base+f"/issues/{ids['issues'][1]}/apply-recommendation"
    repair=await http.post(url,headers={'Idempotency-Key':key},json=payload)
    assert repair.status_code==202,repair.text
    repeat=await http.post(url,headers={'Idempotency-Key':key},json=payload);assert repeat.status_code==202 and repeat.json()==repair.json()
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        run=await db.get(Run,ids['run_id']);assert run.current_translation_revision_id!=revision_id
        revision=await db.get(rm.TranslationRevision,run.current_translation_revision_id)
        changed=await db.get(rm.TranslationSegment,UUID(revision.segment_ids[0]));assert changed.translated_text=='Invoice 001'
        epoch=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id).order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one();assert epoch.purpose=='quality_preview'
        issue=await db.get(rm.Issue,ids['issues'][1]);issue.sources=[{'target':{**issue.sources[0]['target'],'translation_segment_id':str(changed.id),'translation_revision_id':str(revision.id)}}]
        run.status=RunStatus.AWAITING_REVIEW
        current=revision.id
    instruction={**payload,'expected_issue_version':2,'expected_translation_revision_id':str(current),'instruction':'将发票编号用 Invoice No. 表达，保留001'}
    submitted=await http.post(base+f"/issues/{ids['issues'][1]}/request-revision",headers={'Idempotency-Key':uuid4().hex},json=instruction)
    assert submitted.status_code==202,submitted.text
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        epochs=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==ids['run_id']).order_by(ExecutionEpoch.epoch))).scalars().all()
        assert epochs[-1].purpose=='issue_repair' and epochs[-1].repair_request['prompt']['version']==2
        assert epochs[-1].repair_request['instruction']==instruction['instruction']


@pytest.mark.parametrize('recommendation', ['补充漏译的运输条款', 'Translate the missing sentence', 'Use the confirmed source label Shipper'])
def test_evidenced_translation_recommendation_is_not_missing_material(recommendation):
    from app.review.issue_skill import recommendation_requires_assessment
    assert not recommendation_requires_assessment({'recommendation': recommendation})


@pytest.mark.asyncio
@pytest.mark.parametrize('recommendation,replacement,purpose', [
    ('将发票编号写为 Invoice No.，保留001', None, 'issue_repair'),
    ('请补拍发票编号区域，核对原件后再修订', 'Invoice No. 001', 'issue_assessment'),
    ('将发票编号写为 Invoice number 001', 'Invoice number 001', 'issue_assessment'),
])
async def test_apply_freezes_current_recommendation_and_requires_evidence_before_revision(http_db, recommendation, replacement, purpose):
    from app.review.commands import issue_view
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        segment = rm.TranslationSegment(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
            document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'], section_id=ids['section_id'],
            target_language='en', ordinal=0, source_segment_ids=[str(ids['source_id'])], translated_text='Invoice number 001',
            state='translated', protected_tokens={'P0': '001'}, dependency_fingerprint='0'*64)
        db.add(segment); await db.flush()
        revision = rm.TranslationRevision(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
            business_revision_id=ids['revision_id'], target_language='en', revision_no=1,
            parse_artifact_ids=[str(ids['parse_id'])], resolved_scope_ids=[], segment_ids=[str(segment.id)],
            prompt_versions={}, model_bindings=[], qa_status='passed', qa_summary={}, dependency_fingerprint='0'*64)
        db.add(revision); await db.flush()
        run = await db.get(Run, ids['run_id']); run.current_translation_revision_id = revision.id
        db.add(ExecutionEpoch(tenant_id=p.tenant_id, run_id=run.id, epoch=run.current_epoch, purpose='initial',
            graph_version='translate_review@1', model_bindings=await current_model_bindings('extract', 'translate'),
            input_parse_artifact_ids=[str(ids['parse_id'])]))
        issue = await db.get(rm.Issue, ids['issues'][1]); issue.category = 'terminology'; issue.translation_revision_id = revision.id
        issue.guidance = {'recommendation': recommendation, 'replacement_text': replacement}
        issue.sources = [{'target': {'translation_segment_id': str(segment.id), 'source_segment_id': str(ids['source_id'])}}]
        view = await issue_view(db, issue)
        assert view['guidance']['can_apply'] and next(a for a in view['guidance']['actions'] if a['action'] == 'apply-recommendation')['enabled']
        revision_id = revision.id
    body = {'expected_business_revision_id': str(ids['revision_id']), 'expected_issue_version': 1,
        'expected_translation_revision_id': str(revision_id), 'instruction': '不要把请求body替代页面当前推荐'}
    url = f"/api/v1/cases/{ids['case_id']}/issues/{ids['issues'][1]}/apply-recommendation"
    key = uuid4().hex
    queued = await http.post(url, headers={'Idempotency-Key': key}, json=body)
    assert queued.status_code == 202, queued.text
    assert '已受理推荐' in queued.json()['message']
    replay = await http.post(url, headers={'Idempotency-Key': key}, json=body)
    assert replay.status_code == 202 and replay.json() == queued.json()
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        epoch = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == ids['run_id'])
            .order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one()
        assert epoch.purpose == purpose and epoch.repair_request['instruction'] == recommendation
        assert epoch.repair_request['recommendation'] == recommendation
        assert epoch.repair_request['request_action'] == 'apply-recommendation'
        assert epoch.repair_request['segment_id'] == (str(segment.id) if purpose == 'issue_repair' else None)
        assert epoch.input_parse_artifact_ids == [str(ids['parse_id'])]
        run = await db.get(Run, ids['run_id']); issue = await db.get(rm.Issue, ids['issues'][1])
        assert run.current_translation_revision_id == revision_id
        assert (await issue_view(db, issue))['status'] == 'open'
        assert not (await db.execute(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id == issue.id))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize('broken_companion,unchanged', [(False, False), (True, False), (False, True)])
async def test_local_revision_langgraph_updates_only_target_and_keeps_unchanged_issue(http_db,monkeypatch,broken_companion,unchanged):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.workflows.translation import TranslationNodes
    from app.prompts.registry import PromptRegistry
    from app.prompts.schemas import SegmentRevisionResult,GuidedSemanticQaResult
    from langgraph.graph import StateGraph,START,END
    from app.workflows.state import RunGraphState
    http,conn,p,created=http_db
    ids=await seed_fields(conn,p,created)
    registry=PromptRegistry.load()
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        segments=[]
        for i in range(2):
            segment=rm.TranslationSegment(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),run_id=ids['run_id'],document_id=UUID(ids['document_id']),document_version=1,parse_artifact_id=ids['parse_id'],section_id=ids['section_id'],target_language='en',ordinal=i,source_segment_ids=[str(ids['source_id'])],translated_text='Invoice number 001' if not i else 'Unchanged 001',protected_tokens={'P1':'001'},state='translated',dependency_fingerprint='0'*64)
            if i and broken_companion:segment.translated_text='Unchanged ⟦P1⟧'
            db.add(segment);await db.flush();segments.append(segment)
        scope=rm.ResolvedTranslationScope(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),run_id=ids['run_id'],document_id=UUID(ids['document_id']),document_version=1,parse_artifact_id=ids['parse_id'],business_revision_id=ids['revision_id'],selector_version='all@1',source_section_ids=[str(ids['section_id'])],source_segment_ids=[str(ids['source_id'])],fingerprint='0'*64)
        db.add(scope);await db.flush()
        revision=rm.TranslationRevision(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),run_id=ids['run_id'],business_revision_id=ids['revision_id'],target_language='en',revision_no=1,parse_artifact_ids=[str(ids['parse_id'])],resolved_scope_ids=[str(scope.id)],segment_ids=[str(s.id) for s in segments],prompt_versions=registry.freeze(['document.translate','document.semantic_qa']),model_bindings=[],qa_status='passed',qa_summary={'semantic_status':'evaluated_candidates_only'},dependency_fingerprint='0'*64)
        db.add(revision);await db.flush()
        run=await db.get(Run,ids['run_id']);run.current_translation_revision_id=revision.id;run.status=RunStatus.QUEUED
        parse=await db.get(em.ParseArtifact,ids['parse_id']);parse.layout_manifest={'blocks':[{'kind':'text','page':1,'ids':[str(ids['source_id'])]}]}
        issue=await db.get(rm.Issue,ids['issues'][1]);issue.translation_revision_id=revision.id;issue.category='other';issue.sources=[{'target':{'case_id':ids['case_id'],'document_id':ids['document_id'],'document_version':1,'parse_artifact_id':str(ids['parse_id']),'translation_segment_id':str(segments[0].id)}}]
        another=rm.Issue(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),run_id=run.id,business_revision_id=ids['revision_id'],translation_revision_id=revision.id,category='other',severity='yellow',blocks_export=False,title='未改片段提醒',description='原始疑点需继续核对',sources=[{'target':{'translation_segment_id':str(segments[1].id)}}])
        db.add(another)
        material_issue = rm.Issue(tenant_id=p.tenant_id,case_id=UUID(ids['case_id']),run_id=run.id,business_revision_id=ids['revision_id'],
            translation_revision_id=revision.id,category='source_uncertain',severity='undetermined',blocks_export=False,
            title='同段扫描缺字待核验',description='扫描质量不能用译文修订代替核验',sources=[{'target':{'translation_segment_id':str(segments[0].id), 'source_segment_id':str(ids['source_id'])}}])
        db.add(material_issue);await db.flush()
        epoch=ExecutionEpoch(tenant_id=p.tenant_id,run_id=run.id,epoch=run.current_epoch,purpose='issue_repair',graph_version='translate_review@1',model_bindings=[],input_parse_artifact_ids=[str(parse.id)],repair_request={'issue_id':str(issue.id),'segment_id':str(segments[0].id),'translation_revision_id':str(revision.id),'instruction':'Use Invoice No.','actor_id':str(p.user_id),'prompt':registry.freeze(['document.revise'])['document.revise']})
        db.add(epoch);await db.flush()
        calls=[]
        def call(nodes,step,key,variables,model,role):
            calls.append((key,variables))
            return SimpleNamespace(value=SegmentRevisionResult(translated_text='Invoice number 001' if unchanged else 'Invoice No. 001',explanation='Label clarified') if key=='document.revise' else GuidedSemanticQaResult(findings=[]))
        monkeypatch.setattr(TranslationNodes,'_call',call)
        monkeypatch.setattr(TranslationNodes,'_draft_previews',lambda self,ids:None)
        def execute(sync):
            class Worker:
                registry=registry_ref
                tenant=p.tenant_id;run_id=ids['run_id'];epoch=1
                @contextmanager
                def transaction(self):yield sync
                def locked(self,db):return db.get(Run,self.run_id)
                def load_spec(self,state):
                    run=self.locked(sync);self.spec=run.task_spec;self.prompt_versions=registry_ref.freeze(['document.translate','document.semantic_qa']);self.case_id=run.case_id;self.purpose='issue_repair';self.documents=[(UUID(ids['document_id']),1)];self.bindings=[]
                    return {}
                def step(self,name,inputs,fn):return [str(x) for x in fn(uuid4())]
                def confirmed_type(self, db, logical): return logical.type_candidate.value
                def confirmed_cargo_profiles(self, db, logical): return []
                def complete_step(self,*args):pass
                def require_settled_steps(self,*args,**kwargs):pass
            nodes=TranslationNodes(Worker());builder=StateGraph(RunGraphState)
            for name in ['load_review','repair_local','quality_and_preview','publish_review']:builder.add_node(name,getattr(nodes,name))
            for a,b in [(START,'load_review'),('load_review','repair_local'),('repair_local','quality_and_preview'),('quality_and_preview','publish_review'),('publish_review',END)]:builder.add_edge(a,b)
            if unchanged:
                from app.workflows.worker import ModelFailure
                with pytest.raises(ModelFailure, match='未产生译文修改'):
                    builder.compile().invoke({'run_id':str(ids['run_id'])})
                return
            builder.compile().invoke({'run_id':str(ids['run_id'])})
        registry_ref=registry
        await db.run_sync(execute)
        if unchanged:
            run = await db.get(Run, ids['run_id'])
            assert run.current_translation_revision_id == revision.id
            assert not (await db.execute(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id == issue.id))).scalars().all()
            assert [key for key, _ in calls] == ['document.revise']
            return
        run=await db.get(Run,ids['run_id']);assert run.status==RunStatus.AWAITING_REVIEW
        new=await db.get(rm.TranslationRevision,run.current_translation_revision_id)
        assert new.parent_revision_id==revision.id and new.segment_ids[1]==str(segments[1].id)
        edited=await db.get(rm.TranslationSegment,UUID(new.segment_ids[0]));assert edited.translated_text=='Invoice No. 001'
        preview=(await db.execute(select(rm.PreviewArtifact).where(rm.PreviewArtifact.translation_revision_id==new.id))).scalar_one()
        assert 'Invoice No. 001' in preview.reading_html and '原文：' not in preview.reading_html
        copies=(await db.execute(select(rm.Issue).where(rm.Issue.translation_revision_id==new.id,rm.Issue.supersedes_issue_id==another.id))).scalars().all();assert len(copies)==1
        materials=(await db.execute(select(rm.Issue).where(rm.Issue.translation_revision_id==new.id,rm.Issue.supersedes_issue_id==material_issue.id))).scalars().all()
        assert len(materials)==1 and materials[0].sources[0]['target']['translation_segment_id']==str(edited.id)
        assert (await db.execute(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id==materials[0].id))).scalars().all()==[]
        assert [key for key,_ in calls]==['document.revise','document.semantic_qa']
        assert '字段是重要业务信息' in calls[0][1]['issue_rules']
        assert len(calls[1][1]['pairs'])==1
        quality=(await db.execute(select(rm.Issue).where(rm.Issue.translation_revision_id==new.id,rm.Issue.category=='translation_quality'))).scalars().all()
        if broken_companion:
            assert len(quality)==1
            from app.review.issue_skill import translation_target, guidance_for
            assert translation_target(quality[0])['translation_segment_id']==str(segments[1].id)
            assert guidance_for(quality[0])['actions'][0]['enabled']
            assert '保护' in quality[0].description
        else:
            assert not quality


@pytest.mark.asyncio
async def test_whole_page_and_translation_issues_do_not_attach_to_every_field(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        for issue_id in ids['issues']:
            issue = await db.get(rm.Issue, issue_id)
            issue.category = 'source_uncertain'
            issue.sources = [{'target': {'parse_artifact_id': str(ids['parse_id']), 'source_segment_id': str(ids['source_id'])}}]
        quality = rm.Issue(tenant_id=p.tenant_id, case_id=UUID(ids['case_id']), run_id=ids['run_id'],
            business_revision_id=ids['revision_id'], category='meaning_reversal', severity='yellow', blocks_export=False,
            title='同段其他文字的含义待核对', description='整段译文问题不意味着本字段错误',
            sources=[{'target': {'field_id': str(ids['field_id']), 'source_segment_id':str(ids['source_id'])}}])
        db.add(quality)
    base = f"/api/v1/cases/{ids['case_id']}"
    fields = (await http.get(base + '/fields')).json()['items']
    sourced = next(item for item in fields if item['field_id'] == str(ids['field_id']))
    assert sourced['issues'] == [] and sourced['confirm_blocked_reason'] is None
    confirmed = await http.post(base + f"/fields/{ids['field_id']}/confirm", headers={'Idempotency-Key': uuid4().hex},
        json={'expected_business_revision_id': str(ids['revision_id']), 'expected_parse_artifact_id': str(ids['parse_id'])})
    assert confirmed.status_code == 200, confirmed.text
    issues = (await http.get(base + '/issues')).json()['items']
    assert len(issues) == 3
    assert all([action['action'] for action in issue['guidance']['actions']] ==
        ['revise-translation', 'ignore', 'request-revision', 'apply-recommendation', 'supplement-upload'] for issue in issues)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['request-revision', 'apply-recommendation'])
async def test_material_issue_agent_is_queued_and_preserves_unresolved_state(http_db, monkeypatch, action):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.workflows.translation import TranslationNodes
    from app.prompts.registry import PromptRegistry
    from app.prompts.schemas import IssueAssessmentResult
    from app.review.commands import issue_view
    from langgraph.graph import StateGraph, START, END
    from app.workflows.state import RunGraphState
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        issue = await db.get(rm.Issue, ids['issues'][0])
        issue.guidance = {'recommendation': '请说明应当补拍哪个区域，不要猜单号'}
        run = await db.get(Run, ids['run_id']); run.next_event_seq += 1
        db.add(ExecutionEpoch(tenant_id=p.tenant_id,run_id=run.id,epoch=run.current_epoch,purpose='initial',graph_version='translate_review@1',model_bindings=await current_model_bindings('extract'),input_parse_artifact_ids=[str(ids['parse_id'])]))
        db.add(RunEvent(tenant_id=p.tenant_id, run_id=run.id, seq=run.next_event_seq, event_type='run_status', status='awaiting_review', stage='待复核', message='冻结解析', payload={'parse_artifact_ids':[str(ids['parse_id'])]}))
    base = f"/api/v1/cases/{ids['case_id']}/issues/{ids['issues'][0]}"
    request = {'expected_business_revision_id': str(ids['revision_id']), 'expected_issue_version': 1,
        'expected_translation_revision_id': None, 'instruction': '请说明应当补拍哪个区域，不要猜单号'}
    key = uuid4().hex
    queued = await http.post(base + '/' + action, headers={'Idempotency-Key': key}, json=request)
    assert queued.status_code == 202, queued.text
    replay = await http.post(base + '/' + action, headers={'Idempotency-Key': key}, json=request)
    assert replay.status_code == 202 and replay.json() == queued.json()
    repeat = await http.post(base + '/' + action, headers={'Idempotency-Key': uuid4().hex}, json=request)
    assert repeat.status_code == 409
    registry = PromptRegistry.load()
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        epoch = (await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id == ids['run_id']).order_by(ExecutionEpoch.epoch.desc()).limit(1))).scalar_one()
        assert epoch.purpose == 'issue_assessment' and epoch.repair_request['segment_id'] is None
        assert epoch.repair_request['instruction'] == '请说明应当补拍哪个区域，不要猜单号'
        assert epoch.repair_request['request_action'] == action
        calls = []
        def call(nodes, step_id, key, variables, model, role):
            calls.append((key, variables))
            return SimpleNamespace(value=IssueAssessmentResult(explanation='目前资料未足够证明编号冲突的原因。',
                recommendation='请补拍发票编号区域，并与原件核对。', source_segment_ids=[str(ids['source_id'])], recommended_action='supplement-upload'))
        monkeypatch.setattr(TranslationNodes, '_call', call)
        def execute(sync):
            class Worker:
                registry = registry_ref
                tenant = p.tenant_id; run_id = ids['run_id']; epoch = epoch_no
                @contextmanager
                def transaction(self): yield sync
                def locked(self, db): return db.get(Run, self.run_id)
                def load_spec(self, state):
                    run = self.locked(sync); self.spec = run.task_spec; self.prompt_versions = {}
                    self.case_id = run.case_id; run.status = RunStatus.PROCESSING
                    return {}
                def confirmed_type(self, db, logical): return logical.type_candidate.value
                def confirmed_cargo_profiles(self, db, logical): return []
                def step(self, name, inputs, fn): return fn(uuid4())
                def complete_step(self, *args): pass
                def require_settled_steps(self, *args, **kwargs): pass
            nodes = TranslationNodes(Worker()); graph = StateGraph(RunGraphState)
            graph.add_node('load_review', nodes.load_review); graph.add_node('assess_issue', nodes.assess_issue)
            graph.add_edge(START, 'load_review'); graph.add_edge('load_review', 'assess_issue'); graph.add_edge('assess_issue', END)
            graph.compile().invoke({'run_id': str(ids['run_id'])})
        registry_ref = registry; epoch_no = epoch.epoch
        await db.run_sync(execute)
        issue = await db.get(rm.Issue, ids['issues'][0]); run = await db.get(Run, ids['run_id'])
        view = await issue_view(db, issue)
        assert view['status'] == 'open' and view['blocking_delivery']
        assert view['guidance']['agent_status'] == 'succeeded' and '补拍' in view['guidance']['recommendation']
        assert run.status == RunStatus.AWAITING_REVIEW and run.current_translation_revision_id is None
        assert (await db.execute(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id == issue.id))).scalars().all() == []
        assert calls[0][0] == 'document.issue_assess' and '字段是重要业务信息' in calls[0][1]['issue_rules']
        assert not {'agent_status', 'agent_response', 'actions', 'can_apply', 'agent_request_no'} & set(calls[0][1]['issue']['guidance'])
        assert calls[0][1]['issue']['guidance']['recommendation'] == request['instruction']
        assert calls[0][1]['materials'][0]['source_segment_id'] == str(ids['source_id'])
