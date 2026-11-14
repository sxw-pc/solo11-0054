from __future__ import annotations

import random
from collections.abc import Sequence
from pathlib import Path

import pytest

from sensoralign.core.align import MAX_GRID_CELLS, align_contract, build_grid
from sensoralign.core.contract import GridSpec, load_contract
from sensoralign.core.errors import DataError
from sensoralign.core.series import SeriesPoint

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_700_000_000_000_000


def _legacy_min_interval_us(points) -> int | None:
    """The pre-refactor interval scan: materializes every positive delta."""
    deltas = [
        right.ts_us - left.ts_us
        for left, right in zip(points, points[1:])
        if right.ts_us > left.ts_us
    ]
    return min(deltas) if deltas else None


def _legacy_build_grid(grid_spec: GridSpec, corrected_series) -> tuple[int, int, int, tuple[int, ...]]:
    """Pre-refactor reference path: one full scan per source for the step."""
    if grid_spec.mode == "from_union":
        nonempty = [points for points in corrected_series if points]
        if len(nonempty) != len(corrected_series) or not nonempty:
            raise DataError("E_GRID_NO_OVERLAP", "from_union requires at least one point in every series")
        common_start = max(points[0].ts_us for points in nonempty)
        common_end = min(points[-1].ts_us for points in nonempty)
        if common_start > common_end:
            raise DataError("E_GRID_NO_OVERLAP", "input coverage windows do not overlap")
        intervals = [_legacy_min_interval_us(points) for points in nonempty]
        positive = [interval for interval in intervals if interval is not None]
        if not positive:
            raise DataError("E_GRID_NO_STEP", "cannot infer a union-grid step from single-point series")
        step = min(positive)
        end = common_end + 1
        count = (common_end - common_start) // step + 1
        if count > MAX_GRID_CELLS:
            raise DataError("E_GRID_TOO_LARGE", "resolved grid exceeds 1,000,000 cells")
        times = tuple(common_start + index * step for index in range(count))
        return common_start, step, end, times

    origin = int(grid_spec.origin_us)  # type: ignore[arg-type]
    step = int(grid_spec.step_us)  # type: ignore[arg-type]
    end = int(grid_spec.end_us)  # type: ignore[arg-type]
    count = (end - origin + step - 1) // step
    if count <= 0:
        raise DataError("E_GRID_EMPTY", "explicit grid contains no cells")
    if count > MAX_GRID_CELLS:
        raise DataError("E_GRID_TOO_LARGE", "resolved grid exceeds 1,000,000 cells")
    return origin, step, end, tuple(range(origin, end, step))


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


def test_union_grid_multi_source_reduction() -> None:
    # Starts: 100/120/90 -> latest is 120. Ends: 160/180/150 -> earliest is 150.
    # Positive steps: 10, 20 and none (the third source has only zero deltas).
    first = [SeriesPoint(100 + 10 * index, float(index), index + 1) for index in range(7)]  # 100..160
    second = [SeriesPoint(120 + 20 * index, float(index), index + 1) for index in range(4)]  # 120..180
    third = [SeriesPoint(90, 1.0, 1), SeriesPoint(90, 2.0, 2), SeriesPoint(150, 3.0, 3)]
    grid = build_grid(GridSpec("from_union"), [first, second, third])
    assert (grid.origin_us, grid.step_us, grid.end_us) == (120, 10, 151)
    assert grid.cell_count == 4
    assert grid.times == (120, 130, 140, 150)


def test_union_grid_step_ignores_non_positive_deltas_but_keeps_endpoints() -> None:
    source = [SeriesPoint(50, 1.0, 1), SeriesPoint(50, 2.0, 2), SeriesPoint(40, 3.0, 3), SeriesPoint(120, 4.0, 4)]
    stepper = [SeriesPoint(60, 1.0, 1), SeriesPoint(70, 2.0, 2), SeriesPoint(80, 3.0, 3)]
    grid = build_grid(GridSpec("from_union"), [source, stepper])
    assert (grid.origin_us, grid.step_us, grid.end_us) == (60, 10, 81)
    assert grid.times == (60, 70, 80)


@pytest.mark.parametrize(
    "series",
    [
        [[SeriesPoint(3, 1.0, 1)], [SeriesPoint(3, 2.0, 1)]],
        [[SeriesPoint(0, 1.0, 1), SeriesPoint(0, 2.0, 2)], [SeriesPoint(0, 3.0, 1), SeriesPoint(0, 4.0, 2)]],
        [[SeriesPoint(0, 1.0, 1), SeriesPoint(0, 2.0, 2)]],
    ],
)
def test_union_grid_no_step_sources(series) -> None:
    with pytest.raises(DataError) as caught:
        build_grid(GridSpec("from_union"), series)
    assert caught.value.code == "E_GRID_NO_STEP"


@pytest.mark.parametrize(
    "series",
    [
        [],
        [[]],
        [[SeriesPoint(1, 1.0, 1)], []],
        [[], [SeriesPoint(1, 1.0, 1)]],
        [[SeriesPoint(1, 1.0, 1), SeriesPoint(2, 2.0, 2)], [SeriesPoint(5, 3.0, 1)]],
    ],
)
def test_union_grid_no_overlap_before_step_resolution(series) -> None:
    with pytest.raises(DataError) as caught:
        build_grid(GridSpec("from_union"), series)
    assert caught.value.code == "E_GRID_NO_OVERLAP"


def test_union_grid_too_large_is_checked_after_count_before_expansion() -> None:
    # Exactly 1,000,000 cells is legal: step 1 across a 999,999 us span.
    at_limit = [SeriesPoint(0, 1.0, 1), SeriesPoint(1, 2.0, 2), SeriesPoint(MAX_GRID_CELLS - 1, 3.0, 3)]
    grid = build_grid(GridSpec("from_union"), [at_limit])
    assert grid.step_us == 1
    assert grid.cell_count == MAX_GRID_CELLS
    assert grid.times[0] == 0 and grid.times[-1] == MAX_GRID_CELLS - 1

    # One extra cell must fail; the span is far too large to expand eagerly.
    over_limit = [SeriesPoint(0, 1.0, 1), SeriesPoint(1, 2.0, 2), SeriesPoint(MAX_GRID_CELLS, 3.0, 3)]
    with pytest.raises(DataError) as caught:
        build_grid(GridSpec("from_union"), [over_limit])
    assert caught.value.code == "E_GRID_TOO_LARGE"

    huge_span = [SeriesPoint(0, 1.0, 1), SeriesPoint(1, 2.0, 2), SeriesPoint(10**12, 3.0, 3)]
    with pytest.raises(DataError) as caught:
        build_grid(GridSpec("from_union"), [huge_span])
    assert caught.value.code == "E_GRID_TOO_LARGE"

    # An overlapping single-point source fails for a missing step, not for size.
    with pytest.raises(DataError) as caught:
        build_grid(GridSpec("from_union"), [[SeriesPoint(0, 1.0, 1)], [SeriesPoint(0, 2.0, 1)]])
    assert caught.value.code == "E_GRID_NO_STEP"


class _CountedSequence(Sequence[SeriesPoint]):
    """Read-only sequence that counts every element it hands out."""

    def __init__(self, points) -> None:
        self._points = list(points)
        self.accesses = 0

    def __len__(self) -> int:
        return len(self._points)

    def __getitem__(self, index):
        if isinstance(index, slice):
            items = self._points[index]
            self.accesses += len(items)
            return items
        self.accesses += 1
        return self._points[index]

    def __iter__(self):
        for item in self._points:
            self.accesses += 1
            yield item


def test_reduction_matches_legacy_with_many_sources() -> None:
    rng = random.Random(20261114)
    source_count = 64
    raw_series: list[list[SeriesPoint]] = []
    for source_index in range(source_count):
        start = 1_000_000 + rng.randint(0, 3)
        length = 40 + source_index
        native = 1 + source_index % 7
        points = [SeriesPoint(start + native * index, float(index), index + 1) for index in range(length)]
        # Duplicate the first timestamp so a zero delta exists; it must not
        # become the step.
        points.insert(1, SeriesPoint(points[0].ts_us, -1.0, 2))
        raw_series.append(points)

    new_grid = build_grid(GridSpec("from_union"), raw_series)
    legacy_origin, legacy_step, legacy_end, legacy_times = _legacy_build_grid(GridSpec("from_union"), raw_series)
    assert (new_grid.origin_us, new_grid.step_us, new_grid.end_us) == (legacy_origin, legacy_step, legacy_end)
    assert new_grid.cell_count == len(legacy_times)
    assert new_grid.times == legacy_times

    legacy_input = [_CountedSequence(points) for points in raw_series]
    _legacy_build_grid(GridSpec("from_union"), legacy_input)
    legacy_accesses = sum(sequence.accesses for sequence in legacy_input)

    fused_input = [_CountedSequence(points) for points in raw_series]
    build_grid(GridSpec("from_union"), fused_input)
    fused_accesses = sum(sequence.accesses for sequence in fused_input)

    # The legacy path reads every source twice (endpoints plus a full interval
    # scan whose slice re-reads the tail); the fused path reads it once.
    assert legacy_accesses > fused_accesses
    assert fused_accesses == sum(len(points) for points in raw_series)
    assert legacy_accesses == sum(2 * len(points) + 1 for points in raw_series)

