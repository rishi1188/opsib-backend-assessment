"""Domain errors mapped to structured API responses in ``app.api.errors``."""

from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class InvalidTransition(DomainError):
    status_code = 409
    code = "invalid_transition"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"
