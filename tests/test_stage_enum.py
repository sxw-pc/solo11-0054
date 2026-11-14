from __future__ import annotations

import pytest

from sensoralign.core.errors import DataError, Event, Stage
from sensoralign.core.series import SeriesPoint
from sensoralign.core.timestamps import sanitize_points


def point(ts_us: int, value: float, line: int = 1) -> SeriesPoint:
    return SeriesPoint(ts_us, value, line)


def test_event_rejects_bare_string_stage() -> None:
    with pytest.raises(TypeError):
        Event("W_TS_DUPLICATE", "warning", "timestamp_hygiene", "legacy string stage")


def test_data_error_rejects_bare_string_stage() -> None:
    with pytest.raises(TypeError):
        DataError("E_TS_DUPLICATE", "duplicate observation", stage="timestamp_hygiene")


def test_data_error_defaults_to_load_stage() -> None:
    assert DataError("E_INPUT_READ", "cannot read").stage is Stage.LOAD
    assert DataError("E_INPUT_READ", "cannot read").as_dict()["stage"] == "load"


def test_stage_values_are_the_legacy_strings() -> None:
    assert Stage.TIMESTAMP_HYGIENE.value == "timestamp_hygiene"
    assert Stage.CLOCK_OFFSET.value == "clock_offset"
    assert Stage.CLOCK_CORRECTION.value == "clock_correction"
    assert Stage.GAP_SCAN.value == "gap_scan"
    assert Stage.LOAD.value == "load"
    assert Stage.RESAMPLE.value == "resample"
    assert Stage.QUALITY.value == "quality"
    assert {member.value for member in Stage} == {
        "load",
        "timestamp_hygiene",
        "clock_offset",
        "clock_correction",
        "gap_scan",
        "resample",
        "quality",
    }


def test_reject_path_data_error_keeps_legacy_stage_string() -> None:
    with pytest.raises(DataError) as caught:
        sanitize_points(
            [point(100, 1.0, 1), point(100, 1.0, 2)],
            duplicate_policy="reject",
        )
    error = caught.value
    assert error.code == "E_TS_DUPLICATE"
    assert error.stage is Stage.TIMESTAMP_HYGIENE
    # The stage must serialize to the verbatim legacy string because ledger
    # rows and result_sha256 depend on it.
    assert error.stage.value == "timestamp_hygiene"
    assert error.as_dict()["stage"] == "timestamp_hygiene"


def test_event_record_serializes_stage_as_string() -> None:
    event = Event("W_GAP", "warning", Stage.GAP_SCAN, "gap")
    record = event.to_record(1)
    assert record["stage"] == "gap_scan"
    assert isinstance(record["stage"], str)
