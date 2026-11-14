"""Cross-input arbitration over clock-corrected observations.

Single-stream sanitize, clock correction and reordering run first and are never
rewritten here. This stage only compares *different* input streams that report
the same ``(device_id, metric)`` at the same corrected microsecond:

* identical values are redundant: one observation survives, every other side
  emits a :class:`~sensoralign.core.errors.Event` attributed to its own series;
* differing values conflict: the stream declaring the highest ``priority``
  survives (an undeclared priority is the lowest, zero); equal priorities
  resolve by contract declaration order, so the outcome is deterministic.

Every removed observation yields exactly one event owned by the series that
lost it. Events and counters never cross streams, and the single-stream
duplicate/conflict policies are not consulted or counted again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .contract_model import InputSpec
from .errors import Event
from .series import Series, SeriesPoint

ARBITRATION_STAGE = "cross_input_arbitration"


@dataclass(frozen=True)
class ArbitrationStats:
    """Per-series arbitration counters (zero when the stream kept every point)."""

    merged: int = 0
    dropped: int = 0


@dataclass
class ArbitrationResult:
    """Post-arbitration point lists, per-series counters and audit events."""

    points_by_series: dict[str, list[SeriesPoint]]
    stats_by_series: dict[str, ArbitrationStats]
    events: list[Event] = field(default_factory=list)


@dataclass(frozen=True)
class _Claim:
    ordinal: int
    series_id: str
    input_path: str
    priority: int
    point: SeriesPoint


def arbitrate_inputs(
    streams: Sequence[tuple[int, InputSpec, Series, list[SeriesPoint]]],
) -> ArbitrationResult:
    """Arbitrate corrected observations across all declared input streams.

    Args:
        streams: Tuples of ``(declaration ordinal, spec, series, corrected
            points)`` in contract declaration order. Each corrected list is
            already ordered and timestamp-unique within its own stream.

    Returns:
        Per-series surviving points (original order preserved), per-series
        merge/drop counters and the audit events owned by the losing series.
    """
    claims: dict[tuple[str, str, int], _Claim] = {}
    removed: set[tuple[str, int]] = set()
    events: list[Event] = []
    merged: dict[str, int] = {}
    dropped: dict[str, int] = {}

    for ordinal, spec, series, points in streams:
        for point in points:
            key = (series.device_id, series.metric, point.ts_us)
            incumbent = claims.get(key)
            if incumbent is None:
                claims[key] = _Claim(ordinal, series.series_id, series.input_path, spec.priority, point)
                continue
            if point.value == incumbent.point.value:
                _record_loss(
                    removed,
                    events,
                    merged,
                    incumbent,
                    series,
                    point,
                    spec,
                    duplicate=True,
                )
                continue
            if spec.priority > incumbent.priority:
                # Priority inversion: the later declaration overtakes the
                # incumbent, which now becomes the audited losing side.
                _evict(removed, events, dropped, incumbent, series, point, spec)
                claims[key] = _Claim(ordinal, series.series_id, series.input_path, spec.priority, point)
            else:
                _record_loss(
                    removed,
                    events,
                    dropped,
                    incumbent,
                    series,
                    point,
                    spec,
                    duplicate=False,
                )

    points_by_series: dict[str, list[SeriesPoint]] = {}
    stats_by_series: dict[str, ArbitrationStats] = {}
    for _ordinal, spec, series, points in streams:
        surviving = [point for point in points if (series.series_id, point.ts_us) not in removed]
        points_by_series[series.series_id] = surviving
        stats_by_series[series.series_id] = ArbitrationStats(
            merged=merged.get(series.series_id, 0),
            dropped=dropped.get(series.series_id, 0),
        )
    return ArbitrationResult(points_by_series, stats_by_series, events)


def _record_loss(
    removed: set[tuple[str, int]],
    events: list[Event],
    counter: dict[str, int],
    winner: _Claim,
    loser_series: Series,
    loser_point: SeriesPoint,
    loser_spec: InputSpec,
    *,
    duplicate: bool,
) -> None:
    removed.add((loser_series.series_id, loser_point.ts_us))
    counter[loser_series.series_id] = counter.get(loser_series.series_id, 0) + 1
    if duplicate:
        code = "W_ARBITRATION_DUPLICATE"
        message = (
            f"redundant cross-input observation at ts_us {loser_point.ts_us} "
            f"merged into series {winner.series_id}"
        )
    else:
        code = "W_ARBITRATION_CONFLICT"
        message = (
            f"cross-input conflict at ts_us {loser_point.ts_us}; "
            f"series {winner.series_id} kept by priority"
        )
    events.append(
        Event(
            code,
            "warning",
            ARBITRATION_STAGE,
            message,
            series_id=loser_series.series_id,
            path=loser_series.input_path,
            line=loser_point.line,
            field="value",
            detail={
                "ts_us": loser_point.ts_us,
                "device_id": loser_series.device_id,
                "metric": loser_series.metric,
                "winner_series_id": winner.series_id,
                "kept_value": winner.point.value,
                "dropped_value": loser_point.value,
                "winner_priority": winner.priority,
                "loser_priority": loser_spec.priority,
            },
        )
    )


def _evict(
    removed: set[tuple[str, int]],
    events: list[Event],
    counter: dict[str, int],
    incumbent: _Claim,
    challenger_series: Series,
    challenger_point: SeriesPoint,
    challenger_spec: InputSpec,
) -> None:
    # The incumbent loses; the event keeps the incumbent's own provenance.
    removed.add((incumbent.series_id, incumbent.point.ts_us))
    counter[incumbent.series_id] = counter.get(incumbent.series_id, 0) + 1
    events.append(
        Event(
            "W_ARBITRATION_CONFLICT",
            "warning",
            ARBITRATION_STAGE,
            f"cross-input conflict at ts_us {incumbent.point.ts_us}; "
            f"series {challenger_series.series_id} kept by priority",
            series_id=incumbent.series_id,
            path=incumbent.input_path,
            line=incumbent.point.line,
            field="value",
            detail={
                "ts_us": incumbent.point.ts_us,
                "device_id": challenger_series.device_id,
                "metric": challenger_series.metric,
                "winner_series_id": challenger_series.series_id,
                "kept_value": challenger_point.value,
                "dropped_value": incumbent.point.value,
                "winner_priority": challenger_spec.priority,
                "loser_priority": incumbent.priority,
            },
        )
    )
