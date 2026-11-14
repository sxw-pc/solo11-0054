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
