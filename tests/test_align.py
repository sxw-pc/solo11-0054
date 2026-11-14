from __future__ import annotations

from pathlib import Path

import pytest

from sensoralign.core.align import align_contract, build_grid
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
