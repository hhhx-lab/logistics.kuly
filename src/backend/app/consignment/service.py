"""同步领域服务供Worker复用；API在已有租户事务中run_sync。"""
from __future__ import annotations

from itertools import combinations
from uuid import UUID, uuid4
from sqlalchemy import select, func
from app.api.errors import InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.api.idempotency import request_digest
from app.consignment import models as cm
from app.consignment.dto import ScopePayload, Material
from app.consignment.engine import RULE_VERSION, canonical, identifier, match_candidates, comparison_kinds, compare_measurements, compare_fields, stable_key
from app.documents import evidence_models as em
from app.documents.extraction import normalize_evidence_lines
from app.documents.models import DocumentVersion, SourceDocument, Case
from app.review import models as rm
from app.review.issue_state import issue_resolved
from app.workflows.models import Run, RunDocument, StepExecution, StepStatus


def owned(row, case):
    if row is None or row.case_id != case.id or row.tenant_id != case.tenant_id: raise NotFoundOrForbidden()
    return row


def active_materials(session, case):
    rows = session.scalars(select(DocumentVersion).join(SourceDocument,
        (SourceDocument.id == DocumentVersion.document_id) & (SourceDocument.current_version == DocumentVersion.version))
        .where(DocumentVersion.case_id == case.id).order_by(DocumentVersion.document_id)).all()
    return [Material(document_id=x.document_id, document_version=x.version, sha256=x.sha256) for x in rows]


def validate_materials(session, case, payload):
    if payload.baseline_business_revision_id:owned(session.get(em.BusinessRevision,payload.baseline_business_revision_id),case)
    for material in payload.materials:
        version = session.scalar(select(DocumentVersion).where(DocumentVersion.case_id == case.id,
            DocumentVersion.document_id == material.document_id, DocumentVersion.version == material.document_version))
        if not version or version.sha256 != material.sha256: raise NotFoundOrForbidden()
    logical_ids = set()
    for node in payload.parties + payload.legs + payload.cargo:
        logical_ids.update(node.logical_document_ids)
        for ref in node.evidence_ids:
            segment = session.get(em.SourceSegment, UUID(str(ref)))
            owned(segment, case)
    for lid in logical_ids:
        logical = owned(session.get(em.LogicalDocument, UUID(str(lid))), case)
        parse = owned(session.get(em.ParseArtifact, logical.parse_artifact_id), case)
        if (parse.document_id, parse.document_version) not in {(m.document_id,m.document_version) for m in payload.materials if m.purpose != "excluded"}:
            raise InvalidRequest("Scope node evidence is outside material scope")
    for ref, model in [(x, cm.RelationAssertion) for x in payload.relation_ids] + [(x, cm.CargoMatchGroup) for x in payload.match_group_ids]:
        owned(session.get(model, ref), case)


def ensure_scope(session, case, actor_id, *, materials=None):
    current = session.get(cm.CaseScopeRevision, case.current_scope_revision_id) if case.current_scope_revision_id else None
    material_list = materials if materials is not None else active_materials(session, case)
    if current:
        old = ScopePayload.model_validate(current.payload)
        if materials is None:
            purposes = {m.document_id: m.purpose for m in old.materials}
            material_list = [m.model_copy(update={"purpose": purposes.get(m.document_id, m.purpose)}) for m in material_list]
        if {(m.document_id, m.document_version, m.sha256, m.purpose) for m in old.materials} == {(m.document_id,m.document_version,m.sha256,m.purpose) for m in material_list}:
            return owned(current, case)
        # 上传新增/替换时原关系仅能在仍有效材料间保留，完整性必须重确认。
        payload = old.model_copy(update={"materials": material_list, "relation_ids": [], "match_group_ids": [], "completeness": "unknown"})
        payload = payload.model_copy(update={"parties": [], "legs": [], "cargo": []})
    else: payload = ScopePayload(materials=material_list,baseline_business_revision_id=case.current_business_revision_id)
    return append_scope(session, case, actor_id, payload)


def append_scope(session, case, actor_id, payload):
    validate_materials(session, case, payload)
    if payload.baseline_business_revision_id is None and case.current_business_revision_id:
        payload=payload.model_copy(update={"baseline_business_revision_id":case.current_business_revision_id})
    previous = case.current_scope_revision_id
    no = session.scalar(select(func.max(cm.CaseScopeRevision.revision_no)).where(cm.CaseScopeRevision.case_id == case.id)) or 0
    body = payload.model_dump(mode="json")
    scope = cm.CaseScopeRevision(id=uuid4(), tenant_id=case.tenant_id, case_id=case.id,
        revision_no=no + 1, parent_revision_id=previous, payload=body, fingerprint=request_digest(body), created_by=actor_id)
    session.add(scope); session.flush()
    case.current_scope_revision_id = scope.id
    case.review_state_version += 1
    return scope


def scope_stale(session, case, scope):
    payload = ScopePayload.model_validate(scope.payload)
    current = {m.document_id: m for m in active_materials(session, case)}
    stated = {m.document_id: m for m in payload.materials}
    return any(m.document_id not in current or current[m.document_id].document_version != m.document_version or current[m.document_id].sha256 != m.sha256
               for m in payload.materials if m.purpose != "excluded") or any(x not in stated for x in current)


def check_preconditions(case, command):
    actual = {"expected_case_version":case.case_version, "expected_scope_revision_id":case.current_scope_revision_id,
        "expected_business_revision_id":case.current_business_revision_id, "expected_review_state_version":case.review_state_version}
    if any(getattr(command, key) != val for key,val in actual.items()):
        raise VersionConflict("Comparison input changed", current={k.removeprefix("expected_"): str(v) if isinstance(v,UUID) else v for k,v in actual.items()})


async def freeze_task_scope(db, principal, settings, case, sources, parameters):
    scope_id = parameters.get("scope_revision_id")
    if scope_id:
        scope = owned(await db.get(cm.CaseScopeRevision, UUID(str(scope_id))), case)
        if scope.id != case.current_scope_revision_id or await db.run_sync(lambda s: scope_stale(s,case,scope)):
            raise VersionConflict("Scope has changed; prepare a new task")
    else: scope = await db.run_sync(lambda s: ensure_scope(s,case,principal.user_id))
    payload = ScopePayload.model_validate(scope.payload)
    comparison = parameters.get("comparison_source_versions") or [
        {"document_id":str(m.document_id),"document_version":m.document_version} for m in payload.materials if m.purpose != "excluded"]
    allowed = {(str(m.document_id),m.document_version) for m in payload.materials if m.purpose != "excluded"}
    if {(str(x["document_id"]), int(x["document_version"])) for x in comparison} != allowed:
        raise InvalidRequest("Comparison sources must match the declared scope; exclude unrelated materials in scope first")
    if not comparison or any((str(x["document_id"]),int(x["document_version"])) not in allowed for x in comparison + sources):
        raise InvalidRequest("Task materials are outside comparison scope")
    if not {(str(x["document_id"]),int(x["document_version"])) for x in sources}.issubset({(str(x["document_id"]),int(x["document_version"])) for x in comparison}):
        raise InvalidRequest("Comparison scope must contain translation materials")
    result = {**parameters,"scope_revision_id":str(scope.id)}
    if parameters.get("translation_scope") is not None or "target_language" in parameters: result["comparison_source_versions"] = comparison
    return result


async def validate_task_scope(db, principal, settings, case, sources, parameters):
    await freeze_task_scope(db,principal,settings,case,sources,parameters)


async def freeze_scope(db, principal, settings, case_id, sources, expected_scope=None):
    from app.documents.retention import load_case_for_write
    case = await load_case_for_write(db,case_id,user_id=principal.user_id,settings=settings)
    params = await freeze_task_scope(db,principal,settings,case,sources,{"scope_revision_id":str(expected_scope)} if expected_scope else {})
    return await db.get(cm.CaseScopeRevision,UUID(params["scope_revision_id"]))


def assemble_revision(session, run, case, scope, parse_ids, assertion_ids):
    payload = ScopePayload.model_validate(scope.payload)
    allowed = {(m.document_id,m.document_version) for m in payload.materials if m.purpose != "excluded"}
    parses = [owned(session.get(em.ParseArtifact, UUID(str(x))),case) for x in parse_ids]
    if any((p.document_id,p.document_version) not in allowed for p in parses): raise InvalidRequest("Parse is outside frozen scope")
    by_material = {(p.document_id,p.document_version):p for p in parses}
    for material in allowed:
        if material not in by_material:
            p = session.scalar(select(em.ParseArtifact).where(em.ParseArtifact.case_id==case.id,
                em.ParseArtifact.document_id==material[0], em.ParseArtifact.document_version==material[1],
                em.ParseArtifact.quality != em.ParseQuality.FAILED).order_by(em.ParseArtifact.created_at.desc(),em.ParseArtifact.id.desc()).limit(1))
            if not p: raise InvalidRequest("All frozen comparison materials must be parsed")
            by_material[material]=p
    parses=list(by_material.values()); valid_parse_ids={p.id for p in parses}
    incoming=[owned(session.get(em.FieldAssertion,UUID(str(x))),case) for x in assertion_ids]
    if any(f.parse_artifact_id not in valid_parse_ids for f in incoming): raise InvalidRequest("Assertion is outside frozen parse scope")
    revision_ids={x for x in (case.current_business_revision_id,run.current_business_revision_id,payload.baseline_business_revision_id) if x}
    current_fields=list(session.scalars(select(em.FieldAssertion).join(em.BusinessRevisionField,
        em.BusinessRevisionField.field_assertion_id==em.FieldAssertion.id).where(
        em.BusinessRevisionField.business_revision_id.in_(revision_ids),em.FieldAssertion.case_id==case.id))) if revision_ids else []
    # 仅装配已采用版本的成员。历史候选、历史人工断言不按创建时间重新采用。
    selected={f.id:f for f in current_fields + incoming if f.parse_artifact_id in valid_parse_ids}
    for field in tuple(selected.values()):
        if field.is_human and field.supersedes_assertion_id:selected.pop(field.supersedes_assertion_id,None)
    baseline = session.get(em.BusinessRevision, run.current_business_revision_id) if run.current_business_revision_id else None
    decisions = {d.section_id: d.logical_document_type for d in session.scalars(select(em.SectionAssignmentDecision).where(
        em.SectionAssignmentDecision.business_revision_id == run.current_business_revision_id,
        em.SectionAssignmentDecision.logical_document_type.is_not(None)))} if baseline else {}
    for p in parses:
        if not any(f.parse_artifact_id==p.id for f in selected.values()):
            # 通用材料完成抽取后可没有物流字段；不把空结果当成未完成识别。
            logicals = list(session.scalars(select(em.LogicalDocument).where(em.LogicalDocument.parse_artifact_id == p.id)))
            sections = list(session.scalars(select(em.DocumentSection).where(em.DocumentSection.parse_artifact_id == p.id)))
            types = {logical.id: logical.type_candidate.value for logical in logicals}
            generic = bool(logicals and sections) and all(
                decisions.get(section.id, types.get(section.logical_document_id)) == "generic_document" for section in sections)
            extracted = generic and all(session.scalar(select(StepExecution.id).where(
                StepExecution.run_id == run.id, StepExecution.status == StepStatus.SUCCEEDED,
                StepExecution.step_name.startswith(f"抽取:{logical.id}:"))) for logical in logicals)
            if (str(p.id) in {str(value) for value in parse_ids} and baseline and baseline.case_id == case.id
                and baseline.reason in {"baseline_extraction", "consignment_scope"} and extracted):
                continue
            raise InvalidRequest("Frozen document has no adopted field view; index it before comparison")
    no=session.scalar(select(func.max(em.BusinessRevision.revision_no)).where(em.BusinessRevision.case_id==case.id)) or 0
    revision=em.BusinessRevision(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,revision_no=no+1,
        parent_revision_id=case.current_business_revision_id,reason="consignment_scope",created_by=run.initiated_by)
    session.add(revision);session.flush()
    for field in selected.values(): session.add(em.BusinessRevisionField(tenant_id=case.tenant_id,business_revision_id=revision.id,field_assertion_id=field.id))
    old_decisions=list(session.scalars(select(em.SectionAssignmentDecision).where(
        em.SectionAssignmentDecision.business_revision_id==case.current_business_revision_id))) if case.current_business_revision_id else []
    for d in old_decisions:
        section=session.get(em.DocumentSection,d.section_id)
        if section and section.parse_artifact_id in valid_parse_ids:
            session.add(em.SectionAssignmentDecision(tenant_id=case.tenant_id,case_id=case.id,business_revision_id=revision.id,
                section_id=d.section_id,logical_document_type=d.logical_document_type,target_logical_document_id=d.target_logical_document_id,
                confirmed_language=d.confirmed_language,cargo_profile_ids=d.cargo_profile_ids,decided_by=d.decided_by,reason=d.reason,created_at=d.created_at))
    rule_decisions={}
    for prior_id in (payload.baseline_business_revision_id,case.current_business_revision_id,run.current_business_revision_id):
        if not prior_id:continue
        for d in session.scalars(select(rm.RuleInputDecision).where(rm.RuleInputDecision.business_revision_id==prior_id)):
            rule_decisions[(d.kind,d.scope_key)]=d
    for d in rule_decisions.values():
        source_ids=d.payload.get("source_segment_ids",[])
        sources=[session.get(em.SourceSegment,UUID(str(x))) for x in source_ids]
        if any(not source or source.case_id!=case.id or source.parse_artifact_id not in valid_parse_ids for source in sources):continue
        session.add(rm.RuleInputDecision(tenant_id=case.tenant_id,case_id=case.id,business_revision_id=revision.id,
            kind=d.kind,scope_key=d.scope_key,payload=d.payload,basis=d.basis,reason=d.reason,
            decided_by=d.decided_by,created_at=d.created_at))
    run.current_business_revision_id=revision.id
    if case.current_scope_revision_id==scope.id and not scope_stale(session,case,scope):
        case.current_business_revision_id=revision.id;case.business_revision_no=revision.revision_no
    return revision,parses,list(selected.values())


def prepare_comparison(session, run, case, *, scope_revision_id, parse_ids, assertion_ids, parent_batch_id=None):
    previous=session.scalar(select(cm.ComparisonBatch).where(cm.ComparisonBatch.run_id==run.id,cm.ComparisonBatch.epoch==run.current_epoch))
    if previous:return previous
    scope=owned(session.get(cm.CaseScopeRevision,UUID(str(scope_revision_id))),case)
    revision,parses,fields=assemble_revision(session,run,case,scope,parse_ids,assertion_ids)
    payload=ScopePayload.model_validate(scope.payload)
    lines=list(session.scalars(select(em.LineItem).where(em.LineItem.case_id==case.id,em.LineItem.parse_artifact_id.in_([p.id for p in parses]))))
    raw_line_ids = sorted(str(line.id) for line in lines)
    sources = list(session.scalars(select(em.SourceSegment).where(
        em.SourceSegment.case_id == case.id, em.SourceSegment.parse_artifact_id.in_([p.id for p in parses]))))
    source_map = {str(source.id): source.text for source in sources}
    anchors = {str(anchor.source_segment_id): anchor for anchor in session.scalars(select(em.SourceAnchor).where(
        em.SourceAnchor.parse_artifact_id.in_([p.id for p in parses])))}
    table_rows = {str(source.id): (str(source.section_id), anchor.page_no, cell["table_key"], source.table_row)
        for source in sources if source.structure == "table_cell" and source.table_row is not None
        and (anchor := anchors.get(str(source.id))) is not None
        and (cell := (anchor.transform or {}).get("table_cell") or {}).get("table_key")}
    lines, line_aliases = normalize_evidence_lines(lines, source_map, table_rows)
    group_ids,relation_ids=freeze_relationships(session,case,scope,payload,lines,fields,line_aliases=line_aliases)
    manifest={"schema_version":1,"scope_fingerprint":scope.fingerprint,"scope_revision_id":str(scope.id),
        "materials":payload.model_dump(mode="json")["materials"], "parses":[{"id":str(p.id),"document_id":str(p.document_id),"version":p.document_version,"config_hash":p.parser_config_hash} for p in parses],
        "assertion_ids":sorted(str(f.id) for f in fields),"line_ids":sorted(str(l.id) for l in lines),
        "raw_line_ids": raw_line_ids, "duplicate_line_aliases": line_aliases,
        "relation_ids":[str(x) for x in relation_ids],"match_group_ids":[str(x) for x in group_ids],
        "rule_version":RULE_VERSION,"prompt_versions":run.prompt_versions,"business_revision_id":str(revision.id)}
    batch=cm.ComparisonBatch(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,run_id=run.id,epoch=run.current_epoch,
        scope_revision_id=scope.id,business_revision_id=revision.id,parent_batch_id=parent_batch_id,
        manifest=manifest,input_fingerprint=request_digest(manifest),status="building",summary={})
    session.add(batch);session.flush()
    return batch


def freeze_relationships(session,case,scope,payload,lines,fields,*,line_aliases=None):
    line_aliases = line_aliases or {}
    groups=list(payload.match_group_ids);relations=list(payload.relation_ids)
    frozen_line_ids={line.id for line in lines}
    covered=set()
    for gid in groups:
        group=owned(session.get(cm.CargoMatchGroup,gid),case)
        members=list(session.scalars(select(cm.CargoMatchMember).where(cm.CargoMatchMember.group_id==gid)))
        left_ids,right_ids=set(),set()
        for member in members:
            projected_id = UUID(line_aliases.get(str(member.line_item_id), str(member.line_item_id)))
            if projected_id not in frozen_line_ids:raise InvalidRequest("Confirmed cargo group contains stale material")
            (left_ids if member.side=="left" else right_ids).add(projected_id)
        covered.add((frozenset((frozenset(left_ids),frozenset(right_ids))), group.scope.get("quantity_kind", "quantity")))
    for left,right,identity in match_candidates(lines):
        pair = frozenset((frozenset(x.id for x in left),frozenset(x.id for x in right)))
        logicals={x.logical_document_id for x in left+right}
        cargos=[x for x in payload.cargo if logicals.issubset(set(x.logical_document_ids))]
        legs=[x for x in payload.legs if logicals.issubset(set(x.logical_document_ids))]
        proved=len(cargos)==len(legs)==1 and payload.completeness=="complete" and bool(identity["lot_no"] or identity["specification"])
        common={"cargo_id":cargos[0].id if len(cargos)==1 else None,"leg_id":legs[0].id if len(legs)==1 else None,
            "quantity_kind":"quantity","completeness":payload.completeness if proved else "unknown","identity":identity}
        for kind in comparison_kinds(left, right):
            if (pair, kind) in covered: continue
            group=cm.CargoMatchGroup(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,scope_revision_id=scope.id,
                state="auto_confirmed" if proved else "candidate",cardinality="1:1" if len(left)==len(right)==1 else "1:n" if len(left)==1 else "n:1",
                scope={**common, "quantity_kind":kind},basis={"reason":"Unique SKU/lot/specification candidate with declared cargo/stage/coverage" if proved else "Unique SKU/lot/specification candidate; scope and completeness await confirmation"})
            session.add(group);session.flush();groups.append(group.id)
            for side,items in (("left",left),("right",right)):
                for x in items:session.add(cm.CargoMatchMember(tenant_id=case.tenant_id,case_id=case.id,group_id=group.id,side=side,line_item_id=x.id,basis={}))
    grouped={}
    for f in fields:grouped.setdefault(f.logical_document_id,[]).append(f)
    existing=[owned(session.get(cm.RelationAssertion,x),case) for x in relations]
    for party in payload.parties:
        logical_ids = [logical for logical in party.logical_document_ids
            if any(field.role == party.role for field in grouped.get(logical, []))]
        for left_id, right_id in combinations(logical_ids, 2):
            if any(relation.kind == "same_party" and relation.basis.get("role") == party.role
                and {relation.left_logical_id, relation.right_logical_id} == {left_id, right_id} for relation in existing): continue
            relation=cm.RelationAssertion(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,scope_revision_id=scope.id,
                kind="same_party",state="confirmed",left_logical_id=left_id,right_logical_id=right_id,
                decided_by=scope.created_by,
                basis={"declared_party_id":party.id,"role":party.role,"same_semantics":True,"reason":"User declared the same party in the same document role"})
            session.add(relation);session.flush();relations.append(relation.id);existing.append(relation)
    for left_id,right_id in combinations(sorted(grouped,key=str),2):
        if any({r.left_logical_id,r.right_logical_id}=={left_id,right_id} for r in existing):continue
        shared=[(l,r) for l in grouped[left_id] for r in grouped[right_id] if l.field_key==r.field_key and l.field_key in {"invoice_no","invoice_number","order_no","order_number"}
            and identifier(l.normalized_value or l.candidate_value) and identifier(l.normalized_value or l.candidate_value)==identifier(r.normalized_value or r.candidate_value)
            and l.source_segment_ids and r.source_segment_ids]
        if shared:
            relation=cm.RelationAssertion(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,scope_revision_id=scope.id,
                kind="explicit_reference",state="auto_confirmed",left_logical_id=left_id,right_logical_id=right_id,
                basis={"support":source_targets(session,case,list(shared[0]),["left","right"]),"same_semantics":True})
            session.add(relation);session.flush();relations.append(relation.id)
    session.flush()
    return groups,relations


def source_targets(session, case, objects, sides, *, quantity_kind=None):
    result=[]
    for obj,side in zip(objects,sides):
        parse=owned(session.get(em.ParseArtifact,obj.parse_artifact_id),case)
        segment_ids=obj.source_segment_ids
        if quantity_kind and isinstance(obj,em.LineItem):
            segment_ids=(obj.cell_sources or {}).get(quantity_kind) or segment_ids
        for sid in segment_ids:
            segment=owned(session.get(em.SourceSegment,UUID(str(sid))),case)
            if segment.parse_artifact_id!=parse.id:raise NotFoundOrForbidden()
            anchor=session.scalar(select(em.SourceAnchor).where(em.SourceAnchor.source_segment_id==segment.id).order_by(em.SourceAnchor.id).limit(1))
            result.append({"side":side,"target":{"case_id":str(case.id),"document_id":str(parse.document_id),"document_version":parse.document_version,
                "parse_artifact_id":str(parse.id),"logical_document_id":str(obj.logical_document_id),"section_id":str(segment.section_id),
                "source_segment_id":str(segment.id),"source_anchor_id":str(anchor.id) if anchor else None,
                **({"line_item_id":str(obj.id)} if isinstance(obj,em.LineItem) else {"field_id":str(obj.id)})},
                "excerpt":segment.text[:600],"precision":anchor.precision.value if anchor else "none"})
    return result


def store_check(session,run,case,batch,rule,objects,sides,result,basis,group_id=None):
    key=stable_key(rule,[o.id for o in objects],result.get("scope",{}),basis)
    sources=source_targets(session,case,objects,sides,quantity_kind=result.get("kind"))
    calculation={**result,"group_id":str(group_id) if group_id else None,"sources":sources}
    check=rm.RuleCheck(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=batch.business_revision_id,
        comparison_batch_id=batch.id,comparison_basis=basis,stable_check_key=key,rule_id=rule,rule_version=1,
        rule_pack_version=RULE_VERSION,status=result["status"],explanation=result["explanation"],
        input_field_ids=[str(o.id) for o in objects if isinstance(o,em.FieldAssertion)],
        source_segment_ids=sorted({sid for o in objects for sid in o.source_segment_ids}),calculation=calculation,input_fingerprint=request_digest(calculation))
    session.add(check);session.flush()
    if result["status"] not in {"failed","undetermined"}:return check
    from app.review.consignment_skill import comparison_guidance
    guidance=comparison_guidance({**result,"basis":basis},rule)
    issue=rm.Issue(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,run_id=run.id,business_revision_id=batch.business_revision_id,
        comparison_batch_id=batch.id,stable_issue_key=key,rule_check_id=check.id,category="conflict" if result["status"]=="failed" else "undetermined",
        severity=guidance["severity"],blocks_export=guidance["blocks_export"],title=rule,description=guidance["explanation"],sources=sources,
        guidance={**guidance,"comparison_scope":result.get("scope",{})})
    session.add(issue)
    return check


def publish_comparison(session,run,case,batch):
    owned(batch,case)
    if batch.run_id!=run.id or batch.epoch!=run.current_epoch:raise VersionConflict("Comparison execution is stale")
    if batch.status=="completed":return {"batch_id":str(batch.id),**batch.summary}
    scope=owned(session.get(cm.CaseScopeRevision,batch.scope_revision_id),case)
    payload=ScopePayload.model_validate(scope.payload)
    lines=[owned(session.get(em.LineItem,UUID(x)),case) for x in batch.manifest["line_ids"]]
    fields=[owned(session.get(em.FieldAssertion,UUID(x)),case) for x in batch.manifest["assertion_ids"]]
    groups=[owned(session.get(cm.CargoMatchGroup,UUID(str(x))),case) for x in batch.manifest["match_group_ids"]]
    covered=set()
    pending=0
    for group in groups:
        if group.state=="rejected":continue
        if group.state not in {"confirmed","auto_confirmed"}:pending+=1
        members=list(session.scalars(select(cm.CargoMatchMember).where(cm.CargoMatchMember.group_id==group.id)))
        aliases = batch.manifest.get("duplicate_line_aliases", {})
        def projected_side(side):
            projected = {UUID(aliases.get(str(m.line_item_id),str(m.line_item_id))) for m in members if m.side == side}
            return [owned(session.get(em.LineItem,line_id),case) for line_id in sorted(projected,key=str)]
        left,right=projected_side("left"),projected_side("right")
        frozen=set(batch.manifest["line_ids"])
        if not left or not right or any(str(x.id) not in frozen for x in left+right):raise InvalidRequest("Match group is outside frozen scope")
        pair_scope=tuple(sorted({str(x.logical_document_id) for x in left+right}))
        usage={(str(x.id),group.scope.get("quantity_kind","quantity"),group.scope.get("cargo_id"),group.scope.get("leg_id"),pair_scope) for x in left+right}
        if usage & covered:raise InvalidRequest("Cargo flow is counted more than once")
        covered.update(usage)
        result=compare_measurements(left,right,group.scope,group.state)
        for basis in ("source","adopted"):
            store_check(session,run,case,batch,"goods."+group.scope.get("quantity_kind","quantity"),left+right,
                ["left"]*len(left)+["right"]*len(right),result,basis,group.id)
    # 原文引用仅证明显式引用关系；主体名称相同不自动证明主体角色或阶段。
    grouped={}
    for field in fields:grouped.setdefault(field.logical_document_id,[]).append(field)
    relations=[owned(session.get(cm.RelationAssertion,UUID(str(x))),case) for x in batch.manifest["relation_ids"]]
    for relation in relations:
        lf,rf=grouped.get(relation.left_logical_id,[]),grouped.get(relation.right_logical_id,[])
        for key in sorted({f.field_key for f in lf}&{f.field_key for f in rf}):
            ls=[f for f in lf if f.field_key==key];rs=[f for f in rf if f.field_key==key]
            if relation.kind == "same_party" and relation.basis.get("role"):
                ls=[field for field in ls if field.role == relation.basis["role"]]
                rs=[field for field in rs if field.role == relation.basis["role"]]
            def unique(values):
                keys={(field.role,canonical(field.normalized_value or field.candidate_value)) for field in values}
                return values[:1] if len(keys) == 1 else values
            ls,rs=unique(ls),unique(rs)
            if len(ls)!=1 or len(rs)!=1:continue
            l,r=ls[0],rs[0]
            reference=key in {"invoice_no","invoice_number","order_no","order_number"}
            same_role=l.role and l.role==r.role
            if not reference and (relation.kind!="same_party" or not same_role):continue
            kind="document.reference" if reference else "party."+key
            ctx={"relation_id":str(relation.id),"kind":key,"field_key":key,"role":l.role,
                "same_semantics":bool(reference and relation.kind=="explicit_reference" or relation.kind=="same_party" and same_role)}
            source_l=session.get(em.FieldAssertion,l.supersedes_assertion_id) if l.is_human and l.supersedes_assertion_id else l
            source_r=session.get(em.FieldAssertion,r.supersedes_assertion_id) if r.is_human and r.supersedes_assertion_id else r
            for result in compare_fields(l,r,kind=kind,state=relation.state,scope=ctx):
                if result["basis"]=="source" and (source_l!=l or source_r!=r):
                    result=compare_fields(source_l,source_r,kind=kind,state=relation.state,scope=ctx)[0]
                store_check(session,run,case,batch,kind,[l,r],["left","right"],result,result["basis"])
    session.flush()
    issues=list(session.scalars(select(rm.Issue).where(rm.Issue.comparison_batch_id==batch.id)))
    checks=list(session.scalars(select(rm.RuleCheck).where(rm.RuleCheck.comparison_batch_id==batch.id)))
    summary={"checks":len(checks),"red":sum(i.severity=="red" for i in issues),"yellow":sum(i.severity=="yellow" for i in issues),
        "unknown":sum(i.severity in {"unknown","undetermined"} for i in issues),"pending_relations":pending,"files":len(batch.manifest["materials"]),
        "rules_status":"completed" if checks else "no_comparable_objects"}
    batch.status="completed";batch.summary=summary
    if case.current_scope_revision_id==scope.id and case.current_business_revision_id==batch.business_revision_id and not scope_stale(session,case,scope):
        case.current_comparison_batch_id=batch.id;case.review_state_version+=1
    session.flush()
    return {"batch_id":str(batch.id),**summary}


def batch_current(session,case,batch):
    scope=owned(session.get(cm.CaseScopeRevision,batch.scope_revision_id),case)
    return batch.status=="completed" and case.current_scope_revision_id==batch.scope_revision_id and case.current_business_revision_id==batch.business_revision_id and not scope_stale(session,case,scope)


def bind_comparison(session,run,case,batch,*,delivery_document_ids=None):
    owned(batch,case)
    if not batch_current(session,case,batch):raise VersionConflict("Comparison dependencies are stale")
    expected=run.task_spec.get("comparison_source_versions") or run.task_spec.get("source_versions",[])
    expected_set={(str(x["document_id"]),int(x["document_version"])) for x in expected}
    actual_set={(x["document_id"],x["document_version"]) for x in batch.manifest["materials"] if x["purpose"]!="excluded"}
    if expected_set!=actual_set:raise InvalidRequest("Comparison materials are incompatible with frozen task")
    if batch.manifest["rule_version"]!=run.task_spec.get("comparison_rule_version",RULE_VERSION):raise InvalidRequest("Comparison rule version differs")
    if run.current_business_revision_id!=batch.business_revision_id:raise VersionConflict("Translation business revision needs revalidation")
    delivery=delivery_document_ids or [str(x["document_id"]) for x in run.task_spec.get("source_versions",[])]
    if not set(delivery).issubset({x[0] for x in actual_set}):raise InvalidRequest("Delivery is outside compared material scope")
    old=session.get(cm.RunComparisonBinding,run.current_comparison_binding_id) if run.current_comparison_binding_id else None
    if old and old.batch_id==batch.id and old.delivery_document_ids==delivery:return old
    binding=cm.RunComparisonBinding(id=uuid4(),tenant_id=case.tenant_id,case_id=case.id,run_id=run.id,batch_id=batch.id,
        delivery_document_ids=delivery,dependency_fingerprint=batch.input_fingerprint,supersedes_id=old.id if old else None)
    session.add(binding);session.flush();run.current_comparison_binding_id=binding.id
    return binding


def bound_batch(session,run,case,*,strict_current=True):
    if not run.current_comparison_binding_id:return None
    binding=owned(session.get(cm.RunComparisonBinding,run.current_comparison_binding_id),case)
    if binding.run_id!=run.id:raise NotFoundOrForbidden()
    batch=owned(session.get(cm.ComparisonBatch,binding.batch_id),case)
    if strict_current and not batch_current(session,case,batch) or binding.dependency_fingerprint!=batch.input_fingerprint:return None
    return batch


def comparison_owned_rule(run,rule_id):
    return run.workflow_type=="translate_review" and run.workflow_version>=2 and (
        rule_id.startswith("cross_document.") or rule_id=="party.role_mapping" or rule_id.startswith("party.confirmed_mapping"))


def review_issues(session,run,case,*,strict_current=True):
    local=list(session.scalars(select(rm.Issue).where(rm.Issue.run_id==run.id,rm.Issue.comparison_batch_id.is_(None),
        rm.Issue.business_revision_id==run.current_business_revision_id)))
    local=[i for i in local if not i.translation_revision_id or i.translation_revision_id==run.current_translation_revision_id]
    if run.workflow_type=="translate_review" and run.workflow_version>=2:
        checks={check.id:check for check in session.scalars(select(rm.RuleCheck).where(
            rm.RuleCheck.id.in_([issue.rule_check_id for issue in local if issue.rule_check_id])))}
        local=[issue for issue in local if issue.rule_check_id not in checks or
            not comparison_owned_rule(run,checks[issue.rule_check_id].rule_id)]
    batch=bound_batch(session,run,case,strict_current=strict_current)
    if not batch:return sorted(local,key=lambda issue:str(issue.id))
    binding=session.get(cm.RunComparisonBinding,run.current_comparison_binding_id)
    for issue in session.scalars(select(rm.Issue).where(rm.Issue.comparison_batch_id==batch.id)):
        documents={x.get("target",x).get("document_id") for x in issue.sources}
        if issue.guidance.get("case_wide") or documents & set(binding.delivery_document_ids):local.append(issue)
    return sorted({i.id:i for i in local}.values(),key=lambda i:str(i.id))


def review_checks(session,run,case,*,strict_current=True):
    local=list(session.scalars(select(rm.RuleCheck).where(rm.RuleCheck.run_id==run.id,
        rm.RuleCheck.comparison_batch_id.is_(None),rm.RuleCheck.business_revision_id==run.current_business_revision_id)))
    local=[check for check in local if not comparison_owned_rule(run,check.rule_id)]
    batch=bound_batch(session,run,case,strict_current=strict_current)
    if batch:local.extend(session.scalars(select(rm.RuleCheck).where(rm.RuleCheck.comparison_batch_id==batch.id)))
    return sorted({check.id:check for check in local}.values(),key=lambda check:str(check.id))


def comparison_prompts(versions):
    return {key:value for key,value in versions.items() if key in {"document.extract","document.classify","document.vision_region"} or key.startswith("consignment.")}


def business_signature(session,case,revision_id):
    if not revision_id:return None
    owned(session.get(em.BusinessRevision,revision_id),case)
    fields=sorted(str(x) for x in session.scalars(select(em.BusinessRevisionField.field_assertion_id).where(
        em.BusinessRevisionField.business_revision_id==revision_id)))
    sections=[{"section":str(d.section_id),"type":d.logical_document_type,"logical":str(d.target_logical_document_id),
        "language":d.confirmed_language,"cargo":sorted(d.cargo_profile_ids or [])} for d in session.scalars(
            select(em.SectionAssignmentDecision).where(em.SectionAssignmentDecision.business_revision_id==revision_id))]
    decisions=[{"kind":d.kind,"scope_key":d.scope_key,"payload":d.payload} for d in session.scalars(
        select(rm.RuleInputDecision).where(rm.RuleInputDecision.business_revision_id==revision_id))]
    return request_digest({"fields":fields,"sections":sorted(sections,key=lambda x:x["section"]),
        "rules":sorted(decisions,key=lambda x:(x["kind"],x["scope_key"]))})


def binding_incompatibility(session,case,run,batch):
    if not batch_current(session,case,batch):return "COMPARISON_STALE"
    expected=run.task_spec.get("comparison_source_versions") or run.task_spec.get("source_versions",[])
    expected_set={(str(x["document_id"]),int(x["document_version"])) for x in expected}
    actual_set={(x["document_id"],x["document_version"]) for x in batch.manifest["materials"] if x["purpose"]!="excluded"}
    if expected_set!=actual_set:return "COMPARISON_MATERIALS_CHANGED"
    if run.task_spec.get("comparison_rule_version",RULE_VERSION)!=batch.manifest["rule_version"]:return "COMPARISON_RULE_CHANGED"
    if comparison_prompts(run.prompt_versions)!=comparison_prompts(batch.manifest["prompt_versions"]):return "COMPARISON_PROMPT_CHANGED"
    old=bound_batch(session,run,case,strict_current=False)
    # 已绑定批次是比较解析基线；禁止用新解析冒充既有译文来源。
    if old:
        prior_materials={(m["document_id"],m["document_version"],m["sha256"]) for m in old.manifest["materials"] if m["purpose"]!="excluded"}
        new_materials={(m["document_id"],m["document_version"],m["sha256"]) for m in batch.manifest["materials"] if m["purpose"]!="excluded"}
        if prior_materials!=new_materials:return "COMPARISON_MATERIALS_CHANGED"
    if old and sorted(old.manifest.get("parses",[]),key=lambda p:p["id"])!=sorted(batch.manifest.get("parses",[]),key=lambda p:p["id"]):return "COMPARISON_PARSE_CHANGED"
    if business_signature(session,case,run.current_business_revision_id)!=business_signature(session,case,batch.business_revision_id):return "COMPARISON_FACTS_NOT_ADOPTED"
    return None


def _schedule_binding_qa(session,case,run,batch):
    from app.workflows.models import ExecutionEpoch,QueueOutbox,RunStatus,RunEvent
    if run.status in {RunStatus.PROCESSING,RunStatus.QUEUED,RunStatus.EXPORTING,RunStatus.PAUSED_RECONCILE,RunStatus.CANCELLED}:return False
    prior=session.scalar(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id).order_by(ExecutionEpoch.epoch.desc()).limit(1))
    run.current_business_revision_id=batch.business_revision_id
    run.current_epoch+=1;run.execution_fence+=1;run.status=RunStatus.QUEUED;run.current_stage="核对完成，等待译文QA与预览"
    delivery={str(v["document_id"]) for v in run.task_spec["source_versions"]}
    parse_ids=[p["id"] for p in batch.manifest["parses"] if p["document_id"] in delivery]
    session.add(ExecutionEpoch(tenant_id=case.tenant_id,run_id=run.id,epoch=run.current_epoch,purpose="quality_preview",
        graph_version=f"{run.workflow_type}@{run.workflow_version}",input_business_revision_id=batch.business_revision_id,
        input_parse_artifact_ids=parse_ids,model_bindings=prior.model_bindings if prior else [],
        repair_request={"comparison_batch_id":str(batch.id)}))
    session.add(QueueOutbox(tenant_id=case.tenant_id,topic="run",dedupe_key=f"run:{run.id}:{run.current_epoch}",payload={"run_id":str(run.id),"epoch":run.current_epoch}))
    run.next_event_seq+=1
    session.add(RunEvent(tenant_id=case.tenant_id,run_id=run.id,seq=run.next_event_seq,event_type="comparison.qa_pending",
        status=run.status.value,stage=run.current_stage,message="新核对结果已完成，沿用源文件核验译文后绑定",payload={"batch_id":str(batch.id)}))
    return True


def try_rebind_current_for_run(session,case,run):
    if run.workflow_type!="translate_review" or run.workflow_version<2 or not case.current_comparison_batch_id:return None
    batch=owned(session.get(cm.ComparisonBatch,case.current_comparison_batch_id),case)
    if binding_incompatibility(session,case,run,batch):return None
    revision=session.get(rm.TranslationRevision,run.current_translation_revision_id) if run.current_translation_revision_id else None
    if revision is None:
        return None
    previews=list(session.scalars(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id==run.id,
        rm.PreviewArtifact.translation_revision_id==run.current_translation_revision_id,rm.PreviewArtifact.status=="valid")))
    expected={(str(x["document_id"]),int(x["document_version"])) for x in run.task_spec["source_versions"]}
    current={(str(x.document_id),x.document_version) for x in previews}
    if not revision or revision.business_revision_id!=batch.business_revision_id or current!=expected or any(p.business_revision_id!=batch.business_revision_id for p in previews):
        _schedule_binding_qa(session,case,run,batch);return None
    return bind_comparison(session,run,case,batch)


def try_rebind_compatible(session,case,batch):
    from app.workflows.models import RunEvent,RunStatus
    rebound=[]
    if not batch_current(session,case,batch):return rebound
    for run in session.scalars(select(Run).where(Run.case_id==case.id,Run.workflow_type=="translate_review",Run.workflow_version>=2).order_by(Run.id).with_for_update()):
        if run.status==RunStatus.CANCELLED:continue
        reason=binding_incompatibility(session,case,run,batch)
        if reason:
            run.next_event_seq+=1
            session.add(RunEvent(tenant_id=case.tenant_id,run_id=run.id,seq=run.next_event_seq,event_type="comparison.incompatible",
                status=run.status.value,stage=run.current_stage,message="新核对与冻结交付依赖不兼容，请创建新翻译任务",
                payload={"batch_id":str(batch.id),"reason":reason,"required_action":"new_run"}))
            continue
        binding=try_rebind_current_for_run(session,case,run)
        if binding:rebound.append(str(run.id))
    return rebound


async def recomparison_for_change(db,principal,settings,case,*,reparse_document_id=None):
    from app.workflows import commands
    from app.workflows.models import RunStatus,ExecutionEpoch
    scope=await db.run_sync(lambda s:ensure_scope(s,case,principal.user_id))
    payload=ScopePayload.model_validate(scope.payload)
    if payload.baseline_business_revision_id!=case.current_business_revision_id:
        payload=payload.model_copy(update={"baseline_business_revision_id":case.current_business_revision_id})
        scope=await db.run_sync(lambda s:append_scope(s,case,principal.user_id,payload))
    existing=(await db.execute(select(Run).where(Run.case_id==case.id,Run.workflow_type=="consignment_check",
        Run.status.in_([RunStatus.QUEUED,RunStatus.PROCESSING,RunStatus.AWAITING_INPUT]),
        Run.task_spec["scope_revision_id"].as_string()==str(scope.id)).order_by(Run.created_at.desc()).limit(1))).scalar_one_or_none()
    if existing:
        if reparse_document_id:raise VersionConflict("A comparison is active; finish or cancel it before re-parsing",code="RUN_BUSY")
        return existing
    sources=[{"document_id":str(m.document_id),"document_version":m.document_version} for m in payload.materials if m.purpose!="excluded"]
    draft=await commands.prepare_draft(db,principal,settings,case_id=case.id,workflow_type="consignment_check",workflow_version=1,
        source_versions=sources,draft_parameters={"scope_revision_id":str(scope.id),"previous_batch_id":str(case.current_comparison_batch_id) if case.current_comparison_batch_id else None})
    key="comparison-change:"+request_digest({"scope":scope.id,"business":case.current_business_revision_id})[:48]
    run=await commands.start_run(db,principal,settings,draft_task_id=draft.id,expected_draft_version=draft.draft_version,idempotency_key=key)
    if reparse_document_id:
        epoch=(await db.execute(select(ExecutionEpoch).where(ExecutionEpoch.run_id==run.id,ExecutionEpoch.epoch==run.current_epoch))).scalar_one()
        epoch.purpose="reparse"
        keep=[]
        for pid in epoch.input_parse_artifact_ids:
            parse=await db.get(em.ParseArtifact,UUID(pid))
            if parse and parse.document_id!=reparse_document_id:keep.append(pid)
        epoch.input_parse_artifact_ids=keep
    return run


def current_disposition(session,issue):
    return session.scalar(select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id==issue.id).order_by(rm.IssueDisposition.review_sequence.desc()).limit(1))


def dispositions_for(session,issues,*,frozen_disposition_ids=None):
    latest={}
    if not issues:return latest
    query=select(rm.IssueDisposition).where(rm.IssueDisposition.issue_id.in_([i.id for i in issues]))
    if frozen_disposition_ids is not None:
        query=query.where(rm.IssueDisposition.id.in_([UUID(str(x)) for x in frozen_disposition_ids]))
    for d in session.scalars(query.order_by(rm.IssueDisposition.review_sequence.desc())):
        latest.setdefault(d.issue_id,d)
    return latest


def batch_summary(session,batch,*,frozen_disposition_ids=None):
    issues=list(session.scalars(select(rm.Issue).where(rm.Issue.comparison_batch_id==batch.id)))
    latest=dispositions_for(session,issues,frozen_disposition_ids=frozen_disposition_ids)
    unresolved=[i for i in issues if not issue_resolved(i,latest.get(i.id))]
    return {**batch.summary,"initial_counts":{k:batch.summary.get(k,0) for k in ("red","yellow","unknown")},
        "red":sum(i.severity=="red" for i in unresolved),"yellow":sum(i.severity=="yellow" for i in unresolved),
        "unknown":sum(i.severity in {"unknown","undetermined"} for i in unresolved),"resolved":len(issues)-len(unresolved)}
