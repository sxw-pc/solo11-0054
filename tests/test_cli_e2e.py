from __future__ import annotations

import ast
import json
from pathlib import Path

from sensoralign.cli import main

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_700_000_000_000_000

def test_demo_runs_and_creates_ledger(capsys) -> None:
    code = main(["demo"])
    captured = capsys.readouterr()
    assert code == 0
    assert "SensorAlign demo" in captured.out
    assert "completion: 0.700000" in captured.out
    assert (ROOT / "out" / "demo.db").is_file()
    assert (ROOT / "out" / "demo.json").is_file()

def test_run_produces_json_and_markdown_with_constant_values(tmp_path: Path, capsys) -> None:
    out_dir = tmp_path / "run-out"
    code = main(["run", "--contract", str(ROOT / "fixtures" / "contracts" / "basic.yaml"), "--out-dir", str(out_dir)])
    assert code == 0
    assert "completion: 0.700000" in capsys.readouterr().out
    report = json.loads((out_dir / "basic.json").read_text(encoding="utf-8"))
    assert report["grid"]["cell_count"] == 60
    assert report["alignment"]["common_cells"] == 42
    series = {item["series_id"]: item for item in report["series"]}
    assert series["dev-d4"]["points"]["duplicates_removed"] == 1
    assert series["dev-d4"]["points"]["conflicts"] == 1
    assert series["dev-e5"]["quality"]["marker_counts"]["in_gap"] == 9
    assert series["dev-c3"]["clock"]["slope_us_per_s"] == 100.0
    assert series["dev-b2"]["clock"]["median_offset_us"] == 2_000_000
    assert (out_dir / "basic.md").is_file()

def test_deterministic_json_across_two_runs(tmp_path: Path) -> None:
    contract = str(ROOT / "fixtures" / "contracts" / "basic.yaml")
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert main(["run", "--contract", contract, "--out-dir", str(first)]) == 0
    assert main(["run", "--contract", contract, "--out-dir", str(second)]) == 0
    assert (first / "basic.json").read_bytes() == (second / "basic.json").read_bytes()
    assert (first / "basic.md").read_bytes() == (second / "basic.md").read_bytes()

def test_report_markdown_and_json_from_ledger(tmp_path: Path, capsys) -> None:
    out_dir = tmp_path / "report-out"
    assert main(["run", "--contract", str(ROOT / "fixtures" / "contracts" / "basic.yaml"), "--out-dir", str(out_dir)]) == 0
    capsys.readouterr()
    db_path = out_dir / "align.db"
    assert main(["report", "--db", str(db_path), "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    assert "## 每路质量" in markdown
    assert "## 时钟偏移估计" in markdown
    assert "dev-e5" in markdown
    json_out = tmp_path / "replayed.json"
    assert main(["report", "--db", str(db_path), "--format", "json", "--out", str(json_out)]) == 0
    assert json_out.read_bytes() == (out_dir / "basic.json").read_bytes()

def test_contract_error_exit_code_is_two(tmp_path: Path, capsys) -> None:
    contract = tmp_path / "bad.yaml"
    contract.write_text("version: 1\nname: bad\ninputs: []\ngrid: from_union\nunknown: 1\n", encoding="utf-8")
    code = main(["run", "--contract", str(contract), "--out-dir", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 2
    assert "E_CONTRACT_UNKNOWN" in captured.err

def test_data_error_exit_code_is_three(tmp_path: Path, capsys) -> None:
    data = tmp_path / "float.csv"
    data.write_text(
        "ts_us,device_id,metric,value\n1.5,dev-a1,temperature,20.0\n",
        encoding="utf-8",
    )
    contract = tmp_path / "data.yaml"
    contract.write_text(
        f"""version: 1
name: bad-data
inputs:
  - id: dev-a1
    path: {data.as_posix()}
    format: csv
    device_id: dev-a1
    metric: temperature
grid:
  origin_us: 0
  step_us: 1000000
  end_us: 5000000
""",
        encoding="utf-8",
    )
    code = main(["run", "--contract", str(contract), "--out-dir", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 3
    assert "E_TS_FLOAT" in captured.err

def test_missing_ledger_exit_code_is_one(tmp_path: Path, capsys) -> None:
    code = main(["report", "--db", str(tmp_path / "missing.db"), "--format", "markdown"])
    assert code == 1
    assert "E_LEDGER_READ" in capsys.readouterr().err

def test_no_network_modules_are_imported() -> None:
    forbidden = {"socket", "urllib", "http", "requests", "ftplib", "smtplib", "telnetlib", "aiohttp", "websockets"}
    offenders = []
    for path in (ROOT / "sensoralign").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in forbidden:
                    offenders.append((path.name, name))
    assert offenders == []

def test_all_python_files_stay_within_300_lines() -> None:
    oversized = []
    for path in [*(ROOT / "sensoralign").rglob("*.py"), *ROOT.glob("tests/*.py")]:
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > 300:
            oversized.append((path.name, lines))
    assert oversized == []
