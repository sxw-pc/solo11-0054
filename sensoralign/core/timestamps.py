"""Integer-microsecond parsing and hygiene checks.

Public policies are intentionally explicit: monotonic handling is ``reject``,
``sort_stable`` or ``drop_late``; duplicate handling is ``keep_first``,
``keep_last`` or ``reject``; timestamp conflicts keep the first observation or
fail. Every removed point produces a deterministic audit event.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .errors import DataError, Event
from .series import SeriesPoint

_INT_RE = re.compile(r"-?\d+\Z")
_FLOAT_RE = re.compile(r"-?(?:\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d+\.\d*[eE][+-]?\d+)\Z")

MONOTONIC_POLICIES = ("reject", "sort_stable", "drop_late")
DUPLICATE_POLICIES = ("keep_first", "keep_last", "reject")
CONFLICT_POLICIES = ("keep_first", "reject")


@dataclass(frozen=True)
class Gap:
    """A detected gap between two adjacent observations."""

    start_us: int
    end_us: int
    duration_us: int


@dataclass(frozen=True)
class Conflict:
    """A same-timestamp, different-value observation that was discarded."""

    ts_us: int
    kept_value: float
    dropped_value: float
    kept_line: int
    dropped_line: int


@dataclass
class SanitizeResult:
    """Sanitized points plus counts and events for the report."""

    points: list[SeriesPoint] = field(default_factory=list)
    duplicates_removed: int = 0
    conflicts: list[Conflict] = field(default_factory=list)
    dropped_late: int = 0
    inversions: int = 0
    events: list[Event] = field(default_factory=list)

    @property
    def conflict_count(self) -> int:
        """Number of discarded conflicting observations."""
        return len(self.conflicts)


def parse_ts_us(
    raw: object,
    *,
    path: str | None = None,
    line: int | None = None,
    field: str = "ts_us",
) -> int:
    """Parse a timestamp as a signed integer number of microseconds.

    Floats, exponents, decimal points, booleans and empty strings are rejected
    rather than silently coerced. Negative epoch values are legal.

    Raises:
        DataError: ``E_TS_FLOAT`` for floating representations,
            ``E_TS_EMPTY`` for empty values, ``E_TS_NOT_INT`` otherwise.
    """
    if isinstance(raw, bool):
        raise DataError("E_TS_NOT_INT", "boolean timestamp is not an integer", path=path, line=line, field=field)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        raise DataError("E_TS_FLOAT", "timestamp must not be a float", path=path, line=line, field=field)
    if not isinstance(raw, str):
        raise DataError("E_TS_NOT_INT", "timestamp must be an integer", path=path, line=line, field=field)
    text = raw.strip()
    if not text:
        raise DataError("E_TS_EMPTY", "timestamp is empty", path=path, line=line, field=field)
    if _FLOAT_RE.fullmatch(text):
        raise DataError("E_TS_FLOAT", "timestamp must not use floating notation", path=path, line=line, field=field)
    if not _INT_RE.fullmatch(text):
        raise DataError("E_TS_NOT_INT", "timestamp contains non-integer characters", path=path, line=line, field=field)
    return int(text)


def count_inversions(points: Sequence[SeriesPoint]) -> int:
    """Count adjacent pairs whose timestamp is smaller than the previous one."""
    inversions = 0
    for previous, current in zip(points, points[1:]):
        if current.ts_us < previous.ts_us:
            inversions += 1
    return inversions


def sanitize_points(
    points: Iterable[SeriesPoint],
    *,
    monotonic_policy: str = "reject",
    duplicate_policy: str = "keep_first",
    conflict_policy: str = "keep_first",
    series_id: str | None = None,
    path: str | None = None,
) -> SanitizeResult:
    """Order points, remove duplicates and flag conflicting timestamps.

    Duplicate identity is the exact pair ``(ts_us, value)``. Points sharing a
    timestamp but differing in value are conflicts; with ``keep_first`` the
    first value wins and every discarded point becomes a warning event.

    Raises:
        DataError: ``E_TS_NOT_MONOTONIC``, ``E_TS_DUPLICATE`` or
            ``E_TS_CONFLICT`` under the corresponding rejecting policy.
    """
    original = list(points)
    if monotonic_policy not in MONOTONIC_POLICIES:
        raise ValueError(f"unknown monotonic policy: {monotonic_policy}")
    if duplicate_policy not in DUPLICATE_POLICIES:
        raise ValueError(f"unknown duplicate policy: {duplicate_policy}")
    if conflict_policy not in CONFLICT_POLICIES:
        raise ValueError(f"unknown conflict policy: {conflict_policy}")

    result = SanitizeResult(inversions=count_inversions(original))
    working = original
    if monotonic_policy == "reject":
        for previous, current in zip(original, original[1:]):
            if current.ts_us < previous.ts_us:
                raise DataError(
                    "E_TS_NOT_MONOTONIC",
                    "timestamp moves backwards",
                    path=path,
                    line=current.line,
                    field="ts_us",
                )
    elif monotonic_policy == "sort_stable":
        working = sorted(original, key=lambda point: point.ts_us)
        for previous, current in zip(original, original[1:]):
            if current.ts_us < previous.ts_us:
                result.events.append(
                    Event(
                        "W_TS_OUT_OF_ORDER",
                        "warning",
                        "timestamp_hygiene",
                        f"out-of-order timestamp at line {current.line}",
                        series_id=series_id,
                        path=path,
                        line=current.line,
                        field="ts_us",
                        detail={"previous_us": previous.ts_us, "current_us": current.ts_us},
                    )
                )
    else:
        kept: list[SeriesPoint] = []
        latest: int | None = None
        for point in original:
            if latest is not None and point.ts_us < latest:
                result.dropped_late += 1
                result.events.append(
                    Event(
                        "W_TS_OUT_OF_ORDER",
                        "warning",
                        "timestamp_hygiene",
                        f"dropped late timestamp at line {point.line}",
                        series_id=series_id,
                        path=path,
                        line=point.line,
                        field="ts_us",
                        detail={"latest_us": latest, "current_us": point.ts_us},
                    )
                )
                continue
            kept.append(point)
            latest = point.ts_us
        working = kept

    indexed: list[tuple[int, list[SeriesPoint]]] = []
    for point in working:
        if indexed and indexed[-1][0] == point.ts_us:
            indexed[-1][1].append(point)
        else:
            indexed.append((point.ts_us, [point]))

    kept_points: list[SeriesPoint] = []
    for ts_us, group in indexed:
        first = group[0]
        same_value = [point for point in group if point.value == first.value]
        for point in group:
            if point.value != first.value:
                if conflict_policy == "reject":
                    raise DataError(
                        "E_TS_CONFLICT",
                        "conflicting values share one timestamp",
                        path=path,
                        line=point.line,
                        field="value",
                    )
                result.conflicts.append(Conflict(ts_us, first.value, point.value, first.line, point.line))
                result.events.append(
                    Event(
                        "W_TS_CONFLICT",
                        "warning",
                        "timestamp_hygiene",
                        f"conflicting value at ts_us {ts_us}; first value kept",
                        series_id=series_id,
                        path=path,
                        line=point.line,
                        field="value",
                        detail={"ts_us": ts_us},
                    )
                )

        if duplicate_policy == "reject" and len(same_value) > 1:
            raise DataError(
                "E_TS_DUPLICATE",
                "duplicate observation",
                path=path,
                line=same_value[1].line,
                field="value",
            )
        if duplicate_policy == "keep_last":
            representative = same_value[-1]
            removed = same_value[:-1]
        else:
            representative = same_value[0]
            removed = same_value[1:]
        for point in removed:
            result.duplicates_removed += 1
            result.events.append(
                Event(
                    "W_TS_DUPLICATE",
                    "warning",
                    "timestamp_hygiene",
                    f"removed duplicate observation at ts_us {point.ts_us}",
                    series_id=series_id,
                    path=path,
                    line=point.line,
                    field="value",
                    detail={"ts_us": point.ts_us},
                )
            )
        kept_points.append(representative)

    result.points = sorted(kept_points, key=lambda point: point.ts_us)
    return result


def find_gaps(points: Sequence[SeriesPoint], max_gap_us: int) -> list[Gap]:
    """Return adjacent intervals strictly longer than ``max_gap_us``.

    The input is expected to be deduplicated and ordered; empty and one-point
    sequences yield an empty list.
    """
    gaps: list[Gap] = []
    for left, right in zip(points, points[1:]):
        delta = right.ts_us - left.ts_us
        if delta > max_gap_us:
            gaps.append(Gap(left.ts_us, right.ts_us, delta))
    return gaps



def min_interval_us(points: Sequence[SeriesPoint]) -> int | None:
    """Return the minimum positive adjacent interval, or ``None``."""
    deltas = [right.ts_us - left.ts_us for left, right in zip(points, points[1:]) if right.ts_us > left.ts_us]
    return min(deltas) if deltas else None
