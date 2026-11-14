"""Stable per-series and cross-series alignment quality metrics.

Coverage is the share of grid cells with an actual resampled value (marker
``ok``). Gap ratio is the share marked ``in_gap``. Cross-series completion is
the share of cells for which every participating series has a real value.

The window layer adds a second, stricter view. Each series has a *main window*
spanning its first through last ``ok`` grid cell. Leading ``no_left`` and
trailing ``no_right`` cells are therefore never window endpoints, while
``in_gap`` cells inside the window stay in the denominator without contributing
values. The alignment layer intersects those windows at
``[max(start), min(end)]``; an empty intersection has a completion rate of
``0.0`` rather than a missing value.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass

from .resample import Sample, marker_counts
from .timestamps import Gap

SERIES_WINDOW_METRICS = (
    "main_window_start_us",
    "main_window_end_us",
    "main_window_span_us",
    "main_window_cells",
    "main_window_value_cells",
    "main_window_coverage",
)
ALIGNMENT_WINDOW_METRICS = (
    "intersection_start_us",
    "intersection_end_us",
    "intersection_cells",
    "intersection_common_cells",
    "intersection_completion_rate",
)


def ratio(numerator: int, denominator: int) -> float:
    """Return a six-decimal ratio, or ``0.0`` for an empty denominator."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


@dataclass(frozen=True)
class WindowSpan:
    """The first-to-last ``ok`` window, or an empty window."""

    start_us: int | None
    end_us: int | None
    span_us: int
    cells: int
    value_cells: int
    coverage: float


@dataclass(frozen=True)
class SeriesQuality:
    """Quality summary for one resampled series."""

    total_cells: int
    value_cells: int
    coverage: float
    gap_cells: int
    gap_ratio: float
    conflict_count: int
    gap_count: int
    gap_duration_us: int
    marker_counts: dict[str, int]
    main_window_start_us: int | None = None
    main_window_end_us: int | None = None
    main_window_span_us: int = 0
    main_window_cells: int = 0
    main_window_value_cells: int = 0
    main_window_coverage: float = 0.0


@dataclass(frozen=True)
class AlignmentQuality:
    """Cross-series completeness summary."""

    total_cells: int
    common_cells: int
    missing_any_cells: int
    completion_rate: float
    intersection_start_us: int | None = None
    intersection_end_us: int | None = None
    intersection_cells: int = 0
    intersection_common_cells: int = 0
    intersection_completion_rate: float = 0.0


def compute_series_quality(
    samples: Sequence[Sample],
    gaps: Sequence[Gap],
    conflict_count: int = 0,
    coverage_window_us: int | None = None,
    grid_times: Sequence[int] | None = None,
) -> SeriesQuality:
    """Compute deterministic metrics for one series' grid samples.

    ``coverage_window_us`` optionally caps the main window from the first
    ``ok`` cell; omitting it keeps the natural first-to-last ``ok`` window.
    """
    total = len(samples)
    counts = marker_counts(samples)
    values = counts["ok"]
    gap_cells = counts["in_gap"]
    window = compute_main_window(samples, coverage_window_us=coverage_window_us, grid_times=grid_times)
    return SeriesQuality(
        total_cells=total,
        value_cells=values,
        coverage=ratio(values, total),
        gap_cells=gap_cells,
        gap_ratio=ratio(gap_cells, total),
        conflict_count=conflict_count,
        gap_count=len(gaps),
        gap_duration_us=sum(gap.duration_us for gap in gaps),
        marker_counts=counts,
        main_window_start_us=window.start_us,
        main_window_end_us=window.end_us,
        main_window_span_us=window.span_us,
        main_window_cells=window.cells,
        main_window_value_cells=window.value_cells,
        main_window_coverage=window.coverage,
    )


def compute_main_window(
    samples: Sequence[Sample],
    *,
    coverage_window_us: int | None = None,
    grid_times: Sequence[int] | None = None,
) -> WindowSpan:
    """Return the main window for one series.

    Endpoints are the first and last ``ok`` cells, so edge ``no_left`` /
    ``no_right`` markers are excluded by construction. Interior ``in_gap``
    cells remain in ``cells`` but not in ``value_cells``. A series without any
    ``ok`` cell has an empty window: no endpoints and zero coverage.
    """
    indices = _window_indices(samples, coverage_window_us, grid_times)
    if indices is None:
        return WindowSpan(None, None, 0, 0, 0, 0.0)
    start, end = indices
    start_us = _cell_time(start, grid_times)
    end_us = _cell_time(end, grid_times)
    values = sum(1 for index in range(start, end + 1) if samples[index].marker == "ok")
    cells = end - start + 1
    span_us = 0 if start_us is None or end_us is None else end_us - start_us
    return WindowSpan(start_us, end_us, span_us, cells, values, ratio(values, cells))


def compute_alignment_quality(
    sample_sets: Sequence[Sequence[Sample]],
    total_cells: int,
    coverage_window_us: int | None = None,
    grid_times: Sequence[int] | None = None,
) -> AlignmentQuality:
    """Compute cells present in every series and cells missing in any.

    The window intersection is ``[max(starts), min(ends)]`` over all series'
    main windows. It is empty when a series has no ``ok`` cell or when the
    latest start falls after the earliest end; its completion rate is then
    ``0.0``.
    """
    common = 0
    if total_cells > 0:
        for cell in range(total_cells):
            if all(samples[cell].marker == "ok" for samples in sample_sets):
                common += 1
    missing = total_cells - common
    intersection = _intersection(sample_sets, coverage_window_us, grid_times)
    return AlignmentQuality(
        total_cells=total_cells,
        common_cells=common,
        missing_any_cells=missing,
        completion_rate=ratio(common, total_cells),
        intersection_start_us=intersection.start_us,
        intersection_end_us=intersection.end_us,
        intersection_cells=intersection.cells,
        intersection_common_cells=intersection.value_cells,
        intersection_completion_rate=intersection.coverage,
    )


def _intersection(
    sample_sets: Sequence[Sequence[Sample]],
    coverage_window_us: int | None,
    grid_times: Sequence[int] | None,
) -> WindowSpan:
    windows = [_window_indices(samples, coverage_window_us, grid_times) for samples in sample_sets]
    if not windows or any(window is None for window in windows):
        return WindowSpan(None, None, 0, 0, 0, 0.0)
    start = max(window[0] for window in windows if window is not None)
    end = min(window[1] for window in windows if window is not None)
    start_us = _cell_time(start, grid_times)
    end_us = _cell_time(end, grid_times)
    if start > end:
        return WindowSpan(start_us, end_us, 0, 0, 0, 0.0)
    shared = sum(
        1
        for cell in range(start, end + 1)
        if all(samples[cell].marker == "ok" for samples in sample_sets)
    )
    cells = end - start + 1
    span_us = 0 if start_us is None or end_us is None else end_us - start_us
    return WindowSpan(start_us, end_us, span_us, cells, shared, ratio(shared, cells))


def _window_indices(
    samples: Sequence[Sample],
    coverage_window_us: int | None,
    grid_times: Sequence[int] | None,
) -> tuple[int, int] | None:
    ok_indices = [index for index, sample in enumerate(samples) if sample.marker == "ok"]
    if not ok_indices:
        return None
    start, end = ok_indices[0], ok_indices[-1]
    if coverage_window_us is None or grid_times is None:
        return start, end
    first_time = grid_times[start]
    limit = first_time + coverage_window_us
    if grid_times[end] <= limit:
        return start, end
    capped = bisect.bisect_right(grid_times, limit) - 1
    return start, max(start, min(capped, end))


def _cell_time(index: int, grid_times: Sequence[int] | None) -> int | None:
    if grid_times is None or index >= len(grid_times):
        return None
    return grid_times[index]
