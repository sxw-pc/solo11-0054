"""Immutable models used by the alignment-contract loader and engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InputSpec:
    """One named source stream declaration.

    ``priority`` ranks the stream in cross-input arbitration: the larger value
    wins, and undeclared streams sit at the lowest priority (zero). Equal
    priorities resolve by declaration order.
    """

    series_id: str
    path: str
    format: str
    device_id: str
    metric: str
    priority: int = 0


@dataclass(frozen=True)
class GridSpec:
    """Explicit grid or the deterministic ``from_union`` mode."""

    mode: str
    origin_us: int | None = None
    step_us: int | None = None
    end_us: int | None = None


@dataclass(frozen=True)
class ResampleSpec:
    """Default strategy plus stable series-id overrides."""

    default: str
    overrides: tuple[tuple[str, str], ...] = ()

    def strategy_for(self, series_id: str) -> str:
        """Resolve the configured strategy for a series."""
        for key, value in self.overrides:
            if key == series_id:
                return value
        return self.default


@dataclass(frozen=True)
class Contract:
    """A fully validated alignment contract."""

    version: int
    name: str
    description: str
    inputs: tuple[InputSpec, ...]
    anchors_path: str | None
    grid: GridSpec
    max_gap_us: int
    monotonic_policy: str
    duplicate_policy: str
    conflict_policy: str
    clock_skew: str
    resample: ResampleSpec
    gap_report_limit: int
    source_path: Path
    source_sha256: str
    source_dir: Path
    project_root: Path

    def resolve_path(self, raw: str) -> Path:
        """Resolve relative paths against project root, contract dir, then cwd."""
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        for base in (self.project_root, self.source_dir, Path.cwd()):
            resolved = base / candidate
            if resolved.exists():
                return resolved
        return self.project_root / candidate

    def snapshot(self) -> dict[str, Any]:
        """Return a stable, path-sanitized contract snapshot."""
        if self.grid.mode == "from_union":
            grid: dict[str, Any] = {"mode": "from_union"}
        else:
            grid = {
                "mode": "explicit",
                "origin_us": self.grid.origin_us,
                "step_us": self.grid.step_us,
                "end_us": self.grid.end_us,
            }
        return {
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "inputs": [
                {
                    "id": item.series_id,
                    "path": item.path,
                    "format": item.format,
                    "device_id": item.device_id,
                    "metric": item.metric,
                    "priority": item.priority,
                }
                for item in self.inputs
            ],
            "anchors": self.anchors_path,
            "grid": grid,
            "max_gap_us": self.max_gap_us,
            "monotonic_policy": self.monotonic_policy,
            "duplicate_policy": self.duplicate_policy,
            "conflict_policy": self.conflict_policy,
            "clock_skew": self.clock_skew,
            "resample": {
                "default": self.resample.default,
                "overrides": {key: value for key, value in self.resample.overrides},
            },
            "gap_report_limit": self.gap_report_limit,
        }
