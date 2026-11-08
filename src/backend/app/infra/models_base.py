"""
所有业务表的共同约定。

- 不可预测 UUID 主键（uuid4），对外 ID 不构成权限凭据。
- `tenant_id` 必填，业务查询再检查 owner 或 Case 授权。
- 时间统一 timestamptz 存 UTC，响应时才按用户时区格式化。
- 金额与精确数量用 numeric/Decimal，单号类一律字符串保留原始写法。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 统一命名约定，让 Alembic 生成的约束名可预测、可在迁移里引用
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def tenant_column() -> Mapped[uuid.UUID]:
    """每张业务表都带 tenant_id，RLS 策略以它为条件。"""
    return mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TenantMixin:
    """业务表混入：主键、租户与创建时间。"""

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_column()
    created_at: Mapped[datetime] = created_at_column()
