"""Signal-derived data-quality helpers: track detection and biomechanic phase segmentation.

- ``track_concentration`` / ``is_track_like`` flag runs on a 400 m oval, where GNSS distance
  overshoots and per-second speed is unreliable — such runs are excluded from calibration/training.
- ``phase_labels`` classifies every sample as accelerating / steady / decelerating from the cadence
  trend (the instant biomechanic response), independent of GNSS. ``phase_segments`` turns those
  labels into contiguous typed segments. Between a runner's effort changes cadence is steady, so the
  pace is steady too; the accel/decel runs are the interval transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
import numpy.typing as npt
from scipy.ndimage import median_filter
from scipy.signal import savgol_filter

from pace_predict.groundtruth.smoothing import latlon_to_enu

type FloatArray = npt.NDArray[np.float64]
type IntArray = npt.NDArray[np.int_]

_MIN_SMOOTH_POINTS = 5  # below this a run is too short to smooth/differentiate meaningfully
_LOOKBACK_S = 6.0  # seconds of plateau context averaged on each side of a transition

ACCEL = 1
STEADY = 0
DECEL = -1


class PhaseSegment(NamedTuple):
    """A contiguous run of one effort phase over ``[start, end)`` record indices."""

    start: int
    end: int
    phase: int  # ACCEL (+1), STEADY (0) or DECEL (-1)


@dataclass(frozen=True)
class PhaseParams:
    """Tuning thresholds for :func:`phase_labels` (validated defaults; per-runner tunable)."""

    thr_spm_s: float = 0.45  # cadence-slope threshold, steps/min per second
    smooth_s: float = 9.0  # cadence smoothing window
    min_phase_s: float = 3.0  # shortest accel/decel run kept
    min_delta_spm: float = 2.0  # smallest plateau-level change kept


_DEFAULT_PHASE_PARAMS = PhaseParams()  # shared immutable default (see phase_labels)


def _odd(value: float) -> int:
    v = max(round(value), 1)
    return v if v % 2 == 1 else v + 1


def track_concentration(
    lat_deg: npt.ArrayLike,
    lon_deg: npt.ArrayLike,
    *,
    radius_m: float = 150.0,
    cell_m: float = 50.0,
) -> float:
    """Fraction of GPS points within ``radius_m`` of the densest cell.

    On a 400 m track most of the run (including reps) sits inside one small oval, so this is high
    (~0.7); a point-to-point road run scores near zero.
    """
    east, north = latlon_to_enu(lat_deg, lon_deg)
    finite = np.isfinite(east) & np.isfinite(north)
    if not finite.any():
        return 0.0
    east, north = east[finite], north[finite]
    cells = np.stack([np.round(east / cell_m), np.round(north / cell_m)], axis=1)
    unique, counts = np.unique(cells, axis=0, return_counts=True)
    densest = unique[np.argmax(counts)]
    center = densest * cell_m
    return float(np.mean(np.hypot(east - center[0], north - center[1]) < radius_m))


def is_track_like(
    lat_deg: npt.ArrayLike,
    lon_deg: npt.ArrayLike,
    *,
    threshold: float = 0.5,
    radius_m: float = 150.0,
) -> bool:
    """True if the GPS track is concentrated like a running track (exclude from calibration)."""
    return track_concentration(lat_deg, lon_deg, radius_m=radius_m) >= threshold


def _smooth_cadence(cadence_spm: npt.ArrayLike, dt_s: float, smooth_s: float) -> FloatArray:
    """Median- then Savitzky-Golay-smooth cadence so integer steps do not spike the slope."""
    cad = np.asarray(cadence_spm, dtype=np.float64)
    fill = float(np.nanmedian(cad)) if np.isfinite(cad).any() else 0.0
    cleaned: FloatArray = median_filter(np.nan_to_num(cad, nan=fill), size=5)
    if len(cleaned) < _MIN_SMOOTH_POINTS:
        return cleaned
    window = min(_odd(smooth_s / dt_s), len(cleaned) if len(cleaned) % 2 else len(cleaned) - 1)
    smoothed: FloatArray = savgol_filter(cleaned, max(window, 3), 2)
    return smoothed


def _suppress_short(labels: IntArray, dt_s: float, min_phase_s: float) -> IntArray:
    """Relabel accel/decel runs shorter than ``min_phase_s`` to STEADY (drops 1-3 sample flips)."""
    out = labels.copy()
    min_len = max(1, round(min_phase_s / dt_s))
    i, n = 0, len(out)
    while i < n:
        j = i
        while j < n and out[j] == out[i]:
            j += 1
        if out[i] != STEADY and (j - i) < min_len:
            out[i:j] = STEADY
        i = j
    return out


def _suppress_shallow(
    labels: IntArray, smoothed_cadence: FloatArray, dt_s: float, min_delta_spm: float
) -> IntArray:
    """Drop a transition whose surrounding plateau LEVELS differ by < ``min_delta_spm``.

    Comparing the median cadence in a window before and after the run (rather than the run's own
    endpoints) keeps a gradual real transition that only briefly clears the gradient bar, while
    still discarding shallow integer-quantization wobble inside a plateau.
    """
    out = labels.copy()
    look = max(1, round(_LOOKBACK_S / dt_s))
    i, n = 0, len(out)
    while i < n:
        j = i
        while j < n and out[j] == out[i]:
            j += 1
        if out[i] != STEADY:
            before = (
                float(np.median(smoothed_cadence[max(0, i - look) : i]))
                if i > 0
                else float(smoothed_cadence[i])
            )
            after = (
                float(np.median(smoothed_cadence[j : min(n, j + look)]))
                if j < n
                else float(smoothed_cadence[j - 1])
            )
            if abs(after - before) < min_delta_spm:
                out[i:j] = STEADY
        i = j
    return out


def phase_labels(
    cadence_spm: npt.ArrayLike, dt_s: float, *, params: PhaseParams = _DEFAULT_PHASE_PARAMS
) -> IntArray:
    """Per-sample effort phase (ACCEL / STEADY / DECEL) from the cadence trend.

    Cadence responds to a pace change instantly and independently of GNSS. Its smoothed time
    derivative is thresholded at ``params.thr_spm_s`` (steps/min per second) into accelerating,
    steady, and decelerating, then cleaned so integer-quantization noise inside a plateau does not
    read as a transition: runs shorter than ``params.min_phase_s`` or spanning a plateau-level
    change below ``params.min_delta_spm`` are relabelled STEADY.

    (Ground-contact time corroborates cadence but folding it in over-suppressed real ramps in
    testing, so the classifier is cadence-only.)
    """
    cad_s = _smooth_cadence(cadence_spm, dt_s, params.smooth_s)
    grad = np.gradient(cad_s, dt_s)
    raw: IntArray = np.where(
        grad > params.thr_spm_s, ACCEL, np.where(grad < -params.thr_spm_s, DECEL, STEADY)
    )
    labels = _suppress_short(raw, dt_s, params.min_phase_s)
    return _suppress_shallow(labels, cad_s, dt_s, params.min_delta_spm)


def phase_segments(labels: npt.ArrayLike) -> list[PhaseSegment]:
    """Group per-sample phase labels into contiguous ``[start, end)`` typed segments."""
    array = np.asarray(labels)
    segments: list[PhaseSegment] = []
    i, n = 0, len(array)
    while i < n:
        j = i
        while j < n and array[j] == array[i]:
            j += 1
        segments.append(PhaseSegment(i, j, int(array[i])))
        i = j
    return segments
