from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sensoralign.core.errors import DataError
from sensoralign.io.loaders import file_sha256, load_series


def csrf(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "input.csv"
    path.write_bytes(body.encode("utf-8-sig"))
    return path


def test_load_csv_with_bom_and_fingerprint(tmp_path: Path) -> None:
    path = csrf(tmp_path, "ts_us,device_id,metric,value\n10,dev-a1,temperature,20.5\n20,dev-a1,temperature,20.6\n")
    series = load_series(path, series_id="a1", fmt="csv", expected_device="dev-a1", expected_metric="temperature")
    assert [(p.ts_us, p.value) for p in series.points] == [(10, 20.5), (20, 20.6)]
    assert series.fingerprint == hashlib.sha256(path.read_bytes()).hexdigest()
    assert series.input_format == "csv"
    assert file_sha256(path) == series.fingerprint


def test_load_jsonl_and_infer_format(tmp_path: Path) -> None:
    path = tmp_path / "input.jsonl"
    path.write_text(
        '{"ts_us": 1, "device_id": "dev-b2", "metric": "pressure", "value": 3}\n'
        '{"ts_us": 2, "device_id": "dev-b2", "metric": "pressure", "value": 4}\n',
        encoding="utf-8",
    )
    series = load_series(path, series_id="b2")
    assert series.input_format == "jsonl"
    assert [point.line for point in series.points] == [1, 2]


def test_csv_missing_column_reports_code(tmp_path: Path) -> None:
    path = csrf(tmp_path, "ts_us,device_id,metric\n1,dev-a1,temperature\n")
    with pytest.raises(DataError) as caught:
        load_series(path, series_id="a1")
    assert caught.value.code == "E_COLUMN_MISSING"
    assert caught.value.field == "value"


def test_jsonl_missing_field_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "input.jsonl"
    path.write_text('{"ts_us": 1, "device_id": "dev-a1", "metric": "temperature"}\n', encoding="utf-8")
    with pytest.raises(DataError) as caught:
        load_series(path, series_id="a1")
    assert caught.value.code == "E_FIELD_MISSING"
    assert caught.value.line == 1
    assert caught.value.field == "value"


def test_float_timestamp_is_rejected_with_location(tmp_path: Path) -> None:
    path = csrf(tmp_path, "ts_us,device_id,metric,value\n1.5,dev-a1,temperature,20\n")
    with pytest.raises(DataError) as caught:
        load_series(path, series_id="a1")
    assert caught.value.code == "E_TS_FLOAT"
    assert caught.value.line == 2
    assert caught.value.field == "ts_us"


def test_non_finite_value_is_rejected(tmp_path: Path) -> None:
    path = csrf(tmp_path, "ts_us,device_id,metric,value\n1,dev-a1,temperature,nan\n")
    with pytest.raises(DataError) as caught:
        load_series(path, series_id="a1")
    assert caught.value.code == "E_VALUE_NOT_FINITE"


def test_expected_identity_and_empty_input(tmp_path: Path) -> None:
    path = csrf(tmp_path, "ts_us,device_id,metric,value\n1,dev-x,temperature,20\n")
    with pytest.raises(DataError) as caught:
        load_series(path, series_id="a1", expected_device="dev-a1")
    assert caught.value.code == "E_DEVICE_MISMATCH"

    empty = csrf(tmp_path, "ts_us,device_id,metric,value\n")
    with pytest.raises(DataError) as caught_empty:
        load_series(empty, series_id="empty")
    assert caught_empty.value.code == "E_EMPTY_INPUT"
