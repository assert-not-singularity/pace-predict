"""Unit tests for the pure FIT-normalization transforms.

Real activity data carries home GPS and is never committed, so these tests exercise the pure
builders with synthetic records; the fitdecode I/O is verified manually against real files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pandera.errors import SchemaError

from pace_predict import inventory
from pace_predict.io import schema as S
from pace_predict.io.fit import (
    Activity,
    ActivityMeta,
    build_laps_frame,
    build_records_frame,
    build_sessions_frame,
    running_cadence_spm,
    running_sessions,
    semicircles_to_degrees,
)


def test_semicircles_to_degrees() -> None:
    assert semicircles_to_degrees(0) == 0.0
    assert semicircles_to_degrees(2**30) == pytest.approx(90.0)
    assert semicircles_to_degrees(-(2**30)) == pytest.approx(-90.0)
    assert semicircles_to_degrees(None) is None


def test_running_cadence_spm_doubles_per_leg_cadence() -> None:
    assert running_cadence_spm(86, 0.5) == pytest.approx(173.0)
    assert running_cadence_spm(80, None) == pytest.approx(160.0)
    assert running_cadence_spm(None, 0.5) is None


def _synthetic_rows() -> list[dict[str, Any]]:
    base = datetime(2026, 6, 14, 17, 35, 23, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for i in range(3):
        rows.append(
            {
                "timestamp": base.replace(second=23 + i),
                "position_lat": 2**30,  # -> 90 deg
                "position_long": 0,
                "distance": 100.0 + 4.0 * i,
                "enhanced_speed": 4.0,
                "enhanced_altitude": 80.0,
                "cadence": 86,
                "fractional_cadence": 0.5,
                "vertical_oscillation": 110.0,
                "stance_time": 222.0,
                "stance_time_percent": 32.0,
                "vertical_ratio": 7.7,
                "stance_time_balance": 50.9,
                "step_length": 1396.0,
                "heart_rate": 166,
                "Geschwindigkeit (dynamicsPace)": 4.08,
                "Grade (dynamicsPace)": 0.2,
            }
        )
    return rows


def test_build_records_frame_shape_and_normalization() -> None:
    frame = build_records_frame(_synthetic_rows())

    assert list(frame.columns) == list(S.RECORD_COLUMNS)
    assert len(frame) == 3
    assert frame[S.T_S].tolist() == [0.0, 1.0, 2.0]
    assert frame[S.LATITUDE_DEG].iloc[0] == pytest.approx(90.0)
    assert frame[S.CADENCE_SPM].iloc[0] == pytest.approx(173.0)
    assert frame[S.STEP_LENGTH_M].iloc[0] == pytest.approx(1.396)
    assert frame[S.SPEED_MPS].iloc[0] == pytest.approx(4.0)
    assert frame[S.DYNAMICS_PACE_SPEED_MPS].iloc[0] == pytest.approx(4.08)
    assert frame[S.DYNAMICS_PACE_GRADE_PCT].iloc[0] == pytest.approx(0.2)


def test_build_records_frame_validates_against_schema() -> None:
    frame = build_records_frame(_synthetic_rows())
    S.RECORDS_SCHEMA.validate(frame)  # raises on failure


def test_missing_optional_field_becomes_null_column() -> None:
    frame = build_records_frame(_synthetic_rows())
    # running_power was never supplied, so the column exists and is entirely null.
    assert S.RUNNING_POWER_W in frame.columns
    assert frame[S.RUNNING_POWER_W].isna().all()


def test_build_records_frame_empty_raises() -> None:
    with pytest.raises(ValueError, match="no record messages"):
        build_records_frame([])


def test_dynamics_pace_speed_zero_is_preserved() -> None:
    # A legitimate 0.0 (runner at rest) must not be dropped by falsy fallback logic.
    rows = _synthetic_rows()
    rows[0]["Geschwindigkeit (dynamicsPace)"] = 0.0
    frame = build_records_frame(rows)
    assert frame[S.DYNAMICS_PACE_SPEED_MPS].iloc[0] == 0.0


def test_build_laps_frame_computes_end_time() -> None:
    start = datetime(2026, 6, 14, 17, 35, 23, tzinfo=UTC)
    laps = build_laps_frame(
        [{"start_time": start, "total_elapsed_time": 60.0, "total_distance": 400.0}]
    )
    assert list(laps.columns) == list(S.LAP_COLUMNS)
    assert laps[S.LAP_DISTANCE_M].iloc[0] == pytest.approx(400.0)
    assert (laps[S.LAP_END_TIME].iloc[0] - laps[S.LAP_START_TIME].iloc[0]).total_seconds() == 60.0


def test_build_laps_frame_zero_elapsed_keeps_end_time() -> None:
    # A zero-length lap has elapsed 0.0; end time must equal start, not become null.
    start = datetime(2026, 6, 14, 17, 35, 23, tzinfo=UTC)
    laps = build_laps_frame(
        [{"start_time": start, "total_elapsed_time": 0.0, "total_distance": 0.0}]
    )
    assert laps[S.LAP_END_TIME].iloc[0] == laps[S.LAP_START_TIME].iloc[0]


def test_missing_distance_on_one_record_is_allowed() -> None:
    # One record lacking distance must not discard the whole activity.
    rows = _synthetic_rows()
    del rows[1]["distance"]
    frame = build_records_frame(rows)
    S.RECORDS_SCHEMA.validate(frame)
    assert frame[S.DISTANCE_M].isna().iloc[1]


def test_record_without_timestamp_is_dropped_and_frame_is_sorted() -> None:
    rows = _synthetic_rows()  # timestamps at seconds 23, 24, 25
    rows[1]["timestamp"] = None  # drop the middle record
    frame = build_records_frame(list(reversed(rows)))  # reversed input must come out time-sorted
    assert len(frame) == 2
    assert frame[S.T_S].tolist() == [0.0, 2.0]


def test_non_numeric_field_becomes_null_not_crash() -> None:
    rows = _synthetic_rows()
    rows[0]["heart_rate"] = "n/a"
    frame = build_records_frame(rows)  # must not raise
    assert frame[S.HEART_RATE_BPM].isna().iloc[0]


def test_schema_rejects_out_of_range_speed() -> None:
    frame = build_records_frame(_synthetic_rows())
    frame.loc[0, S.SPEED_MPS] = 999.0
    with pytest.raises(SchemaError):
        S.RECORDS_SCHEMA.validate(frame)


def test_inventory_handles_all_files_failing(tmp_path: Path) -> None:
    # A directory of unparseable files must report failure, not crash with KeyError.
    (tmp_path / "bad.fit").write_bytes(b"not a fit file")
    assert inventory.main([str(tmp_path)]) == 1


def test_inventory_rejects_non_directory(tmp_path: Path) -> None:
    # Passing a file (not a directory) must return an error, not crash on iterdir().
    target = tmp_path / "activity.fit"
    target.write_bytes(b"x")
    assert inventory.main([str(target)]) == 1


# ---- multisport session splitting ---------------------------------------------------------------
#
# A triathlon FIT file carries several `session` messages (swim, bike, run, ...) sharing one
# per-second record stream. These synthetic fixtures model a two-session file — a bike leg then a
# run leg — so `running_sessions` can be exercised without a real (GPS-bearing) file.

_MS_BASE = datetime(2026, 8, 16, 11, 30, 0, tzinfo=UTC)


def _multisport_record_rows(n_bike: int = 10, n_run: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(n_bike + n_run):
        is_run = i >= n_bike
        rows.append(
            {
                "timestamp": _MS_BASE + timedelta(seconds=i),
                "position_lat": 2**30,  # -> 90 deg, so GPS is present
                "position_long": 0,
                "distance": 10.0 * i,
                "enhanced_speed": 3.5 if is_run else 8.0,
                "cadence": 85 if is_run else 45,
                "fractional_cadence": 0.0,
            }
        )
    return rows


def _session_row(
    sport: str,
    sub_sport: str,
    window: tuple[int, float],  # (start_offset_s, elapsed_s)
    distance_m: float,
    laps: tuple[int, int],  # (first_lap_index, num_laps)
) -> dict[str, Any]:
    start_offset_s, elapsed_s = window
    first_lap_index, num_laps = laps
    return {
        "sport": sport,
        "sub_sport": sub_sport,
        "start_time": _MS_BASE + timedelta(seconds=start_offset_s),
        "total_elapsed_time": elapsed_s,
        "total_timer_time": elapsed_s,
        "total_distance": distance_m,
        "first_lap_index": first_lap_index,
        "num_laps": num_laps,
    }


def _multisport_laps(n_bike: int = 10, n_run: int = 10) -> pd.DataFrame:
    return build_laps_frame(
        [
            {"start_time": _MS_BASE, "total_elapsed_time": float(n_bike), "total_distance": 100.0},
            {
                "start_time": _MS_BASE + timedelta(seconds=n_bike),
                "total_elapsed_time": float(n_run),
                "total_distance": 50.0,
            },
        ]
    )


def _meta(
    records: pd.DataFrame, *, sport: str = "running", sub_sport: str = "generic"
) -> ActivityMeta:
    return ActivityMeta(
        source_path=Path("triathlon.fit"),
        sport=sport,
        sub_sport=sub_sport,
        start_time=records[S.TIMESTAMP].min(),
        n_records=len(records),
        sampling_dt_s=1.0,
        is_treadmill=False,
    )


def _multisport_activity() -> Activity:
    records = build_records_frame(_multisport_record_rows())
    sessions = build_sessions_frame(
        [
            _session_row("cycling", "generic", (0, 10.0), 100.0, (0, 1)),
            _session_row("running", "generic", (10, 10.0), 50.0, (1, 1)),
        ],
        fallback_sport=None,
        fallback_sub_sport=None,
        records=records,
    )
    # sport="running" mirrors what read_fit_frames' last-wins reports for a swim/bike/run file.
    return Activity(
        records=records, laps=_multisport_laps(), meta=_meta(records), sessions=sessions
    )


def test_build_sessions_frame_shape_and_end_time() -> None:
    records = build_records_frame(_multisport_record_rows())
    sessions = build_sessions_frame(
        [
            _session_row("cycling", "generic", (0, 10.0), 100.0, (0, 1)),
            _session_row("running", "generic", (10, 10.0), 50.0, (1, 1)),
        ],
        fallback_sport=None,
        fallback_sub_sport=None,
        records=records,
    )
    assert list(sessions.columns) == list(S.SESSION_COLUMNS)
    assert sessions[S.SESSION_SPORT].tolist() == ["cycling", "running"]
    run = sessions.iloc[1]
    assert run[S.SESSION_END_TIME] - run[S.SESSION_START_TIME] == pd.Timedelta(seconds=10)
    S.SESSIONS_SCHEMA.validate(sessions)  # raises on failure


def test_running_sessions_extracts_only_the_run_leg() -> None:
    runs = running_sessions(_multisport_activity())
    assert len(runs) == 1
    run = runs[0]
    assert run.meta.sport == "running"
    assert run.meta.n_records == 10
    # t_s is rebased to the run's own start, and the window is the run's [start, end).
    assert run.records[S.T_S].tolist() == [float(i) for i in range(10)]
    assert run.records[S.TIMESTAMP].min() == _MS_BASE + timedelta(seconds=10)
    assert run.records[S.TIMESTAMP].max() == _MS_BASE + timedelta(seconds=19)


def test_running_sessions_boundary_record_is_not_double_assigned() -> None:
    # The record at the bike->run boundary second (t=10) belongs to the run (its window start),
    # leaving the bike with t=9 as its last — so exactly 10 records land in the run, none shared.
    run = running_sessions(_multisport_activity())[0]
    assert len(run.records) == 10
    assert (run.records[S.TIMESTAMP] >= _MS_BASE + timedelta(seconds=10)).all()


def test_running_sessions_selects_the_sessions_laps() -> None:
    run = running_sessions(_multisport_activity())[0]
    assert run.laps[S.LAP_INDEX].tolist() == [1]
    assert run.laps[S.LAP_DISTANCE_M].iloc[0] == pytest.approx(50.0)


def test_running_sessions_empty_when_no_running_leg() -> None:
    records = build_records_frame(_multisport_record_rows())
    sessions = build_sessions_frame(
        [_session_row("cycling", "generic", (0, 20.0), 200.0, (0, 2))],
        fallback_sport=None,
        fallback_sub_sport=None,
        records=records,
    )
    activity = Activity(
        records=records,
        laps=_multisport_laps(),
        meta=_meta(records, sport="cycling"),
        sessions=sessions,
    )
    assert running_sessions(activity) == []


def test_running_sessions_single_running_session_returns_whole_activity() -> None:
    # A plain single-session running file: the one session spans every record, so nothing is lost.
    records = build_records_frame(_multisport_record_rows())
    sessions = build_sessions_frame(
        [_session_row("running", "generic", (0, 20.0), 200.0, (0, 2))],
        fallback_sport="running",
        fallback_sub_sport="generic",
        records=records,
    )
    activity = Activity(
        records=records, laps=_multisport_laps(), meta=_meta(records), sessions=sessions
    )
    runs = running_sessions(activity)
    assert len(runs) == 1
    assert runs[0].meta.n_records == 20


def test_build_sessions_frame_synthesizes_session_when_file_has_none() -> None:
    # No `session` message => one synthetic session spanning all records, with an unbounded end so
    # every record is included on the split.
    records = build_records_frame(_multisport_record_rows())
    sessions = build_sessions_frame(
        [], fallback_sport="running", fallback_sub_sport="generic", records=records
    )
    assert len(sessions) == 1
    assert sessions[S.SESSION_SPORT].iloc[0] == "running"
    assert pd.isna(sessions[S.SESSION_END_TIME].iloc[0])

    activity = Activity(
        records=records, laps=_multisport_laps(), meta=_meta(records), sessions=sessions
    )
    runs = running_sessions(activity)
    assert len(runs) == 1
    assert runs[0].meta.n_records == 20
