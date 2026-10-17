"""Consistent error envelope: ``{"error": {"code", "message", "details"}}``."""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.errors import DomainError
from app.logging_config import get_logger

log = get_logger("api")


def _body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content=_body(exc.code, exc.message, exc.details))

    @app.exception_handler(StaleDataError)
    async def _stale(_: Request, exc: StaleDataError):
        return JSONResponse(
            status_code=409,
            content=_body("concurrent_modification", "The resource was changed by another request; reload and retry"),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):
        log.warning("integrity_error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=409,
            content=_body("constraint_violation", "A concurrent request already created this record; retry"),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_body("request_invalid", "Request validation failed", {"errors": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})}),
        )
