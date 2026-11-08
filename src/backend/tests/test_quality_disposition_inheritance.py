"""未变化单段的相同确定性质量结论保留人工处置；真实PG回滚。"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.models import Case
from app.review import models as rm
from app.workflows.models import Run
from app.workflows.translation import TranslationNodes
from tests.test_review_field_gate import seed_fields
from tests.test_workflow_response_contracts import http_db


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['none', 'segment', 'conclusion', 'rule', 'current_decision'])
async def test_unchanged_quality_disposition_is_carried_once_and_never_overrides_new_evidence(http_db, changed):
    _, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db, db.begin():
        run = await db.get(Run, ids['run_id'])
        segment = rm.TranslationSegment(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'], section_id=ids['section_id'],
            target_language='zh-CN', ordinal=0, source_segment_ids=[str(ids['source_id'])], translated_text='。',
            protected_tokens={}, state='warning', dependency_fingerprint='0'*64)
        db.add(segment); await db.flush()
        summary = {'deterministic_qa_version': 'translation-deterministic@2', 'target_language_defects': {str(segment.id): ['target_language_missing']}}
        parent = rm.TranslationRevision(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            business_revision_id=ids['revision_id'], target_language='zh-CN', revision_no=5, parse_artifact_ids=[str(ids['parse_id'])],
            resolved_scope_ids=[], segment_ids=[str(segment.id)], prompt_versions={}, model_bindings=[], qa_status='warning',
            qa_summary=summary, dependency_fingerprint='1'*64)
        db.add(parent); await db.flush()
        current_segment = segment
        if changed == 'segment':
            current_segment = rm.TranslationSegment(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
                document_id=UUID(ids['document_id']), document_version=1, parse_artifact_id=ids['parse_id'], section_id=ids['section_id'],
                target_language='zh-CN', ordinal=0, source_segment_ids=[str(ids['source_id'])], translated_text='。',
                protected_tokens={}, state='warning', supersedes_segment_id=segment.id, dependency_fingerprint='2'*64)
            db.add(current_segment); await db.flush()
        current = rm.TranslationRevision(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            business_revision_id=ids['revision_id'], target_language='zh-CN', revision_no=6, parent_revision_id=parent.id,
            parse_artifact_ids=[str(ids['parse_id'])], resolved_scope_ids=[], segment_ids=[str(current_segment.id)],
            prompt_versions={}, model_bindings=[], qa_status='warning', qa_summary={**summary,
                'deterministic_qa_version': 'translation-deterministic@3' if changed == 'rule' else 'translation-deterministic@2'}, dependency_fingerprint='3'*64)
        db.add(current); await db.flush()
        def sources(value):
            return [{'target': {'parse_artifact_id': str(ids['parse_id']), 'source_segment_id': str(ids['source_id'])}},
                {'target': {'translation_segment_id': str(value)}}]
        old = rm.Issue(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            business_revision_id=ids['revision_id'], translation_revision_id=parent.id, category='translation_quality',
            severity='red', blocks_export=True, title='对应译段需修订', description='目标语言正文不足', sources=sources(segment.id))
        issue = rm.Issue(tenant_id=principal.tenant_id, case_id=run.case_id, run_id=run.id,
            business_revision_id=ids['revision_id'], translation_revision_id=current.id, category='translation_quality',
            severity='red', blocks_export=True, title='对应译段需修订',
            description='新增漏译结论' if changed == 'conclusion' else old.description, sources=sources(current_segment.id))
        db.add_all([old, issue]); await db.flush()
        case = await db.get(Case, run.case_id); case.review_state_version += 1
        original_time = datetime.now(UTC) - timedelta(days=1)
        db.add(rm.IssueDisposition(tenant_id=principal.tenant_id, case_id=case.id, issue_id=old.id,
            business_revision_id=ids['revision_id'], action='ignore', reason='相邻译段已译出额外含义，尾段只补句号', basis='已核对跨行原译文',
            review_sequence=case.review_state_version, decided_by=principal.user_id, created_at=original_time))
        if changed == 'current_decision':
            case.review_state_version += 1
            db.add(rm.IssueDisposition(tenant_id=principal.tenant_id, case_id=case.id, issue_id=issue.id,
                business_revision_id=ids['revision_id'], action='mark-unresolved', reason='当前仍需核验',
                review_sequence=case.review_state_version, decided_by=principal.user_id))
        await db.flush()
        def carry(sync):
            nodes = TranslationNodes(SimpleNamespace(tenant=principal.tenant_id, case_id=case.id))
            nodes._carry_quality_disposition(sync, run, current, current_segment, issue)
            sync.flush()
            nodes._carry_quality_disposition(sync, run, current, current_segment, issue)
            sync.flush()
        await db.run_sync(carry)
        dispositions = (await db.execute(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id == issue.id))).scalars().all()
        if changed == 'none':
            assert len(dispositions) == 1 and dispositions[0].action == 'ignore'
            assert issue.supersedes_issue_id == old.id and '相邻译段已译出额外含义' in dispositions[0].reason
            assert dispositions[0].basis == '已核对跨行原译文' and dispositions[0].decided_by == principal.user_id
            assert dispositions[0].created_at == original_time
        elif changed == 'current_decision':
            assert len(dispositions) == 1 and dispositions[0].action == 'mark-unresolved' and issue.supersedes_issue_id is None
        else:
            assert dispositions == [] and issue.supersedes_issue_id is None
