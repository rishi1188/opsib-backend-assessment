"""Shared test fixtures.

Tests run against a fresh SQLite file per test by default. Set
``OPSIB_TEST_DATABASE_URL`` to a PostgreSQL URL to run the same suite against
PostgreSQL (tables are dropped and recreated per test).

The supplied fixture files are read from ``fixtures/`` and never modified.
Scenario variants are built in memory from those files (see ``csv_with``)
and documented in the README under "Derived test data".
"""

import csv
import io
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.config import Settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.detection.service import run_detection
from app.ingestion.registry import LOAD_ORDER
from app.ingestion.service import import_dataset
from app.main import create_app

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TENANT = "default"


def fixture_bytes(dataset: str) -> bytes:
    return (FIXTURES / f"{dataset}.csv").read_bytes()


def csv_with(dataset: str, *, drop=None, replace=None, append=None) -> bytes:
    """Derive a variant of a fixture file in memory.

    drop:    predicate(row) -> True to remove the row
    replace: predicate(row) -> dict of field overrides (or None)
    append:  list of extra row dicts
    """
    reader = csv.DictReader(io.StringIO(fixture_bytes(dataset).decode("utf-8-sig")))
    rows = []
    for row in reader:
        if drop and drop(row):
            continue
        if replace:
            override = replace(row)
            if override:
                row = {**row, **override}
        rows.append(row)
    rows.extend(append or [])
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


@dataclass
class Env:
    settings: Settings
    clock: Clock
    factory: sessionmaker[Session]

    def load(self, dataset: str, content: bytes | None = None, filename: str | None = None) -> str:
        return import_dataset(self.factory, self.clock, TENANT, dataset,
                              content if content is not None else fixture_bytes(dataset),
                              filename or f"{dataset}.csv")

    def load_all(self, overrides: dict[str, bytes] | None = None) -> dict[str, str]:
        overrides = overrides or {}
        return {d: self.load(d, overrides.get(d)) for d in LOAD_ORDER}

    def detect(self) -> str:
        return run_detection(self.factory, self.clock, self.settings, TENANT)

    def session(self) -> Session:
        return self.factory()


@pytest.fixture
def database_url(tmp_path) -> str:
    return os.environ.get("OPSIB_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def env(database_url) -> Env:
    settings = Settings(database_url=database_url, log_level="WARNING")
    engine = make_engine(database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield Env(settings, Clock(settings.evaluation_time), make_session_factory(engine))
    engine.dispose()


@pytest.fixture
def client(env) -> TestClient:
    app = create_app(env.settings, env.clock)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def loaded(env) -> Env:
    env.load_all()
    env.detect()
    return env
