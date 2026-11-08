"""
身份与会话表。

登录与会话核验发生在租户上下文建立之前，因此这三张表不启用 RLS，
由窄权限认证查询访问；它们只返回主体 ID、租户 ID 与会话状态，
不开放任何业务表的无租户查询。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models_base import Base, created_at_column, uuid_pk


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # 账号在租户内唯一；登录失败不区分「用户不存在」与「密码错误」，避免账号枚举
        UniqueConstraint("tenant_id", "account", name="uq_users_tenant_id_account"),
        Index("uq_users_account_lower", text("lower(account)"), unique=True),
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Argon2id 哈希，明文口令不入库也不进日志
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 权限矩阵至少分查看、发起、修订、裁定、导出确认与运营核查
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_column()


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_token_hash", "token_hash", unique=True),
        Index("ix_auth_sessions_user_id_expires_at", "user_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # 只存随机令牌的哈希，明文只在 Cookie 中；CSRF 秘密同样只存摘要
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def is_usable(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now
