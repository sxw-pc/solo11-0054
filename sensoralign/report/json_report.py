"""Build the stable JSON report object from an alignment result.

The object contains no absolute paths, wall-clock time, random values or
host-specific fields. Two runs over identical inputs and contracts therefore
produce byte-identical JSON after the canonical serializer is applied.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import SCHEMA_VERSION
from ..core.align import AlignResult
from ..core.offset import OffsetEstimate
from ..core.quality import GroupRollup, PairCellCounts, PairwiseMatrix


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
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "sensoralign",
        "contract": {
            "name": result.contract.name,
            "version": result.contract.version,
            "sha256": result.contract.source_sha256,
            "snapshot": result.contract.snapshot(),
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
            "matrix": _matrix_dict(result.matrix),
            "device_groups": _rollup_list(result.matrix.device_rollups),
            "metric_groups": _rollup_list(result.matrix.metric_rollups),
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


def report_sha256(report: dict[str, Any]) -> str:
    """Return the SHA-256 digest of the canonical report bytes."""
    return hashlib.sha256(canonical_json_bytes(report)).hexdigest()


def run_id_for(contract_sha256: str, result_sha256: str) -> str:
    """Derive a deterministic run identifier from contract and result hashes."""
    payload = f"{contract_sha256}:{result_sha256}".encode()
    return "run-" + hashlib.sha256(payload).hexdigest()[:20]


def _matrix_dict(matrix: PairwiseMatrix) -> dict[str, Any]:
    groups = [
        {"device_id": group.device_id, "metric": group.metric}
        for group in matrix.groups
    ]
    pairs = [
        [_pair_dict(pair) for pair in row]
        for row in matrix.pairs
    ]
    return {
        "total_cells": matrix.total_cells,
        "groups": groups,
        "pairs": pairs,
    }


def _pair_dict(pair: PairCellCounts) -> dict[str, Any]:
    return {
        "total_cells": pair.total_cells,
        "common_cells": pair.common_cells,
        "missing_any_cells": pair.missing_any_cells,
        "left_only_missing_cells": pair.left_only_missing_cells,
        "right_only_missing_cells": pair.right_only_missing_cells,
        "both_missing_cells": pair.both_missing_cells,
    }


def _rollup_list(rollups: tuple[GroupRollup, ...]) -> list[dict[str, Any]]:
    return [
        {
            "key": rollup.key,
            "series_count": rollup.series_count,
            "valid_cells": rollup.valid_cells,
            "common_with_all_cells": rollup.common_with_all_cells,
            "missing_any_cells": rollup.missing_any_cells,
            "group_missing_only_cells": rollup.group_missing_only_cells,
            "others_missing_only_cells": rollup.others_missing_only_cells,
            "both_missing_cells": rollup.both_missing_cells,
        }
        for rollup in rollups
    ]


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
