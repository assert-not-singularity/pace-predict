"""Compose the ground-truth per-second speed for a parsed activity.

Recipe (validated on real data): median-clean Garmin's ``enhanced_speed`` (best fused per-second
speed), then scale so the trustworthy >=1 km laps integrate to their recorded distances. Short reps
and track runs carry large GNSS error and must not set the calibration scale.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

from pace_predict.groundtruth.calibrate import calibrate_ground_truth
from pace_predict.groundtruth.smoothing import robust_speed
from pace_predict.io import schema as S
from pace_predict.io.fit import Activity

type FloatArray = npt.NDArray[np.float64]

_MIN_POINTS = 2


def median_dt_s(records: pd.DataFrame) -> float:
    """Median sampling interval (seconds); defaults to 1.0 for a degenerate frame."""
    t = records[S.T_S].to_numpy()
    if len(t) < _MIN_POINTS:
        return 1.0
    return float(np.median(np.diff(t)))


def lap_end_indices(records: pd.DataFrame, laps: pd.DataFrame) -> tuple[list[int], list[float]]:
    """End-exclusive sample index and distance for each lap, aligned to the record time axis."""
    n = len(records)
    if len(laps) == 0:
        total = float(np.nanmax(records[S.DISTANCE_M].to_numpy())) if n else 0.0
        return [n], [total]

    t_s = records[S.T_S].to_numpy()
    start0 = records[S.TIMESTAMP].min()
    ends: list[int] = []
    distances: list[float] = []
    for end_time, distance in zip(laps[S.LAP_END_TIME], laps[S.LAP_DISTANCE_M], strict=True):
        if pd.isna(end_time):
            ends.append(n)
        else:
            ends.append(
                int(np.searchsorted(t_s, (end_time - start0).total_seconds(), side="right"))
            )
        distances.append(0.0 if pd.isna(distance) else float(distance))
    return ends, distances


def ground_truth_speed(activity: Activity, *, min_segment_m: float = 1000.0) -> FloatArray:
    """Per-second ground-truth speed (m/s) for the activity's records."""
    records = activity.records
    dt = median_dt_s(records)
    cleaned = robust_speed(records[S.SPEED_MPS].to_numpy(), dt)
    ends, distances = lap_end_indices(records, activity.laps)
    return calibrate_ground_truth(cleaned, dt, ends, distances, min_segment_m=min_segment_m)
