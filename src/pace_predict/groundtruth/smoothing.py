"""Offline per-second speed estimation from an activity's distance and GPS track.

Two independent estimators, both non-causal (they use the whole activity, which is fine offline
and is what makes them clean where Garmin's live pace lags):

- ``savgol_speed`` differentiates a Savitzky-Golay-smoothed cumulative-distance signal. Simple and
  robust; leans on Garmin's already-fused distance.
- ``kalman_rts_speed`` runs a constant-acceleration Kalman filter and RTS smoother over the GPS
  track projected to a local metric plane. Independent of Garmin's distance fusion, so it is a
  useful cross-check.

Both return speed in m/s. Calibrate the result to known distances with ``calibrate``.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from filterpy.common import Q_discrete_white_noise
from filterpy.kalman import KalmanFilter
from scipy.linalg import block_diag
from scipy.ndimage import median_filter
from scipy.signal import savgol_filter

type FloatArray = npt.NDArray[np.float64]

_EARTH_RADIUS_M = 6_371_000.0
_MIN_POINTS = 2  # need at least two samples to estimate a velocity
_POLYORDER = 2  # Savitzky-Golay polynomial order used across estimators


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


def savgol_speed(
    distance_m: npt.ArrayLike,
    dt_s: float,
    *,
    window_s: float = 15.0,
    polyorder: int = 2,
) -> FloatArray:
    """Speed (m/s) as the Savitzky-Golay first derivative of cumulative distance."""
    distance = _fill_nan(_as_array(distance_m))
    n = len(distance)
    if n < polyorder + _MIN_POINTS:
        fallback: FloatArray = np.clip(np.gradient(distance, dt_s), 0.0, None)
        return fallback

    window = _odd(max(polyorder + _MIN_POINTS, round(window_s / dt_s)))
    window = min(window, _odd(n) if n % 2 == 1 else n - 1)
    speed: FloatArray = np.clip(
        savgol_filter(distance, window, polyorder, deriv=1, delta=dt_s), 0.0, None
    )
    return speed


def robust_speed(
    speed_mps: npt.ArrayLike,
    dt_s: float,
    *,
    median_s: float = 9.0,
    smooth_s: float = 5.0,
) -> FloatArray:
    """Clean a recorded speed for use as the ground-truth base signal.

    A median filter is spike-robust (it rejects GNSS peaks that averaging would smear in) and
    edge-preserving (interval steps stay sharp), followed by a light Savitzky-Golay pass. Applied
    to Garmin's fused ``enhanced_speed``, which is the best per-second speed in the file.
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


def kalman_rts_speed(
    east_m: npt.ArrayLike,
    north_m: npt.ArrayLike,
    dt_s: float,
    *,
    measurement_noise_m: float = 3.0,
    accel_noise: float = 0.5,
) -> FloatArray:
    """Speed (m/s) from a constant-acceleration Kalman filter + RTS smoother over the GPS track.

    State is ``[x, vx, ax, y, vy, ay]``; measurements are position only. ``measurement_noise_m`` is
    the GPS position standard deviation; ``accel_noise`` is the process (acceleration) noise.
    """
    east = _fill_nan(_as_array(east_m))
    north = _fill_nan(_as_array(north_m))
    n = len(east)
    if n < _MIN_POINTS:
        return np.zeros(n, dtype=np.float64)

    transition_block = np.array([[1.0, dt_s, 0.5 * dt_s**2], [0.0, 1.0, dt_s], [0.0, 0.0, 1.0]])

    kf = KalmanFilter(dim_x=6, dim_z=2)
    kf.F = block_diag(transition_block, transition_block)
    kf.H = np.array([[1.0, 0, 0, 0, 0, 0], [0, 0, 0, 1.0, 0, 0]])
    kf.R = np.eye(2) * measurement_noise_m**2
    kf.Q = Q_discrete_white_noise(dim=3, dt=dt_s, var=accel_noise**2, block_size=2)
    kf.x = np.array([east[0], 0.0, 0.0, north[0], 0.0, 0.0])
    kf.P = np.diag([measurement_noise_m**2, 100.0, 100.0, measurement_noise_m**2, 100.0, 100.0])

    measurements = np.column_stack([east, north])
    means, covariances, _, _ = kf.batch_filter(measurements)
    smoothed, _, _, _ = kf.rts_smoother(means, covariances)

    velocity_east = smoothed[:, 1]
    velocity_north = smoothed[:, 4]
    speed: FloatArray = np.hypot(velocity_east, velocity_north)
    return speed
