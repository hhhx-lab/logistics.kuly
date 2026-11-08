"""D5真实PostgreSQL回滚夹具：分页读取有界，过期摘要与删除闭包不冒充事实。"""
from datetime import UTC,datetime,timedelta
from types import SimpleNamespace
from uuid import uuid4
import time

import pytest
from sqlalchemy import select,text,insert,event,func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import VersionConflict
from app.documents.retention import load_case_for_read,CaseGone
from app.config import get_settings
from app.documents.models import Case,SourceDocument,StoredObject,StoredObjectCategory,DocumentVersion,IdempotencyRecord
from app.documents.evidence_models import ParseArtifact,ParseQuality,BusinessRevision
from app.exports.models import ReviewConfirmation,ExportAttempt,ExportSnapshot
from app.identity.models import User
from app.infra.database import create_database_engine
from app.management.models import SearchProjection,SearchFieldValue,RunDocumentSummary
from app.management.search import ordered_candidates,candidate_page
from app.management.service import query_runs,paginate,summary,facets
from app.management.purge import delete_domain_rows,selected_rows
from app.workflows.models import TaskDraft,DraftStatus,Run,RunStatus,RunDocument,RunEvent,QueueOutbox
from app.review.models import TranslationRevision,Issue


@pytest.fixture
async def fixture_db():
    settings=get_settings();engine=create_database_engine(settings)
    async with engine.connect() as conn:
        tx=await conn.begin()
        db=AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint')
        db.info['fixture_case_ids']=[]
        try:
            user=(await db.execute(select(User).where(User.account=='demo'))).scalar_one()
            await db.execute(text(f"SET LOCAL app.tenant_id = '{user.tenant_id}'"))
            await db.execute(text("SET LOCAL ROLE logistics_app"))
            yield db,SimpleNamespace(user_id=user.id,tenant_id=user.tenant_id),settings
        finally:
            ids=db.info['fixture_case_ids']
            await db.close();await tx.rollback()
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL app.tenant_id = '{user.tenant_id}'"))
                for model in (Case,Run,SearchProjection,StoredObject,DocumentVersion):
                    column=model.id if model is Case else model.case_id
                    assert (await conn.execute(select(func.count()).select_from(model).where(column.in_(ids)))).scalar_one()==0
    await engine.dispose()


async def build_runs(db,p,count):
    now=datetime.now(UTC);marker='D5_'+uuid4().hex
    case=Case(id=uuid4(),tenant_id=p.tenant_id,owner_user_id=p.user_id,name=marker,short_code=marker[:16],
        last_material_event_at=now,retention_expires_at=now+timedelta(days=30))
    db.add(case);await db.flush()
    db.info['fixture_case_ids'].append(case.id)
    ids=[uuid4() for _ in range(count)];draft_ids=[uuid4() for _ in ids]
    await db.execute(insert(TaskDraft),[dict(id=d,tenant_id=p.tenant_id,case_id=case.id,created_by=p.user_id,
        workflow_type='read_index',workflow_version=1,source_versions=[],draft_parameters={},status=DraftStatus.READY,
        task_summary=marker,expires_at=now+timedelta(days=1)) for d in draft_ids])
    await db.execute(insert(Run),[dict(id=r,tenant_id=p.tenant_id,case_id=case.id,draft_task_id=d,
        workflow_type='read_index',workflow_version=1,task_spec={},task_spec_hash='0'*64,prompt_versions={},
        initiated_by=p.user_id,status=RunStatus.INDEXED,created_at=now+timedelta(seconds=i//2),updated_at=now+timedelta(seconds=i//2))
        for i,(r,d) in enumerate(zip(ids,draft_ids))])
    projections=[dict(id=uuid4(),tenant_id=p.tenant_id,case_id=case.id,run_id=r,document_id=uuid4(),document_version=1,
        filename=f'{marker}_中文提单_{i}.pdf',doc_type='bill_of_lading',summary=marker,uploaded_at=now,uploaded_by=p.user_id,
        state='ready',dependency_fingerprint='0'*64) for i,r in enumerate(ids)]
    await db.execute(insert(SearchProjection),projections)
    return case,ids,projections,marker


@pytest.mark.asyncio
async def test_management_check_status_uses_current_business_revision(fixture_db):
    from app.review.models import RuleCheck
    from app.api.response_dto import ManagedRunView

    db,p,settings=fixture_db
    case,ids,_,_=await build_runs(db,p,1)
    filters={'case_id':str(case.id),'run_id':str(ids[0]),'tab':'all'}
    row=(await query_runs(db,p,settings,filters,limit=1))[0]
    assert ManagedRunView.model_validate(row).rules_status=='not_run'
    run=await db.get(Run,ids[0])
    revisions=[]
    for number in (1,2):
        revision=BusinessRevision(tenant_id=p.tenant_id,case_id=case.id,revision_no=number,
            reason='baseline_extraction',created_by=p.user_id)
        db.add(revision);await db.flush();revisions.append(revision)
    db.add(RuleCheck(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,
        business_revision_id=revisions[0].id,rule_id='fixture.current_check',rule_version=1,
        rule_pack_version='core-document@3',status='passed',explanation='fixture',
        input_field_ids=[],source_segment_ids=[],calculation={},input_fingerprint='0'*64))
    run.current_business_revision_id=revisions[0].id
    await db.flush()
    assert (await query_runs(db,p,settings,filters,limit=1))[0]['rules_status']=='run'
    run.current_business_revision_id=revisions[1].id
    await db.flush()
    assert (await query_runs(db,p,settings,filters,limit=1))[0]['rules_status']=='not_run'


@pytest.mark.asyncio
async def test_sql_page_capacity_and_watermark(fixture_db):
    db,p,settings=fixture_db
    case,ids,projections,marker=await build_runs(db,p,1200)
    await db.execute(insert(SearchFieldValue),[dict(id=uuid4(),tenant_id=p.tenant_id,projection_id=v['id'],field_assertion_id=uuid4(),
        role='origin',field_key='origin',normalized_value=f'上海-{marker}' if j==0 else f'candidate-{j}',state='confirmed')
        for v in projections for j in range(8)])
    filters={'q':marker,'sort':'activity_desc'}
    loaded=[]
    def track(session,instance):loaded.append(instance)
    event.listen(db.sync_session,'loaded_as_persistent',track)
    begin=time.perf_counter()
    first=await query_runs(db,p,settings,filters,limit=26)
    elapsed=time.perf_counter()-begin
    assert sum(isinstance(row,SearchProjection) for row in loaded)==26
    assert sum(isinstance(row,SearchFieldValue) for row in loaded)==208
    page=paginate(first,None,25,filters)
    precise=await query_runs(db,p,settings,{'case_id':str(case.id),'run_id':str(ids[0]),'tab':'all'},limit=1)
    assert len(precise)==1 and precise[0]['run_id']==str(ids[0])
    from app.api.routers.management import runs as runs_endpoint
    from starlette.requests import Request
    from urllib.parse import urlencode
    exact_filters={'case_id':str(case.id),'run_id':str(ids[0]),'tab':'all'}
    request=Request({'type':'http','method':'GET','path':'/api/v1/runs','query_string':urlencode(exact_filters).encode(),'headers':[]})
    async with AsyncSession(bind=db.bind,expire_on_commit=False,join_transaction_mode='create_savepoint') as endpoint_db:
        endpoint_page=await runs_endpoint(request,p,endpoint_db,settings,cursor=None,limit=1)
    assert len(endpoint_page['items'])==1 and endpoint_page['items'][0]['run_id']==str(ids[0])
    assert await query_runs(db,p,settings,{'case_id':str(uuid4()),'run_id':str(ids[0]),'tab':'all'},limit=1)==[]
    from app.api.errors import InvalidRequest
    with pytest.raises(InvalidRequest):await query_runs(db,p,settings,{'run_id':'invalid'},limit=1)
    second=await query_runs(db,p,settings,filters,cursor=page['next_cursor'],limit=26)
    assert len(first)==26 and len(second)==26
    assert not {r['run_id'] for r in page['items']} & {r['run_id'] for r in second}
    # 第26条作为has_more探针，在第二页可能由identity map复用。
    assert sum(isinstance(row,SearchProjection) for row in loaded)<=52
    assert sum(isinstance(row,SearchFieldValue) for row in loaded)<=416
    assert len((await db.execute(candidate_page(ordered_candidates(p,filters,'Asia/Shanghai'),filters,limit=26))).all())==26
    sql=str(candidate_page(ordered_candidates(p,filters,'Asia/Shanghai'),filters,limit=26).compile(dialect=db.bind.dialect,compile_kwargs={'literal_binds':True}))
    plan=(await db.execute(text('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+sql))).scalar_one()[0]
    assert plan['Plan']['Node Type']=='Limit' and plan['Plan']['Actual Rows']==26
    for sort in ('activity_asc','initiated_desc','initiated_asc','exported_desc','exported_asc'):
        value={**filters,'sort':sort}
        items=await query_runs(db,p,settings,value,limit=3)
        next_page=await query_runs(db,p,settings,value,cursor=paginate(items,None,2,value)['next_cursor'],limit=3)
        assert len(next_page)==3 and not {row['run_id'] for row in items[:2]} & {row['run_id'] for row in next_page}
    # 选择性中文文件名/起运地子串查询：真实GIN是否选择由planner决定，不强制关闭seqscan。
    filename_plan=(await db.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT id FROM search_projections WHERE filename ILIKE :pattern"),
        {'pattern':f'%{marker}_中文提单_1199.pdf%'})).scalar_one()[0]
    field_plan=(await db.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT id FROM search_field_values WHERE normalized_value ILIKE :pattern"),
        {'pattern':f'%上海-{marker}%'})).scalar_one()[0]
    def indexes(node):
        return ([node['Index Name']] if 'Index Name' in node else [])+[name for child in node.get('Plans',[]) for name in indexes(child)]
    print({'runs':1200,'projections':1200,'fields':9600,'page_runs':26,'page_projections':26,'page_fields':208,
        'first_dto_seconds':round(elapsed,3),'explain_ms':plan['Execution Time'],'plan_root':plan['Plan']['Node Type'],
        'filename_indexes':indexes(filename_plan['Plan']),'field_indexes':indexes(field_plan['Plan'])})
    case.review_state_version+=1;await db.flush()
    with pytest.raises(VersionConflict) as error:
        await query_runs(db,p,settings,filters,cursor=page['next_cursor'],limit=26)
    assert error.value.code=='PAGE_STALE'


@pytest.mark.asyncio
async def test_stale_summary_and_exact_deletion_closure(fixture_db,monkeypatch):
    db,p,settings=fixture_db
    case,ids,projections,marker=await build_runs(db,p,1)
    run=await db.get(Run,ids[0]);source=SourceDocument(tenant_id=p.tenant_id,case_id=case.id,current_version=1)
    obj=StoredObject(tenant_id=p.tenant_id,case_id=case.id,object_key=f't/{p.tenant_id}/c/{case.id}/fixture',category=StoredObjectCategory.SOURCE)
    db.add_all([source,obj]);await db.flush()
    version=DocumentVersion(tenant_id=p.tenant_id,case_id=case.id,document_id=source.id,version=1,filename='fixture.pdf',mime='application/pdf',size_bytes=0,sha256='0'*64,object_id=obj.id,uploaded_by=p.user_id)
    db.add(version);await db.flush()
    parse=ParseArtifact(tenant_id=p.tenant_id,case_id=case.id,document_id=source.id,document_version=1,parser_name='fixture',parser_version='1',parser_config_hash='0'*64,quality=ParseQuality.VALID,is_synthetic=True,page_count=1)
    db.add(parse);await db.flush()
    db.add_all([RunDocument(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,document_id=source.id,document_version=1,ordinal=1),
        RunEvent(tenant_id=p.tenant_id,run_id=run.id,seq=1,event_type='run.indexed',kind='fixture',payload={'parse_artifact_ids':[str(parse.id)]}),
        RunDocumentSummary(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,document_id=source.id,document_version=1,parse_artifact_id=parse.id,summary='旧摘要内容',state='stale',dependency_fingerprint='0'*64)])
    await db.flush()
    value=await summary(db,run,case)
    assert value['documents'][0]['state']=='stale'
    assert '旧摘要内容' not in value['documents'][0]['short_summary']
    from app.management.index import refresh_run_summaries
    await db.run_sync(lambda sync:refresh_run_summaries(sync,run))
    fresh=await summary(db,run,case)
    fingerprint=fresh["dependency_fingerprint"]
    assert fresh["documents"][0]["state"]=="ready"
    from app.api.routers.management import overview,search_suggestions,actors
    from starlette.requests import Request
    monkeypatch.setattr(db,"begin",db.begin_nested)
    directory=await overview(case.id,p,db,settings)
    assert directory["source_history"][0]["source"]["document_version"]==1
    assert directory["source_history"][0]["parses"][0]["parse_artifact_id"]==str(parse.id)
    assert directory["source_history"][0]["parses"][0]["parser_name"]=="fixture"
    request=Request({"type":"http","query_string":b"q=fixture&tab=all"})
    suggestions=await search_suggestions(request,p,db,settings)
    assert suggestions["items"][0]["status"]=="indexed"
    assert suggestions["items"][0]["match_explanation"]
    candidates=await actors(p,db,settings)
    assert str(p.user_id) in {item["id"] for item in candidates["items"]}

    run.failure_reason="本轮解析待核查";await db.flush()
    rebuilt=await summary(db,run,case)
    assert rebuilt["dependency_fingerprint"]!=fingerprint
    assert rebuilt["documents"][0]["state"]=="ready" and "本轮解析待核查" in rebuilt["documents"][0]["short_summary"]

    nested=QueueOutbox(tenant_id=p.tenant_id,topic='run',dedupe_key=marker+'nested',payload={'ref':{'run_id':str(run.id)}},status='published')
    unrelated=QueueOutbox(tenant_id=p.tenant_id,topic='run',dedupe_key=marker+'other',payload={'note':'prefix'+str(run.id)+'suffix'},status='published')
    same=IdempotencyRecord(tenant_id=p.tenant_id,actor_id=p.user_id,scope='run:'+str(run.id),key=uuid4().hex,request_hash='0'*64,result_ref={'refs':[{'run_id':str(run.id)}]})
    other=IdempotencyRecord(tenant_id=p.tenant_id,actor_id=p.user_id,scope='prefix'+str(run.id)+'suffix',key=uuid4().hex,request_hash='0'*64,result_ref={'note':'prefix'+str(run.id)+'suffix'})
    db.add_all([nested,unrelated,same,other]);await db.flush()
    await db.run_sync(lambda sync:delete_domain_rows(sync,case.id,uuid4()))
    assert not any((await db.run_sync(lambda sync:selected_rows(sync,case.id)))[1].values())
    assert (await db.execute(select(QueueOutbox.id).where(QueueOutbox.id==nested.id))).scalar_one_or_none() is None
    assert (await db.execute(select(QueueOutbox.id).where(QueueOutbox.id==unrelated.id))).scalar_one()==unrelated.id
    assert (await db.execute(select(IdempotencyRecord.id).where(IdempotencyRecord.id==same.id))).scalar_one_or_none() is None
    assert (await db.execute(select(IdempotencyRecord.id).where(IdempotencyRecord.id==other.id))).scalar_one()==other.id


@pytest.mark.asyncio
async def test_snapshot_selection_facets_and_post_filter_batches(fixture_db):
    db,p,settings=fixture_db
    case,ids,projections,marker=await build_runs(db,p,32)
    run=await db.get(Run,ids[0])
    business=BusinessRevision(tenant_id=p.tenant_id,case_id=case.id,revision_no=1,reason='baseline_extraction',created_by=p.user_id)
    db.add(business);await db.flush()
    run.current_business_revision_id=business.id
    translation=TranslationRevision(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=business.id,target_language='zh-CN',revision_no=1,
        parse_artifact_ids=[],resolved_scope_ids=[],segment_ids=[],prompt_versions={},model_bindings=[],qa_status='passed',dependency_fingerprint='0'*64,is_synthetic=True)
    db.add(translation);await db.flush()
    confirmation=ReviewConfirmation(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,task_spec_hash='0'*64,review_fingerprint='0'*64,
        business_revision_id=business.id,translation_revision_id=translation.id,pairing_manifest={},review_manifest={},output_formats=['pdf'],confirmed_by=p.user_id)
    db.add(confirmation);await db.flush()
    snapshots=[]
    now=datetime.now(UTC)
    for number in range(2):
        attempt=ExportAttempt(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,review_fingerprint='0'*64,
            status='succeeded',execution_epoch=1,created_by=p.user_id)
        db.add(attempt);await db.flush()
        snapshot=ExportSnapshot(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,attempt_id=attempt.id,
            manifest={'business_revision_id':str(business.id),'confirmed_by':str(p.user_id),'issue_ids':[],'issue_disposition_ids':[],'output_formats':['pdf']},
            exported_by=p.user_id,exported_at=now+timedelta(seconds=number))
        db.add(snapshot);await db.flush();snapshots.append(snapshot)
        values={**projections[0],'id':uuid4(),'snapshot_id':snapshot.id,'filename':'frozen.pdf','summary':marker if number==0 else '另一个成功快照',
            'doc_type':'bill_of_lading' if number==0 else 'packing_list'}
        await db.execute(insert(SearchProjection),values)
    frozen=await query_runs(db,p,settings,{'q':marker,'tab':'exported','doc_type':['bill_of_lading']},limit=5)
    assert frozen==[]
    history=await query_runs(db,p,settings,{'q':marker,'tab':'exported','doc_type':['bill_of_lading'],'version_scope':'include_history'},limit=5)
    assert len(history)==1 and history[0]['displayed_snapshot_id']==str(snapshots[0].id)
    exact=await query_runs(db,p,settings,{'case_id':str(case.id),'run_id':str(run.id),'snapshot_id':str(snapshots[0].id),'tab':'all'},limit=1)
    assert len(exact)==1 and exact[0]['displayed_snapshot_id']==str(snapshots[0].id)
    assert await query_runs(db,p,settings,{'run_id':str(ids[1]),'snapshot_id':str(snapshots[0].id),'tab':'all'},limit=1)==[]
    latest_history=await query_runs(db,p,settings,{'q':marker,'tab':'exported','version_scope':'include_history'},limit=5)
    assert len(latest_history)==1 and latest_history[0]['displayed_snapshot_id']==str(snapshots[1].id)
    counts=await facets(db,p,settings,{'q':marker,'doc_type':['bill_of_lading']})
    assert counts['status_counts']['all']==31 and counts['status_counts']['exported']==0 and counts['status_counts']['indexed']==32
    paused=ExportAttempt(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,review_fingerprint='0'*64,
        status='paused_reconcile',execution_epoch=1,created_by=p.user_id,created_at=now+timedelta(seconds=10))
    db.add(paused);await db.flush()
    failed=await query_runs(db,p,settings,{'q':marker,'tab':'failed'},limit=5)
    counts=await facets(db,p,settings,{'q':marker})
    assert len(failed)==1 and counts['status_counts']['failed']==1
    # 首批较新Run均无warning；只有第32个才命中，仍须返回它。
    db.add(Issue(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=business.id,category='fixture',
        severity='yellow',blocks_export=False,title='warning',description='engineering fixture',sources=[]))
    await db.flush()
    warning=await query_runs(db,p,settings,{'q':marker,'tab':'indexed','risk':['warning']},limit=2)
    assert len(warning)==1 and warning[0]['run_id']==str(run.id)
    from starlette.requests import Request
    from app.api.routers.conversation import references
    from urllib.parse import urlencode
    async def reference_search(params):
        request=Request({'type':'http','method':'GET','path':'/api/v1/search/references','query_string':urlencode(params).encode(),'headers':[]})
        async with AsyncSession(bind=db.bind,expire_on_commit=False,join_transaction_mode='create_savepoint') as endpoint_db:
            return await references(request,p,endpoint_db,settings,q=params.get('q',''),kind='snapshot',conversation_id=None,case_id=None,limit=30)
    frozen_refs=await reference_search({'q':marker,'doc_type':'bill_of_lading'})
    assert frozen_refs['items']==[]
    old_refs=await reference_search({'q':marker,'doc_type':'bill_of_lading','version_scope':'include_history'})
    assert len(old_refs['items'])==1 and old_refs['items'][0]['selection']['snapshot_id']==str(snapshots[0].id)


async def build_reference_sections(db, p):
    from app.documents.evidence_models import LogicalDocument, LogicalDocumentType, DocumentSection
    case, ids, projections, marker = await build_runs(db, p, 1)
    source = SourceDocument(tenant_id=p.tenant_id, case_id=case.id, current_version=1)
    obj = StoredObject(tenant_id=p.tenant_id, case_id=case.id, object_key=f't/{p.tenant_id}/c/{case.id}/reference-fixture', category=StoredObjectCategory.SOURCE)
    db.add_all([source, obj]); await db.flush()
    version = DocumentVersion(tenant_id=p.tenant_id, case_id=case.id, document_id=source.id, version=1,
        filename=marker+'.pdf', mime='application/pdf', size_bytes=0, sha256='0'*64, object_id=obj.id, uploaded_by=p.user_id)
    db.add(version); await db.flush()
    parses, sections = [], []
    for index, quality in enumerate((ParseQuality.VALID, ParseQuality.DEGRADED, ParseQuality.FAILED)):
        parse = ParseArtifact(tenant_id=p.tenant_id, case_id=case.id, document_id=source.id, document_version=1,
            parser_name='fixture', parser_version='1', parser_config_hash='0'*64, quality=quality, page_count=4,
            created_at=datetime.now(UTC)+timedelta(seconds=index), is_synthetic=True)
        db.add(parse); await db.flush(); parses.append(parse)
        local = []
        for ordinal, doc_type in enumerate((LogicalDocumentType.COMMERCIAL_INVOICE, LogicalDocumentType.PACKING_LIST)):
            logical = LogicalDocument(tenant_id=p.tenant_id, case_id=case.id, parse_artifact_id=parse.id, ordinal=ordinal,
                type_candidate=doc_type, title='混合单据'+str(ordinal), page_start=ordinal*2+1, page_end=ordinal*2+2)
            db.add(logical); await db.flush()
            section = DocumentSection(tenant_id=p.tenant_id, case_id=case.id, parse_artifact_id=parse.id, logical_document_id=logical.id,
                ordinal=0, kind='document', title='同名区段', page_start=logical.page_start, page_end=logical.page_end)
            db.add(section); await db.flush(); local.append(section)
        sections.append(local)
    original = await db.get(SearchProjection, projections[0]['id'])
    original.document_id=source.id; original.parse_artifact_id=parses[1].id; original.logical_document_id=sections[1][0].logical_document_id
    original.doc_type='bill_of_lading'; original.filename=version.filename
    db.add(SearchProjection(tenant_id=p.tenant_id, case_id=case.id, run_id=ids[0], document_id=source.id, document_version=1,
        parse_artifact_id=parses[1].id, logical_document_id=sections[1][1].logical_document_id, doc_type='packing_list',
        filename=version.filename, summary=marker, uploaded_at=version.created_at, uploaded_by=p.user_id, state='ready', dependency_fingerprint='0'*64))
    await db.flush()
    return case, await db.get(Run, ids[0]), source, parses, sections, marker


async def search_reference_fixture(db, p, settings, params, *, kind=None, case_id=None):
    from app.api.routers.conversation import references
    from app.api.response_dto import PageView, ReferenceCandidateView
    from starlette.requests import Request
    from urllib.parse import urlencode
    request=Request({'type':'http', 'method':'GET', 'path':'/api/v1/search/references',
        'query_string':urlencode(params, doseq=True).encode(), 'headers':[]})
    async with AsyncSession(bind=db.bind, expire_on_commit=False, join_transaction_mode='create_savepoint') as endpoint_db:
        page=await references(request,p,endpoint_db,settings,q=params.get('q',''),kind=kind,conversation_id=None,case_id=case_id,limit=50)
        PageView[ReferenceCandidateView].model_validate(page)
        return page


@pytest.mark.asyncio
async def test_reference_children_fixed_current_parse_and_logical_filter(fixture_db):
    from app.conversation.references import ReferenceSelection, freeze_references
    db,p,settings=fixture_db
    case,run,source,parses,sections,marker=await build_reference_sections(db,p)
    page=await search_reference_fixture(db,p,settings,{})
    parent=next(row for row in page['items'] if row['selection']['kind']=='document' and row['selection']['id']==str(source.id))
    assert parent['selection']['parse_artifact_id']==str(parses[1].id)
    assert {row['selection']['id'] for row in parent['children']}=={str(row.id) for row in sections[1]}
    assert all(row['selection']['selection_mode']=='fixed' for row in parent['children'])
    assert '混合单据0' in parent['children'][0]['title'] and '商业发票' in parent['children'][0]['title'] and '第 1–2 页' in parent['children'][0]['title']
    async with AsyncSession(bind=db.bind,expire_on_commit=False,join_transaction_mode='create_savepoint') as freeze_db:
        _,frozen=await freeze_references(freeze_db,selections=[ReferenceSelection.model_validate(parent['children'][0]['selection'])],
            bound_case_id=case.id,user_id=p.user_id,settings=settings)
    assert frozen[0]['parse_artifact_id']==str(parses[1].id) and frozen[0]['section_id']==str(sections[1][0].id)
    filtered=await search_reference_fixture(db,p,settings,{'q':marker,'doc_type':'bill_of_lading'})
    parent=next(row for row in filtered['items'] if row['selection']['kind']=='document')
    assert len(parent['children'])==1 and parent['children'][0]['selection']['id']==str(sections[1][0].id)
    assert '提单' in parent['children'][0]['title']
    standalone=await search_reference_fixture(db,p,settings,{'q':marker,'doc_type':'bill_of_lading'},kind='section')
    assert [row['selection'] for row in standalone['items']]==[parent['children'][0]['selection']]
    broad=await search_reference_fixture(db,p,settings,{'q':marker,'doc_type':'bill_of_lading','match_scope':'any_in_case'},kind='document')
    assert len(broad['items'][0]['children'])==2
    projection=(await db.execute(select(SearchProjection).where(SearchProjection.run_id==run.id,SearchProjection.doc_type=='bill_of_lading'))).scalar_one()
    projection.logical_document_id=None;await db.flush()
    legacy=await search_reference_fixture(db,p,settings,{'q':marker,'doc_type':'bill_of_lading'},kind='document')
    assert len(legacy['items'][0]['children'])==2
    from sqlalchemy import delete
    await db.execute(delete(SearchProjection).where(SearchProjection.run_id==run.id));await db.flush()
    metadata=await search_reference_fixture(db,p,settings,{'q':marker},kind='document')
    assert len(metadata['items'])==1 and len(metadata['items'][0]['children'])==2
    assert metadata['items'][0]['selection']['parse_artifact_id']==str(parses[1].id)
    explicit=await search_reference_fixture(db,p,settings,{'q':marker},kind='section')
    assert len(explicit['items'])==2


@pytest.mark.asyncio
async def test_reference_children_snapshot_freeze_does_not_mix_parse(fixture_db):
    from app.conversation.references import ReferenceSelection, freeze_references
    db,p,settings=fixture_db
    case,run,source,parses,sections,marker=await build_reference_sections(db,p)
    business=BusinessRevision(tenant_id=p.tenant_id,case_id=case.id,revision_no=1,reason='baseline_extraction',created_by=p.user_id)
    db.add(business);await db.flush()
    translation=TranslationRevision(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=business.id,target_language='zh-CN',revision_no=1,
        parse_artifact_ids=[],resolved_scope_ids=[],segment_ids=[],prompt_versions={},model_bindings=[],qa_status='passed',dependency_fingerprint='0'*64,is_synthetic=True)
    db.add(translation);await db.flush()
    confirmation=ReviewConfirmation(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,task_spec_hash='0'*64,review_fingerprint='0'*64,
        business_revision_id=business.id,translation_revision_id=translation.id,pairing_manifest={},review_manifest={},output_formats=['pdf'],confirmed_by=p.user_id)
    db.add(confirmation);await db.flush()
    snapshots=[]
    for index in range(2):
        attempt=ExportAttempt(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,review_fingerprint='0'*64,
            status='succeeded',execution_epoch=1,created_by=p.user_id)
        db.add(attempt);await db.flush()
        snapshot=ExportSnapshot(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,attempt_id=attempt.id,
            exported_by=p.user_id,exported_at=datetime.now(UTC)+timedelta(seconds=index),manifest={
                'task_spec_hash':'0'*64,'business_revision_id':str(business.id),'translation_revision_id':str(translation.id),'field_assertion_ids':[],
                'confirmed_by':str(p.user_id),'issue_ids':[],'issue_disposition_ids':[],
                'source_versions':[{'document_id':str(source.id),'document_version':1,'parse_artifact_id':str(parses[index].id)}]})
        db.add(snapshot);await db.flush();snapshots.append(snapshot)
        for section in sections[index]:
            db.add(SearchProjection(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,snapshot_id=snapshot.id,document_id=source.id,document_version=1,
                parse_artifact_id=parses[index].id,logical_document_id=section.logical_document_id,filename=marker+'.pdf',summary=marker,
                doc_type='bill_of_lading' if index==0 else 'packing_list',uploaded_at=datetime.now(UTC),uploaded_by=p.user_id,state='ready',dependency_fingerprint='0'*64))
    await db.flush()
    assert (await search_reference_fixture(db,p,settings,{'q':marker,'doc_type':'bill_of_lading'},kind='document'))['items']==[]
    params={'q':marker,'doc_type':'bill_of_lading','version_scope':'include_history'}
    history=await search_reference_fixture(db,p,settings,params,kind='document')
    assert len(history['items'])==1
    parent=history['items'][0]
    assert parent['selection']['parse_artifact_id']==str(parses[0].id) and parent['selection']['snapshot_id']==str(snapshots[0].id)
    assert {row['selection']['id'] for row in parent['children']}=={str(section.id) for section in sections[0]}
    assert all(row['selection']['snapshot_id']==str(snapshots[0].id) and row['selection']['run_id']==str(run.id) for row in parent['children'])
    standalone=await search_reference_fixture(db,p,settings,params,kind='section')
    assert [row['selection'] for row in standalone['items']]==[row['selection'] for row in parent['children']]
    async with AsyncSession(bind=db.bind,expire_on_commit=False,join_transaction_mode='create_savepoint') as freeze_db:
        _,frozen=await freeze_references(freeze_db,selections=[ReferenceSelection.model_validate(parent['children'][0]['selection'])],
            bound_case_id=case.id,user_id=p.user_id,settings=settings)
    assert frozen[0]['historical'] is True and frozen[0]['parse_artifact_id']==str(parses[0].id)
    assert frozen[1]['snapshot_id']==str(snapshots[0].id)
    # 损坏的历史检索投影不能借同文件版绑定到另一解析。
    old_projection=(await db.execute(select(SearchProjection).where(SearchProjection.snapshot_id==snapshots[0].id))).scalars().first()
    old_projection.parse_artifact_id=parses[1].id;await db.flush()
    history=await search_reference_fixture(db,p,settings,params,kind='document')
    assert len(history['items'])==1 and len(history['items'][0]['children'])==1
    assert history['items'][0]['children'][0]['selection']['parse_artifact_id']==str(parses[0].id)


@pytest.mark.asyncio
async def test_reference_children_unparsed_permission_expiry_and_cross_case(fixture_db):
    from app.documents.models import CaseAccessGrant
    db,p,settings=fixture_db
    case,run,source,parses,sections,marker=await build_reference_sections(db,p)
    other,_,_,_=await build_runs(db,p,1)
    rows=(await search_reference_fixture(db,p,settings,{},kind='document',case_id=other.id))['items']
    parent=next(row for row in rows if row['selection']['document_id']==str(source.id))
    assert parent['cross_case'] and not parent['selectable']
    assert all(child['cross_case'] and not child['selectable'] for child in parent['children'])
    version=DocumentVersion(tenant_id=p.tenant_id,case_id=case.id,document_id=source.id,version=2,
        filename='unparsed.pdf',mime='application/pdf',size_bytes=0,sha256='0'*64,
        object_id=(await db.execute(select(DocumentVersion.object_id).where(DocumentVersion.document_id==source.id))).scalar_one(),uploaded_by=p.user_id)
    db.add(version);await db.flush()
    rows=(await search_reference_fixture(db,p,settings,{},kind='document'))['items']
    unparsed=next(row for row in rows if row['selection']['document_version']==2)
    assert unparsed['children']==[] and 'parse_artifact_id' not in unparsed['selection']
    viewer=User(tenant_id=p.tenant_id,account='ref_'+uuid4().hex,display_name='fixture',password_hash='fixture')
    await db.execute(text('SET LOCAL ROLE NONE'))
    db.add(viewer);await db.flush()
    await db.execute(text('SET LOCAL ROLE logistics_app'))
    owner=case.owner_user_id;case.owner_user_id=viewer.id
    grant=CaseAccessGrant(tenant_id=p.tenant_id,case_id=case.id,subject_user_id=p.user_id,capabilities=['export'])
    db.add(grant);await db.flush()
    assert not any(row['selection']['document_id']==str(source.id) for row in (await search_reference_fixture(db,p,settings,{},kind='document'))['items'])
    grant.capabilities=['read'];await db.flush()
    assert sum(row['selection']['document_id']==str(source.id) for row in (await search_reference_fixture(db,p,settings,{},kind='document'))['items'])==2
    grant.expires_at=datetime.now(UTC)-timedelta(seconds=1);await db.flush()
    assert not any(row['selection']['document_id']==str(source.id) for row in (await search_reference_fixture(db,p,settings,{},kind='section'))['items'])
    case.owner_user_id=owner;case.retention_expires_at=datetime.now(UTC)-timedelta(seconds=1);await db.flush()
    assert not any(row['selection']['document_id']==str(source.id) for row in (await search_reference_fixture(db,p,settings,{},kind='document'))['items'])


@pytest.mark.asyncio
async def test_expiry_and_deletion_revoke_before_purge(fixture_db,tmp_path):
    from app.management.deletion import register_deletion
    from app.management.ledger import DeletionLedger
    db,p,settings=fixture_db
    case,ids,projections,marker=await build_runs(db,p,1)
    before=case.retention_expires_at
    assert len(await query_runs(db,p,settings,{'q':marker},limit=2))==1
    assert case.retention_expires_at==before
    case.retention_expires_at=datetime.now(UTC)-timedelta(seconds=1);await db.flush()
    assert not await query_runs(db,p,settings,{'q':marker},limit=2)
    with pytest.raises(CaseGone):await load_case_for_read(db,case.id,user_id=p.user_id,settings=settings)
    case.retention_expires_at=before;await db.flush()
    isolated=settings.model_copy(update={'deletion_ledger_path':str(tmp_path/'ledger'),'deletion_ledger_key':settings.deletion_ledger_key})
    deletion=await db.run_sync(lambda sync:register_deletion(sync,case,isolated,actor_id=p.user_id,key_hash='a'*64,request_hash='b'*64))
    await db.flush()
    assert deletion.purge_after>=deletion.requested_at+timedelta(seconds=max(settings.upload_signed_url_ttl_seconds,settings.workflow_task_hard_limit_seconds)+settings.purge_inflight_grace_seconds)
    assert not await query_runs(db,p,isolated,{'q':marker},limit=2)
    with pytest.raises(CaseGone):await load_case_for_read(db,case.id,user_id=p.user_id,settings=isolated)
    assert DeletionLedger(isolated).is_denied(p.tenant_id,case.id)
    assert (await db.get(Run,ids[0])).status==RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_summary_dependency_rebuild_and_snapshot_freeze(fixture_db):
    from app.management.index import summary_fingerprint
    from app.review.models import IssueDisposition
    db,p,settings=fixture_db
    case,ids,projections,marker=await build_runs(db,p,1)
    run=await db.get(Run,ids[0])
    original=await db.run_sync(lambda sync:summary_fingerprint(sync,run))
    run.failure_reason="解析结果待核查";await db.flush()
    assert await db.run_sync(lambda sync:summary_fingerprint(sync,run))!=original
    business=BusinessRevision(tenant_id=p.tenant_id,case_id=case.id,revision_no=1,reason="baseline_extraction",created_by=p.user_id)
    db.add(business);await db.flush();run.current_business_revision_id=business.id
    translation=TranslationRevision(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=business.id,target_language="en",revision_no=1,
        parse_artifact_ids=[],resolved_scope_ids=[],segment_ids=[],prompt_versions={},model_bindings=[],qa_status="passed",dependency_fingerprint="0"*64)
    db.add(translation);await db.flush();run.current_translation_revision_id=translation.id
    current=await db.run_sync(lambda sync:summary_fingerprint(sync,run))
    translation.qa_status="failed";await db.flush()
    assert await db.run_sync(lambda sync:summary_fingerprint(sync,run))!=current
    issue=Issue(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=business.id,category="quality",severity="warning",blocks_export=False,title="疑点",description="fixture",sources=[])
    db.add(issue);await db.flush()
    current=await db.run_sync(lambda sync:summary_fingerprint(sync,run))
    disposition=IssueDisposition(tenant_id=p.tenant_id,case_id=case.id,issue_id=issue.id,business_revision_id=business.id,review_sequence=1,
        action="accepted_risk",reason="fixture",decided_by=p.user_id)
    db.add(disposition);await db.flush()
    assert await db.run_sync(lambda sync:summary_fingerprint(sync,run))!=current
    confirmation=ReviewConfirmation(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,task_spec_hash="0"*64,review_fingerprint="0"*64,
        business_revision_id=business.id,translation_revision_id=translation.id,pairing_manifest={},review_manifest={},output_formats=["pdf"],confirmed_by=p.user_id)
    db.add(confirmation);await db.flush()
    attempt=ExportAttempt(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,review_fingerprint="0"*64,status="failed_retryable",execution_epoch=1,created_by=p.user_id)
    db.add(attempt);await db.flush()
    current=await db.run_sync(lambda sync:summary_fingerprint(sync,run))
    attempt.error_summary="下载回检失败";await db.flush()
    assert await db.run_sync(lambda sync:summary_fingerprint(sync,run))!=current
    snapshot=ExportSnapshot(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,confirmation_id=confirmation.id,attempt_id=attempt.id,
        exported_by=p.user_id,manifest={"business_revision_id":str(business.id),"translation_revision_id":str(translation.id),"source_versions":[],"field_assertion_ids":[],"field_review_event_ids":[],"section_assignment_ids":[],"issue_ids":[],"issue_disposition_ids":[],"target_language":"en","translation_scope":"all"})
    db.add(snapshot);await db.flush()
    frozen=await db.run_sync(lambda sync:summary_fingerprint(sync,run,snapshot))
    run.failure_reason="当前工作版失败";translation.qa_status="changed";await db.flush()
    assert await db.run_sync(lambda sync:summary_fingerprint(sync,run,snapshot))==frozen
    value=await summary(db,run,case,snapshot)
    assert "当前工作版失败" not in value["summary"] and value["dependencies"]["snapshot_id"]==str(snapshot.id)
    assert "download" not in str(value["dependencies"]).lower()
    await db.run_sync(lambda sync:delete_domain_rows(sync,case.id,uuid4()))
    assert not any((await db.run_sync(lambda sync:selected_rows(sync,case.id)))[1].values())
