from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import evidence_models as em
from app.documents.models import Case
from app.review.models import RuleInputDecision
from app.review.rules import RULE_PACK_V3, evaluate_rules
from app.workflows.models import Run, RunDocument, TaskDraft
from tests.test_workflow_response_contracts import http_db, seed_source


def mapping_inputs():
    fields = []
    sources = []
    for index, (role, value) in enumerate([('seller', 'ACME'), ('shipper', 'OTHER')]):
        fields.append({'id': f'f{index}', 'logical_document_id': f'L{index}', 'field_key': 'name', 'role': role, 'value': value, 'source_segment_ids': [f's{index}']})
        sources.append({'id': f's{index}', 'logical_document_id': f'L{index}', 'text': value})
    decision = {'decision_id': 'D', 'kind': 'party_mapping', 'scope_key': 'entity-1', 'decided_by': 'actor', 'basis': 'explicit contract',
        'payload': {'members': [{'logical_document_id': 'L0', 'role': 'seller'}, {'logical_document_id': 'L1', 'role': 'shipper'}], 'compare_fields': ['name'], 'field_assertion_ids': ['f0', 'f1']}}
    return fields, sources, decision


def test_mapping_confirmed_scope_preserves_roles_and_missing_sources_unknown():
    fields, sources, decision = mapping_inputs()
    def check(rows, source_rows):
        return next(result for result in evaluate_rules(documents=[],fields=rows,lines=[],source_rows=source_rows,rule_inputs=[decision],rule_pack=RULE_PACK_V3) if result.rule_id == 'party.confirmed_mapping.name')
    assert check(fields, sources).status == 'failed'
    assert check(fields[:1], sources).status == 'undetermined'
    assert check(fields, sources[:1]).status == 'undetermined'
    fields[1]['value'] = 'ACME'; sources[1]['text'] = 'ACME'
    assert check(fields, sources).status == 'passed'


def test_confirmed_multicolumn_packing_complete_and_mixed_layers_unknown():
    cells=[('Complete consignment packing detail', 'CTNS', 'SKU'), ('Shirts','2','A'), ('Trousers','3','B'), ('TOTAL','5','')]
    sources=[{'id':f's{r}{c}','logical_document_id':'L','section_id':'S','structure':'table_cell','table_row':r,'table_col':c,'text':value} for r,row in enumerate(cells) for c,value in enumerate(row)]
    lines=[{'id':f'l{r}','logical_document_id':'L','document_type':'packing_list','package_count':Decimal(count),'package_unit':'CTNS','source_segment_ids':[f's{r}0',f's{r}1']} for r,count in [(1,'2'),(2,'3')]]
    documents=[{'id':'parse','page_count':1,'seen_pages':[1],'segment_count':12,'logical_documents':[{'logical_document_id':'L','document_type':'packing_list'}]}]
    decision={'decision_id':'D','kind':'packing_table','scope_key':'complete-L','decided_by':'actor','basis':'original scope', 'payload':{'logical_document_id':'L','section_id':'S','description_column':0,'count_column':1,'header_row':0,'total_row':3,'packaging_level':'carton','scope_source_segment_ids':['s00'],'complete':True}}
    def check():
        return next(r for r in evaluate_rules(documents=documents,fields=[],lines=lines,source_rows=sources,rule_inputs=[decision],rule_pack=RULE_PACK_V3) if r.rule_id=='packing.confirmed_table_total')
    assert check().status=='passed'
    sources[-2]['text']='6'; assert check().status=='failed'
    sources[2]['text']='PLTS'; assert check().status=='undetermined'


@pytest.mark.asyncio
async def test_real_rule_input_http_cas_sources_history_rls_and_rollback(http_db):
    http, conn, p, created = http_db
    case_id, document_id = await seed_source(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            parse = em.ParseArtifact(tenant_id=p.tenant_id,case_id=UUID(case_id),document_id=UUID(document_id),document_version=1,
                parser_name='fixture',parser_version='1',parser_config_hash='0'*64,quality=em.ParseQuality.VALID)
            db.add(parse); await db.flush()
            logical = em.LogicalDocument(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,ordinal=0,type_candidate=em.LogicalDocumentType.COMMERCIAL_INVOICE)
            db.add(logical); await db.flush()
            section=em.DocumentSection(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,logical_document_id=logical.id,ordinal=0,kind="body")
            db.add(section); await db.flush()
            fields=[]
            for role,value in [('seller','ACME'),('shipper','OTHER')]:
                source=em.SourceSegment(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,logical_document_id=logical.id,section_id=section.id,ordinal=len(fields),text=value,structure='paragraph')
                db.add(source); await db.flush()
                field=em.FieldAssertion(tenant_id=p.tenant_id,case_id=UUID(case_id),parse_artifact_id=parse.id,logical_document_id=logical.id,field_key='name',role=role,group='parties',candidate_value=value,source_segment_ids=[str(source.id)])
                db.add(field); fields.append(field)
            revision=em.BusinessRevision(tenant_id=p.tenant_id,case_id=UUID(case_id),revision_no=1,reason='baseline_extraction',created_by=p.user_id)
            db.add(revision); await db.flush()
            for field in fields:db.add(em.BusinessRevisionField(tenant_id=p.tenant_id,business_revision_id=revision.id,field_assertion_id=field.id))
            case=await db.get(Case,UUID(case_id)); case.current_business_revision_id=revision.id;case.business_revision_no=1
            draft=TaskDraft(tenant_id=p.tenant_id,case_id=case.id,created_by=p.user_id,workflow_type="translate_review",workflow_version=1,source_versions=[],draft_parameters={},status="ready",missing_fields=[],questions=[],task_summary="test",expires_at=datetime.now(UTC)+timedelta(hours=1))
            db.add(draft);await db.flush()
            run=Run(draft_task_id=draft.id,tenant_id=p.tenant_id,case_id=case.id,workflow_type='translate_review',workflow_version=1,task_spec={'rule_pack_version':'core-document@3'},task_spec_hash='0'*64,prompt_versions={},initiated_by=p.user_id,status='awaiting_review',current_stage='待复核',current_business_revision_id=revision.id)
            db.add(run);await db.flush();db.add(RunDocument(tenant_id=p.tenant_id,case_id=case.id,run_id=run.id,ordinal=0,document_id=UUID(document_id),document_version=1))
            body={'run_id':str(run.id),'expected_business_revision_id':str(revision.id),'expected_review_state_version':case.review_state_version,'kind':'party_mapping','scope_key':'party-1',
                'payload':{'members':[{'logical_document_id':str(logical.id),'role':'seller'},{'logical_document_id':str(logical.id),'role':'shipper'}],'compare_fields':['name']},'basis':'合同指明卖方与托运方同一实体（测试）','reason':'显式确认（回滚样本）'}
    key=uuid4().hex;path=f'/api/v1/cases/{case_id}/rule-inputs'
    response=await http.post(path,headers={'Idempotency-Key':key},json=body)
    assert response.status_code==200,response.text
    result=response.json();assert result['business_revision_id']!=body['expected_business_revision_id']
    replay=await http.post(path,headers={'Idempotency-Key':key},json=body);assert replay.json()==result
    stale=await http.post(path,headers={'Idempotency-Key':uuid4().hex},json=body);assert stale.status_code==409
    view=await http.get(path);assert view.status_code==200 and view.json()['items'][0]['decided_by']==str(p.user_id)
    old=await http.get(path,params={'business_revision_id':body['expected_business_revision_id']});assert old.json()['items']==[]
    await conn.execute(text('SET LOCAL ROLE NONE'))
    other=uuid4();await conn.execute(text(f"SET LOCAL app.tenant_id = '{other}'"));await conn.execute(text('SET LOCAL ROLE logistics_app'))
    assert list((await conn.execute(select(RuleInputDecision.id).where(RuleInputDecision.case_id==UUID(case_id)))).scalars())==[]
    await conn.execute(text(f"SET LOCAL app.tenant_id = '{p.tenant_id}'"))


def test_date_sequence_only_with_explicit_same_event_calendar_and_timezone():
    rows=[{'id':'depart','logical_document_id':'L','field_key':'departure_date','value':'2026-10-03','source_segment_ids':['s1']},
          {'id':'arrive','logical_document_id':'L','field_key':'arrival_date','value':'2026-10-02','source_segment_ids':['s2']}]
    sources=[{'id':'s1','logical_document_id':'L','text':'2026-10-03'},{'id':'s2','logical_document_id':'L','text':'2026-10-02'}]
    decision={'decision_id':'D','kind':'date_transport','scope_key':'trip1','decided_by':'actor','basis':'same voyage confirmed',
        'payload':{'field_assertion_ids':['depart','arrive'],'departure_field_id':'depart','arrival_field_id':'arrive','calendar':'gregorian','timezone':'Asia/Shanghai'}}
    def check(inputs):
        return next(r for r in evaluate_rules(documents=[],fields=rows,lines=[],source_rows=sources,rule_inputs=inputs,rule_pack=RULE_PACK_V3) if r.rule_id=='date.transport_sequence')
    assert check([]).status=='undetermined'
    assert check([decision]).status=='failed'
    rows[1]['value']='2026-10-04';sources[1]['text']='2026-10-04'
    assert check([decision]).status=='passed'
    sources[1]['logical_document_id']='OTHER';assert check([decision]).status=='undetermined'
