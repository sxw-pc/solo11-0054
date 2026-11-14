from __future__ import annotations

import pytest

from sensoralign.core.resample import MARKER_ORDER, marker_counts, resample_points
from sensoralign.core.series import SeriesPoint
from sensoralign.core.timestamps import Gap, find_gaps


def pts(*values: tuple[int, float]) -> list[SeriesPoint]:
    return [SeriesPoint(ts, value, index + 1) for index, (ts, value) in enumerate(values)]


def test_linear_interpolation_and_edges() -> None:
    points = pts((0, 0.0), (10, 10.0))
    samples = resample_points(points, [-5, 0, 5, 10, 15], "linear", max_gap_us=100)
    assert [sample.marker for sample in samples] == ["no_left", "ok", "ok", "ok", "no_right"]
    assert samples[2].value == pytest.approx(5.0)


def test_linear_marks_large_gap_instead_of_bridging() -> None:
    points = pts((0, 1.0), (10, 11.0))
    gaps = find_gaps(points, 6)
    samples = resample_points(points, [0, 5, 10], "linear", max_gap_us=6, gaps=gaps)
    assert [sample.marker for sample in samples] == ["ok", "in_gap", "ok"]


def test_step_uses_latest_observation_and_respects_gap() -> None:
    points = pts((0, 1.0), (10, 3.0))
    samples = resample_points(points, [-1, 0, 5, 10, 20], "step", max_gap_us=5)
    assert [sample.value for sample in samples] == [None, 1.0, None, 3.0, 3.0]
    assert [sample.marker for sample in samples] == ["no_left", "ok", "in_gap", "ok", "ok"]


def test_mean_window_and_no_window() -> None:
    points = pts((0, 2.0), (2, 4.0), (10, 9.0))
    samples = resample_points(points, [-5, 1, 5, 11, 20], "mean", max_gap_us=100, step_us=4)
    assert samples[0].marker == "no_left"
    assert samples[1].value == pytest.approx(3.0)
    assert samples[2].marker == "no_window"
    assert samples[3].value == pytest.approx(9.0)
    assert samples[4].marker == "no_right"


def test_lag_window_odd_step_is_left_closed_right_open() -> None:
    points = pts((10, 2.0), (14, 4.0), (15, 8.0), (19, 16.0), (20, 32.0))
    samples = resample_points(
        points, [15, 20], "lag_window", max_gap_us=100, step_us=5
    )
    assert [sample.marker for sample in samples] == ["ok", "ok"]
    # Window [10, 15) excludes the right edge; a centered odd-step window
    # would instead include the observation at 15.
    assert samples[0].value == pytest.approx(3.0)
    # Window [15, 20) excludes the observation exactly at 20.
    assert samples[1].value == pytest.approx(12.0)


def test_lag_window_marks_windows_before_first_and_after_last() -> None:
    points = pts((100, 1.0), (105, 3.0))
    samples = resample_points(
        points, [90, 100, 105, 110, 120], "lag_window", max_gap_us=100, step_us=10
    )
    assert [sample.marker for sample in samples] == [
        "no_window_lower",
        "no_window_lower",
        "ok",
        "ok",
        "no_window_upper",
    ]
    assert samples[2].value == pytest.approx(1.0)
    assert samples[3].value == pytest.approx(2.0)


def test_lag_window_no_window_between_observation_runs() -> None:
    points = pts((0, 1.0), (40, 5.0))
    samples = resample_points(points, [20], "lag_window", max_gap_us=100, step_us=10)
    assert samples[0].marker == "no_window"


def test_lag_window_gap_truncation_beats_window_content() -> None:
    points = pts((0, 1.0), (10, 2.0), (20, 3.0), (100, 9.0))
    gaps = find_gaps(points, 50)
    samples = resample_points(
        points, [20, 50, 140], "lag_window", max_gap_us=50, gaps=gaps, step_us=30
    )
    # Instant 20 is the gap's left edge (not interior): window [-10, 20) has values.
    assert samples[0].marker == "ok"
    assert samples[0].value == pytest.approx(1.5)
    # Instant 50 is inside the gap: in_gap wins over the observation at 20.
    assert samples[1].marker == "in_gap"
    # Window [110, 140) lies entirely after the last observation.
    assert samples[2].marker == "no_window_upper"


def test_lag_window_requires_positive_step() -> None:
    with pytest.raises(ValueError):
        resample_points(pts((0, 1.0)), [0], "lag_window", max_gap_us=5)


def test_empty_and_single_point_series() -> None:
    empty = resample_points([], [0, 10], "linear", max_gap_us=5)
    assert [sample.marker for sample in empty] == ["no_left", "no_left"]
    single = resample_points(pts((10, 7.0)), [0, 10, 20], "linear", max_gap_us=5)
    assert [sample.marker for sample in single] == ["no_left", "ok", "no_right"]


def test_marker_counts_have_stable_keys_and_invalid_strategy_fails() -> None:
    sample_list = resample_points(pts((0, 1.0)), [0, 1], "step", max_gap_us=5)
    counts = marker_counts(sample_list)
    assert tuple(counts) == MARKER_ORDER
    assert counts["ok"] == 2
    with pytest.raises(ValueError):
        resample_points(pts((0, 1.0)), [0], "nearest", max_gap_us=5)
    with pytest.raises(ValueError):
        resample_points(pts((0, 1.0)), [0], "mean", max_gap_us=5)


def test_gap_dataclass_and_empty_gaps() -> None:
    assert find_gaps(pts((0, 1.0), (2, 2.0)), 2) == []
    assert find_gaps(pts((0, 1.0), (3, 2.0)), 2) == [Gap(0, 3, 3)]
