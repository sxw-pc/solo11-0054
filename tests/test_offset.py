from __future__ import annotations

from pathlib import Path

import pytest

from sensoralign.core.errors import DataError
from sensoralign.core.offset import (
    correct_points,
    correct_ts_us,
    estimate_offsets,
    load_anchors,
)
from sensoralign.core.series import SeriesPoint

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_700_000_000_000_000


@pytest.fixture()
def anchors():
    return load_anchors(ROOT / "fixtures" / "anchor" / "anchors.yaml")


def test_constant_offset_estimation_and_correction(anchors) -> None:
    estimates = estimate_offsets(anchors, ["dev-a1", "dev-b2", "dev-d4"], "constant")
    b2 = estimates["dev-b2"]
    assert b2.status == "ok"
    assert b2.anchor_count == 5
    assert b2.median_offset_us == 2_000_000
    assert b2.slope_us_per_s == 0.0
    assert correct_ts_us(BASE + 10_000_000 + 2_000_000, b2) == BASE + 10_000_000
    assert estimates["dev-d4"].median_offset_us == -1_000_000


def test_linear_theil_sen_drift(anchors) -> None:
    estimates = estimate_offsets(anchors, ["dev-a1", "dev-c3"], "linear")
    c3 = estimates["dev-c3"]
    assert c3.status == "ok"
    assert c3.slope_us_per_s == pytest.approx(100.0)
    assert c3.intercept_us == pytest.approx(0.0)
    assert c3.residual_median_us == pytest.approx(0.0)
    raw = BASE + 40_000_000 + 4_000
    assert correct_ts_us(raw, c3) == BASE + 40_000_000


def test_reference_and_no_anchor_statuses(anchors) -> None:
    estimates = estimate_offsets(anchors, ["dev-a1", "dev-z9"], "linear")
    assert estimates["dev-a1"].status == "reference"
    assert estimates["dev-z9"].status == "no_anchors"
    assert estimates["dev-z9"].anchor_count == 0


def test_insufficient_anchors_are_not_applied() -> None:
    sparse = load_anchors(ROOT / "fixtures" / "anchor" / "anchors_sparse.yaml")
    estimate = estimate_offsets(sparse, ["dev-ab", "dev-b2"], "constant")["dev-b2"]
    assert estimate.status == "insufficient"
    assert estimate.anchor_count == 2
    raw = BASE + 2_000_000
    assert correct_ts_us(raw, estimate) == raw


def test_none_model_never_changes_timestamps(anchors) -> None:
    estimate = estimate_offsets(anchors, ["dev-a1", "dev-b2"], "none")["dev-b2"]
    assert estimate.status == "disabled"
    assert correct_ts_us(123, estimate) == 123


def test_correct_points_preserves_lines(anchors) -> None:
    estimate = estimate_offsets(anchors, ["dev-a1", "dev-b2"], "constant")["dev-b2"]
    points = [SeriesPoint(BASE + 2_000_000, 1.5, 9)]
    corrected = correct_points(points, estimate)
    assert corrected[0].ts_us == BASE
    assert corrected[0].line == 9
    assert corrected[0].value == 1.5


def test_malformed_anchor_event_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "anchors.yaml"
    path.write_text(
        "version: 1\nreference_device: dev-a1\nevents:\n  - id: one\n    observations:\n      dev-a1: 10\n",
        encoding="utf-8",
    )
    with pytest.raises(DataError) as caught:
        load_anchors(path)
    assert caught.value.code == "E_ANCHOR_VALUE"
