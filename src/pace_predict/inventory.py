"""Summarize a directory of activity files: coverage, sampling, and field availability.

Run it to see what the dataset looks like before any modelling::

    uv run python -m pace_predict.inventory data/fit
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from pace_predict.io import Activity, load_activity
from pace_predict.io import schema as S

log = logging.getLogger(__name__)


def summarize_activity(activity: Activity) -> dict[str, object]:
    """One-row summary of an activity's coverage and field availability."""
    records = activity.records
    distance = records[S.DISTANCE_M]
    duration_s = float(records[S.T_S].max()) if len(records) else 0.0

    def _mean(column: str) -> float | None:
        series = records[column]
        return round(float(series.mean()), 1) if series.notna().any() else None

    return {
        "file": activity.meta.source_path.name,
        "sport": f"{activity.meta.sport}/{activity.meta.sub_sport}",
        "records": activity.meta.n_records,
        "dt_s": activity.meta.sampling_dt_s,
        "dur_min": round(duration_s / 60.0, 1),
        "dist_km": round(float(distance.max()) / 1000.0, 2) if distance.notna().any() else None,
        "laps": len(activity.laps),
        "has_gps": bool(records[S.LATITUDE_DEG].notna().any()),
        "cadence_spm": _mean(S.CADENCE_SPM),
        "gct_ms": _mean(S.GROUND_CONTACT_TIME_MS),
        "vo_mm": _mean(S.VERTICAL_OSCILLATION_MM),
        "dyn_pace": bool(records[S.DYNAMICS_PACE_SPEED_MPS].notna().any()),
        "treadmill": activity.meta.is_treadmill,
    }


def build_inventory(paths: list[Path]) -> pd.DataFrame:
    """Load each activity and return a summary frame (one row per file)."""
    rows: list[dict[str, object]] = []
    for path in sorted(paths):
        try:
            rows.append(summarize_activity(load_activity(path)))
        except Exception:
            log.exception("failed to load %s", path)
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory of .fit files")
    args = parser.parse_args(argv)

    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}")
        return 1

    paths = sorted({p.resolve() for p in args.directory.iterdir() if p.suffix.lower() == ".fit"})
    if not paths:
        print(f"no .fit files found in {args.directory}")
        return 1

    inventory = build_inventory(paths)
    n_failed = len(paths) - len(inventory)
    if inventory.empty:
        print(f"0 activities loaded from {args.directory} ({n_failed} file(s) failed to parse)")
        return 1

    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(inventory.to_string(index=False))

    total_min = float(inventory["dur_min"].sum())
    failed = f" | {n_failed} failed" if n_failed else ""
    print(
        f"\n{len(inventory)} activities | {total_min / 60.0:.1f} h | "
        f"{int(inventory['records'].sum())} records | "
        f"dynamics_pace baseline in {int(inventory['dyn_pace'].sum())}/{len(inventory)}{failed}"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
