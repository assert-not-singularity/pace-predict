"""Calibrate a smoothed speed so its integral matches known (lap/total) distances.

A smoothed speed has the right *shape* but its absolute scale inherits GPS bias (Garmin distance
runs a few percent short). Scaling the speed so that its time-integral reproduces the FIT's
recorded distances turns a plausible curve into a trustworthy ground truth. Only long laps are
trusted to set the scale — short reps carry several-percent GNSS error over their length.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import NamedTuple

import numpy as np
import numpy.typing as npt

type FloatArray = npt.NDArray[np.float64]

log = logging.getLogger(__name__)

_MIN_POINTS = 2  # need at least two samples to integrate a segment
_PLAUSIBLE_INTEGRAL_FRACTION = (
    0.5  # a long lap's integral must be at least this share of its distance
)


class LapSegment(NamedTuple):
    """A lap as a half-open record-index range ``[start, end)`` and its recorded distance (m)."""

    start: int
    end: int
    distance_m: float


def _as_array(values: npt.ArrayLike) -> FloatArray:
    return np.asarray(values, dtype=np.float64)


def integrate_speed(speed_mps: npt.ArrayLike, dt_s: float) -> FloatArray:
    """Cumulative distance (m) from a per-second speed via the trapezoid rule, starting at 0."""
    speed = _as_array(speed_mps)
    if len(speed) < _MIN_POINTS:
        return np.zeros(len(speed), dtype=np.float64)
    segments = (speed[:-1] + speed[1:]) / 2.0 * dt_s
    return np.concatenate([[0.0], np.cumsum(segments)])


def calibrate_to_distance(
    speed_mps: npt.ArrayLike, dt_s: float, target_distance_m: float
) -> FloatArray:
    """Uniformly scale speed so its total integral equals ``target_distance_m``."""
    speed = _as_array(speed_mps)
    total = float(integrate_speed(speed, dt_s)[-1]) if len(speed) else 0.0
    if total <= 0.0:
        return speed.copy()
    return speed * (target_distance_m / total)


def _segment_integral(speed: FloatArray, dt_s: float, segment: LapSegment) -> float:
    part = speed[segment.start : segment.end]
    if len(part) < _MIN_POINTS:
        return 0.0
    return float(integrate_speed(part, dt_s)[-1])


def calibrate_ground_truth(
    speed_mps: npt.ArrayLike,
    dt_s: float,
    segments: Sequence[LapSegment],
    *,
    min_segment_m: float = 1000.0,
) -> FloatArray:
    """Scale speed globally so the trustworthy long laps integrate to their distances.

    Only laps of at least ``min_segment_m`` set the scale (short laps carry large relative GNSS
    error), and a long lap whose integral is implausibly small (e.g. its speeds were zeroed by a
    GPS gap) is skipped. Falls back to total-distance calibration when no long lap is usable.
    """
    speed = _as_array(speed_mps)
    total_distance = 0.0
    total_integral = 0.0
    for segment in segments:
        if segment.distance_m < min_segment_m:
            continue
        integral = _segment_integral(speed, dt_s, segment)
        if integral < _PLAUSIBLE_INTEGRAL_FRACTION * segment.distance_m:
            log.warning(
                "skipping long lap [%d:%d] with implausible integral %.0f m vs distance %.0f m",
                segment.start,
                segment.end,
                integral,
                segment.distance_m,
            )
            continue
        total_distance += segment.distance_m
        total_integral += integral

    if total_integral <= 0.0:
        fallback = float(sum(segment.distance_m for segment in segments))
        log.warning(
            "no usable >=%.0f m lap for calibration; scaling to total %.0f m",
            min_segment_m,
            fallback,
        )
        return calibrate_to_distance(speed, dt_s, fallback)
    return speed * (total_distance / total_integral)


def calibrate_per_lap(
    speed_mps: npt.ArrayLike, dt_s: float, segments: Sequence[LapSegment]
) -> FloatArray:
    """Scale each lap segment independently so its integral matches that lap's distance."""
    speed = _as_array(speed_mps).copy()
    for segment in segments:
        integral = _segment_integral(speed, dt_s, segment)
        if integral > 0.0 and segment.distance_m > 0.0:
            speed[segment.start : segment.end] *= segment.distance_m / integral
    return speed
