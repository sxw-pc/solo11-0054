from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sensoralign.cli import main
from sensoralign.commands.run import execute_contract
from sensoralign.report.from_ledger import load_report_from_db
from sensoralign.report.json_report import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_700_000_000_000_000


def write_stream(path: Path, rows: list[tuple[int, float]]) -> None:
    lines = ["ts_us,device_id,metric,value"]
    lines.extend(f"{ts},dev-shared,temperature,{value}" for ts, value in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_contract(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    # ts T0: inversion (high wins despite later declaration); T0+1s: default
    # priority loses to an explicit priority; T0+2s: same-value merge; T0+3s:
    # src-b and src-d cross-input conflict while src-d also holds an internal
    # duplicate, exercising both policies on one series.
    write_stream(data / "src-a.csv", [(T0, 1.0), (T0 + 2_000_000, 3.0)])
    write_stream(data / "src-b.csv", [(T0, 9.0), (T0 + 1_000_000, 2.0), (T0 + 3_000_000, 6.0)])
    write_stream(data / "src-c.csv", [(T0 + 1_000_000, 5.0), (T0 + 2_000_000, 3.0)])
    write_stream(
        data / "src-d.csv",
        [(T0 + 3_000_000, 4.0), (T0 + 3_000_000, 4.0), (T0 + 3_000_000, 8.0)],
    )
    contract = tmp_path / "arbitration.yaml"
    contract.write_text(
        f"""version: 1
name: arbitration-e2e
inputs:
  - id: src-a
    path: {data / 'src-a.csv'}
    format: csv
    device_id: dev-shared
    metric: temperature
    priority: 1
  - id: src-b
    path: {data / 'src-b.csv'}
    format: csv
    device_id: dev-shared
    metric: temperature
    priority: 5
  - id: src-c
    path: {data / 'src-c.csv'}
    format: csv
    device_id: dev-shared
    metric: temperature
  - id: src-d
    path: {data / 'src-d.csv'}
    format: csv
    device_id: dev-shared
    metric: temperature
    priority: 2
monotonic_policy: sort_stable
duplicate_policy: keep_first
conflict_policy: keep_first
grid:
  origin_us: {T0}
  step_us: 1000000
  end_us: {T0 + 4000000}
""",
        encoding="utf-8",
    )
    return contract


def test_end_to_end_arbitration(tmp_path: Path, capsys) -> None:
    contract = build_contract(tmp_path)
    out_dir = tmp_path / "out"
    code = main(["run", "--contract", str(contract), "--out-dir", str(out_dir)])
    assert code == 0
    capsys.readouterr()
    report = json.loads((out_dir / "arbitration-e2e.json").read_text(encoding="utf-8"))
    series = {item["series_id"]: item for item in report["series"]}

    # Contract snapshot carries every priority, with zero as the default.
    snapshot_priority = {item["id"]: item["priority"] for item in report["contract"]["snapshot"]["inputs"]}
    assert snapshot_priority == {"src-a": 1, "src-b": 5, "src-c": 0, "src-d": 2}

    # Single-stream hygiene stays orthogonal: src-d's internal duplicate and
    # conflict are resolved before arbitration and keep their own counts.
    assert series["src-d"]["points"]["duplicates_removed"] == 1
    assert series["src-d"]["points"]["conflicts"] == 1

    # Cross-input counters are attributed per series and never cross streams.
    assert series["src-a"]["arbitration"] == {"priority": 1, "merged": 0, "dropped": 1}
    assert series["src-b"]["arbitration"] == {"priority": 5, "merged": 0, "dropped": 0}
    assert series["src-c"]["arbitration"] == {"priority": 0, "merged": 1, "dropped": 1}
    assert series["src-d"]["arbitration"] == {"priority": 2, "merged": 0, "dropped": 1}

    by_code: dict[str, list[dict]] = {}
    for event in report["errors"]:
        by_code.setdefault(event["code"], []).append(event)
    # One single-stream duplicate and one single-stream conflict (both src-d),
    # three cross-input conflicts (inversion, default-priority loss, src-d loss)
    # and one same-value merge.
    assert len(by_code["W_TS_DUPLICATE"]) == 1
    assert len(by_code["W_TS_CONFLICT"]) == 1
    assert by_code["W_TS_DUPLICATE"][0]["series_id"] == "src-d"
    assert by_code["W_TS_CONFLICT"][0]["series_id"] == "src-d"
    assert len(by_code["W_ARBITRATION_CONFLICT"]) == 3
    assert len(by_code["W_ARBITRATION_DUPLICATE"]) == 1
    owners = {event["series_id"] for event in by_code["W_ARBITRATION_CONFLICT"]}
    assert owners == {"src-a", "src-c", "src-d"}
    merge = by_code["W_ARBITRATION_DUPLICATE"][0]
    assert merge["series_id"] == "src-c"
    assert merge["detail"]["winner_series_id"] == "src-a"

    # Winning values reach the aligned grid: T0 -> 9.0 (src-b inversion win),
    # T0+1s -> 2.0 (src-b over default-priority src-c), T0+2s -> 3.0; at
    # T0+3s src-b keeps 6.0 while src-d's sanitized 4.0 loses arbitration and
    # its internal 8.0 had already been discarded by the conflict policy.
    aligned: dict[str, dict[int, float | None]] = {}
    with sqlite3.connect(out_dir / "align.db") as connection:
        for series_id, index, value in connection.execute(
            "SELECT series_id, grid_index, value FROM aligned ORDER BY series_id, grid_index"
        ):
            aligned.setdefault(series_id, {})[index] = value
    assert aligned["src-b"][0] == 9.0
    assert aligned["src-b"][1] == 2.0
    assert aligned["src-a"][2] == 3.0
    assert aligned["src-b"][3] == 6.0
    # Losing series have no value at the grid instants they lost.
    assert aligned["src-a"][0] is None
    assert aligned["src-c"][1] is None
    assert aligned["src-c"][2] is None
    assert aligned["src-d"][3] is None

    # Every arbitration event persists in the ledger under its owning series.
    with sqlite3.connect(out_dir / "align.db") as connection:
        ledger_owners = {
            row[0]
            for row in connection.execute(
                "SELECT series_id FROM errors WHERE code IN ('W_ARBITRATION_CONFLICT', 'W_ARBITRATION_DUPLICATE')"
            )
        }
        assert ledger_owners == {"src-a", "src-c", "src-d"}
        columns = [row[1] for row in connection.execute("PRAGMA table_info(series)")]
    assert "arbitration_merged" in columns and "arbitration_dropped" in columns

    # Ledger replay reproduces the report byte-for-byte: run_id stays stable.
    rebuilt = load_report_from_db(out_dir / "align.db")
    assert canonical_json_bytes(rebuilt) == canonical_json_bytes(report)
    again = execute_contract(contract, tmp_path / "out-again", artifact_prefix="arbitration-e2e")
    assert again.run_id == execute_contract(contract, tmp_path / "out-third", artifact_prefix="arbitration-e2e").run_id
