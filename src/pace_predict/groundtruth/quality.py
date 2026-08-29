"""Signal-derived data-quality helpers: track detection and cadence-based interval segmentation.

- ``track_concentration`` / ``is_track_like`` flag runs on a 400 m oval, where GNSS distance
  overshoots and per-second speed is unreliable — such runs are excluded from calibration.
- ``cadence_change_points`` finds interval boundaries from steps in cadence (the instant biomechanic
  response), independent of GNSS.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from scipy.ndimage import median_filter

from pace_predict.groundtruth.smoothing import latlon_to_enu

type FloatArray = npt.NDArray[np.float64]

_MIN_GAP = 2


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


def cadence_change_points(
    cadence_spm: npt.ArrayLike,
    dt_s: float,
    *,
    step_thr: float = 3.0,
    window_s: float = 8.0,
    min_gap_s: float = 15.0,
) -> list[int]:
    """Sample indices where cadence steps by more than ``step_thr`` spm (interval boundaries).

    Compares the mean cadence over ``window_s`` before and after each point; boundaries are kept at
    least ``min_gap_s`` apart. A larger window than the step itself is essential — a 2 s window is
    too noisy to detect a ~6 spm work/recovery step.
    """
    cadence = np.asarray(cadence_spm, dtype=np.float64)
    filled = np.nan_to_num(
        cadence, nan=float(np.nanmedian(cadence)) if np.isfinite(cadence).any() else 0.0
    )
    smoothed = median_filter(filled, size=_odd(5.0 / dt_s))
    window = max(_MIN_GAP, round(window_s / dt_s))
    min_gap = max(1, round(min_gap_s / dt_s))

    boundaries: list[int] = []
    last = -min_gap
    i = window
    while i < len(smoothed) - window:
        step = abs(float(smoothed[i : i + window].mean()) - float(smoothed[i - window : i].mean()))
        if step > step_thr and (i - last) >= min_gap:
            boundaries.append(i)
            last = i
            i += window
        else:
            i += 1
    return boundaries
