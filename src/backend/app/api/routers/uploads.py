"""U01–U07：Case、上传会话、逐文件验收与鉴权原件读取。"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, tuple_

from app.api import idempotency
from app.api.dto import PublishedView
from app.api.dependencies import CurrentUser, DbSession, SettingsDep, WriteUser, set_tenant_context
from app.api.errors import ApiError, InvalidRequest, NotFoundOrForbidden
from app.api.routers.auth import build_disclosure
from app.config import Settings
from app.documents import uploads as upload_service
from app.documents.models import (
    Case,
    CaseAccessGrant,
    CaseStatus,
    DocumentVersion,
    StoredObject,
    StoredObjectCategory,
    UploadEvent,
    UploadFile,
    UploadSession,
    UploadSessionStatus,
)
from app.documents.retention import CaseGone, compute_expiry, load_case_for_read, load_case_for_write
from app.infra.object_store import ObjectStore

router = APIRouter(tags=["documents"])

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)]


def _store(request: Request) -> ObjectStore:
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        store = ObjectStore(request.app.state.settings)
        request.app.state.object_store = store
    return store


class CaseView(PublishedView):
    case_id: str
    case_name: str
    short_code: str
    owner: str
    case_version: int
    retention_expires_at: datetime
    last_material_event_at: datetime
    current_business_revision_id: str | None = None
    business_revision_no: int | None = None
    allowed_actions: list[str]


class CreateCaseRequest(BaseModel):
    name: str | None = Field(default=None, max_length=160)


def _case_view(case: Case, *, owner_name: str, is_owner: bool) -> CaseView:
    actions = ["upload", "start_run"]
    if is_owner:
        actions.append("delete_case")
    return CaseView(
        case_id=str(case.id),
        case_name=case.name,
        short_code=case.short_code,
        owner=owner_name,
        case_version=case.case_version,
        retention_expires_at=case.retention_expires_at,
        last_material_event_at=case.last_material_event_at,
        current_business_revision_id=str(case.current_business_revision_id) if case.current_business_revision_id else None,
        business_revision_no=case.business_revision_no,
        allowed_actions=actions,
    )


@router.post("/cases", status_code=status.HTTP_201_CREATED)
async def create_case(
    body: CreateCaseRequest,
    principal: WriteUser,
    db: DbSession,
    settings: SettingsDep,
    idempotency_key: IdempotencyKey,
) -> CaseView:
    """U01：建草稿 Case。owner 与 tenant 取自登录身份，不接受请求体传入。"""
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await idempotency.begin(
            db,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            scope="POST /cases",
            key=idempotency_key,
            payload=body.model_dump(),
        )
        if outcome.replayed:
            case = await db.get(Case, UUID(outcome.record.result_ref["case_id"]))
            assert case is not None
            return _case_view(case, owner_name=principal.display_name, is_owner=True)

        now = datetime.now(UTC)
        short_code = secrets.token_hex(3).upper()
        case = Case(
            tenant_id=principal.tenant_id,
            owner_user_id=principal.user_id,
            name=body.name or f"未命名单票 {short_code}",
            short_code=short_code,
            status=CaseStatus.ACTIVE,
            last_material_event_at=now,
            retention_expires_at=compute_expiry(now, settings),
        )
        db.add(case)
        await db.flush()
        # 首版只创建真实 owner 授权；查询入口只认显式授权
        db.add(
            CaseAccessGrant(
                tenant_id=principal.tenant_id,
                case_id=case.id,
                subject_user_id=principal.user_id,
                capabilities=["view", "upload", "start_run", "revise", "adjudicate", "export_confirm", "delete"],
            )
        )
        idempotency.complete(outcome.record, {"case_id": str(case.id)})
        return _case_view(case, owner_name=principal.display_name, is_owner=True)


@router.get("/cases/{case_id}")
async def get_case(case_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep) -> CaseView:
    async with db.begin():
        await set_tenant_context(db, principal)
        case = await load_case_for_read(db, case_id, user_id=principal.user_id, settings=settings)
        return _case_view(case, owner_name=principal.display_name, is_owner=case.owner_user_id == principal.user_id)


class FileSpecIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1, max_length=160)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    replace_document_id: UUID | None = None
    expected_document_version: int | None = None


class CreateUploadSessionRequest(BaseModel):
    files: list[FileSpecIn] = Field(min_length=1)
    disclosure_version_ack: str = Field(min_length=1, max_length=64)
    related_issue_id: UUID | None = None
    expected_issue_version: int | None = Field(default=None, ge=1)


class SourceVersionView(PublishedView):
    document_id: str
    document_version: int
    case_id: str
    filename: str
    mime: str
    size_bytes: int
    uploaded_by: str
    uploaded_at: datetime


class UploadFileView(PublishedView):
    upload_file_id: str
    filename: str
    mime: str
    size_bytes: int
    status: str
    confirmed_bytes: int
    error: str | None = None
    error_code: str | None = None
    put_url: str | None = None
    source_version: SourceVersionView | None = None
    duplicate_of: str | None = None


class UploadSessionView(PublishedView):
    upload_session_id: str
    case_id: str
    case_name: str
    owner: str
    created_at: datetime
    status: str
    files: list[UploadFileView]
    last_seq: int


def _file_view(
    slot: UploadFile, *, put_url: str | None = None, version: DocumentVersion | None = None, case_name: str = ""
) -> UploadFileView:
    source = None
    if version is not None:
        source = SourceVersionView(
            document_id=str(version.document_id),
            document_version=version.version,
            case_id=str(version.case_id),
            filename=version.filename,
            mime=version.mime,
            size_bytes=version.size_bytes,
            uploaded_by=str(version.uploaded_by),
            uploaded_at=version.created_at,
        )
    void = case_name
    del void
    return UploadFileView(
        upload_file_id=str(slot.id),
        filename=slot.filename,
        mime=slot.declared_mime,
        size_bytes=slot.declared_size_bytes,
        status=slot.status.value,
        confirmed_bytes=slot.confirmed_bytes,
        error=slot.error_message,
        error_code=slot.error_code,
        put_url=put_url,
        source_version=source,
        duplicate_of=str(slot.duplicate_of_document_id) if slot.duplicate_of_document_id else None,
    )


async def _session_view(
    db: DbSession, upload_session: UploadSession, *, case: Case, owner: str, put_urls: dict[UUID, str] | None = None
) -> UploadSessionView:
    slots = list(
        (
            await db.execute(
                select(UploadFile)
                .where(UploadFile.upload_session_id == upload_session.id)
                .order_by(UploadFile.created_at, UploadFile.id)
            )
        ).scalars()
    )
    views: list[UploadFileView] = []
    for slot in slots:
        version = None
        if slot.document_id and slot.document_version:
            version = (
                await db.execute(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == slot.document_id,
                        DocumentVersion.version == slot.document_version,
                    )
                )
            ).scalar_one_or_none()
        views.append(_file_view(slot, put_url=(put_urls or {}).get(slot.id), version=version))
    return UploadSessionView(
        upload_session_id=str(upload_session.id),
        case_id=str(case.id),
        case_name=case.name,
        owner=owner,
        created_at=upload_session.created_at,
        status=upload_session.status.value,
        files=views,
        last_seq=upload_session.next_event_seq,
    )


@router.post("/cases/{case_id}/upload-sessions", status_code=status.HTTP_201_CREATED)
async def create_upload_session(
    case_id: UUID,
    body: CreateUploadSessionRequest,
    principal: WriteUser,
    db: DbSession,
    settings: SettingsDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> UploadSessionView:
    """U02：核对部署政策版本后签发直传地址；不预先登记 DocumentVersion。"""
    store = _store(request)
    disclosure = build_disclosure(settings)
    if not disclosure.upload_allowed:
        raise InvalidRequest(disclosure.upload_blocked_reason or "当前不允许上传", code="UPLOAD_NOT_ALLOWED")

    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await idempotency.begin(
            db,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            scope=f"POST /cases/{case_id}/upload-sessions",
            key=idempotency_key,
            payload=body.model_dump(mode="json"),
        )
        case = await load_case_for_write(db, case_id, user_id=principal.user_id, settings=settings)
        if outcome.replayed:
            existing = await db.get(UploadSession, UUID(outcome.record.result_ref["upload_session_id"]))
            assert existing is not None
            # 重放不重新签发地址：旧签名可能仍有效，重复签发会留下多余临时对象
            return await _session_view(db, existing, case=case, owner=principal.display_name)

        supplement = None
        if body.related_issue_id:
            from app.review.models import Issue
            from app.review.commands import issue_view
            from app.api.errors import VersionConflict
            issue = await db.get(Issue, body.related_issue_id)
            if issue is None or issue.case_id != case.id:
                raise NotFoundOrForbidden()
            view = await issue_view(db, issue)
            if body.expected_issue_version != view["issue_version"]:
                raise VersionConflict("疑点状态已更新，请重新选择补充上传", current=view)
            supplement = {"issue_id": str(issue.id), "run_id": str(issue.run_id), "issue_version": view["issue_version"]}
        elif body.expected_issue_version is not None:
            raise InvalidRequest("补充上传版本须同时指定疑点")

        upload_session, _slots, put_urls = await upload_service.create_session(
            db,
            case=case,
            actor_id=principal.user_id,
            files=[
                upload_service.FileSpec(
                    filename=f.filename,
                    mime=f.mime,
                    size_bytes=f.size_bytes,
                    sha256=f.sha256,
                    replace_document_id=f.replace_document_id,
                    expected_document_version=f.expected_document_version,
                )
                for f in body.files
            ],
            disclosure_version_ack=body.disclosure_version_ack,
            expected_disclosure_version=disclosure.disclosure_version,
            store=store,
            settings=settings,
        )
        upload_session.disclosure_snapshot = disclosure.model_dump(mode="json")
        if supplement:
            upload_session.disclosure_snapshot = {**upload_session.disclosure_snapshot, "issue_supplement": supplement}
        idempotency.complete(outcome.record, {"upload_session_id": str(upload_session.id)})
        return await _session_view(db, upload_session, case=case, owner=principal.display_name, put_urls=put_urls)


class UploadSessionPage(PublishedView):
    items: list[UploadSessionView]
    next_cursor: str | None = None
    has_more: bool
    as_of: datetime


@router.get("/upload-sessions")
async def list_upload_sessions(
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
    status: Annotated[str | None, Query(pattern="^(all|drafts|in_progress|open|partial|completed|expired)$")] = None,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> UploadSessionPage:
    """U03：按本人可见 Case 列草稿，只统计有权项。"""
    async with db.begin():
        await set_tenant_context(db, principal)
        moment = datetime.now(UTC)
        as_of, anchor = moment, None
        scope = idempotency.request_digest({"tenant": principal.tenant_id, "user": principal.user_id, "status": status or "all"})
        if cursor:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor))
                if (not isinstance(value, dict) or set(value) != {"scope", "as_of", "last"}
                    or not isinstance(value["scope"], str) or not isinstance(value["as_of"], str)
                    or not isinstance(value["last"], list) or len(value["last"]) != 2
                    or not all(isinstance(item, str) for item in value["last"])):
                    raise ValueError
                as_of = datetime.fromisoformat(value["as_of"])
                anchor = (datetime.fromisoformat(value["last"][0]), UUID(value["last"][1]))
                if value["scope"] != scope or as_of.tzinfo is None or anchor[0].tzinfo is None or as_of > moment or anchor[0] > as_of:
                    raise ValueError
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                raise InvalidRequest("上传草稿游标与当前身份或筛选不一致") from exc
        query = select(UploadSession, Case).join(Case, Case.id == UploadSession.case_id).where(
            Case.status == CaseStatus.ACTIVE, Case.owner_user_id == principal.user_id,
            Case.retention_expires_at > moment, UploadSession.created_at <= as_of,
        )
        if status == "drafts":
            query = query.where(upload_service.draft_session_predicate())
        elif status == "in_progress":
            query = query.where(UploadSession.status.in_([UploadSessionStatus.OPEN, UploadSessionStatus.PARTIAL]))
        elif status and status != "all":
            query = query.where(UploadSession.status == UploadSessionStatus(status))
        visible, access = [], {}
        while len(visible) < limit + 1:
            page_query = query
            if anchor:
                page_query = page_query.where(tuple_(UploadSession.created_at, UploadSession.id) < anchor)
            rows = (await db.execute(page_query.order_by(UploadSession.created_at.desc(), UploadSession.id.desc()).limit(limit + 1))).all()
            if not rows:
                break
            for upload_session, case in rows:
                if case.id not in access:
                    try:
                        await load_case_for_read(db, case.id, user_id=principal.user_id, settings=settings)
                        access[case.id] = True
                    except (CaseGone, NotFoundOrForbidden):
                        access[case.id] = False
                if access[case.id]:
                    visible.append((upload_session, case))
                if len(visible) == limit + 1:
                    break
            anchor = (rows[-1][0].created_at, rows[-1][0].id)
            if len(rows) < limit + 1:
                break
        has_more = len(visible) > limit
        selected = visible[:limit]
        next_cursor = None
        if has_more:
            last = selected[-1][0]
            next_cursor = base64.urlsafe_b64encode(json.dumps({"scope": scope, "as_of": as_of.isoformat(), "last": [last.created_at.isoformat(), str(last.id)]}).encode()).decode()
        items = [await _session_view(db, s, case=c, owner=principal.display_name) for s, c in selected]
        return UploadSessionPage(items=items, next_cursor=next_cursor, has_more=has_more, as_of=as_of)


async def _load_session(db: DbSession, principal: CurrentUser, settings: Settings, session_id: UUID):
    upload_session = await db.get(UploadSession, session_id)
    if upload_session is None:
        raise NotFoundOrForbidden()
    case = await load_case_for_read(db, upload_session.case_id, user_id=principal.user_id, settings=settings)
    return upload_session, case


@router.get("/upload-sessions/{session_id}")
async def get_upload_session(
    session_id: UUID, principal: CurrentUser, db: DbSession, settings: SettingsDep
) -> UploadSessionView:
    async with db.begin():
        await set_tenant_context(db, principal)
        upload_session, case = await _load_session(db, principal, settings, session_id)
        return await _session_view(db, upload_session, case=case, owner=principal.display_name)


class UploadEventView(PublishedView):
    seq: int
    event_type: str
    upload_file_id: str | None = None
    status: str | None = None
    message: str | None = None
    occurred_at: datetime


@router.get("/upload-sessions/{session_id}/events")
async def list_upload_events(
    session_id: UUID,
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
) -> list[UploadEventView]:
    """
    U04：按 seq 补齐已持久化事件。

    只有已提交的数据库事件是进度真值；浏览器 XHR 的字节进度不写成持久事件。
    """
    async with db.begin():
        await set_tenant_context(db, principal)
        upload_session, _case = await _load_session(db, principal, settings, session_id)
        events = list(
            (
                await db.execute(
                    select(UploadEvent)
                    .where(UploadEvent.upload_session_id == upload_session.id, UploadEvent.seq > after_seq)
                    .order_by(UploadEvent.seq)
                )
            ).scalars()
        )
        return [
            UploadEventView(
                seq=e.seq,
                event_type=e.event_type,
                upload_file_id=str(e.upload_file_id) if e.upload_file_id else None,
                status=e.status,
                message=e.message,
                occurred_at=e.occurred_at,
            )
            for e in events
        ]


class CompleteUploadRequest(BaseModel):
    etag: str = Field(min_length=1, max_length=128)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class CompleteUploadResponse(PublishedView):
    file: UploadFileView
    source_version: SourceVersionView


async def _load_slot(db: DbSession, principal: CurrentUser, settings: Settings, file_id: UUID):
    slot = await db.get(UploadFile, file_id)
    if slot is None:
        raise NotFoundOrForbidden()
    upload_session = await db.scalar(
        select(UploadSession)
        .where(UploadSession.id == slot.upload_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if upload_session is None:
        raise NotFoundOrForbidden()
    case = await load_case_for_write(db, slot.case_id, user_id=principal.user_id, settings=settings)
    slot = await db.scalar(
        select(UploadFile)
        .where(UploadFile.id == file_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return slot, upload_session, case


@router.post("/upload-files/{file_id}/complete")
async def complete_upload(
    file_id: UUID,
    body: CompleteUploadRequest,
    principal: WriteUser,
    db: DbSession,
    settings: SettingsDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> CompleteUploadResponse:
    """U05：服务端复核字节、哈希与魔数后才创建 DocumentVersion。"""
    store = _store(request)
    content_failure = None
    completed = None
    async with db.begin():
        await set_tenant_context(db, principal)
        outcome = await idempotency.begin(
            db,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            scope=f"POST /upload-files/{file_id}/complete",
            key=idempotency_key,
            payload=body.model_dump(),
        )
        slot, upload_session, case = await _load_slot(db, principal, settings, file_id)

        if outcome.replayed:
            result_ref = outcome.record.result_ref or {}
            failure = result_ref.get("content_failure") or result_ref.get("archive_failure")
            if failure:
                content_failure = ApiError(
                    failure["message"], code=failure["code"], status_code=failure["status_code"]
                )
            # 同键重放只得到同一个 DocumentVersion，不会产生第二个版本
            else:
                version = (
                    await db.execute(
                        select(DocumentVersion).where(
                            DocumentVersion.document_id == slot.document_id,
                            DocumentVersion.version == slot.document_version,
                        )
                    )
                ).scalar_one()
        else:
            try:
                async with db.begin_nested():
                    version = await upload_service.complete_file(
                        db, slot=slot, upload_session=upload_session, case=case, etag=body.etag,
                        declared_sha256=body.sha256 or slot.declared_sha256, actor_id=principal.user_id,
                        store=store, settings=settings,
                    )
            except ApiError as error:
                content_codes = {
                    "BYTES_MISSING", "SIZE_MISMATCH", "FILE_TOO_LARGE", "HASH_MISMATCH",
                    "UNSUPPORTED_MEDIA_TYPE", "FILE_REJECTED", "TOO_MANY_PAGES",
                }
                archive_failure = isinstance(error, upload_service.UploadArchiveFailure)
                if error.code not in content_codes and not archive_failure:
                    raise
                # savepoint使已改ORM属性过期，重新取锁和权限后保存失败。
                slot, upload_session, case = await _load_slot(db, principal, settings, file_id)
                if archive_failure:
                    db.add(StoredObject(
                        tenant_id=case.tenant_id, case_id=case.id, object_key=error.object_key,
                        category=StoredObjectCategory.TEMP, is_visible=False, mime=slot.declared_mime,
                    ))
                    await upload_service.emit_event(
                        db, upload_session, event_type="file_status", upload_file_id=slot.id,
                        status=slot.status.value, message=error.message,
                    )
                else:
                    await upload_service.persist_content_failure(
                        db, slot=slot, upload_session=upload_session, code=error.code, message=error.message
                    )
                content_failure = error
                idempotency.complete(outcome.record, {
                    "archive_failure" if archive_failure else "content_failure": {
                        "code": error.code, "message": error.message, "status_code": error.status_code,
                    }
                })
            else:
                if case.current_scope_revision_id:
                    from app.consignment.service import ensure_scope
                    await db.flush()
                    await db.run_sync(lambda s: ensure_scope(s, case, principal.user_id))
                supplement = (upload_session.disclosure_snapshot or {}).get("issue_supplement")
                if supplement:
                    from app.workflows.models import Run
                    from app.workflows.commands import append_run_event
                    run = (await db.execute(select(Run).where(Run.id == UUID(supplement["run_id"]),
                        Run.case_id == case.id).with_for_update())).scalar_one_or_none()
                    if run is not None and run.case_id == case.id:
                        await append_run_event(db, run, kind="human", message="已为疑点补充材料，需结合新证据重新处理",
                            payload={"issue_id": supplement["issue_id"], "issue_version": supplement["issue_version"],
                                "upload_session_id": str(upload_session.id), "upload_file_id": str(slot.id),
                                "document_id": str(version.document_id), "document_version": version.version,
                                "actor_id": str(principal.user_id), "requires_new_run": True})
                idempotency.complete(outcome.record, {
                    "document_id": str(version.document_id), "document_version": version.version,
                })

        if content_failure is None:
            file_view = _file_view(slot, version=version)
            assert file_view.source_version is not None
            completed = CompleteUploadResponse(file=file_view, source_version=file_view.source_version)
    if content_failure is not None:
        raise content_failure
    return completed


@router.post("/upload-files/{file_id}/retry")
async def retry_upload(
    file_id: UUID,
    principal: WriteUser,
    db: DbSession,
    settings: SettingsDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> UploadFileView:
    store = _store(request)
    async with db.begin():
        await set_tenant_context(db, principal)
        slot, upload_session, case = await _load_slot(db, principal, settings, file_id)
        # 重试每次都要新的临时对象与签名，因此不按幂等键复用旧结果
        put_url = await upload_service.retry_file(
            db, slot=slot, upload_session=upload_session, case=case, store=store
        )
        del idempotency_key
        return _file_view(slot, put_url=put_url)


@router.post("/upload-files/{file_id}/cancel")
async def cancel_upload(
    file_id: UUID,
    principal: WriteUser,
    db: DbSession,
    settings: SettingsDep,
    idempotency_key: IdempotencyKey,
) -> UploadFileView:
    async with db.begin():
        await set_tenant_context(db, principal)
        slot, upload_session, _case = await _load_slot(db, principal, settings, file_id)
        await upload_service.cancel_file(db, slot=slot, upload_session=upload_session)
        del idempotency_key
        return _file_view(slot)


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """只支持单段 bytes 范围；非法范围由调用方返回 416。"""
    if not header or not header.startswith("bytes="):
        return None
    spec = header.removeprefix("bytes=").split(",")[0].strip()
    if "-" not in spec:
        raise ValueError("范围格式不正确")
    start_s, end_s = spec.split("-", 1)
    if not start_s:
        length = int(end_s)
        if length <= 0:
            raise ValueError("范围长度无效")
        start = max(0, size - length)
        return start, size - 1
    start = int(start_s)
    end = int(end_s) if end_s else size - 1
    if start > end or start >= size:
        raise ValueError("范围超出文件长度")
    return start, min(end, size - 1)


@router.get("/documents/{document_id}/versions/{version}/content")
async def read_document_content(
    document_id: UUID,
    version: int,
    principal: CurrentUser,
    db: DbSession,
    settings: SettingsDep,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    """
    U07：每个 GET/Range 请求都重新鉴权，由 API 代理对象字节。

    不发放可绕过撤权的对象存储直链。
    """
    store = _store(request)
    async with db.begin():
        await set_tenant_context(db, principal)
        doc_version = (
            await db.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document_id, DocumentVersion.version == version
                )
            )
        ).scalar_one_or_none()
        if doc_version is None:
            raise NotFoundOrForbidden()
        # 每次读取都走 Case 授权与期限门禁
        await load_case_for_read(db, doc_version.case_id, user_id=principal.user_id, settings=settings)
        obj = await db.get(StoredObject, doc_version.object_id)
        if obj is None or not obj.is_visible:
            raise NotFoundOrForbidden()
        key, size, mime = obj.object_key, doc_version.size_bytes, doc_version.mime
        # 文件名经过清洗，避免头注入
        safe_name = doc_version.filename.replace('"', "").replace("\r", "").replace("\n", "")

    common = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        # DOCX 不内联渲染，只作附件下载
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}"
            if mime.endswith("wordprocessingml.document")
            else f"inline; filename*=UTF-8''{quote(safe_name, safe='')}"
        ),
    }

    try:
        rng = _parse_range(range_header, size)
    except ValueError:
        return Response(status_code=416, headers={**common, "Content-Range": f"bytes */{size}"})

    start,end=rng if rng else (0,size-1)
    from fastapi.responses import StreamingResponse
    from app.infra.content_stream import prepared_bytes
    stream=await prepared_bytes(request,principal,settings,doc_version.case_id,store=store,key=key,start=start,end=end,expected_size=size)
    return StreamingResponse(stream,
        status_code=206 if rng else 200,media_type=mime,headers={**common,"Content-Length":str(end-start+1),**({"Content-Range":f"bytes {start}-{end}/{size}"} if rng else {})})
