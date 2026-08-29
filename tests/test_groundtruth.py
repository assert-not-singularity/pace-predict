"""Unit tests for the ground-truth speed estimators and distance calibration."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pace_predict.groundtruth import (
    cadence_change_points,
    calibrate_ground_truth,
    calibrate_per_lap,
    calibrate_to_distance,
    ground_truth_speed,
    integrate_speed,
    kalman_rts_speed,
    latlon_to_enu,
    robust_speed,
    savgol_speed,
    track_concentration,
)
from pace_predict.io import schema as S
from pace_predict.io.fit import Activity, ActivityMeta, build_laps_frame


def test_savgol_speed_recovers_constant_speed() -> None:
    dt, speed = 1.0, 3.0
    distance = np.arange(30, dtype=float) * speed  # constant 3 m/s
    estimated = savgol_speed(distance, dt)
    assert np.allclose(estimated, speed, atol=1e-6)


def test_savgol_speed_tolerates_nan_gaps() -> None:
    distance = np.arange(30, dtype=float) * 3.0
    distance[10] = np.nan
    estimated = savgol_speed(distance, 1.0)
    assert np.isfinite(estimated).all()


def test_latlon_to_enu_orientation() -> None:
    lat = np.array([0.0, 0.0, 0.001])  # north increases with latitude
    lon = np.array([0.0, 0.001, 0.0])  # east increases with longitude
    east, north = latlon_to_enu(lat, lon)
    assert east[1] > east[0]
    assert north[2] > north[0]


def test_kalman_rts_speed_recovers_constant_speed() -> None:
    dt, speed, n = 1.0, 3.0, 60
    east = speed * np.arange(n, dtype=float)  # straight track due east at 3 m/s
    north = np.zeros(n)
    estimated = kalman_rts_speed(east, north, dt)
    assert np.mean(estimated[20:]) == pytest.approx(speed, abs=0.4)


def test_integrate_speed_constant() -> None:
    distance = integrate_speed(np.full(11, 2.0), dt_s=1.0)  # 2 m/s for 10 s
    assert distance[-1] == pytest.approx(20.0)


def test_calibrate_to_distance_matches_target() -> None:
    speed = np.array([1.0, 2.0, 3.0, 2.0, 1.0])
    calibrated = calibrate_to_distance(speed, dt_s=1.0, target_distance_m=100.0)
    assert integrate_speed(calibrated, 1.0)[-1] == pytest.approx(100.0)


def test_calibrate_per_lap_matches_each_lap() -> None:
    speed = np.ones(20)
    calibrated = calibrate_per_lap(
        speed, dt_s=1.0, lap_end_indices=[10, 20], lap_distances_m=[50.0, 30.0]
    )
    assert integrate_speed(calibrated[:10], 1.0)[-1] == pytest.approx(50.0)
    assert integrate_speed(calibrated[10:], 1.0)[-1] == pytest.approx(30.0)


def test_robust_speed_removes_a_spike() -> None:
    speed = np.full(30, 3.0)
    speed[15] = 9.0  # single GNSS spike
    out = robust_speed(speed, 1.0)
    assert out[15] == pytest.approx(3.0, abs=0.2)


def test_calibrate_ground_truth_uses_only_long_laps() -> None:
    speed = np.full(400, 5.0)
    # lap 0 is 1 km (trustworthy), lap 1 is 400 m (short, ignored for scale)
    out = calibrate_ground_truth(speed, 1.0, [200, 400], [1000.0, 400.0], min_segment_m=1000.0)
    scale = 1000.0 / float(integrate_speed(speed[:200], 1.0)[-1])
    assert out[0] == pytest.approx(5.0 * scale)


def test_track_concentration_loop_vs_line() -> None:
    theta = np.linspace(0.0, 20.0 * np.pi, 2000)  # many laps of a ~90 m oval
    radius_deg = 0.0008
    lat = 48.0 + radius_deg * np.sin(theta)
    lon = 8.0 + radius_deg * np.cos(theta)
    assert track_concentration(lat, lon) > 0.5
    straight_lat = 48.0 + np.linspace(0.0, 0.018, 2000)  # ~2 km straight
    straight_lon = np.full(2000, 8.0)
    assert track_concentration(straight_lat, straight_lon) < 0.3


def test_cadence_change_points_detects_steps() -> None:
    cadence = np.concatenate([np.full(60, 168.0), np.full(60, 176.0), np.full(60, 168.0)])
    boundaries = cadence_change_points(cadence, 1.0)
    assert any(abs(b - 60) <= 8 for b in boundaries)
    assert any(abs(b - 120) <= 8 for b in boundaries)


def test_ground_truth_speed_calibrates_to_km_lap() -> None:
    n = 300
    speed = np.full(n, 3.325)  # enhanced_speed reads ~5% slow
    ts = pd.date_range("2026-06-14T17:00:00+00:00", periods=n, freq="1s")
    records = pd.DataFrame(
        {
            S.TIMESTAMP: ts,
            S.T_S: np.arange(n, dtype=float),
            S.SPEED_MPS: speed,
            S.DISTANCE_M: np.cumsum(speed),
            S.LATITUDE_DEG: np.full(n, np.nan),
            S.LONGITUDE_DEG: np.full(n, np.nan),
        }
    )
    laps = build_laps_frame(
        [
            {
                "start_time": ts[0].to_pydatetime(),
                "total_elapsed_time": float(n),
                "total_distance": 1050.0,
            }
        ]
    )
    meta = ActivityMeta(
        source_path=Path("synthetic.fit"),
        sport="running",
        sub_sport="generic",
        start_time=ts[0],
        n_records=n,
        sampling_dt_s=1.0,
        is_treadmill=False,
    )
    gt = ground_truth_speed(Activity(records=records, laps=laps, meta=meta))
    assert integrate_speed(gt, 1.0)[-1] == pytest.approx(1050.0, rel=1e-3)
