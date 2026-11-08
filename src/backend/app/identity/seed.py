"""
本地演示账号种子。

方案 3.2 要求「首版单用户演示也应由真实账号和显式角色执行，不用系统自动确认」，
因此这里建的是真实账号与显式权限，只是口令为本地开发值。
两个账号属于**不同租户**，用于验证跨用户隔离。
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings
from app.identity.models import Tenant, User
from app.identity.service import hash_password
from app.infra.database import create_database_engine, create_session_factory

# 固定 ID 让种子可重复执行，且前端演示数据可预期
TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

# 权限矩阵：查看、发起、修订、裁定、导出确认、运营核查
FULL_PERMISSIONS = [
    "case_view",
    "case_create",
    "run_start",
    "review_revise",
    "review_adjudicate",
    "export_confirm",
    "ops_reconcile",
    "ops_health",
    "ops_migrate_model",
]
LIMITED_PERMISSIONS = ["case_view", "case_create", "run_start", "review_revise"]

DEMO_PASSWORD = "demo"


async def seed() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            for tenant_id, name in ((TENANT_A, "演示租户 A"), (TENANT_B, "演示租户 B")):
                if await session.get(Tenant, tenant_id) is None:
                    session.add(Tenant(id=tenant_id, name=name))
            await session.flush()

            accounts = [
                (TENANT_A, "demo", "陈晓", FULL_PERMISSIONS),
                (TENANT_B, "demo2", "李工", LIMITED_PERMISSIONS),
            ]
            for tenant_id, account, display, perms in accounts:
                existing = (
                    await session.execute(select(User).where(User.account == account))
                ).scalar_one_or_none()
                if existing is not None:
                    existing.tenant_id = tenant_id
                    existing.display_name = display
                    existing.permissions = perms
                    existing.password_hash = hash_password(DEMO_PASSWORD)
                    existing.is_active = True
                    continue
                session.add(
                    User(
                        tenant_id=tenant_id,
                        account=account,
                        display_name=display,
                        password_hash=hash_password(DEMO_PASSWORD),
                        permissions=perms,
                        is_active=True,
                    )
                )
    await engine.dispose()
    print(f"演示账号已就绪：demo / demo2，口令均为 {DEMO_PASSWORD}（仅本地开发）")


if __name__ == "__main__":
    asyncio.run(seed())
