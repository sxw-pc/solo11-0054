from __future__ import annotations

import pytest

from sensoralign.core.errors import DataError, Stage
from sensoralign.core.series import SeriesPoint
from sensoralign.core.timestamps import (
    count_inversions,
    find_gaps,
    parse_ts_us,
    sanitize_points,
)


def point(ts_us: int, value: float, line: int = 1) -> SeriesPoint:
    return SeriesPoint(ts_us, value, line)


def test_parse_ts_accepts_integer_forms_and_negatives() -> None:
    assert parse_ts_us(1_700_000_000_000_000) == 1_700_000_000_000_000
    assert parse_ts_us(" -42 ") == -42
    assert parse_ts_us("0") == 0


@pytest.mark.parametrize("raw", [1.0, "1.0", "1e3", "0.5"])
def test_parse_ts_rejects_floating_notation(raw: object) -> None:
    with pytest.raises(DataError) as caught:
        parse_ts_us(raw, path="x.csv", line=4)
    assert caught.value.code == "E_TS_FLOAT"
    assert caught.value.line == 4


@pytest.mark.parametrize("raw", [True, "abc", "+1", "1_000", ""])
def test_parse_ts_rejects_invalid_integers(raw: object) -> None:
    with pytest.raises(DataError) as caught:
        parse_ts_us(raw)
    assert caught.value.code in {"E_TS_NOT_INT", "E_TS_EMPTY"}


def test_count_inversions_and_sort_stable() -> None:
    points = [point(300, 3.0, 1), point(100, 1.0, 2), point(200, 2.0, 3)]
    assert count_inversions(points) == 1
    result = sanitize_points(points, monotonic_policy="sort_stable", series_id="s")
    assert [item.ts_us for item in result.points] == [100, 200, 300]
    assert result.events[0].code == "W_TS_OUT_OF_ORDER"


def test_reject_policy_raises_with_line() -> None:
    points = [point(100, 1.0, 1), point(90, 2.0, 2)]
    with pytest.raises(DataError) as caught:
        sanitize_points(points, monotonic_policy="reject")
    assert caught.value.code == "E_TS_NOT_MONOTONIC"
    assert caught.value.line == 2


def test_drop_late_removes_backwards_point() -> None:
    points = [point(100, 1.0, 1), point(300, 3.0, 2), point(200, 2.0, 3)]
    result = sanitize_points(points, monotonic_policy="drop_late")
    assert [item.ts_us for item in result.points] == [100, 300]
    assert result.dropped_late == 1
    assert result.events[0].code == "W_TS_OUT_OF_ORDER"


def test_duplicates_keep_last_and_conflict_keep_first() -> None:
    points = [
        point(100, 1.0, 1),
        point(100, 1.0, 2),
        point(100, 9.0, 3),
        point(200, 2.0, 4),
    ]
    result = sanitize_points(points, duplicate_policy="keep_last", conflict_policy="keep_first")
    assert [(item.ts_us, item.value) for item in result.points] == [(100, 1.0), (200, 2.0)]
    assert result.duplicates_removed == 1
    assert result.conflict_count == 1
    assert [event.code for event in result.events] == ["W_TS_CONFLICT", "W_TS_DUPLICATE"]


def test_reject_duplicate_and_conflict() -> None:
    with pytest.raises(DataError) as duplicate:
        sanitize_points([point(100, 1.0, 1), point(100, 1.0, 2)], duplicate_policy="reject")
    assert duplicate.value.code == "E_TS_DUPLICATE"
    assert duplicate.value.stage is Stage.TIMESTAMP_HYGIENE
    assert duplicate.value.stage.value == "timestamp_hygiene"
    assert duplicate.value.as_dict()["stage"] == "timestamp_hygiene"
    with pytest.raises(DataError) as conflict:
        sanitize_points([point(100, 1.0, 1), point(100, 2.0, 2)], conflict_policy="reject")
    assert conflict.value.code == "E_TS_CONFLICT"
    assert conflict.value.stage.value == "timestamp_hygiene"


def test_find_gaps_is_strict_and_supports_negative_time() -> None:
    points = [point(-100, 1.0), point(0, 2.0), point(100, 3.0), point(301, 4.0)]
    gaps = find_gaps(points, 100)
    assert len(gaps) == 1
    assert gaps[0].start_us == 100
    assert gaps[0].end_us == 301
    assert gaps[0].duration_us == 201


def test_empty_sequence_sanitizes_to_empty() -> None:
    result = sanitize_points([])
    assert result.points == []
    assert find_gaps([], 10) == []


def test_all_observations_conflict_at_one_timestamp() -> None:
    result = sanitize_points([point(7, 1.0, 1), point(7, 2.0, 2), point(7, 3.0, 3)])
    assert [(item.ts_us, item.value) for item in result.points] == [(7, 1.0)]
    assert result.conflict_count == 2
    assert [event.code for event in result.events] == ["W_TS_CONFLICT", "W_TS_CONFLICT"]


def test_huge_gap_uses_integer_microseconds() -> None:
    points = [point(-10**15, 1.0), point(10**15, 2.0)]
    gap = find_gaps(points, 10**6)[0]
    assert gap.duration_us == 2 * 10**15
