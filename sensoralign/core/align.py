"""Alignment engine: load, sanitize, correct clocks, build grid and resample.

The engine is deliberately batch-only and deterministic. Every transformation
that removes or marks a point produces an :class:`Event`, and the result carries
the raw input fingerprints through to the report and ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..io.loaders import load_series
from .contract_model import Contract, GridSpec, InputSpec
from .errors import DataError, Event
from .offset import OffsetEstimate, correct_points, estimate_offsets, load_anchors
from .quality import (
    AlignmentQuality,
    SeriesQuality,
    compute_alignment_quality,
    compute_series_quality,
)
from .resample import Sample, resample_points
from .series import Series, SeriesPoint
from .timestamps import (
    Gap,
    SanitizeResult,
    count_inversions,
    find_gaps,
    min_interval_us,
    sanitize_points,
)

MAX_GRID_CELLS = 1_000_000


@dataclass(frozen=True)
class GridInfo:
    """The resolved common time axis."""

    mode: str
    origin_us: int
    step_us: int
    end_us: int
    times: tuple[int, ...]

    @property
    def cell_count(self) -> int:
        """Number of grid instants."""
        return len(self.times)


@dataclass
class SeriesRun:
    """All intermediate and final state for one input stream."""

    spec: InputSpec
    series: Series
    sanitized: SanitizeResult
    corrected_points: list[SeriesPoint]
    gaps: list[Gap]
    offset: OffsetEstimate
    strategy: str
    samples: list[Sample]
    quality: SeriesQuality


@dataclass
class AlignResult:
    """Complete in-memory result of one contract execution."""

    contract: Contract
    grid: GridInfo
    runs: list[SeriesRun]
    alignment: AlignmentQuality
    offsets: tuple[OffsetEstimate, ...]
    events: list[Event]


def align_contract(contract: Contract) -> AlignResult:
    """Execute every stage of an alignment contract.

    Raises:
        DataError: on unreadable input, invalid rows, missing anchors, an
            impossible union grid or a grid larger than 1,000,000 cells.
    """
    anchors = load_anchors(contract.resolve_path(contract.anchors_path)) if contract.anchors_path else None
    loaded: list[tuple[InputSpec, Series, SanitizeResult]] = []
    events: list[Event] = []
    for spec in contract.inputs:
        resolved = contract.resolve_path(spec.path)
        shown_path = _display_path(resolved, contract.project_root)
        series = load_series(
            resolved,
            series_id=spec.series_id,
            fmt=spec.format,
            expected_device=spec.device_id,
            expected_metric=spec.metric,
            display_path=shown_path,
        )
        sanitized = sanitize_points(
            series.points,
            monotonic_policy=contract.monotonic_policy,
            duplicate_policy=contract.duplicate_policy,
            conflict_policy=contract.conflict_policy,
            series_id=series.series_id,
            path=series.input_path,
        )
        events.extend(sanitized.events)
        loaded.append((spec, series, sanitized))

    devices = [series.device_id for _spec, series, _san in loaded]
    if anchors is not None:
        estimates = estimate_offsets(anchors, devices, contract.clock_skew)
    else:
        estimates = {
            device: OffsetEstimate(device, "", "none", "disabled", 0)
            for device in devices
        }

    prepared: list[tuple[InputSpec, Series, SanitizeResult, list[SeriesPoint], OffsetEstimate]] = []
    for spec, series, sanitized in loaded:
        estimate = estimates[series.device_id]
        corrected = correct_points(sanitized.points, estimate) if estimate.status == "ok" else list(sanitized.points)
        if contract.clock_skew != "none" and estimate.status not in ("ok", "reference"):
            events.append(
                Event(
                    "W_OFFSET_INSUFFICIENT",
                    "warning",
                    "clock_offset",
                    f"clock correction not applied: status {estimate.status} with {estimate.anchor_count} anchors",
                    series_id=series.series_id,
                    path=series.input_path,
                    field="clock_skew",
                    detail={"status": estimate.status, "anchor_count": estimate.anchor_count},
                )
            )
        corrected = _normalize_corrected(corrected, series, events)
        prepared.append((spec, series, sanitized, corrected, estimate))

    grid = build_grid(contract.grid, [corrected for _s, _r, _sa, corrected, _o in prepared])
    runs: list[SeriesRun] = []
    gaps_for_events: list[Event] = []
    for spec, series, sanitized, corrected, estimate in prepared:
        gaps = find_gaps(corrected, contract.max_gap_us)
        for gap in gaps:
            gaps_for_events.append(
                Event(
                    "W_GAP",
                    "warning",
                    "gap_scan",
                    f"gap of {gap.duration_us} us from {gap.start_us} to {gap.end_us}",
                    series_id=series.series_id,
                    path=series.input_path,
                    detail={"start_us": gap.start_us, "end_us": gap.end_us, "duration_us": gap.duration_us},
                )
            )
        strategy = contract.resample.strategy_for(spec.series_id)
        samples = resample_points(
            corrected,
            grid.times,
            strategy,
            max_gap_us=contract.max_gap_us,
            gaps=gaps,
            step_us=grid.step_us,
        )
        quality = compute_series_quality(
            samples,
            gaps,
            sanitized.conflict_count,
            coverage_window_us=contract.coverage_window_us,
            grid_times=grid.times,
        )
        runs.append(SeriesRun(spec, series, sanitized, corrected, gaps, estimate, strategy, samples, quality))
    events.extend(gaps_for_events)
    alignment = compute_alignment_quality(
        [run.samples for run in runs],
        grid.cell_count,
        coverage_window_us=contract.coverage_window_us,
        grid_times=grid.times,
    )
    return AlignResult(
        contract=contract,
        grid=grid,
        runs=runs,
        alignment=alignment,
        offsets=tuple(estimates.values()),
        events=events,
    )


def build_grid(grid_spec: GridSpec, corrected_series: Sequence[Sequence[SeriesPoint]]) -> GridInfo:
    """Resolve an explicit grid or the common coverage window.

    For ``from_union`` the origin is the latest first observation, the end is
    the earliest last observation, and the step is the minimum positive native
    interval across all inputs. The stored ``end_us`` is exclusive.
    """
    if grid_spec.mode == "from_union":
        nonempty = [points for points in corrected_series if points]
        if len(nonempty) != len(corrected_series) or not nonempty:
            raise DataError("E_GRID_NO_OVERLAP", "from_union requires at least one point in every series")
        common_start = max(points[0].ts_us for points in nonempty)
        common_end = min(points[-1].ts_us for points in nonempty)
        if common_start > common_end:
            raise DataError("E_GRID_NO_OVERLAP", "input coverage windows do not overlap")
        intervals = [min_interval_us(points) for points in nonempty]
        positive = [interval for interval in intervals if interval is not None]
        if not positive:
            raise DataError("E_GRID_NO_STEP", "cannot infer a union-grid step from single-point series")
        step = min(positive)
        end = common_end + 1
        count = (common_end - common_start) // step + 1
        if count > MAX_GRID_CELLS:
            raise DataError("E_GRID_TOO_LARGE", "resolved grid exceeds 1,000,000 cells")
        times = tuple(common_start + index * step for index in range(count))
        return GridInfo("from_union", common_start, step, end, times)

    origin = int(grid_spec.origin_us)  # type: ignore[arg-type]
    step = int(grid_spec.step_us)  # type: ignore[arg-type]
    end = int(grid_spec.end_us)  # type: ignore[arg-type]
    count = (end - origin + step - 1) // step
    if count <= 0:
        raise DataError("E_GRID_EMPTY", "explicit grid contains no cells")
    if count > MAX_GRID_CELLS:
        raise DataError("E_GRID_TOO_LARGE", "resolved grid exceeds 1,000,000 cells")
    times = tuple(range(origin, end, step))
    return GridInfo("explicit", origin, step, end, times)


def _normalize_corrected(points: list[SeriesPoint], series: Series, events: list[Event]) -> list[SeriesPoint]:
    if count_inversions(points) > 0:
        events.append(
            Event(
                "W_CLOCK_REORDER",
                "warning",
                "clock_correction",
                "clock correction reordered timestamps; stable sort applied",
                series_id=series.series_id,
                path=series.input_path,
            )
        )
        points = sorted(points, key=lambda point: point.ts_us)
    unique: list[SeriesPoint] = []
    for point in points:
        if unique and point.ts_us == unique[-1].ts_us:
            events.append(
                Event(
                    "W_CLOCK_DUPLICATE_TS",
                    "warning",
                    "clock_correction",
                    f"clock correction collapsed a timestamp at {point.ts_us}; first kept",
                    series_id=series.series_id,
                    path=series.input_path,
                    detail={"ts_us": point.ts_us},
                )
            )
            continue
        unique.append(point)
    return unique


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.name
