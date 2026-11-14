"""Reconstruct the canonical report object from SQLite ledger rows.

The ledger stores enough typed metrics and ordered events for Markdown and JSON
reporting to be replayed without access to the original contract or data files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..core.errors import LedgerError
from ..core.resample import MARKER_ORDER
from ..io.ledger import read_run_bundle


def load_report_from_db(db_path: Path, run_id: str | None = None) -> dict[str, Any]:
    """Read one run and rebuild the exact report structure."""
    return rebuild_report(read_run_bundle(db_path, run_id))


def rebuild_report(bundle: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the report structure from a five-table run bundle.

    Raises:
        LedgerError: ``E_LEDGER_CORRUPT`` when a required metric is absent.
    """
    metrics = {(row["scope"], row["scope_id"], row["metric_key"]): row for row in bundle["metrics"]}
    snapshot = json.loads(_text(metrics, "contract", "", "snapshot_json"))
    run = bundle["run"]
    series_reports = []
    for row in bundle["series"]:
        series_id = row["series_id"]
        marker_counts = {
            marker: _int_metric(metrics, "series", series_id, f"marker.{marker}")
            for marker in MARKER_ORDER
        }
        series_reports.append(
            {
                "series_id": series_id,
                "device_id": row["device_id"],
                "metric": row["metric"],
                "strategy": row["strategy"],
                "input": {
                    "path": row["input_path"],
                    "format": row["input_format"],
                    "sha256": row["input_sha256"],
                },
                "points": {
                    "raw": row["raw_points"],
                    "sanitized": row["sanitized_points"],
                    "duplicates_removed": row["duplicates_removed"],
                    "conflicts": row["conflict_count"],
                },
                "clock": {
                    "status": row["clock_status"],
                    "reference_device": row["clock_reference"],
                    "anchor_count": row["anchor_count"],
                    "median_offset_us": row["median_offset_us"],
                    "intercept_us": row["intercept_us"],
                    "slope_us_per_s": row["slope_us_per_s"],
                    "residual_median_us": row["residual_median_us"],
                },
                "quality": {
                    "total_cells": _int_metric(metrics, "series", series_id, "total_cells"),
                    "value_cells": _int_metric(metrics, "series", series_id, "value_cells"),
                    "coverage": _float_metric(metrics, "series", series_id, "coverage"),
                    "gap_cells": _int_metric(metrics, "series", series_id, "gap_cells"),
                    "gap_ratio": _float_metric(metrics, "series", series_id, "gap_ratio"),
                    "conflict_count": _int_metric(metrics, "series", series_id, "conflict_count"),
                    "gap_count": _int_metric(metrics, "series", series_id, "gap_count"),
                    "gap_duration_us": _int_metric(metrics, "series", series_id, "gap_duration_us"),
                    "marker_counts": marker_counts,
                },
            }
        )
    errors = [_error_record(row) for row in bundle["errors"]]
    gaps = [
        {
            "series_id": event["series_id"],
            "start_us": event["detail"].get("start_us"),
            "end_us": event["detail"].get("end_us"),
            "duration_us": event["detail"].get("duration_us"),
        }
        for event in errors
        if event["code"] == "W_GAP"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "sensoralign",
        "contract": {
            "name": snapshot["name"],
            "version": snapshot["version"],
            "sha256": run["contract_sha256"],
            "snapshot": snapshot,
        },
        "grid": {
            "mode": _text(metrics, "grid", "", "mode"),
            "origin_us": _int_metric(metrics, "grid", "", "origin_us"),
            "step_us": _int_metric(metrics, "grid", "", "step_us"),
            "end_us": _int_metric(metrics, "grid", "", "end_us"),
            "cell_count": _int_metric(metrics, "grid", "", "cell_count"),
        },
        "series": series_reports,
        "alignment": {
            "total_cells": _int_metric(metrics, "alignment", "", "total_cells"),
            "common_cells": _int_metric(metrics, "alignment", "", "common_cells"),
            "missing_any_cells": _int_metric(metrics, "alignment", "", "missing_any_cells"),
            "completion_rate": _float_metric(metrics, "alignment", "", "completion_rate"),
            "matrix": json.loads(_text(metrics, "alignment", "", "matrix")),
            "device_groups": json.loads(_text(metrics, "alignment", "", "device_groups")),
            "metric_groups": json.loads(_text(metrics, "alignment", "", "metric_groups")),
        },
        "offsets": _offsets(metrics),
        "gaps": gaps,
        "errors": errors,
    }


def _offsets(metrics: dict[tuple[str, str, str], Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for (scope, scope_id, key), row in metrics.items():
        if scope != "offset":
            continue
        grouped.setdefault(scope_id, {})[key] = row
    ordered = sorted(grouped.items(), key=lambda item: int(item[1]["ordinal"]["metric_value"]))
    result = []
    for device_id, values in ordered:
        result.append(
            {
                "device_id": device_id,
                "status": values["status"]["metric_text"],
                "reference_device": values["reference_device"]["metric_text"],
                "anchor_count": int(values["anchor_count"]["metric_value"]),
                "median_offset_us": _nullable_int(values["median_offset_us"]["metric_value"]),
                "intercept_us": _nullable_float(values["intercept_us"]["metric_value"]),
                "slope_us_per_s": _nullable_float(values["slope_us_per_s"]["metric_value"]),
                "residual_median_us": _nullable_float(values["residual_median_us"]["metric_value"]),
            }
        )
    return result


def _error_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "code": row["code"],
        "severity": row["severity"],
        "stage": row["stage"],
        "series_id": row["series_id"],
        "path": row["path"],
        "line": row["line"],
        "field": row["field"],
        "message": row["message"],
        "detail": json.loads(row["detail_json"]),
    }


def _text(metrics: dict[tuple[str, str, str], Any], scope: str, scope_id: str, key: str) -> str:
    row = metrics.get((scope, scope_id, key))
    if row is None or row["metric_text"] is None:
        raise LedgerError("E_LEDGER_CORRUPT", f"missing metric {scope}/{scope_id}/{key}")
    return row["metric_text"]


def _float_metric(metrics: dict[tuple[str, str, str], Any], scope: str, scope_id: str, key: str) -> float:
    row = metrics.get((scope, scope_id, key))
    if row is None or row["metric_value"] is None:
        raise LedgerError("E_LEDGER_CORRUPT", f"missing metric {scope}/{scope_id}/{key}")
    return float(row["metric_value"])


def _int_metric(metrics: dict[tuple[str, str, str], Any], scope: str, scope_id: str, key: str) -> int:
    return int(_float_metric(metrics, scope, scope_id, key))


def _nullable_int(value: float | None) -> int | None:
    return None if value is None else int(value)


def _nullable_float(value: float | None) -> float | None:
    return None if value is None else float(value)
