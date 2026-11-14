from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sensoralign.core.align import align_contract, build_grid
from sensoralign.core.align import _shift_to_floor as shift_to_floor
from sensoralign.core.contract import GridSpec, load_contract
from sensoralign.core.errors import DataError
from sensoralign.core.series import SeriesPoint

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_700_000_000_000_000


@pytest.fixture(scope="module")
def basic_result():
    contract = load_contract(ROOT / "fixtures" / "contracts" / "basic.yaml")
    return align_contract(contract)


def test_basic_alignment_has_expected_shape(basic_result) -> None:
    assert basic_result.grid.cell_count == 60
    assert [run.spec.series_id for run in basic_result.runs] == [
        "dev-a1",
        "dev-b2",
        "dev-c3",
        "dev-d4",
        "dev-e5",
    ]
    assert basic_result.alignment.common_cells == 42
    assert basic_result.alignment.missing_any_cells == 18
    assert basic_result.alignment.completion_rate == 0.7


def test_hygiene_conflicts_and_gap_markers(basic_result) -> None:
    by_id = {run.spec.series_id: run for run in basic_result.runs}
    d4 = by_id["dev-d4"]
    assert d4.sanitized.duplicates_removed == 1
    assert d4.sanitized.conflict_count == 1
    assert d4.quality.marker_counts["no_right"] == 9
    e5 = by_id["dev-e5"]
    assert e5.quality.gap_count == 1
    assert e5.quality.marker_counts["in_gap"] == 9
    assert {event.code for event in basic_result.events} == {
        "W_TS_OUT_OF_ORDER",
        "W_TS_DUPLICATE",
        "W_TS_CONFLICT",
        "W_GAP",
    }


def test_clock_correction_moves_drifted_device_onto_reference(basic_result) -> None:
    by_id = {run.spec.series_id: run for run in basic_result.runs}
    c3 = by_id["dev-c3"]
    assert c3.offset.slope_us_per_s == pytest.approx(100.0)
    source = next(point for point in c3.sanitized.points if point.ts_us == BASE + 36_000_000 + 3_600)
    corrected = next(point for point in c3.corrected_points if point.ts_us == BASE + 36_000_000)
    assert source.ts_us - corrected.ts_us == 3_600
    assert c3.quality.coverage == 1.0


def test_sparse_anchor_reports_warning_without_correction() -> None:
    contract = load_contract(ROOT / "fixtures" / "contracts" / "sparse_anchor.yaml")
    result = align_contract(contract)
    by_id = {run.spec.series_id: run for run in result.runs}
    assert by_id["dev-b2"].offset.status == "insufficient"
    assert any(event.code == "W_OFFSET_INSUFFICIENT" for event in result.events)
    assert by_id["dev-b2"].sanitized.points[0].ts_us == BASE + 2_000_000


def test_union_grid_and_failure_modes() -> None:
    first = [SeriesPoint(0, 1.0, 1), SeriesPoint(2, 2.0, 2), SeriesPoint(4, 3.0, 3)]
    second = [SeriesPoint(2, 4.0, 1), SeriesPoint(4, 5.0, 2), SeriesPoint(6, 6.0, 3)]
    grid = build_grid(GridSpec("from_union"), [first, second])
    assert grid.origin_us == 2
    assert grid.step_us == 2
    assert grid.times == (2, 4)
    with pytest.raises(DataError) as no_overlap:
        build_grid(GridSpec("from_union"), [first, [SeriesPoint(10, 1.0, 1)]])
    assert no_overlap.value.code == "E_GRID_NO_OVERLAP"
    with pytest.raises(DataError) as no_step:
        build_grid(GridSpec("from_union"), [[SeriesPoint(3, 1.0, 1)], [SeriesPoint(3, 2.0, 1)]])
    assert no_step.value.code == "E_GRID_NO_STEP"


def shift_floor_contract(floor_us: int | None):
    return replace(load_contract(ROOT / "fixtures" / "contracts" / "basic.yaml"), shift_floor_us=floor_us)


def intervals(points: list[SeriesPoint]) -> list[int]:
    return [right.ts_us - left.ts_us for left, right in zip(points, points[1:])]


def test_shift_floor_disabled_matches_baseline(basic_result) -> None:
    explicit_none = align_contract(shift_floor_contract(None))
    for before, after in zip(basic_result.runs, explicit_none.runs):
        assert after.corrected_points == before.corrected_points
        assert after.samples == before.samples
    assert explicit_none.events == basic_result.events
    assert "shift_floor_us" not in explicit_none.contract.snapshot()


def test_shift_floor_preserves_intervals_values_and_lines(basic_result) -> None:
    floor = BASE + 10_000_000
    shifted = align_contract(shift_floor_contract(floor))
    assert [run.spec.series_id for run in shifted.runs] == [run.spec.series_id for run in basic_result.runs]
    for before, after in zip(basic_result.runs, shifted.runs):
        old_points, new_points = before.corrected_points, after.corrected_points
        assert len(new_points) == len(old_points)
        assert after.offset == before.offset
        assert after.offset.median_offset_us == before.offset.median_offset_us
        assert after.offset.residual_median_us == before.offset.residual_median_us
        assert min(point.ts_us for point in new_points) == floor
        assert [point.value for point in new_points] == [point.value for point in old_points]
        assert [point.line for point in new_points] == [point.line for point in old_points]
        assert intervals(new_points) == intervals(old_points)
        assert all(right.ts_us > left.ts_us for left, right in zip(new_points, new_points[1:]))


def test_shift_floor_does_not_introduce_clock_reorder(basic_result) -> None:
    shifted = align_contract(shift_floor_contract(BASE + 10_000_000))
    assert all(event.code != "W_CLOCK_REORDER" for event in shifted.events)
    assert [event.code for event in shifted.events] == [event.code for event in basic_result.events]
    for before, after in zip(basic_result.events, shifted.events):
        if before.code == "W_GAP":
            # Absolute gap positions follow the shift; duration must not change.
            assert after.detail["duration_us"] == before.detail["duration_us"]
        else:
            assert after.detail == before.detail


def test_shift_floor_leaves_series_already_above_floor_untouched(basic_result) -> None:
    floor = BASE - 5_000_000
    result = align_contract(shift_floor_contract(floor))
    for before, after in zip(basic_result.runs, result.runs):
        assert after.corrected_points == before.corrected_points


def test_shift_floor_applies_per_series_independently() -> None:
    contract = shift_floor_contract(BASE + 10_000_000)
    result = align_contract(contract)
    starts = {run.spec.series_id: run.corrected_points[0].ts_us for run in result.runs}
    assert set(starts.values()) == {BASE + 10_000_000}
    assert result.grid == align_contract(shift_floor_contract(None)).grid


def write_series_csv(path: Path, device: str, start_us: int, gaps_us: list[int]) -> Path:
    lines = ["ts_us,device_id,metric,value"]
    ts = start_us
    for index in range(len(gaps_us) + 1):
        lines.append(f"{ts},{device},temperature,{20.0 + index}")
        if index < len(gaps_us):
            ts += gaps_us[index]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_shift_floor_uses_a_distinct_delta_per_series(tmp_path: Path) -> None:
    floor = BASE + 10_000_000
    early = write_series_csv(
        tmp_path / "early.csv", "dev-early", BASE - 4_000_000, [1_000_000, 3_000_000, 2_500_000, 1_500_000]
    )
    late = write_series_csv(
        tmp_path / "late.csv", "dev-late", BASE + 3_000_000, [2_000_000, 1_500_000, 4_000_000, 2_000_000]
    )
    contract_path = tmp_path / "shift.yaml"
    contract_path.write_text(
        f"""version: 1
name: shift-per-series
inputs:
  - id: dev-early
    path: {early.as_posix()}
    format: csv
    device_id: dev-early
    metric: temperature
  - id: dev-late
    path: {late.as_posix()}
    format: csv
    device_id: dev-late
    metric: temperature
grid:
  origin_us: {floor}
  step_us: 1000000
  end_us: {floor + 20_000_000}
shift_floor_us: {floor}
""",
        encoding="utf-8",
    )
    result = align_contract(load_contract(contract_path))
    original = {
        "dev-early": [1_000_000, 3_000_000, 2_500_000, 1_500_000],
        "dev-late": [2_000_000, 1_500_000, 4_000_000, 2_000_000],
    }
    by_id = {run.spec.series_id: run for run in result.runs}
    assert set(by_id) == set(original)
    for series_id, run in by_id.items():
        points = run.corrected_points
        assert points[0].ts_us == floor
        assert intervals(points) == original[series_id]
        assert all(right.ts_us > left.ts_us for left, right in zip(points, points[1:]))
    # Distinct starting points mean distinct per-series shifts.
    deltas = {sid: by_id[sid].corrected_points[0].ts_us for sid in by_id}
    assert set(deltas.values()) == {floor}
    assert not any(event.code.startswith("W_CLOCK") for event in result.events)


def test_shift_to_floor_edge_cases() -> None:
    points = [SeriesPoint(-5, 1.0, 7), SeriesPoint(0, 2.0, 9), SeriesPoint(3, 3.0, 11)]
    assert shift_to_floor(points, None) is points
    assert shift_to_floor([], 1_000) == []
    shifted = shift_to_floor(points, 1_000)
    assert [point.ts_us for point in shifted] == [1_000, 1_005, 1_008]
    assert [(point.value, point.line) for point in shifted] == [(1.0, 7), (2.0, 9), (3.0, 11)]
    assert intervals(shifted) == intervals(points)
    above = [SeriesPoint(10, 1.0, 1), SeriesPoint(20, 2.0, 2)]
    assert shift_to_floor(above, 5) is above
    assert [point.ts_us for point in shift_to_floor(above, 10)] == [10, 20]
    assert [point.ts_us for point in shift_to_floor(above, 15)] == [15, 25]
