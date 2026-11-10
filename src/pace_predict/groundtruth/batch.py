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
  short collapse such as a GNSS dropout at an interval start is ignored instead of followed. Where
  cadence shows the runner stopped, GNSS keeps full weight so a real stop is not mistaken for one.
- **Trusted laps are exact constraints.** Each >= 1 km lap integrates (trapezoid rule) to its
  recorded distance.

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
from scipy.signal import lfilter, lfilter_zi, savgol_filter

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
_LAG_STEP_SAMPLES = 0.25  # grid step of the lag search


@dataclass(frozen=True)
class SmootherParams:
    """Tuning for :func:`smooth_speed`; defaults were chosen on real interval and steady runs."""

    lag_s: float | None = None  # GNSS lag time constant in [0, max_lag_s]; None estimates it
    max_lag_s: float = 15.0  # upper bound for the lag (search range of the estimate)
    smoothing: float = 250.0  # strength of the L1 third-difference penalty on speed
    order: int = 3  # difference order on speed (3 = piecewise-quadratic speed)
    transition_pad_s: int = 3  # widen each cadence transition by this many seconds per side
    transition_floor: float = 0.01  # penalty multiplier inside transitions (1.0 = no gating)
    outlier_scale_mps: float = 0.25  # Cauchy scale; residuals well beyond it lose influence
    iterations: int = 25  # reweighting rounds
    eps: float = 1e-3  # L1 smoothing constant for the reweighting
    min_lap_m: float = 1000.0  # laps at least this long are exact distance constraints
    dropout_gap_s: float = 8.0  # longest GNSS dropout interpolated before fitting
    stop_cadence_spm: float = 100.0  # below this the runner is stopped/walking: GNSS stays trusted
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


def _first_order_lag(x: FloatArray, lag_samples: float) -> FloatArray:
    """Backward-Euler first-order lag of ``x`` (the model the smoother inverts)."""
    if lag_samples <= 0.0:
        return x
    b, a = [1.0 / (1.0 + lag_samples)], [1.0, -lag_samples / (1.0 + lag_samples)]
    lagged: FloatArray = lfilter(b, a, x, zi=lfilter_zi(b, a) * x[0])[0]
    return lagged


def estimate_gnss_lag_s(
    speed_mps: npt.ArrayLike, cadence_spm: npt.ArrayLike, dt_s: float, *, max_lag_s: float = 15.0
) -> float:
    """GNSS lag time constant (s) by matched filtering of cadence and speed slopes.

    Cadence reacts to a pace change instantly; ``enhanced_speed`` follows with a lag. The estimate
    is the first-order lag (same model as the smoother) that, applied to the cadence slope, best
    correlates with the speed slope. On synthetic runs it tracks the true lag over 0-12 s with a
    positive offset of about 1-2 s.
    """
    speed = np.asarray(speed_mps, dtype=np.float64)
    if len(speed) <= _SMOOTH_WINDOW or not np.isfinite(speed).any():
        return 0.0
    cad_slope, speed_slope = _smoothed_slope(cadence_spm), _smoothed_slope(speed)
    if cad_slope.std() == 0.0 or speed_slope.std() == 0.0:
        return 0.0

    best_lag, best_corr = 0.0, -np.inf
    for lag in np.arange(0.0, max_lag_s / dt_s + _LAG_STEP_SAMPLES, _LAG_STEP_SAMPLES):
        corr = float(np.corrcoef(_first_order_lag(cad_slope, lag), speed_slope)[0, 1])
        if corr > best_corr:
            best_lag, best_corr = float(lag), corr
    return min(best_lag * dt_s, max_lag_s)


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
    """Trapezoid-rule rows (as ``calibrate.integrate_speed``) for non-overlapping trusted laps.

    Laps are clipped to the run; empty laps and laps overlapping an earlier accepted one are
    dropped, so the constraint matrix always has full row rank.
    """
    clipped = [
        LapSegment(max(lap.start, 0), min(lap.end, n), lap.distance_m)
        for lap in laps
        if lap.distance_m > 0.0
    ]
    trusted: list[LapSegment] = []
    for lap in sorted(clipped):
        long_enough = lap.distance_m >= min_lap_m and lap.end - lap.start >= _MIN_POINTS
        if long_enough and (not trusted or lap.start >= trusted[-1].end):
            trusted.append(lap)
    if not trusted:  # fall back to the run total so the overall scale is still anchored
        total = sum(lap.distance_m for lap in clipped)
        trusted = [LapSegment(0, n, total)] if total > 0.0 and n >= _MIN_POINTS else []
    rows = sp.lil_matrix((len(trusted), n))
    for k, lap in enumerate(trusted):
        rows[k, lap.start : lap.end] = dt_s
        rows[k, lap.start] = rows[k, lap.end - 1] = dt_s / 2.0
    return rows.tocsr(), np.array([lap.distance_m for lap in trusted], dtype=np.float64)


def smooth_speed(
    records: pd.DataFrame,
    laps: list[LapSegment],
    dt_s: float,
    *,
    params: SmootherParams = _DEFAULT_PARAMS,
) -> FloatArray:
    """Per-second speed (m/s) from the lag-aware, cadence-gated, outlier-robust batch fit."""
    raw = records[S.SPEED_MPS].to_numpy(dtype=np.float64)
    n = len(raw)
    if n < params.order + _MIN_POINTS:
        return robust_speed(raw, dt_s)
    if params.lag_s is not None and not 0.0 <= params.lag_s <= params.max_lag_s:
        raise ValueError(f"lag_s must lie in [0, {params.max_lag_s}], got {params.lag_s}")
    speed = guard_dropouts(raw, dt_s, max_gap_s=params.dropout_gap_s)  # also fills NaN gaps
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
    stopped = np.nan_to_num(cadence, nan=np.inf) < params.stop_cadence_spm

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
        data_w[stopped] = 1.0  # a real stop is not a GNSS fault

    speed_out: FloatArray = np.clip(lag_m @ g, 0.0, None)
    if not np.isfinite(speed_out).all():
        raise ValueError("batch smoother produced non-finite speed (inconsistent lap constraints?)")
    return speed_out
