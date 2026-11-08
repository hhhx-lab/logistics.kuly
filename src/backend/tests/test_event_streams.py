"""
SSE 事件流定向测试。

验证：先快照后订阅不丢事件、`after_seq` 与 `Last-Event-ID` 游标取较新者、
按序输出且不重复、流在终态自然结束、另一用户订阅被立即关闭、心跳不占序号。
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.api.main import create_app
from app.api.routers.events import router as events_router
from app.api.sse import resolve_cursor
from app.identity.seed import DEMO_PASSWORD, seed

ORIGIN = "http://localhost:5173"
PDF_MIME = "application/pdf"


def _pdf(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


def _parse_sse(raw: str) -> list[dict]:
    """把 SSE 文本拆成事件；注释行（心跳）不计入。"""
    events = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        item: dict = {}
        for ln in lines:
            key, _, value = ln.partition(": ")
            item[key] = value
        if "data" in item:
            item["data"] = json.loads(item["data"])
        events.append(item)
    return events


@pytest.fixture(scope="module", autouse=True)
async def _seeded() -> None:
    await seed()


@pytest.fixture
async def http():
    # 事件路由在主应用尚未注册前，挂到一个独立实例上验证
    application = create_app()
    application.include_router(events_router, prefix="/api/v1")
    async with LifespanManager(application) as managed:
        async with AsyncClient(transport=ASGITransport(app=managed.app), base_url="http://test", timeout=30) as c:
            yield c


async def _login(http: AsyncClient, account: str = "demo") -> str:
    r = await http.post("/api/v1/auth/login", json={"account": account, "password": DEMO_PASSWORD}, headers={"Origin": ORIGIN})
    assert r.status_code == 200
    return r.json()["csrf_token"]


async def _finished_upload_session(http: AsyncClient, csrf: str) -> str:
    """走真实上传闭环，产出一条已终态的上传会话及其事件。"""
    h = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    case_id = (await http.post("/api/v1/cases", json={"name": "SSE 探针"}, headers={**h, "Idempotency-Key": uuid.uuid4().hex})).json()["case_id"]
    ver = (await http.get("/api/v1/data-processing-disclosure")).json()["disclosure_version"]
    content = _pdf("SSE")
    session = (
        await http.post(
            f"/api/v1/cases/{case_id}/upload-sessions",
            json={"files": [{"filename": "s.pdf", "mime": PDF_MIME, "size_bytes": len(content)}], "disclosure_version_ack": ver},
            headers={**h, "Idempotency-Key": uuid.uuid4().hex},
        )
    ).json()
    slot = session["files"][0]
    put = await httpx.AsyncClient(timeout=60).put(slot["put_url"], content=content)
    await http.post(
        f"/api/v1/upload-files/{slot['upload_file_id']}/complete",
        json={"etag": put.headers.get("ETag", "x").strip('"'), "sha256": hashlib.sha256(content).hexdigest()},
        headers={**h, "Idempotency-Key": uuid.uuid4().hex},
    )
    return session["upload_session_id"]


def test_cursor_prefers_newer_of_query_and_header() -> None:
    assert resolve_cursor(3, None) == 3
    assert resolve_cursor(3, "7") == 7
    assert resolve_cursor(9, "7") == 9
    # 非法 Last-Event-ID 不猜测，退回 query 值
    assert resolve_cursor(4, "abc") == 4
    assert resolve_cursor(None, None) == 0


async def test_stream_replays_all_events_in_order_and_ends(http: AsyncClient) -> None:
    csrf = await _login(http)
    sid = await _finished_upload_session(http, csrf)

    snapshot = (await http.get(f"/api/v1/upload-sessions/{sid}/events")).json()
    r = await http.get(f"/api/v1/upload-sessions/{sid}/events/stream?after_seq=0")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store"

    streamed = _parse_sse(r.text)
    seqs = [int(e["id"]) for e in streamed]
    assert seqs == [e["seq"] for e in snapshot], "SSE 必须与 JSON 补齐接口读同一份事件"
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert all(e["data"]["seq"] == int(e["id"]) for e in streamed)


async def test_stream_resumes_after_cursor(http: AsyncClient) -> None:
    csrf = await _login(http)
    sid = await _finished_upload_session(http, csrf)
    full = [int(e["id"]) for e in _parse_sse((await http.get(f"/api/v1/upload-sessions/{sid}/events/stream")).text)]

    # 模拟浏览器自动重连：Last-Event-ID 比 query 更新时以它为准
    tail = await http.get(
        f"/api/v1/upload-sessions/{sid}/events/stream?after_seq=0",
        headers={"Last-Event-ID": str(full[1])},
    )
    assert [int(e["id"]) for e in _parse_sse(tail.text)] == full[2:]


async def test_other_user_stream_closes_without_data(http: AsyncClient) -> None:
    csrf = await _login(http)
    sid = await _finished_upload_session(http, csrf)
    await _login(http, "demo2")
    r = await http.get(f"/api/v1/upload-sessions/{sid}/events/stream")
    # 响应头发送前拒绝访问，与JSON入口使用同一错误封套。
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


async def test_stream_requires_login(http: AsyncClient) -> None:
    r = await http.get(f"/api/v1/upload-sessions/{uuid.uuid4()}/events/stream")
    assert r.status_code == 401
