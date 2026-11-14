from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import pytest

from sensoralign.core import offset as offset_module
from sensoralign.core.errors import DataError
from sensoralign.core.offset import (
    AnchorEvent,
    AnchorSet,
    _clean_float,
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


def _synthetic_anchors() -> AnchorSet:
    """Many devices with varied drift, jitter, sparse and degenerate anchors.

    The jitter and non-round slopes produce non-trivial float pairwise slopes,
    so any reordering inside one device's computation would surface as a
    differing median.
    """
    reference = "dev-ref"
    drift_devices = [f"dev-drift-{index}" for index in range(8)]
    sparse = "dev-sparse"
    blind = "dev-blind"
    degenerate = "dev-degenerate"
    events: list[AnchorEvent] = []
    for event_index in range(10):
        ref_ts = BASE + event_index * 5_000_000
        observations: dict[str, int] = {reference: ref_ts}
        for index, device in enumerate(drift_devices):
            slope_us_per_s = 3.3 * (index + 1)
            jitter = ((event_index * 7 + index * 3) % 5 - 2) * 13
            observations[device] = ref_ts + round(
                ref_ts / 1_000_000.0 * slope_us_per_s
            ) + jitter
        # A slope of -1,000,000 us/s trips the degenerate guard.
        observations[degenerate] = ref_ts - (ref_ts - BASE)
        if event_index < 2:
            observations[sparse] = ref_ts + 9_000
        events.append(AnchorEvent(f"evt-{event_index:03d}", observations))
    # Events without the reference device are ignored for every estimate.
    events.append(
        AnchorEvent("evt-extra", {blind: BASE + 1_000, "dev-other": BASE + 2_000})
    )
    return AnchorSet(reference, tuple(events), "synthetic-fingerprint")


def _device_order(estimates) -> list[str]:
    return list(estimates.keys())


def test_parallel_matches_serial_bit_for_bit_on_many_devices() -> None:
    anchors = _synthetic_anchors()
    devices = [
        *(f"dev-drift-{index}" for index in range(8)),
        "dev-sparse",
        "dev-blind",
        "dev-degenerate",
    ]
    serial = estimate_offsets(anchors, devices, "linear", max_workers=1)
    parallel = estimate_offsets(anchors, devices, "linear")
    assert _device_order(parallel) == _device_order(serial)
    assert _device_order(serial)[0] == "dev-ref"
    assert _device_order(serial) == ["dev-ref", *devices]
    for device in serial:
        before = serial[device]
        after = parallel[device]
        assert after == before
        for field in (
            "median_offset_us",
            "slope_us_per_s",
            "intercept_us",
            "residual_median_us",
        ):
            assert getattr(after, field) == getattr(before, field)
            assert type(getattr(after, field)) is type(getattr(before, field))
    assert serial["dev-sparse"].status == "insufficient"
    assert serial["dev-blind"].status == "no_anchors"
    assert serial["dev-degenerate"].status == "degenerate"
    assert all(serial[f"dev-drift-{index}"].status == "ok" for index in range(8))


def test_parallel_and_constant_model_keep_order_and_statuses() -> None:
    anchors = _synthetic_anchors()
    devices = ["dev-blind", "dev-sparse", "dev-drift-0", "dev-drift-7"]
    serial = estimate_offsets(anchors, devices, "constant", max_workers=1)
    parallel = estimate_offsets(anchors, devices, "constant", max_workers=4)
    assert _device_order(parallel) == ["dev-ref", "dev-blind", "dev-sparse", "dev-drift-0", "dev-drift-7"]
    assert [estimate.status for estimate in parallel.values()] == [
        "reference",
        "no_anchors",
        "insufficient",
        "ok",
        "ok",
    ]
    for device in serial:
        assert parallel[device] == serial[device]


def test_parallel_schedule_uses_worker_threads_without_reordering(monkeypatch) -> None:
    anchors = _synthetic_anchors()
    devices = [f"dev-drift-{index}" for index in range(6)]
    seen_threads: set[int] = set()
    lock = threading.Lock()
    original = offset_module._estimate_one

    def tracked(anchor_set, device, clock_skew):
        current = threading.current_thread()
        if current is not threading.main_thread():
            with lock:
                seen_threads.add(threading.get_ident())
            # Hold the worker briefly so several tasks must be in flight at
            # once instead of being drained one-by-one by a single thread.
            time.sleep(0.01)
        return original(anchor_set, device, clock_skew)

    monkeypatch.setattr(offset_module, "_estimate_one", tracked)
    estimates = estimate_offsets(anchors, devices, "linear", max_workers=4)
    assert len(seen_threads) >= 2
    assert _device_order(estimates) == ["dev-ref", *devices]


def test_single_device_skips_the_pool(monkeypatch) -> None:
    anchors = _synthetic_anchors()
    original = offset_module._estimate_one
    calls: list[str] = []

    def tracking(anchor_set, device, clock_skew):
        calls.append(threading.current_thread().name)
        return original(anchor_set, device, clock_skew)

    monkeypatch.setattr(offset_module, "_estimate_one", tracking)
    estimate_offsets(anchors, ["dev-ref"], "linear")
    assert calls == ["MainThread"]


def test_invalid_max_workers_is_rejected() -> None:
    anchors = _synthetic_anchors()
    with pytest.raises(ValueError, match="max_workers"):
        estimate_offsets(anchors, ["dev-drift-0"], "linear", max_workers=0)
    with pytest.raises(ValueError, match="max_workers"):
        estimate_offsets(anchors, ["dev-drift-0"], "linear", max_workers=-2)


def test_pairwise_slope_traversal_order_is_preserved(monkeypatch) -> None:
    anchors = _synthetic_anchors()
    captured: list[tuple[float, ...]] = []
    median = offset_module.statistics.median

    def capturing_median(values):
        materialized = tuple(values)
        if materialized and isinstance(materialized[0], float) and len(materialized) > 1:
            captured.append(materialized)
        return median(materialized)

    monkeypatch.setattr(offset_module.statistics, "median", capturing_median)
    estimate_offsets(anchors, ["dev-drift-3"], "linear", max_workers=4)
    # The first float-typed median input is the pairwise Theil-Sen slope list;
    # intercept and residual medians also use floats but run afterwards.
    slope_sequence = captured[0]
    assert len(slope_sequence) == 45
    # Re-derive the same pairwise slopes by hand, in the original left/right
    # nested-loop traversal order; the median input must be exactly that order.
    pairs = sorted(
        (event.observations["dev-ref"], event.observations["dev-drift-3"])
        for event in anchors.events
        if "dev-ref" in event.observations and "dev-drift-3" in event.observations
    )
    offsets = [device_ts - ref_ts for ref_ts, device_ts in pairs]
    expected: list[float] = []
    for left in range(len(pairs) - 1):
        for right in range(left + 1, len(pairs)):
            delta_ref = pairs[right][0] - pairs[left][0]
            if delta_ref:
                expected.append((offsets[right] - offsets[left]) * 1_000_000.0 / delta_ref)
    assert slope_sequence == tuple(expected)


def test_clean_float_normalizes_zero_and_passthrough_values() -> None:
    assert _clean_float(0.0) == 0.0
    assert math.copysign(1.0, _clean_float(-0.0)) == 1.0
    assert _clean_float(float("nan")) == 0.0
    assert _clean_float(float("inf")) == 0.0
    assert _clean_float(float("-inf")) == 0.0
    assert _clean_float(12.5) == 12.5
    assert _clean_float(-3.25) == -3.25
    assert _clean_float(1e-300) == 1e-300
