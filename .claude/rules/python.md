---
paths:
  - "**/*.py"
---

# Python standards

Language, tooling, and hygiene conventions that are safe to apply to any Python repo. Anything
that assumes a particular architecture (adapters, dependency injection, boundary validation,
idempotent writes) lives in the optional `scalable-architecture` rule, not here.

## Language & types

- **Python 3.13+** — use modern syntax; full type hints on every public interface.
- **PEP 695 type-parameter syntax**: `class Foo[T]:`, `def fn[T](...):`, `type Alias = …`. MUST
  NOT use `TypeVar`, `Generic`, `TypeAlias`, or `ParamSpec` from `typing`.
- **`uv` for all commands** — never bare `pip`.
- Prefer timezone-aware UTC timestamps; avoid naive datetimes.

## Async contract — violations are bugs, not style

Applies to any `async` codebase:

- Every `async def` that does I/O MUST await a **true async primitive** (one that registers an
  event with the loop). Pure in-memory computation inside `async def` is fine.
- **MUST NOT call blocking I/O inside `async def`** — sync DB clients, `requests`, `time.sleep`,
  blocking file writes in loops. These silently freeze the event loop.
- Prefer async-native libraries (async DB/HTTP clients, `asyncio.sleep`).
- **Long CPU-bound work also blocks the loop** — offload via `run_in_executor` / a process pool.
- If a blocking call is truly unavoidable, offload it via `run_in_executor` and add a comment.
- **Background tasks:** a task sharing state with a handler owns synchronisation — use
  `asyncio.Lock` (snapshot under lock, act outside) or `asyncio.Queue`. A plain list plus a
  background task is a silent race condition.
- Linters catch stdlib violations; third-party blocking calls are caught only by review.

## Logging

- Use **structured logging** — key–value fields, not f-strings baked into the message:
  `log.info("order submitted", order_id=…, symbol=…)`. Configure it once at startup; get a logger
  per module. (Which library is the project's choice.)
- **Log levels:** `debug` for internal/infrastructure mechanics; `info` for business-significant
  events; `warning` for recoverable surprises; `error` for failures needing attention; `critical`
  for unrecoverable ones.
- Attach correlation IDs (request/run/entity id) at a workflow's entry point so they propagate to
  downstream log lines.
- Log exceptions *with their traceback*, not a hand-formatted string.

## Definition ordering

*Imports* follow PEP 8: `__future__` → stdlib → third-party → local. Module-level dunders
(`__all__`, `__version__`) go after imports, before other definitions.

*Module level:* dependency order — define helpers before consumers. Group related definitions;
leave a spare blank line between groups.

*Within a class* — public interface first, implementation details last:

1. Class-level constants / class variables
2. `__new__` / `__init__`
3. Other dunder / special methods
4. Properties
5. Public methods
6. Protected methods (`_`)
7. Private methods (`__`)

A class-level alias that references a method (e.g. `log = debug = info = msg`) follows the method
it aliases.

*Within a function or method* — separate logical phases with a blank line and a short comment
naming each; omit the comment when the phase is obvious from a single line:

```python
def handle(message: Foo) -> None:
    # Validate
    if not message.items:
        return

    # Process
    result = process(message.content)

    # Persist
    store.insert(result)
```

## Suppressions are a last resort

`# noqa` (linter) and `# type: ignore` / `# ty: ignore` (type checker) MUST NOT be a default move.
Almost every warning signals a real design problem — the correct response is to restructure the
code, not silence the tool. Examples: a "not at top" import warning usually means an optional
heavy dependency that belongs behind a top-level `try/except ImportError`; a type-ignore on a
wrong argument usually means constructing the correct type instead.

If a suppression genuinely cannot be avoided:

1. Exhaust design-level alternatives.
2. In production code, **ask the user** — name the rule, the alternatives considered, the residual
   risk — before adding it.
3. Add it *with an inline comment stating the reason* (and a follow-up issue reference if apt).
   Tests MAY carry a commented suppression without prior approval when it reflects deliberate
   behaviour (e.g. passing a bad value to assert rejection) and no simpler fix is as readable.

A bare `# noqa` / `# ty: ignore` with no explanation is never acceptable, anywhere.

## Verify before committing

Run the project's full check (lint + format-check + type check + tests) before committing, and
MUST NOT commit if it fails. Report real output — never claim a check passed that you did not
observe passing.

## Dependencies

- Check the latest version before adding a package; pin to `>=<latest>`, not a stale lower bound.
- Re-lock (`uv sync`) and re-run checks after changing dependencies.
