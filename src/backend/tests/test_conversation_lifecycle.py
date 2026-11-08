"""会话管理真实HTTP/数据库回滚，不调用外部模型。"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation.models import Conversation
from app.workflows.models import TaskDraft
from tests.test_workflow_response_contracts import http_db, seed_source


async def test_archive_restore_delete_scope_and_purge_preserve_case(http_db, monkeypatch):
    from app.conversation.lifecycle import purge_deleted_conversations
    from app.documents.models import Case
    from app.config import get_settings
    from contextlib import contextmanager
    http, conn, p, created = http_db
    case_id, _ = await seed_source(conn, p, created)
    response = await http.post('/api/v1/conversations',json={"initial_reference_selections":[{"kind":"case","id":case_id,"case_id":case_id,"selection_mode":"current"}]},headers={"Idempotency-Key":str(uuid4())})
    assert response.status_code == 201, response.text
    item=response.json(); cid=item['conversation_id']
    async def command(action,version,key=None):
        url=f'/api/v1/conversations/{cid}'+('' if action=='delete' else '/'+action)
        return await http.request('DELETE' if action=='delete' else 'POST',url,json={"expected_conversation_version":version},headers={"Idempotency-Key":key or str(uuid4())})
    assert (await command('delete',1)).status_code == 409
    archive=await command('archive',1);assert archive.status_code==200,archive.text
    assert archive.json()['archived_at'] and archive.json()['status']=='active'
    assert cid not in {c['conversation_id'] for c in (await http.get('/api/v1/conversations')).json()['items']}
    assert cid in {c['conversation_id'] for c in (await http.get('/api/v1/conversations?archived=true')).json()['items']}
    assert (await http.post(f'/api/v1/conversations/{cid}/turns',json={"text":"你好"},headers={"Idempotency-Key":str(uuid4())})).status_code==409
    assert (await command('restore',1)).status_code==409
    restore=await command('restore',2);assert restore.status_code==200
    assert restore.json()['archived_at'] is None and restore.json()['status']=='active'
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        other=Conversation(tenant_id=p.tenant_id,owner_user_id=uuid4(),title='其他用户',status='active',archived_at=datetime.now(UTC))
        db.add(other);await db.flush();other_id=other.id
    assert (await http.post(f'/api/v1/conversations/{other_id}/restore',json={"expected_conversation_version":1},headers={"Idempotency-Key":str(uuid4())})).status_code==404
    assert (await command('archive',3)).status_code==200
    key=str(uuid4()); assert (await command('delete',4,key)).status_code==204
    assert (await command('delete',4,key)).status_code==204
    assert (await http.get(f'/api/v1/conversations/{cid}')).status_code==404
    assert (await command('restore',5)).status_code==404
    cleaned=[]
    @contextmanager
    def checkpointer(url):
        yield type('Saver',(),{'delete_thread':lambda self,thread:cleaned.append(thread)})()
    monkeypatch.setattr('app.infra.checkpoints.open_checkpointer',checkpointer)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode='create_savepoint') as db,db.begin():
        assert await db.run_sync(lambda sync:purge_deleted_conversations(sync,get_settings(),datetime.now(UTC)+timedelta(days=1),50))==1
        assert cleaned and await db.get(Conversation,cid) is None
        assert await db.get(Case,case_id) is not None
