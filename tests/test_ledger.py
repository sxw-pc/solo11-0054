from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sensoralign.commands.run import execute_contract
from sensoralign.core.resample import MARKER_ORDER
from sensoralign.io.ledger import SCHEMA_TABLES, list_tables, read_run_bundle
from sensoralign.report.from_ledger import load_report_from_db
from sensoralign.report.json_report import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def execution(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("ledger")
    return execute_contract(
        ROOT / "fixtures" / "contracts" / "basic.yaml",
        out_dir,
        db_name="align.db",
        artifact_prefix="basic",
    )


def count_rows(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_all_five_tables_present(execution) -> None:
    assert list_tables(execution.db_path) == sorted(SCHEMA_TABLES)
    assert set(SCHEMA_TABLES) == {"run", "series", "aligned", "metrics", "errors"}


def test_expected_row_counts(execution) -> None:
    assert count_rows(execution.db_path, "run") == 1
    assert count_rows(execution.db_path, "series") == 5
    assert count_rows(execution.db_path, "aligned") == 300
    assert count_rows(execution.db_path, "errors") == 4
    assert count_rows(execution.db_path, "metrics") > 40


def test_run_table_contains_only_ids_and_hashes(execution) -> None:
    with sqlite3.connect(execution.db_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(run)")]
        row = connection.execute("SELECT * FROM run").fetchone()
    assert columns == ["run_id", "contract_sha256", "result_sha256"]
    assert row[0] == execution.run_id
    assert len(row[1]) == 64
    assert len(row[2]) == 64
    assert "time" not in " ".join(columns)


def test_series_fingerprints_and_aligned_provenance(execution) -> None:
    with sqlite3.connect(execution.db_path) as connection:
        fingerprint, = connection.execute("SELECT input_sha256 FROM series WHERE series_id = 'dev-a1'").fetchone()
        rows = connection.execute(
            "SELECT grid_index, ts_us, marker FROM aligned WHERE series_id = 'dev-e5' ORDER BY grid_index"
        ).fetchall()
    assert len(fingerprint) == 64
    assert len(rows) == 60
    assert rows[0][0] == 0
    assert rows[0][1] == 1_700_000_000_000_000
    assert rows[21][2] == "in_gap"
    assert rows[30][2] == "ok"


def test_ledger_report_roundtrips_to_identical_json(execution) -> None:
    original = execution.json_path.read_bytes()
    rebuilt = load_report_from_db(execution.db_path)
    assert canonical_json_bytes(rebuilt) == original
    report = json.loads(original.decode("utf-8"))
    assert report["grid"]["cell_count"] == 60


def test_rewriting_same_run_is_idempotent(execution) -> None:
    first_id = execution.run_id
    second = execute_contract(
        ROOT / "fixtures" / "contracts" / "basic.yaml",
        execution.db_path.parent,
        db_name="align.db",
        artifact_prefix="basic",
    )
    assert second.run_id == first_id
    assert count_rows(execution.db_path, "run") == 1
    assert count_rows(execution.db_path, "aligned") == 300
    bundle = read_run_bundle(execution.db_path, first_id)
    assert bundle["aligned_count"] == 300
    assert bundle["run"]["run_id"] == first_id


def test_ledger_freezes_extended_marker_order(tmp_path: Path) -> None:
    contract = tmp_path / "lag.yaml"
    contract.write_text(
        f"""version: 1
name: lag
inputs:
  - id: dev-a1
    path: {(ROOT / "fixtures" / "series" / "dev-a1.csv").as_posix()}
    format: csv
    device_id: dev-a1
    metric: temperature
grid:
  origin_us: 1699999999000000
  step_us: 1000000
  end_us: 1700000003000000
resample:
  default: lag_window
""",
        encoding="utf-8",
    )
    execution = execute_contract(contract, tmp_path / "out")
    rebuilt = load_report_from_db(execution.db_path)
    assert canonical_json_bytes(rebuilt) == execution.json_path.read_bytes()
    marker_counts = rebuilt["series"][0]["quality"]["marker_counts"]
    assert tuple(marker_counts) == MARKER_ORDER
    assert marker_counts["no_window_lower"] == 2
    assert marker_counts["no_window_upper"] == 0
    assert marker_counts["no_left"] == 0
    assert marker_counts["ok"] == 2
