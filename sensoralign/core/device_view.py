"""Device-level wide view over metrics sharing one grid.

A device may declare several metric streams in the contract.  The ledger stores
them flattened by series, so downstream consumers would otherwise have to join
rows back on ``grid_index``.  This module projects those series onto one row per
device and one cell per grid instant, keeping each metric's own resampling
marker (``no_left``, ``in_gap`` and so on) intact.

The projection is a view only: it never changes the per-series samples used by
``alignment.completion_rate``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import LedgerError
from .resample import Sample

if TYPE_CHECKING:  # Avoid an import cycle: align.py imports this module.
    from .align import GridInfo, SeriesRun


@dataclass(frozen=True)
class DeviceColumn:
    """One declared metric column in a device view."""

    metric: str
    series_id: str
    strategy: str
    key: str


@dataclass(frozen=True)
class DeviceCell:
    """One grid instant for one device.

    ``samples`` is positionally aligned with :attr:`DeviceView.columns`, so a
    metric missing at this cell keeps its own marker instead of gaining a
    synthetic value or an empty marker.
    """

    grid_index: int
    ts_us: int
    samples: tuple[Sample, ...]


@dataclass(frozen=True)
class DeviceView:
    """All declared metrics of one device over the common grid."""

    device_id: str
    columns: tuple[DeviceColumn, ...]
    cells: tuple[DeviceCell, ...]


def build_device_views(runs: Sequence[SeriesRun], grid: GridInfo) -> tuple[DeviceView, ...]:
    """Group series runs by ``device_id`` without changing their samples.

    Devices follow first appearance in contract input order, and columns follow
    declaration order within each device.  A column key is the metric name; when
    a device declares the same metric more than once, colliding columns use
    ``metric@series_id`` so no column can silently overwrite another.
    """
    device_ids: list[str] = []
    for run in runs:
        if run.series.device_id not in device_ids:
            device_ids.append(run.series.device_id)

    metric_counts: dict[str, dict[str, int]] = {device_id: {} for device_id in device_ids}
    for run in runs:
        counts = metric_counts[run.series.device_id]
        counts[run.series.metric] = counts.get(run.series.metric, 0) + 1

    columns_by_device: dict[str, list[DeviceColumn]] = {device_id: [] for device_id in device_ids}
    runs_by_device: dict[str, list[SeriesRun]] = {device_id: [] for device_id in device_ids}
    for run in runs:
        device_id = run.series.device_id
        metric = run.series.metric
        key = metric if metric_counts[device_id][metric] == 1 else f"{metric}@{run.series.series_id}"
        columns_by_device[device_id].append(DeviceColumn(metric, run.series.series_id, run.strategy, key))
        runs_by_device[device_id].append(run)

    views: list[DeviceView] = []
    for device_id in device_ids:
        columns = columns_by_device[device_id]
        column_runs = runs_by_device[device_id]
        cells = tuple(
            DeviceCell(index, ts_us, tuple(run.samples[index] for run in column_runs))
            for index, ts_us in enumerate(grid.times)
        )
        views.append(DeviceView(device_id, tuple(columns), cells))
    return tuple(views)


def build_views_from_ledger(
    series_rows: Sequence[Mapping[str, Any]],
    aligned_rows: Sequence[Mapping[str, Any]],
    cell_count: int,
) -> tuple[DeviceView, ...]:
    """Rebuild device views from the ledger's ``series`` and ``aligned`` tables.

    Only five-table data is used.  Missing cells or columns whose timestamps
    disagree are reported as ``E_LEDGER_CORRUPT`` rather than filled.
    """
    raster: dict[str, dict[int, tuple[int, float | None, str]]] = {}
    for row in aligned_rows:
        raster.setdefault(row["series_id"], {})[int(row["grid_index"])] = (
            int(row["ts_us"]),
            row["value"],
            row["marker"],
        )
    metric_counts: dict[str, dict[str, int]] = {}
    for row in series_rows:
        counts = metric_counts.setdefault(row["device_id"], {})
        counts[row["metric"]] = counts.get(row["metric"], 0) + 1

    ordered_devices: list[str] = []
    columns_by_device: dict[str, list[tuple[DeviceColumn, str]]] = {}
    for row in series_rows:
        device_id = row["device_id"]
        if device_id not in columns_by_device:
            ordered_devices.append(device_id)
            columns_by_device[device_id] = []
        metric = row["metric"]
        key = metric if metric_counts[device_id][metric] == 1 else f"{metric}@{row['series_id']}"
        columns_by_device[device_id].append(
            (DeviceColumn(metric, row["series_id"], row["strategy"], key), row["series_id"])
        )

    views: list[DeviceView] = []
    for device_id in ordered_devices:
        columns = columns_by_device[device_id]
        cells: list[DeviceCell] = []
        for index in range(cell_count):
            samples: list[Sample] = []
            ts_us: int | None = None
            for column, series_id in columns:
                entry = raster.get(series_id, {}).get(index)
                if entry is None:
                    raise LedgerError(
                        "E_LEDGER_CORRUPT",
                        f"missing aligned row for series {series_id} at grid index {index}",
                    )
                row_ts, value, marker = entry
                if ts_us is None:
                    ts_us = row_ts
                elif row_ts != ts_us:
                    raise LedgerError(
                        "E_LEDGER_CORRUPT",
                        f"grid index {index} has inconsistent timestamps across series",
                    )
                samples.append(Sample(value, marker))
            if ts_us is None:  # A device always has at least one declared column.
                raise LedgerError("E_LEDGER_CORRUPT", f"device {device_id} has no series columns")
            cells.append(DeviceCell(index, ts_us, tuple(samples)))
        views.append(DeviceView(device_id, tuple(column for column, _sid in columns), tuple(cells)))
    return tuple(views)


def device_records(views: Sequence[DeviceView]) -> list[dict[str, Any]]:
    """Render device views as the stable wide shape used in JSON reports.

    Each cell carries one ``values`` and one ``markers`` entry per column key.
    A non-``ok`` marker always keeps its ledger value (``null``) and its
    descriptive marker; nothing is replaced by an empty string or a guess.
    """
    records: list[dict[str, Any]] = []
    for view in views:
        keys = [column.key for column in view.columns]
        records.append(
            {
                "device_id": view.device_id,
                "columns": [
                    {
                        "key": column.key,
                        "metric": column.metric,
                        "series_id": column.series_id,
                        "strategy": column.strategy,
                    }
                    for column in view.columns
                ],
                "cells": [
                    {
                        "grid_index": cell.grid_index,
                        "ts_us": cell.ts_us,
                        "values": {key: sample.value for key, sample in zip(keys, cell.samples)},
                        "markers": {key: sample.marker for key, sample in zip(keys, cell.samples)},
                    }
                    for cell in view.cells
                ],
            }
        )
    return records
