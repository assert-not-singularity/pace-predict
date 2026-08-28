# data/

Drop Garmin `.fit` activity files here. **This directory is gitignored** — everything under it
except this README is excluded from version control and never committed.

FIT files contain home GPS coordinates and health data. They MUST NOT be committed, uploaded to any
external service, or pasted into logs or PR bodies. Tests use synthetic fixtures instead.

## Expected layout

```
data/
├─ fit/        # raw .fit files, one per activity
└─ …           # derived/cached artifacts (also gitignored)
```

## Recording settings for best results

- **1-second recording** (not "smart recording") — irregular sampling breaks per-second smoothing
  and the interval-settling evaluation.
- A running-dynamics source (HRM-Pro / RD Pod / compatible watch) so cadence, ground-contact time,
  vertical oscillation, and vertical ratio are recorded.
