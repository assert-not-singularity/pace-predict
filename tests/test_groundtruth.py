"""Unit tests for the ground-truth speed estimators and distance calibration."""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pace_predict.groundtruth import (
    ACCEL,
    DECEL,
    STEADY,
    LapSegment,
    calibrate_ground_truth,
    calibrate_per_lap,
    calibrate_to_distance,
    ground_truth_speed,
    guard_dropouts,
    integrate_speed,
    lap_segments,
    latlon_to_enu,
    median_dt_s,
    phase_labels,
    phase_segments,
    robust_speed,
    segment_pace,
    track_concentration,
)
from pace_predict.io import schema as S
from pace_predict.io.fit import Activity, ActivityMeta, build_laps_frame


def test_latlon_to_enu_orientation() -> None:
    lat = np.array([0.0, 0.0, 0.001])  # north increases with latitude
    lon = np.array([0.0, 0.001, 0.0])  # east increases with longitude
    east, north = latlon_to_enu(lat, lon)
    assert east[1] > east[0]
    assert north[2] > north[0]


def test_integrate_speed_constant() -> None:
    distance = integrate_speed(np.full(11, 2.0), dt_s=1.0)  # 2 m/s for 10 s
    assert distance[-1] == pytest.approx(20.0)


def test_calibrate_to_distance_matches_target() -> None:
    speed = np.array([1.0, 2.0, 3.0, 2.0, 1.0])
    calibrated = calibrate_to_distance(speed, dt_s=1.0, target_distance_m=100.0)
    assert integrate_speed(calibrated, 1.0)[-1] == pytest.approx(100.0)


def test_calibrate_per_lap_matches_each_lap() -> None:
    speed = np.ones(20)
    segments = [LapSegment(0, 10, 50.0), LapSegment(10, 20, 30.0)]
    calibrated = calibrate_per_lap(speed, 1.0, segments)
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
    segments = [LapSegment(0, 200, 1000.0), LapSegment(200, 400, 400.0)]
    out = calibrate_ground_truth(speed, 1.0, segments, min_segment_m=1000.0)
    scale = 1000.0 / float(integrate_speed(speed[:200], 1.0)[-1])
    assert out[0] == pytest.approx(5.0 * scale)


def test_calibrate_ground_truth_falls_back_without_long_laps() -> None:
    speed = np.full(100, 4.0)
    segments = [LapSegment(0, 50, 400.0), LapSegment(50, 100, 300.0)]  # both short
    out = calibrate_ground_truth(speed, 1.0, segments, min_segment_m=1000.0)
    assert integrate_speed(out, 1.0)[-1] == pytest.approx(700.0)  # scaled to total distance


def test_median_dt_s_ignores_duplicate_timestamps() -> None:
    records = pd.DataFrame({S.T_S: np.array([0.0, 0.0, 1.0, 2.0, 3.0])})  # a duplicate at t=0
    assert median_dt_s(records) == pytest.approx(1.0)


def test_lap_segments_are_contiguous_without_double_counting() -> None:
    n = 400
    ts = pd.date_range("2026-06-14T17:00:00+00:00", periods=n, freq="1s")
    records = pd.DataFrame(
        {S.TIMESTAMP: ts, S.T_S: np.arange(n, dtype=float), S.DISTANCE_M: np.arange(n, dtype=float)}
    )
    laps = build_laps_frame(
        [
            {
                "start_time": ts[0].to_pydatetime(),
                "total_elapsed_time": 200.0,
                "total_distance": 1000.0,
            },
            {
                "start_time": ts[200].to_pydatetime(),
                "total_elapsed_time": 200.0,
                "total_distance": 1000.0,
            },
        ]
    )
    segments = lap_segments(records, laps)
    assert segments == [LapSegment(0, 200, 1000.0), LapSegment(200, 400, 1000.0)]


def test_lap_segments_drops_lap_with_missing_start() -> None:
    n = 100
    ts = pd.date_range("2026-06-14T17:00:00+00:00", periods=n, freq="1s")
    records = pd.DataFrame(
        {S.TIMESTAMP: ts, S.T_S: np.arange(n, dtype=float), S.DISTANCE_M: np.arange(n, dtype=float)}
    )
    laps = pd.DataFrame(
        {
            S.LAP_INDEX: [0, 1],
            S.LAP_START_TIME: [ts[0], pd.NaT],
            S.LAP_END_TIME: [ts[50], ts[99]],
            S.LAP_ELAPSED_S: [50.0, 49.0],
            S.LAP_TIMER_S: [50.0, 49.0],
            S.LAP_DISTANCE_M: [500.0, 500.0],
        }
    )
    segments = lap_segments(records, laps)
    assert segments == [LapSegment(0, n, 500.0)]  # only the valid lap survives, tiled to end


def test_track_concentration_loop_vs_line() -> None:
    theta = np.linspace(0.0, 20.0 * np.pi, 2000)  # many laps of a ~90 m oval
    radius_deg = 0.0008
    lat = 48.0 + radius_deg * np.sin(theta)
    lon = 8.0 + radius_deg * np.cos(theta)
    assert track_concentration(lat, lon) > 0.5
    straight_lat = 48.0 + np.linspace(0.0, 0.018, 2000)  # ~2 km straight
    straight_lon = np.full(2000, 8.0)
    assert track_concentration(straight_lat, straight_lon) < 0.3


# ---- phase segmentation -------------------------------------------------------------------------


def _cadence_step(fast: float = 176.0, slow: float = 168.0, ramp: int = 8) -> np.ndarray:
    """Cadence: slow plateau -> ramp up -> fast plateau -> ramp down -> slow plateau."""
    return np.concatenate(
        [
            np.full(40, slow),
            np.linspace(slow, fast, ramp),
            np.full(40, fast),
            np.linspace(fast, slow, ramp),
            np.full(40, slow),
        ]
    )


def test_phase_labels_detects_accel_and_decel() -> None:
    labels = phase_labels(_cadence_step(), 1.0)
    assert (labels == ACCEL).any()  # cadence rising
    assert (labels == DECEL).any()  # cadence falling
    assert labels[20] == STEADY  # inside the first plateau
    assert labels[-20] == STEADY  # inside the last plateau


def test_phase_labels_suppresses_quantization_wobble() -> None:
    cadence = 174.0 + np.tile([0.0, 1.0, 0.0, -1.0], 20)  # integer +/-1 spm jitter, no real trend
    labels = phase_labels(cadence, 1.0)
    assert np.all(labels == STEADY)


def test_phase_segments_partition_is_contiguous() -> None:
    labels = np.array([0, 0, 1, 1, 1, 0, -1, -1, 0])
    segments = phase_segments(labels)
    assert segments[0].start == 0
    assert segments[-1].end == len(labels)
    assert [s.phase for s in segments] == [0, 1, 0, -1, 0]
    for earlier, later in itertools.pairwise(segments):
        assert earlier.end == later.start  # no gap, no overlap


# ---- dropout guard ------------------------------------------------------------------------------


def test_guard_dropouts_interpolates_short_dropout() -> None:
    speed = np.full(60, 3.0)
    speed[30:34] = 0.05  # 4 s GNSS dropout under canopy
    out = guard_dropouts(speed, 1.0)
    assert out[31] == pytest.approx(3.0, abs=0.3)  # filled from the surrounding level


def test_guard_dropouts_preserves_sustained_stop() -> None:
    speed = np.concatenate([np.full(20, 3.0), np.zeros(30), np.full(20, 3.0)])  # a 30 s stop
    out = guard_dropouts(speed, 1.0, max_gap_s=8.0)
    assert out[35] == pytest.approx(0.0, abs=0.1)  # a real stop is not interpolated away


def test_guard_dropouts_keeps_short_run_at_the_edge() -> None:
    # A brief low-speed run at the very start has no context on one side to interpolate from, so it
    # is left as recorded (a real start-up, not extended flat to running speed).
    speed = np.concatenate([np.full(4, 0.05), np.full(56, 3.0)])
    out = guard_dropouts(speed, 1.0)
    assert out[1] == pytest.approx(0.05, abs=0.3)


# ---- reconstruction (segment_pace) --------------------------------------------------------------


def test_segment_pace_holds_steady_levels_and_ramps() -> None:
    cadence = _cadence_step()
    speed = np.concatenate(
        [
            np.full(40, 4.0),
            np.linspace(4.0, 3.0, 8),
            np.full(40, 3.0),
            np.linspace(3.0, 4.0, 8),
            np.full(40, 4.0),
        ]
    )
    records = pd.DataFrame({S.SPEED_MPS: speed, S.CADENCE_SPM: cadence})
    out = segment_pace(records, 1.0)
    assert out[:40].std() < 0.1  # fast plateau held near-constant
    assert out[48:88].std() < 0.1  # slow plateau held near-constant
    assert out[20] == pytest.approx(4.0, abs=0.2)
    assert out[68] == pytest.approx(3.0, abs=0.2)
    assert out[20] > out[68]  # distinct levels preserved, not smoothed together
    # The decel transition (indices 40-47) ramps monotonically between the two plateau levels.
    ramp = out[40:48]
    assert out[68] - 0.2 < ramp.min() and ramp.max() < out[20] + 0.2
    assert np.all(np.diff(ramp) < 0.0)


def test_segment_pace_follows_measured_speed_on_trailing_accel() -> None:
    # A run that ends mid-acceleration (sprint finish): the final transition has no plateau to ramp
    # to, so it must follow the measured speed rather than flatten to the preceding plateau level.
    plateau, ramp = 50, 15
    cadence = np.concatenate([np.full(plateau, 168.0), np.linspace(168.0, 182.0, ramp)])
    speed = np.concatenate([np.full(plateau, 3.0), np.linspace(3.0, 4.8, ramp)])
    records = pd.DataFrame({S.SPEED_MPS: speed, S.CADENCE_SPM: cadence})
    out = segment_pace(records, 1.0)
    assert out[-1] > 4.0  # sprint speed is followed, not held at the 3.0 plateau
    assert out[-1] > out[20] + 1.0


def test_segment_pace_falls_back_without_cadence() -> None:
    # No usable cadence must not collapse a real interval to one flat level.
    speed = np.concatenate([np.full(30, 3.0), np.full(30, 5.0), np.full(30, 3.0)])
    records = pd.DataFrame({S.SPEED_MPS: speed, S.CADENCE_SPM: np.full(90, np.nan)})
    out = segment_pace(records, 1.0)
    assert out.std() > 0.3  # the fast middle survives instead of being flattened away
    assert out[45] > out[15] + 1.0


def _synthetic_activity(speed: np.ndarray, cadence: np.ndarray, lap_distance_m: float) -> Activity:
    n = len(speed)
    ts = pd.date_range("2026-06-14T17:00:00+00:00", periods=n, freq="1s")
    records = pd.DataFrame(
        {
            S.TIMESTAMP: ts,
            S.T_S: np.arange(n, dtype=float),
            S.SPEED_MPS: speed,
            S.CADENCE_SPM: cadence,
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
                "total_distance": lap_distance_m,
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
    return Activity(records=records, laps=laps, meta=meta)


def test_ground_truth_speed_calibrates_to_km_lap() -> None:
    n = 300
    speed = np.full(n, 3.325)  # enhanced_speed reads ~5% slow
    activity = _synthetic_activity(speed, np.full(n, 170.0), lap_distance_m=1050.0)
    gt = ground_truth_speed(activity)
    assert integrate_speed(gt, 1.0)[-1] == pytest.approx(1050.0, rel=1e-3)


def test_ground_truth_speed_all_zero_speed_does_not_crash() -> None:
    n = 60
    activity = _synthetic_activity(np.zeros(n), np.zeros(n), lap_distance_m=0.0)
    gt = ground_truth_speed(activity)
    assert np.all(gt == 0.0)
