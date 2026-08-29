"""Compose the ground-truth per-second speed for a parsed activity.

Recipe (validated on real data): median-clean Garmin's ``enhanced_speed`` (best fused per-second
speed), then scale so the trustworthy >=1 km laps integrate to their recorded distances. Short reps
carry several-percent GNSS error and must not set the scale.

This estimator is best-effort for *any* run. Deciding which runs to trust — excluding track runs
(GPS overshoot) and holding out short-interval runs for validation — is a dataset-selection concern
handled by the caller with :func:`pace_predict.groundtruth.quality.is_track_like`, not baked in
here.
"""

from __future__ import annotations

import logging

import numpy as np
import numpy.typing as npt
import pandas as pd

from pace_predict.groundtruth.calibrate import LapSegment, calibrate_ground_truth
from pace_predict.groundtruth.smoothing import robust_speed
from pace_predict.io import schema as S
from pace_predict.io.fit import Activity

type FloatArray = npt.NDArray[np.float64]

log = logging.getLogger(__name__)

_MIN_POINTS = 2


def median_dt_s(records: pd.DataFrame) -> float:
    """Median sampling interval (seconds) over finite, positive gaps; 1.0 for a degenerate frame.

    Filtering to positive gaps guards against duplicate or non-monotonic timestamps that would
    otherwise yield a zero or NaN interval and divide-by-zero downstream.
    """
    t = records[S.T_S].to_numpy()
    if len(t) < _MIN_POINTS:
        return 1.0
    gaps = np.diff(t)
    positive = gaps[np.isfinite(gaps) & (gaps > 0.0)]
    if len(positive) == 0:
        return 1.0
    return float(np.median(positive))


def lap_segments(records: pd.DataFrame, laps: pd.DataFrame) -> list[LapSegment]:
    """Map laps to contiguous half-open record-index ranges using their start times.

    Laps are tiled by consecutive start times (each lap runs until the next lap's start, the last
    to the end of the records), so every record belongs to exactly one lap — no double-counting a
    boundary sample and no folding un-lapped records into a neighbour. Laps with a missing start
    time or distance are dropped with a warning. Falls back to one whole-run segment.
    """
    n = len(records)
    if n == 0:
        return []
    distance = records[S.DISTANCE_M].to_numpy()
    whole_run = [
        LapSegment(0, n, float(np.nanmax(distance)) if np.isfinite(distance).any() else 0.0)
    ]
    if len(laps) == 0:
        return whole_run

    t_s = records[S.T_S].to_numpy()
    start0 = records[S.TIMESTAMP].min()
    starts: list[tuple[int, float]] = []
    for start_time, lap_distance in zip(
        laps[S.LAP_START_TIME], laps[S.LAP_DISTANCE_M], strict=True
    ):
        if pd.isna(start_time) or pd.isna(lap_distance):
            log.warning("dropping lap with missing start time or distance")
            continue
        idx = int(
            np.clip(np.searchsorted(t_s, (start_time - start0).total_seconds(), side="left"), 0, n)
        )
        starts.append((idx, float(lap_distance)))

    if not starts:
        return whole_run
    starts.sort()

    segments: list[LapSegment] = []
    for i, (start_idx, lap_distance) in enumerate(starts):
        end_idx = starts[i + 1][0] if i + 1 < len(starts) else n
        if end_idx > start_idx:
            segments.append(LapSegment(start_idx, end_idx, lap_distance))
    return segments or whole_run


def ground_truth_speed(activity: Activity, *, min_segment_m: float = 1000.0) -> FloatArray:
    """Per-second ground-truth speed (m/s) for the activity's records."""
    records = activity.records
    dt = median_dt_s(records)
    cleaned = robust_speed(records[S.SPEED_MPS].to_numpy(), dt)
    if not np.any(cleaned > 0.0):
        log.warning(
            "%s has no positive speed; ground truth is all zero", activity.meta.source_path.name
        )
    return calibrate_ground_truth(
        cleaned, dt, lap_segments(records, activity.laps), min_segment_m=min_segment_m
    )
