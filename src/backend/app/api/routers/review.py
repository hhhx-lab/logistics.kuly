"""
R01–R03 与 U08–U09：证据解析、轻量复核视图、字段分页、阅读视图与页图。

查询当前工作版、固定来源版本或成功快照中的解析、字段、规则、疑点、译文与预览。
只读索引未运行规则时明确返回尚未运行，不将无疑点误作检查通过。
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select,or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dto import PublishedView
from app.api.dependencies import CurrentUser, DbSession, SettingsDep, set_tenant_context
from app.api.errors import InvalidRequest, NotFoundOrForbidden, VersionConflict
from app.conversation.references import latest_valid_parse
from app.documents.evidence_models import (
    AnchorPrecision,
    DocumentSection,
    FieldAssertion,
    FieldReviewEvent,
    LogicalDocument,
    LogicalDocumentType,
    ParseArtifact,
    ParseQuality,
    SourceAnchor,
    SourceSegment,
    BusinessRevisionField,
    BusinessRevision,
    SectionAssignmentDecision,
)
from app.review import models as rm
from app.review.commands import issue_view, field_issue_links
from app.consignment.service import review_issues, review_checks
from app.exports.models import ExportSnapshot
from app.documents.models import Case, DocumentVersion, SourceDocument, StoredObject
from app.documents.retention import load_case_for_read
from app.workflows.models import Run, RunDocument, RunStatus, StepExecution, StepStatus

from app.api import response_dto as d
router = APIRouter(tags=["review"])

PAGE_LIMIT_DEFAULT = 50


# ---------- 共享 DTO ----------


class EvidenceTarget(PublishedView):
    case_id: UUID
    document_id: UUID
    document_version: int
    parse_artifact_id: UUID | None = None
    logical_document_id: UUID | None = None
    section_id: UUID | None = None
    source_segment_id: UUID | None = None
    source_anchor_id: UUID | None = None
    business_revision_id: UUID | None = None
    field_id: UUID | None = None
    issue_id: UUID | None = None
    run_id: UUID | None = None
    target_language: str | None = None
    translation_revision_id: UUID | None = None
    translation_segment_id: UUID | None = None
    preview_version: int | None = None
    export_snapshot_id: UUID | None = None
    compatibility_id: UUID | None = None
    line_item_id: UUID | None = None


class EvidenceResolution(PublishedView):
    normalized_target: EvidenceTarget
    mode: Literal["working", "historical", "source_only"]
    allowed_actions: list[str]
    disabled_reasons: dict[str, str]
    anchor_precision: Literal["rect", "page_only", "paragraph_only", "cell", "none"]
    target_status: Literal["valid", "stale_preview", "not_found"]
    page_no: int | None = None
    # 只有 rect 精度才返回矩形；其余精度绝不返回坐标，前端据此不画框
    bbox: tuple[float, float, float, float] | None = None
    coord_system: str | None = None
    notice: str | None = None


# ---------- R01 证据解析 ----------


@router.post("/evidence/resolve", response_model=EvidenceResolution, response_model_exclude_unset=True)
async def resolve_evidence(
    target: EvidenceTarget, principal: CurrentUser, db: DbSession, settings: SettingsDep
) -> EvidenceResolution:
    """
    逐层核对：Case → DocumentVersion → ParseArtifact → LogicalDocument → Section → Segment/Anchor。

    纯查询，不消费幂等键。任一层不属于上一层即视为无权，绝不回原文。
    """
    async with db.begin():
        await set_tenant_context(db, principal)
        await load_case_for_read(db, target.case_id, user_id=principal.user_id, settings=settings)

        version = (
            await db.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == target.document_id,
                    DocumentVersion.version == target.document_version,
                )
            )
        ).scalar_one_or_none()
        if version is None or version.case_id != target.case_id:
            raise NotFoundOrForbidden()

        notices: list[str] = []
        precision: str = AnchorPrecision.NONE.value
        page_no: int | None = None
        bbox: list[float] | None = None
        coord: str | None = None

        snapshot = None
        if target.export_snapshot_id is not None:
            snapshot=await db.get(ExportSnapshot,target.export_snapshot_id)
            if snapshot is None or snapshot.case_id!=target.case_id:raise NotFoundOrForbidden()
            snapshot_sources = list(snapshot.manifest["source_versions"])
            comparison = snapshot.manifest.get("comparison")
            if comparison:
                snapshot_sources += [{"document_id": p["document_id"], "document_version": p["version"], "parse_artifact_id": p["id"]}
                    for p in comparison["manifest"]["parses"]]
            source=next((s for s in snapshot_sources if s["document_id"]==str(target.document_id) and s["document_version"]==target.document_version),None)
            if source is None:raise NotFoundOrForbidden()
            if target.parse_artifact_id and source.get("parse_artifact_id")!=str(target.parse_artifact_id):raise NotFoundOrForbidden()
            if target.business_revision_id and snapshot.manifest["business_revision_id"]!=str(target.business_revision_id):raise NotFoundOrForbidden()
            if target.translation_revision_id and snapshot.manifest["translation_revision_id"]!=str(target.translation_revision_id):raise NotFoundOrForbidden()
            if target.run_id and snapshot.run_id != target.run_id:
                from app.consignment.models import ComparisonBatch
                batch = await db.get(ComparisonBatch, UUID(comparison["batch_id"])) if comparison else None
                if not batch or batch.case_id != target.case_id or target.run_id != batch.run_id or target.translation_revision_id or target.translation_segment_id:
                    raise NotFoundOrForbidden()
            if target.target_language and snapshot.manifest["target_language"] != target.target_language:raise NotFoundOrForbidden()
            allowed_fields = snapshot.manifest["field_assertion_ids"] + (comparison["manifest"]["assertion_ids"] if comparison else [])
            if target.field_id and str(target.field_id) not in allowed_fields:raise NotFoundOrForbidden()
            if target.issue_id and str(target.issue_id) not in snapshot.manifest["issue_ids"]:raise NotFoundOrForbidden()

        # 保存的疑点来源和译段映射补全定位链；不借用最新解析的位置。
        if target.issue_id:
            issue = await db.get(rm.Issue, target.issue_id)
            if issue is None or issue.case_id != target.case_id or (target.run_id and issue.run_id != target.run_id):
                raise NotFoundOrForbidden()
            if not any((target.source_segment_id, target.field_id, target.section_id, target.translation_segment_id)):
                for side in issue.sources:
                    saved = side.get("target", side)
                    if str(saved.get("document_id")) != str(target.document_id) or saved.get("document_version") != target.document_version:
                        continue
                    if target.parse_artifact_id and str(saved.get("parse_artifact_id")) != str(target.parse_artifact_id):
                        continue
                    updates = {key: saved[key] for key in ("parse_artifact_id", "logical_document_id", "section_id", "source_segment_id", "source_anchor_id") if saved.get(key)}
                    if updates:
                        target = EvidenceTarget.model_validate({**target.model_dump(), **updates})
                        break
        translation_revision = None
        if target.translation_segment_id:
            if target.translation_revision_id is None:
                raise InvalidRequest("定位译文必须指定译文版本")
            translation_revision = await db.get(rm.TranslationRevision, target.translation_revision_id)
            translated = await db.get(rm.TranslationSegment, target.translation_segment_id)
            if (translation_revision is None or translation_revision.case_id != target.case_id
                or translated is None or str(translated.id) not in translation_revision.segment_ids
                or translated.case_id != target.case_id or translated.run_id != translation_revision.run_id
                or translated.document_id != target.document_id or translated.document_version != target.document_version
                or (target.run_id and translation_revision.run_id != target.run_id)
                or (target.parse_artifact_id and translated.parse_artifact_id != target.parse_artifact_id)):
                raise NotFoundOrForbidden()
            if target.source_segment_id and str(target.source_segment_id) not in translated.source_segment_ids:
                raise NotFoundOrForbidden()
            updates = {"parse_artifact_id": translated.parse_artifact_id}
            if target.source_segment_id is None and translated.source_segment_ids:
                updates["source_segment_id"] = UUID(translated.source_segment_ids[0])
            target = target.model_copy(update=updates)
        if target.source_anchor_id and target.source_segment_id is None:
            anchor = await db.get(SourceAnchor, target.source_anchor_id)
            if anchor is None or anchor.case_id != target.case_id or anchor.parse_artifact_id != target.parse_artifact_id:
                raise NotFoundOrForbidden()
            target = target.model_copy(update={"source_segment_id": anchor.source_segment_id})
        if snapshot is not None and target.parse_artifact_id and source.get("parse_artifact_id") != str(target.parse_artifact_id):
            raise NotFoundOrForbidden()
        if target.parse_artifact_id is None and any((target.field_id, target.source_segment_id, target.section_id, target.logical_document_id)):
            raise InvalidRequest("业务来源定位必须指定识别版本")

        if target.parse_artifact_id is None:
            # 未解析原件只能打开文件本身，不虚构解析或业务版本
            mode = "source_only"
        else:
            parse = await db.get(ParseArtifact, target.parse_artifact_id)
            if (
                parse is None
                or parse.document_id != target.document_id
                or parse.document_version != target.document_version
            ):
                raise NotFoundOrForbidden()
            current = await latest_valid_parse(db, target.document_id, target.document_version)
            mode = "working" if current is not None and current.id == parse.id else "historical"
            if mode == "historical":
                notices.append("这是较早的一次识别结果，只读显示；可切换到当前识别版本。")

            if target.logical_document_id is not None:
                ld = await db.get(LogicalDocument, target.logical_document_id)
                if ld is None or ld.parse_artifact_id != parse.id:
                    raise NotFoundOrForbidden()
            if target.section_id is not None:
                section = await db.get(DocumentSection, target.section_id)
                if section is None or section.parse_artifact_id != parse.id or (
                    target.logical_document_id is not None and section.logical_document_id != target.logical_document_id
                ):
                    raise NotFoundOrForbidden()
                page_no = section.page_start
                precision = AnchorPrecision.PAGE_ONLY.value if page_no else AnchorPrecision.NONE.value

            segment_id = target.source_segment_id
            if target.field_id is not None:
                field = await db.get(FieldAssertion, target.field_id)
                if field is None or field.parse_artifact_id != parse.id:
                    raise NotFoundOrForbidden()
                if target.logical_document_id and field.logical_document_id != target.logical_document_id:raise NotFoundOrForbidden()
                if target.section_id and field.section_id != target.section_id:raise NotFoundOrForbidden()
                if target.business_revision_id:
                    member = await db.scalar(select(BusinessRevisionField.id).where(
                        BusinessRevisionField.business_revision_id == target.business_revision_id,
                        BusinessRevisionField.field_assertion_id == field.id))
                    if member is None:raise NotFoundOrForbidden()
                if segment_id is not None and str(segment_id) not in field.source_segment_ids:raise NotFoundOrForbidden()
                if segment_id is None and field.source_segment_ids:
                    segment_id = UUID(field.source_segment_ids[0])

            if segment_id is not None:
                segment = await db.get(SourceSegment, segment_id)
                if segment is None or segment.parse_artifact_id != parse.id:
                    raise NotFoundOrForbidden()
                if target.logical_document_id and segment.logical_document_id != target.logical_document_id:raise NotFoundOrForbidden()
                if target.section_id and segment.section_id != target.section_id:raise NotFoundOrForbidden()
                target = target.model_copy(update={"source_segment_id": segment.id,
                    "logical_document_id": segment.logical_document_id, "section_id": segment.section_id})
                precision = "cell" if segment.structure in {"table_cell", "cell"} else "paragraph_only"
                anchor_query = select(SourceAnchor).where(SourceAnchor.source_segment_id == segment.id).order_by(SourceAnchor.id)
                if target.source_anchor_id is not None:
                    anchor_query = anchor_query.where(SourceAnchor.id == target.source_anchor_id)
                anchor = (await db.execute(anchor_query.limit(1))).scalar_one_or_none()
                if target.source_anchor_id is not None and anchor is None:raise NotFoundOrForbidden()
                if anchor is not None:
                    from math import isfinite
                    target = target.model_copy(update={"source_anchor_id": anchor.id})
                    precision = anchor.precision.value
                    if precision == "none":
                        precision = "cell" if segment.structure in {"table_cell", "cell"} else "paragraph_only"
                    page_no = anchor.page_no if anchor.page_no and anchor.page_no > 0 and (not parse.page_count or anchor.page_no <= parse.page_count) else page_no
                    values = anchor.bbox
                    valid_rect = (isinstance(values, list) and len(values) == 4
                        and all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) for value in values))
                    valid_rect = bool(valid_rect and values[0] < values[2] and values[1] < values[3])
                    if anchor.coord_system == "page_normalized_top_left" and valid_rect:
                        valid_rect = all(0 <= value <= 1 for value in values)
                    if anchor.precision is AnchorPrecision.RECT and valid_rect and page_no and anchor.coord_system:
                        bbox, coord = list(values), anchor.coord_system
                    elif anchor.precision is AnchorPrecision.RECT:
                        precision = "cell" if segment.structure in {"table_cell", "cell"} else "paragraph_only"
                        notices.append("该区域坐标无效，已定位到对应识别段落。")
                    elif anchor.precision is not AnchorPrecision.RECT:
                        notices.append("这个位置没有可靠的坐标，只能定位到页或段落。")

        target_status = "valid"
        if target.run_id:
            run=await db.get(Run,target.run_id)
            if run is None or run.case_id!=target.case_id:raise NotFoundOrForbidden()
            member=(await db.execute(select(RunDocument).where(RunDocument.run_id==run.id,
                RunDocument.document_id==target.document_id,RunDocument.document_version==target.document_version))).scalar_one_or_none()
            if member is None:raise NotFoundOrForbidden()
        if target.issue_id:
            issue=await db.get(rm.Issue,target.issue_id)
            if issue is None or issue.case_id!=target.case_id or (target.run_id and issue.run_id!=target.run_id):raise NotFoundOrForbidden()
        if target.translation_revision_id:
            revision=translation_revision or await db.get(rm.TranslationRevision,target.translation_revision_id)
            if revision is None or revision.case_id!=target.case_id or (target.run_id and revision.run_id!=target.run_id):raise NotFoundOrForbidden()
            if target.parse_artifact_id and str(target.parse_artifact_id) not in revision.parse_artifact_ids:raise NotFoundOrForbidden()
            if target.target_language and target.target_language!=revision.target_language:raise NotFoundOrForbidden()
            if target.business_revision_id and revision.business_revision_id!=target.business_revision_id:
                compatibility=await db.get(rm.TranslationCompatibility,target.compatibility_id) if target.compatibility_id else None
                if compatibility is None or compatibility.run_id!=revision.run_id or compatibility.business_revision_id!=target.business_revision_id or compatibility.translation_revision_id!=revision.id:
                    raise VersionConflict("业务版与译文版没有有效配对",code="PAIRING_STALE")
            if target.translation_segment_id:
                segment=await db.get(rm.TranslationSegment,target.translation_segment_id)
                if segment is None or str(segment.id) not in revision.segment_ids or segment.document_id!=target.document_id or segment.document_version!=target.document_version:raise NotFoundOrForbidden()
            run=await db.get(Run,revision.run_id)
            if run.current_translation_revision_id!=revision.id:
                mode="historical"
                notices.append("这是较早的译文版本，只读显示。")
            if target.preview_version:
                preview=(await db.execute(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id==revision.run_id,
                    rm.PreviewArtifact.document_id==target.document_id,rm.PreviewArtifact.translation_revision_id==revision.id,
                    rm.PreviewArtifact.preview_version==target.preview_version).limit(1))).scalar_one_or_none()
                if preview is None or preview.status!="valid":
                    target_status="stale_preview"
                    notices.append("该译文预览已过期，来源仍可核验。")

        if target.export_snapshot_id:mode="historical"
        return EvidenceResolution(
            normalized_target=target,
            mode=mode,  # type: ignore[arg-type]
            allowed_actions=["open_source"] + (["open_reading_view"] if mode != "source_only" else []),
            disabled_reasons={} if mode != "historical" else {"revise": "历史识别版本只读"},
            anchor_precision=precision,  # type: ignore[arg-type]
            target_status=target_status,  # type: ignore[arg-type]
            page_no=page_no,
            bbox=bbox,
            coord_system=coord,
            notice="；".join(notices) or None,
        )


# ---------- R02 轻量复核视图 ----------


_DOC_TYPE_MAP = {
    LogicalDocumentType.COMMERCIAL_INVOICE: "commercial_invoice",
    LogicalDocumentType.PACKING_LIST: "packing_list",
    LogicalDocumentType.BILL_OF_LADING: "bill_of_lading",
    LogicalDocumentType.SUPPORTING: "other",
    LogicalDocumentType.UNKNOWN: "unclassified",
}
from app.documents.type_catalog import DOCUMENT_PROFILES, candidate_document_type, candidate_profile

_DOC_TYPE_LABEL = {
    "commercial_invoice": "商业发票",
    "packing_list": "装箱单",
    "bill_of_lading": "提单",
    "other": "其他佐证材料",
    "unclassified": "未确认类型",
    **{profile.code:profile.label for profile in DOCUMENT_PROFILES},
}


def _page_range(start: int | None, end: int | None) -> str:
    if not start:
        return "页码未知"
    return f"第 {start} 页" if not end or end == start else f"第 {start}–{end} 页"


def _source_language(sections,decisions):
    confirmed={d.section_id:d.confirmed_language for d in decisions if d.confirmed_language}
    languages={confirmed.get(s.id) or s.detected_language or "unknown" for s in sections}
    uncertain=not sections or any(s.language_uncertain and s.id not in confirmed for s in sections)
    detected=next(iter(languages)) if len(languages)==1 and languages<={"en","zh-CN","fr","es","ru"} else "mixed"
    return {"detected_language":detected,"language_uncertain":uncertain or detected=="mixed"}


async def _file_nodes(db: AsyncSession, case: Case, versions: list[DocumentVersion], run: Run | None = None, fixed_parse: ParseArtifact | None=None,business_revision_id:UUID|None=None) -> list[dict[str, Any]]:
    nodes = []
    translation=await db.get(rm.TranslationRevision,run.current_translation_revision_id) if run and run.current_translation_revision_id else None
    translated_segments=(await db.execute(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(x) for x in translation.segment_ids])))).scalars().all() if translation else []
    translated_source_ids={UUID(x) for segment in translated_segments for x in segment.source_segment_ids}
    current_issues = await db.run_sync(lambda sync: review_issues(sync, run, case)) if run else []
    current_views=[await issue_view(db,x) for x in current_issues]
    for dv in versions:
        from app.management.versions import run_parse
        parse = fixed_parse if fixed_parse and fixed_parse.document_id==dv.document_id and fixed_parse.document_version==dv.version else await db.run_sync(lambda sync:run_parse(sync,run,dv.document_id,dv.version)) if run else await latest_valid_parse(db,dv.document_id,dv.version)
        logical: list[dict[str, Any]] = []
        all_sections=[];all_decisions=[]
        if parse is not None:
            lds = (
                await db.execute(
                    select(LogicalDocument).where(LogicalDocument.parse_artifact_id == parse.id).order_by(LogicalDocument.ordinal)
                )
            ).scalars()
            for ld in lds:
                sections = (
                    await db.execute(
                        select(DocumentSection).where(DocumentSection.logical_document_id == ld.id).order_by(DocumentSection.ordinal)
                    )
                ).scalars()
                sections=list(sections)
                decisions=list((await db.execute(select(SectionAssignmentDecision).where(
                    SectionAssignmentDecision.section_id.in_([s.id for s in sections]),
                    SectionAssignmentDecision.business_revision_id==(business_revision_id or run.current_business_revision_id if run else business_revision_id or case.current_business_revision_id))
                    .order_by(SectionAssignmentDecision.created_at,SectionAssignmentDecision.id))).scalars())
                all_sections.extend(sections);all_decisions.extend(decisions)
                typed=[d for d in decisions if d.logical_document_type]
                chosen=typed[-1].logical_document_type if typed else candidate_document_type(ld,parse)
                doc_type={"supporting":"other","unknown":"unclassified"}.get(chosen,chosen)
                logical.append(
                    {
                        "logical_document_id": str(ld.id),
                        "doc_type": doc_type,
                        "cargo_profile_ids": typed[-1].cargo_profile_ids if typed else [p["code"] for p in candidate_profile(ld,parse).get("cargo_candidates",[])],
                        "label": ld.title or _DOC_TYPE_LABEL[doc_type],
                        "page_range": _page_range(ld.page_start, ld.page_end),
                        # 机器分类不清时要人工确认，不能默认为已分类
                        "needs_confirmation": chosen=="unknown",
                        "sections": [
                            {"section_id": str(s.id), "title": s.title or s.kind, "translated": bool((await db.execute(select(SourceSegment.id).where(SourceSegment.section_id==s.id,SourceSegment.id.in_(list(translated_source_ids))).limit(1))).scalar_one_or_none())} for s in sections
                        ],
                    }
                )
        preview=None
        if run and run.current_translation_revision_id and parse and translation and str(parse.id) in translation.parse_artifact_ids:
            preview=(await db.execute(select(rm.PreviewArtifact).where(rm.PreviewArtifact.run_id==run.id,
                rm.PreviewArtifact.document_id==dv.document_id,rm.PreviewArtifact.document_version==dv.version,
                rm.PreviewArtifact.translation_revision_id==run.current_translation_revision_id,
                rm.PreviewArtifact.status=="valid").order_by(rm.PreviewArtifact.created_at.desc()).limit(1))).scalar_one_or_none()
        nodes.append(
            {
                "source": {
                    "document_id": str(dv.document_id),
                    "document_version": dv.version,
                    "case_id": str(case.id),
                    "filename": dv.filename,
                    "mime": dv.mime,
                    "size_bytes": dv.size_bytes,
                    "uploaded_by": str(dv.uploaded_by),
                    "uploaded_at": dv.created_at.isoformat(),
                    **_source_language(all_sections,all_decisions),
                },
                "parse_artifact_id": str(parse.id) if parse else None,
                "parse_status": "parsed" if parse else "not_parsed",
                "preview_id": str(preview.id) if preview else None,
                "page_count": (parse.page_count if parse else None) or dv.page_count or 0,
                "issue_counts": _issue_counts([v for v in current_views if any(side and side["target"].get("document_id")==str(dv.document_id) for side in (v["a"],v["b"]))]),
                "logical_documents": logical,
            }
        )
    return nodes


def _issue_counts(views):
    return {"blocking":sum(v["blocking_delivery"] for v in views),
            "warning":sum(v["severity"]=="warning" and v["status"] in {"open","unresolved"} for v in views),
            "undetermined":sum(v["severity"]=="undetermined" and v["status"] in {"open","unresolved"} for v in views)}


@router.get("/cases/{case_id}/review-view", response_model=d.ReviewView, response_model_exclude_unset=True)
async def review_view(
    case_id: UUID,
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    run_id: UUID | None = None,
    snapshot_id: UUID | None = None,
    document_id: UUID | None = None,
    parse_artifact_id: UUID | None = None,
    business_revision_id: UUID | None = None,
) -> dict[str, Any]:
    """
    只返回轻量抬头、文件树、版本与计数；不打包整份原件、OCR JSON 或全部译段。
    仅当前工作版带 `run_event_cursor` 与 `review_state_version`。
    """
    async with db.begin():
        await set_tenant_context(db, principal)
        case = await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
        if snapshot_id is not None:
            snapshot=await db.get(ExportSnapshot,snapshot_id)
            if snapshot is None or snapshot.case_id!=case.id:raise NotFoundOrForbidden()
            return await _snapshot_review_view(db,case,snapshot,document_id)

        run = None
        if run_id is not None:
            run = await db.get(Run, run_id)
            if run is None or run.case_id != case.id:
                raise NotFoundOrForbidden()
            refs = (
                await db.execute(select(RunDocument).where(RunDocument.run_id == run.id).order_by(RunDocument.ordinal))
            ).scalars()
            versions = []
            for ref in refs:
                dv = (
                    await db.execute(
                        select(DocumentVersion).where(
                            DocumentVersion.document_id == ref.document_id, DocumentVersion.version == ref.document_version
                        )
                    )
                ).scalar_one()
                versions.append(dv)
        else:
            # 没有 Run 时列该票每份原件的当前版本
            docs = (await db.execute(select(SourceDocument).where(SourceDocument.case_id == case.id))).scalars()
            versions = []
            for doc in docs:
                dv = (
                    await db.execute(
                        select(DocumentVersion).where(
                            DocumentVersion.document_id == doc.id, DocumentVersion.version == doc.current_version
                        )
                    )
                ).scalar_one_or_none()
                if dv is not None:
                    versions.append(dv)

        if document_id is not None:
            versions = [v for v in versions if v.document_id == document_id]
            if not versions:raise NotFoundOrForbidden()
        fixed_parse=None
        if parse_artifact_id:
            fixed_parse=await db.get(ParseArtifact,parse_artifact_id)
            if fixed_parse is None or fixed_parse.case_id!=case.id or not any(v.document_id==fixed_parse.document_id and v.version==fixed_parse.document_version for v in versions):raise NotFoundOrForbidden()

        files = await _file_nodes(db, case, versions, run, fixed_parse,business_revision_id)
        any_parsed = any(f["parse_status"] == "parsed" for f in files)
        mode = "working" if any_parsed else "source_only"

        gate = "识别结果尚未经过规则核对与翻译，不能确认或导出。"
        if run is not None and run.workflow_type == "read_index":
            gate = "这是识别与索引任务，不产生译文，也不提供导出。"
        revision=await db.get(rm.TranslationRevision,run.current_translation_revision_id) if run and run.current_translation_revision_id else None
        if fixed_parse and revision and str(fixed_parse.id) not in revision.parse_artifact_ids:revision=None
        issues = await db.run_sync(lambda sync: review_issues(sync, run, case)) if run else []
        views=[await issue_view(db,x) for x in issues]
        if fixed_parse:views=[v for v in views if any(side and side["target"].get("parse_artifact_id")==str(fixed_parse.id) for side in (v["a"],v["b"]))]
        checks = await db.run_sync(lambda sync: review_checks(sync, run, case)) if run else []
        counts={**_issue_counts(views),"not_covered":sum(c.status=="not_covered" for c in checks)}
        if run and run.workflow_type=="translate_review" and revision:
            gate="请核对当前未解决红项；其他提示不阻断导出。" if revision.qa_status in {"passed","warning"} else "译文检查尚未完成。"
        business_id=business_revision_id or (run.current_business_revision_id if run else case.current_business_revision_id)
        business=await db.get(BusinessRevision,business_id) if business_id else None
        if business and business.case_id!=case.id:raise NotFoundOrForbidden()
        readonly=bool(fixed_parse or business_revision_id and business_revision_id!=case.current_business_revision_id or run and run.current_business_revision_id and run.current_business_revision_id!=case.current_business_revision_id)
        if readonly:mode="historical";gate="固定来源与业务版本，只读核验；编辑请打开当前工作版。"
        editable="review_revise" in principal.permissions and any_parsed and not readonly
        actions=["open_source"]+(["confirm_field","mark_field_unknown","revise_field","assign_section","confirm_language","reparse","revise_segment"] if editable else [])
        disabled={}
        if run and run.workflow_type=="translate_review" and not readonly and revision:
            from app.exports.gate import review_gate
            checklist=await db.run_sync(lambda sync:review_gate(sync,run,can_confirm="export_confirm" in principal.permissions))
            if "export_confirm" in principal.permissions:actions.append("confirm_review")
            else:disabled["confirm_review"]="当前账号没有复核确认权限"
            if checklist["current_review_confirmation_id"] and "export_confirm" in principal.permissions:actions.append("export")
            else:disabled["export"]="请先核对复核清单并确认本次版本"
        else:disabled={"export":"当前视图尚无可导出的复核确认","confirm_review":"固定来源或只读识别不提供翻译确认"}
        unsettled=await db.scalar(select(StepExecution.id).where(StepExecution.run_id==run.id,
            StepExecution.status.in_([StepStatus.SUBMITTING,StepStatus.NEEDS_RECONCILE])).limit(1)) if run else None
        if unsettled and not readonly:
            gate="存在待回执或受理未知的外部请求，请先核查；当前仅可阅读已提交原件与解析。"
            disabled.update({action:gate for action in actions if action!="open_source"})
            actions=["open_source"]

        return {
            "case_id": str(case.id),
            "case_name": case.name,
            "run_id": str(run.id) if run else None,
            "snapshot_id": None,
            "mode": mode,
            "run_status": run.status.value if run else None,
            "target_language": run.target_language if run else None,
            "business_revision_id": str(business_id) if business_id else None,
            "business_revision_no": business.revision_no if business else None,
            "translation_revision_id": str(revision.id) if revision else None,
            "translation_revision_no": revision.revision_no if revision else None,
            "review_state_version": case.review_state_version if mode == "working" else None,
            "files": files,
            # 未运行规则与未覆盖法规保持独立计数。
            "issue_counts": counts,
            "gate_summary": gate,
            "allowed_actions": actions,
            "disabled_reasons": disabled,
            "run_event_cursor": {"run_id": str(run.id), "last_seq": run.next_event_seq} if run and mode == "working" else None,
            "snapshot_exported_at": None,
        }


# ---------- R03 字段与 Issue 分页 ----------

async def _snapshot_review_view(db,case,snapshot,document_id):
    manifest=snapshot.manifest
    revision=await db.get(rm.TranslationRevision,UUID(manifest["translation_revision_id"]))
    segments=(await db.execute(select(rm.TranslationSegment).where(rm.TranslationSegment.id.in_([UUID(x) for x in revision.segment_ids])))).scalars().all()
    translated_source_ids={UUID(x) for segment in segments for x in segment.source_segment_ids}
    issues=(await db.execute(select(rm.Issue).where(rm.Issue.id.in_([UUID(x) for x in manifest["issue_ids"]])))).scalars().all()
    views=[await issue_view(db,x,manifest["issue_disposition_ids"]) for x in issues]
    files=[]
    for source in manifest["source_versions"]:
        if document_id and source["document_id"]!=str(document_id):continue
        parse=await db.get(ParseArtifact,UUID(source["parse_artifact_id"]))
        lds=(await db.execute(select(LogicalDocument).where(LogicalDocument.parse_artifact_id==parse.id).order_by(LogicalDocument.ordinal))).scalars().all()
        logical=[]
        all_sections=[];all_decisions=[]
        for ld in lds:
            sections=(await db.execute(select(DocumentSection).where(DocumentSection.logical_document_id==ld.id).order_by(DocumentSection.ordinal))).scalars().all()
            decisions=(await db.execute(select(SectionAssignmentDecision).where(SectionAssignmentDecision.id.in_([UUID(x) for x in manifest.get("section_assignment_ids",[])]),SectionAssignmentDecision.section_id.in_([s.id for s in sections])).order_by(SectionAssignmentDecision.created_at))).scalars().all()
            typed=[d for d in decisions if d.logical_document_type]
            all_sections.extend(sections);all_decisions.extend(decisions)
            chosen=typed[-1].logical_document_type if typed else candidate_document_type(ld,parse)
            kind={"supporting":"other","unknown":"unclassified"}.get(chosen,chosen)
            logical.append({"logical_document_id":str(ld.id),"doc_type":kind,"cargo_profile_ids":typed[-1].cargo_profile_ids if typed else [p["code"] for p in candidate_profile(ld,parse).get("cargo_candidates",[])],"label":ld.title or _DOC_TYPE_LABEL[kind],
                "page_range":_page_range(ld.page_start,ld.page_end),"needs_confirmation":False,
                "sections":[{"section_id":str(s.id),"title":s.title or s.kind,"translated":bool((await db.execute(select(SourceSegment.id).where(SourceSegment.section_id==s.id,SourceSegment.id.in_(list(translated_source_ids))).limit(1))).scalar_one_or_none())} for s in sections]})
        pairing=next(p for p in manifest["pairings"] if p["document_id"]==source["document_id"])
        files.append({"source":{**source,**_source_language(all_sections,all_decisions)},"parse_artifact_id":str(parse.id),"parse_status":"parsed","preview_id":pairing["preview_id"],
            "page_count":parse.page_count or 0,"issue_counts":_issue_counts([v for v in views if any(side and side["target"].get("document_id")==source["document_id"] for side in (v["a"],v["b"]))]),"logical_documents":logical})
    return {"case_id":str(case.id),"case_name":case.name,"run_id":str(snapshot.run_id),"snapshot_id":str(snapshot.id),"mode":"historical",
        "run_status":"exported","target_language":manifest["target_language"],"business_revision_id":manifest["business_revision_id"],
        "business_revision_no":manifest["business_revision_no"],"translation_revision_id":manifest["translation_revision_id"],
        "translation_revision_no":manifest["translation_revision_no"],"review_state_version":None,"files":files,
        "issue_counts":{**_issue_counts(views),"not_covered":len(manifest.get("not_covered_notes",[]))},
        "gate_summary":"历史成功交付，只读核验；本视图不会切换为当前工作版。","allowed_actions":["open_source","download"],
        "disabled_reasons":{"revise":"历史成功快照只读"},"run_event_cursor":None,"snapshot_exported_at":snapshot.exported_at}


from app.documents.extraction import field_label

_GROUP_OF = {
    "parties": "parties",
    "logistics": "logistics",
    "transport": "logistics",
    "identifiers": "logistics",
    "goods": "goods",
    "amounts": "amounts",
}


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidRequest("分页游标无效，请从第一页重新读取") from exc


@router.get("/cases/{case_id}/fields", response_model=d.PageView[d.FieldView], response_model_exclude_unset=True)
async def list_fields(
    case_id: UUID,
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    review_state_version: int | None = None,
    business_revision_id: UUID | None = None,
    snapshot_id: UUID | None = None,
    run_id: UUID | None = None,
    parse_artifact_id: UUID | None = None,
    document_id: UUID | None = None,
    logical_document_id: UUID | None = None,
    source_segment_id: UUID | None = None,
    source_anchor_id: UUID | None = None,
    group: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = PAGE_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """
    当前工作版分页：首个游标绑定 review_state_version 与过滤条件；
    后续页发现版本已变返回 409 PAGE_STALE，前端从第一页重取，不拼接新旧处置。
    """
    async with db.begin():
        await set_tenant_context(db, principal)
        case = await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
        frozen_snapshot=None
        if snapshot_id:
            frozen_snapshot=await db.get(ExportSnapshot,snapshot_id)
            if frozen_snapshot is None or frozen_snapshot.case_id!=case.id:raise NotFoundOrForbidden()
        fixed_parse=await db.get(ParseArtifact,parse_artifact_id) if parse_artifact_id else None
        if parse_artifact_id and (fixed_parse is None or fixed_parse.case_id!=case.id):raise NotFoundOrForbidden()
        selected_business=await db.get(BusinessRevision,business_revision_id) if business_revision_id else None
        if business_revision_id and (selected_business is None or selected_business.case_id!=case.id):raise NotFoundOrForbidden()
        selected_run = await db.get(Run, run_id) if run_id else None
        if run_id and (selected_run is None or selected_run.case_id != case.id):
            raise NotFoundOrForbidden()
        if frozen_snapshot and run_id and frozen_snapshot.run_id != run_id:
            raise NotFoundOrForbidden()
        expected_business = selected_run.current_business_revision_id if selected_run else case.current_business_revision_id
        if not frozen_snapshot and not fixed_parse and business_revision_id and business_revision_id != expected_business:
            raise VersionConflict("业务版已变化，请重新加载",code="PAGE_STALE",current={"business_revision_id":str(expected_business) if expected_business else None})
        selected_source = await db.get(SourceSegment, source_segment_id) if source_segment_id else None
        selected_anchor = await db.get(SourceAnchor, source_anchor_id) if source_anchor_id else None
        if source_anchor_id:
            if selected_anchor is None or selected_anchor.case_id != case.id:raise NotFoundOrForbidden()
            if source_segment_id and selected_anchor.source_segment_id != source_segment_id:raise NotFoundOrForbidden()
            selected_source = await db.get(SourceSegment, selected_anchor.source_segment_id)
        if (source_segment_id or source_anchor_id) and (selected_source is None or selected_source.case_id != case.id
            or fixed_parse and selected_source.parse_artifact_id != fixed_parse.id):raise NotFoundOrForbidden()
        if selected_source and (selected_anchor and selected_anchor.parse_artifact_id != selected_source.parse_artifact_id
            or logical_document_id and selected_source.logical_document_id != logical_document_id):raise NotFoundOrForbidden()
        if selected_source and document_id:
            source_parse = await db.get(ParseArtifact, selected_source.parse_artifact_id)
            if source_parse is None or source_parse.document_id != document_id:raise NotFoundOrForbidden()
        filter_hash = hashlib.sha256(json.dumps([str(logical_document_id), str(document_id), str(parse_artifact_id),str(business_revision_id), str(run_id), group,str(source_segment_id),str(source_anchor_id)]).encode()).hexdigest()[:16]

        offset = 0
        if cursor and frozen_snapshot:
            decoded=_decode_cursor(cursor)
            if decoded.get("snapshot")!=str(snapshot_id) or decoded.get("f")!=filter_hash:raise InvalidRequest("历史分页游标与快照或筛选不一致")
            offset=int(decoded["o"])
        elif cursor and fixed_parse:
            decoded=_decode_cursor(cursor)
            if decoded.get("f")!=filter_hash:raise InvalidRequest("固定来源游标与筛选不一致")
            if review_state_version is not None and (review_state_version != case.review_state_version or decoded.get("rsv") != case.review_state_version):
                raise VersionConflict("复核状态已变化，请从第一页重新读取",code="PAGE_STALE",current={"review_state_version":case.review_state_version})
            offset=int(decoded["o"])
        elif cursor:
            decoded = _decode_cursor(cursor)
            if decoded.get("rsv") != case.review_state_version or decoded.get("f") != filter_hash:
                raise VersionConflict(
                    "复核状态已变化，请从第一页重新读取",
                    code="PAGE_STALE",
                    current={"review_state_version": case.review_state_version},
                )
            offset = int(decoded["o"])
        elif not frozen_snapshot and review_state_version is not None and review_state_version != case.review_state_version:
            raise VersionConflict(
                "复核状态已变化，请重新读取",
                code="PAGE_STALE",
                current={"review_state_version": case.review_state_version},
            )

        # 只取每个字段当前有效的断言：人工采用值断言会取代其引用的原始断言
        query = (
            select(FieldAssertion)
            .where(FieldAssertion.case_id == case.id,FieldAssertion.field_key != "vision_transcription_candidate")
            .order_by(FieldAssertion.logical_document_id, FieldAssertion.group, FieldAssertion.field_key, FieldAssertion.id)
        )
        if frozen_snapshot:
            query=query.where(FieldAssertion.id.in_([UUID(x) for x in frozen_snapshot.manifest["field_assertion_ids"]]))
        elif fixed_parse:
            query=query.where(FieldAssertion.parse_artifact_id==fixed_parse.id)
            if selected_business:query=query.join(BusinessRevisionField,BusinessRevisionField.field_assertion_id==FieldAssertion.id).where(BusinessRevisionField.business_revision_id==selected_business.id)
        elif selected_run and selected_run.current_business_revision_id:
            if selected_business and selected_business.id != selected_run.current_business_revision_id:
                raise NotFoundOrForbidden()
            selected_business = await db.get(BusinessRevision, selected_run.current_business_revision_id)
            query=query.join(BusinessRevisionField,BusinessRevisionField.field_assertion_id==FieldAssertion.id).where(
                BusinessRevisionField.business_revision_id==selected_run.current_business_revision_id)
        elif case.current_business_revision_id and not selected_run:
            query=query.join(BusinessRevisionField,BusinessRevisionField.field_assertion_id==FieldAssertion.id).where(
                BusinessRevisionField.business_revision_id==case.current_business_revision_id)
        elif selected_run:
            # 尚未发布业务版的 Run 也只读取自身已成功解析的冻结来源。
            # 下方统一按 run_parse_ids 限定，不能先取同票原件的最新解析。
            pass
        else:
            current_ids=[]
            versions=(await db.execute(select(DocumentVersion).join(SourceDocument,SourceDocument.id==DocumentVersion.document_id)
                .where(SourceDocument.case_id==case.id,DocumentVersion.version==SourceDocument.current_version))).scalars().all()
            for version in versions:
                parse=await latest_valid_parse(db,version.document_id,version.version)
                if parse:current_ids.append(parse.id)
            query=query.where(FieldAssertion.parse_artifact_id.in_(current_ids))
        if document_id:
            query=query.join(ParseArtifact,ParseArtifact.id==FieldAssertion.parse_artifact_id).where(ParseArtifact.document_id==document_id)
        if selected_run and not frozen_snapshot:
            from app.management.versions import run_parse_ids
            parse_ids = await db.run_sync(lambda sync: run_parse_ids(sync, selected_run))
            query = query.where(FieldAssertion.parse_artifact_id.in_(parse_ids))
        if logical_document_id is not None:
            query = query.where(FieldAssertion.logical_document_id == logical_document_id)
        if group is not None:
            query = query.where(FieldAssertion.group == group)
        rows = list((await db.execute(query)).scalars())

        superseded = {r.supersedes_assertion_id for r in rows if r.supersedes_assertion_id}
        effective = [r for r in rows if r.id not in superseded]
        if selected_source:
            effective = [r for r in effective if str(selected_source.id) in r.source_segment_ids]
        page = effective[offset : offset + limit]

        reviews: dict[UUID, FieldReviewEvent] = {}
        histories: dict[UUID, list] = {}
        if page:
            events = (
                await db.execute(
                    select(FieldReviewEvent)
                    .where(FieldReviewEvent.field_assertion_id.in_([r.id for r in page]))
                    .order_by(FieldReviewEvent.review_sequence,FieldReviewEvent.created_at,FieldReviewEvent.id)
                )
            ).scalars()
            if frozen_snapshot:
                allowed={UUID(x) for x in frozen_snapshot.manifest["field_review_event_ids"]}
                events=[e for e in events if e.id in allowed]
            else:
                events=[e for e in events if e.business_revision_id==(selected_business.id if selected_business else selected_run.current_business_revision_id if selected_run else case.current_business_revision_id)]
            for e in events:
                reviews[e.field_assertion_id] = e
                histories.setdefault(e.field_assertion_id, []).append({"at": e.created_at,
                    "by": str(e.decided_by), "action": e.action, "value": "",
                    "reason": e.reason, "business_revision_id": str(e.business_revision_id)})

        # 沿已选择断言的前驱回溯，绝不加载其后产生的修订或其他字段链。
        for field in page:
            lineage = [field]
            predecessor = field.supersedes_assertion_id
            visited = {field.id}
            while predecessor and predecessor not in visited:
                prior_field = await db.get(FieldAssertion, predecessor)
                if prior_field is None or prior_field.case_id != case.id or prior_field.parse_artifact_id != field.parse_artifact_id or prior_field.field_key != field.field_key or prior_field.logical_document_id != field.logical_document_id: break
                visited.add(prior_field.id); lineage.append(prior_field); predecessor = prior_field.supersedes_assertion_id
            events = list((await db.execute(select(FieldReviewEvent).where(FieldReviewEvent.field_assertion_id.in_([item.id for item in lineage]))
                .order_by(FieldReviewEvent.created_at, FieldReviewEvent.id))).scalars())
            boundary = frozen_snapshot.created_at if frozen_snapshot else None
            history = [{"at": item.created_at, "by": str(item.decided_by), "action": "revised", "value": item.candidate_value,
                "reason": item.reason or "", "business_revision_id": None} for item in lineage if item.is_human and (boundary is None or item.created_at <= boundary)]
            history.extend({"at": event.created_at, "by": str(event.decided_by), "action": event.action, "value": "", "reason": event.reason,
                "business_revision_id": str(event.business_revision_id)} for event in events if boundary is None or event.created_at <= boundary)
            histories[field.id] = sorted(history, key=lambda item: item["at"])

        selected_id = UUID(frozen_snapshot.manifest["business_revision_id"]) if frozen_snapshot else selected_business.id if selected_business else selected_run.current_business_revision_id if selected_run else case.current_business_revision_id
        issue_links = await field_issue_links(db, case.id, selected_id, page, snapshot=frozen_snapshot, run_id=run_id) if selected_id else {}

        items = []
        for fa in page:
            parse = await db.get(ParseArtifact, fa.parse_artifact_id)
            review = reviews.get(fa.id)
            if fa.is_human:
                status = "revised"
            elif review is not None:
                status = "confirmed" if review.action == "confirmed" else "unknown"
            else:
                # 机器候选永远不显示为已确认
                status = "candidate"
            field_source_id = selected_source.id if selected_source else UUID(fa.source_segment_ids[0]) if fa.source_segment_ids else None
            field_anchor_id = selected_anchor.id if selected_anchor else await db.scalar(select(SourceAnchor.id).where(
                SourceAnchor.source_segment_id == field_source_id).order_by(SourceAnchor.id).limit(1)) if field_source_id else None
            items.append(
                {
                    "field_id": str(fa.id),
                    "source_segment_ids": fa.source_segment_ids,
                    "group": _GROUP_OF.get(fa.group, "goods"),
                    "name": field_label(fa.field_key, fa.role),
                    "role": fa.role,
                    "document_id": str(parse.document_id) if parse else None,
                    "logical_document_id": str(fa.logical_document_id),
                    "original_text": fa.source_text or "",
                    "normalized_value": fa.normalized_value or fa.candidate_value or "",
                    "adopted_value": fa.candidate_value if fa.is_human else None,
                    "unit": fa.unit,
                    "review_status": status,
                    "issue_id": next((i["issue_id"] for i in issue_links.get(str(fa.id), []) if not i["resolved"]), None),
                    "issues": issue_links.get(str(fa.id), []),
                    "confirm_blocked_reason": "该字段仍有未解决的红色事实疑点，请先核对" if any(i["blocks_confirmation"] for i in issue_links.get(str(fa.id), [])) else "该字段缺少可核验原文来源" if not fa.source_segment_ids else "该字段为空，请核对原件后补充信息" if not (fa.normalized_value or fa.candidate_value or "").strip() else None,
                    "history": histories.get(fa.id, []),
                    "confidence_note": fa.ambiguity,
                    "target": {
                        "case_id": str(case.id),
                        "run_id": str(frozen_snapshot.run_id) if frozen_snapshot else str(selected_run.id) if selected_run else None,
                        "document_id": str(parse.document_id) if parse else None,
                        "document_version": parse.document_version if parse else None,
                        "parse_artifact_id": str(fa.parse_artifact_id),
                        "logical_document_id": str(fa.logical_document_id) if not selected_source or selected_source.logical_document_id == fa.logical_document_id else None,
                        "section_id": str(fa.section_id) if fa.section_id and (not selected_source or selected_source.section_id == fa.section_id) else None,
                        "field_id": str(fa.id),
                        "source_segment_id": str(field_source_id) if field_source_id else None,
                        "source_anchor_id": str(field_anchor_id) if field_anchor_id else None,
                        "business_revision_id": str(selected_id) if selected_id else None,
                        "export_snapshot_id":str(snapshot_id) if frozen_snapshot else None,
                    },
                }
            )

        next_offset = offset + len(page)
        has_more = next_offset < len(effective)
        return {
            "items": items,
            "next_cursor": _encode_cursor({"o":next_offset,"snapshot":str(snapshot_id),"f":filter_hash} if frozen_snapshot else {"o": next_offset, "rsv": case.review_state_version, "f": filter_hash}) if has_more else None,
            "has_more": has_more,
            "as_of": datetime.now(UTC).isoformat(),
            "review_state_version": case.review_state_version,
        }


@router.get("/cases/{case_id}/issues", response_model=d.IssuePageView, response_model_exclude_unset=True)
async def list_issues(
    case_id: UUID,
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    business_revision_id: UUID | None = None,
    review_state_version: int | None = None,
    run_id: UUID | None = None,
    document_id: UUID | None = None,
    snapshot_id: UUID | None = None,
    parse_artifact_id: UUID | None = None,
    severity: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = PAGE_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """按业务版和复核游标读取真实 Issue；不把未运行规则说成已通过。"""
    async with db.begin():
        await set_tenant_context(db, principal)
        case = await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
        snapshot=await db.get(ExportSnapshot,snapshot_id) if snapshot_id else None
        if snapshot_id and (snapshot is None or snapshot.case_id!=case.id):raise NotFoundOrForbidden()
        fixed_parse=await db.get(ParseArtifact,parse_artifact_id) if parse_artifact_id else None
        if parse_artifact_id and (fixed_parse is None or fixed_parse.case_id!=case.id):raise NotFoundOrForbidden()
        selected_run=await db.get(Run,run_id) if run_id else None
        if run_id and (selected_run is None or selected_run.case_id!=case.id):raise NotFoundOrForbidden()
        if snapshot and selected_run and snapshot.run_id!=selected_run.id:raise NotFoundOrForbidden()
        expected_business=selected_run.current_business_revision_id if selected_run else case.current_business_revision_id
        if not snapshot and not fixed_parse and business_revision_id and business_revision_id!=expected_business:raise VersionConflict("业务版已变化",code="PAGE_STALE")
        if not snapshot and not fixed_parse and review_state_version is not None and review_state_version!=case.review_state_version:raise VersionConflict("复核状态已变化",code="PAGE_STALE")
        selected_business=snapshot.manifest["business_revision_id"] if snapshot else str(business_revision_id or expected_business)
        page_version=str(snapshot.id) if snapshot else str(fixed_parse.id) if fixed_parse else case.review_state_version
        filter_hash=hashlib.sha256(json.dumps([str(run_id),str(document_id),str(parse_artifact_id),selected_business,severity]).encode()).hexdigest()[:16]
        offset=0
        if cursor:
            decoded=_decode_cursor(cursor)
            if decoded.get("v")!=page_version or decoded.get("f")!=filter_hash:raise VersionConflict("复核版本或筛选已变化",code="PAGE_STALE")
            offset=int(decoded["o"])
        query=select(rm.Issue).where(rm.Issue.case_id==case.id).order_by(rm.Issue.created_at,rm.Issue.id)
        if snapshot:query=query.where(rm.Issue.id.in_([UUID(x) for x in snapshot.manifest["issue_ids"]]))
        else:
            query=query.join(Run,Run.id==rm.Issue.run_id).where(rm.Issue.business_revision_id==business_revision_id if business_revision_id else rm.Issue.business_revision_id==expected_business)
            query=query.where(or_(rm.Issue.translation_revision_id.is_(None),rm.Issue.translation_revision_id==Run.current_translation_revision_id,select(rm.IssueDisposition.id).where(rm.IssueDisposition.issue_id==rm.Issue.id,rm.IssueDisposition.action=="revise").exists()))
        if run_id:
            run=await db.get(Run,run_id)
            if run is None or run.case_id!=case.id:raise NotFoundOrForbidden()
            query=query.where(rm.Issue.run_id==run.id)
            if not snapshot:query=query.where(or_(rm.Issue.translation_revision_id.is_(None),rm.Issue.translation_revision_id==run.current_translation_revision_id,select(rm.IssueDisposition.id).where(rm.IssueDisposition.issue_id==rm.Issue.id,rm.IssueDisposition.action=="revise").exists()))
        rows = await db.run_sync(lambda sync: review_issues(sync, run, case)) if run_id and not snapshot else list((await db.execute(query)).scalars())
        if fixed_parse:rows=[x for x in rows if any(s.get("parse_artifact_id")==str(fixed_parse.id) or s.get("target",{}).get("parse_artifact_id")==str(fixed_parse.id) for s in x.sources)]
        if document_id:rows=[x for x in rows if any(s.get("document_id")==str(document_id) or s.get("target",{}).get("document_id")==str(document_id) for s in x.sources)]
        views = [await issue_view(db, row, snapshot.manifest["issue_disposition_ids"] if snapshot else None, permissions=principal.permissions) for row in rows]
        pending = [item for item in views if item["status"] in {"open", "unresolved"}]
        counts = {"all": len(views), "blocking": sum(item["severity"] == "blocking" for item in pending),
            "warning": sum(item["severity"] == "warning" for item in pending),
            "undetermined": sum(item["severity"] == "undetermined" or item["status"] == "unresolved" for item in pending),
            "done": sum(item["status"] not in {"open", "unresolved"} for item in views)}
        views.sort(key=lambda item: (item["status"] not in {"open", "unresolved"},
            {"blocking": 0, "warning": 1, "undetermined": 2}.get(item["severity"], 3)))
        if severity and severity != "all":
            if severity not in {"open", "blocking", "warning", "undetermined", "done"}:
                raise InvalidRequest("疑点筛选状态不合法")
            views = [item for item in views if (item["status"] not in {"open", "unresolved"})] if severity == "done" else [
                item for item in views if item["status"] in {"open", "unresolved"} and
                (severity == "open" or item["severity"] == severity or severity == "undetermined" and item["status"] == "unresolved")]
        page=views[offset:offset+limit]
        check_query=select(rm.RuleCheck).where(rm.RuleCheck.case_id==case.id)
        check_query=check_query.where(rm.RuleCheck.id.in_([UUID(x) for x in snapshot.manifest["rule_check_ids"]])) if snapshot else check_query.where(rm.RuleCheck.business_revision_id==business_revision_id if business_revision_id else rm.RuleCheck.business_revision_id==expected_business)
        if run_id:check_query=check_query.where(rm.RuleCheck.run_id==run_id)
        checks = await db.run_sync(lambda sync: bool(review_checks(sync, run, case))) if run_id and not snapshot else (await db.execute(check_query.limit(1))).scalar_one_or_none()
        return {
            "items": page,
            "counts": counts,
            "next_cursor": _encode_cursor({"o":offset+len(page),"v":page_version,"f":filter_hash}) if offset+len(page)<len(views) else None,
            "has_more": offset+len(page)<len(views),
            "as_of": datetime.now(UTC).isoformat(),
            "review_state_version": case.review_state_version,
            "rules_status": "run" if checks else "not_run",
        }


# ---------- U08 阅读视图、U09 页图 ----------


@router.get("/parse-artifacts/{parse_artifact_id}/reading-view", response_model=d.ReadingView, response_model_exclude_unset=True)
async def reading_view(
    parse_artifact_id: UUID,
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    section_id: UUID | None = None,
    query: str | None = None,
    source_segment_id: UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """
    按区段／游标分页返回源片段、阅读顺序与定位精度。

    内容来自解析后的结构化片段，本身就是纯文本；不返回任何 HTML、脚本或外链，
    也没有可靠页码时不编造。
    """
    async with db.begin():
        await set_tenant_context(db, principal)
        parse = await db.get(ParseArtifact, parse_artifact_id)
        if parse is None:
            raise NotFoundOrForbidden()
        await load_case_for_read(db, parse.case_id, user_id=principal.user_id, settings=settings)

        stmt = (
            select(SourceSegment, DocumentSection)
            .join(DocumentSection, DocumentSection.id == SourceSegment.section_id)
            .join(LogicalDocument,LogicalDocument.id==SourceSegment.logical_document_id)
            .where(SourceSegment.parse_artifact_id == parse.id)
            .order_by(LogicalDocument.ordinal,DocumentSection.ordinal,SourceSegment.ordinal,SourceSegment.id)
        )
        if section_id is not None:
            stmt = stmt.where(SourceSegment.section_id == section_id)
        if query:
            # 原文搜索只在本解析版内进行，已先完成授权
            stmt = stmt.where(SourceSegment.text.ilike(f"%{query.replace('%', r'\%').replace('_', r'\_')}%"))
        scope=hashlib.sha256(json.dumps([str(parse.id),str(section_id),query,str(source_segment_id)]).encode()).hexdigest()
        offset=0
        if cursor:
            decoded=_decode_cursor(cursor)
            if decoded.get("f")!=scope:raise InvalidRequest("阅读游标不属于当前来源／筛选")
            offset=int(decoded["o"])
            if offset<0:raise InvalidRequest("阅读游标不能为负")
        elif source_segment_id:
            target=await db.get(SourceSegment,source_segment_id)
            if target is None or target.parse_artifact_id!=parse.id:raise NotFoundOrForbidden()
            ordered=stmt.with_only_columns(SourceSegment.id,func.row_number().over(order_by=(LogicalDocument.ordinal,DocumentSection.ordinal,SourceSegment.ordinal,SourceSegment.id)).label("position")).order_by(None).subquery()
            position=(await db.execute(select(ordered.c.position).where(ordered.c.id==source_segment_id))).scalar_one_or_none()
            if position is None:raise NotFoundOrForbidden()
            offset=(position-1)//limit*limit
        rows=list((await db.execute(stmt.offset(offset).limit(limit+1))).all())
        page=rows[:limit]
        anchors: dict[UUID, SourceAnchor] = {}
        if page:
            for a in (
                await db.execute(select(SourceAnchor).where(SourceAnchor.source_segment_id.in_([s.id for s, _ in page])))
            ).scalars():
                anchors.setdefault(a.source_segment_id, a)

        items = []
        for seg, sec in page:
            anchor = anchors.get(seg.id)
            items.append(
                {
                    "source_segment_id": str(seg.id),
                    "section_id": str(sec.id),
                    "logical_document_id": str(seg.logical_document_id),
                    "structure": seg.structure,
                    "text": seg.text,
                    "table_row": seg.table_row,
                    "table_col": seg.table_col,
                    "page_no": anchor.page_no if anchor else None,
                    "anchor_precision": anchor.precision.value if anchor else AnchorPrecision.NONE.value,
                    "bbox": anchor.bbox if anchor and anchor.precision is AnchorPrecision.RECT else None,
                }
            )
        next_offset = offset + len(page)
        return {
            "parse_artifact_id": str(parse.id),
            "document_id": str(parse.document_id),
            "document_version": parse.document_version,
            "is_synthetic": parse.is_synthetic,
            "quality": parse.quality.value,
            "quality_flags": parse.quality_flags,
            "items": items,
            "next_cursor": _encode_cursor({"o": next_offset,"f":scope}) if len(rows)>limit else None,
            "has_more": len(rows)>limit,
        }


@router.get("/parse-artifacts/{parse_artifact_id}/pages/{page_no}/image")
async def page_image(
    parse_artifact_id: UUID,
    page_no: int,
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    request: Request,
    scale: Annotated[float, Query(ge=0.5, le=3.0)] = 1.5,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    """
    校验解析版属于有权原件，渲染固定版本的页图。分辨率有上限，避免被用来放大拉取。
    """
    async with db.begin():
        await set_tenant_context(db, principal)
        parse = await db.get(ParseArtifact, parse_artifact_id)
        if parse is None:
            raise NotFoundOrForbidden()
        await load_case_for_read(db, parse.case_id, user_id=principal.user_id, settings=settings)
        dv = (
            await db.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == parse.document_id, DocumentVersion.version == parse.document_version
                )
            )
        ).scalar_one()
        obj = await db.get(StoredObject, dv.object_id)
        if obj is None or not obj.is_visible:
            raise NotFoundOrForbidden()
        key, mime, size = obj.object_key, dv.mime, dv.size_bytes

    from app.api.routers.uploads import _store

    from fastapi.responses import StreamingResponse
    from app.infra.content_stream import prepared_bytes
    from app.api.routers.uploads import _parse_range
    store = _store(request)
    content = None
    if mime.startswith("image/"):
        if page_no != 1:
            raise NotFoundOrForbidden()
    elif mime != "application/pdf":
        raise InvalidRequest("这种格式没有页图，请使用阅读视图", code="NO_PAGE_IMAGE")
    else:
        import asyncio
        import io
        import pypdfium2 as pdfium

        def render():
            pdf=pdfium.PdfDocument(io.BytesIO(store.read_all(key)))
            try:
                if page_no < 1 or page_no > len(pdf):
                    raise NotFoundOrForbidden()
                page=pdf[page_no - 1]
                try:
                    bitmap=page.render(scale=scale)
                    try:
                        image=bitmap.to_pil()
                        try:
                            buf=io.BytesIO();image.save(buf,format="PNG")
                            return buf.getvalue()
                        finally:image.close()
                    finally:
                        bitmap.close()
                finally:page.close()
            finally:pdf.close()
        content = await asyncio.to_thread(render)
        size = len(content);mime = "image/png"
    headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff","Accept-Ranges":"bytes",
        "X-Render-Scale":str(scale),"X-Parse-Artifact-Id":str(parse_artifact_id)}
    try:bounds=_parse_range(range_header,size)
    except ValueError:
        return Response(status_code=416,headers={**headers,"Content-Range":f"bytes */{size}"})
    start,end=bounds if bounds else (0,size-1)
    stream=await prepared_bytes(request,principal,settings,parse.case_id,data=content,store=store,key=key,start=start,end=end,expected_size=size)
    return StreamingResponse(stream,
        status_code=206 if bounds else 200,media_type=mime,headers={**headers,"Content-Length":str(end-start+1),
            **({"Content-Range":f"bytes {start}-{end}/{size}"} if bounds else {})})
