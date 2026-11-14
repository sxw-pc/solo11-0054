"""Shared contract execution and the ``run`` subcommand."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.align import AlignResult, align_contract
from ..core.contract import load_contract
from ..io.ledger import write_run
from ..io.writers import write_json_report, write_markdown_report
from ..report.json_report import build_report


@dataclass(frozen=True)
class Execution:
    """Paths and in-memory artefacts produced by one contract execution."""

    report: dict[str, Any]
    json_path: Path
    markdown_path: Path
    db_path: Path
    run_id: str


def execute_contract(
    contract_path: Path,
    out_dir: Path,
    *,
    db_name: str = "align.db",
    artifact_prefix: str | None = None,
) -> Execution:
    """Load a contract, align all inputs, write reports and the ledger."""
    contract = load_contract(contract_path)
    result = align_contract(contract)
    report = build_report(result)
    prefix = artifact_prefix or contract.name
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{prefix}.json"
    markdown_path = out_dir / f"{prefix}.md"
    db_path = out_dir / db_name
    write_json_report(report, json_path)
    write_markdown_report(report, markdown_path)
    run_id = write_run(db_path, report, _aligned_rows(result))
    return Execution(report, json_path, markdown_path, db_path, run_id)


def run_command(args: argparse.Namespace) -> int:
    """Execute the CLI ``run`` command and print a deterministic summary."""
    execution = execute_contract(Path(args.contract), Path(args.out_dir))
    report = execution.report
    print("SensorAlign run")
    print(f"contract: {report['contract']['name']}")
    print(f"grid cells: {report['grid']['cell_count']}")
    print(f"series: {len(report['series'])}")
    print(f"completion: {report['alignment']['completion_rate']:.6f}")
    print(f"events: {len(report['errors'])}")
    print(f"json: {_shown(execution.json_path)}")
    print(f"markdown: {_shown(execution.markdown_path)}")
    print(f"ledger: {_shown(execution.db_path)}")
    print(f"run_id: {execution.run_id}")
    return 0


def _aligned_rows(result: AlignResult) -> list[tuple[str, int, int, float | None, str]]:
    rows: list[tuple[str, int, int, float | None, str]] = []
    for run in result.runs:
        for index, (ts_us, sample) in enumerate(zip(result.grid.times, run.samples)):
            rows.append((run.series.series_id, index, ts_us, sample.value, sample.marker))
    return rows


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name
