"""Schema migrations match the models; concurrent pipeline runs on PostgreSQL
produce one result."""

import os
import threading

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select

from tests.conftest import TENANT, fixture_bytes

from app.db.base import Base
from app.db.models import ExceptionCase, SourceRecord, Task
from app.db.session import make_engine, make_session_factory
from app.detection.service import run_detection
from app.ingestion.registry import LOAD_ORDER
from app.ingestion.service import import_dataset

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_alembic_upgrade_produces_the_model_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
    command.downgrade(cfg, "base")


@pytest.mark.skipif(not os.environ.get("OPSIB_TEST_DATABASE_URL", "").startswith("postgresql"),
                    reason="needs PostgreSQL (set OPSIB_TEST_DATABASE_URL)")
def test_concurrent_imports_and_detection_create_one_result(env):
    """Several workers import the same files and run detection at the same
    time. The advisory lock serialises them and unique constraints back it up,
    so the end state equals a single run."""
    engine = make_engine(env.settings.database_url)
    factory = make_session_factory(engine)
    errors = []

    def worker():
        try:
            for dataset in LOAD_ORDER:
                import_dataset(factory, env.clock, TENANT, dataset, fixture_bytes(dataset), dataset)
            run_detection(factory, env.clock, env.settings, TENANT)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(SourceRecord)) == 36
        assert s.scalar(select(func.count()).select_from(ExceptionCase)) == 3
        assert s.scalar(select(func.count()).select_from(Task)) == 3
    engine.dispose()
