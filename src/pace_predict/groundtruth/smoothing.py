"""Offline speed smoothing for the ground truth.

- ``latlon_to_enu`` projects GPS to a local east/north metric plane (used by track detection).
- ``median_clean`` is the shared spike/dropout-robust median filter (window given in seconds).
- ``guard_dropouts`` interpolates over short GNSS dropouts (speed collapsing far below the local
  level under tree cover), while leaving a genuine sustained stop untouched.
- ``robust_speed`` cleans a recorded speed with a median filter (spike-robust and edge-preserving)
  plus a light Savitzky-Golay pass; it is applied *within* a cadence/GCT segment so transitions
  between efforts are never smoothed across.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from scipy.ndimage import median_filter
from scipy.signal import savgol_filter

type FloatArray = npt.NDArray[np.float64]

_EARTH_RADIUS_M = 6_371_000.0
_MIN_POINTS = 2  # need at least two samples to estimate a velocity
_POLYORDER = 2  # Savitzky-Golay polynomial order


def _as_array(values: npt.ArrayLike) -> FloatArray:
    return np.asarray(values, dtype=np.float64)


def _fill_nan(series: FloatArray) -> FloatArray:
    """Linearly interpolate NaNs by index; leave an all-NaN input untouched."""
    filled = series.astype(np.float64, copy=True)
    finite = np.isfinite(filled)
    if finite.all() or not finite.any():
        return filled
    index = np.arange(len(filled), dtype=np.float64)
    filled[~finite] = np.interp(index[~finite], index[finite], filled[finite])
    return filled


def _odd(value: float) -> int:
    """Nearest odd integer >= 1 (windows must be odd and positive)."""
    v = max(round(value), 1)
    return v if v % 2 == 1 else v + 1


def latlon_to_enu(lat_deg: npt.ArrayLike, lon_deg: npt.ArrayLike) -> tuple[FloatArray, FloatArray]:
    """Project latitude/longitude (degrees) to a local east/north metric plane (metres).

    An equirectangular projection about the track's mean position — accurate to well under a metre
    over the few-km extent of a single run.
    """
    lat = _fill_nan(_as_array(lat_deg))
    lon = _fill_nan(_as_array(lon_deg))
    lat0 = float(np.nanmean(lat))
    lon0 = float(np.nanmean(lon))
    east = np.radians(lon - lon0) * np.cos(np.radians(lat0)) * _EARTH_RADIUS_M
    north = np.radians(lat - lat0) * _EARTH_RADIUS_M
    return east, north


def median_clean(speed_mps: npt.ArrayLike, dt_s: float, window_s: float) -> FloatArray:
    """Median-filter a speed signal with the window given in seconds (spike/dropout-robust)."""
    speed = np.nan_to_num(_fill_nan(_as_array(speed_mps)), nan=0.0)
    if len(speed) < _MIN_POINTS:
        return np.clip(speed, 0.0, None)
    filtered: FloatArray = median_filter(speed, size=_odd(round(window_s / dt_s)))
    return np.clip(filtered, 0.0, None)


def guard_dropouts(
    speed_mps: npt.ArrayLike,
    dt_s: float,
    *,
    max_gap_s: float = 8.0,
    low_frac: float = 0.4,
    baseline_s: float = 31.0,
) -> FloatArray:
    """Interpolate over short GNSS dropouts — a speed collapse far below the local level.

    Under tree cover the recorded speed briefly falls to near zero even though the runner keeps
    going. Such a sample sits far below a robust ~``baseline_s`` median of its surroundings, so any
    run below ``low_frac`` of that baseline and no longer than ``max_gap_s`` is treated as a dropout
    and linearly interpolated across. A genuine sustained stop drives the baseline itself to zero
    (nothing is flagged) or lasts longer than ``max_gap_s``, so it is preserved.
    """
    speed = np.nan_to_num(_fill_nan(_as_array(speed_mps)), nan=0.0)
    n = len(speed)
    if n < _MIN_POINTS:
        return np.clip(speed, 0.0, None)

    # Flag short low-speed runs against a robust local baseline.
    baseline = median_filter(speed, size=_odd(round(baseline_s / dt_s)))
    low = speed < low_frac * baseline
    max_gap = max(1, round(max_gap_s / dt_s))

    # Blank the dropouts, then interpolate over them by index.
    out = speed.astype(np.float64, copy=True)
    i = 0
    while i < n:
        if not low[i]:
            i += 1
            continue
        j = i
        while j < n and low[j]:
            j += 1
        if (j - i) <= max_gap:
            out[i:j] = np.nan
        i = j
    return np.clip(_fill_nan(out), 0.0, None)


def robust_speed(
    speed_mps: npt.ArrayLike,
    dt_s: float,
    *,
    median_s: float = 9.0,
    smooth_s: float = 25.0,
) -> FloatArray:
    """Median-clean then lightly Savitzky-Golay-smooth a speed signal.

    The median filter rejects GNSS spikes that averaging would smear in; the Savitzky-Golay pass
    follows genuine variation (terrain over tens of seconds) without the jitter. Sized for use
    within a single segment.
    """
    speed = np.nan_to_num(_fill_nan(_as_array(speed_mps)), nan=0.0)
    if len(speed) < _MIN_POINTS:
        return np.clip(speed, 0.0, None)
    cleaned: FloatArray = median_filter(speed, size=_odd(round(median_s / dt_s)))
    if smooth_s > 0 and len(cleaned) >= _POLYORDER + _MIN_POINTS:
        window = min(
            _odd(round(smooth_s / dt_s)),
            _odd(len(cleaned)) if len(cleaned) % 2 else len(cleaned) - 1,
        )
        cleaned = savgol_filter(cleaned, max(window, _POLYORDER + 1), _POLYORDER)
    return np.clip(cleaned, 0.0, None)
