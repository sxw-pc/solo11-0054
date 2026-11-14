"""Deterministic clock-offset and drift estimation from anchor events.

For each non-reference device the estimator computes the median raw offset and
a Theil-Sen robust slope (microseconds of offset per second of reference time).
Theil-Sen uses the median of all pairwise slopes, so it is deterministic and
needs no random sampling. Fewer than three shared events are reported as
``insufficient`` and never modify timestamps.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import DataError
from .series import SeriesPoint

ANCHOR_KEYS = {"version", "reference_device", "events"}
EVENT_KEYS = {"id", "observations"}


@dataclass(frozen=True)
class AnchorEvent:
    """One physical event observed by two or more device clocks."""

    event_id: str
    observations: dict[str, int]


@dataclass(frozen=True)
class AnchorSet:
    """Validated anchors plus the raw-file fingerprint."""

    reference_device: str
    events: tuple[AnchorEvent, ...]
    fingerprint: str


@dataclass(frozen=True)
class OffsetEstimate:
    """Estimated relationship ``device_ts - reference_ts`` over time."""

    device_id: str
    reference_device: str
    clock_skew: str
    status: str
    anchor_count: int
    median_offset_us: int | None = None
    intercept_us: float | None = None
    slope_us_per_s: float | None = None
    residual_median_us: float | None = None
    origin_us: int | None = None


def load_anchors(path: Path) -> AnchorSet:
    """Load and validate an anchor-event YAML file.

    Raises:
        DataError: ``E_ANCHOR_READ``, ``E_ANCHOR_YAML``, ``E_ANCHOR_TYPE``,
            ``E_ANCHOR_MISSING``, ``E_ANCHOR_UNKNOWN``, ``E_ANCHOR_VALUE`` or
            ``E_ANCHOR_FLIP``.
    """
    shown = path.name
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DataError("E_ANCHOR_READ", f"cannot read anchors: {exc}", path=shown) from exc
    try:
        document = yaml.safe_load(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DataError("E_ANCHOR_YAML", f"invalid anchor YAML: {exc}", path=shown) from exc
    if not isinstance(document, dict):
        raise DataError("E_ANCHOR_TYPE", "anchor root must be a mapping", path=shown)
    unknown = sorted(set(document) - ANCHOR_KEYS)
    if unknown:
        raise DataError("E_ANCHOR_UNKNOWN", f"unknown anchor field: {unknown[0]}", path=shown, field=unknown[0])
    for required in ("version", "reference_device", "events"):
        if required not in document:
            raise DataError("E_ANCHOR_MISSING", f"missing anchor field: {required}", path=shown, field=required)
    if document["version"] != 1:
        raise DataError("E_ANCHOR_VALUE", "anchor version must be 1", path=shown, field="version")
    reference = document["reference_device"]
    if not isinstance(reference, str) or not reference.strip():
        raise DataError("E_ANCHOR_VALUE", "reference_device must be text", path=shown, field="reference_device")
    raw_events = document["events"]
    if not isinstance(raw_events, list) or not raw_events:
        raise DataError("E_ANCHOR_VALUE", "events must be a non-empty list", path=shown, field="events")

    events: list[AnchorEvent] = []
    seen: set[str] = set()
    reference_seen = False
    for index, entry in enumerate(raw_events):
        field = f"events[{index}]"
        if not isinstance(entry, dict):
            raise DataError("E_ANCHOR_TYPE", f"{field} must be a mapping", path=shown, field=field)
        unknown = sorted(set(entry) - EVENT_KEYS)
        if unknown:
            raise DataError("E_ANCHOR_UNKNOWN", f"unknown event field: {unknown[0]}", path=shown, field=field)
        event_id = entry.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise DataError("E_ANCHOR_VALUE", f"{field}.id must be text", path=shown, field=field)
        if event_id in seen:
            raise DataError("E_ANCHOR_VALUE", f"duplicate event id: {event_id}", path=shown, field=field)
        seen.add(event_id)
        observations = entry.get("observations")
        if not isinstance(observations, dict) or len(observations) < 2:
            raise DataError("E_ANCHOR_VALUE", f"{field}.observations needs at least two devices", path=shown, field=field)
        parsed: dict[str, int] = {}
        for device, timestamp in observations.items():
            if not isinstance(device, str) or not device.strip():
                raise DataError("E_ANCHOR_VALUE", "observation device id must be text", path=shown, field=field)
            if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                raise DataError("E_ANCHOR_VALUE", "anchor timestamps must be integer microseconds", path=shown, field=field)
            parsed[device] = int(timestamp)
        reference_seen = reference_seen or reference in parsed
        events.append(AnchorEvent(event_id, parsed))
    if not reference_seen:
        raise DataError("E_ANCHOR_VALUE", "reference device is absent from every event", path=shown, field="reference_device")
    _check_offset_sign_consistency(events, reference, shown)
    return AnchorSet(reference, tuple(events), hashlib.sha256(raw).hexdigest())


def _check_offset_sign_consistency(events: Sequence[AnchorEvent], reference: str, path: str) -> None:
    """Reject a per-device sign flip of ``device_ts - reference_ts``.

    Events are walked in file order. A comparison is skipped when either the
    device or the reference device is absent from an event, so gaps never count
    as a flip. A zero difference carries no sign and does not establish one.
    """
    last_sign: dict[str, int] = {}
    last_event_id: dict[str, str] = {}
    last_diff: dict[str, int] = {}
    for event in events:
        reference_ts = event.observations.get(reference)
        if reference_ts is None:
            continue
        for device, device_ts in event.observations.items():
            if device == reference:
                continue
            diff = int(device_ts) - int(reference_ts)
            sign = (diff > 0) - (diff < 0)
            if sign == 0:
                continue
            previous = last_sign.get(device)
            if previous is not None and previous != sign:
                raise DataError(
                    "E_ANCHOR_FLIP",
                    f"offset sign flipped for device {device} between events "
                    f"{last_event_id[device]} and {event.event_id}",
                    path=path,
                    field="events",
                    detail={
                        "device": device,
                        "first_event_id": last_event_id[device],
                        "flip_event_id": event.event_id,
                        "first_offset_us": last_diff[device],
                        "flip_offset_us": diff,
                    },
                )
            last_sign[device] = sign
            last_event_id[device] = event.event_id
            last_diff[device] = diff


def estimate_offsets(
    anchors: AnchorSet,
    devices: Iterable[str],
    clock_skew: str,
) -> dict[str, OffsetEstimate]:
    """Estimate one offset model per input device.

    Device order is preserved, with the anchor reference device inserted first
    when it is not itself an input. The returned statuses are ``reference``,
    ``disabled``, ``insufficient``, ``no_anchors``, ``degenerate`` or ``ok``.
    """
    ordered: list[str] = []
    for device in [anchors.reference_device, *devices]:
        if device not in ordered:
            ordered.append(device)
    estimates: dict[str, OffsetEstimate] = {}
    for device in ordered:
        estimates[device] = _estimate_one(anchors, device, clock_skew)
    return estimates


def _estimate_one(anchors: AnchorSet, device: str, clock_skew: str) -> OffsetEstimate:
    pairs = sorted(
        (event.observations[anchors.reference_device], event.observations[device])
        for event in anchors.events
        if anchors.reference_device in event.observations and device in event.observations
    )
    count = len(pairs)
    if device == anchors.reference_device:
        return OffsetEstimate(device, anchors.reference_device, clock_skew, "reference", count, 0, 0.0, 0.0, 0.0)
    if clock_skew == "none":
        return OffsetEstimate(device, anchors.reference_device, clock_skew, "disabled", count)
    if count == 0:
        return OffsetEstimate(device, anchors.reference_device, clock_skew, "no_anchors", 0)
    if count < 3:
        return OffsetEstimate(device, anchors.reference_device, clock_skew, "insufficient", count)

    offsets = [device_ts - reference_ts for reference_ts, device_ts in pairs]
    median_offset = _round_half_away(statistics.median(offsets))
    origin = pairs[0][0]
    if clock_skew == "constant":
        slope = 0.0
        intercept = float(median_offset)
    else:
        slopes: list[float] = []
        for left in range(len(pairs) - 1):
            for right in range(left + 1, len(pairs)):
                delta_ref = pairs[right][0] - pairs[left][0]
                if delta_ref:
                    slopes.append((offsets[right] - offsets[left]) * 1_000_000.0 / delta_ref)
        if not slopes:
            return OffsetEstimate(device, anchors.reference_device, clock_skew, "insufficient", count)
        slope = _clean_float(statistics.median(slopes))
        intercept = _clean_float(
            statistics.median(
                offsets[index] - slope * (pairs[index][0] - origin) / 1_000_000.0
                for index in range(len(pairs))
            )
        )
    residuals = [
        abs(offsets[index] - (intercept + slope * (pairs[index][0] - origin) / 1_000_000.0))
        for index in range(len(pairs))
    ]
    residual = _clean_float(statistics.median(residuals))
    status = "ok"
    if abs(1.0 + slope / 1_000_000.0) < 1e-12:
        status = "degenerate"
    return OffsetEstimate(
        device_id=device,
        reference_device=anchors.reference_device,
        clock_skew=clock_skew,
        status=status,
        anchor_count=count,
        median_offset_us=median_offset,
        intercept_us=intercept,
        slope_us_per_s=slope,
        residual_median_us=residual,
        origin_us=origin,
    )


def correct_ts_us(ts_us: int, estimate: OffsetEstimate) -> int:
    """Map one device timestamp onto the reference timeline.

    Only ``ok`` estimates alter a timestamp. The linear inversion is
    ``t_ref = (t_dev - intercept + slope * origin / 1e6) / (1 + slope / 1e6)``.
    Rounding is half-away-from-zero so it is platform-independent.
    """
    if estimate.status != "ok" or estimate.intercept_us is None or estimate.slope_us_per_s is None:
        return ts_us
    slope = estimate.slope_us_per_s
    origin = estimate.origin_us or 0
    denominator = 1.0 + slope / 1_000_000.0
    corrected = (ts_us - estimate.intercept_us + (slope / 1_000_000.0) * origin) / denominator
    return _round_half_away(corrected)


def correct_points(points: Sequence[SeriesPoint], estimate: OffsetEstimate) -> list[SeriesPoint]:
    """Apply an offset estimate to every point, preserving source line numbers."""
    return [SeriesPoint(correct_ts_us(point.ts_us, estimate), point.value, point.line) for point in points]


def _round_half_away(value: float) -> int:
    if value >= 0:
        return math.floor(value + 0.5)
    return math.ceil(value - 0.5)


def _clean_float(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return 0.0 if value == 0 else float(value)
