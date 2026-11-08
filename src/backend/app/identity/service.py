"""
认证与会话服务。

设计要点（方案 3.2 节）：
- 登录核验发生在租户上下文建立之前，只用认证三表，不触碰业务表。
- 会话令牌与 CSRF 秘密都只存哈希，会话秘密只在 HttpOnly Cookie，CSRF 明文可在同源身份响应稳定恢复。
- 登录失败一律同一错误，不区分账号不存在与口令错误，避免账号枚举。
- 停用用户立即失效；会话过期或被撤销后不可用。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import AuthSession, User

SESSION_COOKIE = "lda_session"
SESSION_TTL = timedelta(hours=12)
_hasher = PasswordHasher()


@dataclass(frozen=True)
class Principal:
    """本次请求的认证主体。权限只作呈现提示，每个命令仍重新校验。"""

    user_id: UUID
    tenant_id: UUID
    display_name: str
    permissions: tuple[str, ...]
    session_id: UUID
    csrf_token: str


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def csrf_for_session(token: str) -> str:
    """从高熵 HttpOnly 会话秘密派生独立 CSRF 值，刷新后可稳定恢复。"""
    return hmac.new(token.encode(), b"lda/csrf/v1", hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: datetime


async def login(session: AsyncSession, account: str, password: str) -> tuple[Principal, IssuedSession]:
    """
    校验口令并签发会话。

    无论账号不存在、口令错误还是用户停用，都抛同一个错误，
    调用方据此返回统一提示。
    """
    from app.api.errors import Unauthenticated

    normalized = account.strip().lower()
    user = (await session.execute(select(User).where(
        or_(func.lower(User.account) == normalized, func.lower(User.email) == normalized)
    ))).scalar_one_or_none()
    # 账号不存在时也走一次哈希校验，避免用响应耗时区分账号是否存在
    stored = user.password_hash if user else _hasher.hash("placeholder-for-timing")
    try:
        _hasher.verify(stored, password)
        password_ok = True
    except VerifyMismatchError:
        password_ok = False
    if user is None or not password_ok or not user.is_active:
        raise Unauthenticated("账号或密码不正确")

    return await issue_session(session, user)


async def issue_session(session: AsyncSession, user: User) -> tuple[Principal, IssuedSession]:

    now = datetime.now(UTC)
    token = secrets.token_urlsafe(48)
    csrf_token = csrf_for_session(token)
    record = AuthSession(
        user_id=user.id,
        tenant_id=user.tenant_id,
        token_hash=_digest(token),
        csrf_secret_hash=_digest(csrf_token),
        expires_at=now + SESSION_TTL,
        last_seen_at=now,
    )
    session.add(record)
    await session.flush()

    principal = Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        display_name=user.display_name,
        permissions=tuple(user.permissions or ()),
        session_id=record.id,
        csrf_token=csrf_token,
    )
    return principal, IssuedSession(token=token, csrf_token=csrf_token, expires_at=record.expires_at)


async def resolve_session(session: AsyncSession, token: str | None) -> Principal | None:
    """
    从 Cookie 令牌解析主体。

    返回的 `csrf_token` 是哈希（明文由同源登录/身份响应恢复），
    因此 CSRF 校验比较的是「请求头明文的哈希」与库中摘要。
    """
    if not token:
        return None
    row = (
        await session.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == _digest(token))
        )
    ).first()
    if row is None:
        return None
    auth, user = row
    now = datetime.now(UTC)
    if not auth.is_usable(now) or not user.is_active:
        return None
    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        display_name=user.display_name,
        permissions=tuple(user.permissions or ()),
        session_id=auth.id,
        csrf_token=auth.csrf_secret_hash,
    )


def csrf_matches(principal: Principal, header_value: str | None) -> bool:
    """请求头带的是明文 CSRF token，库里存的是它的摘要。"""
    if not header_value:
        return False
    return secrets.compare_digest(_digest(header_value), principal.csrf_token)


async def logout(session: AsyncSession, session_id: UUID) -> None:
    auth = await session.get(AuthSession, session_id)
    if auth is not None and auth.revoked_at is None:
        auth.revoked_at = datetime.now(UTC)


def avatar_initial(display_name: str) -> str:
    return display_name.strip()[:1] if display_name.strip() else "?"
