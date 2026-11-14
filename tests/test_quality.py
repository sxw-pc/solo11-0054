from __future__ import annotations

from sensoralign.core.quality import (
    compute_alignment_quality,
    compute_main_window,
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
    assert quality.main_window_start_us is None
    assert quality.main_window_end_us is None
    assert quality.main_window_cells == 0
    assert quality.main_window_coverage == 0.0
    alignment = compute_alignment_quality([], 0)
    assert alignment.total_cells == 0
    assert alignment.completion_rate == 0.0


def test_main_window_skips_edge_markers_but_counts_interior_gap() -> None:
    samples = [
        Sample(None, "no_left"),
        Sample(1.0, "ok"),
        Sample(None, "in_gap"),
        Sample(3.0, "ok"),
        Sample(None, "no_right"),
    ]
    times = (0, 10, 20, 30, 40)
    quality = compute_series_quality(samples, [], grid_times=times)
    assert quality.main_window_start_us == 10
    assert quality.main_window_end_us == 30
    assert quality.main_window_span_us == 20
    assert quality.main_window_cells == 3
    assert quality.main_window_value_cells == 2
    assert quality.main_window_coverage == round(2 / 3, 6)


def test_main_window_without_ok_cells_is_empty() -> None:
    window = compute_main_window([Sample(None, "no_left"), Sample(None, "no_right")], grid_times=(0, 10))
    assert window.start_us is None
    assert window.end_us is None
    assert window.span_us == 0
    assert window.cells == 0
    assert window.coverage == 0.0


def test_coverage_window_caps_span_from_first_ok_cell() -> None:
    samples = [Sample(1.0, "ok")] * 5
    quality = compute_series_quality(samples, [], coverage_window_us=20, grid_times=(0, 10, 20, 30, 40))
    assert quality.main_window_start_us == 0
    assert quality.main_window_end_us == 20
    assert quality.main_window_span_us == 20
    assert quality.main_window_cells == 3
    assert quality.main_window_coverage == 1.0


def test_alignment_window_intersection_and_completion() -> None:
    first = [Sample(1.0, "ok"), Sample(2.0, "ok"), Sample(None, "in_gap"), Sample(4.0, "ok")]
    second = [Sample(None, "no_left"), Sample(2.0, "ok"), Sample(3.0, "ok"), Sample(4.0, "ok")]
    times = (0, 10, 20, 30)
    alignment = compute_alignment_quality([first, second], 4, grid_times=times)
    assert alignment.intersection_start_us == 10
    assert alignment.intersection_end_us == 30
    assert alignment.intersection_cells == 3
    assert alignment.intersection_common_cells == 2
    assert alignment.intersection_completion_rate == round(2 / 3, 6)


def test_empty_window_intersection_reports_zero_not_null() -> None:
    early = [Sample(1.0, "ok"), Sample(None, "no_right"), Sample(None, "no_right")]
    late = [Sample(None, "no_left"), Sample(None, "no_left"), Sample(3.0, "ok")]
    times = (0, 10, 20)
    alignment = compute_alignment_quality([early, late], 3, grid_times=times)
    assert alignment.intersection_start_us == 20
    assert alignment.intersection_end_us == 0
    assert alignment.intersection_cells == 0
    assert alignment.intersection_common_cells == 0
    assert alignment.intersection_completion_rate == 0.0
