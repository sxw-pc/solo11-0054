"""Offline CSV and JSONL loaders with precise source diagnostics.

Each input file is read once, fingerprinted with SHA-256, and validated with
stable error codes. Required fields are ``ts_us``, ``device_id``, ``metric``
and ``value``; extra fields are ignored. Timestamps must be integer
microseconds and values must be finite numbers.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

from ..core.errors import DataError
from ..core.series import Series, SeriesPoint
from ..core.timestamps import parse_ts_us

REQUIRED_FIELDS = ("ts_us", "device_id", "metric", "value")
SUPPORTED_FORMATS = ("csv", "jsonl")


def file_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of a file's raw bytes."""
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DataError("E_INPUT_READ", f"cannot read input file: {exc}", path=path.name) from exc
    return hashlib.sha256(payload).hexdigest()


def infer_format(path: Path, explicit: str | None = None) -> str:
    """Resolve and validate the input format from an explicit value or suffix."""
    if explicit is not None:
        normalized = explicit.strip().lower()
    else:
        suffix = path.suffix.lower()
        normalized = {"csv": "csv", ".csv": "csv", "jsonl": "jsonl", ".jsonl": "jsonl", ".ndjson": "jsonl"}.get(
            suffix, ""
        )
    if normalized not in SUPPORTED_FORMATS:
        raise DataError("E_FORMAT", f"unsupported input format: {explicit or path.suffix}", path=path.name, field="format")
    return normalized


def load_series(
    path: Path,
    *,
    series_id: str,
    fmt: str | None = None,
    expected_device: str | None = None,
    expected_metric: str | None = None,
    display_path: str | None = None,
) -> Series:
    """Load exactly one device/metric stream from a CSV or JSONL file.

    Args:
        path: Resolved local path. No network URLs are accepted.
        series_id: Stable identifier supplied by the contract's input entry.
        fmt: ``csv``, ``jsonl`` or ``None`` (infer from the suffix).
        expected_device: When set, every row must carry this device id.
        expected_metric: When set, every row must carry this metric.
        display_path: Relative path used in reports; defaults to the file name.

    Returns:
        A series containing raw points in source order plus its SHA-256
        fingerprint.

    Raises:
        DataError: ``E_INPUT_READ``, ``E_ENCODING``, ``E_JSON``,
            ``E_COLUMN_MISSING``, ``E_FIELD_MISSING``, ``E_FIELD_EMPTY``,
            ``E_DEVICE_MISMATCH``, ``E_METRIC_MISMATCH``, ``E_VALUE_*``,
            ``E_TS_*`` or ``E_EMPTY_INPUT``.
    """
    resolved_format = infer_format(path, fmt)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise DataError("E_INPUT_READ", f"cannot read input file: {exc}", path=display_path or path.name) from exc
    fingerprint = hashlib.sha256(raw_bytes).hexdigest()
    shown_path = display_path or path.name
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataError("E_ENCODING", "input is not valid UTF-8", path=shown_path) from exc

    if resolved_format == "csv":
        rows = _csv_rows(text, shown_path)
    else:
        rows = _jsonl_rows(text, shown_path)

    groups: dict[tuple[str, str], list[SeriesPoint]] = {}
    for line, row in rows:
        device_id = _text_field(row, "device_id", shown_path, line)
        metric = _text_field(row, "metric", shown_path, line)
        if expected_device is not None and device_id != expected_device:
            raise DataError(
                "E_DEVICE_MISMATCH",
                f"expected device_id {expected_device!r}, found {device_id!r}",
                path=shown_path,
                line=line,
                field="device_id",
            )
        if expected_metric is not None and metric != expected_metric:
            raise DataError(
                "E_METRIC_MISMATCH",
                f"expected metric {expected_metric!r}, found {metric!r}",
                path=shown_path,
                line=line,
                field="metric",
            )
        ts_us = parse_ts_us(_required(row, "ts_us", shown_path, line), path=shown_path, line=line)
        value = _value_field(row, shown_path, line)
        groups.setdefault((device_id, metric), []).append(SeriesPoint(ts_us, value, line))

    if not groups:
        raise DataError("E_EMPTY_INPUT", "input contains no observations", path=shown_path)
    if len(groups) != 1:
        raise DataError(
            "E_SERIES_AMBIGUOUS",
            "input file contains multiple device/metric streams; split the file",
            path=shown_path,
            detail={"streams": [f"{device}/{metric}" for device, metric in groups]},
        )
    (device_id, metric), points = next(iter(groups.items()))
    return Series(
        series_id=series_id,
        device_id=device_id,
        metric=metric,
        points=points,
        input_path=shown_path,
        input_format=resolved_format,
        fingerprint=fingerprint,
    )


def _required(row: dict[str, Any], field: str, path: str, line: int) -> Any:
    if field not in row or row[field] is None:
        raise DataError("E_FIELD_MISSING", f"missing required field {field!r}", path=path, line=line, field=field)
    return row[field]


def _text_field(row: dict[str, Any], field: str, path: str, line: int) -> str:
    raw = _required(row, field, path, line)
    if not isinstance(raw, str):
        raise DataError("E_FIELD_TYPE", f"field {field!r} must be text", path=path, line=line, field=field)
    value = raw.strip()
    if not value:
        raise DataError("E_FIELD_EMPTY", f"field {field!r} is empty", path=path, line=line, field=field)
    return value


def _value_field(row: dict[str, Any], path: str, line: int) -> float:
    raw = _required(row, "value", path, line)
    if isinstance(raw, bool):
        raise DataError("E_VALUE_TYPE", "value must be a number", path=path, line=line, field="value")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise DataError("E_VALUE_TYPE", "value must be a finite number", path=path, line=line, field="value") from exc
    else:
        raise DataError("E_VALUE_TYPE", "value must be a number", path=path, line=line, field="value")
    if not math.isfinite(value):
        raise DataError("E_VALUE_NOT_FINITE", "value must be finite", path=path, line=line, field="value")
    return 0.0 if value == 0.0 else value


def _csv_rows(text: str, path: str) -> list[tuple[int, dict[str, Any]]]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise DataError("E_COLUMN_MISSING", "CSV header is missing", path=path, line=1)
    fieldnames = [name.strip() if isinstance(name, str) else name for name in reader.fieldnames]
    reader.fieldnames = fieldnames
    for required in REQUIRED_FIELDS:
        if required not in fieldnames:
            raise DataError("E_COLUMN_MISSING", f"CSV is missing column {required!r}", path=path, line=1, field=required)
    rows: list[tuple[int, dict[str, Any]]] = []
    for row in reader:
        if row is None or all(value is None or value == "" for value in row.values()):
            continue
        rows.append((reader.line_num, dict(row)))
    return rows


def _jsonl_rows(text: str, path: str) -> list[tuple[int, dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise DataError("E_JSON", f"invalid JSON: {exc.msg}", path=path, line=line_number) from exc
        if not isinstance(row, dict):
            raise DataError("E_JSON", "each JSONL line must be an object", path=path, line=line_number)
        for required in REQUIRED_FIELDS:
            if required not in row:
                raise DataError(
                    "E_FIELD_MISSING",
                    f"missing required field {required!r}",
                    path=path,
                    line=line_number,
                    field=required,
                )
        rows.append((line_number, row))
    return rows
