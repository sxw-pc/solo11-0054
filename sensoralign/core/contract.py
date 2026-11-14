"""YAML alignment-contract loading and validation.

The contract is the sole source of policy: inputs, grid mode, timestamp
hygiene, gap threshold, clock-skew model and resampling overrides. Validation
errors always carry a stable ``E_CONTRACT_*`` code and exit status 2.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from .contract_model import Contract, GridSpec, InputSpec, ResampleSpec
from .errors import ContractError
from .timestamps import CONFLICT_POLICIES, DUPLICATE_POLICIES, MONOTONIC_POLICIES

__all__ = ["Contract", "GridSpec", "InputSpec", "ResampleSpec", "find_project_root", "load_contract"]

STRATEGIES = ("linear", "step", "mean")
CLOCK_MODELS = ("none", "constant", "linear")
TOP_LEVEL_KEYS = {
    "version",
    "name",
    "description",
    "inputs",
    "anchors",
    "grid",
    "max_gap_us",
    "monotonic_policy",
    "duplicate_policy",
    "conflict_policy",
    "clock_skew",
    "resample",
    "gap_report_limit",
    "shift_floor_us",
}
INPUT_KEYS = {"id", "path", "format", "device_id", "metric"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def find_project_root(start: Path) -> Path:
    """Find the nearest ancestor containing ``pyproject.toml``."""
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return resolved.parent


def load_contract(path: Path) -> Contract:
    """Read and validate an alignment contract.

    Raises:
        ContractError: ``E_CONTRACT_READ``, ``E_CONTRACT_YAML``,
            ``E_CONTRACT_TYPE``, ``E_CONTRACT_MISSING``,
            ``E_CONTRACT_UNKNOWN``, ``E_CONTRACT_DUPLICATE``,
            ``E_CONTRACT_GRID`` or ``E_CONTRACT_VALUE``.
    """
    shown = path.name
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContractError("E_CONTRACT_READ", f"cannot read contract: {exc}", path=shown) from exc
    try:
        document = yaml.safe_load(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractError("E_CONTRACT_YAML", f"invalid YAML: {exc}", path=shown) from exc
    if not isinstance(document, dict):
        raise ContractError("E_CONTRACT_TYPE", "contract root must be a mapping", path=shown)
    unknown = sorted(set(document) - TOP_LEVEL_KEYS)
    if unknown:
        raise ContractError("E_CONTRACT_UNKNOWN", f"unknown contract field: {unknown[0]}", path=shown, field=unknown[0])
    for required in ("version", "name", "inputs", "grid"):
        if required not in document:
            raise ContractError("E_CONTRACT_MISSING", f"missing required field: {required}", path=shown, field=required)

    version = _int(document["version"], "version", shown, minimum=1)
    if version != 1:
        raise ContractError("E_CONTRACT_VALUE", "only contract version 1 is supported", path=shown, field="version")
    name = _string(document["name"], "name", shown)
    if not NAME_RE.fullmatch(name):
        raise ContractError("E_CONTRACT_VALUE", "name must match [A-Za-z0-9][A-Za-z0-9._-]*", path=shown, field="name")
    description = document.get("description", "")
    if not isinstance(description, str):
        raise ContractError("E_CONTRACT_TYPE", "description must be text", path=shown, field="description")

    inputs = _parse_inputs(document["inputs"], shown)
    seen_ids: set[str] = set()
    for item in inputs:
        if item.series_id in seen_ids:
            raise ContractError("E_CONTRACT_DUPLICATE", f"duplicate input id: {item.series_id}", path=shown, field="inputs")
        seen_ids.add(item.series_id)

    grid = _parse_grid(document["grid"], shown)
    anchors_path = document.get("anchors")
    if anchors_path is not None and (not isinstance(anchors_path, str) or not anchors_path.strip()):
        raise ContractError("E_CONTRACT_TYPE", "anchors must be a non-empty path", path=shown, field="anchors")
    if isinstance(anchors_path, str):
        anchors_path = anchors_path.strip()

    max_gap_us = _int(document.get("max_gap_us", 2_000_000), "max_gap_us", shown, minimum=1)
    monotonic_policy = _choice(document.get("monotonic_policy", "reject"), MONOTONIC_POLICIES, "monotonic_policy", shown)
    duplicate_policy = _choice(document.get("duplicate_policy", "keep_first"), DUPLICATE_POLICIES, "duplicate_policy", shown)
    conflict_policy = _choice(document.get("conflict_policy", "keep_first"), CONFLICT_POLICIES, "conflict_policy", shown)
    clock_skew = _choice(document.get("clock_skew", "none"), CLOCK_MODELS, "clock_skew", shown)
    if clock_skew != "none" and anchors_path is None:
        raise ContractError(
            "E_CONTRACT_MISSING",
            "anchors are required when clock_skew is not 'none'",
            path=shown,
            field="anchors",
        )
    resample = _parse_resample(document.get("resample"), seen_ids, shown)
    gap_report_limit = _int(document.get("gap_report_limit", 20), "gap_report_limit", shown, minimum=1)
    shift_floor_us = (
        None if "shift_floor_us" not in document else _int(document["shift_floor_us"], "shift_floor_us", shown, minimum=0)
    )

    root = find_project_root(path)
    return Contract(
        version=version,
        name=name,
        description=description,
        inputs=inputs,
        anchors_path=anchors_path,
        grid=grid,
        max_gap_us=max_gap_us,
        monotonic_policy=monotonic_policy,
        duplicate_policy=duplicate_policy,
        conflict_policy=conflict_policy,
        clock_skew=clock_skew,
        resample=resample,
        gap_report_limit=gap_report_limit,
        shift_floor_us=shift_floor_us,
        source_path=path.resolve(),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source_dir=path.resolve().parent,
        project_root=root,
    )


def _parse_inputs(raw: Any, path: str) -> tuple[InputSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise ContractError("E_CONTRACT_TYPE", "inputs must be a non-empty list", path=path, field="inputs")
    parsed: list[InputSpec] = []
    for index, entry in enumerate(raw):
        field = f"inputs[{index}]"
        if not isinstance(entry, dict):
            raise ContractError("E_CONTRACT_TYPE", f"{field} must be a mapping", path=path, field=field)
        unknown = sorted(set(entry) - INPUT_KEYS)
        if unknown:
            raise ContractError("E_CONTRACT_UNKNOWN", f"unknown input field: {unknown[0]}", path=path, field=field)
        for required in ("id", "path", "device_id", "metric"):
            if required not in entry:
                raise ContractError("E_CONTRACT_MISSING", f"missing {required} in {field}", path=path, field=field)
        series_id = _string(entry["id"], f"{field}.id", path)
        raw_path = _string(entry["path"], f"{field}.path", path)
        device_id = _string(entry["device_id"], f"{field}.device_id", path)
        metric = _string(entry["metric"], f"{field}.metric", path)
        fmt = entry.get("format")
        if fmt is None:
            suffix = Path(raw_path).suffix.lower()
            fmt = "csv" if suffix == ".csv" else "jsonl" if suffix in (".jsonl", ".ndjson") else None
            if fmt is None:
                raise ContractError("E_CONTRACT_VALUE", f"cannot infer format for {raw_path}", path=path, field=field)
        else:
            fmt = _string(fmt, f"{field}.format", path).lower()
        if fmt not in ("csv", "jsonl"):
            raise ContractError("E_CONTRACT_VALUE", f"unsupported format: {fmt}", path=path, field=f"{field}.format")
        parsed.append(InputSpec(series_id, raw_path, fmt, device_id, metric))
    return tuple(parsed)


def _parse_grid(raw: Any, path: str) -> GridSpec:
    if raw == "from_union":
        return GridSpec("from_union")
    if not isinstance(raw, dict):
        raise ContractError("E_CONTRACT_GRID", "grid must be 'from_union' or a mapping", path=path, field="grid")
    unknown = sorted(set(raw) - {"origin_us", "step_us", "end_us"})
    if unknown:
        raise ContractError("E_CONTRACT_GRID", f"unknown grid field: {unknown[0]}", path=path, field="grid")
    for required in ("origin_us", "step_us", "end_us"):
        if required not in raw:
            raise ContractError("E_CONTRACT_GRID", f"missing grid field: {required}", path=path, field="grid")
    origin = _int(raw["origin_us"], "grid.origin_us", path)
    step = _int(raw["step_us"], "grid.step_us", path, minimum=1)
    end = _int(raw["end_us"], "grid.end_us", path)
    if origin >= end:
        raise ContractError("E_CONTRACT_GRID", "grid.origin_us must be < grid.end_us", path=path, field="grid")
    return GridSpec("explicit", origin, step, end)


def _parse_resample(raw: Any, series_ids: set[str], path: str) -> ResampleSpec:
    if raw is None:
        return ResampleSpec("linear")
    if not isinstance(raw, dict):
        raise ContractError("E_CONTRACT_TYPE", "resample must be a mapping", path=path, field="resample")
    unknown = sorted(set(raw) - {"default", "overrides"})
    if unknown:
        raise ContractError("E_CONTRACT_UNKNOWN", f"unknown resample field: {unknown[0]}", path=path, field="resample")
    default = _choice(raw.get("default", "linear"), STRATEGIES, "resample.default", path)
    overrides_raw = raw.get("overrides", {})
    if not isinstance(overrides_raw, dict):
        raise ContractError("E_CONTRACT_TYPE", "resample.overrides must be a mapping", path=path, field="resample")
    overrides: list[tuple[str, str]] = []
    for series_id, strategy in overrides_raw.items():
        if not isinstance(series_id, str) or series_id not in series_ids:
            raise ContractError("E_CONTRACT_VALUE", f"unknown series id: {series_id}", path=path, field="resample")
        overrides.append((series_id, _choice(strategy, STRATEGIES, f"resample.overrides.{series_id}", path)))
    return ResampleSpec(default, tuple(sorted(overrides)))


def _choice(raw: Any, allowed: tuple[str, ...], field: str, path: str) -> str:
    if not isinstance(raw, str) or raw not in allowed:
        raise ContractError("E_CONTRACT_VALUE", f"{field} must be one of {', '.join(allowed)}", path=path, field=field)
    return raw


def _int(raw: Any, field: str, path: str, minimum: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ContractError("E_CONTRACT_TYPE", f"{field} must be an integer", path=path, field=field)
    value = int(raw)
    if minimum is not None and value < minimum:
        raise ContractError("E_CONTRACT_VALUE", f"{field} must be >= {minimum}", path=path, field=field)
    return value


def _string(raw: Any, field: str, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError("E_CONTRACT_TYPE", f"{field} must be a non-empty string", path=path, field=field)
    return raw.strip()
