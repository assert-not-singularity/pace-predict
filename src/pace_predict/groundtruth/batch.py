"""Lag-aware, outlier-robust batch smoother for the per-second ground-truth speed.

One global fit over the whole run instead of per-segment shapes:

- **GNSS lag is modelled, not smoothed over.** The watch's ``enhanced_speed`` behaves like the true
  speed passed through a first-order lag. The fit works on that lagged signal ``g`` and recovers
  the speed as ``v = M @ g`` (``M`` is the bidiagonal inverse of the lag), so interval starts and
  ends land where they happened rather than where GNSS caught up.
- **Natural shape.** An L1 penalty on the third difference of ``v`` (solved by iterative
  reweighting) gives smooth, piecewise-quadratic speed with automatically placed breakpoints.
- **Cadence sets timing, never values.** Inside cadence-detected transitions the penalty is almost
  switched off, so pace may change quickly there and stays near-constant elsewhere.
- **GNSS faults lose influence.** Data weights follow a Cauchy (Student-t-like) reweighting, so a
  short collapse such as a GNSS dropout at an interval start is ignored instead of followed.
- **Trusted laps are exact constraints.** Each >= 1 km lap integrates to its recorded distance.

Every step is a sparse linear (KKT) solve, so a run takes about a second.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
import pandas as pd
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.ndimage import binary_dilation, median_filter
from scipy.signal import savgol_filter

from pace_predict.groundtruth.calibrate import LapSegment
from pace_predict.groundtruth.quality import STEADY, PhaseParams, phase_labels
from pace_predict.groundtruth.smoothing import guard_dropouts, robust_speed
from pace_predict.io import schema as S

type FloatArray = npt.NDArray[np.float64]

log = logging.getLogger(__name__)

_MIN_POINTS = 2
_MIN_CADENCE_FRACTION = 0.5  # below this share of finite cadence the run cannot be gated
_SMOOTH_WINDOW = 9  # samples; Savitzky-Golay window used before differentiating for lag estimation
_MEDIAN_SIZE = 5


@dataclass(frozen=True)
class SmootherParams:
    """Tuning for :func:`smooth_speed`; defaults were chosen on real interval and steady runs."""

    lag_s: float | None = None  # GNSS lag time constant; None estimates it per run
    max_lag_s: float = 15.0  # search range (and clamp) for the lag estimate
    smoothing: float = 250.0  # strength of the L1 third-difference penalty on speed
    order: int = 3  # difference order on speed (3 = piecewise-quadratic speed)
    transition_pad_s: int = 3  # widen each cadence transition by this many seconds per side
    transition_floor: float = 0.01  # penalty multiplier inside transitions (1.0 = no gating)
    outlier_scale_mps: float = 0.25  # Cauchy scale; residuals well beyond it lose influence
    iterations: int = 25  # reweighting rounds
    eps: float = 1e-3  # L1 smoothing constant for the reweighting
    min_lap_m: float = 1000.0  # laps at least this long are exact distance constraints
    dropout_gap_s: float = 8.0  # longest GNSS dropout interpolated before fitting
    phase: PhaseParams = field(default_factory=PhaseParams)


_DEFAULT_PARAMS = SmootherParams()


def _smoothed_slope(values: npt.ArrayLike) -> FloatArray:
    array = np.asarray(values, dtype=np.float64)
    array = np.nan_to_num(
        array, nan=float(np.nanmedian(array)) if np.isfinite(array).any() else 0.0
    )
    smooth = savgol_filter(median_filter(array, size=_MEDIAN_SIZE), _SMOOTH_WINDOW, 2)
    slope: FloatArray = np.gradient(smooth)
    return slope


def estimate_gnss_lag_s(
    speed_mps: npt.ArrayLike, cadence_spm: npt.ArrayLike, dt_s: float, *, max_lag_s: float = 15.0
) -> float:
    """GNSS lag time constant (s): centroid of the cadence-to-speed slope cross-correlation.

    Cadence reacts to a pace change instantly; ``enhanced_speed`` follows with a lag. The centroid
    of the positive cross-correlation of their slopes estimates a first-order lag's time constant
    (the peak alone underestimates it, because a first-order response peaks immediately).
    """
    speed = np.asarray(speed_mps, dtype=np.float64)
    max_lag = max(1, round(max_lag_s / dt_s))
    if len(speed) <= max(_SMOOTH_WINDOW, max_lag + _MIN_POINTS):
        return 0.0

    # Correlate standardized slopes over non-negative delays
    cad_slope, speed_slope = _smoothed_slope(cadence_spm), _smoothed_slope(speed)
    if cad_slope.std() == 0.0 or speed_slope.std() == 0.0:
        return 0.0
    cad_slope = (cad_slope - cad_slope.mean()) / cad_slope.std()
    speed_slope = (speed_slope - speed_slope.mean()) / speed_slope.std()
    n = len(speed)
    corr = np.array(
        [np.mean(cad_slope[: n - lag] * speed_slope[lag:]) for lag in range(max_lag + 1)]
    )

    # Centroid of the positive part
    positive = np.clip(corr, 0.0, None)
    if positive.sum() == 0.0:
        return 0.0
    centroid = float((positive * np.arange(len(corr))).sum() / positive.sum())
    return min(centroid * dt_s, max_lag_s)


def _difference(n: int, order: int) -> sp.csr_matrix:
    op = sp.identity(n, format="csr")
    for _ in range(order):
        op = (op[1:] - op[:-1]).tocsr()
    return op


def _inverse_lag(n: int, lag_samples: float) -> sp.csr_matrix:
    """``M`` with ``v = M @ g`` for ``g`` = backward-Euler first-order lag of ``v``."""
    m = (sp.identity(n) * (1.0 + lag_samples) - sp.eye(n, k=-1) * lag_samples).tolil()
    m[0, 0] = 1.0
    return m.tocsr()


def _distance_constraints(
    laps: list[LapSegment], n: int, dt_s: float, min_lap_m: float
) -> tuple[sp.csr_matrix, FloatArray]:
    trusted = [lap for lap in laps if lap.distance_m >= min_lap_m and lap.end > lap.start]
    if not trusted:  # fall back to the run total so the overall scale is still anchored
        total = sum(lap.distance_m for lap in laps)
        trusted = [LapSegment(0, n, total)] if total > 0.0 else []
    rows = sp.lil_matrix((len(trusted), n))
    for k, lap in enumerate(trusted):
        rows[k, lap.start : lap.end] = dt_s
    return rows.tocsr(), np.array([lap.distance_m for lap in trusted], dtype=np.float64)


def smooth_speed(
    records: pd.DataFrame,
    laps: list[LapSegment],
    dt_s: float,
    *,
    params: SmootherParams = _DEFAULT_PARAMS,
) -> FloatArray:
    """Per-second speed (m/s) from the lag-aware, cadence-gated, outlier-robust batch fit."""
    speed = np.nan_to_num(records[S.SPEED_MPS].to_numpy(dtype=np.float64), nan=0.0)
    n = len(speed)
    if n < params.order + _MIN_POINTS:
        return robust_speed(speed, dt_s)
    speed = guard_dropouts(speed, dt_s, max_gap_s=params.dropout_gap_s)
    cadence = records[S.CADENCE_SPM].to_numpy(dtype=np.float64)
    if np.isfinite(cadence).mean() < _MIN_CADENCE_FRACTION:
        log.warning("cadence largely absent; falling back to unsegmented robust speed")
        return robust_speed(speed, dt_s)

    # Operators: lag inverse, penalty, cadence gate, lap constraints
    lag_s = params.lag_s
    if lag_s is None:
        lag_s = estimate_gnss_lag_s(speed, cadence, dt_s, max_lag_s=params.max_lag_s)
    lag_m = _inverse_lag(n, lag_s / dt_s)
    penalty = _difference(n, params.order) @ lag_m
    in_transition = phase_labels(cadence, dt_s, params=params.phase) != STEADY
    pad = round(params.transition_pad_s / dt_s)
    if pad > 0:
        in_transition = binary_dilation(in_transition, iterations=pad)
    gate = np.where(in_transition, params.transition_floor, 1.0)[: n - params.order]
    a_lap, b_lap = _distance_constraints(laps, n, dt_s, params.min_lap_m)
    a = a_lap @ lag_m

    # Iteratively reweighted least squares: L1 penalty + Cauchy data weights
    g = speed.copy()
    data_w = np.ones(n)
    for _ in range(params.iterations):
        pen_w = params.smoothing * gate / (np.abs(penalty @ g) + params.eps)
        q = sp.diags(data_w) + penalty.T @ sp.diags(pen_w) @ penalty
        if a.shape[0]:
            kkt = sp.bmat([[2.0 * q, a.T], [a, None]], format="csc")
            g = spla.spsolve(kkt, np.concatenate([2.0 * data_w * speed, b_lap]))[:n]
        else:
            g = spla.spsolve((2.0 * q).tocsc(), 2.0 * data_w * speed)
        data_w = 1.0 / (1.0 + ((g - speed) / params.outlier_scale_mps) ** 2)

    speed_out: FloatArray = np.clip(lag_m @ g, 0.0, None)
    return speed_out
