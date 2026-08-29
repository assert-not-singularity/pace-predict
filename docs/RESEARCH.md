# Prior art & research notes

Curated findings that inform the design. Each load-bearing claim links a source; confidence is
flagged where it matters. See `docs/ROADMAP.md` for how these feed the plan.

## Why Garmin's running pace lags

Garmin's `SPORT_RUNNING` current pace is **deliberately low-pass filtered** to produce a stable
number, while `SPORT_CYCLING` is unfiltered. Developer testing measures the running filter as
roughly a **1-minute rolling average**, so a pace change takes 60+ s to register — and a paired
foot pod bypasses it (responds immediately). A firmware change (~FR955 v14.13) widened the window,
which users call "useless for short intervals." This is the exact lag/re-convergence the project
targets.

- `Activity.Info.currentSpeed` (what a data field reads) is in m/s and carries this filtering.
- Sources: [currentSpeed filtering (official statement + measurement)](https://forums.garmin.com/developer/connect-iq/f/discussion/1421/getactivityinfo-current-speed-filtering/8950),
  [FR955 one-minute filter](https://forums.garmin.com/sports-fitness/running-multisport/f/forerunner-955-series/323411/running-pace-filter-time-now-set-to-a-minute),
  [Activity.Info API](https://developer.garmin.com/connect-iq/api-docs/Toybox/Activity/Info.html).

## Contact-time → speed is the validated predictor

Per-runner power law `speed = c · GCT^d` (c ≈ 0.59, d ≈ −0.63; GCT in ms, speed in m/s) fits
outdoor overground running with **individual r² > 0.96** (mean 0.98) and **~2.5% median error**
over 4 km — versus **1.6% for GPS**. Critically, **at turns/starts/finishes the contact-time model
(2.7%) beat GPS (5.0%)** — dynamics win exactly at interval boundaries. The fit is **per-runner and
needs calibration**; coefficients differ between people and even day to day. Speed range tested
2.1–4.8 m/s. (Confidence: high — peer-reviewed.)

- Physics: `speed = cadence × step_length`; at fixed speed, higher cadence → shorter GCT. Cadence
  alone is a weak proxy (step length varies); **GCT + cadence + vertical oscillation** together
  span the degrees of freedom.
- Wrist/waist accelerometer speed models plateau at ~11–12% MAPE; shoe/pod-based do far better —
  another reason to use pod-based GCT/VO rather than wrist motion.
- Sources: [Running speed from foot contact time (PMC5029865)](https://pmc.ncbi.nlm.nih.gov/articles/PMC5029865/),
  [shoe-IMU speed models](https://www.frontiersin.org/journals/sports-and-active-living/articles/10.3389/fspor.2021.585809/full).

## Existing Connect IQ pace fields (and the gap)

No existing CIQ data field predicts pace **primarily from running dynamics** — the ecosystem either
smooths GPS or reads a foot pod. That gap is the differentiator. The most instructive prior field,
**Accurate Pace** (open source), **inverts** the usual approach: it starts from a smooth *predicted*
pace and uses GPS only to *correct* it. Reusable constants from its source: a 5 s recent-pace
window, 20 s history, a weighted blend of the 40/50/60th pace percentiles, and error redistributed
back onto recent coordinates.

- A simple **EMA** is the "example smoothing algorithm" worth reusing as a tunable output stage:
  `EMA += α·(x − EMA)` (α≈0.1 smooth … 0.5 responsive), O(1) memory.
- Sources: [Accurate Pace repo](https://github.com/matthiasmullie/connect-iq-datafield-accurate-pace),
  [EMA smoothing thread](https://forums.garmin.com/developer/connect-iq/f/discussion/401952/how-to-smooth-sharply-fluctuating-data-for-fitcontributor-graph-legibility).

## Track overshoot (410–420 m per 400 m lap)

On a 400 m track, GPS/track mode commonly reads 410–420 m/lap (pace reads fast) from curve
random-walk and accelerometer/arm-swing miscounting, even with Track Run calibration. Because
cadence/GCT are stable and lap-geometry-independent, a dynamics-based pace should be immune to this
— a strong demo scenario, and the true lap distance (400 m in lane 1) is a near-perfect
ground-truth source. Sources:
[FR965 track distance off](https://forums.garmin.com/sports-fitness/running-multisport/f/forerunner-965/406157/track-run-mode-distance-way-off/1916782),
[Track Run calibration](https://forums.garmin.com/sports-fitness/running-multisport/f/forerunner-745/264626/track-run-calibration).

## Reading running dynamics live in a data field (Fenix 6 Pro)

Use `Toybox.AntPlus.RunningDynamics` (API ≥ 2.4.0; Fenix 6 qualifies). `getRunningDynamics()`
returns `cadence` (strides/min), `stepLength` (mm), `groundContactTime` (ms), `groundContactBalance`
(%), `verticalOscillation` (mm), `verticalRatio` (%), `stanceTime`, `stepCount`, `walkingFlag`.

- **Hardware requirement:** GCT/VO/VR/GCB require a chest strap or pod (HRM-Run/Tri/**Pro** or RD
  Pod); the Fenix 6 wrist provides cadence only. The project already assumes an HRM-Pro.
- **Caveats:** the object and data are `null` until ANT+ data arrives (null-check both); running
  dynamics populate only while running; the **simulator does not emulate ANT+**, so this must be
  tested on real hardware. Note the strides/min vs steps/min ×2 nuance between
  `RunningDynamicsData.cadence` and `Info.currentCadence` — verify on device.
- **Memory & slots:** data-field memory is **≈124.7 KB on the Fenix 6 Pro** (128 KB minus ~4 KB
  overhead) but only **≈28.7 KB on the base Fenix 6** — design the shipped model for the tight
  tier. Only **2 CIQ data fields** run per activity profile. Authoritative per-device numbers live
  in each device's `compiler.json` in the CIQ SDK.
- Sources: [RunningDynamicsData API](https://developer.garmin.com/connect-iq/api-docs/Toybox/AntPlus/RunningDynamicsData.html),
  [using RunningDynamics in a datafield](https://forums.garmin.com/developer/connect-iq/f/discussion/237087/using-runningdynamicsdata-groundcontactbalance-and-other-metrics-in-a-datafield),
  [data-field memory limits (131072)](https://forums.garmin.com/developer/connect-iq/f/discussion/258441/maximum-memory-for-data-field-per-device).

## Peer-reviewed anchors (from the modelling literature)

- Hébert-Losier et al. 2016, PLOS One — contact-time power law (the primary baseline & target).
- Apte et al. 2021, Frontiers — personalized IMU speed models beat generic.
- Van Hooren et al. 2020 — treadmill GCT ~12% shorter than overground (exclude treadmill runs).
- Rauch–Tung–Striebel smoother — standard offline INS/GPS position+velocity post-processing.
