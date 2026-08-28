---
paths:
  - "**/*.md"
---

# Prose & documentation standards

How any Markdown in the repo should read — commit messages, PR bodies, READMEs, and docs alike.

## Content honesty

- **Document what exists.** Describe the current behaviour; MUST NOT describe planned or
  hypothetical features.
- **No historical narration.** Docstrings, comments, commit messages, PR bodies, and doc prose
  describe what the code **does now** — never what it used to do, the bug it had, or the
  investigation that led here. State the current behaviour (and, for commits/PRs, the one-line
  change); drop the backstory.
  - **Avoid:** "Fixed the bug where matching silently failed — found while investigating the
    2026-07-10 run failure, reproduced in isolation."
  - **Prefer:** "Raise if the edge file is missing instead of silently skipping the match."
  - The tell is emphasis around a negation ("does **not**…", "is the **only**…", "no longer…")
    that exists only because the text used to say otherwise. State the current fact plainly.
    **Exception:** keep a "not X" framing when X is a real, live alternative a reader would
    otherwise assume (a genuine disambiguation, not narration).
- **Verify before you state it.** MUST confirm that every class, method, column, config key, and
  CLI command exists (grep, read the source) before naming it. When you cannot verify, describe
  generically or state the uncertainty — MUST NOT fabricate an API signature, column name, or
  example.

## Voice

- Active voice, present tense. No marketing language ("powerful", "cutting-edge", "seamless").
- Short paragraphs (3–5 lines). Tables for structured information; code blocks only when they
  carry their weight.

## Diagrams and visuals support text

Text is the primary carrier. A diagram, table, or code block **illustrates** what the prose
already says — it never stands in for it. Every section opens with at least a sentence of prose,
and every diagram is followed by a sentence or two interpreting its takeaway. Back-to-back
diagrams with no prose between them is wrong.

## Ordering

Order enumerations (package lists, config/env tables, flags) **alphabetically** by default so
diffs stay stable. Keep a **meaningful order** where one exists — pipeline stages, workflow steps,
precedence rankings — and never alphabetize those.

## Examples

- **Do:** concrete and factual — says what it does, in verifiable terms:
  "The pipeline builder reads class paths from config, resolves them via import, and injects
  dependencies before starting."
- **Don't:** marketing / vague — adjectives, no information:
  "Our powerful, cutting-edge engine leverages advanced architecture."
- **Do:** honest about the unknown — states the fact, defers the detail you haven't confirmed:
  "Writes a per-item label column — check the schema for the exact name."
- **Don't:** fabricated specifics — asserts an API you haven't verified:
  "Call `upsert()`" when you haven't confirmed the method exists.

## Directive discipline & leanness (instruction files)

Applies when authoring `CLAUDE.md`/`AGENTS.md`, persona files, rules, and skills:

- Use RFC 2119 keywords (MUST, MUST NOT, SHOULD, SHOULD NOT, MAY) in directives. Hedging ("might
  want to", "perhaps consider") is not a directive — state the rule.
- The root `CLAUDE.md`/`AGENTS.md` is a thin index, not a manual — target one screen. Deep
  conventions live in focused files that load only when relevant.
- Add a rule only when it is agent-relevant **and** not derivable from the code, the docs, or a
  tool's defaults. A rule that repeats what a linter or the code already enforces is noise.
- When a shared standard covers something, reference it; do not restate it.
