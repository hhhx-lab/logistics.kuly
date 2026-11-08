"""
上传接收服务（U01–U06）。

状态流转：`pending → uploading → received → verified/failed/cancelled`。

关键不变量：
- 浏览器 XHR 的 100% 只是本连接传输进度；`confirmed_bytes` 只在服务端
  完整校验通过后才等于文件大小。
- 只有 U05 校验成功才创建 DocumentVersion；替换只新增同 Document 的版本。
- 部分失败互不吞并；取消只停当前槽，绝不删除已验收原件。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError, FileRejected, InvalidRequest, NotFoundOrForbidden, UnsupportedMedia, VersionConflict
from app.config import Settings
from app.documents.models import (
    Case,
    DocumentVersion,
    SourceDocument,
    StoredObject,
    StoredObjectCategory,
    UploadEvent,
    UploadFile,
    UploadFileStatus,
    UploadSession,
    UploadSessionStatus,
)
from app.documents.retention import touch_material_event
from app.infra.object_store import ACCEPTED_MIME, ObjectStore, sniff_mime


@dataclass(frozen=True)
class FileSpec:
    filename: str
    mime: str
    size_bytes: int
    sha256: str | None = None
    replace_document_id: UUID | None = None
    expected_document_version: int | None = None


class UploadArchiveFailure(ApiError):
    code = "COPY_VERIFY_FAILED"
    status_code = 503
    message = "原件归档暂未完成，请重试；尚未创建原件版本"

    def __init__(self, object_key):
        super().__init__()
        # 内部待清对象，不进入错误响应或幂等公开引用。
        self.object_key = object_key


async def _next_event_seq(session: AsyncSession, upload_session: UploadSession) -> int:
    """事件序号与状态在同一事务内分配，保证断线后可按 seq 精确补齐。"""
    upload_session.next_event_seq += 1
    return upload_session.next_event_seq


async def emit_event(
    session: AsyncSession,
    upload_session: UploadSession,
    *,
    event_type: str,
    upload_file_id: UUID | None = None,
    status: str | None = None,
    message: str | None = None,
) -> UploadEvent:
    event = UploadEvent(
        tenant_id=upload_session.tenant_id,
        upload_session_id=upload_session.id,
        seq=await _next_event_seq(session, upload_session),
        event_type=event_type,
        upload_file_id=upload_file_id,
        status=status,
        message=message,
    )
    session.add(event)
    return event


async def create_session(
    session: AsyncSession,
    *,
    case: Case,
    actor_id: UUID,
    files: list[FileSpec],
    disclosure_version_ack: str,
    expected_disclosure_version: str,
    store: ObjectStore,
    settings: Settings,
) -> tuple[UploadSession, list[UploadFile], dict[UUID, str]]:
    """
    U02：验证告知版本与逐文件限制，为每份文件建临时对象键与短期签名。

    某个文件被拒不影响其他文件；不预先登记 DocumentVersion。
    """
    if not files:
        raise InvalidRequest("请至少选择一个文件")
    if len(files) > settings.run_max_files:
        raise FileRejected(f"一次最多上传 {settings.run_max_files} 个文件")
    # 服务配置改变后旧告知版本不能签发新上传
    if disclosure_version_ack != expected_disclosure_version:
        raise VersionConflict(
            "上传配置已更新，请重试上传",
            code="DISCLOSURE_VERSION_STALE",
            current={"disclosure_version": expected_disclosure_version},
        )

    now = datetime.now(UTC)
    upload_session = UploadSession(
        tenant_id=case.tenant_id,
        case_id=case.id,
        created_by=actor_id,
        disclosure_version_ack=disclosure_version_ack,
        disclosure_acked_at=now,
        status=UploadSessionStatus.OPEN,
    )
    session.add(upload_session)
    await session.flush()

    slots: list[UploadFile] = []
    put_urls: dict[UUID, str] = {}
    for spec in files:
        slot = UploadFile(
            tenant_id=case.tenant_id,
            upload_session_id=upload_session.id,
            case_id=case.id,
            filename=spec.filename[:255],
            declared_mime=spec.mime,
            declared_size_bytes=spec.size_bytes,
            declared_sha256=spec.sha256,
            replace_document_id=spec.replace_document_id,
            expected_document_version=spec.expected_document_version,
        )

        reject: tuple[str, str] | None = None
        if spec.mime not in ACCEPTED_MIME:
            reject = ("UNSUPPORTED_MEDIA_TYPE", f"不支持此格式，当前支持 {', '.join(ACCEPTED_MIME)}")
        elif spec.size_bytes <= 0:
            reject = ("EMPTY_FILE", "文件内容为空")
        elif spec.size_bytes > settings.upload_max_bytes:
            reject = ("FILE_TOO_LARGE", f"超过单文件上限 {settings.upload_max_bytes} 字节")
        elif spec.replace_document_id is not None:
            # 替换目标必须确属该 Case，且所见版本是当前版
            target = (
                await session.execute(
                    select(SourceDocument).where(
                        SourceDocument.id == spec.replace_document_id,
                        SourceDocument.case_id == case.id,
                    )
                )
            ).scalar_one_or_none()
            if target is None:
                reject = ("REPLACE_TARGET_NOT_FOUND", "要替换的原件不属于这一票")
            elif spec.expected_document_version != target.current_version:
                reject = ("REPLACE_VERSION_STALE", f"该原件当前版本为 v{target.current_version}，请刷新后重试")

        if reject is not None:
            slot.status = UploadFileStatus.FAILED
            slot.error_code, slot.error_message = reject
            session.add(slot)
            await session.flush()
            await emit_event(
                session, upload_session, event_type="file_status",
                upload_file_id=slot.id, status=slot.status.value, message=slot.error_message,
            )
        else:
            obj = StoredObject(
                tenant_id=case.tenant_id,
                case_id=case.id,
                object_key=ObjectStore.build_key(
                    case.tenant_id, case.id, StoredObjectCategory.TEMP, session_id=upload_session.id
                ),
                category=StoredObjectCategory.TEMP,
                mime=spec.mime,
                size_bytes=spec.size_bytes,
                is_visible=False,
            )
            session.add(obj)
            await session.flush()
            slot.temp_object_id = obj.id
            session.add(slot)
            await session.flush()
            put_urls[slot.id] = store.presign_put(obj.object_key, content_length=spec.size_bytes)
            await emit_event(
                session, upload_session, event_type="file_status",
                upload_file_id=slot.id, status=slot.status.value, message=f"{slot.filename} 等待上传",
            )
        slots.append(slot)

    await _refresh_session_status(session, upload_session, slots)
    return upload_session, slots, put_urls


async def _refresh_session_status(
    session: AsyncSession, upload_session: UploadSession, slots: list[UploadFile]
) -> None:
    terminal = {UploadFileStatus.VERIFIED, UploadFileStatus.FAILED, UploadFileStatus.CANCELLED}
    if not all(s.status in terminal for s in slots):
        upload_session.status = UploadSessionStatus.OPEN
        return
    verified = [s for s in slots if s.status is UploadFileStatus.VERIFIED]
    if verified and len(verified) == len(slots):
        upload_session.status = UploadSessionStatus.COMPLETED
    else:
        upload_session.status = UploadSessionStatus.PARTIAL


async def persist_content_failure(session, *, slot, upload_session, code, message):
    """验收savepoint回滚后只保存内容失败，不登记原件或暴露临时字节。"""
    slot.status = UploadFileStatus.FAILED
    slot.error_code, slot.error_message = code, message
    slot.confirmed_bytes = 0
    await emit_event(session,upload_session,event_type="file_status",upload_file_id=slot.id,status="failed",message=message)
    siblings=list((await session.execute(select(UploadFile).where(UploadFile.upload_session_id==upload_session.id))).scalars())
    await _refresh_session_status(session,upload_session,siblings)
    await emit_event(session,upload_session,event_type="session_status",status=upload_session.status.value,message="上传会话状态已更新")


def _validate_bytes(head: bytes, declared_mime: str, full: bytes | None) -> None:
    """
    不信任浏览器报告的后缀与 Content-Type，按魔数判定真实格式。
    DOCX 还要能作为 zip 打开并含 word/document.xml，防止改名的任意 zip。
    """
    actual = sniff_mime(head)
    if actual is None:
        raise UnsupportedMedia("无法识别文件真实格式，请确认文件未损坏")
    if actual != declared_mime:
        # zip 容器只能判到 PK 头，DOCX 的声明与判定一致才放行
        raise UnsupportedMedia(f"文件真实格式与声明不符（声明 {declared_mime}，实际 {actual}）")
    if declared_mime.endswith("wordprocessingml.document") and full is not None:
        try:
            with zipfile.ZipFile(BytesIO(full)) as zf:
                names = zf.namelist()
                if "word/document.xml" not in names:
                    raise UnsupportedMedia("这不是有效的 DOCX 文件")
                # 压缩包展开大小上限，避免 zip bomb
                if sum(i.file_size for i in zf.infolist()) > 400 * 1024 * 1024:
                    raise FileRejected("DOCX 解压后体积过大")
        except zipfile.BadZipFile as exc:
            raise UnsupportedMedia("DOCX 文件已损坏") from exc


async def complete_file(
    session: AsyncSession,
    *,
    slot: UploadFile,
    upload_session: UploadSession,
    case: Case,
    etag: str,
    declared_sha256: str | None,
    actor_id: UUID,
    store: ObjectStore,
    settings: Settings,
) -> DocumentVersion:
    """
    U05：服务端复核后才创建 DocumentVersion。

    校验顺序：对象存在 → 实际字节数 → SHA-256 → 魔数与结构 → 页数上限。
    任一不符都不登记原件，上传槽保留供用户重选。
    """
    if slot.status is UploadFileStatus.VERIFIED:
        raise VersionConflict("这个文件已经验收过了", code="UPLOAD_ALREADY_VERIFIED")
    if slot.status in {UploadFileStatus.CANCELLED, UploadFileStatus.FAILED}:
        raise VersionConflict(
            "这个上传槽已取消或失败，请先重试再验收",
            code="SLOT_NOT_UPLOADABLE",
            current={"status": slot.status.value},
        )
    if not etag:
        raise InvalidRequest("缺少直传结果标识")

    temp_obj = await session.get(StoredObject, slot.temp_object_id) if slot.temp_object_id else None
    if temp_obj is None:
        raise NotFoundOrForbidden()

    document = None
    if slot.replace_document_id is not None:
        document = await session.scalar(select(SourceDocument).where(
            SourceDocument.id==slot.replace_document_id,SourceDocument.case_id==case.id).with_for_update())
        if document is None:raise NotFoundOrForbidden()
        if slot.expected_document_version != document.current_version:
            raise VersionConflict("该原件已有更新版本，请刷新后重试",code="REPLACE_VERSION_STALE",
                current={"document_version":document.current_version})

    async def fail(code: str, message: str) -> None:
        slot.status = UploadFileStatus.FAILED
        slot.error_code, slot.error_message = code, message
        slot.confirmed_bytes = 0
        await emit_event(
            session, upload_session, event_type="file_status",
            upload_file_id=slot.id, status=slot.status.value, message=message,
        )

    head = store.head(temp_obj.object_key)
    if head is None:
        await fail("BYTES_MISSING", "服务端没有收到这份文件的内容，请重新上传")
        raise InvalidRequest("服务端没有收到这份文件的内容，请重新上传", code="BYTES_MISSING")

    if head.size_bytes != slot.declared_size_bytes:
        await fail("SIZE_MISMATCH", f"实际字节数 {head.size_bytes} 与声明的 {slot.declared_size_bytes} 不一致")
        raise InvalidRequest("文件实际大小与声明不一致", code="SIZE_MISMATCH")
    if head.size_bytes > settings.upload_max_bytes:
        await fail("FILE_TOO_LARGE", f"超过单文件上限 {settings.upload_max_bytes} 字节")
        raise FileRejected()

    # 小文件整体读入做结构校验；大文件只取头部魔数，哈希走流式
    content: bytes | None = None
    if head.size_bytes <= 32 * 1024 * 1024:
        content = store.read_all(temp_obj.object_key)
        actual_sha, actual_size = __import__("hashlib").sha256(content).hexdigest(), len(content)
        head_bytes = content[:16]
    else:
        actual_sha, actual_size = store.sha256_of(temp_obj.object_key)
        head_bytes = store.read_range(temp_obj.object_key, 0, 15)

    if actual_size != slot.declared_size_bytes:
        await fail("SIZE_MISMATCH", "实际字节数与声明不一致")
        raise InvalidRequest("文件实际大小与声明不一致", code="SIZE_MISMATCH")
    # 前端若已算出哈希则必须一致；未提供时以服务端计算值为准
    if declared_sha256 and declared_sha256.lower() != actual_sha:
        await fail("HASH_MISMATCH", "文件哈希与声明不一致，可能在传输中损坏")
        raise InvalidRequest("文件哈希与声明不一致", code="HASH_MISMATCH")

    try:
        _validate_bytes(head_bytes, slot.declared_mime, content)
    except (UnsupportedMedia, FileRejected) as exc:
        await fail(exc.code, exc.message)
        raise

    page_count = _count_pages(slot.declared_mime, content)
    if page_count is not None and page_count > settings.upload_max_pages:
        await fail("TOO_MANY_PAGES", f"页数 {page_count} 超过上限 {settings.upload_max_pages}")
        raise FileRejected(f"页数超过上限 {settings.upload_max_pages}")

    slot.status = UploadFileStatus.RECEIVED
    await emit_event(
        session, upload_session, event_type="file_status",
        upload_file_id=slot.id, status=slot.status.value, message=f"{slot.filename} 已接收，正在校验",
    )

    # 同一用户范围内的重复文件只作提示，不跨用户去重
    duplicate = (
        await session.execute(
            select(DocumentVersion.document_id)
            .where(DocumentVersion.case_id == case.id, DocumentVersion.sha256 == actual_sha)
            .limit(1)
        )
    ).scalar_one_or_none()
    slot.duplicate_of_document_id = duplicate

    # 复制到不可覆盖的 source 键；旧对象不改
    source_key = ObjectStore.build_key(case.tenant_id, case.id, StoredObjectCategory.SOURCE)
    try:
        store.copy(temp_obj.object_key, source_key)
        copied = store.head(source_key)
        if copied is None or copied.size_bytes != actual_size:
            raise UploadArchiveFailure(source_key)
    except UploadArchiveFailure:
        raise
    except Exception as error:
        raise UploadArchiveFailure(source_key) from error

    source_obj = StoredObject(
        tenant_id=case.tenant_id,
        case_id=case.id,
        object_key=source_key,
        category=StoredObjectCategory.SOURCE,
        size_bytes=actual_size,
        sha256=actual_sha,
        mime=slot.declared_mime,
        is_visible=True,
    )
    session.add(source_obj)
    await session.flush()

    if document is None:
        # 补件是新的 SourceDocument
        document = SourceDocument(tenant_id=case.tenant_id, case_id=case.id, current_version=0)
        session.add(document)
        await session.flush()

    next_version = document.current_version + 1
    version = DocumentVersion(
        tenant_id=case.tenant_id,
        case_id=case.id,
        document_id=document.id,
        version=next_version,
        filename=slot.filename,
        mime=slot.declared_mime,
        size_bytes=actual_size,
        sha256=actual_sha,
        page_count=page_count,
        object_id=source_obj.id,
        uploaded_by=actor_id,
    )
    session.add(version)
    document.current_version = next_version

    slot.status = UploadFileStatus.VERIFIED
    slot.confirmed_bytes = actual_size
    slot.document_id = document.id
    slot.document_version = next_version
    slot.error_code = slot.error_message = None

    # 成功验收属于实质性业务变更，刷新 30 天期限
    touch_material_event(case, settings)

    await emit_event(
        session, upload_session, event_type="file_status",
        upload_file_id=slot.id, status=slot.status.value, message=f"{slot.filename} 已验收",
    )
    siblings = list(
        (
            await session.execute(select(UploadFile).where(UploadFile.upload_session_id == upload_session.id))
        ).scalars()
    )
    await _refresh_session_status(session, upload_session, siblings)
    await emit_event(
        session, upload_session, event_type="session_status",
        status=upload_session.status.value, message="上传会话状态已更新",
    )
    return version


def _count_pages(mime: str, content: bytes | None) -> int | None:
    """只有能可靠计页的格式才返回页数；DOCX 不在上传阶段计页。"""
    if content is None or mime != "application/pdf":
        return None
    try:
        import pypdfium2 as pdfium

        return len(pdfium.PdfDocument(BytesIO(content)))
    except Exception:
        # 页数读取失败不阻断上传，留给解析阶段判断可读性
        return None


async def retry_file(
    session: AsyncSession,
    *,
    slot: UploadFile,
    upload_session: UploadSession,
    case: Case,
    store: ObjectStore,
) -> str:
    """U06 retry：只为失败或未确认的槽签发新临时对象键，旧对象隔离待清。"""
    if slot.status is UploadFileStatus.VERIFIED:
        raise VersionConflict("这个文件已经验收，无需重试", code="UPLOAD_ALREADY_VERIFIED")

    obj = StoredObject(
        tenant_id=case.tenant_id,
        case_id=case.id,
        object_key=ObjectStore.build_key(
            case.tenant_id, case.id, StoredObjectCategory.TEMP, session_id=upload_session.id
        ),
        category=StoredObjectCategory.TEMP,
        mime=slot.declared_mime,
        size_bytes=slot.declared_size_bytes,
        is_visible=False,
    )
    session.add(obj)
    await session.flush()

    slot.temp_object_id = obj.id
    slot.status = UploadFileStatus.PENDING
    slot.confirmed_bytes = 0
    slot.error_code = slot.error_message = None
    upload_session.status = UploadSessionStatus.OPEN
    await emit_event(
        session, upload_session, event_type="file_status",
        upload_file_id=slot.id, status=slot.status.value, message=f"{slot.filename} 可重新上传",
    )
    return store.presign_put(obj.object_key, content_length=slot.declared_size_bytes)


async def cancel_file(
    session: AsyncSession, *, slot: UploadFile, upload_session: UploadSession
) -> None:
    """U06 cancel：只取消尚未完成的传输，不删除已接收原件也不影响 Run。"""
    if slot.status is UploadFileStatus.VERIFIED:
        raise VersionConflict(
            "这个文件已经验收，取消上传不会删除已接收的原件",
            code="UPLOAD_ALREADY_VERIFIED",
        )
    slot.status = UploadFileStatus.CANCELLED
    slot.confirmed_bytes = 0
    await emit_event(
        session, upload_session, event_type="file_status",
        upload_file_id=slot.id, status=slot.status.value, message=f"{slot.filename} 已取消上传",
    )
    siblings = list(
        (
            await session.execute(select(UploadFile).where(UploadFile.upload_session_id == upload_session.id))
        ).scalars()
    )
    await _refresh_session_status(session, upload_session, siblings)


async def verified_count(session: AsyncSession, upload_session_id: UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(UploadFile)
            .where(
                UploadFile.upload_session_id == upload_session_id,
                UploadFile.status == UploadFileStatus.VERIFIED,
            )
        )
    ).scalar_one()


def draft_session_predicate():
    from sqlalchemy import and_, or_, select
    from app.workflows.models import Run
    has_run = select(Run.id).where(Run.case_id == UploadSession.case_id).exists()
    return or_(
        UploadSession.status.in_([UploadSessionStatus.OPEN, UploadSessionStatus.PARTIAL]),
        and_(~has_run, UploadSession.status.in_([UploadSessionStatus.COMPLETED, UploadSessionStatus.EXPIRED])),
    )
