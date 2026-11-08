"""
请求管线依赖（方案 5.1 节）。

每个路由的固定顺序：
1. 生成 trace_id（由中间件写入 request.state）
2. 从 HttpOnly Cookie 解析真实用户与租户；写操作校验 Origin 与 CSRF
3. 进入数据库事务并 `SET LOCAL app.tenant_id`
4. 按 Case 与动作检查权限、active 状态与 UTC 期限
5. 命令锁定幂等键与期望版本

路由只做 DTO 校验与响应映射，业务在服务层。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import Unauthenticated
from app.config import Settings, get_settings
from app.identity.service import Principal, SESSION_COOKIE, csrf_matches, resolve_session
from app.infra.database import TENANT_SETTING


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """
    请求级 AsyncSession；连接归还池前不残留会话级设置。

    依赖里的认证查询会隐式开启事务，若不回滚，路由再调 `session.begin()`
    会报「transaction is already begun」。因此认证查询后统一回滚，
    把干净的 Session 交给路由自己开显式事务。
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def optional_principal(request: Request, db: DbSession) -> Principal | None:
    """
    认证查询不需要租户上下文——它就是用来确定租户的。

    查完立即回滚，让路由拿到未开启事务的 Session。
    """
    try:
        return await resolve_session(db, request.cookies.get(SESSION_COOKIE))
    finally:
        await db.rollback()


async def current_principal(principal: Annotated[Principal | None, Depends(optional_principal)]) -> Principal:
    if principal is None:
        raise Unauthenticated()
    return principal


CurrentUser = Annotated[Principal, Depends(current_principal)]


async def write_principal(
    request: Request,
    principal: CurrentUser,
    settings: SettingsDep,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Principal:
    """
    写请求额外校验 Origin 与 CSRF。

    浏览器直传对象存储用的是限对象、限大小、短有效期的上传签名，
    不走这条管线，也不持有任何管理凭据。
    """
    origin = request.headers.get("origin")
    if origin and settings.app_public_origin and origin.rstrip("/") != settings.app_public_origin.rstrip("/"):
        raise Unauthenticated("请求来源不被信任，请重新登录后重试")
    if not csrf_matches(principal, x_csrf_token):
        raise Unauthenticated("CSRF 校验未通过，请刷新页面后重试")
    return principal


WriteUser = Annotated[Principal, Depends(write_principal)]


async def set_tenant_context(db: AsyncSession, principal: Principal) -> None:
    """
    在当前事务内设置租户上下文。

    读接口同样要调用——「只是读取」不能跳过 RLS。
    RLS 策略在未设置该变量时失败关闭，因此漏调用会得到空结果而不是越权数据。

    `SET LOCAL` 是工具性语句，不支持参数绑定，只能内联值。
    这里先用 `UUID()` 强制解析，确保拼进 SQL 的一定是规范 UUID 字面量。
    """
    tenant_id = UUID(str(principal.tenant_id))
    await db.execute(text("SET LOCAL ROLE logistics_app"))
    await db.execute(text(f"SET LOCAL {TENANT_SETTING} = '{tenant_id}'"))
