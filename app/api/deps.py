"""FastAPI dependencies.

``X-Tenant-Id`` and ``X-Actor-Id`` headers stand in for authentication in this
exercise. A live pilot would derive both from a verified token and enforce
tenant scoping in the auth layer; they are documented as not implemented.
"""

from collections.abc import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session, sessionmaker

from app.ai.provider import SummaryProvider
from app.clock import Clock
from app.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def get_factory(request: Request) -> sessionmaker[Session]:
    return request.app.state.session_factory


def get_provider(request: Request) -> SummaryProvider:
    return request.app.state.ai_provider


def get_session(factory: sessionmaker[Session] = Depends(get_factory)) -> Iterator[Session]:
    """Read-only request session. Write endpoints open explicit transactions."""
    session = factory()
    try:
        yield session
    finally:
        session.close()


def get_tenant(
    x_tenant_id: str | None = Header(default=None), settings: Settings = Depends(get_settings)
) -> str:
    return x_tenant_id or settings.default_tenant_id


def get_actor(x_actor_id: str = Header(..., description="Employee ID performing the action")) -> str:
    return x_actor_id
