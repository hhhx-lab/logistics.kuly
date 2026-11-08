"""
Case 30 天留存与读写门禁（方案 4.4 节）。

`retention_expires_at = last_material_event_at + 30 天` 是唯一口径，全部存 UTC。

实质性业务变更才刷新期限：成功原件验收／替换、TaskDraft 提交或 Run 发起、
人工修订／处置／确认、成功 ExportSnapshot 登记。
普通查询、下载、对话问答、Worker 阶段事件与失败重试**都不刷新**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError, NotFoundOrForbidden
from app.config import Settings
from app.documents.models import Case, CaseAccessGrant, CaseStatus


class CaseGone(ApiError):
    """到期或正在删除的票据一律不可读写，即使清理扫描还没跑到。"""

    status_code = 410
    code = "CASE_GONE"
    message = "这一票已过保留期或正在删除，无法再访问"


def compute_expiry(last_material_event_at: datetime, settings: Settings) -> datetime:
    return last_material_event_at + timedelta(days=settings.case_retention_days)


def touch_material_event(case: Case, settings: Settings, *, now: datetime | None = None) -> None:
    """
    实质性业务变更：同一锁 Case 的事务内更新期限与 case_version。

    调用方必须已经持有该 Case 的行锁。
    """
    moment = now or datetime.now(UTC)
    case.last_material_event_at = moment
    case.retention_expires_at = compute_expiry(moment, settings)
    case.case_version += 1


async def load_case_for_read(
    session: AsyncSession,
    case_id: UUID,
    *,
    user_id: UUID,
    settings: Settings,
    now: datetime | None = None,
) -> Case:
    """
    读取路径的统一门禁：授权 → 状态 → UTC 期限。

    RLS 已挡住跨租户，这里再检查 owner 或显式授权；
    无权与不存在合并为同一错误，不泄露对象存在性。
    """
    case = await session.get(Case, case_id)
    if case is None:
        raise NotFoundOrForbidden()

    if case.owner_user_id != user_id:
        grant = (
            await session.execute(
                select(CaseAccessGrant).where(
                    CaseAccessGrant.case_id == case_id,
                    CaseAccessGrant.subject_user_id == user_id,
                ).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if grant is None or not any(capability in grant.capabilities for capability in ("read","view","write")):
            raise NotFoundOrForbidden()
        moment = now or datetime.now(UTC)
        if grant.expires_at is not None and grant.expires_at <= moment:
            raise NotFoundOrForbidden()

    if settings.deletion_ledger_path or settings.deletion_ledger_key:
        from app.management.ledger import DeletionLedger, LedgerUnavailable
        from app.api.errors import ServiceUnavailable
        try:
            denied = DeletionLedger(settings).is_denied(case.tenant_id, case.id)
        except (LedgerUnavailable, OSError) as exc:
            raise ServiceUnavailable("删除账本校验暂不可用，单票访问已暂停") from exc
        if denied:
            raise CaseGone()

    moment = now or datetime.now(UTC)
    if case.status is CaseStatus.DELETING:
        raise CaseGone()
    # 定时扫描延迟不构成继续提供原文的理由
    if case.retention_expires_at <= moment:
        raise CaseGone()
    return case


async def load_case_for_write(
    session: AsyncSession,
    case_id: UUID,
    *,
    user_id: UUID,
    settings: Settings,
    now: datetime | None = None,
) -> Case:
    """
    写入路径：先按读门禁校验，再取行锁避免与删除、并发写竞态。
    """
    case = await load_case_for_read(session, case_id, user_id=user_id, settings=settings, now=now)
    locked = (
        await session.execute(select(Case).where(Case.id == case.id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one()
    # 等锁期间权限、期限及独立删除账本都可能变化；用已刷新实体重跑完整门禁。
    await load_case_for_read(session,locked.id,user_id=user_id,settings=settings,now=now)
    if locked.owner_user_id!=user_id:
        grant=(await session.execute(select(CaseAccessGrant).where(CaseAccessGrant.case_id==locked.id,
            CaseAccessGrant.subject_user_id==user_id).execution_options(populate_existing=True))).scalar_one_or_none()
        if grant is None or "write" not in grant.capabilities:
            raise NotFoundOrForbidden()
    return locked
