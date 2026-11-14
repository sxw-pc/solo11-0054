"""SQLite audit ledger with exactly five public tables.

Tables:
    run      deterministic run id plus contract and result content hashes.
    series   input provenance, hygiene counts and clock-estimate columns.
    aligned  one row per series/grid cell, including marker and grid timestamp.
    metrics  typed report metrics grouped by contract/grid/alignment/series/offset.
    errors   ordered audit events, including recoverable ``W_*`` findings.

No table stores a wall-clock timestamp, so content tables remain reproducible.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..core.errors import LedgerError
from ..report.json_report import report_sha256, run_id_for

SCHEMA_TABLES = ("run", "series", "aligned", "metrics", "errors")
SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    contract_sha256 TEXT NOT NULL,
    result_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series (
    run_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    series_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    strategy TEXT NOT NULL,
    input_path TEXT NOT NULL,
    input_format TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    raw_points INTEGER NOT NULL,
    sanitized_points INTEGER NOT NULL,
    duplicates_removed INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    clock_status TEXT NOT NULL,
    clock_reference TEXT NOT NULL,
    anchor_count INTEGER NOT NULL,
    median_offset_us INTEGER,
    slope_us_per_s REAL,
    intercept_us REAL,
    residual_median_us REAL,
    gap_count INTEGER NOT NULL,
    gap_duration_us INTEGER NOT NULL,
    PRIMARY KEY (run_id, series_id)
);
CREATE TABLE IF NOT EXISTS aligned (
    run_id TEXT NOT NULL,
    series_id TEXT NOT NULL,
    grid_index INTEGER NOT NULL,
    ts_us INTEGER NOT NULL,
    value REAL,
    marker TEXT NOT NULL,
    PRIMARY KEY (run_id, series_id, grid_index)
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    metric_value REAL,
    metric_text TEXT,
    PRIMARY KEY (run_id, scope, scope_id, metric_key)
);
CREATE TABLE IF NOT EXISTS errors (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    stage TEXT NOT NULL,
    series_id TEXT,
    path TEXT,
    line INTEGER,
    field TEXT,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


def write_run(
    db_path: Path,
    report: dict[str, Any],
    aligned_rows: Iterable[tuple[str, int, int, float | None, str]],
) -> str:
    """Insert or replace one deterministic run in the ledger.

    ``aligned_rows`` items are ``(series_id, grid_index, ts_us, value, marker)``.
    The same report and rows always produce the same ``run_id``; rewriting it
    replaces all five tables' rows for that id without duplication.
    """
    result_sha = report_sha256(report)
    contract_sha = report["contract"]["sha256"]
    run_id = run_id_for(contract_sha, result_sha)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(db_path))
        try:
            connection.executescript(SCHEMA)
            with connection:
                for table in ("aligned", "metrics", "errors", "series", "run"):
                    connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
                connection.execute(
                    "INSERT INTO run (run_id, contract_sha256, result_sha256) VALUES (?, ?, ?)",
                    (run_id, contract_sha, result_sha),
                )
                connection.executemany(
                    "INSERT INTO series VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    list(_series_rows(run_id, report)),
                )
                connection.executemany(
                    "INSERT INTO aligned VALUES (?, ?, ?, ?, ?, ?)",
                    [(run_id, series_id, index, ts_us, value, marker) for series_id, index, ts_us, value, marker in aligned_rows],
                )
                connection.executemany(
                    "INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?)",
                    list(_metric_rows(run_id, report)),
                )
                connection.executemany(
                    "INSERT INTO errors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    list(_error_rows(run_id, report)),
                )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise LedgerError("E_LEDGER_WRITE", f"cannot write ledger: {exc}", path=db_path.name) from exc
    return run_id


def list_tables(db_path: Path) -> list[str]:
    """Return public table names present in an existing ledger."""
    connection = _open(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return [row[0] for row in rows if row[0] in SCHEMA_TABLES]
    finally:
        connection.close()


def list_run_ids(db_path: Path) -> list[str]:
    """Return run ids in ledger insertion order (oldest first)."""
    connection = _open(db_path)
    try:
        return [row[0] for row in connection.execute("SELECT run_id FROM run ORDER BY rowid").fetchall()]
    finally:
        connection.close()


def read_run_bundle(db_path: Path, run_id: str | None = None) -> dict[str, Any]:
    """Read one run from the five tables.

    If ``run_id`` is omitted, the most recently inserted run is selected.
    """
    connection = _open(db_path)
    try:
        if run_id is None:
            row = connection.execute("SELECT run_id FROM run ORDER BY rowid DESC LIMIT 1").fetchone()
            if row is None:
                raise LedgerError("E_LEDGER_RUN_NOT_FOUND", "ledger contains no runs", path=db_path.name)
            selected = row[0]
        else:
            selected = run_id
        run_row = connection.execute("SELECT * FROM run WHERE run_id = ?", (selected,)).fetchone()
        if run_row is None:
            raise LedgerError("E_LEDGER_RUN_NOT_FOUND", f"run not found: {selected}", path=db_path.name)
        bundle = {
            "run": dict(run_row),
            "series": [dict(row) for row in connection.execute("SELECT * FROM series WHERE run_id = ? ORDER BY ordinal", (selected,))],
            "metrics": [dict(row) for row in connection.execute("SELECT * FROM metrics WHERE run_id = ?", (selected,))],
            "errors": [dict(row) for row in connection.execute("SELECT * FROM errors WHERE run_id = ? ORDER BY seq", (selected,))],
            "aligned_count": connection.execute("SELECT COUNT(*) FROM aligned WHERE run_id = ?", (selected,)).fetchone()[0],
        }
        return bundle
    except sqlite3.Error as exc:
        raise LedgerError("E_LEDGER_READ", f"cannot read ledger: {exc}", path=db_path.name) from exc
    finally:
        connection.close()


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise LedgerError("E_LEDGER_READ", "ledger file does not exist", path=db_path.name)
    try:
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as exc:
        raise LedgerError("E_LEDGER_READ", f"cannot open ledger: {exc}", path=db_path.name) from exc
    missing = [table for table in SCHEMA_TABLES if table not in tables]
    if missing:
        connection.close()
        raise LedgerError("E_LEDGER_CORRUPT", f"ledger is missing table: {missing[0]}", path=db_path.name)
    return connection


def _series_rows(run_id: str, report: dict[str, Any]) -> Iterable[Sequence[Any]]:
    for ordinal, item in enumerate(report["series"]):
        clock = item["clock"]
        quality = item["quality"]
        yield (
            run_id,
            ordinal,
            item["series_id"],
            item["device_id"],
            item["metric"],
            item["strategy"],
            item["input"]["path"],
            item["input"]["format"],
            item["input"]["sha256"],
            item["points"]["raw"],
            item["points"]["sanitized"],
            item["points"]["duplicates_removed"],
            item["points"]["conflicts"],
            clock["status"],
            clock["reference_device"],
            clock["anchor_count"],
            clock["median_offset_us"],
            clock["slope_us_per_s"],
            clock["intercept_us"],
            clock["residual_median_us"],
            quality["gap_count"],
            quality["gap_duration_us"],
        )


def _metric_rows(run_id: str, report: dict[str, Any]) -> Iterable[tuple[Any, ...]]:
    contract = report["contract"]
    grid = report["grid"]
    alignment = report["alignment"]
    yield (run_id, "contract", "", "name", None, contract["name"])
    yield (run_id, "contract", "", "version", float(contract["version"]), None)
    yield (run_id, "contract", "", "snapshot_json", None, json.dumps(contract["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    for key, value in (
        ("origin_us", grid["origin_us"]),
        ("step_us", grid["step_us"]),
        ("end_us", grid["end_us"]),
        ("cell_count", grid["cell_count"]),
    ):
        yield (run_id, "grid", "", key, float(value), None)
    yield (run_id, "grid", "", "mode", None, grid["mode"])
    for key in ("total_cells", "common_cells", "missing_any_cells", "completion_rate"):
        yield (run_id, "alignment", "", key, float(alignment[key]), None)
    for key in ("matrix", "device_groups", "metric_groups"):
        yield (
            run_id,
            "alignment",
            "",
            key,
            None,
            json.dumps(alignment[key], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    for item in report["series"]:
        quality = item["quality"]
        for key in ("total_cells", "value_cells", "coverage", "gap_cells", "gap_ratio", "conflict_count", "gap_count", "gap_duration_us"):
            yield (run_id, "series", item["series_id"], key, float(quality[key]), None)
        for marker, count in quality["marker_counts"].items():
            yield (run_id, "series", item["series_id"], f"marker.{marker}", float(count), None)
    for ordinal, estimate in enumerate(report["offsets"]):
        scope_id = estimate["device_id"]
        yield (run_id, "offset", scope_id, "ordinal", float(ordinal), None)
        for key in ("status", "reference_device"):
            yield (run_id, "offset", scope_id, key, None, estimate[key])
        for key in ("anchor_count", "median_offset_us", "intercept_us", "slope_us_per_s", "residual_median_us"):
            value = estimate[key]
            yield (run_id, "offset", scope_id, key, None if value is None else float(value), None)


def _error_rows(run_id: str, report: dict[str, Any]) -> Iterable[tuple[Any, ...]]:
    for event in report["errors"]:
        yield (
            run_id,
            event["seq"],
            event["code"],
            event["severity"],
            event["stage"],
            event["series_id"],
            event["path"],
            event["line"],
            event["field"],
            event["message"],
            json.dumps(event["detail"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
