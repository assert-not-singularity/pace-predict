---
paths:
  - "**/*.py"
---

# Scalable architecture (opt-in)

Patterns that pay off as a project grows — ports-and-adapters with dependency injection. They earn
their keep in services, pipelines, and larger applications that must stay testable and swappable
as they scale. Skip them for scripts, small libraries, notebooks, or framework apps (e.g.
Django/FastAPI) that have their own idioms — adopting them there is over-engineering. Keep this
rule only if the project follows this style.

## Separate pure logic from I/O

Keep computation — transforms, domain rules, decisions — free of side effects so it is testable
in isolation. This is the part worth keeping even if you take nothing else here: a pure core is
easy to test and reason about. I/O (network, disk, database, external services) lives in thin
adapters at the edges; pure code imports no I/O clients.

## Inject dependencies

Wire concrete infrastructure in at a composition root (the top-level place the app is assembled),
via constructor args, setters, or factory callables. Business logic MUST NOT construct or look up
its own infrastructure — that is what keeps adapters swappable and logic testable with fakes.

Where you genuinely need to swap an implementation — multiple backends, test fakes, or a boundary
likely to change — depend on an interface and pick the concrete type at the composition root.
Don't add this indirection speculatively: a single implementation unlikely to change doesn't need
an interface.

## Validate at boundaries

Data crossing a module or process boundary is validated where it enters:

- `pydantic` models for structured data — config, message/event types, API contracts, and
  structured model output.
- A schema check (e.g. `pandera`) for DataFrames that cross a boundary.

### Pydantic default-value rule

MUST NOT add a field default just because a value is common. Before adding one, ask: *does this
default make the behaviour obvious to someone reading the config?* If not — or if the field
materially affects output quality or risk — declare it **without** a default so an explicit value
is forced, and add a short docstring explaining why.

```python
temperature: float  # required — affects output quality; no default
assets: list[str]  # required — drives what is processed
retry: RetryConfig = RetryConfig()  # optional — safe mechanical default
handler_timeout_s: float = 120.0  # optional — infrastructure concern, obvious safe value
```

## Configuration boundary

Infrastructure config (connection strings, endpoints, tuning) and secrets are injected from
app-level config or the environment — never hardcoded in business logic or baked into per-unit
params. This keeps units environment-agnostic and testable.

## Resilience (long-running & background work)

Applies to services, workers, and consumers — not one-shot scripts:

- Implement graceful shutdown and cancellation.
- Retry external calls with jittered exponential backoff, with a cap.
- A failure MUST NOT silently propagate — fail fast or fall back explicitly, and log it with
  context.
- Log lifecycle and health events (startup, shutdown, degraded state).

## Idempotent writes

Where a unit performs writes that may be retried or re-run (batch jobs, pipelines, event
consumers), those writes MUST be idempotent — re-running MUST NOT duplicate. Not applicable to
projects without that write pattern.
