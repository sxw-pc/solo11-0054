from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sensoralign.commands.run import execute_contract
from sensoralign.core.contract import load_contract
from sensoralign.core.align import align_contract
from sensoralign.io.ledger import SCHEMA_TABLES, list_tables, read_run_bundle
from sensoralign.report.from_ledger import load_report_from_db
from sensoralign.report.json_report import build_report, canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
WINDOW_KEYS = (
    "main_window_start_us",
    "main_window_end_us",
    "main_window_span_us",
    "main_window_cells",
    "main_window_value_cells",
    "main_window_coverage",
)
INTERSECTION_KEYS = (
    "intersection_start_us",
    "intersection_end_us",
    "intersection_cells",
    "intersection_common_cells",
    "intersection_completion_rate",
)


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
    assert "aligned" not in bundle


def test_window_metrics_are_stored_in_both_scopes(execution) -> None:
    with sqlite3.connect(execution.db_path) as connection:
        series_keys = {
            row[0]
            for row in connection.execute(
                "SELECT metric_key FROM metrics WHERE scope = 'series' AND scope_id = 'dev-e5'"
            )
        }
        alignment_keys = {
            row[0]
            for row in connection.execute(
                "SELECT metric_key FROM metrics WHERE scope = 'alignment'"
            )
        }
    assert set(WINDOW_KEYS) <= series_keys
    assert set(INTERSECTION_KEYS) <= alignment_keys
    report = load_report_from_db(execution.db_path)
    assert report["alignment"]["intersection_completion_rate"] == pytest.approx(42 / 51, abs=1e-6)


def test_old_ledger_without_window_keys_is_rebuilt_from_aligned(execution, tmp_path: Path) -> None:
    old_db = tmp_path / "old.db"
    old_db.write_bytes(execution.db_path.read_bytes())
    with sqlite3.connect(old_db) as connection:
        connection.execute(
            "DELETE FROM metrics WHERE metric_key IN ({})".format(
                ",".join("?" for _ in (*WINDOW_KEYS, *INTERSECTION_KEYS))
            ),
            (*WINDOW_KEYS, *INTERSECTION_KEYS),
        )
        connection.commit()
    bundle = read_run_bundle(old_db)
    assert "aligned" in bundle
    rebuilt = load_report_from_db(old_db)
    assert canonical_json_bytes(rebuilt) == execution.json_path.read_bytes()


def test_window_intersection_fixture_crosses_windows(tmp_path: Path) -> None:
    result = align_contract(load_contract(ROOT / "fixtures" / "contracts" / "window_intersection.yaml"))
    report = build_report(result)
    by_id = {item["series_id"]: item["quality"] for item in report["series"]}
    assert by_id["dev-w1"]["main_window_start_us"] == 0
    assert by_id["dev-w1"]["main_window_end_us"] == 40
    assert by_id["dev-w2"]["main_window_start_us"] == 50
    assert by_id["dev-w2"]["main_window_end_us"] == 90
    alignment = report["alignment"]
    assert alignment["intersection_start_us"] == 50
    assert alignment["intersection_end_us"] == 40
    assert alignment["intersection_cells"] == 0
    assert alignment["intersection_completion_rate"] == 0.0

    execution = execute_contract(
        ROOT / "fixtures" / "contracts" / "window_intersection.yaml",
        tmp_path,
        db_name="windows.db",
        artifact_prefix="windows",
    )
    assert canonical_json_bytes(load_report_from_db(execution.db_path)) == execution.json_path.read_bytes()
