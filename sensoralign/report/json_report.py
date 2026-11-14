"""Build the stable JSON report object from an alignment result.

The object contains no absolute paths, wall-clock time, random values or
host-specific fields. Two runs over identical inputs and contracts therefore
produce byte-identical JSON after the canonical serializer is applied.

This module is the single definition of canonical JSON serialization: reports
use :func:`canonical_json_bytes`, while ledger sidecar payloads (contract
``snapshot_json`` and event ``detail_json``) use :func:`canonical_json_text`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import SCHEMA_VERSION
from ..core.align import AlignResult
from ..core.offset import OffsetEstimate


def build_report(result: AlignResult) -> dict[str, Any]:
    """Convert an :class:`AlignResult` into the public report structure."""
    events = [event.to_record(index + 1) for index, event in enumerate(result.events)]
    gaps = [
        {
            "series_id": event["series_id"],
            "start_us": event["detail"].get("start_us"),
            "end_us": event["detail"].get("end_us"),
            "duration_us": event["detail"].get("duration_us"),
        }
        for event in events
        if event["code"] == "W_GAP"
    ]
    series_reports = []
    for run in result.runs:
        quality = run.quality
        series_reports.append(
            {
                "series_id": run.series.series_id,
                "device_id": run.series.device_id,
                "metric": run.series.metric,
                "strategy": run.strategy,
                "input": {
                    "path": run.series.input_path,
                    "format": run.series.input_format,
                    "sha256": run.series.fingerprint,
                },
                "points": {
                    "raw": run.series.raw_count,
                    "sanitized": len(run.sanitized.points),
                    "duplicates_removed": run.sanitized.duplicates_removed,
                    "conflicts": quality.conflict_count,
                },
                "clock": _clock_dict(run.offset),
                "quality": {
                    "total_cells": quality.total_cells,
                    "value_cells": quality.value_cells,
                    "coverage": quality.coverage,
                    "gap_cells": quality.gap_cells,
                    "gap_ratio": quality.gap_ratio,
                    "conflict_count": quality.conflict_count,
                    "gap_count": quality.gap_count,
                    "gap_duration_us": quality.gap_duration_us,
                    "marker_counts": dict(quality.marker_counts),
                },
            }
        )
    alignment = result.alignment
    snapshot = result.contract.snapshot()
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "sensoralign",
        "contract": {
            "name": result.contract.name,
            "version": result.contract.version,
            "sha256": snapshot_sha256(snapshot),
            "snapshot": snapshot,
        },
        "grid": {
            "mode": result.grid.mode,
            "origin_us": result.grid.origin_us,
            "step_us": result.grid.step_us,
            "end_us": result.grid.end_us,
            "cell_count": result.grid.cell_count,
        },
        "series": series_reports,
        "alignment": {
            "total_cells": alignment.total_cells,
            "common_cells": alignment.common_cells,
            "missing_any_cells": alignment.missing_any_cells,
            "completion_rate": alignment.completion_rate,
        },
        "offsets": [
            {"device_id": estimate.device_id, **_clock_dict(estimate)}
            for estimate in result.offsets
        ],
        "gaps": gaps,
        "errors": events,
    }


def canonical_json_bytes(report: dict[str, Any]) -> bytes:
    """Serialize a report deterministically as UTF-8 JSON with a final newline."""
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return (text + "\n").encode("utf-8")


def canonical_json_text(value: Any) -> str:
    """Serialize a ledger sidecar payload as deterministic compact JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_json_sha256(text: str) -> str:
    """Return the SHA-256 digest of canonical JSON text encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    """Return the content hash of a contract snapshot in canonical form."""
    return canonical_json_sha256(canonical_json_text(snapshot))


def report_sha256(report: dict[str, Any]) -> str:
    """Return the SHA-256 digest of the canonical report bytes."""
    return hashlib.sha256(canonical_json_bytes(report)).hexdigest()


def run_id_for(contract_sha256: str, result_sha256: str) -> str:
    """Derive a deterministic run identifier from contract and result hashes."""
    payload = f"{contract_sha256}:{result_sha256}".encode()
    return "run-" + hashlib.sha256(payload).hexdigest()[:20]


def _clock_dict(estimate: OffsetEstimate) -> dict[str, Any]:
    return {
        "status": estimate.status,
        "reference_device": estimate.reference_device,
        "anchor_count": estimate.anchor_count,
        "median_offset_us": estimate.median_offset_us,
        "intercept_us": estimate.intercept_us,
        "slope_us_per_s": estimate.slope_us_per_s,
        "residual_median_us": estimate.residual_median_us,
    }
