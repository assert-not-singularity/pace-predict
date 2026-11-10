"""Tests for the lag-aware batch smoother on synthetic runs with known true speed."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pace_predict.groundtruth import (
    LapSegment,
    SmootherParams,
    estimate_gnss_lag_s,
    integrate_speed,
    smooth_speed,
)
from pace_predict.io import schema as S

_TRUE_LAG_S = 7.0


def _interval_run(
    seed: int = 7, lag_s: float = _TRUE_LAG_S
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[LapSegment]]:
    """True speed, lagged noisy GNSS speed, instant integer cadence, and lap segments.

    The GNSS lag is generated with the same backward-Euler first-order model the smoother
    inverts, so these tests check self-consistency of the method, not that the model matches a
    real watch.
    """
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
        lagged[i] = (lagged[i - 1] * lag_s + truth[i]) / (lag_s + 1.0)
    gnss = np.clip(lagged + rng.normal(0.0, 0.1, len(truth)), 0.0, None)
    cadence = np.round(150.0 + 6.0 * truth + rng.normal(0.0, 0.5, len(truth)))
    return truth, gnss, cadence, laps


def _records(speed: np.ndarray, cadence: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({S.SPEED_MPS: speed, S.CADENCE_SPM: cadence})


def _rmse(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(np.mean((a - b) ** 2)))


@pytest.mark.parametrize("lag_s", [0.0, 4.0, 7.0, 12.0])
def test_estimate_gnss_lag_tracks_true_lag(lag_s: float) -> None:
    _truth, gnss, cadence, _laps = _interval_run(lag_s=lag_s)
    estimate = estimate_gnss_lag_s(gnss, cadence, 1.0)
    assert lag_s - 0.5 <= estimate <= lag_s + 2.5  # known positive offset of ~1-2 s


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
            covered = integrate_speed(out[lap.start : lap.end], 1.0)[-1]
            assert covered == pytest.approx(lap.distance_m, rel=1e-6)


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
    mid = laps[0].start + 150  # inside the steady 1 km warm-up
    collapsed[mid : mid + 12] = 1.0  # 12 s collapse, longer than the dropout guard bridges
    out = smooth_speed(_records(collapsed, cadence), laps, 1.0)
    assert np.max(np.abs(out[mid : mid + 12] - truth[mid : mid + 12])) < 0.4


def test_smooth_speed_without_trusted_laps_anchors_total_distance() -> None:
    _truth, gnss, cadence, laps = _interval_run()
    params = SmootherParams(min_lap_m=10_000.0)  # no lap is long enough to be trusted
    out = smooth_speed(_records(gnss, cadence), laps, 1.0, params=params)
    total = sum(lap.distance_m for lap in laps)
    assert integrate_speed(out, 1.0)[-1] == pytest.approx(total, rel=1e-6)


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


def test_smooth_speed_keeps_a_real_stop() -> None:
    truth, gnss, cadence, laps = _interval_run(lag_s=0.0)
    stop = slice(laps[0].start + 150, laps[0].start + 210)  # 60 s standing inside the warm-up
    truth, gnss, cadence = truth.copy(), gnss.copy(), cadence.copy()
    truth[stop], gnss[stop], cadence[stop] = 0.0, 0.0, 0.0
    lap = LapSegment(laps[0].start, laps[0].end, float(truth[laps[0].start : laps[0].end].sum()))
    out = smooth_speed(_records(gnss, cadence), [lap, *laps[1:]], 1.0)
    warmup = slice(lap.start, lap.end)
    assert np.max(out[warmup]) < np.max(truth[warmup]) + 0.3  # no invented speed spikes
    inner = slice(stop.start + 5, stop.stop - 5)  # the instant synthetic 3->0 step is eased ~5 s
    assert np.max(out[inner]) < 0.1


def test_smooth_speed_bridges_missing_gnss_samples() -> None:
    truth, gnss, cadence, laps = _interval_run()
    gap = slice(laps[0].start + 100, laps[0].start + 220)  # 120 s of NaN speed in the warm-up
    gnss = gnss.copy()
    gnss[gap] = np.nan
    out = smooth_speed(_records(gnss, cadence), laps, 1.0)
    warmup = slice(laps[0].start, laps[0].end)
    assert _rmse(out[warmup], truth[warmup]) < 0.3
    assert np.max(out[warmup]) < 4.0


def test_smooth_speed_tolerates_duplicate_and_overlapping_laps() -> None:
    _truth, gnss, cadence, laps = _interval_run()
    whole = LapSegment(0, len(gnss), sum(lap.distance_m for lap in laps))
    messy = [laps[0], laps[0], whole, LapSegment(len(gnss), len(gnss) + 10, 1500.0), *laps[1:]]
    out = smooth_speed(_records(gnss, cadence), messy, 1.0)
    assert np.isfinite(out).all()


def test_smooth_speed_rejects_out_of_range_lag() -> None:
    _truth, gnss, cadence, laps = _interval_run()
    with pytest.raises(ValueError, match="lag_s"):
        smooth_speed(_records(gnss, cadence), laps, 1.0, params=SmootherParams(lag_s=-1.0))
