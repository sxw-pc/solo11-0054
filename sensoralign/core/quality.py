"""Stable per-series and cross-series alignment quality metrics.

Coverage is the share of grid cells with an actual resampled value (marker
``ok``). Gap ratio is the share marked ``in_gap``. Cross-series completion is
the share of cells for which every participating series has a real value.

Only the ``ok`` marker counts as a real value. Every other marker
(``no_left``, ``no_right``, ``in_gap``, ``no_window``) is treated as missing,
so the pairwise matrix can never disagree with :func:`compute_alignment_quality`
about how many cells are missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .resample import Sample, marker_counts
from .timestamps import Gap


def ratio(numerator: int, denominator: int) -> float:
    """Return a six-decimal ratio, or ``0.0`` for an empty denominator."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


@dataclass(frozen=True)
class SeriesQuality:
    """Quality summary for one resampled series."""

    total_cells: int
    value_cells: int
    coverage: float
    gap_cells: int
    gap_ratio: float
    conflict_count: int
    gap_count: int
    gap_duration_us: int
    marker_counts: dict[str, int]


@dataclass(frozen=True)
class AlignmentQuality:
    """Cross-series completeness summary."""

    total_cells: int
    common_cells: int
    missing_any_cells: int
    completion_rate: float


@dataclass(frozen=True)
class GroupKey:
    """A ``(device_id, metric)`` grouping of one or more series."""

    device_id: str
    metric: str


@dataclass(frozen=True)
class PairCellCounts:
    """Cell partition for one ordered ``(left, right)`` group pair.

    The four cell counts partition the grid: ``common_cells`` plus the three
    missing directions always equals ``total_cells``. On the diagonal both
    sides are the same group, so the one-way directions are zero by
    construction and every missing cell lands in ``both_missing_cells``;
    ``common_cells`` is therefore the group's own validity count (``ok``
    markers only), never a mix of inferred marker types.
    """

    total_cells: int
    common_cells: int
    missing_any_cells: int
    left_only_missing_cells: int
    right_only_missing_cells: int
    both_missing_cells: int


@dataclass(frozen=True)
class GroupRollup:
    """Completeness of one device or metric against the whole contract."""

    key: str
    series_count: int
    valid_cells: int
    common_with_all_cells: int
    missing_any_cells: int
    group_missing_only_cells: int
    others_missing_only_cells: int
    both_missing_cells: int


@dataclass(frozen=True)
class PairwiseMatrix:
    """Pairwise completeness view over ``(device_id, metric)`` groups."""

    total_cells: int
    groups: tuple[GroupKey, ...]
    pairs: tuple[tuple[PairCellCounts, ...], ...]
    device_rollups: tuple[GroupRollup, ...]
    metric_rollups: tuple[GroupRollup, ...]


def compute_series_quality(
    samples: Sequence[Sample],
    gaps: Sequence[Gap],
    conflict_count: int = 0,
) -> SeriesQuality:
    """Compute deterministic metrics for one series' grid samples."""
    total = len(samples)
    counts = marker_counts(samples)
    values = counts["ok"]
    gap_cells = counts["in_gap"]
    return SeriesQuality(
        total_cells=total,
        value_cells=values,
        coverage=ratio(values, total),
        gap_cells=gap_cells,
        gap_ratio=ratio(gap_cells, total),
        conflict_count=conflict_count,
        gap_count=len(gaps),
        gap_duration_us=sum(gap.duration_us for gap in gaps),
        marker_counts=counts,
    )


def compute_alignment_quality(
    sample_sets: Sequence[Sequence[Sample]],
    total_cells: int,
) -> AlignmentQuality:
    """Compute cells present in every series and cells missing in any.

    A cell is common only when every series carries marker ``ok`` there. The
    validity test is the same bit-set rule used by :func:`compute_pairwise_matrix`,
    so matrix-derived missing counts always reconcile with this summary.
    """
    if total_cells <= 0:
        return AlignmentQuality(0, 0, 0, 0.0)
    common_mask = _full_mask(total_cells)
    for samples in sample_sets:
        common_mask &= _validity_mask(samples, total_cells)
    common = common_mask.bit_count()
    return AlignmentQuality(
        total_cells=total_cells,
        common_cells=common,
        missing_any_cells=total_cells - common,
        completion_rate=ratio(common, total_cells),
    )


def compute_pairwise_matrix(
    sample_sets: Sequence[Sequence[Sample]],
    labels: Sequence[GroupKey],
    total_cells: int,
) -> PairwiseMatrix:
    """Build the pairwise completeness matrix over ``(device_id, metric)`` groups.

    Validity is aggregated once per grid cell into an integer bit-set per group
    (bit ``c`` is set exactly when every series of that group has marker ``ok``
    at cell ``c``); every pair is then derived with bit operations, avoiding an
    ``O(N² * cells)`` rescan of the samples.
    """
    if len(sample_sets) != len(labels):
        raise ValueError("sample_sets and labels must have the same length")
    if total_cells <= 0:
        return PairwiseMatrix(0, (), (), (), ())
    series_masks = [_validity_mask(samples, total_cells) for samples in sample_sets]
    # Independent path: intersect every raw-series validity set directly.
    # Because group masks are ANDs of exactly these series masks, the two
    # intersections must be the same set; this is the completion-rate tie-out.
    series_common = _full_mask(total_cells)
    for mask in series_masks:
        series_common &= mask
    order = sorted(range(len(labels)), key=lambda index: (labels[index].device_id, labels[index].metric))
    groups = tuple(dict.fromkeys(labels[index] for index in order))
    members_by_group: dict[GroupKey, list[int]] = {group: [] for group in groups}
    for index, label in enumerate(labels):
        members_by_group[label].append(index)
    masks: list[int] = []
    series_per_group: list[int] = []
    for group in groups:
        member_indexes = members_by_group[group]
        group_mask = _full_mask(total_cells)
        for member in member_indexes:
            group_mask &= series_masks[member]
        masks.append(group_mask)
        series_per_group.append(len(member_indexes))
    pairs = tuple(
        tuple(_pair_counts(masks[left], masks[right], total_cells) for right in range(len(masks)))
        for left in range(len(masks))
    )
    all_valid = _full_mask(total_cells)
    for mask in masks:
        all_valid &= mask
    # Reconciliation guard: independent series-level and group-level
    # intersections must coincide, and every pair must partition the grid.
    # Non-ok markers (including in_gap) stay unset in every mask, so they can
    # never be counted as valid here.
    assert all_valid == series_common
    for index, mask in enumerate(masks):
        diagonal = pairs[index][index]
        own_missing = total_cells - mask.bit_count()
        assert diagonal.common_cells == mask.bit_count()
        assert diagonal.missing_any_cells == diagonal.both_missing_cells == own_missing
        assert diagonal.left_only_missing_cells == diagonal.right_only_missing_cells == 0
    assert all(
        pair.common_cells
        + pair.left_only_missing_cells
        + pair.right_only_missing_cells
        + pair.both_missing_cells
        == total_cells
        and pair.missing_any_cells
        == pair.left_only_missing_cells + pair.right_only_missing_cells + pair.both_missing_cells
        for row in pairs
        for pair in row
    )
    return PairwiseMatrix(
        total_cells=total_cells,
        groups=groups,
        pairs=pairs,
        device_rollups=_rollups(groups, masks, series_per_group, total_cells, by_device=True),
        metric_rollups=_rollups(groups, masks, series_per_group, total_cells, by_device=False),
    )


def _rollups(
    groups: Sequence[GroupKey],
    masks: Sequence[int],
    series_per_group: Sequence[int],
    total_cells: int,
    *,
    by_device: bool,
) -> tuple[GroupRollup, ...]:
    keys = sorted({group.device_id if by_device else group.metric for group in groups})
    full = _full_mask(total_cells)
    rollups: list[GroupRollup] = []
    for key in keys:
        member_indexes = [
            index
            for index, group in enumerate(groups)
            if (group.device_id if by_device else group.metric) == key
        ]
        group_mask = full
        series_count = 0
        for index in member_indexes:
            group_mask &= masks[index]
            series_count += series_per_group[index]
        others_mask = full
        for index, mask in enumerate(masks):
            if index not in member_indexes:
                others_mask &= mask
        counts = _pair_counts(group_mask, others_mask, total_cells)
        rollups.append(
            GroupRollup(
                key=key,
                series_count=series_count,
                valid_cells=group_mask.bit_count(),
                common_with_all_cells=counts.common_cells,
                missing_any_cells=counts.missing_any_cells,
                group_missing_only_cells=counts.left_only_missing_cells,
                others_missing_only_cells=counts.right_only_missing_cells,
                both_missing_cells=counts.both_missing_cells,
            )
        )
    return tuple(rollups)


def _pair_counts(left_mask: int, right_mask: int, total_cells: int) -> PairCellCounts:
    common = left_mask & right_mask
    common_cells = common.bit_count()
    left_missing = (~left_mask) & _full_mask(total_cells)
    right_missing = (~right_mask) & _full_mask(total_cells)
    return PairCellCounts(
        total_cells=total_cells,
        common_cells=common_cells,
        missing_any_cells=total_cells - common_cells,
        left_only_missing_cells=(left_missing & right_mask).bit_count(),
        right_only_missing_cells=(right_missing & left_mask).bit_count(),
        both_missing_cells=(left_missing & right_missing).bit_count(),
    )


def _validity_mask(samples: Sequence[Sample], total_cells: int) -> int:
    """Bit-set of cells whose marker is exactly ``ok``; in_gap stays unset."""
    mask = 0
    for cell, sample in enumerate(samples[:total_cells]):
        if sample.marker == "ok":
            mask |= 1 << cell
    return mask


def _full_mask(total_cells: int) -> int:
    return (1 << total_cells) - 1
