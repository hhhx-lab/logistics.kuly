"""R04–R12：版本限定的复核查询与独立人工命令。"""
from __future__ import annotations

import base64
import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser, set_tenant_context
from app.api.errors import InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.api.routers.workflows import _begin
from app.api import response_dto as d
from app.api import idempotency
from app.documents import evidence_models as em
from app.documents.models import DocumentVersion, StoredObject
from app.documents.retention import load_case_for_read
from app.infra.object_store import ObjectStore
from app.review import commands, models as rm
from app.exports.models import ExportSnapshot
from app.workflows import commands as wc

router=APIRouter(tags=["review-commands"])
_preview_render_limit=asyncio.Semaphore(2)
IdempotencyKey=Annotated[str,Header(alias="Idempotency-Key",min_length=8,max_length=80)]


class CommandBody(BaseModel):
    model_config=ConfigDict(extra="forbid")


class BusinessBody(CommandBody):
    expected_business_revision_id: UUID
    expected_business_revision_no: int | None=Field(default=None,ge=1)


class LineBasis(CommandBody):
    same_goods:bool
    complete:bool
    scope_key:str=Field(min_length=1,max_length=200)
    role:str="goods"
    packaging_level:str=Field(min_length=1,max_length=80)


class LineMatchBody(BusinessBody):
    run_id:UUID
    expected_review_state_version:int=Field(ge=1)
    left_line_id:UUID|None=None
    right_line_id:UUID|None=None
    left_line_ids:list[UUID]=Field(default_factory=list,max_length=20)
    right_line_ids:list[UUID]=Field(default_factory=list,max_length=20)
    relation:str="one_to_one"
    basis:LineBasis
    reason:str=Field(min_length=1,max_length=2000)


class FieldBody(BusinessBody):
    expected_parse_artifact_id: UUID
    value: str | None=Field(default=None,max_length=20000)
    unit: str | None=Field(default=None,max_length=16)
    reason: str | None=Field(default=None,max_length=2000)
    basis: str | None=Field(default=None,max_length=10000)
    required_materials: str | None=Field(default=None,max_length=2000)


class IssueBody(BusinessBody):
    expected_issue_version: int=Field(ge=1)
    reason: str=Field(default="",max_length=2000)
    basis: str | None=Field(default=None,max_length=10000)
    value: str | None=Field(default=None,max_length=20000)


class SectionBody(BusinessBody):
    expected_parse_artifact_id: UUID
    doc_type: str | None=None
    cargo_profile_ids: list[str] | None = Field(default=None, max_length=16)
    source_language: str | None=None
    sections: dict[str,str] | None=None
    section_id: UUID | None=None
    reason: str=Field(min_length=1,max_length=2000)


class SegmentBody(CommandBody):
    expected_translation_revision_id: UUID
    target_language: str
    translated_text: str=Field(min_length=1,max_length=50000)
    reason: str=Field(min_length=1,max_length=2000)


class ReparseBody(CommandBody):
    run_id: UUID
    expected_parse_artifact_id: UUID
    reason: str=Field(min_length=1,max_length=2000)


class ImpactBody(CommandBody):
    edit_kind: str
    target: dict
    proposed_value: str | None=Field(default=None,max_length=50000)


def _page(rows,cursor,limit,version,filters):
    offset=0
    if cursor:
        try:
            previous=json.loads(base64.urlsafe_b64decode(cursor))
            if previous["v"]!=version or previous["f"]!=filters:raise VersionConflict("复核版本或筛选已变化，请从第一页读取",code="PAGE_STALE")
            offset=int(previous["o"])
            if offset<0:raise ValueError
        except (ValueError,KeyError,TypeError) as exc:raise InvalidRequest("分页游标无效") from exc
    page=rows[offset:offset+limit]
    next_cursor=base64.urlsafe_b64encode(json.dumps({"v":version,"f":filters,"o":offset+len(page)}).encode()).decode() if len(rows)>offset+len(page) else None
    return page,next_cursor


async def _version_case(db,principal,settings,case_id,business_revision_id,review_state_version,snapshot_id):
    case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
    if snapshot_id:
        raise NotFoundOrForbidden("成功快照版本须从已登记的快照读取")
    if business_revision_id and business_revision_id!=case.current_business_revision_id:
        raise VersionConflict("业务版已变化，请重新加载",code="PAGE_STALE",current={"business_revision_id":str(case.current_business_revision_id) if case.current_business_revision_id else None})
    if review_state_version and review_state_version!=case.review_state_version:
        raise VersionConflict("复核状态已变化，请重新加载",code="PAGE_STALE",current={"review_state_version":case.review_state_version})
    return case


@router.get("/cases/{case_id}/line-items", response_model=d.PageView[d.LineItemView], response_model_exclude_unset=True)
async def line_items(case_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,
    business_revision_id:UUID|None=None,review_state_version:int|None=None,logical_document_id:UUID|None=None,run_id:UUID|None=None,
    snapshot_id:UUID|None=None,parse_artifact_id:UUID|None=None,cursor:str|None=None,limit:Annotated[int,Query(ge=1,le=200)]=50):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        snapshot=await db.get(ExportSnapshot,snapshot_id) if snapshot_id else None
        if snapshot_id and (snapshot is None or snapshot.case_id!=case.id):raise NotFoundOrForbidden()
        fixed_parse=await db.get(em.ParseArtifact,parse_artifact_id) if parse_artifact_id else None
        if parse_artifact_id and (fixed_parse is None or fixed_parse.case_id!=case.id):raise NotFoundOrForbidden()
        if not snapshot and not fixed_parse:await _version_case(db,principal,settings,case_id,business_revision_id,review_state_version,None)
        parses=list((await db.execute(select(em.ParseArtifact).where(em.ParseArtifact.case_id==case.id,
            em.ParseArtifact.quality!=em.ParseQuality.FAILED).order_by(em.ParseArtifact.created_at.desc()))).scalars())
        latest={}
        for parse in parses:latest.setdefault((parse.document_id,parse.document_version),parse.id)
        if snapshot:latest={s["document_id"]:UUID(s["parse_artifact_id"]) for s in snapshot.manifest["source_versions"]}
        elif fixed_parse:latest={str(fixed_parse.document_id):fixed_parse.id}
        elif run_id:
            run,_=await wc.load_run(db,principal,settings,run_id)
            if run.case_id!=case.id:raise NotFoundOrForbidden()
            from app.management.versions import run_parse_ids
            ids=await db.run_sync(lambda sync:run_parse_ids(sync,run))
            latest={str(value):value for value in ids}
        query=select(em.LineItem).where(em.LineItem.case_id==case.id,em.LineItem.parse_artifact_id.in_(list(latest.values()))).order_by(em.LineItem.logical_document_id,em.LineItem.line_no,em.LineItem.id)
        if logical_document_id:query=query.where(em.LineItem.logical_document_id==logical_document_id)
        rows=list((await db.execute(query)).scalars())
        # 使用已核验物理来源的冻结投影，避免多次抽取的同一行撑大复核列表。
        # 历史快照只读其自身清单；没有可配对批次时不猜测哪些记录重复。
        line_manifest = None
        if snapshot:
            line_manifest = (snapshot.manifest.get("comparison") or {}).get("manifest")
        else:
            from app.consignment.models import ComparisonBatch
            batch_query = select(ComparisonBatch).where(ComparisonBatch.case_id == case.id,
                ComparisonBatch.status == "completed")
            if run_id:
                batch_query = batch_query.where(ComparisonBatch.run_id == run_id)
            elif case.current_comparison_batch_id:
                batch_query = batch_query.where(ComparisonBatch.id == case.current_comparison_batch_id)
            else:
                batch_query = batch_query.where(ComparisonBatch.id.is_(None))
            batch = (await db.execute(batch_query.order_by(ComparisonBatch.created_at.desc()).limit(1))).scalar_one_or_none()
            if batch:
                line_manifest = batch.manifest
        if line_manifest is not None and "line_ids" in line_manifest and "raw_line_ids" in line_manifest:
            frozen_parses = {entry["id"] for entry in line_manifest.get("parses", [])}
            raw_ids = set(line_manifest["raw_line_ids"])
            if set(map(str, latest.values())).issubset(frozen_parses) and all(str(row.id) in raw_ids for row in rows):
                visible_ids = set(line_manifest["line_ids"])
                rows = [row for row in rows if str(row.id) in visible_ids]
        frozen_version=str(snapshot.id) if snapshot else str(fixed_parse.id) if fixed_parse else case.review_state_version
        page,next_cursor=_page(rows,cursor,limit,frozen_version,[str(logical_document_id),str(run_id)])
        items=[]
        for line in page:
            parse=await db.get(em.ParseArtifact,line.parse_artifact_id)
            if snapshot:
                matches=[m for m in snapshot.manifest.get("line_matches",[]) if str(line.id) in {m["left"],m["right"]}]
            else:
                matches=[{"left":str(m.left_line_id),"right":str(m.right_line_id),"state":m.state,"relation":m.relation,"basis":m.basis,"by":str(m.decided_by) if m.decided_by else None,"at":m.created_at.isoformat()}
                    for m in (await db.execute(select(em.LineMatchEdge).where(
                        ((em.LineMatchEdge.left_line_id==line.id)|(em.LineMatchEdge.right_line_id==line.id)),em.LineMatchEdge.state!="rejected"))).scalars()]
            for match in matches:
                for side in ("left", "right"):
                    counterpart = await db.get(em.LineItem, UUID(match[side]))
                    if counterpart is None or counterpart.case_id != case.id: continue
                    counterpart_parse = await db.get(em.ParseArtifact, counterpart.parse_artifact_id)
                    if counterpart_parse is None or counterpart_parse.id not in latest.values(): continue
                    version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == counterpart_parse.document_id,
                        DocumentVersion.version == counterpart_parse.document_version))).scalar_one_or_none()
                    match[side + "_detail"] = {"line_id": str(counterpart.id), "label": f"{version.filename if version else '原件'} · 第{counterpart.line_no}行 · {counterpart.description or '未提取货描'}",
                        "target": {"case_id":str(case.id),"document_id":str(counterpart_parse.document_id),"document_version":counterpart_parse.document_version,
                            "parse_artifact_id":str(counterpart_parse.id),"logical_document_id":str(counterpart.logical_document_id),"line_item_id":str(counterpart.id),
                            "source_segment_id":counterpart.source_segment_ids[0] if counterpart.source_segment_ids else None,"export_snapshot_id":str(snapshot.id) if snapshot else None}}
            items.append({"line_id":str(line.id),"line_no":line.line_no,"level":line.package_unit or "包装层级未确认","description":line.description or "","hs_code":line.hs_code,
                "quantity":str(line.quantity) if line.quantity is not None else "","unit":line.quantity_unit or "",
                "amount":str(line.amount) if line.amount is not None else None,
                "unit_price":str(line.unit_price) if line.unit_price is not None else None,"currency":line.currency,
                "package_count":str(line.package_count) if line.package_count is not None else None,"package_unit":line.package_unit,
                "net_weight":str(line.net_weight) if line.net_weight is not None else None,
                "gross_weight":str(line.gross_weight) if line.gross_weight is not None else None,"weight_unit":line.weight_unit,
                "matches":matches,
                "match_status":"matched" if any(m["state"]=="confirmed" for m in matches) else "partial" if matches else "unmatched",
                "matched_with":"、".join(m["right"] if m["left"]==str(line.id) else m["left"] for m in matches) or None,
                "target":{"case_id":str(case.id),"document_id":str(parse.document_id),"document_version":parse.document_version,
                    "parse_artifact_id":str(parse.id),"logical_document_id":str(line.logical_document_id),"line_item_id":str(line.id),
                    "source_segment_id":line.source_segment_ids[0] if line.source_segment_ids else None,
                    "business_revision_id":snapshot.manifest["business_revision_id"] if snapshot else str(business_revision_id or case.current_business_revision_id) if business_revision_id or case.current_business_revision_id else None,
                    "export_snapshot_id":str(snapshot.id) if snapshot else None}})
        return {"items":items,"next_cursor":next_cursor,"review_state_version":case.review_state_version}


async def _check_sources(db, check, snapshot):
    segments = list((await db.execute(select(em.SourceSegment).where(em.SourceSegment.id.in_(
        [UUID(value) for value in check.source_segment_ids])))).scalars())
    sources = []
    for segment in segments:
        if segment.case_id != check.case_id: continue
        parse = await db.get(em.ParseArtifact, segment.parse_artifact_id)
        if parse is None or parse.case_id != check.case_id: continue
        if snapshot and not any(source.get("parse_artifact_id") == str(parse.id) for source in snapshot.manifest["source_versions"]): continue
        sources.append({"label": "规则输入原文", "target": {"case_id":str(check.case_id),"run_id":str(check.run_id),
            "document_id":str(parse.document_id),"document_version":parse.document_version,
            "parse_artifact_id":str(parse.id),"logical_document_id":str(segment.logical_document_id),
            "section_id":str(segment.section_id),"source_segment_id":str(segment.id),
            "business_revision_id":str(check.business_revision_id),"export_snapshot_id":str(snapshot.id) if snapshot else None}})
    return sources


@router.get("/runs/{run_id}/checks", response_model=d.ChecksPageView, response_model_exclude_unset=True)
async def checks(run_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,business_revision_id:UUID|None=None,
    review_state_version:int|None=None,snapshot_id:UUID|None=None,parse_artifact_id:UUID|None=None,cursor:str|None=None,limit:Annotated[int,Query(ge=1,le=200)]=50):
    async with db.begin():
        await set_tenant_context(db,principal)
        run,case=await wc.load_run(db,principal,settings,run_id)
        snapshot=await db.get(ExportSnapshot,snapshot_id) if snapshot_id else None
        if snapshot_id and (snapshot is None or snapshot.run_id!=run.id):raise NotFoundOrForbidden()
        if not snapshot and not parse_artifact_id:await _version_case(db,principal,settings,case.id,business_revision_id,review_state_version,None)
        if parse_artifact_id:
            parse=await db.get(em.ParseArtifact,parse_artifact_id)
            if parse is None or parse.case_id!=case.id:raise NotFoundOrForbidden()
        query=select(rm.RuleCheck).where(rm.RuleCheck.run_id==run.id).order_by(rm.RuleCheck.rule_id,rm.RuleCheck.id)
        query=query.where(rm.RuleCheck.id.in_([UUID(x) for x in snapshot.manifest["rule_check_ids"]])) if snapshot else query.where(rm.RuleCheck.business_revision_id==business_revision_id if business_revision_id else rm.RuleCheck.business_revision_id==run.current_business_revision_id)
        from app.consignment.service import review_checks
        rows = await db.run_sync(lambda sync: review_checks(sync, run, case)) if not snapshot else list((await db.execute(query)).scalars())
        if parse_artifact_id:
            source_ids={str(x) for x in (await db.execute(select(em.SourceSegment.id).where(em.SourceSegment.parse_artifact_id==parse_artifact_id))).scalars()}
            field_ids={str(x) for x in (await db.execute(select(em.FieldAssertion.id).where(em.FieldAssertion.parse_artifact_id==parse_artifact_id))).scalars()}
            rows=[check for check in rows if source_ids.intersection(check.source_segment_ids) or field_ids.intersection(check.input_field_ids)]
        version=str(snapshot.id) if snapshot else str(parse_artifact_id) if parse_artifact_id else case.review_state_version
        selected_business=snapshot.manifest["business_revision_id"] if snapshot else str(business_revision_id or run.current_business_revision_id)
        page,next_cursor=_page(rows,cursor,limit,version,selected_business)
        return {"items":[{"check_id":str(check.id),"rule_name":check.rule_id,"rule_version":str(check.rule_version),
            "status":check.status,"executed_at":check.created_at.isoformat(),"rule_pack_version":check.rule_pack_version,"condition":check.explanation,"inputs":"、".join(check.input_field_ids),
            "basis":json.dumps(check.calculation,ensure_ascii=False),"source_segment_ids":check.source_segment_ids,"sources":await _check_sources(db,check,snapshot)} for check in page],
            "next_cursor":next_cursor,"review_state_version":case.review_state_version,"rules_status":"run" if rows else "not_run"}


@router.get("/runs/{run_id}/translations", response_model=d.TranslationsPageView, response_model_exclude_unset=True)
async def translations(run_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,
    business_revision_id:UUID|None=None,translation_revision_id:UUID|None=None,compatibility_id:UUID|None=None,
    document_id:UUID|None=None,section_id:UUID|None=None,snapshot_id:UUID|None=None,
    parse_artifact_id:UUID|None=None,source_segment_id:UUID|None=None,source_anchor_id:UUID|None=None,
    cursor:str|None=None,limit:Annotated[int,Query(ge=1,le=200)]=50):
    async with db.begin():
        await set_tenant_context(db,principal)
        run,case=await wc.load_run(db,principal,settings,run_id)
        snapshot=await db.get(ExportSnapshot,snapshot_id) if snapshot_id else None
        if snapshot_id and (snapshot is None or snapshot.run_id!=run.id):raise NotFoundOrForbidden()
        if run.workflow_type in {"read_index", "consignment_check"}:return {"items":[],"next_cursor":None,"not_applicable":True,"reason":"本任务不生成译文"}
        if snapshot:
            business_revision_id=UUID(snapshot.manifest["business_revision_id"])
            translation_revision_id=UUID(snapshot.manifest["translation_revision_id"])
        if not business_revision_id or not translation_revision_id:raise InvalidRequest("当前译文查询必须指定业务版和译文版")
        revision=await db.get(rm.TranslationRevision,translation_revision_id)
        if revision is None or revision.run_id!=run.id:raise NotFoundOrForbidden()
        if not snapshot and run.current_translation_revision_id!=revision.id:raise VersionConflict("当前译文版已变化",code="PAGE_STALE")
        if parse_artifact_id:
            selected_parse=await db.get(em.ParseArtifact,parse_artifact_id)
            if selected_parse is None or selected_parse.case_id!=case.id or str(selected_parse.id) not in revision.parse_artifact_ids:raise NotFoundOrForbidden()
            if document_id and selected_parse.document_id!=document_id:raise NotFoundOrForbidden()
        selected_source=await db.get(em.SourceSegment,source_segment_id) if source_segment_id else None
        selected_anchor=await db.get(em.SourceAnchor,source_anchor_id) if source_anchor_id else None
        if source_anchor_id:
            if selected_anchor is None or selected_anchor.case_id!=case.id or source_segment_id and selected_anchor.source_segment_id!=source_segment_id:raise NotFoundOrForbidden()
            selected_source=await db.get(em.SourceSegment,selected_anchor.source_segment_id)
        if source_segment_id or source_anchor_id:
            if selected_source is None or selected_source.case_id!=case.id or str(selected_source.parse_artifact_id) not in revision.parse_artifact_ids:raise NotFoundOrForbidden()
            source_parse=await db.get(em.ParseArtifact,selected_source.parse_artifact_id)
            if (source_parse is None or parse_artifact_id and source_parse.id!=parse_artifact_id or document_id and source_parse.document_id!=document_id
                or section_id and selected_source.section_id!=section_id or selected_anchor and selected_anchor.parse_artifact_id!=source_parse.id):raise NotFoundOrForbidden()
        if document_id:
            member=(await db.execute(select(wc.RunDocument).where(wc.RunDocument.run_id==run.id,wc.RunDocument.document_id==document_id))).scalar_one_or_none()
            if member is None:raise NotFoundOrForbidden()
        if section_id:
            section=await db.get(em.DocumentSection,section_id)
            parse=await db.get(em.ParseArtifact,section.parse_artifact_id) if section else None
            if section is None or str(section.parse_artifact_id) not in revision.parse_artifact_ids or document_id and parse.document_id!=document_id:raise NotFoundOrForbidden()
        if revision.business_revision_id!=business_revision_id:
            compatible=await db.get(rm.TranslationCompatibility,compatibility_id) if compatibility_id else None
            if compatible is None or compatible.run_id!=run.id or compatible.business_revision_id!=business_revision_id or compatible.translation_revision_id!=revision.id:
                raise VersionConflict("业务版与译文版没有有效兼容配对",code="PAIRING_STALE")
        query=select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(x) for x in revision.segment_ids]))
        if parse_artifact_id:query=query.where(rm.TranslationSegment.parse_artifact_id==parse_artifact_id)
        if selected_source:query=query.where(rm.TranslationSegment.source_segment_ids.contains([str(selected_source.id)]))
        rows=list((await db.execute(query)).scalars())
        by_id={str(x.id):x for x in rows};rows=[by_id[x] for x in revision.segment_ids if x in by_id]
        if document_id:rows=[x for x in rows if x.document_id==document_id]
        if section_id:rows=[x for x in rows if x.section_id==section_id]
        page,next_cursor=_page(rows,cursor,limit,str(revision.id),[str(document_id),str(section_id),str(business_revision_id),str(compatibility_id),str(snapshot_id),str(parse_artifact_id),str(source_segment_id),str(source_anchor_id)])
        items=[await commands.segment_view(db,x,revision) for x in page]
        for item in items:
            if selected_source:
                item['source_segment_id']=str(selected_source.id)
                item['target']['source_segment_id']=str(selected_source.id)
                item['target']['source_anchor_id']=str(selected_anchor.id) if selected_anchor else None
                if item['target']['section_id']!=str(selected_source.section_id):item['target']['section_id']=None
            if snapshot:item['target']['export_snapshot_id']=str(snapshot.id)
        return {"items":items,"next_cursor":next_cursor,"not_applicable":False}


async def _preview(db,principal,settings,preview_id):
    preview=await db.get(rm.PreviewArtifact,preview_id)
    if preview is None:raise NotFoundOrForbidden()
    await load_case_for_read(db,preview.case_id,user_id=principal.user_id,settings=settings)
    return preview


@router.get("/previews/{preview_id}/manifest", response_model=d.PreviewManifestView, response_model_exclude_unset=True)
async def preview_manifest(preview_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        preview=await _preview(db,principal,settings,preview_id)
        run=await db.get(wc.Run,preview.run_id)
        version=(await db.execute(select(DocumentVersion).where(DocumentVersion.document_id==preview.document_id,DocumentVersion.version==preview.document_version))).scalar_one()
        scope=await db.get(rm.ResolvedTranslationScope,preview.resolved_scope_id)
        sections=list((await db.execute(select(em.DocumentSection).where(em.DocumentSection.parse_artifact_id==preview.parse_artifact_id).order_by(em.DocumentSection.ordinal))).scalars())
        def ref(section):return {"case_id":str(preview.case_id),"run_id":str(preview.run_id),"document_id":str(preview.document_id),"document_version":preview.document_version,"parse_artifact_id":str(preview.parse_artifact_id),"logical_document_id":str(section.logical_document_id),"section_id":str(section.id)}
        obj=await db.get(StoredObject,preview.draft_pdf_object_id) if preview.draft_pdf_object_id else None
        pdf_available=bool(preview.reading_html)
        return {"preview_id":str(preview.id),"run_id":str(run.id),"source_version":{"document_id":str(version.document_id),"document_version":version.version,"case_id":str(version.case_id),"filename":version.filename,"mime":version.mime,"size_bytes":version.size_bytes,"uploaded_by":str(version.uploaded_by),"uploaded_at":version.created_at,"parse_artifact_id":str(preview.parse_artifact_id)},
            "preview_version":preview.preview_version,"translation_scope":run.task_spec["translation_scope"],"status":preview.status,
            "business_revision_id":str(preview.business_revision_id),"translation_revision_id":str(preview.translation_revision_id),"compatibility_id":str(preview.compatibility_id) if preview.compatibility_id else None,
            "target_language":preview.target_language,"resolved_scope_id":str(preview.resolved_scope_id),"render_config_version":preview.render_config_version,
            "selected_section_refs":[ref(s) for s in sections if scope and str(s.id) in scope.source_section_ids],
            "untranslated_section_refs":[ref(s) for s in sections if not scope or str(s.id) not in scope.source_section_ids],"segment_mappings":preview.segment_mappings,
            "representations":[{"kind":"reading_html","mime":"text/html","size_bytes":len(preview.reading_html.encode()),"available":bool(preview.reading_html)},
                {"kind":"draft_pdf","mime":"application/pdf","available":pdf_available,"size_bytes":None,"disabled_reason":None if pdf_available else preview.draft_pdf_disabled_reason or "本预览仅提供结构阅读视图"}],
            "allowed_actions":["view"]+(["download_draft"] if pdf_available and preview.status=="valid" else [])}


async def _layout_html(db, preview, settings, mode, pdf=False):
    import asyncio
    from app.review.layout import document_layout, render_html
    parse = await db.get(em.ParseArtifact, preview.parse_artifact_id)
    version = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == preview.document_id, DocumentVersion.version == preview.document_version))).scalar_one()
    raw = ocr_raw = None
    if not parse.layout_manifest:
        store = ObjectStore(settings)
        obj = await db.get(StoredObject, version.object_id)
        raw = await asyncio.to_thread(store.read_all, obj.object_key)
        if parse.raw_response_object_id:
            ocr = await db.get(StoredObject, parse.raw_response_object_id)
            if ocr and ocr.is_visible and ocr.mime == "application/zip":
                ocr_raw = await asyncio.to_thread(store.read_all, ocr.object_key)
    revision = await db.get(rm.TranslationRevision, preview.translation_revision_id)
    segments = (await db.execute(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(i) for i in revision.segment_ids]), rm.TranslationSegment.document_id == preview.document_id).order_by(rm.TranslationSegment.ordinal))).scalars().all()
    layout, sources = await db.run_sync(lambda sync: document_layout(sync, parse, raw, version.mime, ocr_raw))
    source_text={str(s.id):s.text for s in sources}
    if pdf:
        from app.exports import render
        def make():
            docx,expected=render.build_layout_translation(layout,source_text,segments,target_language=preview.target_language,draft=True)
            result=render.to_pdf(docx)
            render.verify(result,"pdf",expected,allow_draft=True)
            return result
        if _preview_render_limit.locked():raise InvalidRequest("预览正在生成，请稍后重试")
        async with _preview_render_limit:
            return await asyncio.to_thread(make)
    return render_html(layout, source_text, segments, mode)


@router.get("/previews/{preview_id}/content")
async def preview_content(preview_id:UUID,principal:CurrentUser,db:DbSession,settings:SettingsDep,request:Request,
    representation:str="reading_html",view_mode:str="translation",range_header:Annotated[str|None,Header(alias="Range")]=None):
    content=None;key=None
    async with db.begin():
        await set_tenant_context(db,principal)
        preview=await _preview(db,principal,settings,preview_id)
        if representation=="reading_html":
            from app.workflows.translation import sanitize_html
            if view_mode not in {"source", "translation", "bilingual"}:
                raise InvalidRequest("阅读模式无效")
            content=sanitize_html(await _layout_html(db, preview, settings, view_mode)).encode()
            size=len(content)
            mime="text/html; charset=utf-8"
        elif representation=="draft_pdf":
            content=await _layout_html(db,preview,settings,"translation",pdf=True)
            size=len(content);mime="application/pdf"
        else:raise NotFoundOrForbidden("该草稿呈现不可用")
    headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff",
        "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; sandbox",
        "Accept-Ranges":"bytes"}
    from app.api.routers.uploads import _parse_range
    try:bounds=_parse_range(range_header,size)
    except ValueError:return Response(status_code=416,headers={**headers,"Content-Range":f"bytes */{size}"})
    start,end=bounds if bounds else (0,size-1)
    from fastapi.responses import StreamingResponse
    from app.infra.content_stream import prepared_bytes
    stream=await prepared_bytes(request,principal,settings,preview.case_id,data=content,
        store=ObjectStore(settings) if key else None,key=key,start=start,end=end,close_store=True)
    return StreamingResponse(stream,
        status_code=206 if bounds else 200,media_type=mime,headers={**headers,"Content-Length":str(end-start+1),
            **({"Content-Range":f"bytes {start}-{end}/{size}"} if bounds else {})})


@router.post("/cases/{case_id}/change-impact/preview", response_model=d.ChangeImpactView, response_model_exclude_unset=True)
async def impact(case_id:UUID,body:ImpactBody,principal:CurrentUser,db:DbSession,settings:SettingsDep):
    async with db.begin():
        await set_tenant_context(db,principal)
        case=await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
        if body.target.get("case_id") and body.target["case_id"]!=str(case.id):raise NotFoundOrForbidden()
        return await commands.change_impact(db,case,body.target)


async def _command(scope,key,body,principal,db,settings,callback):
    async with db.begin():
        await set_tenant_context(db,principal)
        outcome=await _begin(db,principal,scope=scope,key=key,payload=body.model_dump(mode="json"))
        if outcome.replayed:
            # 重放仍重新鉴权整票，并遵循删除与到期门禁。
            case_id=UUID(outcome.record.result_ref["case_id"])
            await load_case_for_read(db,case_id,user_id=principal.user_id,settings=settings)
            return outcome.record.result_ref["result"]
        result=await callback()
        from fastapi.encoders import jsonable_encoder
        case_id=result.pop("_case_id")
        idempotency.complete(outcome.record,{"case_id":str(case_id),"result":jsonable_encoder(result)})
        return result


@router.post("/cases/{case_id}/line-matches", response_model=d.LineMatchCommandView, response_model_exclude_unset=True)
async def line_match(case_id:UUID,body:LineMatchBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async def apply():
        result=await commands.confirm_line_match(db,principal,settings,case_id=case_id,body=body)
        result["_case_id"]=case_id;return result
    return await _command(f"POST /cases/{case_id}/line-matches",idempotency_key,body,principal,db,settings,apply)


def _field_endpoint(action):
    async def endpoint(case_id:UUID,field_id:UUID,body:FieldBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
        async def apply():
            result=await commands.field_action(db,principal,settings,case_id=case_id,field_id=field_id,action=action,body=body)
            result["_case_id"]=case_id;return result
        return await _command(f"POST /cases/{case_id}/fields/{field_id}/{action}",idempotency_key,body,principal,db,settings,apply)
    return endpoint


for action in ("confirm","mark-unknown","revise"):
    router.add_api_route(f"/cases/{{case_id}}/fields/{{field_id}}/{action}",_field_endpoint(action),methods=["POST"],name=f"field_{action}", response_model=d.FieldCommandView, response_model_exclude_unset=True)


def _issue_endpoint(action):
    async def endpoint(case_id:UUID,issue_id:UUID,body:IssueBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
        async def apply():
            result=await commands.issue_action(db,principal,settings,case_id=case_id,issue_id=issue_id,action=action,body=body)
            result["_case_id"]=case_id;return result
        return await _command(f"POST /cases/{case_id}/issues/{issue_id}/{action}",idempotency_key,body,principal,db,settings,apply)
    return endpoint


for action in ("accept","adjudicate","mark-unresolved","not-applicable","ignore"):
    router.add_api_route(f"/cases/{{case_id}}/issues/{{issue_id}}/{action}",_issue_endpoint(action),methods=["POST"],name=f"issue_{action}", response_model=d.IssueView, response_model_exclude_unset=True)


class IssueRepairBody(BusinessBody):
    expected_issue_version: int=Field(ge=1)
    expected_translation_revision_id: UUID | None = None
    instruction: str=Field(default="",max_length=4000)


def _repair_endpoint(apply):
    async def endpoint(case_id:UUID,issue_id:UUID,body:IssueRepairBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
        from app.review.repair import prepare_repair
        async def work():
            result=await prepare_repair(db,principal,settings,case_id,issue_id,body,apply=apply)
            result["_case_id"]=case_id;return result
        name="apply-recommendation" if apply else "request-revision"
        return await _command(f"POST /cases/{case_id}/issues/{issue_id}/{name}",idempotency_key,body,principal,db,settings,work)
    return endpoint

for name,apply in [("apply-recommendation",True),("request-revision",False)]:
    router.add_api_route(f"/cases/{{case_id}}/issues/{{issue_id}}/{name}",_repair_endpoint(apply),methods=["POST"],status_code=202,response_model=d.IssueRepairView,response_model_exclude_unset=True,name="issue_"+name)


def _section_endpoint(action):
    async def endpoint(case_id:UUID,section_id:UUID,body:SectionBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
        async def apply():
            result=await commands.section_action(db,principal,settings,case_id=case_id,section_id=section_id,action=action,body=body)
            result["_case_id"]=case_id;return result
        return await _command(f"POST /cases/{case_id}/sections/{section_id}/{action}",idempotency_key,body,principal,db,settings,apply)
    return endpoint


for action in ("assign","confirm-language"):
    router.add_api_route(f"/cases/{{case_id}}/sections/{{section_id}}/{action}",_section_endpoint(action),methods=["POST"],name=f"section_{action}", response_model=d.FieldCommandView, response_model_exclude_unset=True)


@router.post("/runs/{run_id}/translation-segments/{segment_id}/revise", response_model=d.TranslationSegmentView, response_model_exclude_unset=True)
async def revise_segment(run_id:UUID,segment_id:UUID,body:SegmentBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async def apply():
        result=await commands.revise_segment(db,principal,settings,run_id=run_id,segment_id=segment_id,body=body)
        result["_case_id"]=result["target"]["case_id"];return result
    return await _command(f"POST /runs/{run_id}/translation-segments/{segment_id}/revise",idempotency_key,body,principal,db,settings,apply)


@router.post("/documents/{document_id}/versions/{version}/reparse", response_model=d.RunAcceptedView, response_model_exclude_unset=True)
async def reparse(document_id:UUID,version:int,body:ReparseBody,principal:WriteUser,db:DbSession,settings:SettingsDep,idempotency_key:IdempotencyKey):
    async def apply():
        result=await commands.reparse(db,principal,settings,document_id=document_id,version=version,body=body)
        run=await db.get(wc.Run,body.run_id);result["_case_id"]=run.case_id;return result
    return await _command(f"POST /documents/{document_id}/versions/{version}/reparse",idempotency_key,body,principal,db,settings,apply)


from app.review.rule_inputs import RuleInputBody


@router.get("/cases/{case_id}/rule-inputs", response_model=d.RuleInputsView, response_model_exclude_unset=True)
async def rule_inputs(case_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep, business_revision_id: UUID | None = None, snapshot_id: UUID | None = None):
    from app.review.rule_inputs import list_rule_inputs
    async with db.begin():
        await set_tenant_context(db, principal)
        return await list_rule_inputs(db, principal, settings, case_id, business_revision_id, snapshot_id)


@router.post("/cases/{case_id}/rule-inputs", response_model=d.RuleInputAcceptedView, response_model_exclude_unset=True)
async def confirm_rule_inputs(case_id: UUID, body: RuleInputBody, principal: WriteUser, db: DbSession,
    settings: SettingsDep, idempotency_key: IdempotencyKey):
    from app.review.rule_inputs import confirm_rule_input
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await _begin(db, principal, scope=f"POST /cases/{case_id}/rule-inputs", key=idempotency_key, payload=body.model_dump(mode="json"))
        if outcome.replayed:
            from app.documents.retention import load_case_for_read
            await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
            return outcome.record.result_ref
        result = await confirm_rule_input(db, principal, settings, case_id, body)
        idempotency.complete(outcome.record, result)
        return result
