"""管理草稿与整票删除真实HTTP/PG回滚，独立删除账本只写临时目录。"""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_settings as settings_dep
from app.config import get_settings
from app.documents.models import UploadSession, UploadSessionStatus
from tests.test_workflow_response_contracts import http_db, seed_source


async def test_draft_listing_delete_replay_version_and_scope(http_db, tmp_path):
    http, conn, principal, created = http_db
    settings = get_settings().model_copy(update={"deletion_ledger_path": str(tmp_path / 'ledger')})
    http._transport.app.dependency_overrides[settings_dep] = lambda: settings
    case_id, _ = await seed_source(conn, principal, created)
    second, _ = await seed_source(conn, principal, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
        async with db.begin():
            session = UploadSession(tenant_id=principal.tenant_id, case_id=UUID(case_id),
                created_by=principal.user_id, disclosure_version_ack="test-v1", disclosure_acked_at=datetime.now(UTC), status=UploadSessionStatus.COMPLETED)
            db.add(session)
            await db.flush()
            session_id = str(session.id)
    draft_response = await http.get('/api/v1/upload-sessions?status=drafts')
    assert draft_response.status_code == 200, draft_response.text
    assert session_id in {item['upload_session_id'] for item in draft_response.json()['items']}
    assert session_id not in {item['upload_session_id'] for item in (await http.get('/api/v1/upload-sessions?status=in_progress')).json()['items']}
    preview = (await http.get(f'/api/v1/cases/{case_id}/deletion-preview')).json()
    assert preview['allowed'] and preview['source_count'] == 1 and preview['run_count'] == 0
    stale = await http.post(f'/api/v1/cases/{case_id}/delete', json={'expected_case_version':preview['case_version'] + 1,'confirm_short_code':preview['short_code']}, headers={'Idempotency-Key':str(uuid4())})
    assert stale.status_code == 409, stale.text
    key=str(uuid4())
    body={'expected_case_version':preview['case_version'],'confirm_short_code':preview['short_code']}
    deleted=await http.post(f'/api/v1/cases/{case_id}/delete', json=body, headers={'Idempotency-Key':key})
    assert deleted.status_code == 202, deleted.text
    replay=await http.post(f'/api/v1/cases/{case_id}/delete',json=body,headers={'Idempotency-Key':key})
    assert replay.status_code == 202 and replay.json()['deletion_id'] == deleted.json()['deletion_id']
    assert (await http.get(f'/api/v1/cases/{case_id}')).status_code == 410
    assert session_id not in {item['upload_session_id'] for item in (await http.get('/api/v1/upload-sessions?status=drafts')).json()['items']}
    assert (await http.get(f'/api/v1/cases/{second}')).status_code == 200
    assert (await http.get(f'/api/v1/cases/{uuid4()}/deletion-preview')).status_code == 404
    facets=await http.get('/api/v1/runs/facets')
    assert facets.status_code == 200, facets.text
    assert isinstance(facets.json()['status_counts']['drafts'],int)
