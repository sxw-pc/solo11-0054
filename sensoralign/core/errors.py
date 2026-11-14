"""Stable error codes, exception types and non-fatal audit events.

Exit code convention:
    0 success, 2 contract error, 3 data error, 1 any other failure.
All user-facing failures carry a code beginning with ``E_``.  Recoverable
data-quality findings carry a code beginning with ``W_`` and are persisted in
the ledger's ``errors`` table alongside fatal errors from failed runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

EXIT_OK = 0
EXIT_OTHER = 1
EXIT_CONTRACT = 2
EXIT_DATA = 3


class SensorAlignError(Exception):
    """Base class for a coded, user-facing SensorAlign failure."""

    exit_code = EXIT_OTHER

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        line: int | None = None,
        field: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.line = line
        self.field = field
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-ready representation of the failure."""
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "field": self.field,
            "detail": self.detail,
        }


class ContractError(SensorAlignError):
    """Invalid or unreadable alignment contract (exit status 2)."""

    exit_code = EXIT_CONTRACT


class DataError(SensorAlignError):
    """Invalid input data or an alignment precondition failure (exit status 3)."""

    exit_code = EXIT_DATA


class LedgerError(SensorAlignError):
    """Unreadable, missing or inconsistent ledger state (exit status 1)."""

    exit_code = EXIT_OTHER


@dataclass(frozen=True)
class Event:
    """A deterministic audit event retained in reports and the ledger."""

    code: str
    severity: str
    stage: str
    message: str
    series_id: str | None = None
    path: str | None = None
    line: int | None = None
    field: str | None = None
    detail: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_record(self, seq: int) -> dict[str, Any]:
        """Convert the event to the stable row shape used by reports."""
        return {
            "seq": seq,
            "code": self.code,
            "severity": self.severity,
            "stage": self.stage,
            "series_id": self.series_id,
            "path": self.path,
            "line": self.line,
            "field": self.field,
            "message": self.message,
            "detail": dict(self.detail),
        }
