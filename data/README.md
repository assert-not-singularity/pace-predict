# data/

Drop Garmin `.fit` activity files here. **This directory is gitignored** — everything under it
except this README is excluded from version control and never committed.

FIT files contain home GPS coordinates and health data. They MUST NOT be committed, uploaded to any
external service, or pasted into logs or PR bodies. Tests use synthetic fixtures instead.

## Accepted formats

- **`.fit` (preferred)** — carries Garmin **running dynamics** (ground-contact time, vertical
  oscillation, vertical ratio, stance time, step length) needed for the biomechanics model. Get
  them via Garmin Connect → activity → *Export Original*, or from the watch's `GARMIN/Activity`
  folder.
- **`.tcx`** — Training Center XML. Has time, position, distance, altitude, heart rate, speed,
  cadence, and lap markers at 1 s, but **not** running dynamics (Garmin drops them on TCX export).
  Usable for the ground-truth smoothing track and cadence, **not** for the GCT/VO model.

## Recording settings for best results

- **1-second recording** (not "smart recording") — irregular sampling breaks per-second smoothing
  and the interval-settling evaluation.
- A running-dynamics source (HRM-Pro / RD Pod / compatible watch) so cadence, ground-contact time,
  vertical oscillation, and vertical ratio are recorded — and export as `.fit` to keep them.
