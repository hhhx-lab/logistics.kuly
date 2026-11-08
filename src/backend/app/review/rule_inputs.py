"""版本限定的人工规则适用输入，不覆盖源字段、机器规则结论或历史决定。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.errors import InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.documents import evidence_models as em
from app.review import commands as rc
from app.review.models import RuleInputDecision
from app.workflows import commands as wc
from app.workflows.models import RunDocument


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PartyMember(StrictInput):
    logical_document_id: UUID
    role: Literal["seller", "buyer", "shipper", "consignee", "notify_party"]


class PartyMappingInput(StrictInput):
    members: list[PartyMember] = Field(min_length=2, max_length=10)
    compare_fields: list[Literal["name", "address", "company_no"]] = Field(min_length=1, max_length=3)


class PackingTableInput(StrictInput):
    logical_document_id: UUID
    section_id: UUID
    description_column: int = Field(ge=0, le=50)
    count_column: int = Field(ge=0, le=50)
    header_row: int = Field(ge=0)
    total_row: int = Field(ge=2)
    packaging_level: Literal["carton", "pallet", "piece"]
    scope_source_segment_ids: list[UUID] = Field(min_length=1, max_length=10)
    complete: Literal[True]


class TransportDateInput(StrictInput):
    departure_field_id: UUID
    arrival_field_id: UUID
    timezone: str = Field(min_length=1, max_length=80)
    calendar: Literal["gregorian"]


class RuleInputBody(StrictInput):
    run_id: UUID
    expected_business_revision_id: UUID
    expected_business_revision_no: int | None = Field(default=None, ge=1)
    expected_review_state_version: int = Field(ge=1)
    kind: Literal["party_mapping", "packing_table", "date_transport"]
    scope_key: str = Field(min_length=1, max_length=200)
    payload: PartyMappingInput | PackingTableInput | TransportDateInput
    basis: str = Field(min_length=1, max_length=10000)
    reason: str = Field(min_length=1, max_length=2000)


def decision_view(decision):
    return {"decision_id": str(decision.id), "kind": decision.kind, "scope_key": decision.scope_key,
            "payload": decision.payload, "basis": decision.basis, "reason": decision.reason,
            "decided_by": str(decision.decided_by), "created_at": decision.created_at,
            "business_revision_id": str(decision.business_revision_id)}


async def list_rule_inputs(db, principal, settings, case_id, business_revision_id=None, snapshot_id=None):
    from app.documents.retention import load_case_for_read
    case = await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
    snapshot = None
    if snapshot_id:
        from app.exports.models import ExportSnapshot
        snapshot = await db.get(ExportSnapshot, snapshot_id)
        if snapshot is None or snapshot.case_id != case.id:
            raise NotFoundOrForbidden()
        frozen_revision = UUID(snapshot.manifest["business_revision_id"])
        if business_revision_id and business_revision_id != frozen_revision:
            raise NotFoundOrForbidden()
        revision_id = frozen_revision
    else:
        revision_id = business_revision_id or case.current_business_revision_id
    revision = await db.get(em.BusinessRevision, revision_id) if revision_id else None
    if revision is None or revision.case_id != case.id:
        raise NotFoundOrForbidden()
    query = select(RuleInputDecision).where(RuleInputDecision.case_id == case.id, RuleInputDecision.business_revision_id == revision_id)
    if snapshot:
        query = query.where(RuleInputDecision.id.in_([UUID(value) for value in snapshot.manifest.get("rule_input_decision_ids", [])]))
    decisions = list((await db.execute(query.order_by(RuleInputDecision.created_at, RuleInputDecision.id))).scalars())
    return {"business_revision_id": str(revision_id), "review_state_version": snapshot.manifest.get("review_state_version", 1) if snapshot else case.review_state_version,
            "items": [decision_view(decision) for decision in decisions]}


async def confirm_rule_input(db, principal, settings, case_id, body: RuleInputBody):
    if not body.reason.strip() or not body.basis.strip() or not body.scope_key.strip():
        raise InvalidRequest("确认规则适用事实必须填写范围、理由及可核验依据")
    case = await rc.check_business(db, principal, settings, case_id,
        expected_business_revision_id=body.expected_business_revision_id, expected_business_revision_no=body.expected_business_revision_no)
    if case.review_state_version != body.expected_review_state_version:
        raise VersionConflict(current={"review_state_version": case.review_state_version})
    run, _ = await wc.load_run(db, principal, settings, body.run_id, write=True)
    if run.case_id != case.id or run.current_business_revision_id != case.current_business_revision_id:
        raise NotFoundOrForbidden()
    from app.review.jurisdiction import MATERIAL_PACK
    if run.task_spec.get("rule_pack_version") not in {"core-document@3", MATERIAL_PACK}:
        raise InvalidRequest("仅通用核对@3及其一般消费品材料试行扩展支持此规则输入；历史Run不得改变已冻结规则版本，请新建任务")
    allowed_parse = set()
    for document in (await db.execute(select(RunDocument).where(RunDocument.run_id == run.id))).scalars():
        parse = await wc.latest_parse(db, document.document_id, document.document_version)
        if parse:
            allowed_parse.add(parse.id)
    fields = list((await db.execute(select(em.FieldAssertion).join(em.BusinessRevisionField,
        em.BusinessRevisionField.field_assertion_id == em.FieldAssertion.id).where(
        em.BusinessRevisionField.business_revision_id == case.current_business_revision_id,
        em.FieldAssertion.case_id == case.id, em.FieldAssertion.parse_artifact_id.in_(allowed_parse)))).scalars())
    sources = {source.id: source for source in (await db.execute(select(em.SourceSegment).where(
        em.SourceSegment.case_id == case.id, em.SourceSegment.parse_artifact_id.in_(allowed_parse)))).scalars()}
    payload = body.payload.model_dump(mode="json")
    source_ids = set()
    if body.kind == "party_mapping":
        if not isinstance(body.payload, PartyMappingInput):
            raise InvalidRequest("主体映射参数类型不符")
        members = {(member.logical_document_id, member.role) for member in body.payload.members}
        if len(members) != len(body.payload.members) or len(set(body.payload.compare_fields)) != len(body.payload.compare_fields):
            raise InvalidRequest("主体角色或比较字段不可重复")
        selected = []
        for member in body.payload.members:
            local = [field for field in fields if field.logical_document_id == member.logical_document_id and field.role == member.role]
            names = [field for field in local if field.field_key in {"name", member.role + "_name"} and field.source_segment_ids]
            if len(names) != 1:
                raise InvalidRequest("每个主体角色必须有唯一、有来源的名称候选；缺失或多候选请先复核")
            selected.extend(local)
        for field in selected:
            if any(UUID(value) not in sources or sources[UUID(value)].logical_document_id != field.logical_document_id for value in field.source_segment_ids):
                raise InvalidRequest("主体字段来源不属于对应角色逻辑单据")
        source_ids = {UUID(value) for field in selected for value in field.source_segment_ids}
        payload["field_assertion_ids"] = [str(field.id) for field in selected]
    elif body.kind == "packing_table":
        if not isinstance(body.payload, PackingTableInput):
            raise InvalidRequest("箱单汇总参数类型不符")
        table = body.payload
        logical = await db.get(em.LogicalDocument, table.logical_document_id)
        if logical is None or logical.case_id != case.id or logical.parse_artifact_id not in allowed_parse:
            raise NotFoundOrForbidden()
        if table.description_column == table.count_column or table.header_row >= table.total_row - 1:
            raise InvalidRequest("包装表列或汇总行范围不合法")
        cells = [source for source in sources.values() if source.section_id == table.section_id
                 and source.logical_document_id == table.logical_document_id and source.structure == "table_cell"]
        if not cells:
            raise InvalidRequest("所选区段没有结构化表格来源")
        source_ids = set(body.payload.scope_source_segment_ids) | {source.id for source in cells}
        payload["parse_artifact_id"] = str(logical.parse_artifact_id)
    else:
        if not isinstance(body.payload, TransportDateInput):
            raise InvalidRequest("运输日期参数类型不符")
        try:
            ZoneInfo(body.payload.timezone)
        except ZoneInfoNotFoundError:
            raise InvalidRequest("时区必须为有效IANA时区名") from None
        selected = [field for field in fields if field.id in {body.payload.departure_field_id, body.payload.arrival_field_id}]
        if len(selected) != 2 or {field.field_key for field in selected} != {"departure_date", "arrival_date"} or len({field.logical_document_id for field in selected}) != 1:
            raise InvalidRequest("离港和到港日期须分别选当前同一逻辑单据的对应字段")
        for field in selected:
            expected = "departure_date" if field.id == body.payload.departure_field_id else "arrival_date"
            if field.field_key != expected or not field.source_segment_ids:
                raise InvalidRequest("日期用途或直接来源未证实")
        source_ids = {UUID(value) for field in selected for value in field.source_segment_ids}
        payload["field_assertion_ids"] = [str(field.id) for field in selected]
    if not source_ids or not source_ids <= sources.keys():
        raise InvalidRequest("规则输入来源必须属于本次Run当前解析与单票范围")
    payload["source_segment_ids"] = [str(value) for value in sorted(source_ids)]
    existing_decisions = list((await db.execute(select(RuleInputDecision).where(
        RuleInputDecision.business_revision_id == case.current_business_revision_id, RuleInputDecision.kind == body.kind))).scalars())
    for existing in existing_decisions:
        if existing.scope_key == body.scope_key:
            continue
        if body.kind == "party_mapping":
            members = {(member["logical_document_id"], member["role"]) for member in payload["members"]}
            existing_members = {(member["logical_document_id"], member["role"]) for member in existing.payload.get("members", [])}
            overlaps = bool(members & existing_members)
        elif body.kind == "packing_table":
            overlaps = payload["section_id"] == existing.payload.get("section_id")
        else:
            overlaps = bool(set(payload["field_assertion_ids"]) & set(existing.payload.get("field_assertion_ids", [])))
        if overlaps:
            raise InvalidRequest("同一角色、表格或日期不能归入重复范围；修改原scope_key以追加新版本")
    runs = await rc.affected_runs(db, case.id)
    revision = await rc.new_business_revision(db, case, principal.user_id, reason="rule_input")
    # 新版继承其他范围的决定；同scope覆盖只影响新业务版，旧版始终只读。
    old_decisions = list((await db.execute(select(RuleInputDecision).where(
        RuleInputDecision.business_revision_id == body.expected_business_revision_id))).scalars())
    for old in old_decisions:
        if old.kind == body.kind and old.scope_key == body.scope_key:
            continue
        db.add(RuleInputDecision(tenant_id=case.tenant_id, case_id=case.id, business_revision_id=revision.id,
            kind=old.kind, scope_key=old.scope_key, payload=old.payload, basis=old.basis, reason=old.reason,
            decided_by=old.decided_by, created_at=old.created_at))
    decision = RuleInputDecision(tenant_id=case.tenant_id, case_id=case.id, business_revision_id=revision.id,
        kind=body.kind, scope_key=body.scope_key, payload=payload, basis=body.basis, reason=body.reason,
        decided_by=principal.user_id, created_at=datetime.now(UTC))
    db.add(decision)
    case.review_state_version += 1
    await db.flush()
    await rc.invalidate_and_enqueue(db, runs, case, principal=principal, settings=settings, message="人工确认规则适用事实，按新业务版重算")
    from app.documents.retention import touch_material_event
    touch_material_event(case, settings)
    return {"decision_id": str(decision.id), "business_revision_id": str(revision.id), "business_revision_no": revision.revision_no,
            "review_state_version": case.review_state_version, "affected_runs": [str(run.id) for run in runs]}
