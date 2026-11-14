"""Command-line interface for SensorAlign.

Subcommands:
    demo    run the bundled synthetic fixture and write out/demo.db.
    run     execute a contract from ``--contract``.
    report  render a stored run from a ledger database.

Exit codes: 0 success, 2 contract error, 3 data error, 1 other failure.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .commands.demo import demo_command
from .commands.report import report_command
from .commands.run import run_command
from .core.errors import EXIT_OTHER, SensorAlignError


def build_parser() -> argparse.ArgumentParser:
    """Construct the deterministic argparse command tree."""
    parser = argparse.ArgumentParser(
        prog="sensoralign",
        description="Offline multi-sensor time-base alignment and quality reporting.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the bundled fixture end to end")
    demo.set_defaults(handler=demo_command)

    run = subparsers.add_parser("run", help="execute an alignment contract")
    run.add_argument("--contract", required=True, help="path to a YAML alignment contract")
    run.add_argument("--out-dir", default="out", help="output directory (default: out)")
    run.set_defaults(handler=run_command)

    report = subparsers.add_parser("report", help="render a report from a ledger database")
    report.add_argument("--db", required=True, help="path to a SensorAlign SQLite ledger")
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--out", default=None, help="write to this path instead of stdout")
    report.add_argument("--run-id", default=None, help="select a run when the ledger has several")
    report.set_defaults(handler=report_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, dispatch a command and translate coded errors to exits."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except SensorAlignError as exc:
        location = ""
        if exc.path:
            location = f" ({exc.path}" + (f":{exc.line}" if exc.line else "") + ")"
        print(f"{exc.code}: {exc.message}{location}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - defensive boundary
        print(f"E_INTERNAL: {exc}", file=sys.stderr)
        return EXIT_OTHER
