from __future__ import annotations

from pathlib import Path

import pytest

from sensoralign.core.contract import load_contract
from sensoralign.core.errors import ContractError

ROOT = Path(__file__).resolve().parents[1]


def minimal_contract() -> str:
    return """
version: 1
name: contract-test
inputs:
  - id: a1
    path: fixtures/series/dev-a1.csv
    format: csv
    device_id: dev-a1
    metric: temperature
grid:
  origin_us: 0
  step_us: 1000000
  end_us: 5000000
"""


def write_contract(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_basic_contract_and_snapshot() -> None:
    contract = load_contract(ROOT / "fixtures" / "contracts" / "basic.yaml")
    assert contract.name == "basic"
    assert len(contract.inputs) == 5
    assert contract.resample.strategy_for("dev-b2") == "step"
    assert contract.resample.strategy_for("dev-a1") == "linear"
    snapshot = contract.snapshot()
    assert snapshot["grid"]["mode"] == "explicit"
    assert snapshot["resample"]["overrides"] == {"dev-b2": "step", "dev-c3": "mean"}
    assert "source_path" not in snapshot
    assert [item["priority"] for item in snapshot["inputs"]] == [0, 0, 0, 0, 0]
    assert all(spec.priority == 0 for spec in contract.inputs)


def test_priority_is_parsed_and_included_in_snapshot(tmp_path: Path) -> None:
    body = minimal_contract().replace(
        "    device_id: dev-a1\n    metric: temperature\n",
        "    device_id: dev-a1\n    metric: temperature\n    priority: 7\n",
    )
    contract = load_contract(write_contract(tmp_path, body))
    assert contract.inputs[0].priority == 7
    assert contract.snapshot()["inputs"][0]["priority"] == 7


def test_priority_rejects_negative_and_non_integer(tmp_path: Path) -> None:
    negative = minimal_contract().replace(
        "    device_id: dev-a1\n    metric: temperature\n",
        "    device_id: dev-a1\n    metric: temperature\n    priority: -1\n",
    )
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, negative))
    assert caught.value.code == "E_CONTRACT_VALUE"
    boolean = minimal_contract().replace(
        "    device_id: dev-a1\n    metric: temperature\n",
        "    device_id: dev-a1\n    metric: temperature\n    priority: true\n",
    )
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, boolean))
    assert caught.value.code == "E_CONTRACT_TYPE"


def test_unknown_input_field_still_rejected(tmp_path: Path) -> None:
    body = minimal_contract().replace(
        "    device_id: dev-a1\n    metric: temperature\n",
        "    device_id: dev-a1\n    metric: temperature\n    rank: 3\n",
    )
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, body))
    assert caught.value.code == "E_CONTRACT_UNKNOWN"


def test_unknown_top_level_field(tmp_path: Path) -> None:
    body = minimal_contract() + "surprise: true\n"
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, body))
    assert caught.value.code == "E_CONTRACT_UNKNOWN"


def test_missing_required_field(tmp_path: Path) -> None:
    body = minimal_contract().replace("version: 1\n", "")
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, body))
    assert caught.value.code == "E_CONTRACT_MISSING"
    assert caught.value.field == "version"


def test_invalid_grid_interval(tmp_path: Path) -> None:
    body = minimal_contract().replace("end_us: 5000000", "end_us: 0")
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, body))
    assert caught.value.code == "E_CONTRACT_GRID"


def test_duplicate_input_id(tmp_path: Path) -> None:
    body = minimal_contract().replace(
        "grid:",
        """  - id: a1
    path: fixtures/series/dev-b2.csv
    device_id: dev-b2
    metric: temperature
grid:""",
    )
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, body))
    assert caught.value.code == "E_CONTRACT_DUPLICATE"


def test_override_must_reference_known_series(tmp_path: Path) -> None:
    body = minimal_contract() + """
resample:
  default: linear
  overrides:
    unknown-series: step
"""
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, body))
    assert caught.value.code == "E_CONTRACT_VALUE"


def test_clock_model_requires_anchors(tmp_path: Path) -> None:
    body = minimal_contract() + "clock_skew: linear\n"
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, body))
    assert caught.value.code == "E_CONTRACT_MISSING"
    assert caught.value.field == "anchors"


def test_invalid_yaml_is_coded(tmp_path: Path) -> None:
    with pytest.raises(ContractError) as caught:
        load_contract(write_contract(tmp_path, "version: [unterminated\n"))
    assert caught.value.code == "E_CONTRACT_YAML"
