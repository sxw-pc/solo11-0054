from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from sensoralign.commands.run import execute_contract
from sensoralign.core.errors import LedgerError
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


def manual_compact(value) -> str:
    """Reference serialization spelling out the canonical sidecar parameters."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_snapshot_json_matches_manual_dumps_and_run_hash(execution) -> None:
    manual = manual_compact(execution.report["contract"]["snapshot"])
    with sqlite3.connect(execution.db_path) as connection:
        stored, = connection.execute(
            "SELECT metric_text FROM metrics WHERE scope = 'contract' AND metric_key = 'snapshot_json'"
        ).fetchone()
        contract_sha, = connection.execute("SELECT contract_sha256 FROM run").fetchone()
    assert stored == manual
    assert hashlib.sha256(stored.encode("utf-8")).hexdigest() == contract_sha
    assert execution.report["contract"]["sha256"] == contract_sha


def test_error_detail_json_matches_manual_dumps(execution) -> None:
    details = {event["seq"]: event["detail"] for event in execution.report["errors"]}
    with sqlite3.connect(execution.db_path) as connection:
        rows = connection.execute("SELECT seq, detail_json FROM errors ORDER BY seq").fetchall()
    assert len(rows) == len(details)
    for seq, detail_json in rows:
        assert detail_json == manual_compact(details[seq])


def test_tampered_snapshot_json_whitespace_fails_rebuild(execution, tmp_path) -> None:
    db_copy = tmp_path / "tampered.db"
    shutil.copy(execution.db_path, db_copy)
    with sqlite3.connect(db_copy) as connection:
        stored, = connection.execute(
            "SELECT metric_text FROM metrics WHERE scope = 'contract' AND metric_key = 'snapshot_json'"
        ).fetchone()
        tampered = stored.replace(",", ", ", 1)
        assert tampered != stored
        connection.execute(
            "UPDATE metrics SET metric_text = ? WHERE scope = 'contract' AND metric_key = 'snapshot_json'",
            (tampered,),
        )
    with pytest.raises(LedgerError) as excinfo:
        load_report_from_db(db_copy)
    assert excinfo.value.code == "E_LEDGER_CORRUPT"


def test_untampered_copy_still_rebuilds(execution, tmp_path) -> None:
    db_copy = tmp_path / "pristine.db"
    shutil.copy(execution.db_path, db_copy)
    rebuilt = load_report_from_db(db_copy)
    assert canonical_json_bytes(rebuilt) == execution.json_path.read_bytes()
