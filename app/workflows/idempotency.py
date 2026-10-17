"""Idempotency-Key support for client commands.

The stored response is written in the same transaction as the command's state
change, so either both commit or neither does. A retry with the same key and
the same request body gets the stored response back without re-executing; the
same key with a different body is rejected with 409.
"""

import hashlib
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db.models import IdempotencyRecord
from app.errors import Conflict


def request_fingerprint(operation: str, body: dict[str, Any]) -> str:
    raw = json.dumps({"op": operation, "body": body}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def run_idempotent(
    session: Session,
    clock: Clock,
    tenant_id: str,
    key: str | None,
    operation: str,
    body: dict[str, Any],
    execute: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any], bool]:
    """Returns (status_code, response_body, replayed)."""
    if not key:
        status, response = execute()
        return status, response, False

    fingerprint = request_fingerprint(operation, body)
    stored = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id, IdempotencyRecord.idempotency_key == key
        )
    )
    if stored is not None:
        if stored.request_hash != fingerprint:
            raise Conflict("Idempotency-Key was already used for a different request",
                           {"operation": stored.operation})
        return stored.response_status, stored.response_body, True

    status, response = execute()
    session.add(
        IdempotencyRecord(
            tenant_id=tenant_id,
            idempotency_key=key,
            operation=operation,
            request_hash=fingerprint,
            response_status=status,
            response_body=response,
            created_at=clock.now(),
        )
    )
    session.flush()
    return status, response, False
