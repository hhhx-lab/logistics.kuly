"""处理必要确认使用真实HTTP/PG回滚验证，不启动外部模型或Worker。"""
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.evidence_models import ParseArtifact, ParseQuality
from app.workflows.models import InputRequest, InputRequestStatus, Run, RunStatus, QueueOutbox, ExecutionEpoch
from tests.test_workflow_response_contracts import http_db, seed_source


@pytest.mark.parametrize('with_companion', [False, True])
async def test_active_confirmation_version_single_choice_and_idempotent_resume(http_db, monkeypatch, with_companion):
    http, conn, principal, created = http_db
    case_id, document_id = await seed_source(conn, principal, created)
    from app.documents.models import StoredObject
    companion_id = None
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            if with_companion:
                from app.documents.models import SourceDocument, DocumentVersion, StoredObjectCategory
                source = SourceDocument(tenant_id=principal.tenant_id, case_id=UUID(case_id), current_version=1)
                obj = StoredObject(tenant_id=principal.tenant_id, case_id=UUID(case_id),
                    object_key=f'confirmation-fixture/{uuid4()}', category=StoredObjectCategory.SOURCE)
                db.add_all([source, obj]); await db.flush()
                companion_id = str(source.id)
                db.add(DocumentVersion(tenant_id=principal.tenant_id, case_id=UUID(case_id), document_id=source.id,
                    version=1, filename='配套箱单.pdf', mime='application/pdf', size_bytes=128,
                    sha256='1'*64, object_id=obj.id, uploaded_by=principal.user_id))
                await db.flush()
            objects=(await db.execute(select(StoredObject).where(StoredObject.case_id==UUID(case_id)))).scalars().all()
            for obj in objects:
                obj.is_visible=True
    # 物理文件预检与字节输送单独替身；原件元数据、Range和中文头编码仍走生产HTTP。
    async def bytes_for_fixture(*args, start, end, **kwargs):
        async def stream():
            yield b'x' * (end - start + 1)
        return stream()
    monkeypatch.setattr('app.infra.content_stream.prepared_bytes', bytes_for_fixture)
    http._transport.app.state.object_store = object()
    from urllib.parse import unquote
    for headers, code, size in [({}, 200, 128), ({'Range':'bytes=0-9'}, 206, 10)]:
        content=await http.get(f'/api/v1/documents/{document_id}/versions/1/content', headers=headers)
        assert content.status_code == code and len(content.content) == size
        assert '真实元数据_响应契约.pdf' in unquote(content.headers['content-disposition'])
    draft = await http.post('/api/v1/task-drafts', json={
        'case_id':case_id, 'workflow_type':'translate_review',
        'source_versions':[{'document_id':value,'document_version':1} for value in [document_id, companion_id] if value],
        'draft_parameters':{'target_language':'zh-CN'}}, headers={'Idempotency-Key':uuid4().hex})
    assert draft.status_code == 200, draft.text
    value=draft.json()
    started=await http.post('/api/v1/runs',json={'draft_task_id':value['draft_task_id'],'expected_draft_version':value['draft_version']}, headers={'Idempotency-Key':uuid4().hex})
    assert started.status_code == 200, started.text
    run_id=started.json()['run_id']
    expected_revision = None
    companion_parse_id = None
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            run=await db.get(Run,UUID(run_id));run.status=RunStatus.AWAITING_INPUT
            parse=ParseArtifact(tenant_id=principal.tenant_id,case_id=UUID(case_id),document_id=UUID(document_id),document_version=1,
                parser_name='fixture',parser_version='1',parser_config_hash='0'*64,quality=ParseQuality.VALID,is_synthetic=True,page_count=1)
            db.add(parse);await db.flush()
            if companion_id:
                from app.documents.evidence_models import BusinessRevision, BusinessRevisionField, FieldAssertion, LogicalDocument, LogicalDocumentType
                from app.documents.models import Case
                adopted = ParseArtifact(tenant_id=principal.tenant_id,case_id=UUID(case_id),document_id=UUID(companion_id),document_version=1,
                    parser_name='fixture',parser_version='1',parser_config_hash='1'*64,quality=ParseQuality.VALID,is_synthetic=True,page_count=1)
                newer = ParseArtifact(tenant_id=principal.tenant_id,case_id=UUID(case_id),document_id=UUID(companion_id),document_version=1,
                    parser_name='fixture',parser_version='2',parser_config_hash='2'*64,quality=ParseQuality.VALID,is_synthetic=True,page_count=1)
                db.add_all([adopted,newer]);await db.flush()
                logical = LogicalDocument(tenant_id=principal.tenant_id,case_id=UUID(case_id),parse_artifact_id=adopted.id,
                    ordinal=1,type_candidate=LogicalDocumentType.PACKING_LIST)
                revision = BusinessRevision(tenant_id=principal.tenant_id,case_id=UUID(case_id),revision_no=1,reason='baseline_extraction')
                db.add_all([logical,revision]);await db.flush()
                field = FieldAssertion(tenant_id=principal.tenant_id,case_id=UUID(case_id),parse_artifact_id=adopted.id,
                    logical_document_id=logical.id,field_key='document_number',group='references',source_text='PK-001',candidate_value='PK-001')
                db.add(field);await db.flush()
                db.add(BusinessRevisionField(tenant_id=principal.tenant_id,business_revision_id=revision.id,field_assertion_id=field.id))
                case = await db.get(Case,UUID(case_id))
                case.current_business_revision_id=run.current_business_revision_id=revision.id
                case.business_revision_no=1
                expected_revision=str(revision.id); companion_parse_id=str(adopted.id)
                initial=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id,ExecutionEpoch.epoch==1))).scalar_one()
                initial.input_parse_artifact_ids=[str(parse.id),str(newer.id)]
            item=InputRequest(tenant_id=principal.tenant_id,run_id=run.id,document_id=UUID(document_id),document_version=1,
                kind='document_boundary',expected_parse_artifact_id=parse.id,expected_business_revision_id=UUID(expected_revision) if expected_revision else None,question='确认原件类型',status=InputRequestStatus.ACTIVE,
                source_refs=[],options=[{'option_id':'commercial_invoice','label':'commercial_invoice'},{'option_id':'packing_list','label':'packing_list'}])
            db.add(item);await db.flush();input_id=str(item.id);parse_id=str(parse.id)
    response=await http.get(f'/api/v1/runs/{run_id}/input-requests')
    assert response.status_code == 200, response.text
    request=response.json()['items'][0]
    assert request['status']=='active' and request['options'][0]['label']=='商业发票'
    body={'selected_option_ids':['commercial_invoice'],'expected_request_version':1,'expected_parse_artifact_id':parse_id,'expected_business_revision_id':expected_revision}
    async def resolve(payload,key=None):
        return await http.post(f'/api/v1/input-requests/{input_id}/resolve',json=payload,headers={'Idempotency-Key':key or uuid4().hex})
    assert (await resolve({**body,'selected_option_ids':['commercial_invoice','packing_list']})).status_code == 422
    assert (await resolve({**body,'expected_request_version':2})).status_code == 409
    key=uuid4().hex
    resolved=await resolve(body,key)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()['status']=='queued' and resolved.json()['execution_epoch']==2
    assert (await resolve(body,key)).json()['execution_epoch']==2
    assert (await http.get(f'/api/v1/runs/{run_id}/input-requests')).json()['items']==[]
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db:
        item=await db.get(InputRequest,UUID(input_id))
        assert item.status==InputRequestStatus.RESOLVED and item.answer['selected_option_ids']==['commercial_invoice']
        outboxes=(await db.execute(select(QueueOutbox).where(QueueOutbox.payload['run_id'].astext==run_id))).scalars().all()
        resumes=[v for v in outboxes if v.payload.get('epoch')==2]
        assert len(resumes)==1
        epoch=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==UUID(run_id),ExecutionEpoch.epoch==2))).scalar_one()
        assert epoch.purpose=='resume_input' and set(epoch.input_parse_artifact_ids)=={value for value in [parse_id, companion_parse_id] if value}
        initial=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==UUID(run_id),ExecutionEpoch.epoch==1))).scalar_one()
        if companion_id:
            assert str(newer.id) in initial.input_parse_artifact_ids
            assert str(newer.id) not in epoch.input_parse_artifact_ids
    assert (await resolve(body)).status_code==409
