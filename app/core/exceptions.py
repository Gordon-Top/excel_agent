from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "app_error",
        status_code: int = status.HTTP_400_BAD_REQUEST,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.detail = detail or {}


class FileProcessingError(AppError):
    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="file_processing_error", detail=detail)


class ModelInvocationError(AppError):
    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="model_invocation_error", detail=detail)


class SearchProviderError(AppError):
    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_code="search_provider_error", detail=detail)


def serialize_exception(exc: Exception) -> dict[str, Any]:
    """Return task-safe error diagnostics without exposing secrets."""
    payload: dict[str, Any] = {
        "message": str(exc),
        "type": type(exc).__name__,
    }
    if isinstance(exc, AppError):
        payload["error_code"] = exc.error_code
        if exc.detail:
            payload["detail"] = exc.detail
    return payload


class SandboxExecutionError(AppError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            error_code="sandbox_execution_error",
            status_code=status_code,
            detail=detail,
        )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error_code": exc.error_code,
                "message": exc.message,
                "detail": exc.detail,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "success": False,
                "error_code": "request_validation_error",
                "message": "请求参数校验失败",
                "detail": {"errors": exc.errors()},
            },
        )

    @app.exception_handler(Exception)
    async def handle_unknown_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error_code": "internal_server_error",
                "message": "服务内部异常",
                "detail": {"exception": str(exc)},
            },
        )
