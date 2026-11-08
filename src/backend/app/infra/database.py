"""
数据库连接与租户隔离上下文。

API 用请求级 AsyncSession，Celery Worker 用独立同步 Session，两者不共享连接池。
每个业务事务开始后必须先 `SET LOCAL app.tenant_id`，RLS 策略在未设置上下文时失败关闭；
`SET LOCAL` 随事务结束自动失效，连接归还池后不残留会话级设置。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

TENANT_SETTING = "app.tenant_id"


def create_database_engine(settings: Settings) -> AsyncEngine:
    if not settings.database_url:
        raise ValueError("DATABASE_URL 尚未配置")
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("DATABASE_URL 必须使用 postgresql+asyncpg 协议")
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def sync_database_url(settings: Settings) -> str:
    """Worker 与 Alembic 用同步驱动；psycopg 与 asyncpg 的连接参数不能互传。"""
    if not settings.database_url:
        raise ValueError("DATABASE_URL 尚未配置")
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


def create_sync_engine(settings: Settings) -> Engine:
    """Celery prefork 子进程启动后各自调用，不继承父进程的连接池。"""
    return create_engine(sync_database_url(settings), pool_pre_ping=True)


def create_sync_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_transaction(session: AsyncSession, tenant_id: UUID) -> AsyncIterator[AsyncSession]:
    """
    在显式事务内先设置租户上下文，再执行业务查询。

    读接口同样要建立上下文——「只是读取」不能跳过 RLS 与权限检查。
    """
    async with session.begin():
        # SET LOCAL 不支持参数绑定；UUID() 保证拼入的是规范字面量
        await session.execute(text(f"SET LOCAL {TENANT_SETTING} = '{UUID(str(tenant_id))}'"))
        yield session


@contextmanager
def sync_tenant_transaction(session: Session, tenant_id: UUID) -> Iterator[Session]:
    """Worker 侧的同等上下文。租户 ID 由受限解析函数从 Run ID 反查，不取自队列消息。"""
    with session.begin():
        session.execute(text(f"SET LOCAL {TENANT_SETTING} = '{UUID(str(tenant_id))}'"))
        yield session
