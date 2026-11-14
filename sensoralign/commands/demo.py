"""The zero-configuration ``demo`` subcommand.

It uses the repository's synthetic five-device contract, the complete fixture
set, and writes ``out/demo.db`` plus JSON and Markdown snapshots.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .run import execute_contract


def demo_command(_args: argparse.Namespace) -> int:
    """Run the bundled fixture end to end and print a compact summary."""
    root = Path(__file__).resolve().parents[2]
    contract_path = root / "fixtures" / "contracts" / "basic.yaml"
    execution = execute_contract(
        contract_path,
        root / "out",
        db_name="demo.db",
        artifact_prefix="demo",
    )
    report = execution.report
    print("SensorAlign demo")
    print(f"fixture contract: {report['contract']['name']}")
    print(f"series: {', '.join(item['series_id'] for item in report['series'])}")
    print(f"grid cells: {report['grid']['cell_count']}")
    print(f"completion: {report['alignment']['completion_rate']:.6f}")
    print(f"warnings/events: {len(report['errors'])}")
    print(f"json: {_relative(root, execution.json_path)}")
    print(f"markdown: {_relative(root, execution.markdown_path)}")
    print(f"ledger: {_relative(root, execution.db_path)}")
    print(f"run_id: {execution.run_id}")
    return 0


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
