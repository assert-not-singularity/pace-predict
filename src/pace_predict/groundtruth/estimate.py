"""Compose the ground-truth per-second speed for a parsed activity.

Recipe (validated on real data): classify each sample by cadence phase (steady effort => steady
pace), then reconstruct Garmin's fused ``enhanced_speed`` per segment — a median-anchored,
drift-capped degree-1 line inside each steady segment, and a linear ramp between neighbouring steady
levels across each transition (so an interval boundary is a crisp step, not the lagged GNSS
re-convergence). A spike-robust median prefilter feeds the per-segment fits, and short GNSS dropouts
are interpolated first. Finally the shape is scaled so the trustworthy >=1 km laps integrate to
their recorded distances; short reps carry several-percent GNSS error and must not set the scale.

This estimator is best-effort for *any* run (a run with no usable cadence falls back to an
unsegmented robust smoothing). Deciding which runs/segments to trust — excluding track runs (GPS
overshoot) and degraded-GNSS stretches — is a dataset-selection concern handled by the caller with
the helpers in :mod:`pace_predict.groundtruth.quality`, not baked in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
import pandas as pd

from pace_predict.groundtruth.calibrate import LapSegment, calibrate_ground_truth
from pace_predict.groundtruth.quality import (
    STEADY,
    PhaseParams,
    PhaseSegment,
    phase_labels,
    phase_segments,
)
from pace_predict.groundtruth.smoothing import guard_dropouts, median_clean, robust_speed
from pace_predict.io import schema as S
from pace_predict.io.fit import Activity

type FloatArray = npt.NDArray[np.float64]

log = logging.getLogger(__name__)

_MIN_POINTS = 2
_MIN_FIT_POINTS = 3  # need >= 3 samples for a meaningful degree-1 fit
_MIN_CADENCE_FRACTION = 0.5  # below this share of finite cadence, the run cannot be segmented


@dataclass(frozen=True)
class SegmentParams:
    """Tuning for the segmented ground-truth reconstruction.

    Defaults are the values validated on real interval/steady/woods runs; every field is exposed so
    the notebook (and per-runner calibration) can tune them without touching code.
    """

    phase: PhaseParams = field(default_factory=PhaseParams)  # cadence phase classification
    prefilter_s: float = 7.0  # median window that rejects spikes before the steady fit
    max_drift_mps: float = 0.25  # cap on a steady segment's total drift (gentle hills only)
    dropout_gap_s: float = 8.0  # longest GNSS dropout interpolated across


_DEFAULT_PARAMS = SegmentParams()  # shared immutable default for the functions below


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


def segment_pace(
    records: pd.DataFrame, dt_s: float, *, params: SegmentParams = _DEFAULT_PARAMS
) -> FloatArray:
    """Per-second speed shape: near-constant within each steady effort, ramped across transitions.

    The run is segmented by cadence phase (steady effort => steady pace), GNSS dropouts are
    interpolated over, and ``enhanced_speed`` is fit per segment: a median-anchored, drift-capped
    degree-1 line inside each steady segment (flat, with only a gentle slope for small hills), and a
    linear ramp between neighbouring steady levels across each accel/decel transition. The result is
    crisp at interval starts — where GNSS pace lags — because the ramp follows the instant cadence
    response, not the GNSS re-convergence.
    """
    speed = records[S.SPEED_MPS].to_numpy()
    n = len(speed)
    if n < _MIN_POINTS:
        return robust_speed(speed, dt_s)

    # Clean GNSS dropouts, classify the effort phase, reconstruct per segment.
    guarded = guard_dropouts(speed, dt_s, max_gap_s=params.dropout_gap_s)
    cadence = records[S.CADENCE_SPM].to_numpy()
    if np.isfinite(cadence).mean() < _MIN_CADENCE_FRACTION:
        # No usable cadence (a run recorded without running dynamics): the effort cannot be
        # segmented, so fall back to a plain robust smoothing of the measured speed rather than
        # collapsing the whole run to a single flat level.
        log.warning("cadence largely absent; falling back to unsegmented robust speed")
        return robust_speed(guarded, dt_s)
    labels = phase_labels(cadence, dt_s, params=params.phase)
    return _reconstruct(
        guarded,
        phase_segments(labels),
        dt_s,
        prefilter_s=params.prefilter_s,
        max_drift_mps=params.max_drift_mps,
    )


def _reconstruct(
    speed: FloatArray,
    segments: list[PhaseSegment],
    dt_s: float,
    *,
    prefilter_s: float,
    max_drift_mps: float,
) -> FloatArray:
    """Median-anchored degree-1 fit per steady segment; linear ramp across transitions."""
    n = len(speed)
    base = median_clean(speed, dt_s, prefilter_s)  # spike-robust level for the fits
    out = np.full(n, np.nan, dtype=np.float64)

    # Steady segments: level = median, with a drift-capped degree-1 slope.
    for start, end, phase in segments:
        if phase != STEADY:
            continue
        block = base[start:end]
        level = float(np.median(block))
        if end - start >= _MIN_FIT_POINTS:
            x = np.arange(end - start, dtype=np.float64)
            slope = float(np.polyfit(x, block, 1)[0])
            span = float(end - start - 1)
            if abs(slope) * span > max_drift_mps:
                slope = np.sign(slope) * max_drift_mps / span
            out[start:end] = level + slope * (x - x.mean())
        else:
            out[start:end] = level

    # Transitions: ramp from the previous steady end value to the next steady start value.
    for start, end, phase in segments:
        if phase == STEADY:
            continue
        left = out[start - 1] if start > 0 and np.isfinite(out[start - 1]) else np.nan
        right = out[end] if end < n and np.isfinite(out[end]) else np.nan
        if np.isfinite(left) and np.isfinite(right):
            # Both plateau levels known: a crisp instant ramp between them (no GNSS lag).
            out[start:end] = np.linspace(left, right, end - start + 2)[1:-1]
        else:
            # A transition at the start or end of the run has no plateau to ramp to on one side, so
            # follow the measured speed instead of holding flat — flattening would discard a real
            # fast start or sprint finish and skew the lap calibration that scales this segment.
            out[start:end] = base[start:end]

    remaining = ~np.isfinite(out)
    out[remaining] = base[remaining]
    return np.clip(out, 0.0, None)


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


def ground_truth_speed(
    activity: Activity,
    *,
    params: SegmentParams = _DEFAULT_PARAMS,
    min_segment_m: float = 1000.0,
) -> FloatArray:
    """Per-second ground-truth speed (m/s) for the activity's records."""
    records = activity.records
    dt = median_dt_s(records)
    shaped = segment_pace(records, dt, params=params)
    if not np.any(shaped > 0.0):
        log.warning(
            "%s has no positive speed; ground truth is all zero", activity.meta.source_path.name
        )
    return calibrate_ground_truth(
        shaped, dt, lap_segments(records, activity.laps), min_segment_m=min_segment_m
    )
