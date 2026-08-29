"""Parse Garmin ``.fit`` activity files into a tidy, validated per-second record frame.

The fitdecode I/O (``read_fit_frames``) is kept separate from the pure normalization
(``build_records_frame`` / ``build_laps_frame``) so the transforms are unit-testable without a
real FIT file — and so real activity data (which carries home GPS coordinates) never needs to be
committed as a fixture.

Units are normalized and made explicit in the column names (see ``schema``). Notably:

- Cadence is stored per leg (strides/min); running cadence in steps/min is ``2 * (cadence +
  fractional_cadence)``.
- ``step_length`` and ``vertical_oscillation`` are millimetres in FIT; step length is converted to
  metres, vertical oscillation is kept in millimetres.
- Latitude/longitude are semicircles in FIT and converted to degrees.
- The ``dynamicsPace`` Connect IQ data field logs its own speed/grade estimate as developer
  fields; the speed estimate is captured as ``dynamics_pace_speed_mps`` — the baseline to beat.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitdecode
import pandas as pd

from pace_predict.io import schema as S

log = logging.getLogger(__name__)

_SEMICIRCLE_TO_DEG = 180.0 / 2**31
_MIN_ROWS_FOR_DT = 2  # need at least two timestamps to compute a sampling interval

# Direct source-field -> normalized-column copies (first present, non-null source wins).
_DIRECT: tuple[tuple[str, tuple[str, ...]], ...] = (
    (S.DISTANCE_M, ("distance",)),
    (S.SPEED_MPS, ("enhanced_speed", "speed")),
    (S.ALTITUDE_M, ("enhanced_altitude", "altitude")),
    (S.VERTICAL_OSCILLATION_MM, ("vertical_oscillation",)),
    (S.GROUND_CONTACT_TIME_MS, ("stance_time",)),
    (S.STANCE_TIME_PCT, ("stance_time_percent",)),
    (S.VERTICAL_RATIO_PCT, ("vertical_ratio",)),
    (S.GCT_BALANCE_PCT, ("stance_time_balance",)),
    (S.HEART_RATE_BPM, ("heart_rate",)),
    (S.RESPIRATION_RATE_BPM, ("enhanced_respiration_rate", "respiration_rate")),
    (S.TEMPERATURE_C, ("temperature",)),
    (S.RUNNING_POWER_W, ("RP_Power", "power")),
)


@dataclass(frozen=True)
class ActivityMeta:
    """Non-record metadata about a parsed activity."""

    source_path: Path
    sport: str | None
    sub_sport: str | None
    start_time: pd.Timestamp | None
    n_records: int
    sampling_dt_s: float | None
    is_treadmill: bool


@dataclass(frozen=True)
class Activity:
    """A parsed activity: the per-second record frame, the lap frame, and metadata."""

    records: pd.DataFrame
    laps: pd.DataFrame
    meta: ActivityMeta


# ---- pure helpers -------------------------------------------------------------------------------


def semicircles_to_degrees(value: float | None) -> float | None:
    """Convert a FIT semicircle coordinate to degrees."""
    if value is None:
        return None
    return float(value) * _SEMICIRCLE_TO_DEG


def running_cadence_spm(cadence: float | None, fractional_cadence: float | None) -> float | None:
    """Running cadence in steps/min from the per-leg FIT cadence fields.

    FIT stores cadence per leg (strides/min); steps/min is twice the total.
    """
    if cadence is None:
        return None
    frac = 0.0 if fractional_cadence is None else float(fractional_cadence)
    return 2.0 * (float(cadence) + frac)


def _first(row: Mapping[str, Any], names: Iterable[str]) -> Any | None:
    for name in names:
        value = row.get(name)
        if value is not None:
            return value
    return None


def _as_float(value: Any | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _developer_value(row: Mapping[str, Any], *, contains: str, kind: str) -> float | None:
    """Find a developer field by substring match on its name (robust to app language)."""
    for key, value in row.items():
        if value is None:
            continue
        lowered = key.lower()
        if contains in lowered and kind in lowered:
            return float(value)
    return None


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {name: None for name in S.RECORD_COLUMNS}
    out[S.TIMESTAMP] = row.get("timestamp")
    out[S.LATITUDE_DEG] = semicircles_to_degrees(_as_float(row.get("position_lat")))
    out[S.LONGITUDE_DEG] = semicircles_to_degrees(_as_float(row.get("position_long")))
    out[S.CADENCE_SPM] = running_cadence_spm(
        _as_float(row.get("cadence")), _as_float(row.get("fractional_cadence"))
    )
    step_length_mm = _as_float(row.get("step_length"))
    out[S.STEP_LENGTH_M] = None if step_length_mm is None else step_length_mm / 1000.0
    out[S.DYNAMICS_PACE_SPEED_MPS] = _developer_value(
        row, contains="dynamicspace", kind="geschwindig"
    ) or _developer_value(row, contains="dynamicspace", kind="speed")
    out[S.DYNAMICS_PACE_GRADE_PCT] = _developer_value(row, contains="dynamicspace", kind="grade")
    for column, sources in _DIRECT:
        out[column] = _as_float(_first(row, sources))
    return out


def build_records_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Build the normalized, ordered per-second record frame from raw record dicts."""
    if not rows:
        raise ValueError("no record messages found in activity")

    frame = pd.DataFrame.from_records([_normalize_row(r) for r in rows], columns=S.RECORD_COLUMNS)

    # Timestamps -> tz-aware UTC; elapsed seconds from the first record.
    frame[S.TIMESTAMP] = pd.to_datetime(frame[S.TIMESTAMP], utc=True)
    start = frame[S.TIMESTAMP].min()
    frame[S.T_S] = (frame[S.TIMESTAMP] - start).dt.total_seconds()

    # Everything except the timestamp is float.
    numeric = [c for c in S.RECORD_COLUMNS if c != S.TIMESTAMP]
    frame[numeric] = frame[numeric].astype("float64")
    return frame


def build_laps_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Build the lap frame (interval boundaries and lap distances) from raw lap dicts."""
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        start = row.get("start_time")
        elapsed = _as_float(row.get("total_elapsed_time"))
        start_ts = pd.to_datetime(start, utc=True) if start is not None else None
        end_ts = start_ts + pd.to_timedelta(elapsed, unit="s") if (start_ts and elapsed) else None
        records.append(
            {
                S.LAP_INDEX: index,
                S.LAP_START_TIME: start_ts,
                S.LAP_END_TIME: end_ts,
                S.LAP_ELAPSED_S: elapsed,
                S.LAP_TIMER_S: _as_float(row.get("total_timer_time")),
                S.LAP_DISTANCE_M: _as_float(row.get("total_distance")),
            }
        )
    return pd.DataFrame.from_records(records, columns=S.LAP_COLUMNS)


def _median_dt_s(frame: pd.DataFrame) -> float | None:
    if len(frame) < _MIN_ROWS_FOR_DT:
        return None
    diffs = frame[S.TIMESTAMP].diff().dropna().dt.total_seconds()
    if diffs.empty:
        return None
    return float(diffs.median())


# ---- I/O ----------------------------------------------------------------------------------------


def read_fit_frames(
    path: Path,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], str | None, str | None]:
    """Read a FIT file into raw record dicts, lap dicts, and sport/sub_sport.

    For each record the first non-null value per field name is kept, so native fields take
    precedence over the developer-field duplicates some Connect IQ apps re-log.
    """
    record_rows: list[Mapping[str, Any]] = []
    lap_rows: list[Mapping[str, Any]] = []
    sport: str | None = None
    sub_sport: str | None = None

    with fitdecode.FitReader(str(path)) as reader:
        for frame in reader:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "record":
                row: dict[str, Any] = {}
                for field in frame.fields:
                    if field.name not in row or row[field.name] is None:
                        row[field.name] = field.value
                record_rows.append(row)
            elif frame.name == "lap":
                lap_rows.append({f.name: f.value for f in frame.fields})
            elif frame.name in ("sport", "session"):
                sport = frame.get_value("sport", fallback=sport)
                sub_sport = frame.get_value("sub_sport", fallback=sub_sport)

    return record_rows, lap_rows, sport, sub_sport


def load_activity(path: str | Path, *, validate: bool = True) -> Activity:
    """Parse a FIT file into a validated :class:`Activity`."""
    path = Path(path)
    record_rows, lap_rows, sport, sub_sport = read_fit_frames(path)

    records = build_records_frame(list(record_rows))
    laps = build_laps_frame(list(lap_rows))
    if validate:
        records = S.RECORDS_SCHEMA.validate(records)

    has_gps = bool(records[S.LATITUDE_DEG].notna().any())
    meta = ActivityMeta(
        source_path=path,
        sport=sport,
        sub_sport=sub_sport,
        start_time=records[S.TIMESTAMP].min(),
        n_records=len(records),
        sampling_dt_s=_median_dt_s(records),
        is_treadmill=(sub_sport == "treadmill") or not has_gps,
    )
    log.info(
        "loaded activity: %s records=%d sport=%s/%s dt=%ss treadmill=%s",
        path.name,
        meta.n_records,
        meta.sport,
        meta.sub_sport,
        meta.sampling_dt_s,
        meta.is_treadmill,
    )
    return Activity(records=records, laps=laps, meta=meta)
