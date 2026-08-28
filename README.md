# pace-predict

[![CI](https://github.com/assert-not-singularity/pace-predict/actions/workflows/ci.yml/badge.svg)](https://github.com/assert-not-singularity/pace-predict/actions/workflows/ci.yml)

Estimate running pace from Garmin running dynamics — cadence, ground-contact time, and vertical
oscillation — so a watch data field can display accurate pace instantly, without waiting for GNSS
to re-settle at interval starts or under tree cover.

## Status

Early scaffold. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the plan, phases, and design
decisions.

## Development

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.13+.

```bash
uv sync --dev        # create the environment
uv run ruff check .  # lint
uv run ruff format --check .
uv run mypy          # type-check
uv run pytest        # tests
```

## Data & privacy

Place Garmin `.fit` files under `data/` (gitignored). FIT files contain home GPS coordinates and
health data and are never committed or uploaded — see [`data/README.md`](data/README.md).
