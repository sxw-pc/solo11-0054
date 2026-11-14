from __future__ import annotations

import random

from sensoralign.core.quality import (
    GroupKey,
    compute_alignment_quality,
    compute_pairwise_matrix,
    compute_series_quality,
    ratio,
)
from sensoralign.core.resample import MARKER_ORDER, Sample
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
    matrix = compute_pairwise_matrix([], [], 0)
    assert matrix.groups == ()
    assert matrix.pairs == ()
    assert matrix.device_rollups == ()
    assert matrix.metric_rollups == ()


def _ok() -> Sample:
    return Sample(1.0, "ok")


def _missing(marker: str = "no_left") -> Sample:
    return Sample(None, marker)


def test_pairwise_matrix_partitions_cells_and_directions() -> None:
    # 4 cells, two groups: d1/temp missing cell 2, d2/pressure missing cell 3.
    first = [_ok(), _ok(), _missing("no_right"), _ok()]
    second = [_ok(), _ok(), _ok(), _missing("in_gap")]
    labels = [GroupKey("d1", "temp"), GroupKey("d2", "pressure")]
    matrix = compute_pairwise_matrix([first, second], labels, 4)
    assert [group.device_id for group in matrix.groups] == ["d1", "d2"]
    pair = matrix.pairs[0][1]
    assert pair.common_cells == 2
    assert pair.missing_any_cells == 2
    assert pair.left_only_missing_cells == 1
    assert pair.right_only_missing_cells == 1
    assert pair.both_missing_cells == 0
    reverse = matrix.pairs[1][0]
    assert reverse.left_only_missing_cells == 1
    assert reverse.right_only_missing_cells == 1
    # Every row partitions the grid without overlap.
    for row in matrix.pairs:
        for entry in row:
            assert (
                entry.common_cells
                + entry.left_only_missing_cells
                + entry.right_only_missing_cells
                + entry.both_missing_cells
                == 4
            )


def test_diagonal_uses_own_validity_even_when_all_no_left() -> None:
    # A whole series marked no_left: the diagonal is the group's own validity
    # rate, never mixing the inferred marker type into the valid side.
    samples = [_missing("no_left") for _ in range(4)]
    labels = [GroupKey("d1", "temp")]
    matrix = compute_pairwise_matrix([samples], labels, 4)
    diagonal = matrix.pairs[0][0]
    assert diagonal.common_cells == 0
    assert diagonal.missing_any_cells == 4
    assert diagonal.left_only_missing_cells == 0
    assert diagonal.right_only_missing_cells == 0
    assert diagonal.both_missing_cells == 4
    alignment = compute_alignment_quality([samples], 4)
    assert alignment.common_cells == 0
    assert alignment.missing_any_cells == 4
    assert alignment.completion_rate == 0.0


def test_in_gap_never_counts_as_valid_and_reconciles_completion() -> None:
    # in_gap on the first series at cells 1 and 2; the second series covers
    # cell 1 but also misses cell 2. The matrix missing count must equal the
    # completion-rate missing_any_cells derived independently.
    first = [_ok(), _missing("in_gap"), _missing("in_gap"), _ok()]
    second = [_ok(), _ok(), _missing("no_window"), _ok()]
    labels = [GroupKey("d1", "temp"), GroupKey("d1", "pressure")]
    matrix = compute_pairwise_matrix([first, second], labels, 4)
    index = {(group.device_id, group.metric): i for i, group in enumerate(matrix.groups)}
    temp_i, pressure_i = index[("d1", "temp")], index[("d1", "pressure")]
    pair = matrix.pairs[temp_i][pressure_i]
    # Cell 1: only the left group (d1/temp) misses; cell 2: both groups miss.
    assert pair.common_cells == 2
    assert pair.left_only_missing_cells == 1
    assert pair.right_only_missing_cells == 0
    assert pair.both_missing_cells == 1
    reverse = matrix.pairs[pressure_i][temp_i]
    assert reverse.left_only_missing_cells == 0
    assert reverse.right_only_missing_cells == 1
    alignment = compute_alignment_quality([first, second], 4)
    assert alignment.missing_any_cells == pair.missing_any_cells == 2
    assert alignment.common_cells == 2
    # The in_gap cells cannot appear as valid on either diagonal.
    assert matrix.pairs[temp_i][temp_i].common_cells == 2
    assert matrix.pairs[pressure_i][pressure_i].common_cells == 3


def test_series_sharing_a_group_are_intersected_before_pairing() -> None:
    # Two series of the same (device, metric): a group cell is valid only when
    # every member series is ok there.
    member_a = [_ok(), _ok(), _missing("no_left"), _ok()]
    member_b = [_ok(), _missing("no_right"), _ok(), _ok()]
    other = [_ok(), _ok(), _ok(), _missing("in_gap")]
    labels = [GroupKey("d1", "temp"), GroupKey("d1", "temp"), GroupKey("d2", "temp")]
    matrix = compute_pairwise_matrix([member_a, member_b, other], labels, 4)
    assert len(matrix.groups) == 2
    diagonal = matrix.pairs[0][0]
    # Group d1/temp is valid only at cells 0 and 3.
    assert diagonal.common_cells == 2
    assert diagonal.both_missing_cells == 2
    pair = matrix.pairs[0][1]
    # Common: cell 0 only; cell 1 left-missing, cell 2 left-missing, cell 3 right-missing.
    assert pair.common_cells == 1
    assert pair.left_only_missing_cells == 2
    assert pair.right_only_missing_cells == 1
    assert pair.both_missing_cells == 0


def test_device_and_metric_rollups() -> None:
    # Cell:                 0    1        2        3
    first = [_ok(), _ok(), _missing("no_left"), _ok()]      # d1 temp:     ok ok -- ok
    second = [_ok(), _missing("in_gap"), _ok(), _ok()]      # d1 pressure: ok -- ok ok
    third = [_ok(), _ok(), _ok(), _missing("no_right")]     # d2 temp:     ok ok ok --
    labels = [
        GroupKey("d1", "temp"),
        GroupKey("d1", "pressure"),
        GroupKey("d2", "temp"),
    ]
    matrix = compute_pairwise_matrix([first, second, third], labels, 4)
    devices = {rollup.key: rollup for rollup in matrix.device_rollups}
    d1 = devices["d1"]
    # d1 needs both of its series ok: valid only at cells 0 and 3.
    assert d1.series_count == 2
    assert d1.valid_cells == 2
    assert d1.common_with_all_cells == 1  # cell 0 is the only all-group cell
    # Cells 1 and 2: d1 missing while d2 stays valid; cell 3: only d2 missing.
    assert d1.group_missing_only_cells == 2
    assert d1.others_missing_only_cells == 1
    assert d1.both_missing_cells == 0
    assert d1.missing_any_cells == 3
    d2 = devices["d2"]
    assert d2.series_count == 1
    assert d2.valid_cells == 3
    assert d2.group_missing_only_cells == 1  # cell 3
    assert d2.others_missing_only_cells == 2  # cells 1 and 2
    metrics = {rollup.key: rollup for rollup in matrix.metric_rollups}
    temp = metrics["temp"]
    assert temp.series_count == 2
    assert temp.valid_cells == 2  # cells 0 and 2
    assert temp.common_with_all_cells == 1
    assert temp.group_missing_only_cells == 2
    assert temp.others_missing_only_cells == 1
    pressure = metrics["pressure"]
    assert pressure.series_count == 1
    assert pressure.valid_cells == 3  # cells 0, 2, 3
    assert pressure.group_missing_only_cells == 1
    assert pressure.others_missing_only_cells == 2
    for rollup in (*matrix.device_rollups, *matrix.metric_rollups):
        assert (
            rollup.group_missing_only_cells
            + rollup.others_missing_only_cells
            + rollup.both_missing_cells
            == rollup.missing_any_cells
        )


def test_matrix_matches_naive_cell_scan_under_random_markers() -> None:
    rng = random.Random(20261114)
    total_cells = 37
    series_count = 7
    labels = [
        GroupKey(f"d{rng.randint(0, 2)}", f"m{rng.randint(0, 2)}")
        for _ in range(series_count)
    ]
    sample_sets = [
        [
            _ok() if rng.random() < 0.6 else _missing(rng.choice(MARKER_ORDER[1:]))
            for _ in range(total_cells)
        ]
        for _ in range(series_count)
    ]
    matrix = compute_pairwise_matrix(sample_sets, labels, total_cells)
    group_to_series: dict[GroupKey, list[int]] = {}
    for index, label in enumerate(labels):
        group_to_series.setdefault(label, []).append(index)
    for left_index, left_group in enumerate(matrix.groups):
        for right_index, right_group in enumerate(matrix.groups):
            common = left_only = right_only = both = 0
            for cell in range(total_cells):
                left_ok = all(
                    sample_sets[series][cell].marker == "ok"
                    for series in group_to_series[left_group]
                )
                right_ok = all(
                    sample_sets[series][cell].marker == "ok"
                    for series in group_to_series[right_group]
                )
                if left_ok and right_ok:
                    common += 1
                elif left_ok:
                    right_only += 1
                elif right_ok:
                    left_only += 1
                else:
                    both += 1
            entry = matrix.pairs[left_index][right_index]
            assert entry.common_cells == common
            assert entry.left_only_missing_cells == left_only
            assert entry.right_only_missing_cells == right_only
            assert entry.both_missing_cells == both
    alignment = compute_alignment_quality(sample_sets, total_cells)
    all_groups_common = sum(
        all(
            all(sample_sets[series][cell].marker == "ok" for series in members)
            for members in group_to_series.values()
        )
        for cell in range(total_cells)
    )
    assert alignment.common_cells == all_groups_common
    assert alignment.missing_any_cells == total_cells - all_groups_common


def test_label_count_mismatch_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        compute_pairwise_matrix([[_ok()]], [GroupKey("d1", "temp"), GroupKey("d2", "temp")], 1)
