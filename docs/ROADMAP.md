# pace-predict — implementation plan

## Context

When running with a Garmin watch, the displayed **current pace** is derived from GNSS and is
noisy — it lags badly at interval starts (it re-converges from the interval's first GPS fixes) and
takes several hundred metres to settle under tree cover. A Connect IQ datafield that tries to fix
this exists but works poorly.

**Hypothesis:** for a given runner in a given training state, on flat terrain, **running dynamics
predict pace within a few seconds**. Speed has a physical backbone — `speed ≡ stride_length ×
cadence` — and the GPS-independent biomechanics (cadence, ground-contact time, vertical
oscillation, vertical ratio) track speed strongly and respond instantly to a pace change, unlike
GPS.

**Goal of this repo:** train a personal model that estimates pace from running dynamics, evaluated
specifically on *how fast it settles at interval starts vs Garmin's GPS pace*. The model is later
exported to a custom Connect IQ datafield (separate project). This repo is the offline
data → ground-truth → features → model → evaluation pipeline, built by an **autonomous
agent-driven PR workflow**.

### Decisions locked with the user
- **Merge policy:** auto-merge on green (CI + both reviewers pass). No per-PR human gate after
  this plan is approved. → strong CI gates + branch protection are load-bearing.
- **Review:** GitHub **Copilot** review **+** an independent **Claude adversarial** review agent on
  every PR (cross-LLM). Copilot availability to be verified in Phase 0.
- **Model inputs:** **biomechanics-first + ablation** — primary model uses only GPS-independent
  signals; a stride-length-inclusive model is fit only as an upper bound to quantify leakage.
- **OSM map-matching:** **deferred** to a later track (Phase 7), not in v1.
- **Repo visibility:** **public** — code and docs only; no personal data is ever committed (see the
  privacy note below), so public hosting is safe.

### Hard constraint — the model must run in a Fenix 6 Pro data field
This is a **primary design constraint on model selection, not an afterthought.** A Connect IQ data
field runs in a slow interpreted Monkey C VM, has **no ML runtime**, and shares a tight memory
budget with the app (Fenix 5 was 28 KB per data field; the Fenix 6 got a modest bump via the
System 5 update — the exact cap lives in the SDK device XML and we read it at build time). So:
- **Shipped model = a handful of coefficients evaluated with O(1) work per tick** — a few
  multiply-adds and at most one `pow`/`exp`. No per-tick allocation, no trees to walk, no matrices.
- **Deployable families only:** the GCT power law, a small regularized **linear / low-degree
  polynomial**, a tiny **GAM** (a few 1-D lookup terms), or at most a **tiny MLP** (≤2 layers,
  single-digit neurons). Coefficients + normalization stats ≤ ~1–2 KB.
- **LightGBM / GPR / SVR are offline-only reference ceilings** — used to measure how much accuracy
  a heavy model would add, **never shipped**. If the gap to the best tiny model is small (the
  literature suggests it will be), we ship the tiny model with confidence.

### Key technical corrections baked in
- **Stride-length leakage.** Garmin's running `stride/step length` is GPS-derived (distance ÷
  steps), so it carries the same GNSS error and lags at interval starts. It is excluded from the
  deployable model and used only as a reference/ablation signal.
- **Validation split.** A random 75/25 split leaks autocorrelated within-run neighbours into test.
  We use **leave-run-out (GroupKFold by activity)** + a **temporal holdout** (older→train,
  newer→test) to catch fitness drift. The 75/25 idea becomes an activity-grouped 75/25.
- **The metric that matters** is **settling time at interval starts** — FIT `lap`/`event` markers
  give exact interval boundaries, so we measure convergence speed of model-pace vs GPS-pace
  directly, not just aggregate MAE.
- **Ground truth caution.** Garmin FIT `distance` is already smoothed and undercounts true path by
  up to ~5%; per-second speed is noisy. We reconstruct clean per-second ground-truth speed offline
  with a bidirectional smoother and **calibrate it to the known lap/total distances**.

---

## Tech stack & conventions

- **Python 3.13+**, **`uv`** for everything (never bare `pip`), per the scaffolded `python` rule.
- The **preflight** starter kit is **already stamped into this repo** (all 10 components:
  `working-style`, rules `python`/`prose-and-docs`/`scalable-architecture`, skills
  `git-conventions`/`subagent-orchestration`, personas
  `lean-implementer`/`web-researcher`/`janitor`/`technical-writer`, plus `CLAUDE.md` and
  `.preflight-base/` snapshots). These personas are the agents that execute the PRs. Note:
  `scalable-architecture` is installed in full — we apply it **pragmatically** for a research
  pipeline (separate pure logic from I/O; validate DataFrames at boundaries with pandera) rather
  than forcing full ports-and-adapters/DI everywhere.
- **Libraries** (pin `>=latest` at add time): `fitdecode` (FIT parsing), `numpy`, `pandas`,
  `scipy` (Savitzky-Golay), `filterpy` (Kalman + RTS smoother), `pandera` (DataFrame schemas),
  `scikit-learn`, `lightgbm`, `matplotlib`. Dev: `ruff`, `ty` (or `mypy`), `pytest`,
  `pytest-cov`. Deferred track: `osmnx` + a map-matching lib (`leuven.mapmatching` / `fmm`).
- **Package layout:**
  ```
  pace-predict/
  ├─ pyproject.toml                # uv, py3.13, deps, ruff/pytest config
  ├─ .gitignore                    # data/, *.fit, *.FIT, models/*.pkl, .venv, __pycache__, .env
  ├─ CLAUDE.md                     # thin index (preflight)
  ├─ .github/workflows/ci.yml      # lint + typecheck + test gate (the auto-merge gate)
  ├─ .claude/                      # scaffolded standards/rules/skills/agents
  ├─ src/pace_predict/
  │  ├─ io/fit.py                  # FIT → tidy per-second DataFrame (+ laps/events)
  │  ├─ groundtruth/smoothing.py   # Savitzky-Golay / Kalman+RTS estimators
  │  ├─ groundtruth/calibrate.py   # scale smoothed speed to known lap distances
  │  ├─ features/build.py          # biomechanics + rolling/lag/delta transient features
  │  ├─ features/schema.py         # pandera schemas for the feature/target frame
  │  ├─ models/baseline.py         # physics + linear/poly/GAM
  │  ├─ models/train.py            # LightGBM + model selection; export coefficients
  │  ├─ eval/split.py              # GroupKFold-by-activity + temporal holdout
  │  ├─ eval/metrics.py            # MAE/RMSE (s/km, m/s) + interval settling-time metric
  │  └─ eval/report.py             # plots + results/model-card markdown
  ├─ configs/                      # yaml: thresholds, feature sets, model params
  ├─ data/                         # GITIGNORED — FIT files land here; committed README describes layout
  ├─ tests/                        # unit tests; tiny synthetic FIT fixtures (no real GPS)
  └─ docs/ROADMAP.md               # this plan, committed for agents/future sessions
  ```
- **Privacy (important):** FIT files contain home GPS coordinates and health data. `data/` is
  gitignored; real FIT files are **never** committed, never uploaded to any external service, and
  never pasted into logs/PR bodies. Test fixtures are synthetic. No secrets in code — `gh` uses the
  existing keyring auth. The repo is public, so the never-commit-FIT rule matters doubly.

---

## The autonomous agentic PR workflow (the meta-process)

This is how every phase below is executed. Once you approve this plan, agents run this loop per
scoped PR **without further per-PR approval** (auto-merge on green):

1. **Branch** off up-to-date `main`: `feat/<phase>-<slug>` (git-conventions skill).
2. **Implement** — spawn a **`lean-implementer`** subagent scoped to that one PR's files, with the
   relevant standards. It edits + runs `uv run` checks/tests locally, leaves the tree for commit.
3. **Open PR** via `gh pr create` (base `main`), body = current-behaviour + the change (no
   backstory in the permanent message), `Co-Authored-By` trailer for the model.
4. **Request reviews (cross-LLM):**
   - **Copilot:** `gh pr edit <n> --add-reviewer @copilot` (REST fallback: request
     `copilot-pull-request-reviewer[bot]`). Consider the `gh-copilot-review` extension for its
     wait-for-completion support.
   - **Claude adversarial:** spawn a separate review agent (a general agent or a `code-review`
     pass) prompted to *refute* the change — correctness, leakage, test honesty, standards drift.
5. **Wait for feedback** — poll the PR (`gh pr view <n> --json reviews,comments,statusCheckRollup`)
   until Copilot posts and CI finishes.
6. **Triage & address** — a Claude agent reads both reviews, fixes real issues in new commits, and
   replies to/​resolves comments. Re-request review if it pushed changes.
7. **Merge gate (auto-merge on green):** enable `gh pr merge <n> --auto --squash`. It lands only
   when **branch protection** is satisfied: CI green **+** Copilot review resolved **+** Claude
   adversarial sign-off. If red, the loop returns to step 6; it never force-merges.

**Guardrails that make auto-merge safe:** `main` is protected (require PR, require the CI check,
no direct pushes, require conversations resolved); CI must pass `ruff` + typecheck + `pytest`; each
PR is small and single-purpose; the user can intervene or pause at any time. **Repo creation and
enabling branch protection are the last outward actions I confirm before autonomy starts** — they
happen in Phase 0 with your go-ahead.

---

## Phases (each = one or a few PRs)

### Phase 0 — Foundation & autonomy setup  *(gated: creates the GitHub repo)*
- Preflight scaffold is **already installed** (agents/rules/skills/`CLAUDE.md` present). Remaining:
  fill the `CLAUDE.md` one-line purpose placeholder. `git init`, `main` branch, initial commit.
  `uv init` + `pyproject.toml` (py3.13, deps, ruff/pytest). `.gitignore` with `data/` + FIT globs.
  `.github/workflows/ci.yml` running `uv run ruff check`, typecheck, `uv run pytest`. Commit
  `docs/ROADMAP.md` (this plan) + `data/README.md`.
- **Verify Copilot code review is enabled** on `assert-not-singularity` (probe `gh` reviewer
  list / a scratch PR). If unavailable, fall back to Claude-only adversarial review and tell you.
- `gh repo create pace-predict --private --source . --remote origin`, push `main`, then apply
  **branch protection** (require CI + reviews, no direct push). This is the switch that turns on
  autonomy.
- *Exit criteria:* green CI on a trivial PR that exercises the full loop (branch → PR → Copilot +
  Claude review → auto-merge). This proves the workflow before real code.

### Phase 1 — FIT ingestion & data inventory
- `io/fit.py`: parse `record` messages → per-second tidy DataFrame (timestamp, lat/long from
  semicircles→deg, `distance`, `enhanced_speed`, `enhanced_altitude`, `cadence` +
  `fractional_cadence`, `vertical_oscillation`, `stance_time` (GCT), `stance_time_balance`,
  `vertical_ratio`, `step_length` if present, `heart_rate`); parse `lap`/`event`/`session` for
  interval boundaries. Handle developer fields, unit normalization, and **detect smart-recording
  vs 1 s recording** (irregular sampling breaks everything → flag/resample, recommend 1 s).
- **Exclude treadmill runs** from training: treadmill running shortens GCT ~12% and shifts
  biomechanics vs overground (Van Hooren 2020), which would bias the contact-time→speed relation.
  Detect via `sub_sport == treadmill` / missing GPS and drop or model separately.
- Pandera schema on the output frame. `scripts/inventory.py`: how many activities, which RD fields
  present per activity, sampling cadence, flat vs hilly. Report to you.
- Tests use tiny synthetic FIT fixtures.

### Phase 2 — Ground-truth pace (the smoothing core)
- Have **`web-researcher`** locate the specific Garmin-forum smoothing thread + academic refs (GPS
  speed estimation / jerk-minimizing / Kalman for wrist GPS) to inform parameters.
- `groundtruth/smoothing.py`: implement and compare (a) Savitzky-Golay on cumulative distance,
  (b) **constant-acceleration Kalman + RTS bidirectional smoother** on ENU-projected positions
  (offline ⇒ non-causal smoothing is allowed and better). RTS is the standard offline INS/GPS
  position+velocity post-processor (Rauch 1965; wearable-sport precedents: Blank/ski-jump RTS,
  MDPI Sensors 2020; smartphone INS path reconstruction, Cortés 2019). `calibrate.py`: scale
  integrated smoothed speed to match known **lap/total distances** (corrects the ~5% GPS
  undercount).
- *Validation:* integrated `v_true` must reproduce each lap distance within tolerance; compare to
  your 1-km averages; residual diagnostics. Pick a default estimator. Output per-second `v_true`.

### Phase 3 — Features & leakage audit
- `features/build.py`: biomechanics features + **transient features** (rolling means over short
  windows, first differences / rates of change) so the model can react within seconds. Grade from
  barometric altitude; **flat-segment filter** (`|grade| < τ`) for the v1 flat-terrain scope.
- **Leakage audit:** quantify how much `step_length` tracks GPS speed and how it lags at interval
  starts — the evidence behind excluding it. `features/schema.py` pandera-validates the
  feature/target frame.

### Phase 4 — Modeling (biomechanics-first + ablation)
- **Primary interpretable baseline — contact-time power law.** The literature is strong here:
  per-runner `speed = c · GCT^d` (equivalently `log v = a + b·log GCT`) fits outdoor overground
  running at **r² ≈ 0.98**, with contact time alone explaining **~85%** of within-run speed
  variation and **~2.5% median speed error** — only slightly worse than good GPS (~1.6%) and
  available instantly when GPS is not (Hébert-Losier 2016; personalized-model advantage in
  Apte 2021). This sets the accuracy target and is trivially deployable in Monkey C.
- `models/baseline.py` — **the deployable candidates** (all Fenix-6-Pro-friendly): GCT power law,
  physics (`stride×cadence`), a regularized multi-feature **linear / low-degree polynomial** (GCT +
  cadence + VO + vertical ratio + transient terms), a tiny **GAM**, and optionally a **tiny MLP**.
  `models/train.py` — **offline-only ceilings** (never shipped): **LightGBM**, optionally
  **Gaussian-process regression** (also yields uncertainty), and **SVR** (your prior tool) as a
  comparison point. The ceilings exist only to bound how much the tiny model gives up.
- **Ablation:** biomechanics-only vs +stride-length — reports exactly what the leaky feature buys.
- **Model selection:** pick the **smallest deployable model within ε of the ceiling**; report the
  accuracy gap explicitly so the trade-off is visible.
- **Export contract + parity test (critical):** `export.py` emits the chosen model as a small
  coefficients + feature-list + normalization file (a few hundred bytes) plus a **pure-Python
  reference implementation of the exact on-watch arithmetic**. A unit test asserts the reference
  math reproduces the trained model's predictions to tolerance — this catches train/deploy drift
  before the Monkey C port and gives the future datafield an executable spec.
- Note fitness-drift/personalization strategy (recent-data weighting / periodic retrain + re-export).

### Phase 5 — Evaluation & reporting
- `eval/split.py`: GroupKFold-by-activity + temporal holdout (**not** random 75/25).
- `eval/metrics.py`: MAE/RMSE in s/km and m/s **plus the interval settling-time metric** (seconds
  to converge within X s/km of true pace after an interval start, model vs Garmin GPS pace).
- `eval/report.py`: timeline plots (pred vs true vs GPS), interval-start settling comparison, error
  distributions, per-run breakdown; a results/model-card markdown. This answers "does it actually
  beat GPS pace at the moments that matter?"

### Phase 6 — Hardening & docs
- `janitor` cleanup pass; `technical-writer` fills `README.md` (overview → usage → internals) and
  the model card. Reproducible end-to-end `uv run` entrypoint (ingest → groundtruth → features →
  train → eval).

### Phase 7 — Deferred tracks (not v1; separate follow-up PRs when you choose)
- **OSM map-matching:** snap traces to runnable OSM ways, use road arc-length as an alternative
  ground truth, add DEM-based grade, and **discard activities whose trace deviates too far** as bad
  data.
- **Connect IQ datafield (Fenix 6 Pro target):** consume the exported coefficients in a Monkey C
  data field for live on-watch pace, porting the Phase 4 pure-Python reference arithmetic 1:1 and
  checking it against the parity-test vectors. Respect the device's data-field memory cap (from the
  SDK device XML). Separate repo/add-on.

---

## Outlook — far-future milestones (vision, not committed scope)
Explicitly beyond v1; recorded so the architecture leaves room for them. Each is its own future
epic, gated on the core pipeline working.

- **Grade-adjusted pace (GAP) & pace on hilly terrain.** The most tractable of the three. Drop the
  flat-only restriction and model grade explicitly (barometric-altimeter grade + the Phase 4
  transient features), using the canonical energy-cost-vs-gradient curve (**Minetti et al. 2002**,
  the basis of Strava-style GAP) as prior/feature. Feasible; mainly needs reliable per-second grade
  and hilly-terrain training data. Still must stay within the Fenix-6-Pro compute budget.
- **Running-efficiency / economy estimate from biomechanics — feasibility: weak, treat as a
  research bet.** A 2024 systematic review/meta-analysis found most spatiotemporal metrics have
  *trivial, non-significant* links to running economy (contact time r≈−0.02, duty factor r≈−0.06);
  only **smaller vertical oscillation** and **higher cadence** show small significant associations
  (Anderson 2024; VO imbalance/economy work). Absolute economy needs a **metabolic (VO₂)
  reference** to calibrate, which we don't have. Realistic scope: a *relative* per-runner
  efficiency proxy (e.g. VO/vertical-ratio trend at matched pace), clearly labelled as a proxy —
  not an absolute mlO₂/kg/km figure.
- **Geofenced / terrain-specialized model weights.** If activity data shows one global model
  underperforms in specific terrains or locations, cluster runs by terrain/geozone and select model
  variants per zone (a small mixture-of-experts keyed by geofence/grade regime). Data-driven,
  triggered only if the residual analysis demands it. Constraint: still a *set of tiny models* the
  watch can hold and switch between, not one big model.
  - **Track mode (compelling special case).** Geofence known 400 m running tracks and switch to a
    dedicated "track" model. GPS overshoots on a track (410–420 m/lap → pace reads too high) even
    with Garmin's track-calibration mode, so a biomechanics model can *beat* GPS there. Bonus: the
    true lap distance on a track is exactly known (400 m in lane 1), which makes it a near-perfect
    ground-truth source for calibrating and training the model — worth capturing lap-marked track
    sessions specifically.

---

## Verification

- **Workflow itself:** Phase 0's trivial PR must traverse branch → Copilot + Claude review →
  green CI → auto-merge, proving the loop before real code lands.
- **Per PR:** `uv run ruff check`, typecheck, and `uv run pytest` green in CI (the auto-merge
  gate); both reviewers resolved.
- **Ground truth:** integrated smoothed speed reproduces lap/total distances within tolerance and
  matches your 1-km averages.
- **Model:** report activity-grouped + temporal-holdout MAE/RMSE **and** interval settling time vs
  GPS pace, with the biomechanics-only vs +stride-length ablation. Literature benchmark: aim for
  steady-state error near the **~2.5% (≈9 s/km at 6:00/km)** the contact-time power law achieves.
  Success = model settles **materially faster** than GPS pace at interval starts on held-out runs,
  at comparable steady-state accuracy.
- **End-to-end:** one `uv run` command runs ingest→groundtruth→features→train→eval on the FIT
  files in `data/` and emits the report.

## Prior art / references (leverage during implementation)
- **Hébert-Losier et al. 2016, PLOS One** — *Running Speed Can Be Predicted from Foot Contact Time
  during Outdoor over Ground Running.* Per-runner power law `v = c·CT^d`, r²≈0.98, ~2.5% median
  error, flat terrain. → the primary interpretable baseline and accuracy target.
- **Apte et al. 2021, Frontiers Sports Act. Living** — *Running Speed Estimation Using Shoe-Worn
  Inertial Sensors: Direct Integration, Linear, and Personalized Model.* → personalized models win.
- **Van Hooren et al. 2020 (meta-analysis)** — treadmill vs overground: GCT ~12% shorter on
  treadmill. → exclude/segregate treadmill runs.
- **Rauch–Tung–Striebel smoother** — standard offline INS/GPS position+velocity post-processing;
  wearable-sport precedents (ski-jump RTS, MDPI Sensors 2020; smartphone INS reconstruction 2019).
  → Phase 2 ground truth.
- Sports-eng IMU running-parameter estimation (ambulatory speed, spatiotemporal params; SVR/GPR/NN
  methods) — supplementary feature/method ideas. `web-researcher` will pull the specific
  Garmin-forum smoothing thread and any newer papers during Phase 2.

## Open logistics (not blocking approval)
- Drop some `.fit` files into `data/` when ready (how many activities do you have? are they 1 s
  recording?). The pipeline can be built against synthetic fixtures first, then validated on yours.
