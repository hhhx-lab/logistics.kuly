"""B06：已有源事实候选与税则未覆盖边界；真实PG均外层回滚。"""
from contextlib import contextmanager
from pathlib import Path
import runpy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.documents import evidence_models as em
from app.review import models as rm
from app.review.rules import RULE_PACK, RULE_PACK_V2, RULE_PACK_V3, evaluate_rules
from app.workflows.models import Run
from tests.test_review_field_gate import seed_fields
from tests.test_workflow_response_contracts import http_db


def facts(right_code="620520"):
    lines = [{"id":"left", "logical_document_id":"A", "description":"Cotton shirts", "hs_code":"610910", "source_segment_ids":["a"]},
             {"id":"right", "logical_document_id":"B", "description":"Cotton shirts", "hs_code":right_code, "source_segment_ids":["b"]}]
    sources = [{"id":"a", "logical_document_id":"A", "text":"Cotton shirts HS 610910"},
               {"id":"b", "logical_document_id":"B", "text":"Cotton shirts HS "+right_code}]
    edge = {"id":"edge", "left_line_id":"left", "right_line_id":"right", "state":"confirmed", "relation":"one_to_one", "decided_by":"reviewer",
            "basis":{"same_goods":True, "complete":True, "scope_key":"verified shipment", "role":"goods", "reason":"比对原件SKU与合同核验同货品"}}
    return lines, sources, [edge]


def check(lines, sources, edges, pack=RULE_PACK_V3):
    return evaluate_rules(documents=[], fields=[], lines=lines, source_rows=sources, line_matches=edges, rule_pack=pack)


def test_confirmed_same_goods_different_source_hs_is_yellow_candidate_not_classification():
    lines, sources, edges = facts()
    rules = check(lines, sources, edges)
    result = next(r for r in rules if r.rule_id == "hs.same_goods_source_consistency")
    assert result.status == "failed" and result.severity == "yellow" and not result.blocks_export
    assert result.source_segment_ids == ("a", "b")
    assert [f["description"] for f in result.calculation["facts"]] == ["Cotton shirts"] * 2
    assert result.calculation["line_match_id"] == "edge" and result.calculation["decided_by"] == "reviewer"
    assert next(r for r in rules if r.rule_id == "hs.description_classification").status == "not_covered"


@pytest.mark.parametrize("right_code", ["610910", "61091010", "6109101000"])
def test_same_six_digits_and_country_extensions_never_prove_classification(right_code):
    rules = check(*facts(right_code))
    result = next(r for r in rules if r.rule_id == "hs.same_goods_source_consistency")
    assert result.status == "not_covered" and result.calculation["country_extensions"] == "not_assessed"
    assert not result.blocks_export


@pytest.mark.parametrize("defect", ["no_confirmation", "no_reviewer", "no_reason", "no_scope", "incomplete", "other_role", "not_same_goods",
    "one_to_many", "ambiguous", "no_hs", "no_description", "no_source", "wrong_document", "substring_code", "no_description_source", "numeric_hs", "malformed"])
def test_incomplete_facts_or_mapping_cannot_claim_hs_disagreement(defect):
    lines, sources, edges = facts()
    if defect == "no_confirmation": edges[0]["state"] = "candidate"
    elif defect == "no_reviewer": edges[0]["decided_by"] = None
    elif defect == "no_reason": edges[0]["basis"].pop("reason")
    elif defect == "no_scope": edges[0]["basis"]["scope_key"] = " "
    elif defect == "incomplete": edges[0]["basis"]["complete"] = False
    elif defect == "other_role": edges[0]["basis"]["role"] = "packaging"
    elif defect == "not_same_goods": edges[0]["basis"]["same_goods"] = False
    elif defect == "one_to_many": edges[0]["relation"] = "one_to_many"
    elif defect == "ambiguous": edges.append({**edges[0], "id":"duplicate"})
    elif defect == "no_hs": lines[0]["hs_code"] = None
    elif defect == "no_description": lines[0]["description"] = None
    elif defect == "no_source": lines[0]["source_segment_ids"] = []
    elif defect == "wrong_document": sources[0]["logical_document_id"] = "B"
    elif defect == "substring_code": sources[0]["text"] = "Cotton shirts 16109100"
    elif defect == "no_description_source": sources[0]["text"] = "610910"
    elif defect == "numeric_hs": lines[0]["hs_code"] = 610910
    elif defect == "malformed": lines[0]["hs_code"] = "6109.10"
    result = [r for r in check(lines, sources, edges) if r.rule_id == "hs.same_goods_source_consistency"]
    assert result and all(r.status == "undetermined" and not r.blocks_export for r in result)


def test_line_hs_leading_zero_and_legacy_rule_versions():
    lines, sources, edges = facts("080810")
    rules = check(lines, sources, edges)
    result = next(r for r in rules if r.rule_id == "line.lexical.hs_code" and r.calculation["line_id"] == "right")
    assert result.status == "passed" and result.calculation["raw_value"] == "080810"
    for pack in (RULE_PACK, RULE_PACK_V2):
        rules = check(lines, sources, edges, pack)
        assert not any(r.rule_id.startswith("hs.") or r.rule_id == "line.lexical.hs_code" for r in rules)


def migration():
    return runpy.run_path(str(Path(__file__).parents[1] / "alembic/versions/c2e84a7d910f_明细HS原值.py"))


def upgrade_inside_rollback(connection):
    columns = {c["name"]:c for c in inspect(connection).get_columns("line_items")}
    was_missing = "hs_code" not in columns
    if was_missing:
        with Operations.context(MigrationContext.configure(connection)):
            migration()["upgrade"]()
        # 本次迁移只能添加空列，旧数据不从货描猜HS。
        assert connection.scalar(text("SELECT count(*) FROM line_items WHERE hs_code IS NOT NULL")) == 0
    hs = next(c for c in inspect(connection).get_columns("line_items") if c["name"] == "hs_code")
    assert hs["nullable"] and str(hs["type"]) == "TEXT"
    return was_missing


@pytest.fixture
async def hs_http_db(http_db):
    http, conn, principal, created = http_db
    await conn.execute(text("RESET ROLE"))
    await conn.run_sync(upgrade_inside_rollback)
    await conn.execute(text("SET LOCAL ROLE logistics_app"))
    return http, conn, principal, created


@pytest.mark.asyncio
async def test_migration_schema_and_rollback_leave_database_head_unchanged():
    from app.config import get_settings
    from app.infra.database import create_database_engine
    engine = create_database_engine(get_settings())
    async with engine.connect() as conn:
        before = await conn.run_sync(lambda c:"hs_code" in {x["name"] for x in inspect(c).get_columns("line_items")})
        head = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        await conn.rollback()
        outer = await conn.begin()
        await conn.run_sync(upgrade_inside_rollback)
        await outer.rollback()
        assert await conn.run_sync(lambda c:"hs_code" in {x["name"] for x in inspect(c).get_columns("line_items")}) == before
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_worker_extract_saves_source_hs_and_rules_http_are_idempotent(hs_http_db, monkeypatch):
    http, conn, p, created = hs_http_db
    ids = await seed_fields(conn, p, created)
    from app.infra.model_gateway import Succeeded
    from app.prompts.registry import PromptRegistry
    from app.prompts.schemas import ExtractionResult
    from app.workflows import worker as module
    from app.workflows.translation import TranslationNodes
    registry = PromptRegistry.load()
    prompt = registry.get("document.extract")

    def execute(sync_conn):
        with Session(bind=sync_conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as db, db.begin():
            source = db.scalar(select(em.SourceSegment).where(em.SourceSegment.logical_document_id == ids["logical_id"]))
            source.text = "Fresh apples HS 080810"
            other = em.LogicalDocument(tenant_id=p.tenant_id, case_id=UUID(ids["case_id"]), parse_artifact_id=ids["parse_id"], ordinal=1, type_candidate=em.LogicalDocumentType.PACKING_LIST)
            db.add(other); db.flush()
            section = em.DocumentSection(tenant_id=p.tenant_id, case_id=UUID(ids["case_id"]), parse_artifact_id=ids["parse_id"], logical_document_id=other.id, ordinal=0, kind="body")
            db.add(section); db.flush()
            second = em.SourceSegment(tenant_id=p.tenant_id, case_id=UUID(ids["case_id"]), parse_artifact_id=ids["parse_id"], logical_document_id=other.id, section_id=section.id, ordinal=0, text="Fresh apples HS 200899", structure="paragraph")
            db.add(second); db.flush()
            # 本地返回合法结构化源事实，验证生产Worker保存链；没有供应商外发。
            answers = {str(s.id):ExtractionResult(fields=[], line_items=[{"row_source_segment_ids":[str(s.id)], "cells":[
                {"column_key":"description", "raw_value":"Fresh apples", "source_segment_ids":[str(s.id)]},
                {"column_key":"hs_code", "raw_value":code, "source_segment_ids":[str(s.id)]}]}]) for s,code in [(source,"080810"),(second,"200899")]}
            class Gateway:
                def structured_generate(self, binding, prompts, *args, **kwargs):
                    return Succeeded(answers[prompts[0].variables["segments"][0]["id"]], (), True)
                def aclose(self): return None
            monkeypatch.setattr(module, "build_model_gateway", lambda *args, **kwargs:Gateway())
            class Worker:
                tenant=p.tenant_id; case_id=UUID(ids["case_id"]); run_id=ids["run_id"]; business_id=ids["revision_id"]
                parse_ids=[ids["parse_id"]]; purpose="initial"; input_business_id=None; input_parse_ids=[]
                bindings=[{"role":"extract", "base_url":"https://local.invalid", "model_name":"fixture", "protocol":"openai_chat", "timeout_seconds":10,
                    "max_output_tokens":1000, "max_concurrency":1, "public_config_hash":"0"*64, "adapter_version":"fixture"}]
                prompt_versions={"document.extract":{"version":prompt.version,"sha256":prompt.sha256}}
                spec={"rule_pack_version":RULE_PACK_V3}; factory=None; settings=None
                runner=SimpleNamespace(run=lambda value:value)
                @contextmanager
                def transaction(self): yield db
                def locked(self, db): return db.get(Run, self.run_id)
                def confirmed_type(self, db, ld): return ld.type_candidate.value
                def model_budget(self, binding): return None
                def ensure_time(self, seconds): pass
                def submit_intent(self, step): pass
                def request_key(self, step): return "local-only"
                def complete_step(self, *args): pass
                def step(self, name, inputs, callback): return callback(uuid4())
            worker=Worker();worker.registry=registry
            module.DocumentWorker.extract_fields(worker, {})
            db.flush()
            lines = list(db.scalars(select(em.LineItem).where(em.LineItem.case_id == worker.case_id).order_by(em.LineItem.line_no,em.LineItem.logical_document_id)))
            assert {line.hs_code for line in lines} == {"080810","200899"}
            assert all(line.description == "Fresh apples" for line in lines)
            db.add(em.LineMatchEdge(tenant_id=p.tenant_id, case_id=worker.case_id, left_line_id=lines[0].id, right_line_id=lines[1].id,
                relation="one_to_one", state="confirmed", decided_by=p.user_id,
                basis=json.dumps({"same_goods":True,"complete":True,"scope_key":"fixture contract", "role":"goods", "reason":"核验同SKU原件"})))
            db.flush()
            nodes=TranslationNodes(worker);run=worker.locked(db);parse=db.get(em.ParseArtifact,ids["parse_id"])
            nodes._rules(db,run,[parse]);db.flush()
            check = db.scalar(select(rm.RuleCheck).where(rm.RuleCheck.run_id == run.id, rm.RuleCheck.rule_id == "hs.same_goods_source_consistency"))
            assert check.status == "failed" and len(check.source_segment_ids) == 2
            issue = db.scalar(select(rm.Issue).where(rm.Issue.rule_check_id == check.id))
            assert issue.severity == "yellow" and not issue.blocks_export and len(issue.sources) == 2
            count=db.scalar(select(func.count()).select_from(rm.RuleCheck).where(rm.RuleCheck.run_id == run.id))
            nodes._rules(db,run,[parse]);db.flush()
            assert db.scalar(select(func.count()).select_from(rm.RuleCheck).where(rm.RuleCheck.run_id == run.id)) == count
    await conn.run_sync(execute)
    response=await http.get(f"/api/v1/cases/{ids['case_id']}/line-items",params={"parse_artifact_id":str(ids["parse_id"])})
    assert response.status_code == 200, response.text
    assert {row["hs_code"] for row in response.json()["items"]} == {"080810","200899"}
