from __future__ import annotations

from sensoralign.core.quality import (
    compute_alignment_quality,
    compute_series_quality,
    ratio,
)
from sensoralign.core.resample import Sample
from sensoralign.core.timestamps import Gap


def test_series_quality_counts_and_ratios() -> None:
    samples = [
        Sample(1.0, "ok"),
        Sample(None, "no_left"),
        Sample(None, "in_gap"),
        Sample(None, "in_gap"),
    ]
    quality = compute_series_quality(samples, [Gap(10, 20, 10)], conflict_count=2)
    assert quality.total_cells == 4
    assert quality.value_cells == 1
    assert quality.coverage == 0.25
    assert quality.gap_cells == 2
    assert quality.gap_ratio == 0.5
    assert quality.conflict_count == 2
    assert quality.gap_count == 1
    assert quality.gap_duration_us == 10


def test_marker_distribution_contains_all_markers() -> None:
    quality = compute_series_quality([Sample(1.0, "ok")], [])
    assert quality.marker_counts == {
        "ok": 1,
        "no_left": 0,
        "no_right": 0,
        "in_gap": 0,
        "no_window": 0,
    }


def test_cross_series_completion() -> None:
    first = [Sample(1.0, "ok"), Sample(2.0, "ok"), Sample(None, "in_gap")]
    second = [Sample(1.0, "ok"), Sample(None, "no_left"), Sample(3.0, "ok")]
    alignment = compute_alignment_quality([first, second], 3)
    assert alignment.common_cells == 1
    assert alignment.missing_any_cells == 2
    assert alignment.completion_rate == round(1 / 3, 6)


def test_empty_quality_is_zero_safe() -> None:
    assert ratio(1, 0) == 0.0
    quality = compute_series_quality([], [])
    assert quality.coverage == 0.0
    assert quality.gap_ratio == 0.0
    alignment = compute_alignment_quality([], 0)
    assert alignment.total_cells == 0
    assert alignment.completion_rate == 0.0
