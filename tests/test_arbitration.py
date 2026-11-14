from __future__ import annotations

from sensoralign.core.arbitration import arbitrate_inputs
from sensoralign.core.contract_model import InputSpec
from sensoralign.core.series import Series, SeriesPoint

DEVICE = "dev-x"
METRIC = "temperature"


def make_series(series_id: str, points: list[SeriesPoint]) -> Series:
    return Series(series_id, DEVICE, METRIC, points, f"fixtures/{series_id}.csv", "csv", f"sha-{series_id}")


def make_spec(series_id: str, priority: int | None = None) -> InputSpec:
    if priority is None:
        return InputSpec(series_id, f"fixtures/{series_id}.csv", "csv", DEVICE, METRIC)
    return InputSpec(series_id, f"fixtures/{series_id}.csv", "csv", DEVICE, METRIC, priority)


def bundle(spec: InputSpec, series: Series, points: list[SeriesPoint], ordinal: int):
    return (ordinal, spec, series, points)


def test_higher_priority_wins_conflict_even_when_declared_later() -> None:
    # Priority inversion: the low-priority stream is declared first but the
    # later high-priority observation must survive at the shared microsecond.
    low_points = [SeriesPoint(10, 1.0, 2), SeriesPoint(20, 2.0, 3)]
    high_points = [SeriesPoint(10, 9.0, 2), SeriesPoint(30, 3.0, 4)]
    result = arbitrate_inputs(
        [
            bundle(make_spec("low", 0), make_series("low", low_points), low_points, 0),
            bundle(make_spec("high", 5), make_series("high", high_points), high_points, 1),
        ]
    )
    assert [point.ts_us for point in result.points_by_series["low"]] == [20]
    assert [point.ts_us for point in result.points_by_series["high"]] == [10, 30]
    stats_low = result.stats_by_series["low"]
    assert stats_low.dropped == 1
    assert stats_low.merged == 0
    assert result.stats_by_series["high"] == type(stats_low)()
    event = next(event for event in result.events if event.code == "W_ARBITRATION_CONFLICT")
    assert event.series_id == "low"
    assert event.path == "fixtures/low.csv"
    assert event.line == 2
    assert event.detail["winner_series_id"] == "high"
    assert event.detail["kept_value"] == 9.0
    assert event.detail["dropped_value"] == 1.0
    assert event.detail["winner_priority"] == 5
    assert event.detail["loser_priority"] == 0


def test_undeclared_priority_is_lowest_and_declaration_order_breaks_ties() -> None:
    first_points = [SeriesPoint(10, 1.0, 2)]
    second_points = [SeriesPoint(10, 2.0, 2)]
    result = arbitrate_inputs(
        [
            bundle(make_spec("first"), make_series("first", first_points), first_points, 0),
            bundle(make_spec("second"), make_series("second", second_points), second_points, 1),
        ]
    )
    # Both default to priority 0: the earlier declaration keeps its value.
    assert result.points_by_series["first"][0].value == 1.0
    assert result.points_by_series["second"] == []
    assert result.stats_by_series["second"].dropped == 1
    event = result.events[0]
    assert event.code == "W_ARBITRATION_CONFLICT"
    assert event.series_id == "second"
    assert event.detail["winner_series_id"] == "first"
    assert event.detail["winner_priority"] == 0
    assert event.detail["loser_priority"] == 0


def test_same_value_at_same_microsecond_is_merged_without_grid_overlap() -> None:
    left_points = [SeriesPoint(10, 7.5, 2), SeriesPoint(20, 1.0, 3)]
    right_points = [SeriesPoint(10, 7.5, 5), SeriesPoint(30, 8.0, 6)]
    result = arbitrate_inputs(
        [
            bundle(make_spec("left", 2), make_series("left", left_points), left_points, 0),
            bundle(make_spec("right", 1), make_series("right", right_points), right_points, 1),
        ]
    )
    assert [point.ts_us for point in result.points_by_series["left"]] == [10, 20]
    assert [point.ts_us for point in result.points_by_series["left"]] == [10, 20]
    assert [point.ts_us for point in result.points_by_series["right"]] == [30]
    assert result.stats_by_series["right"].merged == 1
    assert result.stats_by_series["right"].dropped == 0
    merge_event = next(event for event in result.events if event.code == "W_ARBITRATION_DUPLICATE")
    assert merge_event.series_id == "right"
    assert merge_event.detail["kept_value"] == merge_event.detail["dropped_value"] == 7.5
    # A three-way identical observation produces two owned events, one per loser.
    third_points = [SeriesPoint(10, 7.5, 8)]
    triple = arbitrate_inputs(
        [
            bundle(make_spec("a", 2), make_series("a", third_points), third_points, 0),
            bundle(make_spec("b", 1), make_series("b", third_points), third_points, 1),
            bundle(make_spec("c", 0), make_series("c", third_points), third_points, 2),
        ]
    )
    assert [event.series_id for event in triple.events] == ["b", "c"]
    assert all(event.code == "W_ARBITRATION_DUPLICATE" for event in triple.events)


def test_different_devices_or_metrics_never_arbitrate() -> None:
    other = Series("other", "dev-y", "pressure", [SeriesPoint(10, 1.0, 1)], "fixtures/other.csv", "csv", "sha-other")
    own_points = [SeriesPoint(10, 2.0, 1)]
    own = make_series("own", own_points)
    result = arbitrate_inputs(
        [
            bundle(make_spec("other", 9), other, other.points, 0),
            bundle(make_spec("own", 0), own, own_points, 1),
        ]
    )
    assert result.events == []
    assert result.points_by_series["other"][0].value == 1.0
    assert result.points_by_series["own"][0].value == 2.0
    assert all(stats.dropped == 0 and stats.merged == 0 for stats in result.stats_by_series.values())


def test_events_and_counters_never_cross_streams() -> None:
    a_points = [SeriesPoint(10, 1.0, 1), SeriesPoint(20, 1.0, 2)]
    b_points = [SeriesPoint(10, 2.0, 1), SeriesPoint(20, 1.0, 2)]
    result = arbitrate_inputs(
        [
            bundle(make_spec("a", 1), make_series("a", a_points), a_points, 0),
            bundle(make_spec("b", 0), make_series("b", b_points), b_points, 1),
        ]
    )
    assert {event.series_id for event in result.events} == {"b"}
    assert result.stats_by_series["a"].dropped == 0
    assert result.stats_by_series["b"].dropped == 1
    assert result.stats_by_series["b"].merged == 1
    assert len(result.events) == 2


def test_evicted_incumbent_keeps_its_own_provenance() -> None:
    first_points = [SeriesPoint(10, 1.0, 42)]
    second_points = [SeriesPoint(10, 2.0, 7)]
    result = arbitrate_inputs(
        [
            bundle(make_spec("first", 1), make_series("first", first_points), first_points, 0),
            bundle(make_spec("second", 9), make_series("second", second_points), second_points, 1),
        ]
    )
    event = result.events[0]
    assert event.series_id == "first"
    assert event.path == "fixtures/first.csv"
    assert event.line == 42
    assert event.detail["winner_series_id"] == "second"
    assert result.points_by_series["first"] == []
    assert result.points_by_series["second"][0].value == 2.0


def test_priority_zero_inputspec_default() -> None:
    spec = InputSpec("s", "p.csv", "csv", DEVICE, METRIC)
    assert spec.priority == 0
