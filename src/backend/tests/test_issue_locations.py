"""G07：未选疑点真实矩形、来源归属与固定旧Parse；真实PG回滚。"""
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents import evidence_models as em
from app.review import models as rm
from app.review.commands import issue_view
from tests.test_review_field_gate import seed_fields
from tests.test_workflow_response_contracts import http_db


@pytest.mark.asyncio
async def test_supplement_upload_keeps_issue_case_version_and_idempotency(http_db, monkeypatch):
    from app.api.routers import uploads
    from app.documents.models import UploadSession
    from app.api.routers.auth import build_disclosure
    from app.config import get_settings
    from types import SimpleNamespace
    http, conn, principal, created = http_db
    ids = await seed_fields(conn, principal, created)
    other = await seed_fields(conn, principal, created)
    monkeypatch.setattr(uploads, "_store", lambda request: SimpleNamespace(presign_put=lambda *args, **kwargs: "http://test.invalid/put"))
    payload = {"files": [{"filename": "supplement.pdf", "mime": "application/pdf", "size_bytes": 128}],
        "disclosure_version_ack": build_disclosure(get_settings()).disclosure_version,
        "related_issue_id": str(ids["issues"][0]), "expected_issue_version": 1}
    url = f"/api/v1/cases/{ids['case_id']}/upload-sessions"
    key = {"Idempotency-Key": uuid4().hex}
    accepted = await http.post(url, json=payload, headers=key)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["case_id"] == ids["case_id"]
    replayed = await http.post(url, json=payload, headers=key)
    assert replayed.status_code == 201 and replayed.json()["upload_session_id"] == accepted.json()["upload_session_id"]
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        session = await db.get(UploadSession, UUID(accepted.json()["upload_session_id"]))
        assert session.disclosure_snapshot["issue_supplement"] == {"issue_id": str(ids["issues"][0]), "run_id": str(ids["run_id"]), "issue_version": 1}
    stale = await http.post(url, json={**payload, "expected_issue_version": 2}, headers={"Idempotency-Key": uuid4().hex})
    alien = await http.post(f"/api/v1/cases/{other['case_id']}/upload-sessions", json=payload, headers={"Idempotency-Key": uuid4().hex})
    assert stale.status_code == 409 and alien.status_code == 404


@pytest.mark.asyncio
async def test_http_all_issue_sides_have_real_rect_and_keep_old_parse_after_reparse(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        parse = await db.get(em.ParseArtifact, ids["parse_id"]); parse.page_count = 3
        source = (await db.execute(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id == parse.id))).scalar_one()
        anchors=[];sources=[source]
        second=em.SourceSegment(tenant_id=p.tenant_id,case_id=source.case_id,parse_artifact_id=parse.id,
            logical_document_id=source.logical_document_id,section_id=source.section_id,ordinal=1,text="another source",structure="paragraph")
        db.add(second);await db.flush();sources.append(second)
        for index,s in enumerate(sources):
            anchor=em.SourceAnchor(tenant_id=p.tenant_id,case_id=s.case_id,parse_artifact_id=parse.id,source_segment_id=s.id,
                precision=em.AnchorPrecision.RECT,page_no=index+1,bbox=[.1,.2,.4,.3],coord_system="page_normalized_top_left",render_version="old-render")
            db.add(anchor);anchors.append(anchor)
        await db.flush()
        for issue_id in ids["issues"]:
            issue=await db.get(rm.Issue,issue_id)
            issue.sources=[{"label":"side", "value":s.text,"target":{"case_id":ids["case_id"],"document_id":ids["document_id"],"document_version":1,
                "parse_artifact_id":str(parse.id),"logical_document_id":str(s.logical_document_id),"section_id":str(s.section_id),
                "source_segment_id":str(s.id),"source_anchor_id":str(a.id)}} for s,a in zip(sources,anchors)]
        # 后续同原件重解析有其他位置，旧疑点不能借用它。
        newer=em.ParseArtifact(tenant_id=p.tenant_id,case_id=source.case_id,document_id=parse.document_id,document_version=1,
            parser_name="new-fixture",parser_version="2",parser_config_hash="1"*64,quality=em.ParseQuality.VALID,page_count=3)
        db.add(newer);await db.flush()
        logical=em.LogicalDocument(tenant_id=p.tenant_id,case_id=source.case_id,parse_artifact_id=newer.id,ordinal=0,type_candidate=em.LogicalDocumentType.COMMERCIAL_INVOICE)
        db.add(logical);await db.flush()
        section=em.DocumentSection(tenant_id=p.tenant_id,case_id=source.case_id,parse_artifact_id=newer.id,logical_document_id=logical.id,ordinal=0,kind="body")
        db.add(section);await db.flush()
        latest=em.SourceSegment(tenant_id=p.tenant_id,case_id=source.case_id,parse_artifact_id=newer.id,logical_document_id=logical.id,section_id=section.id,ordinal=0,text=source.text,structure="paragraph")
        db.add(latest);await db.flush()
        db.add(em.SourceAnchor(tenant_id=p.tenant_id,case_id=source.case_id,parse_artifact_id=newer.id,source_segment_id=latest.id,
            precision=em.AnchorPrecision.RECT,page_no=3,bbox=[.6,.7,.9,.8],coord_system="page_normalized_top_left"))
    response=await http.get(f"/api/v1/cases/{ids['case_id']}/issues")
    assert response.status_code==200,response.text
    rows=response.json()["items"]
    assert len(rows)==2
    for row in rows:
        for index,side in enumerate([row["a"],row["b"]]):
            assert side["target"]["parse_artifact_id"]==str(ids["parse_id"])
            assert side["location"]=={"source_anchor_id":str(anchors[index].id),"precision":"rect","page_no":index+1,
                "bbox":[.1,.2,.4,.3],"coord_system":"page_normalized_top_left","render_version":"old-render"}
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        issue=await db.get(rm.Issue,ids["issues"][0])
        historical=await issue_view(db,issue,[])
        assert historical["a"]["location"]["source_anchor_id"]==str(anchors[0].id)
        assert historical["b"]["location"]["page_no"]==2


@pytest.mark.asyncio
async def test_invalid_source_ownership_or_missing_location_never_returns_rect(http_db):
    _,conn,p,created=http_db
    ids=await seed_fields(conn,p,created)
    other=await seed_fields(conn,p,created)
    async with AsyncSession(bind=conn,expire_on_commit=False,join_transaction_mode="create_savepoint") as db,db.begin():
        issue=await db.get(rm.Issue,ids["issues"][0])
        source=(await db.execute(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id==ids["parse_id"]))).scalar_one()
        alien=(await db.execute(select(em.SourceSegment).where(em.SourceSegment.parse_artifact_id==other["parse_id"]))).scalar_one()
        anchor=em.SourceAnchor(tenant_id=p.tenant_id,case_id=issue.case_id,parse_artifact_id=ids["parse_id"],source_segment_id=source.id,
            precision=em.AnchorPrecision.RECT,page_no=1,bbox=[.1,.2,.4,.3],coord_system="page_normalized_top_left")
        db.add(anchor);await db.flush()
        target={"document_id":ids["document_id"],"document_version":1,"parse_artifact_id":str(ids["parse_id"]),
            "source_segment_id":str(source.id),"source_anchor_id":str(anchor.id),"logical_document_id":str(source.logical_document_id),"section_id":str(source.section_id)}
        for changes in [{"document_id":other["document_id"]},{"document_version":2},{"parse_artifact_id":str(other["parse_id"])},
            {"source_segment_id":str(alien.id)},{"source_anchor_id":str(uuid4())},{"logical_document_id":str(uuid4())},{"section_id":str(uuid4())},
            {"parse_artifact_id":None}]:
            issue.sources=[{"label":"invalid fixture","value":"","target":{**target,**changes}}]
            assert (await issue_view(db,issue))["a"]["location"] is None
        issue.sources=[{"label":"invalid coordinate fixture","value":"","target":target}]
        for page,bbox in [(1,[.4,.2,.1,.3]),(1,[-.1,.2,.4,.3]),(None,[.1,.2,.4,.3]),(0,[.1,.2,.4,.3])]:
            anchor.page_no=page;anchor.bbox=bbox;await db.flush()
            location=(await issue_view(db,issue))["a"]["location"]
            assert location["precision"] in {"page_only","none"} and location["bbox"] is None
        anchor.precision=em.AnchorPrecision.PAGE_ONLY;anchor.page_no=2;anchor.bbox=None;await db.flush()
        location=(await issue_view(db,issue))["a"]["location"]
        assert location["precision"]=="page_only" and location["page_no"]==2 and location["bbox"] is None


@pytest.mark.asyncio
async def test_evidence_resolves_saved_issue_and_translation_mapping_without_latest_parse(http_db):
    http, conn, p, created = http_db
    ids = await seed_fields(conn, p, created)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        source = await db.get(em.SourceSegment, ids["source_id"])
        source.structure = "table_cell"
        anchor = em.SourceAnchor(tenant_id=p.tenant_id, case_id=source.case_id, parse_artifact_id=source.parse_artifact_id,
            source_segment_id=source.id, precision=em.AnchorPrecision.RECT, page_no=1,
            bbox=[.1,.2,.4,.3], coord_system="page_normalized_top_left")
        translated = rm.TranslationSegment(tenant_id=p.tenant_id, case_id=source.case_id, run_id=ids["run_id"],
            document_id=UUID(ids["document_id"]), document_version=1, parse_artifact_id=ids["parse_id"], section_id=ids["section_id"],
            target_language="en", ordinal=0, source_segment_ids=[str(source.id)], translated_text="Invoice 001",
            state="translated", dependency_fingerprint="0"*64)
        db.add_all([anchor, translated]); await db.flush()
        revision = rm.TranslationRevision(tenant_id=p.tenant_id, case_id=source.case_id, run_id=ids["run_id"],
            business_revision_id=ids["revision_id"], target_language="en", revision_no=1,
            parse_artifact_ids=[str(ids["parse_id"])], resolved_scope_ids=[], segment_ids=[str(translated.id)],
            prompt_versions={}, model_bindings=[], qa_status="passed", qa_summary={}, dependency_fingerprint="0"*64)
        db.add(revision); await db.flush()
        from app.workflows.models import Run
        run = await db.get(Run, ids["run_id"]); run.current_translation_revision_id = revision.id
    base = {"case_id":ids["case_id"], "document_id":ids["document_id"], "document_version":1, "run_id":str(ids["run_id"])}
    issue = await http.post("/api/v1/evidence/resolve", json={**base, "issue_id":str(ids["issues"][0])})
    assert issue.status_code == 200, issue.text
    assert issue.json()["normalized_target"]["source_segment_id"] == str(ids["source_id"])
    translation = await http.post("/api/v1/evidence/resolve", json={**base,
        "translation_revision_id":str(revision.id), "translation_segment_id":str(translated.id)})
    assert translation.status_code == 200, translation.text
    assert translation.json()["normalized_target"]["source_anchor_id"] == str(anchor.id)
    assert translation.json()["bbox"] == [.1,.2,.4,.3]
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        saved = await db.get(em.SourceAnchor, anchor.id); saved.bbox = [-.1,.2,.4,.3]
    invalid = await http.post("/api/v1/evidence/resolve", json={**base, "issue_id":str(ids["issues"][0])})
    assert invalid.status_code == 200
    assert invalid.json()["bbox"] is None and invalid.json()["anchor_precision"] == "cell"
    wrong = await http.post("/api/v1/evidence/resolve", json={**base,
        "translation_revision_id":str(revision.id), "translation_segment_id":str(translated.id), "source_segment_id":str(uuid4())})
    assert wrong.status_code == 404
