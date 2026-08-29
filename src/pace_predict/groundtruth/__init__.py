"""Ground-truth pace: reconstruct a clean per-second speed offline and calibrate it to distance."""

from pace_predict.groundtruth.calibrate import (
    calibrate_ground_truth,
    calibrate_per_lap,
    calibrate_to_distance,
    integrate_speed,
)
from pace_predict.groundtruth.estimate import ground_truth_speed, lap_end_indices, median_dt_s
from pace_predict.groundtruth.quality import (
    cadence_change_points,
    is_track_like,
    track_concentration,
)
from pace_predict.groundtruth.smoothing import (
    kalman_rts_speed,
    latlon_to_enu,
    robust_speed,
    savgol_speed,
)

__all__ = [
    "cadence_change_points",
    "calibrate_ground_truth",
    "calibrate_per_lap",
    "calibrate_to_distance",
    "ground_truth_speed",
    "integrate_speed",
    "is_track_like",
    "kalman_rts_speed",
    "lap_end_indices",
    "latlon_to_enu",
    "median_dt_s",
    "robust_speed",
    "savgol_speed",
    "track_concentration",
]
