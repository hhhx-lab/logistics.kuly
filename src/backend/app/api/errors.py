"""
统一错误封套（契约第 2 节）。

响应形如：
`{"error": {"code", "message", "details", "trace_id", "current"}}`

约束：
- `message` 是可直接显示的中文；不返回栈、Key、原文或内部对象键。
- 无权与不存在共用同一类错误，不泄露另一用户对象是否存在。
- 版本冲突用 `current` 带上当前可见版本，供前端展示差异而不丢草稿。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """所有业务错误的基类。稳定 `code` 是前端分流依据，不允许靠 message 文本判断。"""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"
    message: str = "请求无法处理"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        current: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}
        self.current = current

    def to_body(self, trace_id: str) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "trace_id": trace_id,
        }
        if self.current is not None:
            error["current"] = self.current
        return {"error": error}


class Unauthenticated(ApiError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHENTICATED"
    message = "登录已失效，请重新登录"


class NotFoundOrForbidden(ApiError):
    """
    无权与不存在合并为 404：任何一方单独存在都会泄露对象存在性。
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    message = "无权访问或对象不存在"


class InvalidRequest(ApiError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "INVALID_REQUEST"
    message = "参数不合法"


class VersionConflict(ApiError):
    status_code = status.HTTP_409_CONFLICT
    code = "VERSION_CONFLICT"
    message = "数据已变化，请重新加载后再操作"


class IdempotencyConflict(ApiError):
    status_code = status.HTTP_409_CONFLICT
    code = "IDEMPOTENCY_CONFLICT"
    message = "这个幂等键已用于另一组参数，请重新提交"


class FileRejected(ApiError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "FILE_REJECTED"
    message = "文件不符合上传限制"


class UnsupportedMedia(ApiError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "UNSUPPORTED_MEDIA_TYPE"
    message = "不支持这种文件格式"


class RateLimited(ApiError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    message = "请求过于频繁或超出预算，请稍后再试"


class ServiceUnavailable(ApiError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "SERVICE_UNAVAILABLE"
    message = "依赖的外部服务暂不可用"


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "-")


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(exc.to_body(_trace_id(request))),
        headers={"Cache-Control": "private, no-store"},
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """把 FastAPI 的校验错误也收进同一封套，前端只需处理一种结构。"""
    assert isinstance(exc, RequestValidationError)
    fields = [
        {"path": ".".join(str(p) for p in err.get("loc", ())), "reason": err.get("msg", "")}
        for err in exc.errors()
    ]
    err = InvalidRequest(details={"fields": fields})
    return JSONResponse(
        status_code=err.status_code,
        content=jsonable_encoder(err.to_body(_trace_id(request))),
        headers={"Cache-Control": "private, no-store"},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：绝不把栈或内部细节返回浏览器，只给 trace_id 供服务端日志对照。"""
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误，请稍后重试",
                "details": {},
                "trace_id": _trace_id(request),
            }
        },
        headers={"Cache-Control": "private, no-store"},
    )
