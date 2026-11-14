"""Grid resampling with explicit, auditable missing-value markers.

Strategies:
    linear:     interpolate only when observations exist on both sides.
    step:       carry the latest observation at or before the grid instant.
    mean:       arithmetic mean of observations in a centered one-grid-step window.
    lag_window: arithmetic mean over the fixed trailing window
                ``[instant - step_us, instant)``, whose right edge is aligned to
                the grid step. Integer bounds keep odd ``step_us`` free of the
                ``step_us // 2`` asymmetry used by ``mean``.

Large gaps are excluded before any strategy runs. No marker is ever silently
filled with a synthetic value.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass

from .series import SeriesPoint
from .timestamps import Gap

MARKER_ORDER = (
    "ok",
    "no_left",
    "no_right",
    "in_gap",
    "no_window",
    "no_window_lower",
    "no_window_upper",
)
STRATEGIES = ("linear", "step", "mean", "lag_window")


@dataclass(frozen=True)
class Sample:
    """One grid cell for one series."""

    value: float | None
    marker: str


def resample_points(
    points: Sequence[SeriesPoint],
    grid_times: Sequence[int],
    strategy: str,
    *,
    max_gap_us: int,
    gaps: Sequence[Gap] | None = None,
    step_us: int | None = None,
) -> list[Sample]:
    """Resample sanitized points onto integer-microsecond grid instants.

    Args:
        points: Sanitized, timestamp-unique points in ascending order.
        grid_times: Ascending grid instants in microseconds.
        strategy: ``linear``, ``step``, ``mean`` or ``lag_window``.
        max_gap_us: Gap threshold used when ``gaps`` is not supplied.
        gaps: Precomputed gaps; if omitted they are derived from ``points``.
        step_us: Grid step, required by windowed strategies.

    Raises:
        ValueError: for an unknown strategy or a missing/invalid window step.

    Returns:
        One :class:`Sample` per grid instant, in grid order.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown resampling strategy: {strategy}")
    if strategy in ("mean", "lag_window") and (step_us is None or step_us <= 0):
        raise ValueError(f"{strategy} resampling requires a positive step_us")
    ordered = list(points)
    gap_list = list(gaps) if gaps is not None else _derive_gaps(ordered, max_gap_us)
    stamps = [point.ts_us for point in ordered]
    if not ordered:
        return [Sample(None, "no_left") for _ in grid_times]
    if strategy == "linear":
        return [_linear(ordered, stamps, instant, gap_list) for instant in grid_times]
    if strategy == "step":
        return [_step(ordered, stamps, instant, gap_list) for instant in grid_times]
    assert step_us is not None
    if strategy == "lag_window":
        return [_lag_window(ordered, stamps, instant, gap_list, step_us) for instant in grid_times]
    return [_mean(ordered, stamps, instant, gap_list, step_us) for instant in grid_times]


def _linear(
    points: Sequence[SeriesPoint],
    stamps: Sequence[int],
    instant: int,
    gaps: Sequence[Gap],
) -> Sample:
    index = bisect.bisect_left(stamps, instant)
    if index < len(stamps) and stamps[index] == instant:
        return Sample(points[index].value, "ok")
    if _inside_gap(gaps, instant):
        return Sample(None, "in_gap")
    if index == 0:
        return Sample(None, "no_left")
    if index == len(stamps):
        return Sample(None, "no_right")
    left = points[index - 1]
    right = points[index]
    span = right.ts_us - left.ts_us
    if span <= 0:
        return Sample(None, "in_gap")
    fraction = (instant - left.ts_us) / span
    return Sample(left.value + (right.value - left.value) * fraction, "ok")


def _step(
    points: Sequence[SeriesPoint],
    stamps: Sequence[int],
    instant: int,
    gaps: Sequence[Gap],
) -> Sample:
    if _inside_gap(gaps, instant):
        return Sample(None, "in_gap")
    index = bisect.bisect_right(stamps, instant) - 1
    if index < 0:
        return Sample(None, "no_left")
    return Sample(points[index].value, "ok")


def _mean(
    points: Sequence[SeriesPoint],
    stamps: Sequence[int],
    instant: int,
    gaps: Sequence[Gap],
    step_us: int,
) -> Sample:
    if _inside_gap(gaps, instant):
        return Sample(None, "in_gap")
    lower = instant - step_us // 2
    upper = instant + (step_us - step_us // 2)
    start = bisect.bisect_left(stamps, lower)
    stop = bisect.bisect_left(stamps, upper)
    if start < stop:
        window = [points[index].value for index in range(start, stop)]
        return Sample(math.fsum(window) / len(window), "ok")
    if instant < stamps[0]:
        return Sample(None, "no_left")
    if instant > stamps[-1]:
        return Sample(None, "no_right")
    return Sample(None, "no_window")


def _lag_window(
    points: Sequence[SeriesPoint],
    stamps: Sequence[int],
    instant: int,
    gaps: Sequence[Gap],
    step_us: int,
) -> Sample:
    """Mean over ``[instant - step_us, instant)`` with integer bounds.

    The right edge is the grid instant itself, so no half-step rounding is
    involved even for odd ``step_us``. Gap membership of the grid instant is
    decided before any window content is inspected.
    """
    if _inside_gap(gaps, instant):
        return Sample(None, "in_gap")
    lower = instant - step_us
    upper = instant
    start = bisect.bisect_left(stamps, lower)
    stop = bisect.bisect_left(stamps, upper)
    if start < stop:
        window = [points[index].value for index in range(start, stop)]
        return Sample(math.fsum(window) / len(window), "ok")
    if upper <= stamps[0]:
        return Sample(None, "no_window_lower")
    if lower > stamps[-1]:
        return Sample(None, "no_window_upper")
    return Sample(None, "no_window")


def _inside_gap(gaps: Sequence[Gap], instant: int) -> bool:
    for gap in gaps:
        if gap.start_us < instant < gap.end_us:
            return True
        if instant <= gap.start_us:
            break
    return False


def _derive_gaps(points: Sequence[SeriesPoint], max_gap_us: int) -> list[Gap]:
    gaps: list[Gap] = []
    for left, right in zip(points, points[1:]):
        delta = right.ts_us - left.ts_us
        if delta > max_gap_us:
            gaps.append(Gap(left.ts_us, right.ts_us, delta))
    return gaps


def marker_counts(samples: Sequence[Sample]) -> dict[str, int]:
    """Return counts for every marker in stable order, including zeros."""
    counts = {marker: 0 for marker in MARKER_ORDER}
    for sample in samples:
        counts[sample.marker] += 1
    return counts
