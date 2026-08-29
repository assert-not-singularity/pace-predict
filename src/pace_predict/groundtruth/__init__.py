"""Ground-truth pace: reconstruct a clean per-second speed offline and calibrate it to distance."""

from pace_predict.groundtruth.calibrate import (
    LapSegment,
    calibrate_ground_truth,
    calibrate_per_lap,
    calibrate_to_distance,
    integrate_speed,
)
from pace_predict.groundtruth.estimate import (
    SegmentParams,
    ground_truth_speed,
    lap_segments,
    median_dt_s,
    segment_pace,
)
from pace_predict.groundtruth.quality import (
    ACCEL,
    DECEL,
    STEADY,
    PhaseParams,
    PhaseSegment,
    is_track_like,
    phase_labels,
    phase_segments,
    track_concentration,
)
from pace_predict.groundtruth.smoothing import (
    guard_dropouts,
    latlon_to_enu,
    median_clean,
    robust_speed,
)

__all__ = [
    "ACCEL",
    "DECEL",
    "STEADY",
    "LapSegment",
    "PhaseParams",
    "PhaseSegment",
    "SegmentParams",
    "calibrate_ground_truth",
    "calibrate_per_lap",
    "calibrate_to_distance",
    "ground_truth_speed",
    "guard_dropouts",
    "integrate_speed",
    "is_track_like",
    "lap_segments",
    "latlon_to_enu",
    "median_clean",
    "median_dt_s",
    "phase_labels",
    "phase_segments",
    "robust_speed",
    "segment_pace",
    "track_concentration",
]
