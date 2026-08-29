# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
# ---

# %% [markdown]
# # Ground-truth pace — smoothing & estimation
#
# Reconstruct a clean per-second pace for each run and sanity-check it, using the `pace_predict`
# library. Reads FIT files from `data/` (gitignored), so run it locally — outputs are stripped
# before commit, and no GPS/health data is ever committed.
#
# Open the cells (`# %%`) in Jupyter or VS Code, or convert with
# `jupytext --to notebook notebooks/groundtruth.py`. Prediction-quality plots (model vs baseline,
# interval settling time) are added once the model exists.

# %%
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from pace_predict.groundtruth import ground_truth_speed
from pace_predict.inventory import build_inventory
from pace_predict.io import load_activity
from pace_predict.io import schema as S


def _repo_root() -> Path:
    """Repo root (the directory with pyproject.toml), so the notebook works from any kernel cwd."""
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return cwd


DATA = _repo_root() / "data" / "fit"


def to_pace(speed_mps):
    """m/s -> min/km, blanking near-stops so pauses don't dominate the axis."""
    v = np.asarray(speed_mps, float)
    v = np.where(v > 0.6, v, np.nan)
    return 1000.0 / v / 60.0


# %% [markdown]
# ## Dataset inventory
# One row per activity. `track` flags runs on a 400 m oval (GNSS overshoots — held out of
# calibration). `dyn_pace` marks the embedded `dynamicsPace` field, our baseline to beat.

# %%
inventory = build_inventory(sorted(DATA.glob("*.fit")))
if inventory.empty:
    raise RuntimeError(f"No activities found under {DATA} — add Garmin .fit files (see data/README.md).")
inventory[["file", "sport", "dur_min", "dist_km", "laps", "track", "cadence_spm", "gct_ms", "dyn_pace"]]

# %% [markdown]
# Pick example runs from the inventory — a long non-track run for the smoothing demo and the
# most-lapped run for the interval demo. Override `RUN` / `INTERVAL_RUN` with any file you like.

# %%
_road = inventory[inventory["sport"].str.startswith("running") & ~inventory["track"]]
RUN = (_road if not _road.empty else inventory).sort_values("dist_km", ascending=False)["file"].iloc[0]
INTERVAL_RUN = inventory.sort_values("laps", ascending=False)["file"].iloc[0]
print(f"smoothing demo: {RUN}  |  interval demo: {INTERVAL_RUN}")

# %% [markdown]
# ## Smoothing: raw vs ground truth
# The ground truth median-cleans Garmin's `enhanced_speed` (edge-preserving, spike-robust) and
# calibrates to the >=1 km laps. On a clean run it should be a smooth pace line, far tidier than raw.

# %%
activity = load_activity(DATA / RUN)
records = activity.records
minutes = records[S.T_S].to_numpy() / 60.0
ground_truth = ground_truth_speed(activity)

fig, ax = plt.subplots(figsize=(13, 5))
ax.plot(minutes, to_pace(records[S.SPEED_MPS].to_numpy()), color="0.75", lw=0.7, label="raw enhanced_speed")
ax.plot(minutes, to_pace(records[S.DYNAMICS_PACE_SPEED_MPS].to_numpy()), color="tab:orange", lw=0.9, alpha=0.8, label="dynamicsPace field")
ax.plot(minutes, to_pace(ground_truth), color="tab:blue", lw=1.7, label="ground truth (smoothed)")
ax.set_ylim(3.5, 6.5)
ax.invert_yaxis()
ax.set_xlabel("time (min)")
ax.set_ylabel("pace (min/km)")
ax.set_title(f"{RUN} — ground-truth pace vs raw vs dynamicsPace")
ax.legend(loc="upper right")
plt.show()

# %% [markdown]
# ## Interval reaction vs cadence
# Cadence steps instantly at each rep (biomechanics, GNSS-independent). A good ground truth turns
# at the same moments — it reacts, it doesn't lag.

# %%
iv = load_activity(DATA / INTERVAL_RUN)
iv_minutes = iv.records[S.T_S].to_numpy() / 60.0
iv_pace = to_pace(ground_truth_speed(iv))
cadence = iv.records[S.CADENCE_SPM].to_numpy()
lap_minutes = [
    (end - iv.records[S.TIMESTAMP].min()).total_seconds() / 60.0
    for end in iv.laps[S.LAP_END_TIME]
    if not pd.isna(end)
]

fig, ax = plt.subplots(figsize=(13, 5))
ax.plot(iv_minutes, iv_pace, color="tab:blue", lw=1.6, label="ground-truth pace")
for lap_m in lap_minutes:
    ax.axvline(lap_m, color="0.9", lw=0.5, zorder=0)
ax.set_ylim(3.0, 6.0)
ax.invert_yaxis()
ax.set_xlabel("time (min)")
ax.set_ylabel("pace (min/km)")
ax.set_title(f"{INTERVAL_RUN} — pace (blue) vs cadence (green); grey = laps")
cadence_ax = ax.twinx()
cadence_ax.plot(iv_minutes, cadence, color="tab:green", lw=0.8, alpha=0.6)
cadence_ax.set_ylabel("cadence (spm)", color="tab:green")
plt.show()

# %% [markdown]
# ## Per-lap validation
# Over >=1 km, GNSS pace is trustworthy, so the exact lap pace (distance/time) and `enhanced_speed`
# agree to a few s/km. Short reps diverge (GNSS error) — and cadence/GCT still separate work from
# recovery, which is what the model will exploit.

# %%
def per_lap_table(act) -> pd.DataFrame:
    r = act.records
    t_s = r[S.T_S].to_numpy()
    start0 = r[S.TIMESTAMP].min()
    speed = r[S.SPEED_MPS].to_numpy()
    cadence = r[S.CADENCE_SPM].to_numpy()
    gct = r[S.GROUND_CONTACT_TIME_MS].to_numpy()
    ends = [
        (e - start0).total_seconds() if not pd.isna(e) else float(t_s[-1])
        for e in act.laps[S.LAP_END_TIME]
    ]
    starts = [0.0, *ends[:-1]]
    rows = []
    for i, (a, b) in enumerate(zip(starts, ends, strict=True)):
        mask = (t_s >= a) & (t_s < b)
        distance = act.laps[S.LAP_DISTANCE_M].iloc[i]
        duration = act.laps[S.LAP_ELAPSED_S].iloc[i]
        if mask.sum() == 0 or not distance or not duration:
            continue
        rows.append(
            {
                "lap": i,
                "dist_m": round(distance),
                "exact_pace": round(1000.0 / (distance / duration) / 60.0, 2),
                "es_pace": round(float(np.nanmean(to_pace(speed[mask]))), 2),
                "cadence": round(float(np.nanmean(cadence[mask]))),
                "gct_ms": round(float(np.nanmean(gct[mask]))),
                "trusted_>=1km": distance >= 1000,
            }
        )
    return pd.DataFrame(rows)


per_lap_table(iv)
