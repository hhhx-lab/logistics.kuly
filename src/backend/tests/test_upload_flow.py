"""
上传闭环定向测试（方案实施段 A）。

必须走通的验收：两用户同名文件隔离、上传前告知、三份中一份失败仍保留两份、
刷新后不伪造部分字节、重试／取消不误删已接收原件、Range 请求撤权后拒绝。
"""

from __future__ import annotations

import hashlib
import io
import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.identity.seed import DEMO_PASSWORD, seed

ORIGIN = "http://localhost:5173"
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _pdf(text: str = "PROBE") -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Helvetica", 14)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


@pytest.fixture(scope="module", autouse=True)
async def _seeded() -> None:
    await seed()


class Client:
    """把「登录 + 幂等键 + CSRF」这些固定动作收在一处。"""

    def __init__(self, http: AsyncClient) -> None:
        self.http = http
        self.csrf = ""

    async def login(self, account: str = "demo") -> None:
        r = await self.http.post(
            "/api/v1/auth/login",
            json={"account": account, "password": DEMO_PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]

    def _headers(self, key: str | None) -> dict[str, str]:
        h = {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}
        if key:
            h["Idempotency-Key"] = key
        return h

    async def post(self, url: str, *, json=None, key: str | None = None):
        return await self.http.post(url, json=json, headers=self._headers(key or uuid.uuid4().hex))

    async def get(self, url: str, **kw):
        return await self.http.get(url, **kw)

    async def create_case(self, name: str = "测试票") -> str:
        r = await self.post("/api/v1/cases", json={"name": name})
        assert r.status_code == 201, r.text
        return r.json()["case_id"]

    async def disclosure_version(self) -> str:
        return (await self.http.get("/api/v1/data-processing-disclosure")).json()["disclosure_version"]

    async def open_session(self, case_id: str, files: list[dict]) -> dict:
        r = await self.post(
            f"/api/v1/cases/{case_id}/upload-sessions",
            json={"files": files, "disclosure_version_ack": await self.disclosure_version()},
        )
        assert r.status_code == 201, r.text
        return r.json()

    async def upload_and_complete(self, slot: dict, content: bytes, *, key: str | None = None):
        """走真实签名直传，再让服务端复核验收。"""
        put = await httpx.AsyncClient(timeout=60).put(slot["put_url"], content=content)
        assert put.status_code == 200, put.text
        etag = (put.headers.get("ETag") or "x").strip('"')
        return await self.post(
            f"/api/v1/upload-files/{slot['upload_file_id']}/complete",
            json={"etag": etag, "sha256": hashlib.sha256(content).hexdigest()},
            key=key,
        )


@pytest.fixture
async def client():
    async with LifespanManager(app) as managed:
        async with AsyncClient(transport=ASGITransport(app=managed.app), base_url="http://test") as http:
            c = Client(http)
            await c.login()
            yield c


async def test_upload_requires_disclosure_version(client: Client) -> None:
    """告知版本不符时拒绝签发上传，避免用旧告知继续传文件。"""
    case_id = await client.create_case()
    r = await client.post(
        f"/api/v1/cases/{case_id}/upload-sessions",
        json={
            "files": [{"filename": "a.pdf", "mime": PDF_MIME, "size_bytes": 100}],
            "disclosure_version_ack": "disclosure-outdated-v0",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DISCLOSURE_VERSION_STALE"


async def test_full_upload_creates_document_version(client: Client) -> None:
    case_id = await client.create_case()
    content = _pdf("INVOICE")
    session = await client.open_session(
        case_id, [{"filename": "invoice.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    slot = session["files"][0]
    assert slot["status"] == "pending"
    assert slot["put_url"], "应签发仅写入地址"
    # 未验收前确认字节必须是 0，不能提前写成文件大小
    assert slot["confirmed_bytes"] == 0

    r = await client.upload_and_complete(slot, content)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"]["status"] == "verified"
    assert body["file"]["confirmed_bytes"] == len(content)
    assert body["source_version"]["document_version"] == 1
    assert body["source_version"]["case_id"] == case_id


async def test_partial_failure_keeps_successful_files(client: Client) -> None:
    """三份中一份格式不支持：另外两份仍可验收，失败项不吞并成功项。"""
    case_id = await client.create_case()
    good_a, good_b = _pdf("A"), _pdf("B")
    session = await client.open_session(
        case_id,
        [
            {"filename": "a.pdf", "mime": PDF_MIME, "size_bytes": len(good_a)},
            {"filename": "bad.edi", "mime": "application/edifact", "size_bytes": 10},
            {"filename": "b.pdf", "mime": PDF_MIME, "size_bytes": len(good_b)},
        ],
    )
    by_name = {f["filename"]: f for f in session["files"]}
    assert by_name["bad.edi"]["status"] == "failed"
    assert by_name["bad.edi"]["error_code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert by_name["bad.edi"]["put_url"] is None, "被拒文件不应拿到上传地址"

    for name, content in (("a.pdf", good_a), ("b.pdf", good_b)):
        assert (await client.upload_and_complete(by_name[name], content)).status_code == 200

    final = (await client.get(f"/api/v1/upload-sessions/{session['upload_session_id']}")).json()
    statuses = {f["filename"]: f["status"] for f in final["files"]}
    assert statuses == {"a.pdf": "verified", "bad.edi": "failed", "b.pdf": "verified"}
    assert final["status"] == "partial"


async def test_declared_mime_must_match_real_bytes(client: Client) -> None:
    """声明 PDF 实际传 DOCX 字节：按魔数判定并拒绝，不信任声明。"""
    case_id = await client.create_case()
    fake = b"PK\x03\x04" + b"0" * 200
    session = await client.open_session(
        case_id, [{"filename": "liar.pdf", "mime": PDF_MIME, "size_bytes": len(fake)}]
    )
    r = await client.upload_and_complete(session["files"][0], fake)
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_size_mismatch_rejected(client: Client) -> None:
    """实际字节数与声明不符时拒绝验收。"""
    case_id = await client.create_case()
    content = _pdf("SHORT")
    session = await client.open_session(
        case_id, [{"filename": "x.pdf", "mime": PDF_MIME, "size_bytes": len(content) + 500}]
    )
    slot = session["files"][0]
    await httpx.AsyncClient(timeout=60).put(slot["put_url"], content=content)
    r = await client.post(
        f"/api/v1/upload-files/{slot['upload_file_id']}/complete",
        json={"etag": "x"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SIZE_MISMATCH"


async def test_complete_is_idempotent(client: Client) -> None:
    """同键重放只得到同一个 DocumentVersion，不会产生第二个版本。"""
    case_id = await client.create_case()
    content = _pdf("IDEM")
    session = await client.open_session(
        case_id, [{"filename": "i.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    slot = session["files"][0]
    key = uuid.uuid4().hex
    digest = hashlib.sha256(content).hexdigest()
    put = await httpx.AsyncClient(timeout=60).put(slot["put_url"], content=content)
    etag = (put.headers.get("ETag") or "x").strip('"')
    payload = {"etag": etag, "sha256": digest}

    first = await client.post(
        f"/api/v1/upload-files/{slot['upload_file_id']}/complete", json=payload, key=key
    )
    assert first.status_code == 200
    # 同键同内容：返回同一个 DocumentVersion，不产生第二个版本
    second = await client.post(
        f"/api/v1/upload-files/{slot['upload_file_id']}/complete", json=payload, key=key
    )
    assert second.status_code == 200
    assert first.json()["source_version"] == second.json()["source_version"]

    # 同键异内容：必须拒绝，不能静默返回旧结果
    conflict = await client.post(
        f"/api/v1/upload-files/{slot['upload_file_id']}/complete",
        json={"etag": "different-etag", "sha256": digest},
        key=key,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_replace_creates_new_version_same_document(client: Client) -> None:
    """替换原件只新增同 Document 的版本，旧版仍可读。"""
    case_id = await client.create_case()
    v1 = _pdf("V1")
    session = await client.open_session(
        case_id, [{"filename": "bl.pdf", "mime": PDF_MIME, "size_bytes": len(v1)}]
    )
    first = (await client.upload_and_complete(session["files"][0], v1)).json()
    document_id = first["source_version"]["document_id"]

    v2 = _pdf("V2-REPLACED")
    session2 = await client.open_session(
        case_id,
        [
            {
                "filename": "bl.pdf",
                "mime": PDF_MIME,
                "size_bytes": len(v2),
                "replace_document_id": document_id,
                "expected_document_version": 1,
            }
        ],
    )
    second = (await client.upload_and_complete(session2["files"][0], v2)).json()
    assert second["source_version"]["document_id"] == document_id
    assert second["source_version"]["document_version"] == 2

    # 旧版本字节仍可按版本号读取
    old = await client.get(f"/api/v1/documents/{document_id}/versions/1/content")
    assert old.status_code == 200
    assert old.content == v1


async def test_replace_with_stale_version_is_rejected(client: Client) -> None:
    case_id = await client.create_case()
    content = _pdf("ONLY")
    session = await client.open_session(
        case_id, [{"filename": "a.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    doc = (await client.upload_and_complete(session["files"][0], content)).json()["source_version"]["document_id"]
    r = await client.post(
        f"/api/v1/cases/{case_id}/upload-sessions",
        json={
            "files": [
                {
                    "filename": "a.pdf",
                    "mime": PDF_MIME,
                    "size_bytes": 100,
                    "replace_document_id": doc,
                    "expected_document_version": 99,
                }
            ],
            "disclosure_version_ack": await client.disclosure_version(),
        },
    )
    assert r.status_code == 201
    assert r.json()["files"][0]["error_code"] == "REPLACE_VERSION_STALE"


async def test_cancel_does_not_delete_verified_document(client: Client) -> None:
    """取消只停未完成的槽；已验收原件不受影响。"""
    case_id = await client.create_case()
    content = _pdf("KEEP")
    session = await client.open_session(
        case_id, [{"filename": "k.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    slot = session["files"][0]
    verified = (await client.upload_and_complete(slot, content)).json()

    r = await client.post(f"/api/v1/upload-files/{slot['upload_file_id']}/cancel")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "UPLOAD_ALREADY_VERIFIED"

    doc = verified["source_version"]
    still = await client.get(f"/api/v1/documents/{doc['document_id']}/versions/{doc['document_version']}/content")
    assert still.status_code == 200


async def test_retry_reissues_upload_slot(client: Client) -> None:
    case_id = await client.create_case()
    content = _pdf("RETRY")
    session = await client.open_session(
        case_id, [{"filename": "r.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    slot = session["files"][0]
    # 先制造一次失败：不传字节直接验收
    failed = await client.post(
        f"/api/v1/upload-files/{slot['upload_file_id']}/complete", json={"etag": "x"}
    )
    assert failed.status_code == 422
    assert failed.json()["error"]["code"] == "BYTES_MISSING"

    again = await client.post(f"/api/v1/upload-files/{slot['upload_file_id']}/retry")
    assert again.status_code == 200
    fresh = again.json()
    assert fresh["status"] == "pending"
    assert fresh["put_url"] and fresh["put_url"] != slot["put_url"], "重试应换新的临时对象与签名"
    assert (await client.upload_and_complete(fresh, content)).status_code == 200


async def test_events_are_sequential_and_resumable(client: Client) -> None:
    """断线后按 after_seq 补齐，不漏不重。"""
    case_id = await client.create_case()
    content = _pdf("EVT")
    session = await client.open_session(
        case_id, [{"filename": "e.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    await client.upload_and_complete(session["files"][0], content)
    sid = session["upload_session_id"]

    all_events = (await client.get(f"/api/v1/upload-sessions/{sid}/events")).json()
    seqs = [e["seq"] for e in all_events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))

    tail = (await client.get(f"/api/v1/upload-sessions/{sid}/events?after_seq={seqs[0]}")).json()
    assert [e["seq"] for e in tail] == seqs[1:]


async def test_range_requests_supported(client: Client) -> None:
    case_id = await client.create_case()
    content = _pdf("RANGE")
    session = await client.open_session(
        case_id, [{"filename": "g.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    doc = (await client.upload_and_complete(session["files"][0], content)).json()["source_version"]
    url = f"/api/v1/documents/{doc['document_id']}/versions/{doc['document_version']}/content"

    part = await client.get(url, headers={"Range": "bytes=0-7"})
    assert part.status_code == 206
    assert part.content == content[:8]
    assert part.headers["content-range"] == f"bytes 0-7/{len(content)}"
    assert part.headers["accept-ranges"] == "bytes"
    assert part.headers["cache-control"] == "private, no-store"

    bad = await client.get(url, headers={"Range": f"bytes={len(content) + 10}-"})
    assert bad.status_code == 416


async def test_cross_user_cannot_read_document(client: Client) -> None:
    """另一用户直接请求已知 ID 也必须被拒，且不泄露存在性。"""
    case_id = await client.create_case()
    content = _pdf("SECRET")
    session = await client.open_session(
        case_id, [{"filename": "s.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]
    )
    doc = (await client.upload_and_complete(session["files"][0], content)).json()["source_version"]

    await client.login("demo2")
    denied = await client.get(
        f"/api/v1/documents/{doc['document_id']}/versions/{doc['document_version']}/content"
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "NOT_FOUND"

    assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 404


async def test_same_filename_across_users_is_isolated(client: Client) -> None:
    """两用户同名文件互不影响，也不跨用户去重。"""
    content = _pdf("SAME")
    spec = [{"filename": "同名文件.pdf", "mime": PDF_MIME, "size_bytes": len(content)}]

    case_a = await client.create_case("A 票")
    doc_a = (await client.upload_and_complete((await client.open_session(case_a, spec))["files"][0], content)).json()

    await client.login("demo2")
    case_b = await client.create_case("B 票")
    doc_b = (await client.upload_and_complete((await client.open_session(case_b, spec))["files"][0], content)).json()

    assert doc_a["source_version"]["document_id"] != doc_b["source_version"]["document_id"]
    # 内容哈希相同也不能跨用户提示重复
    assert doc_b["file"]["duplicate_of"] is None


async def test_upload_session_list_only_shows_own(client: Client) -> None:
    case_id = await client.create_case()
    content = _pdf("MINE")
    await client.open_session(case_id, [{"filename": "m.pdf", "mime": PDF_MIME, "size_bytes": len(content)}])
    mine = (await client.get("/api/v1/upload-sessions")).json()
    assert any(s["case_id"] == case_id for s in mine["items"])

    await client.login("demo2")
    other = (await client.get("/api/v1/upload-sessions")).json()
    assert all(s["case_id"] != case_id for s in other["items"])
