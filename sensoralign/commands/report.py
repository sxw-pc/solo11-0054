"""The ``report`` subcommand: replay Markdown or JSON from the SQLite ledger."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..io.writers import write_text_file
from ..report.from_ledger import load_report_from_db
from ..report.json_report import canonical_json_bytes
from ..report.markdown import render_markdown


def report_command(args: argparse.Namespace) -> int:
    """Render a stored run and either print it or write it to ``--out``."""
    report = load_report_from_db(Path(args.db), args.run_id)
    if args.format == "json":
        text = canonical_json_bytes(report).decode("utf-8")
    else:
        text = render_markdown(report)
    if args.out:
        output = Path(args.out)
        write_text_file(output, text)
        print(f"report: {_shown(output)}")
    else:
        sys.stdout.write(text)
    return 0


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name
