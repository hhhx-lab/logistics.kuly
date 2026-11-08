"""
`@ ` 引用的鉴权与版本冻结（Agent 方案第 4 节）。

发送时逐层核对 `tenant → user/Case 权限 → Run/Document/Snapshot 归属 → 版本配对`，
写不可变 ReferenceSnapshot。模型只读到冻结结果，不重新按「最新」解析。

规则：
- 一个会话只绑定一个 Case；第二票返回 CROSS_CASE_REFERENCE_UNSUPPORTED 并引导新建会话。
- Section 首版只能 fixed，必须携完整五元组；旧解析的区段不按同名映射到新解析。
- 一轮最多一个 Export Snapshot；它只提供历史语境，事实范围取与原件标签的交集。
- 未解析原件只冻结真实存在的原件版本，不虚构解析或业务版本。
"""


from __future__ import annotations
from app.languages import LanguageCode, requested_language

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError, InvalidRequest, NotFoundOrForbidden
from app.config import Settings
from app.documents.evidence_models import DocumentSection, ParseArtifact, ParseQuality
from app.documents.models import Case, DocumentVersion, SourceDocument
from app.documents.retention import load_case_for_read
from app.exports.models import ExportSnapshot
from app.workflows.models import Run, RunDocument


class CrossCaseReference(ApiError):
    status_code = 409
    code = "CROSS_CASE_REFERENCE_UNSUPPORTED"
    message = "本会话只处理一票。要查看另一票，请新建会话。"


class ReferenceSelection(BaseModel):
    """前端 Lexical 标签里保存的稳定 ID；服务端不信任其中的名称或权限声明。"""

    kind: Literal["case", "run", "document", "section", "snapshot"]
    id: UUID
    selection_mode: Literal["current", "fixed"]
    case_id: UUID
    label: str | None = Field(default=None, max_length=200)
    document_id: UUID | None = None
    document_version: int | None = None
    parse_artifact_id: UUID | None = None
    logical_document_id: UUID | None = None
    section_id: UUID | None = None
    snapshot_id: UUID | None = None
    run_id: UUID | None = None
    target_language: LanguageCode | None = None
    translation_revision_id: UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ReferenceSelection":
        if self.kind == "snapshot" and self.selection_mode != "fixed":
            raise ValueError("成功快照只能固定引用")
        if self.kind == "section":
            if self.selection_mode != "fixed":
                raise ValueError("区段首版只能固定引用")
            missing = [
                f
                for f in ("document_id", "document_version", "parse_artifact_id", "logical_document_id", "section_id")
                if getattr(self, f) is None
            ]
            if missing:
                raise ValueError(f"区段引用缺少：{', '.join(missing)}")
        return self


async def latest_valid_parse(session: AsyncSession, document_id: UUID, version: int) -> ParseArtifact | None:
    return (
        await session.execute(
            select(ParseArtifact)
            .where(
                ParseArtifact.document_id == document_id,
                ParseArtifact.document_version == version,
                ParseArtifact.quality != ParseQuality.FAILED,
            )
            .order_by(ParseArtifact.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def adopted_case_parse(session, case, document_id, version):
    from app.documents.evidence_models import BusinessRevisionField, FieldAssertion
    if case.current_business_revision_id:
        rows=list((await session.execute(select(ParseArtifact).join(FieldAssertion,FieldAssertion.parse_artifact_id==ParseArtifact.id)
            .join(BusinessRevisionField,BusinessRevisionField.field_assertion_id==FieldAssertion.id)
            .where(BusinessRevisionField.business_revision_id==case.current_business_revision_id,
                ParseArtifact.case_id==case.id,ParseArtifact.document_id==document_id,ParseArtifact.document_version==version,
                ParseArtifact.quality!=ParseQuality.FAILED).distinct())).scalars())
        if len(rows)>1:raise InvalidRequest("当前业务版采用了多个解析，请选择具体Run或区段")
        if rows:return rows[0]
    return await latest_valid_parse(session,document_id,version)


async def freeze_references(
    session: AsyncSession,
    *,
    selections: list[ReferenceSelection],
    bound_case_id: UUID | None,
    user_id: UUID,
    settings: Settings,
) -> tuple[UUID | None, list[dict[str, Any]]]:
    """
    返回 (本轮绑定的 Case, 冻结结果列表)。

    冻结结果只含受控 ID 与版本，不含原文、对象键或模型数据。
    """
    if not selections:
        return bound_case_id, []
    selections = list(selections)
    explicit_snapshots = {selection.snapshot_id or selection.id for selection in selections if selection.kind == "snapshot"}
    for selection in list(selections):
        if selection.kind != "snapshot" and selection.snapshot_id and selection.snapshot_id not in explicit_snapshots:
            selections.append(ReferenceSelection(kind="snapshot", id=selection.snapshot_id, snapshot_id=selection.snapshot_id,
                case_id=selection.case_id, run_id=selection.run_id, selection_mode="fixed"))
            explicit_snapshots.add(selection.snapshot_id)

    case_ids = {s.case_id for s in selections}
    if len(case_ids) > 1:
        raise CrossCaseReference()
    target_case = next(iter(case_ids))
    if bound_case_id is not None and target_case != bound_case_id:
        raise CrossCaseReference(details={"requested_case_id": str(target_case)})
    if sum(1 for s in selections if s.kind == "snapshot") > 1:
        raise InvalidRequest("一轮最多选择一个成功快照；跨快照比较请分轮提问")

    case: Case = await load_case_for_read(session, target_case, user_id=user_id, settings=settings)
    frozen: list[dict[str, Any]] = []

    for sel in selections:
        item: dict[str, Any] = {"kind": sel.kind, "id": str(sel.id), "selection_mode": sel.selection_mode, "case_id": str(case.id)}

        if sel.kind == "case":
            # 当前指针：发送时冻结当前业务版；未处理的票明确为 null，不虚构版本
            item["business_revision_id"] = str(case.current_business_revision_id) if case.current_business_revision_id else None
            sources = list((await session.execute(select(SourceDocument).where(SourceDocument.case_id == case.id))).scalars())
            item["source_versions"] = []
            for source in sources:
                if source.current_version:
                    parse = await adopted_case_parse(session, case, source.id, source.current_version)
                    item["source_versions"].append({"document_id": str(source.id), "document_version": source.current_version,
                                                    "parse_artifact_id": str(parse.id) if parse else None})

        elif sel.kind == "run":
            run = await session.get(Run, sel.id)
            if run is None or run.case_id != case.id:
                raise NotFoundOrForbidden()
            docs = (
                await session.execute(
                    select(RunDocument).where(RunDocument.run_id == run.id).order_by(RunDocument.ordinal)
                )
            ).scalars()
            item.update(
                run_id=str(run.id),
                task_spec_hash=run.task_spec_hash,
                source_versions=[],
                business_revision_id=str(run.current_business_revision_id) if run.current_business_revision_id else None,
                translation_revision_id=str(run.current_translation_revision_id) if run.current_translation_revision_id else None,
            )

            for d in docs:
                from app.management.versions import run_parse
                parse = await session.run_sync(lambda db: run_parse(db, run, d.document_id, d.document_version))
                item["source_versions"].append({"document_id": str(d.document_id), "document_version": d.document_version,
                                                "parse_artifact_id": str(parse.id) if parse else None})

        elif sel.kind == "document":
            doc = await session.get(SourceDocument, sel.document_id or sel.id)
            if doc is None or doc.case_id != case.id:
                raise NotFoundOrForbidden()
            # current 在发送时解析为最新原件版；fixed 必须给出版本
            version = doc.current_version if sel.selection_mode == "current" else sel.document_version
            if version is None:
                raise InvalidRequest("固定原件引用必须给出版本号")
            dv = (
                await session.execute(
                    select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.version == version)
                )
            ).scalar_one_or_none()
            if dv is None:
                raise NotFoundOrForbidden()
            parse = await session.get(ParseArtifact, sel.parse_artifact_id) if sel.parse_artifact_id else await latest_valid_parse(session, doc.id, version)
            if sel.parse_artifact_id and parse is None:
                raise NotFoundOrForbidden()
            if parse and (parse.tenant_id != case.tenant_id or parse.case_id != case.id or parse.document_id != doc.id or parse.document_version != version):
                raise NotFoundOrForbidden()
            item.update(
                document_id=str(doc.id),
                document_version=version,
                filename=dv.filename,
                # 未解析只冻结原件版本，不虚构解析版
                parse_artifact_id=str(parse.id) if parse else None,
            )

        elif sel.kind == "section":
            section = await session.get(DocumentSection, sel.section_id)
            if (
                section is None
                or section.case_id != case.id
                or section.parse_artifact_id != sel.parse_artifact_id
                or section.logical_document_id != sel.logical_document_id
            ):
                raise NotFoundOrForbidden()
            parse = await session.get(ParseArtifact, sel.parse_artifact_id)
            if parse is None or parse.document_id != sel.document_id or parse.document_version != sel.document_version:
                raise NotFoundOrForbidden()
            current = await latest_valid_parse(session, parse.document_id, parse.document_version)
            item.update(
                document_id=str(sel.document_id),
                document_version=sel.document_version,
                parse_artifact_id=str(sel.parse_artifact_id),
                logical_document_id=str(sel.logical_document_id),
                section_id=str(sel.section_id),
                # 重解析后旧区段只作历史只读，不自动映射到新解析的同名节
                historical=current is not None and current.id != parse.id,
            )

        elif sel.kind == "snapshot":
            exported = await session.get(ExportSnapshot, sel.snapshot_id or sel.id)
            if exported is None or exported.case_id != case.id or exported.tenant_id != case.tenant_id or (sel.run_id and sel.run_id != exported.run_id):
                raise NotFoundOrForbidden()
            manifest = exported.manifest
            item.update(snapshot_id=str(exported.id), run_id=str(exported.run_id),
                task_spec_hash=manifest["task_spec_hash"], source_versions=manifest["source_versions"],
                business_revision_id=manifest["business_revision_id"], translation_revision_id=manifest["translation_revision_id"],
                field_assertion_ids=manifest["field_assertion_ids"], historical=True)
            item["comparison"] = manifest.get("comparison")
            item["issue_disposition_ids"] = manifest.get("issue_disposition_ids", [])
            if item["comparison"]:
                item["source_versions"] = [{"document_id": p["document_id"], "document_version": p["version"],
                    "parse_artifact_id": p["id"]} for p in item["comparison"]["manifest"]["parses"]]

        if sel.kind in {"case", "run", "document", "section"}:
            from app.consignment import models as cm, service as cs
            if sel.kind == "run":
                batch = await session.run_sync(lambda db: cs.bound_batch(db, run, case))
            else:
                batch = await session.get(cm.ComparisonBatch, case.current_comparison_batch_id) if case.current_comparison_batch_id else None
                if batch and not await session.run_sync(lambda db: cs.batch_current(db, case, batch)):
                    batch = None
            if batch and sel.kind in {"document", "section"} and not any(
                p.get("document_id") == item.get("document_id") and p.get("version") == item.get("document_version")
                and p.get("id") == item.get("parse_artifact_id") for p in batch.manifest.get("parses", [])):
                # 固定旧原件不能附带后来其他解析的核对批次，否则原文货物行会被排除。
                batch = None
            if batch:
                from app.review.models import Issue, IssueDisposition
                disposition_ids = list((await session.execute(select(IssueDisposition.id).join(Issue,
                    Issue.id == IssueDisposition.issue_id).where(Issue.comparison_batch_id == batch.id))).scalars())
                item["comparison"] = {"batch_id": str(batch.id), "scope_revision_id": str(batch.scope_revision_id),
                    "input_fingerprint": batch.input_fingerprint, "manifest": batch.manifest,
                    "issue_disposition_ids": [str(x) for x in disposition_ids]}
                if sel.kind in {"case", "run"}:
                    item["source_versions"] = [{"document_id": p["document_id"], "document_version": p["version"],
                        "parse_artifact_id": p["id"]} for p in batch.manifest["parses"]]

        frozen.append(item)

    historical = next((item for item in frozen if item["kind"] == "snapshot"), None)
    if historical:
        by_source = {(source["document_id"], source["document_version"]): source for source in historical["source_versions"]}
        for item in frozen:
            if item["kind"] in {"document", "section"}:
                source = by_source.get((item["document_id"], item["document_version"]))
                if source is None:
                    raise InvalidRequest("原件或区段不属于所选成功快照")
                if item["kind"] == "section" and item["parse_artifact_id"] != source.get("parse_artifact_id"):
                    raise InvalidRequest("区段解析版与成功快照不匹配")
                item["parse_artifact_id"] = source.get("parse_artifact_id")
                item["business_revision_id"] = historical["business_revision_id"]
                item["historical"] = True

    # 接单时固定处置记录；模型执行时不读取后来追加的人工结论。
    if not historical:
        from app.review.models import Issue, IssueDisposition
        revision_ids = {UUID(f["business_revision_id"]) for f in frozen if f.get("business_revision_id")}
        if revision_ids:
            ids = list((await session.execute(select(IssueDisposition.id).join(Issue, Issue.id == IssueDisposition.issue_id)
                .where(Issue.case_id == case.id, Issue.business_revision_id.in_(revision_ids)))).scalars())
            for item in frozen:
                item["issue_disposition_ids"] = [str(x) for x in ids]

    return case.id, frozen


def fact_scope(frozen: list[dict[str, Any]]) -> dict[str, Any]:
    """
    默认事实范围：有快照时取「快照 ∩ 原件／区段标签」；
    只有明确要求同票比较时，才扩展到快照内其余文件（由工具层显式放宽）。
    """
    documents = [f for f in frozen if f["kind"] in {"document", "section"}]
    snapshot = next((f for f in frozen if f["kind"] == "snapshot"), None)
    return {"snapshot": snapshot, "documents": documents, "narrowed": bool(snapshot and documents)}


async def freeze_translation_context(db, conversation, frozen, selections, question):
    """译文选择只发生于接单冻结；执行图不再取后来产生的最新译文。"""
    required = any(word in question for word in ("译文", "翻译结果", "怎么译"))
    from app.review.models import TranslationRevision, TranslationSegment
    explicit = next((s.translation_revision_id for s in selections if s.translation_revision_id), None)
    language = next((s.target_language for s in selections if s.target_language), None)
    language = language or requested_language(question)
    docs = [f for f in frozen if f["kind"] in {"document", "section"}]
    sources = docs or [v for f in frozen for v in f.get("source_versions", [])]
    if not sources:
        return frozen
    versions = {(v["document_id"], v["document_version"]): v.get("parse_artifact_id") for v in sources}
    chosen_ids = [f.get("translation_revision_id") for f in frozen if f.get("translation_revision_id")]
    explicit_run = next((s.run_id or s.id for s in selections if s.kind == "run" or s.run_id), None)
    candidates = []
    if explicit:
        revision = await db.get(TranslationRevision, explicit)
        if revision is None or revision.case_id != conversation.case_id:
            raise NotFoundOrForbidden()
        candidates = [revision]
    elif chosen_ids:
        candidates = list((await db.execute(select(TranslationRevision).where(TranslationRevision.id.in_([UUID(x) for x in chosen_ids])))).scalars())
    elif explicit_run:
        run = await db.get(Run, explicit_run)
        if run is None or run.case_id != conversation.case_id:
            raise NotFoundOrForbidden()
        candidates = [await db.get(TranslationRevision, run.current_translation_revision_id)] if run.current_translation_revision_id else []
    else:
        candidates = list((await db.execute(select(TranslationRevision).join(Run, Run.current_translation_revision_id == TranslationRevision.id)
            .where(TranslationRevision.case_id == conversation.case_id, TranslationRevision.is_synthetic.is_(False),
                TranslationRevision.qa_status.in_(["passed", "warning"])).order_by(TranslationRevision.created_at.desc()).limit(100))).scalars())
    valid = []
    for revision in candidates:
        if revision is None or revision.is_synthetic or (language and revision.target_language != language):
            continue
        segments = (await db.execute(select(TranslationSegment.document_id, TranslationSegment.document_version, TranslationSegment.parse_artifact_id)
            .where(TranslationSegment.id.in_([UUID(x) for x in revision.segment_ids])))).all()
        paired = {(str(x.document_id), x.document_version): str(x.parse_artifact_id) for x in segments}
        selected_versions = versions if docs else {key: value for key, value in versions.items() if key in paired}
        if not selected_versions or any(key not in paired or (parse and paired[key] != parse) for key, parse in selected_versions.items()):
            continue
        if not (explicit or explicit_run or chosen_ids) and conversation.case_id:
            case = await db.get(Case, conversation.case_id)
            if revision.business_revision_id != case.current_business_revision_id:
                continue
        valid.append(revision)
    selected = next((r for r in valid if r.run_id == conversation.active_run_id), None)
    if selected is None and valid:
        languages = {r.target_language for r in valid}
        if len(languages) == 1:
            selected = valid[0]
    if selected is None:
        notice = "此固定原件有多个译文语种，请明确中文或英文，或关联对应 Run。" if len({r.target_language for r in valid}) > 1 else "所选原件/解析与要求的译文尚无有效配对，请选择对应 Run、语种或成功快照。"
        return frozen + [{"kind": "translation_notice", "notice": notice}] if required else frozen
    return frozen + [{"kind": "translation_context", "translation_revision_id": str(selected.id), "target_language": selected.target_language,
        "run_id": str(selected.run_id), "business_revision_id": str(selected.business_revision_id), "translation_revision_no": selected.revision_no,
        "historical": bool(explicit or chosen_ids), "selection_mode": "fixed"}]
