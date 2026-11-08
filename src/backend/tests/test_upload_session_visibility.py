"""U03真实数据库回滚核验：撤权前置过滤、进行中游标与身份绑定。"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import InvalidRequest
from app.api.routers.uploads import list_upload_sessions
from app.config import get_settings
from app.documents.models import Case, UploadSession, UploadSessionStatus
from app.identity.models import User
from app.infra.database import create_database_engine
from app.management.ledger import DeletionLedger


@pytest.fixture
async def upload_scope(tmp_path):
    settings = get_settings().model_copy(update={"deletion_ledger_path": str(tmp_path / "ledger")})
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                user = (await db.execute(select(User).where(User.account == "demo"))).scalar_one()
                principal = SimpleNamespace(user_id=user.id, tenant_id=user.tenant_id, display_name=user.display_name)
                await db.rollback()
                yield db, principal, settings
            finally:
                await db.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


async def populate(db, principal, settings):
    now = datetime.now(UTC)
    sessions = []
    async with db.begin():
        await db.execute(text("SET LOCAL ROLE logistics_app"))
        await db.execute(text(f"SET LOCAL app.tenant_id = '{principal.tenant_id}'"))
        for i in range(9):
            case = Case(tenant_id=principal.tenant_id, owner_user_id=principal.user_id, name="U03回滚样件",
                short_code=uuid4().hex[:16], last_material_event_at=now,
                retention_expires_at=now + timedelta(days=30) if i < 7 else now - timedelta(seconds=1))
            db.add(case)
            await db.flush()
            session = UploadSession(tenant_id=principal.tenant_id, case_id=case.id, created_by=principal.user_id,
                disclosure_version_ack="fixture", disclosure_acked_at=now,
                status=UploadSessionStatus.OPEN if i != 4 else UploadSessionStatus.COMPLETED,
                created_at=now - timedelta(microseconds=9 - i))
            db.add(session)
            await db.flush()
            if i in (5, 6):
                # 仅持久删除意图，Case仍ACTIVE：覆盖删除事务回滚后的读取边界。
                DeletionLedger(settings).append(deletion_id=uuid4(), tenant_id=case.tenant_id,
                    case_id=case.id, requested_at=now, state="intent")
            sessions.append(session)
    return sessions


async def test_upload_list_filters_before_paging_and_freezes_watermark(upload_scope):
    db, principal, settings = upload_scope
    sessions = await populate(db, principal, settings)
    first = await list_upload_sessions(principal, db, settings, limit=2, status="in_progress", cursor=None)
    assert [item.upload_session_id for item in first.items] == [str(sessions[3].id), str(sessions[2].id)]
    assert first.has_more and first.next_cursor
    second = await list_upload_sessions(principal, db, settings, limit=2, status="in_progress", cursor=first.next_cursor)
    assert [item.upload_session_id for item in second.items] == [str(sessions[1].id), str(sessions[0].id)]
    assert second.as_of == first.as_of
    assert all(item.case_id not in {str(s.case_id) for s in sessions[4:]} for item in first.items + second.items)
    with pytest.raises(InvalidRequest):
        await list_upload_sessions(principal, db, settings, limit=2, status="completed", cursor=first.next_cursor)
    with pytest.raises(InvalidRequest):
        await list_upload_sessions(SimpleNamespace(**{**vars(principal), "user_id": uuid4()}), db, settings,
            limit=2, status="in_progress", cursor=first.next_cursor)


async def test_upload_cursor_invalid_encoding_uses_business_error(upload_scope):
    db, principal, settings = upload_scope
    for cursor in ("invalid", "W10=", "eyJzY29wZSI6bnVsbH0="):
        with pytest.raises(InvalidRequest):
            await list_upload_sessions(principal, db, settings, limit=2, status="in_progress", cursor=cursor)


async def test_upload_cursor_rejects_invalid_shapes_and_future_anchor(upload_scope):
    import base64
    import json
    from app.api.idempotency import request_digest
    db, principal, settings = upload_scope
    now = datetime.now(UTC)
    valid = {"scope": request_digest({"tenant": principal.tenant_id, "user": principal.user_id, "status": "all"}),
        "as_of": now.isoformat(), "last": [now.isoformat(), str(uuid4())]}
    malformed = [None, 1, [], *[{**valid, "last": value} for value in (None, 1, {}, [], [now.isoformat()])],
        *[{**valid, "last": [now.isoformat(), value]} for value in (None, 1, [], {})],
        {**valid, "as_of": []}, {**valid, "last": [(now + timedelta(seconds=1)).isoformat(), str(uuid4())]},
        {**valid, "last": [now.replace(tzinfo=None).isoformat(), str(uuid4())]}]
    for value in malformed:
        cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        with pytest.raises(InvalidRequest):
            await list_upload_sessions(principal, db, settings, cursor=cursor)
