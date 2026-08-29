"""Normalized column names and the pandera schema for parsed activity records.

One tidy per-second record frame is produced from any activity source. Column names are
explicit about units so downstream code never has to guess. The schema validates the frame at
the ingestion boundary (see the project's scalable-architecture rule).
"""

from __future__ import annotations

import pandera.pandas as pa

# Record columns (order is the canonical column order of the records frame).
TIMESTAMP = "timestamp"
T_S = "t_s"
LATITUDE_DEG = "latitude_deg"
LONGITUDE_DEG = "longitude_deg"
DISTANCE_M = "distance_m"
SPEED_MPS = "speed_mps"
ALTITUDE_M = "altitude_m"
CADENCE_SPM = "cadence_spm"
VERTICAL_OSCILLATION_MM = "vertical_oscillation_mm"
GROUND_CONTACT_TIME_MS = "ground_contact_time_ms"
STANCE_TIME_PCT = "stance_time_pct"
VERTICAL_RATIO_PCT = "vertical_ratio_pct"
GCT_BALANCE_PCT = "gct_balance_pct"
STEP_LENGTH_M = "step_length_m"
HEART_RATE_BPM = "heart_rate_bpm"
RESPIRATION_RATE_BPM = "respiration_rate_bpm"
TEMPERATURE_C = "temperature_c"
RUNNING_POWER_W = "running_power_w"
DYNAMICS_PACE_SPEED_MPS = "dynamics_pace_speed_mps"
DYNAMICS_PACE_GRADE_PCT = "dynamics_pace_grade_pct"

RECORD_COLUMNS: tuple[str, ...] = (
    TIMESTAMP,
    T_S,
    LATITUDE_DEG,
    LONGITUDE_DEG,
    DISTANCE_M,
    SPEED_MPS,
    ALTITUDE_M,
    CADENCE_SPM,
    VERTICAL_OSCILLATION_MM,
    GROUND_CONTACT_TIME_MS,
    STANCE_TIME_PCT,
    VERTICAL_RATIO_PCT,
    GCT_BALANCE_PCT,
    STEP_LENGTH_M,
    HEART_RATE_BPM,
    RESPIRATION_RATE_BPM,
    TEMPERATURE_C,
    RUNNING_POWER_W,
    DYNAMICS_PACE_SPEED_MPS,
    DYNAMICS_PACE_GRADE_PCT,
)

# Lap columns.
LAP_INDEX = "lap_index"
LAP_START_TIME = "start_time"
LAP_END_TIME = "end_time"
LAP_ELAPSED_S = "total_elapsed_time_s"
LAP_TIMER_S = "total_timer_time_s"
LAP_DISTANCE_M = "total_distance_m"

LAP_COLUMNS: tuple[str, ...] = (
    LAP_INDEX,
    LAP_START_TIME,
    LAP_END_TIME,
    LAP_ELAPSED_S,
    LAP_TIMER_S,
    LAP_DISTANCE_M,
)


def _f(*, nullable: bool = True, ge: float | None = None, le: float | None = None) -> pa.Column:
    """A nullable, coercible float column with optional physical-range bounds."""
    checks = []
    if ge is not None:
        checks.append(pa.Check.ge(ge))
    if le is not None:
        checks.append(pa.Check.le(le))
    return pa.Column(float, nullable=nullable, coerce=True, checks=checks or None)


# Records: timestamp and t_s are the always-present fields; distance and every sensor field may be
# null (a sample can be absent early in a run or on some devices), so one bad record does not
# discard the whole activity.
RECORDS_SCHEMA = pa.DataFrameSchema(
    {
        TIMESTAMP: pa.Column("datetime64[ns, UTC]", nullable=False, coerce=True),
        T_S: _f(nullable=False, ge=0.0),
        LATITUDE_DEG: _f(ge=-90.0, le=90.0),
        LONGITUDE_DEG: _f(ge=-180.0, le=180.0),
        # Nullable: an occasional record can lack distance (before GPS lock, indoor); one bad
        # sample must not discard the whole activity.
        DISTANCE_M: _f(ge=0.0),
        # Wide bound: raw Garmin speed carries GPS spikes, and non-running sports (cycling) reach
        # higher speeds. Ingestion accepts the raw signal; cleaning happens in the smoothing stage.
        SPEED_MPS: _f(ge=0.0, le=25.0),
        ALTITUDE_M: _f(ge=-500.0, le=9000.0),
        CADENCE_SPM: _f(ge=0.0, le=320.0),
        VERTICAL_OSCILLATION_MM: _f(ge=0.0, le=300.0),
        GROUND_CONTACT_TIME_MS: _f(ge=0.0, le=2047.0),
        STANCE_TIME_PCT: _f(ge=0.0, le=100.0),
        VERTICAL_RATIO_PCT: _f(ge=0.0, le=100.0),
        GCT_BALANCE_PCT: _f(ge=0.0, le=100.0),
        STEP_LENGTH_M: _f(ge=0.0, le=8.0),
        HEART_RATE_BPM: _f(ge=0.0, le=260.0),
        RESPIRATION_RATE_BPM: _f(ge=0.0, le=120.0),
        TEMPERATURE_C: _f(ge=-40.0, le=60.0),
        RUNNING_POWER_W: _f(ge=0.0, le=2000.0),
        DYNAMICS_PACE_SPEED_MPS: _f(ge=0.0, le=25.0),
        DYNAMICS_PACE_GRADE_PCT: _f(ge=-60.0, le=60.0),
    },
    strict=True,
    ordered=False,
    coerce=True,
)

# Laps cross the module boundary inside Activity, so they are validated too.
LAPS_SCHEMA = pa.DataFrameSchema(
    {
        LAP_INDEX: pa.Column(int, nullable=False, coerce=True, checks=pa.Check.ge(0)),
        LAP_START_TIME: pa.Column("datetime64[ns, UTC]", nullable=True, coerce=True),
        LAP_END_TIME: pa.Column("datetime64[ns, UTC]", nullable=True, coerce=True),
        LAP_ELAPSED_S: _f(ge=0.0),
        LAP_TIMER_S: _f(ge=0.0),
        LAP_DISTANCE_M: _f(ge=0.0),
    },
    strict=True,
    coerce=True,
)
