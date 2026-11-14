"""Deterministic filesystem writers for JSON and Markdown reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..report.json_report import canonical_json_bytes
from ..report.markdown import render_markdown


def write_json_report(report: dict[str, Any], path: Path) -> int:
    """Write canonical JSON bytes, replacing the destination, and return length."""
    payload = canonical_json_bytes(report)
    _write_bytes(path, payload)
    return len(payload)


def write_markdown_report(report: dict[str, Any], path: Path) -> int:
    """Write a Markdown report with UTF-8 and LF line endings."""
    payload = render_markdown(report).encode("utf-8")
    _write_bytes(path, payload)
    return len(payload)


def write_text_file(path: Path, text: str) -> int:
    """Write UTF-8 text with LF newlines and return the byte length."""
    payload = text.encode("utf-8")
    _write_bytes(path, payload)
    return len(payload)


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
