"""Stable per-series and cross-series alignment quality metrics.

Coverage is the share of grid cells with an actual resampled value (marker
``ok``). Gap ratio is the share marked ``in_gap``. Cross-series completion is
the share of cells for which every participating series has a real value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .resample import Sample, marker_counts
from .timestamps import Gap


def ratio(numerator: int, denominator: int) -> float:
    """Return a six-decimal ratio, or ``0.0`` for an empty denominator."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


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


@dataclass(frozen=True)
class AlignmentQuality:
    """Cross-series completeness summary."""

    total_cells: int
    common_cells: int
    missing_any_cells: int
    completion_rate: float


def compute_series_quality(
    samples: Sequence[Sample],
    gaps: Sequence[Gap],
    conflict_count: int = 0,
) -> SeriesQuality:
    """Compute deterministic metrics for one series' grid samples."""
    total = len(samples)
    counts = marker_counts(samples)
    values = counts["ok"]
    gap_cells = counts["in_gap"]
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
    )


def compute_alignment_quality(
    sample_sets: Sequence[Sequence[Sample]],
    total_cells: int,
) -> AlignmentQuality:
    """Compute cells present in every series and cells missing in any."""
    if total_cells <= 0:
        return AlignmentQuality(0, 0, 0, 0.0)
    common = 0
    for cell in range(total_cells):
        if all(samples[cell].marker == "ok" for samples in sample_sets):
            common += 1
    return AlignmentQuality(
        total_cells=total_cells,
        common_cells=common,
        missing_any_cells=total_cells - common,
        completion_rate=ratio(common, total_cells),
    )
