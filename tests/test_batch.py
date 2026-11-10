"""Tests for the lag-aware batch smoother on synthetic runs with known true speed."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pace_predict.groundtruth import (
    LapSegment,
    SmootherParams,
    estimate_gnss_lag_s,
    smooth_speed,
)
from pace_predict.io import schema as S

_TRUE_LAG_S = 7.0


def _interval_run(seed: int = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[LapSegment]]:
    """True speed, lagged noisy GNSS speed, instant integer cadence, and lap segments."""
    rng = np.random.default_rng(seed)
    plan = [(1000.0, 3.0)] + [(400.0, 4.25), (400.0, 3.2)] * 4 + [(1000.0, 2.95)]
    speed: list[float] = []
    laps: list[LapSegment] = []
    current = 2.8
    for distance, target in plan:
        start, covered = len(speed), 0.0
        tau = 3.0 if target > current else 4.0
        while covered < distance:
            current = target + (current - target) * np.exp(-1.0 / tau)
            speed.append(current)
            covered += current
        laps.append(LapSegment(start, len(speed), covered))
    truth = np.array(speed)
    lagged = np.empty_like(truth)
    lagged[0] = truth[0]
    for i in range(1, len(truth)):
        lagged[i] = (lagged[i - 1] * _TRUE_LAG_S + truth[i]) / (_TRUE_LAG_S + 1.0)
    gnss = np.clip(lagged + rng.normal(0.0, 0.1, len(truth)), 0.0, None)
    cadence = np.round(150.0 + 6.0 * truth + rng.normal(0.0, 0.5, len(truth)))
    return truth, gnss, cadence, laps


def _records(speed: np.ndarray, cadence: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({S.SPEED_MPS: speed, S.CADENCE_SPM: cadence})


def _rmse(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(np.mean((a - b) ** 2)))


def test_estimate_gnss_lag_recovers_time_constant() -> None:
    _truth, gnss, cadence, _laps = _interval_run()
    assert estimate_gnss_lag_s(gnss, cadence, 1.0) == pytest.approx(_TRUE_LAG_S, abs=2.0)


def test_estimate_gnss_lag_is_zero_for_short_input() -> None:
    assert estimate_gnss_lag_s(np.ones(5), np.ones(5), 1.0) == 0.0


def test_smooth_speed_beats_raw_gnss_and_tracks_truth() -> None:
    truth, gnss, cadence, laps = _interval_run()
    out = smooth_speed(_records(gnss, cadence), laps, 1.0)
    assert _rmse(out, truth) < 0.5 * _rmse(gnss, truth)
    assert _rmse(out, truth) < 0.12


def test_smooth_speed_holds_trusted_lap_distances() -> None:
    _truth, gnss, cadence, laps = _interval_run()
    out = smooth_speed(_records(gnss, cadence), laps, 1.0)
    for lap in laps:
        if lap.distance_m >= 1000.0:
            assert out[lap.start : lap.end].sum() == pytest.approx(lap.distance_m, rel=1e-6)


def test_smooth_speed_removes_gnss_lag_at_interval_start() -> None:
    truth, gnss, cadence, laps = _interval_run()
    out = smooth_speed(_records(gnss, cadence), laps, 1.0)
    rep = laps[1]  # first fast rep
    window = slice(rep.start, rep.start + 15)  # first 15 s, where GNSS still lags
    assert np.mean(np.abs(out[window] - truth[window])) < 0.5 * np.mean(
        np.abs(gnss[window] - truth[window])
    )


def test_smooth_speed_ignores_short_gnss_collapse() -> None:
    truth, gnss, cadence, laps = _interval_run()
    collapsed = gnss.copy()
    mid = laps[0].start + 500  # inside the steady warm-up
    collapsed[mid : mid + 12] = 1.0  # 12 s collapse, longer than the dropout guard bridges
    out = smooth_speed(_records(collapsed, cadence), laps, 1.0)
    assert np.max(np.abs(out[mid : mid + 12] - truth[mid : mid + 12])) < 0.4


def test_smooth_speed_without_trusted_laps_anchors_total_distance() -> None:
    _truth, gnss, cadence, laps = _interval_run()
    params = SmootherParams(min_lap_m=10_000.0)  # no lap is long enough to be trusted
    out = smooth_speed(_records(gnss, cadence), laps, 1.0, params=params)
    assert out.sum() == pytest.approx(sum(lap.distance_m for lap in laps), rel=1e-6)


def test_smooth_speed_falls_back_without_cadence() -> None:
    speed = np.concatenate([np.full(60, 3.0), np.full(60, 5.0), np.full(60, 3.0)])
    out = smooth_speed(_records(speed, np.full(180, np.nan)), [LapSegment(0, 180, 660.0)], 1.0)
    assert out[90] > out[30] + 1.0  # the fast middle survives instead of being flattened


def test_smooth_speed_respects_fixed_lag_parameter() -> None:
    truth, gnss, cadence, laps = _interval_run()
    no_lag = smooth_speed(_records(gnss, cadence), laps, 1.0, params=SmootherParams(lag_s=0.0))
    matched = smooth_speed(
        _records(gnss, cadence), laps, 1.0, params=SmootherParams(lag_s=_TRUE_LAG_S)
    )
    assert _rmse(matched, truth) < _rmse(no_lag, truth)
