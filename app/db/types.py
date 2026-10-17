"""Column types that behave identically on PostgreSQL and SQLite."""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime stored as UTC.

    PostgreSQL keeps ``timestamptz`` natively. SQLite has no timezone support,
    so values are normalised to UTC before storage and re-tagged as UTC when
    loaded. Naive datetimes are rejected rather than guessed.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Naive datetimes are not allowed; supply a UTC offset")
        value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
