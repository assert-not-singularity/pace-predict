"""Unit tests for the pure FIT-normalization transforms.

Real activity data carries home GPS and is never committed, so these tests exercise the pure
builders with synthetic records; the fitdecode I/O is verified manually against real files.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from pace_predict.io import schema as S
from pace_predict.io.fit import (
    build_laps_frame,
    build_records_frame,
    running_cadence_spm,
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


def test_build_laps_frame_computes_end_time() -> None:
    start = datetime(2026, 6, 14, 17, 35, 23, tzinfo=UTC)
    laps = build_laps_frame(
        [{"start_time": start, "total_elapsed_time": 60.0, "total_distance": 400.0}]
    )
    assert list(laps.columns) == list(S.LAP_COLUMNS)
    assert laps[S.LAP_DISTANCE_M].iloc[0] == pytest.approx(400.0)
    assert (laps[S.LAP_END_TIME].iloc[0] - laps[S.LAP_START_TIME].iloc[0]).total_seconds() == 60.0
