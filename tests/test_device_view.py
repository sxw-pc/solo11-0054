from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sensoralign.commands.run import execute_contract
from sensoralign.core.align import align_contract
from sensoralign.core.contract import load_contract
from sensoralign.core.errors import LedgerError
from sensoralign.io.ledger import read_run_bundle
from sensoralign.report.from_ledger import rebuild_report
from sensoralign.report.json_report import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_700_000_000_000_000
MARKERS = {"ok", "no_left", "no_right", "in_gap", "no_window"}


@pytest.fixture(scope="module")
def multi_execution(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("device-view")
    return execute_contract(
        ROOT / "fixtures" / "contracts" / "multimetric.yaml",
        out_dir,
        artifact_prefix="multimetric",
    )


@pytest.fixture(scope="module")
def multi_result():
    return align_contract(load_contract(ROOT / "fixtures" / "contracts" / "multimetric.yaml"))


def _device(report: dict, device_id: str) -> dict:
    return next(item for item in report["devices"] if item["device_id"] == device_id)


def _common_cells(result) -> int:
    return sum(
        1
        for index in range(result.grid.cell_count)
        if all(run.samples[index].marker == "ok" for run in result.runs)
    )


def test_device_wide_row_shape(multi_execution) -> None:
    report = multi_execution.report
    device = _device(report, "dev-f6")
    assert [column["key"] for column in device["columns"]] == ["temperature", "pressure"]
    assert [column["series_id"] for column in device["columns"]] == [
        "dev-f6-temperature",
        "dev-f6-pressure",
    ]
    assert len(device["cells"]) == report["grid"]["cell_count"] == 6
    assert [cell["grid_index"] for cell in device["cells"]] == list(range(6))
    assert [cell["ts_us"] for cell in device["cells"]] == list(
        range(BASE, BASE + 6_000_000, 1_000_000)
    )
    assert all(set(cell["values"]) == {"temperature", "pressure"} for cell in device["cells"])
    assert all(set(cell["markers"]) == {"temperature", "pressure"} for cell in device["cells"])


def test_partial_coverage_keeps_each_metric_marker(multi_execution) -> None:
    """Metrics cover different intervals; the idle one keeps its existing marker."""
    cells = _device(multi_execution.report, "dev-f6")["cells"]
    assert [cell["markers"]["temperature"] for cell in cells] == [
        "no_left",
        "no_left",
        "ok",
        "ok",
        "ok",
        "no_right",
    ]
    assert [cell["markers"]["pressure"] for cell in cells] == [
        "ok",
        "ok",
        "ok",
        "in_gap",
        "in_gap",
        "ok",
    ]
    assert [cell["values"]["temperature"] for cell in cells] == [None, None, 30.0, 30.1, 30.2, None]
    assert [cell["values"]["pressure"] for cell in cells] == [101.0, 101.1, 101.2, None, None, 101.3]
    assert all(cell["markers"][key] in MARKERS for cell in cells for key in cell["markers"])
    assert all(cell["markers"][key] != "" for cell in cells for key in cell["markers"])
    assert all(
        (cell["values"][key] is None) == (cell["markers"][key] != "ok")
        for cell in cells
        for key in cell["values"]
    )


def test_wide_table_does_not_move_completion_rate(multi_execution, multi_result) -> None:
    """The wide projection is a view; completion_rate stays per-series identical."""
    alignment = multi_execution.report["alignment"]
    common = _common_cells(multi_result)
    per_series_rate = round(common / multi_result.grid.cell_count, 6)
    assert common == 1
    assert alignment["common_cells"] == common
    assert alignment["missing_any_cells"] == 5
    assert alignment["completion_rate"] == per_series_rate == 0.166667

    wide_cells = _device(multi_execution.report, "dev-f6")["cells"]
    wide_ok = sum(1 for cell in wide_cells if all(marker == "ok" for marker in cell["markers"].values()))
    assert wide_ok == common


def test_wide_view_replays_byte_identically_from_ledger(multi_execution) -> None:
    original = multi_execution.json_path.read_bytes()
    rebuilt = rebuild_report(read_run_bundle(multi_execution.db_path))
    assert canonical_json_bytes(rebuilt) == original
    assert rebuilt["devices"] == multi_execution.report["devices"]
    assert rebuilt["alignment"]["completion_rate"] == 0.166667


def test_markdown_replay_includes_device_wide_table(multi_execution, capsys) -> None:
    from sensoralign.cli import main

    assert main(["report", "--db", str(multi_execution.db_path), "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    assert "## 设备宽表" in markdown
    assert "### `dev-f6`" in markdown
    assert "| 0 | 1700000000000000 | - | no_left | 101.000000 | ok |" in markdown
    assert "| 3 | 1700000003000000 | 30.100000 | ok | - | in_gap |" in markdown


def test_ledger_device_view_uses_only_five_tables(multi_execution) -> None:
    with sqlite3.connect(multi_execution.db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        aligned_rows = connection.execute("SELECT COUNT(*) FROM aligned").fetchone()[0]
    assert tables == {"run", "series", "aligned", "metrics", "errors"}
    assert aligned_rows == 12  # two series x six cells; still one row per series/cell


def test_basic_contract_has_one_column_per_device() -> None:
    basic = align_contract(load_contract(ROOT / "fixtures" / "contracts" / "basic.yaml"))
    assert len(basic.devices) == 5
    for view in basic.devices:
        assert len(view.columns) == 1
        assert len(view.cells) == basic.grid.cell_count == 60
        assert view.columns[0].metric == "temperature"
    assert basic.alignment.completion_rate == 0.7
    assert [view.device_id for view in basic.devices] == [
        "dev-a1",
        "dev-b2",
        "dev-c3",
        "dev-d4",
        "dev-e5",
    ]
    from sensoralign.report.json_report import build_report

    report = build_report(basic)
    assert [item["device_id"] for item in report["devices"]] == [
        "dev-a1",
        "dev-b2",
        "dev-c3",
        "dev-d4",
        "dev-e5",
    ]
    assert report["alignment"]["completion_rate"] == 0.7


def test_missing_aligned_row_is_corrupt_not_filled(multi_execution) -> None:
    bundle = read_run_bundle(multi_execution.db_path)
    bundle["aligned"] = [
        row
        for row in bundle["aligned"]
        if not (row["series_id"] == "dev-f6-pressure" and row["grid_index"] == 3)
    ]
    with pytest.raises(LedgerError) as caught:
        rebuild_report(bundle)
    assert caught.value.code == "E_LEDGER_CORRUPT"


def test_inconsistent_timestamps_across_columns_are_corrupt(multi_execution) -> None:
    bundle = read_run_bundle(multi_execution.db_path)
    for row in bundle["aligned"]:
        if row["series_id"] == "dev-f6-pressure" and row["grid_index"] == 2:
            row["ts_us"] += 1
    with pytest.raises(LedgerError) as caught:
        rebuild_report(bundle)
    assert caught.value.code == "E_LEDGER_CORRUPT"


def test_duplicate_metric_columns_get_distinct_keys(tmp_path: Path) -> None:
    """Two series on one device may share a metric name without collision."""
    header = "ts_us,device_id,metric,value\n"
    rows = "{p},dev-g7,temperature,{v}\n"
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text(
        header + rows.format(p=BASE, v=1.0) + rows.format(p=BASE + 1_000_000, v=1.1),
        encoding="utf-8",
    )
    second.write_text(
        header + rows.format(p=BASE, v=2.0) + rows.format(p=BASE + 1_000_000, v=2.2),
        encoding="utf-8",
    )
    contract = tmp_path / "collision.yaml"
    contract.write_text(
        f"""version: 1
name: collision
inputs:
  - id: dev-g7-first
    path: {first.as_posix()}
    format: csv
    device_id: dev-g7
    metric: temperature
  - id: dev-g7-second
    path: {second.as_posix()}
    format: csv
    device_id: dev-g7
    metric: temperature
grid:
  origin_us: {BASE}
  step_us: 1000000
  end_us: {BASE + 2_000_000}
""",
        encoding="utf-8",
    )
    result = align_contract(load_contract(contract))
    assert [column.key for column in result.devices[0].columns] == [
        "temperature@dev-g7-first",
        "temperature@dev-g7-second",
    ]
    assert len(result.devices[0].cells) == 2
    assert [sample.marker for sample in result.devices[0].cells[0].samples] == ["ok", "ok"]
