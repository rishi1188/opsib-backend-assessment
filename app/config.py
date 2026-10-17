"""Runtime configuration.

Every value can be overridden with an environment variable prefixed ``OPSIB_``
(for example ``OPSIB_DATABASE_URL``). Scenario logic never reads the machine
clock: the evaluation instant comes from ``OPSIB_EVALUATION_TIME``.
"""

from datetime import datetime
from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSIB_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./opsib.db"
    default_tenant_id: str = "default"

    # Frozen evaluation instant used for deadlines, shift checks and all
    # system-generated timestamps. Must carry an explicit UTC offset.
    evaluation_time: datetime = datetime.fromisoformat("2026-10-16T15:00:00+05:30")

    # Detection rule configuration.
    shortfall_tolerance_base_units: Decimal = Decimal("0")

    # Routing: an availability sample older than this (relative to the
    # evaluation time) is treated as stale and the employee is not eligible.
    availability_max_age_minutes: int = 60

    # Due time for a newly created investigation task.
    task_sla_hours: int = 24

    # Optional AI investigation summary. Disabled by default; no API calls.
    ai_provider: str = "disabled"
    ai_timeout_seconds: float = 10.0

    log_level: str = "INFO"

    @field_validator("evaluation_time")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation_time must include a UTC offset")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
