# pace-predict — AI agent instructions

Estimate running pace from Garmin running dynamics (cadence, ground-contact time, vertical
oscillation) so a watch data field can show accurate pace instantly — without waiting for GNSS to
re-settle at interval starts or under tree cover.

This file is a **thin index**, not a manual. Deep conventions live in focused files that load
only when relevant: personas in `.claude/agents/`, task-triggered skills in `.claude/skills/`,
and file-scoped rules in `.claude/rules/`. Keep this file lean — add a fact only when an agent
needs it and cannot derive it from the code or the docs.

## Always-on standards

@.claude/standards/working-style.md

## Orient yourself

- Repo overview / architecture → `README.md`
- Roadmap, phases, and design decisions → `docs/ROADMAP.md`
- Python package → `src/pace_predict/` (pipeline: `io` → `groundtruth` → `features` → `models` → `eval`)
- Data (FIT files) → `data/` (gitignored; see `data/README.md`)

## Project-specific facts

- **The shipped model MUST fit a Garmin Fenix 6 Pro Connect IQ data field** — a handful of
  coefficients, O(1) arithmetic per tick, ≤ ~1–2 KB. Heavy models (LightGBM / GPR / SVR) are
  offline-only reference ceilings and MUST NOT be shipped. Deployable families are listed in
  `docs/ROADMAP.md`.
- **MUST NOT train the deployable model on `step/stride_length`** — it is GPS-derived and leaks the
  GNSS error we are trying to avoid, and it lags at interval starts. Use it only for the ablation.
- **Privacy: `data/` and all `*.fit` files are gitignored and MUST NOT be committed, uploaded to
  any external service, or pasted into logs or PR bodies** — they contain home GPS coordinates and
  health data. Tests use synthetic fixtures only.
