"""Calibrate a smoothed speed so its integral matches known (lap/total) distances.

A smoothed speed has the right *shape* but its absolute scale inherits GPS bias (Garmin distance
runs a few percent short). Scaling the speed so that its time-integral reproduces the FIT's
recorded distances turns a plausible curve into a trustworthy ground truth.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

type FloatArray = npt.NDArray[np.float64]

_MIN_POINTS = 2  # need at least two samples to integrate a segment


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


def calibrate_ground_truth(
    speed_mps: npt.ArrayLike,
    dt_s: float,
    lap_end_indices: Sequence[int],
    lap_distances_m: Sequence[float],
    *,
    min_segment_m: float = 1000.0,
) -> FloatArray:
    """Scale speed globally so the trustworthy long laps integrate to their distances.

    Short laps carry large relative GNSS distance error (a few percent over 400 m), so only laps
    of at least ``min_segment_m`` set the scale; it is then applied to the whole run. Falls back to
    total-distance calibration if no long laps exist.
    """
    speed = _as_array(speed_mps)
    total_distance = 0.0
    total_integral = 0.0
    start = 0
    for end, distance in zip(lap_end_indices, lap_distances_m, strict=True):
        segment = speed[start:end]
        if distance >= min_segment_m and len(segment) >= _MIN_POINTS:
            total_distance += distance
            total_integral += float(integrate_speed(segment, dt_s)[-1])
        start = end
    if total_integral <= 0.0:
        return calibrate_to_distance(speed, dt_s, float(sum(lap_distances_m)))
    return speed * (total_distance / total_integral)


def calibrate_per_lap(
    speed_mps: npt.ArrayLike,
    dt_s: float,
    lap_end_indices: Sequence[int],
    lap_distances_m: Sequence[float],
) -> FloatArray:
    """Scale each lap segment so its integral matches that lap's recorded distance.

    ``lap_end_indices`` are end-exclusive sample indices, one per lap (aligned with
    ``lap_distances_m``); the final index should be the sample count.
    """
    speed = _as_array(speed_mps).copy()
    start = 0
    for end, distance in zip(lap_end_indices, lap_distances_m, strict=True):
        segment = speed[start:end]
        integral = float(integrate_speed(segment, dt_s)[-1]) if len(segment) >= _MIN_POINTS else 0.0
        if integral > 0.0 and distance > 0.0:
            speed[start:end] = segment * (distance / integral)
        start = end
    return speed
