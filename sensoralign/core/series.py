"""Data models for one sensor measurement stream.

Timestamps are always signed integer microseconds since the Unix epoch.  The
models deliberately do not depend on the loader, so unit tests can construct
series without touching the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesPoint:
    """A single observation.

    Attributes:
        ts_us: Signed integer timestamp in microseconds.
        value: Finite floating-point measurement.
        line: One-based physical source line, used in diagnostics.
    """

    ts_us: int
    value: float
    line: int


@dataclass
class Series:
    """One named input stream and its input provenance."""

    series_id: str
    device_id: str
    metric: str
    points: list[SeriesPoint]
    input_path: str
    input_format: str
    fingerprint: str

    @property
    def raw_count(self) -> int:
        """Number of observations as read from the source file."""
        return len(self.points)

    @property
    def first_ts_us(self) -> int | None:
        """First timestamp in source order, or ``None`` when empty."""
        return self.points[0].ts_us if self.points else None

    @property
    def last_ts_us(self) -> int | None:
        """Last timestamp in source order, or ``None`` when empty."""
        return self.points[-1].ts_us if self.points else None

