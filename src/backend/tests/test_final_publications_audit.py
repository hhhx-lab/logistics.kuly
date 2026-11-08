from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
import pytest
from app.extensions.audit import audit_context, safe_destination, safe_parameter_summary
from app.review.publications import description_candidates, freeze_publications


def publication():
    today = datetime.now(UTC).date()
    return {"version":"sample@1", "kind":"hs_description_candidate", "reviewed_by":"test reviewer", "review_reference":"synthetic review fixture",
        "source_url":"https://example.org/tariff", "source_sha256":"a"*64,
        "valid_from": (today-timedelta(days=30)).isoformat(), "valid_before": (today+timedelta(days=30)).isoformat(),
        "destination_country":"DE", "commodity":"fresh_apples", "descriptions":["Fresh apples"], "allowed_six_digit_codes":["080810"]}


def approved_publication(tmp_path):
    pack = {**publication(), "publication_status": "approved", "source_artifact": "source.txt", "review_artifact": "review.json"}
    source = b"Synthetic tariff source for proof consistency tests, not official regulatory evidence."
    pack["source_sha256"] = hashlib.sha256(source).hexdigest()
    (tmp_path / "source.txt").write_bytes(source)
    record = {key: pack[key] for key in ("version", "kind", "reviewed_by", "review_reference", "source_sha256", "source_url",
        "destination_country", "commodity", "valid_from", "valid_before", "descriptions", "allowed_six_digit_codes")}
    record.update(decision="approved", reviewer_qualification_reference="synthetic-reviewer-reference")
    payload = json.dumps(record).encode()
    (tmp_path / "review.json").write_bytes(payload)
    pack["review_sha256"] = hashlib.sha256(payload).hexdigest()
    settings = SimpleNamespace(reviewed_rule_publications=[pack], rule_evidence_directory=str(tmp_path), rule_official_domains=["example.org"])
    return pack, settings


def test_reviewed_candidates_require_exact_scope_and_direct_sources(tmp_path):
    pack, settings = approved_publication(tmp_path)
    frozen = freeze_publications(settings)
    assert len(frozen) == 1
    assert frozen[0]["evidence_verified"] is True
    assert frozen[0]["source_artifact"] is None and frozen[0]["review_artifact"] is None
    assert str(tmp_path) not in json.dumps(frozen)
    line={"id":"line", "logical_document_id":"logical", "description":"Fresh apples", "hs_code":"200899", "source_segment_ids":["source"]}
    sources=[{"id":"source", "logical_document_id":"logical", "text":"Fresh apples HS 200899"}]
    inputs={"destination_country":"DE", "commodity":"fresh_apples", "import_date":datetime.now(UTC).date().isoformat()}
    out=description_candidates([line],sources,frozen,inputs)
    assert len(out)==1 and out[0].status=="failed" and not out[0].blocks_export
    assert out[0].calculation["final_classification"]=="not_assessed"
    for altered in ({}, {**inputs,"destination_country":"US"}, {**inputs,"import_date":pack["valid_before"]}):
        assert description_candidates([line],sources,frozen,altered)==[]
    assert description_candidates([line],[],frozen,inputs)==[]
    assert description_candidates([{**line,"description":"apples"}],sources,frozen,inputs)==[]


@pytest.mark.parametrize("status", ["pending", "withdrawn", "expired"])
def test_unpublished_revoked_and_expired_packages_never_freeze(tmp_path, status):
    pack, settings = approved_publication(tmp_path)
    if status == "expired":
        pack["valid_before"] = datetime.now(UTC).date().isoformat()
    else:
        pack["publication_status"] = status
    assert freeze_publications(settings) == []


@pytest.mark.parametrize("tamper", ["source_bytes", "review_bytes", "source_sha", "review_sha", "missing_source", "outside_root",
    "unknown_host", "rule_codes", "rule_descriptions", "review_reference", "json_array", "qualification_object", "qualification_blank"])
def test_proof_mismatch_and_unreviewed_rule_definition_fail_closed(tmp_path, tamper):
    pack, settings = approved_publication(tmp_path)
    if tamper == "source_bytes": (tmp_path / "source.txt").write_bytes(b"changed")
    elif tamper == "review_bytes": (tmp_path / "review.json").write_bytes(b"changed")
    elif tamper == "source_sha": pack["source_sha256"] = "0" * 64
    elif tamper == "review_sha": pack["review_sha256"] = "0" * 64
    elif tamper == "missing_source": (tmp_path / "source.txt").unlink()
    elif tamper == "outside_root":
        outside = tmp_path.parent / (tmp_path.name + "-outside-source.txt")
        outside.write_bytes((tmp_path / "source.txt").read_bytes())
        pack["source_artifact"] = str(outside)
    elif tamper == "unknown_host": settings.rule_official_domains = ["official.example.org"]
    elif tamper == "rule_codes": pack["allowed_six_digit_codes"] = ["200899"]
    elif tamper == "rule_descriptions": pack["descriptions"] = ["Processed apples"]
    elif tamper == "review_reference": pack["review_reference"] = "a different unreviewed reference"
    else:
        record = json.loads((tmp_path / "review.json").read_text())
        if tamper == "json_array": record = [record]
        elif tamper == "qualification_object": record["reviewer_qualification_reference"] = {"claimed": "yes"}
        elif tamper == "qualification_blank": record["reviewer_qualification_reference"] = "   "
        payload = json.dumps(record).encode()
        (tmp_path / "review.json").write_bytes(payload)
        pack["review_sha256"] = hashlib.sha256(payload).hexdigest()
    assert freeze_publications(settings) == []


def test_publication_validation_rejects_unreviewed_or_invalid_inputs():
    for change in ({"reviewed_by":""}, {"source_url":"http://example.org"}, {"source_sha256":"none"}, {"allowed_six_digit_codes":["1"]}, {"valid_before":"2025-01-01"}):
        with pytest.raises(ValueError):
            freeze_publications(SimpleNamespace(reviewed_rule_publications=[{**publication(), **change}]))


def test_audit_never_retains_parameter_values_or_endpoint_secrets():
    text=safe_parameter_summary({"api_key":"private", "question":"customer document", "nested":{"token":"bearer", "amount":999}})
    assert "private" not in text and "customer document" not in text and "bearer" not in text and "999" not in text
    assert safe_destination("https://user:password@example.org/tools?key=secret")=="https://example.org"


@pytest.mark.asyncio
async def test_audit_time_is_explicit_and_nonnegative():
    start=datetime.now(UTC)-timedelta(seconds=1)
    out=await audit_context(None,None,{"user_parameters":{"secret":"value"}},start)
    assert out["started_at"]==start and out["duration_ms"]>=1000 and out["finished_at"]>=start


@pytest.mark.asyncio
async def test_extension_calls_http_has_auditable_details_and_hides_expired_case(http_db):
    from uuid import UUID, uuid4
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.conversation.models import Conversation
    from app.documents.models import Case
    from app.extensions.models import ExtensionCall
    from tests.test_workflow_response_contracts import seed_source
    http, conn, principal, created = http_db
    case_id, _ = await seed_source(conn, principal, created)
    started = datetime.now(UTC)
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        conversation = Conversation(tenant_id=principal.tenant_id, owner_user_id=principal.user_id, case_id=UUID(case_id), title="审计回滚验收")
        db.add(conversation)
        await db.flush()
        call = ExtensionCall(tenant_id=principal.tenant_id, owner_user_id=principal.user_id, extension_id=uuid4(), extension_version=1,
            conversation_id=conversation.id, name="受控调用", action="query_case_facts", status="succeeded", result_redacted="已读取冻结字段",
            **await audit_context(db,None,{"user_parameters":{"token":"hidden"}},started))
        db.add(call)
        await db.flush()
        identifier = str(call.id)
    result = await http.get("/api/v1/extension-calls")
    assert result.status_code == 200, result.text
    row = next(item for item in result.json()["items"] if item["call_id"] == identifier)
    assert row["case_ref"] == case_id and row["started_at"] and row["finished_at"] and row["duration_ms"] >= 0
    assert "hidden" not in row["params_redacted"]
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
        case = await db.get(Case,UUID(case_id))
        case.retention_expires_at = started-timedelta(seconds=1)
    expired = await http.get("/api/v1/extension-calls")
    assert expired.status_code == 200 and not any(item["call_id"] == identifier for item in expired.json()["items"])


from tests.test_workflow_response_contracts import http_db
