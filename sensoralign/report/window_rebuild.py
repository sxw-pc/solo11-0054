"""Window-metric reconstruction for ledgers written before the window layer.

New ledgers store every window key in ``metrics``. Older ledgers predate those
rows but still carry per-cell ``aligned`` markers, so their windows are
recomputed from those rows. If neither source is available, the window
degrades to an empty one (``None`` endpoints, zero coverage) rather than
failing the whole report with ``E_LEDGER_CORRUPT``.
"""

from __future__ import annotations

from typing import Any

from ..core.quality import (
    ALIGNMENT_WINDOW_METRICS,
    SERIES_WINDOW_METRICS,
    WindowSpan,
    compute_alignment_quality,
    compute_main_window,
)
from ..core.resample import Sample


class WindowRebuilder:
    """Resolve window metrics from typed metric rows or aligned markers."""

    def __init__(
        self,
        bundle: dict[str, Any],
        snapshot: dict[str, Any],
        metrics: dict[tuple[str, str, str], Any],
    ) -> None:
        self._bundle = bundle
        self._metrics = metrics
        self._cap_value = snapshot.get("coverage_window_us")
        self._samples: dict[str, list[Sample]] | None = None
        self._grid_times: tuple[int, ...] | None = None
        self._grid_loaded = False

    def series_window(self, series_id: str) -> dict[str, Any]:
        """Return the window block for one series."""
        stored = self._stored("series", series_id, SERIES_WINDOW_METRICS)
        if stored is not None:
            return _window_dict(stored)
        return _window_dict(
            compute_main_window(
                self._series_samples().get(series_id, []),
                coverage_window_us=self._cap(),
                grid_times=self._times(),
            )
        )

    def alignment_window(self) -> dict[str, Any]:
        """Return the intersection block for all series."""
        stored = self._stored("alignment", "", ALIGNMENT_WINDOW_METRICS)
        if stored is not None:
            return _alignment_window_dict(stored)
        grouped = self._series_samples()
        cell_count = int(self._metric_row("grid", "", "cell_count")["metric_value"])
        sample_sets = [grouped.get(row["series_id"], []) for row in self._bundle["series"]]
        if any(len(samples) != cell_count for samples in sample_sets):
            return _empty_alignment_window()
        quality = compute_alignment_quality(
            sample_sets,
            cell_count,
            coverage_window_us=self._cap(),
            grid_times=self._times(),
        )
        return _alignment_window_dict(
            {
                "intersection_start_us": quality.intersection_start_us,
                "intersection_end_us": quality.intersection_end_us,
                "intersection_cells": quality.intersection_cells,
                "intersection_common_cells": quality.intersection_common_cells,
                "intersection_completion_rate": quality.intersection_completion_rate,
            }
        )

    def _stored(self, scope: str, scope_id: str, keys: tuple[str, ...]) -> dict[str, Any] | None:
        rows = [self._metrics.get((scope, scope_id, key)) for key in keys]
        if any(row is None for row in rows):
            return None
        return {key: row["metric_value"] for key, row in zip(keys, rows) if row is not None}

    def _metric_row(self, scope: str, scope_id: str, key: str) -> dict[str, Any]:
        row = self._metrics.get((scope, scope_id, key))
        if row is None:
            return {"metric_value": 0.0}
        return row

    def _cap(self) -> int | None:
        return None if self._cap_value is None else int(self._cap_value)

    def _times(self) -> tuple[int, ...] | None:
        if not self._grid_loaded:
            self._grid_loaded = True
            origin = int(self._metric_row("grid", "", "origin_us")["metric_value"])
            step = int(self._metric_row("grid", "", "step_us")["metric_value"])
            count = int(self._metric_row("grid", "", "cell_count")["metric_value"])
            if step > 0 and count > 0:
                self._grid_times = tuple(origin + index * step for index in range(count))
        return self._grid_times

    def _series_samples(self) -> dict[str, list[Sample]]:
        if self._samples is None:
            grouped: dict[str, list[Sample]] = {}
            for row in self._bundle.get("aligned", []):
                grouped.setdefault(row["series_id"], []).append(Sample(None, row["marker"]))
            self._samples = grouped
        return self._samples


def _window_dict(window: Any) -> dict[str, Any]:
    if isinstance(window, WindowSpan):
        return {
            "main_window_start_us": window.start_us,
            "main_window_end_us": window.end_us,
            "main_window_span_us": window.span_us,
            "main_window_cells": window.cells,
            "main_window_value_cells": window.value_cells,
            "main_window_coverage": window.coverage,
        }
    return {
        "main_window_start_us": _nullable_int(window["main_window_start_us"]),
        "main_window_end_us": _nullable_int(window["main_window_end_us"]),
        "main_window_span_us": int(window["main_window_span_us"]),
        "main_window_cells": int(window["main_window_cells"]),
        "main_window_value_cells": int(window["main_window_value_cells"]),
        "main_window_coverage": float(window["main_window_coverage"]),
    }


def _alignment_window_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "intersection_start_us": _nullable_int(values["intersection_start_us"]),
        "intersection_end_us": _nullable_int(values["intersection_end_us"]),
        "intersection_cells": int(values["intersection_cells"]),
        "intersection_common_cells": int(values["intersection_common_cells"]),
        "intersection_completion_rate": float(values["intersection_completion_rate"]),
    }


def _empty_alignment_window() -> dict[str, Any]:
    return {
        "intersection_start_us": None,
        "intersection_end_us": None,
        "intersection_cells": 0,
        "intersection_common_cells": 0,
        "intersection_completion_rate": 0.0,
    }


def _nullable_int(value: float | None) -> int | None:
    return None if value is None else int(value)
