"""
R01–R03、U08–U09 定向测试。

解析产物本应由 Worker 写入；这里在租户上下文里用 ORM 直接造一版解析，
原件本身仍走真实上传闭环，保证归属链与生产一致。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from uuid import UUID

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.main import create_app
from app.api.routers.review import router as review_router
from app.config import get_settings
from app.documents.evidence_models import (
    AnchorPrecision,
    BusinessRevision,
    BusinessRevisionField,
    DocumentSection,
    FieldAssertion,
    FieldReviewEvent,
    LogicalDocument,
    LogicalDocumentType,
    ParseArtifact,
    ParseQuality,
    SourceAnchor,
    SourceSegment,
)
from app.documents.models import Case
from app.identity.seed import DEMO_PASSWORD, TENANT_A, seed
from app.infra.database import create_database_engine, create_session_factory, tenant_transaction

ORIGIN = "http://localhost:5173"
PDF_MIME = "application/pdf"


def _pdf(pages: int = 2) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f"INVOICE PAGE {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture(scope="module", autouse=True)
async def _seeded() -> None:
    await seed()


@pytest.fixture
async def http():
    application = create_app()
    application.include_router(review_router, prefix="/api/v1")
    async with LifespanManager(application) as managed:
        async with AsyncClient(transport=ASGITransport(app=managed.app), base_url="http://test", timeout=30) as c:
            yield c


async def _login(http: AsyncClient, account: str = "demo") -> dict:
    r = await http.post("/api/v1/auth/login", json={"account": account, "password": DEMO_PASSWORD}, headers={"Origin": ORIGIN})
    assert r.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": r.json()["csrf_token"]}


async def _uploaded_pdf(http: AsyncClient, h: dict) -> dict:
    case_id = (await http.post("/api/v1/cases", json={"name": "复核探针"}, headers={**h, "Idempotency-Key": uuid.uuid4().hex})).json()["case_id"]
    ver = (await http.get("/api/v1/data-processing-disclosure")).json()["disclosure_version"]
    content = _pdf()
    session = (
        await http.post(
            f"/api/v1/cases/{case_id}/upload-sessions",
            json={"files": [{"filename": "inv.pdf", "mime": PDF_MIME, "size_bytes": len(content)}], "disclosure_version_ack": ver},
            headers={**h, "Idempotency-Key": uuid.uuid4().hex},
        )
    ).json()
    slot = session["files"][0]
    put = await httpx.AsyncClient(timeout=60).put(slot["put_url"], content=content)
    done = await http.post(
        f"/api/v1/upload-files/{slot['upload_file_id']}/complete",
        json={"etag": put.headers.get("ETag", "x").strip('"'), "sha256": hashlib.sha256(content).hexdigest()},
        headers={**h, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert done.status_code == 200, done.text
    return {"case_id": case_id, **done.json()["source_version"]}


async def _seed_parse(src: dict, *, precision: AnchorPrecision = AnchorPrecision.RECT) -> dict:
    """造一版解析：1 张发票、1 个区段、2 个片段（一个带矩形，一个只到页）、2 个字段。"""
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    case_id, doc_id, ver = UUID(src["case_id"]), UUID(src["document_id"]), src["document_version"]
    async with factory() as session:
        async with tenant_transaction(session, TENANT_A):
            pa = ParseArtifact(
                tenant_id=TENANT_A, case_id=case_id, document_id=doc_id, document_version=ver,
                parser_name="test", parser_version="1", parser_config_hash="0" * 64,
                page_count=2, quality=ParseQuality.VALID, quality_flags=[], is_synthetic=True,
            )
            session.add(pa)
            await session.flush()
            ld = LogicalDocument(
                tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, ordinal=1,
                type_candidate=LogicalDocumentType.COMMERCIAL_INVOICE, page_start=1, page_end=2,
            )
            session.add(ld)
            await session.flush()
            sec = DocumentSection(
                tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, logical_document_id=ld.id,
                ordinal=1, kind="header", title="抬头", page_start=1, page_end=1,
            )
            session.add(sec)
            await session.flush()
            seg_rect = SourceSegment(
                tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, logical_document_id=ld.id,
                section_id=sec.id, ordinal=1, structure="paragraph", text="Invoice No. INV-2026-0918",
            )
            seg_page = SourceSegment(
                tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, logical_document_id=ld.id,
                section_id=sec.id, ordinal=2, structure="paragraph", text="Seller: ACME Trading Ltd.",
            )
            session.add_all([seg_rect, seg_page])
            await session.flush()
            session.add_all(
                [
                    SourceAnchor(
                        tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, source_segment_id=seg_rect.id,
                        precision=precision, page_no=1,
                        bbox=[72.0, 700.0, 300.0, 730.0] if precision is AnchorPrecision.RECT else None,
                        coord_system="pdf_points_bottom_left" if precision is AnchorPrecision.RECT else None,
                    ),
                    SourceAnchor(
                        tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, source_segment_id=seg_page.id,
                        precision=AnchorPrecision.PAGE_ONLY, page_no=1,
                    ),
                ]
            )
            f_no = FieldAssertion(
                tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, logical_document_id=ld.id,
                section_id=sec.id, field_key="invoice_no", group="logistics",
                source_text="INV-2026-0918", candidate_value="INV-2026-0918", normalized_value="INV-2026-0918",
                source_segment_ids=[str(seg_rect.id)],
            )
            f_seller = FieldAssertion(
                tenant_id=TENANT_A, case_id=case_id, parse_artifact_id=pa.id, logical_document_id=ld.id,
                section_id=sec.id, field_key="seller_name", role="seller", group="parties",
                source_text="ACME Trading Ltd.", candidate_value="ACME Trading Ltd.",
                source_segment_ids=[str(seg_page.id)],
            )
            session.add_all([f_no, f_seller])
            await session.flush()
            ids = {
                "parse_artifact_id": str(pa.id), "logical_document_id": str(ld.id), "section_id": str(sec.id),
                "seg_rect": str(seg_rect.id), "seg_page": str(seg_page.id),
                "field_no": str(f_no.id), "field_seller": str(f_seller.id),
            }
    await engine.dispose()
    return ids


def _target(src: dict, ids: dict, **extra) -> dict:
    return {
        "case_id": src["case_id"], "document_id": src["document_id"], "document_version": src["document_version"],
        "parse_artifact_id": ids["parse_artifact_id"], "logical_document_id": ids["logical_document_id"],
        "section_id": ids["section_id"], **extra,
    }


async def test_resolve_rect_anchor_returns_bbox(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    ids = await _seed_parse(src)
    r = await http.post("/api/v1/evidence/resolve", json=_target(src, ids, source_segment_id=ids["seg_rect"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "working"
    assert body["anchor_precision"] == "rect"
    assert body["bbox"] == [72.0, 700.0, 300.0, 730.0]
    assert body["page_no"] == 1


async def test_resolve_page_only_never_returns_bbox(http: AsyncClient) -> None:
    """坐标不可靠时退到页级，绝不返回矩形，前端据此不画猜测框。"""
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    ids = await _seed_parse(src)
    r = await http.post("/api/v1/evidence/resolve", json=_target(src, ids, field_id=ids["field_seller"]))
    body = r.json()
    assert body["anchor_precision"] == "page_only"
    assert body["bbox"] is None
    assert body["notice"]


async def test_resolve_unparsed_document_is_source_only(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    r = await http.post(
        "/api/v1/evidence/resolve",
        json={"case_id": src["case_id"], "document_id": src["document_id"], "document_version": src["document_version"]},
    )
    body = r.json()
    assert body["mode"] == "source_only"
    assert "open_reading_view" not in body["allowed_actions"]


async def test_resolve_rejects_mismatched_chain(http: AsyncClient) -> None:
    """区段不属于所给解析版时视为无权：两票的 ID 不能拼接。"""
    h = await _login(http)
    a = await _uploaded_pdf(http, h)
    b = await _uploaded_pdf(http, h)
    ids_a = await _seed_parse(a)
    ids_b = await _seed_parse(b)
    forged = _target(a, ids_a)
    forged["section_id"] = ids_b["section_id"]
    r = await http.post("/api/v1/evidence/resolve", json=forged)
    assert r.status_code == 404


async def test_resolve_denied_for_other_tenant(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    ids = await _seed_parse(src)
    await _login(http, "demo2")
    r = await http.post("/api/v1/evidence/resolve", json=_target(src, ids, source_segment_id=ids["seg_rect"]))
    assert r.status_code == 404
    assert "INV-2026" not in r.text


async def test_historical_parse_is_read_only(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    old = await _seed_parse(src)
    await _seed_parse(src)  # 重新识别产生更新的解析版
    r = await http.post("/api/v1/evidence/resolve", json=_target(src, old))
    body = r.json()
    assert body["mode"] == "historical"
    assert "revise" in body["disabled_reasons"]


async def test_review_view_tree_and_honest_gate(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    await _seed_parse(src)
    r = await http.get(f"/api/v1/cases/{src['case_id']}/review-view")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "working"
    f = body["files"][0]
    assert f["parse_status"] == "parsed"
    ld = f["logical_documents"][0]
    assert ld["doc_type"] == "commercial_invoice"
    assert ld["sections"][0]["title"] == "抬头"
    # 规则未运行时不能允许确认或导出
    assert "confirm_review" in body["disabled_reasons"]
    assert "export" in body["disabled_reasons"]


async def test_fields_are_candidates_until_reviewed(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    ids = await _seed_parse(src)
    body = (await http.get(f"/api/v1/cases/{src['case_id']}/fields")).json()
    statuses = {i["field_id"]: i["review_status"] for i in body["items"]}
    assert statuses == {ids["field_no"]: "candidate", ids["field_seller"]: "candidate"}
    seller = next(i for i in body["items"] if i["field_id"] == ids["field_seller"])
    assert seller["role"] == "seller"
    assert seller["target"]["field_id"] == ids["field_seller"]


async def test_fields_page_stale_when_review_state_changes(http: AsyncClient) -> None:
    """后续页发现复核状态已变 → 409 PAGE_STALE，不拼接新旧处置。"""
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    await _seed_parse(src)
    first = (await http.get(f"/api/v1/cases/{src['case_id']}/fields?limit=1")).json()
    assert first["has_more"]

    settings = get_settings()
    engine = create_database_engine(settings)
    async with create_session_factory(engine)() as session:
        async with tenant_transaction(session, TENANT_A):
            case = await session.get(Case, UUID(src["case_id"]))
            case.review_state_version += 1
    await engine.dispose()

    r = await http.get(f"/api/v1/cases/{src['case_id']}/fields?limit=1&cursor={first['next_cursor']}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PAGE_STALE"


async def test_confirmed_field_status(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    ids = await _seed_parse(src)
    settings = get_settings()
    engine = create_database_engine(settings)
    async with create_session_factory(engine)() as session:
        async with tenant_transaction(session, TENANT_A):
            case = await session.scalar(select(Case).where(Case.id == UUID(src["case_id"])).with_for_update())
            business = BusinessRevision(tenant_id=TENANT_A, case_id=case.id, revision_no=1,
                reason="baseline_extraction", created_by=case.owner_user_id)
            session.add(business)
            await session.flush()
            for field_id in (ids["field_no"], ids["field_seller"]):
                session.add(BusinessRevisionField(tenant_id=TENANT_A, business_revision_id=business.id,
                    field_assertion_id=UUID(field_id)))
            case.current_business_revision_id, case.business_revision_no = business.id, business.revision_no
            case.review_state_version += 1
            session.add(
                FieldReviewEvent(
                    tenant_id=TENANT_A, case_id=UUID(src["case_id"]), field_assertion_id=UUID(ids["field_no"]),
                    business_revision_id=business.id, action="confirmed", decided_by=case.owner_user_id,
                    review_sequence=case.review_state_version,
                )
            )
    await engine.dispose()
    body = (await http.get(f"/api/v1/cases/{src['case_id']}/fields")).json()
    statuses = {i["field_id"]: i["review_status"] for i in body["items"]}
    assert statuses[ids["field_no"]] == "confirmed"
    assert statuses[ids["field_seller"]] == "candidate"


async def test_issues_honestly_report_rules_not_run(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    body = (await http.get(f"/api/v1/cases/{src['case_id']}/issues")).json()
    assert body["items"] == []
    assert body["rules_status"] == "not_run"


async def test_reading_view_pagination_and_search(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    ids = await _seed_parse(src)
    url = f"/api/v1/parse-artifacts/{ids['parse_artifact_id']}/reading-view"
    first = (await http.get(f"{url}?limit=1")).json()
    assert len(first["items"]) == 1 and first["has_more"]
    assert first["is_synthetic"] is True
    second = (await http.get(f"{url}?limit=1&cursor={first['next_cursor']}")).json()
    assert second["items"][0]["source_segment_id"] != first["items"][0]["source_segment_id"]

    hits = (await http.get(f"{url}?query=ACME")).json()
    assert [i["text"] for i in hits["items"]] == ["Seller: ACME Trading Ltd."]
    # 只有 rect 精度才带 bbox
    assert hits["items"][0]["bbox"] is None


async def test_page_image_renders_and_bounds(http: AsyncClient) -> None:
    h = await _login(http)
    src = await _uploaded_pdf(http, h)
    ids = await _seed_parse(src)
    base = f"/api/v1/parse-artifacts/{ids['parse_artifact_id']}/pages"
    r = await http.get(f"{base}/1/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert r.headers["cache-control"] == "private, no-store"
    assert (await http.get(f"{base}/9/image")).status_code == 404
    # 分辨率有上限，防止被用来放大拉取
    assert (await http.get(f"{base}/1/image?scale=10")).status_code == 422

    await _login(http, "demo2")
    assert (await http.get(f"{base}/1/image")).status_code == 404
