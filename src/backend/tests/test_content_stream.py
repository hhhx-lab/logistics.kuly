"""私有代理首块预检、长度与客户端生命周期的定向验证。"""
from types import SimpleNamespace

import httpx
import pytest
import anyio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from app.api.errors import ApiError, api_error_handler, NotFoundOrForbidden, ServiceUnavailable
from app.infra import content_stream


class Store:
    def __init__(self, data=b"abcdefgh", *, missing=False, short=False):
        self.data, self.missing, self.short = data, missing, short
        self.reads, self.closes = [], 0

    def head(self, key):
        return None if self.missing else SimpleNamespace(size_bytes=len(self.data))

    def read_range(self, key, start, end):
        self.reads.append((start, end))
        return self.data[start:end] if self.short else self.data[start:end+1]

    def close(self):
        self.closes += 1


class Connected:
    async def is_disconnected(self):
        return False


@pytest.fixture
def authorize(monkeypatch):
    async def yes(*args):
        return True
    monkeypatch.setattr(content_stream, "authorized", yes)


@pytest.mark.parametrize("start,end,expected", [(0,7,b"abcdefgh"),(2,5,b"cdef")])
async def test_prepared_full_and_range_are_exact_and_do_not_close_shared_store(authorize,start,end,expected):
    store = Store()
    stream = await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",start=start,end=end,expected_size=8,chunk_size=3)
    assert store.reads == [(start,min(end,start+2))]
    assert b"".join([chunk async for chunk in stream]) == expected
    assert store.closes == 0


@pytest.mark.parametrize("options,error", [({"missing":True},NotFoundOrForbidden),({"short":True},ServiceUnavailable)])
async def test_preflight_errors_are_raised_before_response_start_and_owned_store_closes(authorize,options,error):
    store = Store(**options)
    with pytest.raises(error):
        await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",end=7,expected_size=8,close_store=True)
    assert store.closes == 1


async def test_verified_size_mismatch_does_not_read_or_serve_changed_object(authorize):
    store = Store()
    with pytest.raises(ServiceUnavailable):
        await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",end=6,expected_size=7)
    assert store.reads == [] and store.closes == 0


async def test_owned_store_closes_after_consumption(authorize):
    store = Store()
    stream = await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",end=7,expected_size=8,close_store=True)
    assert b"".join([chunk async for chunk in stream]) == store.data
    assert store.closes == 1


async def test_empty_first_chunk_is_not_reported_as_success(authorize):
    store=Store()
    store.read_range=lambda *args:b""
    with pytest.raises(ServiceUnavailable):
        await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",end=7,expected_size=8)


async def test_empty_later_chunk_aborts_instead_of_finishing_short_body(authorize):
    store=Store()
    original=store.read_range
    store.read_range=lambda key,start,end:original(key,start,end) if start==0 else b""
    stream=await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",end=7,expected_size=8,chunk_size=4,close_store=True)
    assert await anext(stream) == b"abcd"
    with pytest.raises(RuntimeError,match="length"):
        await anext(stream)
    assert store.closes == 1


async def test_revocation_after_first_read_prevents_any_content(monkeypatch):
    checks=iter([True,False])
    async def check(*args):return next(checks)
    monkeypatch.setattr(content_stream,"authorized",check)
    store=Store()
    with pytest.raises(NotFoundOrForbidden):
        await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",end=7,expected_size=8,close_store=True)
    assert store.reads == [(0,7)] and store.closes == 1


async def test_revocation_stops_next_read_and_never_yields_more_private_bytes(monkeypatch):
    checks = iter([True,True,True,False])
    async def check(*args):
        return next(checks)
    monkeypatch.setattr(content_stream,"authorized",check)
    store = Store()
    stream = await content_stream.prepared_bytes(Connected(),None,None,None,store=store,key="fixture",end=7,expected_size=8,chunk_size=4)
    assert [chunk async for chunk in stream] == [b"abcd"]
    assert store.reads == [(0,3)]


async def test_missing_object_returns_complete_error_response_through_http_middleware(authorize):
    app=FastAPI()
    app.add_exception_handler(ApiError,api_error_handler)
    @app.middleware("http")
    async def context(request,call_next):
        return await call_next(request)
    @app.get("/content")
    async def content(request:Request):
        stream=await content_stream.prepared_bytes(request,None,None,None,store=Store(missing=True),key="fixture",end=7,expected_size=8)
        return StreamingResponse(stream,headers={"Content-Length":"8"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response=await client.get("/content")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert int(response.headers["content-length"]) == len(response.content)


async def test_anyio_download_cancellation_closes_auth_session_and_does_not_return_success(monkeypatch):
    from uuid import uuid4
    from sqlalchemy import text
    from app.config import get_settings
    from app.infra.database import create_database_engine,create_session_factory
    entered=anyio.Event()
    principal=SimpleNamespace(user_id=uuid4(),tenant_id=uuid4())
    engine=create_database_engine(get_settings())
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=create_session_factory(engine))),cookies={})
    async def resolve(db,*args):
        await db.connection()
        entered.set()
        await db.execute(text("SELECT pg_sleep(0.02)"))
        return principal
    async def complete(db,*args,**kwargs):await db.execute(text("SELECT 1"))
    monkeypatch.setattr(content_stream,"resolve_session",resolve)
    monkeypatch.setattr(content_stream,"set_tenant_context",complete)
    monkeypatch.setattr(content_stream,"load_case_for_read",complete)
    outcomes=[]
    async def check():outcomes.append(await content_stream.authorized(request,principal,None,None))
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(check)
            await entered.wait()
            group.cancel_scope.cancel()
        assert engine.pool.checkedout() == 0
        assert outcomes == []
    finally:await engine.dispose()
