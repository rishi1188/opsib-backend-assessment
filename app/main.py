"""Application factory."""

from fastapi import FastAPI

from app.ai.provider import DisabledProvider, SummaryProvider
from app.api.errors import install_error_handlers
from app.api.routes import router
from app.clock import Clock
from app.config import Settings, get_settings
from app.db.session import make_engine, make_session_factory
from app.logging_config import configure_logging


def create_app(
    settings: Settings | None = None,
    clock: Clock | None = None,
    ai_provider: SummaryProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)

    app = FastAPI(
        title="OPSiB Receipt Exception Service",
        version="1.0.0",
        description=(
            "Imports retail source exports, reconciles PO lines against goods receipts, "
            "detects receipt shortfalls at a frozen evaluation time, routes investigation "
            "tasks to eligible employees and records an auditable task lifecycle."
        ),
    )
    app.state.settings = settings
    app.state.clock = clock or Clock(settings.evaluation_time)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.ai_provider = ai_provider or DisabledProvider()
    install_error_handlers(app)
    app.include_router(router)
    return app
