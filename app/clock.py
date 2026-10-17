"""The only source of "now" in the application.

The assessment requires that scenario logic uses a frozen evaluation time and
never the machine clock. Every service receives a ``Clock`` and asks it for the
current instant, so tests can pin or move time explicitly.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Clock:
    """A clock pinned to a fixed, timezone-aware instant."""

    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.tzinfo is None or self.instant.utcoffset() is None:
            raise ValueError("Clock instant must be timezone-aware")

    def now(self) -> datetime:
        return self.instant
