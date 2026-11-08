"""
API 应用装配。

统一 `/api/v1` 前缀、trace_id、异常映射与私有缓存头。
`/api/health/live` 只表明进程存活，不代表数据库、解析或模型就绪；
真正的可用性由 `/api/v1/health/ready` 与 `/api/v1/workflows` 给出。
"""

from __future__ import annotations

import uuid
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.api.errors import ApiError, api_error_handler, unhandled_error_handler, validation_error_handler
from app.api.health import router as health_router
from app.api.routers.auth import router as auth_router
from app.api.routers.uploads import router as uploads_router
from app.api.routers.events import router as events_router
from app.api.routers.review import router as review_router
from app.api.routers.document_catalog import router as document_catalog_router
from app.api.routers.workflows import router as workflows_router
from app.api.routers.conversation import router as conversation_router
from app.api.routers.review_commands import router as review_commands_router
from app.api.routers.preferences import router as preferences_router
from app.api.routers.extensions import router as extensions_router
from app.api.routers.exports import router as exports_router
from app.api.routers.ops import router as ops_router
from app.api.routers.observability import router as observability_router
from app.api.routers.management import router as management_router
from app.api.routers.consignment import router as consignment_router
from app.config import get_settings
from app.infra.database import create_database_engine, create_session_factory

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.deletion_ledger_path and settings.deletion_ledger_key:
        from app.management.ledger import guard_restored_database
        await asyncio.to_thread(guard_restored_database, settings)
    engine = create_database_engine(settings)
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    from app.infra.outbox import publisher_loop
    stop = asyncio.Event()
    publisher = asyncio.create_task(publisher_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await publisher
        store=getattr(application.state,"object_store",None)
        if store is not None:
            store.close()
            application.state.object_store=None
        await engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(
        title="物流单证智能体",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs",
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.trace_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.trace_id
        # 私有查询与字节流一律不缓存，避免撤权后浏览器回放旧正文
        response.headers.setdefault("Cache-Control", "private, no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    application.add_exception_handler(ApiError, api_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)

    # liveness 留在 /api 下，与业务前缀区分开
    application.include_router(health_router, prefix="/api")
    application.include_router(auth_router, prefix=API_PREFIX)
    application.include_router(uploads_router, prefix=API_PREFIX)
    application.include_router(events_router, prefix=API_PREFIX)
    application.include_router(review_router, prefix=API_PREFIX)
    application.include_router(document_catalog_router, prefix=API_PREFIX)
    application.include_router(management_router, prefix=API_PREFIX)
    application.include_router(consignment_router, prefix=API_PREFIX)
    application.include_router(workflows_router, prefix=API_PREFIX)
    application.include_router(conversation_router, prefix=API_PREFIX)
    application.include_router(review_commands_router, prefix=API_PREFIX)
    application.include_router(preferences_router, prefix=API_PREFIX)
    application.include_router(extensions_router, prefix=API_PREFIX)
    application.include_router(exports_router, prefix=API_PREFIX)
    application.include_router(ops_router, prefix=API_PREFIX)
    application.include_router(observability_router, prefix=API_PREFIX)
    return application


app = create_app()
