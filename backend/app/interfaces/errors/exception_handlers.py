from fastapi import Request, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import logging
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.application.errors.exceptions import AppException
from app.interfaces.schemas.base import APIResponse

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers"""

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Return useful validation messages without echoing rejected values."""
        errors = []
        for error in exc.errors():
            errors.append({
                "loc": [
                    part for part in error.get("loc", ())
                    if isinstance(part, (str, int))
                ],
                "msg": str(error.get("msg") or "Invalid value"),
                "type": str(error.get("type") or "value_error"),
            })
        logger.warning("Request validation failed with %s error(s)", len(errors))
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(AppException)
    async def api_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        """Handle custom API exceptions"""
        # Exception messages may contain provider payloads, paths, or request
        # values. The HTTP response keeps the established application contract,
        # while production logs retain only bounded classification metadata.
        logger.warning(
            "Application exception error_type=%s status_code=%d code=%d",
            type(exc).__name__,
            exc.status_code,
            exc.code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=APIResponse(
                code=exc.code,
                msg=exc.msg,
                data=None
            ).model_dump(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Handle HTTP exceptions"""
        logger.warning("HTTP exception status_code=%d", exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=APIResponse(
                code=exc.status_code,
                msg=exc.detail,
                data=None
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle all uncaught exceptions"""
        # Do not attach ``exc_info`` here: tracebacks include local variables
        # and source paths and can therefore disclose tool arguments.
        logger.error("Unhandled exception error_type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content=APIResponse(
                code=500,
                msg="Internal server error",
                data=None
            ).model_dump(),
        )
