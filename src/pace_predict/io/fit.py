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

Multisport files (e.g. a triathlon) carry several ``session`` messages, one per sport segment.
These are retained in ``Activity.sessions``; :func:`running_sessions` slices the running segment(s)
out as standalone activities.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitdecode
import pandas as pd

from pace_predict.io import schema as S

log = logging.getLogger(__name__)

_SEMICIRCLE_TO_DEG = 180.0 / 2**31
_MIN_ROWS_FOR_DT = 2  # need at least two timestamps to compute a sampling interval

# Direct source-field -> normalized-column copies: the first present, non-null source name wins,
# so the tuples encode source precedence (e.g. running power is the `RP_Power` developer field,
# preferred over a native cycling `power` field if both are present).
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


def _empty_sessions_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(S.SESSION_COLUMNS))


@dataclass(frozen=True)
class Activity:
    """A parsed activity: the per-second record frame, the lap frame, sessions, and metadata.

    As produced by :func:`load_activity`, ``sessions`` has one row per sport segment — a plain
    activity has a single session; a multisport file (e.g. a triathlon) has several, each with its
    own sport and time/lap range. It defaults to an empty frame when an ``Activity`` is constructed
    directly. Use :func:`running_sessions` to pull the running segment(s) out as standalone
    activities.
    """

    records: pd.DataFrame
    laps: pd.DataFrame
    meta: ActivityMeta
    sessions: pd.DataFrame = field(default_factory=_empty_sessions_frame)


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
    """Coerce a raw FIT value to float, or None if it is absent or non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any | None) -> int | None:
    """Coerce a raw FIT value to int, or None if it is absent or non-integral."""
    parsed = _as_float(value)
    if parsed is None or parsed != int(parsed):
        return None
    return int(parsed)


def _str_or_none(value: Any | None) -> str | None:
    """A trimmed string, or None if the value is absent or blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _developer_value(row: Mapping[str, Any], *, contains: str, kind: str) -> float | None:
    """Find a developer field by substring match on its name (robust to app language)."""
    for key, value in row.items():
        if value is None:
            continue
        lowered = key.lower()
        if contains in lowered and kind in lowered:
            parsed = _as_float(value)
            if parsed is not None:
                return parsed
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
    speed = _developer_value(row, contains="dynamicspace", kind="geschwindig")
    if speed is None:  # explicit: a legitimate 0.0 (at rest) must not fall through
        speed = _developer_value(row, contains="dynamicspace", kind="speed")
    out[S.DYNAMICS_PACE_SPEED_MPS] = speed
    out[S.DYNAMICS_PACE_GRADE_PCT] = _developer_value(row, contains="dynamicspace", kind="grade")
    for column, sources in _DIRECT:
        out[column] = _as_float(_first(row, sources))
    return out


def build_records_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Build the normalized, ordered per-second record frame from raw record dicts."""
    if not rows:
        raise ValueError("no record messages found in activity")

    frame = pd.DataFrame.from_records([_normalize_row(r) for r in rows], columns=S.RECORD_COLUMNS)

    # Drop records with no usable timestamp — they cannot be placed on the time axis.
    frame[S.TIMESTAMP] = pd.to_datetime(frame[S.TIMESTAMP], utc=True)
    dropped = int(frame[S.TIMESTAMP].isna().sum())
    if dropped:
        log.warning("dropping %d record(s) with no timestamp", dropped)
        frame = frame[frame[S.TIMESTAMP].notna()]
    if frame.empty:
        raise ValueError("no records with a valid timestamp")

    # Order by time so t_s is monotonic and the sampling interval is meaningful.
    frame = frame.sort_values(S.TIMESTAMP, kind="stable").reset_index(drop=True)
    frame[S.T_S] = (frame[S.TIMESTAMP] - frame[S.TIMESTAMP].min()).dt.total_seconds()

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
        end_ts = None
        if start_ts is not None and elapsed is not None:  # elapsed may legitimately be 0.0
            end_ts = start_ts + pd.to_timedelta(elapsed, unit="s")
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


def build_sessions_frame(
    rows: Sequence[Mapping[str, Any]],
    *,
    fallback_sport: str | None,
    fallback_sub_sport: str | None,
    records: pd.DataFrame,
) -> pd.DataFrame:
    """Build the session frame (one row per sport segment) from raw ``session`` message dicts.

    A session's end time is ``start_time + total_elapsed_time`` (the convention
    :func:`build_laps_frame` uses for laps). A file with no ``session`` messages yields a single
    synthetic session spanning all records, carrying the fallback sport — so ``sessions`` is never
    empty for a real activity and single-sport files behave exactly as before.
    """
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        start = row.get("start_time")
        start_ts = pd.to_datetime(start, utc=True) if start is not None else None
        elapsed = _as_float(row.get("total_elapsed_time"))
        end_ts = None
        if start_ts is not None and elapsed is not None:  # elapsed may legitimately be 0.0
            end_ts = start_ts + pd.to_timedelta(elapsed, unit="s")
        out.append(
            {
                S.SESSION_INDEX: index,
                S.SESSION_SPORT: _str_or_none(row.get("sport")),
                S.SESSION_SUB_SPORT: _str_or_none(row.get("sub_sport")),
                S.SESSION_START_TIME: start_ts,
                S.SESSION_END_TIME: end_ts,
                S.SESSION_ELAPSED_S: elapsed,
                S.SESSION_TIMER_S: _as_float(row.get("total_timer_time")),
                S.SESSION_DISTANCE_M: _as_float(row.get("total_distance")),
                S.SESSION_FIRST_LAP_INDEX: _as_int(row.get("first_lap_index")),
                S.SESSION_NUM_LAPS: _as_int(row.get("num_laps")),
            }
        )
    if not out:
        out.append(_synthetic_session(fallback_sport, fallback_sub_sport, records))
    return pd.DataFrame.from_records(out, columns=S.SESSION_COLUMNS)


def _synthetic_session(
    sport: str | None, sub_sport: str | None, records: pd.DataFrame
) -> dict[str, Any]:
    """A single session spanning every record, for files that carry no ``session`` message."""
    timestamps = records[S.TIMESTAMP]
    distance = records[S.DISTANCE_M]
    return {
        S.SESSION_INDEX: 0,
        S.SESSION_SPORT: _str_or_none(sport),
        S.SESSION_SUB_SPORT: _str_or_none(sub_sport),
        S.SESSION_START_TIME: timestamps.min() if len(records) else None,
        # None end => the split includes every record from the start onward (no upper bound).
        S.SESSION_END_TIME: None,
        S.SESSION_ELAPSED_S: None,
        S.SESSION_TIMER_S: None,
        S.SESSION_DISTANCE_M: float(distance.max()) if distance.notna().any() else None,
        S.SESSION_FIRST_LAP_INDEX: 0,
        S.SESSION_NUM_LAPS: None,
    }


# ---- session splitting --------------------------------------------------------------------------


def running_sessions(activity: Activity) -> list[Activity]:
    """Extract each running session as a standalone :class:`Activity`.

    For every session with ``sport == "running"`` the records are sliced to that session's time
    window, the laps to its lap range, and ``t_s`` is rebased to zero — so each returned activity
    drops straight into the ground-truth pipeline. This is how the running leg of a multisport file
    (e.g. the 5 km run of a triathlon) becomes usable instead of the whole file being one blob.
    Sessions with no records in their window (a transition, say) are skipped.
    """
    sessions = activity.sessions
    windows = _session_windows(sessions)
    out: list[Activity] = []
    for position, (_, row) in enumerate(sessions.iterrows()):
        if row[S.SESSION_SPORT] != "running":
            continue
        sub = _slice_session(activity, position, row, *windows[position])
        if sub is not None:
            out.append(sub)
    return out


def _session_windows(
    sessions: pd.DataFrame,
) -> list[tuple[pd.Timestamp | None, pd.Timestamp | None]]:
    """Half-open ``[start, end)`` record window per session, in the frame's row order.

    Each session runs until the next session's start **in time**, the last to the end of the
    records — the same way ``estimate.lap_segments`` tiles laps. Ordering by start time (rather than
    row order) means every record lands in exactly one session even if the ``session`` messages are
    not stored chronologically; no boundary sample is double-counted or dropped, and the sub-second
    overlap that elapsed-time rounding can leave between contiguous sessions is absorbed. A ``None``
    bound means "unbounded on that side".
    """
    starts = [s if not pd.isna(s) else None for s in sessions[S.SESSION_START_TIME]]
    # Rank rows by start time (rows with no start sort last) so each window ends at the next start
    # in time; results stay in the caller's row order so they index alongside ``sessions``.
    far_future = pd.Timestamp.max.tz_localize("UTC")
    sort_keys: list[pd.Timestamp] = [far_future if s is None else s for s in starts]
    order = sorted(range(len(starts)), key=lambda i: sort_keys[i])
    end_for_row: dict[int, pd.Timestamp | None] = {}
    for rank, row_index in enumerate(order):
        next_row = order[rank + 1] if rank + 1 < len(order) else None
        end_for_row[row_index] = starts[next_row] if next_row is not None else None
    return [(starts[i], end_for_row[i]) for i in range(len(starts))]


def _slice_session(
    activity: Activity,
    position: int,
    row: Mapping[str, Any],
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
) -> Activity | None:
    """One session as a standalone activity, or None if no records fall in its window."""
    records = activity.records
    timestamps = records[S.TIMESTAMP]
    mask = pd.Series(True, index=records.index)
    if start is not None:
        mask &= timestamps >= start
    if end is not None:
        mask &= timestamps < end

    sliced = records[mask]
    if sliced.empty:
        log.warning(
            "session %s (%s) has no records in its window; skipping",
            row[S.SESSION_INDEX],
            row[S.SESSION_SPORT],
        )
        return None

    sliced = _rebase_t_s(sliced)
    laps = _slice_laps(activity.laps, row, start, end)
    sessions = activity.sessions.iloc[[position]].reset_index(drop=True)
    return Activity(
        records=sliced, laps=laps, meta=_session_meta(activity, row, sliced), sessions=sessions
    )


def _rebase_t_s(frame: pd.DataFrame) -> pd.DataFrame:
    """Copy of a record slice with ``t_s`` re-zeroed to the slice's own start."""
    frame = frame.sort_values(S.TIMESTAMP, kind="stable").reset_index(drop=True)
    frame[S.T_S] = (frame[S.TIMESTAMP] - frame[S.TIMESTAMP].min()).dt.total_seconds()
    return frame


def _slice_laps(
    laps: pd.DataFrame,
    row: Mapping[str, Any],
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
) -> pd.DataFrame:
    """Laps belonging to a session: by the session's lap-index range, or by time window if absent.

    ``lap_index`` in the lap frame is the FIT global lap index (laps are enumerated in file order),
    so the session's ``first_lap_index``/``num_laps`` select its laps exactly.
    """
    if laps.empty:
        return laps

    first_lap = row[S.SESSION_FIRST_LAP_INDEX]
    num_laps = row[S.SESSION_NUM_LAPS]
    if not pd.isna(first_lap) and not pd.isna(num_laps):
        low = int(first_lap)
        high = low + int(num_laps)
        selected = laps[(laps[S.LAP_INDEX] >= low) & (laps[S.LAP_INDEX] < high)]
    else:
        starts = laps[S.LAP_START_TIME]
        mask = pd.Series(True, index=laps.index)
        if start is not None:
            mask &= starts >= start
        if end is not None:
            mask &= starts < end
        selected = laps[mask]
    return selected.reset_index(drop=True)


def _session_meta(activity: Activity, row: Mapping[str, Any], sliced: pd.DataFrame) -> ActivityMeta:
    """Metadata for a single session sliced out of a larger activity."""
    sub_sport = _str_or_none(row[S.SESSION_SUB_SPORT])
    has_gps = bool(sliced[S.LATITUDE_DEG].notna().any())
    return ActivityMeta(
        source_path=activity.meta.source_path,
        sport=_str_or_none(row[S.SESSION_SPORT]),
        sub_sport=sub_sport,
        start_time=sliced[S.TIMESTAMP].min(),
        n_records=len(sliced),
        sampling_dt_s=_median_dt_s(sliced),
        is_treadmill=(sub_sport == "treadmill") or not has_gps,
    )


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
) -> tuple[
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    str | None,
    str | None,
]:
    """Read a FIT file into raw record, lap, and session dicts, plus a fallback sport/sub_sport.

    For each record the first non-null value per field name is kept, so native fields take
    precedence over the developer-field duplicates some Connect IQ apps re-log. Every ``session``
    message is retained (a multisport file has one per sport). The scalar sport/sub_sport is the
    last-seen value across ``sport``/``session`` messages, used only as a fallback for files that
    carry no ``session`` message.
    """
    record_rows: list[Mapping[str, Any]] = []
    lap_rows: list[Mapping[str, Any]] = []
    session_rows: list[Mapping[str, Any]] = []
    sport: str | None = None
    sub_sport: str | None = None

    with fitdecode.FitReader(str(path)) as reader:
        for frame in reader:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "record":
                row: dict[str, Any] = {}
                for record_field in frame.fields:
                    if record_field.name not in row or row[record_field.name] is None:
                        row[record_field.name] = record_field.value
                record_rows.append(row)
            elif frame.name == "lap":
                lap_rows.append({f.name: f.value for f in frame.fields})
            elif frame.name in ("sport", "session"):
                sport = frame.get_value("sport", fallback=sport)
                sub_sport = frame.get_value("sub_sport", fallback=sub_sport)
                if frame.name == "session":
                    session_rows.append({f.name: f.value for f in frame.fields})

    return record_rows, lap_rows, session_rows, sport, sub_sport


def load_activity(path: str | Path, *, validate: bool = True) -> Activity:
    """Parse a FIT file into a validated :class:`Activity`."""
    path = Path(path)
    record_rows, lap_rows, session_rows, sport, sub_sport = read_fit_frames(path)

    records = build_records_frame(list(record_rows))
    laps = build_laps_frame(list(lap_rows))
    sessions = build_sessions_frame(
        list(session_rows), fallback_sport=sport, fallback_sub_sport=sub_sport, records=records
    )
    if validate:
        records = S.RECORDS_SCHEMA.validate(records)
        if not laps.empty:
            laps = S.LAPS_SCHEMA.validate(laps)
        if not sessions.empty:
            sessions = S.SESSIONS_SCHEMA.validate(sessions)

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
    return Activity(records=records, laps=laps, meta=meta, sessions=sessions)
