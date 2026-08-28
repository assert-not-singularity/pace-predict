---
name: subagent-orchestration
description: >
  Guidelines for spawning subagents and designing multi-agent workflows. Use when about to use
  the Agent or Workflow tools, choose a model/effort for a subagent, restrict a subagent's tools,
  brief a subagent, decide how to decompose work, or verify what one returns. Not needed by a
  subagent that is itself just executing one task.
---

# Subagent & orchestration guidelines

## Right-size the model and effort

Default to a **mid-tier model at medium effort**. Escalate to a larger model or higher effort only
when the task's complexity strictly requires it, and state the reason up front. Over-powering a
unit of work wastes tokens *and* wall-clock — an over-tier agent will spend minutes deliberating
on a one-line change (measured: ~600s of deliberation vs. ~15s of execution on a trivial change a
mid-tier agent did in under two minutes). "Decompose into a workflow" does not mean "spend maximum
tokens per agent" — token cost is a real constraint.

### Match the tier to the task

| Task | Tier | Effort |
|---|---|---|
| Mechanical, high-volume, low-judgment — bulk edits, format/log cleanup, simple extraction, first-pass search/triage | fast/cheap (Haiku class) | low |
| The bulk of work — implementation, wiring, config, docs, schema/model edits, standard review, test writing | **mid (Sonnet class) — default** | medium |
| Costly-if-wrong reasoning — subtle correctness cores, concurrency/security, ambiguous architecture, deriving test oracles/golden values, adversarial verification, hard debugging | top/frontier (Opus class) | high/xhigh, only when justified |

Model names change — pick by capability tier, not version. When unsure, start at mid and escalate
only with a stated reason.

## Restrict each subagent's tools

Every MCP tool schema a subagent loads is billed as a cache **write** on first use (~12.5× the
read price). A file-editing agent that never calls Slack / Jira / cloud / browser tools should not
load their schemas — give it a minimal tool list. This cuts the per-spawn baseline from ~100K+
tokens to a fraction, every spawn.

## Brief each subagent to stand alone

A subagent starts fresh and its final report is **not** shown to the user. So:

- The prompt MUST carry everything the agent needs — file paths, the goal, constraints, and what
  to return. It cannot see your conversation.
- State the output you expect, and ask for **structured output** when you will consume the result
  programmatically — it avoids brittle parsing.
- **Relay the essence.** When the agent returns, extract what matters for the user; don't paste
  the raw report.

## Decompose to fit context; size the fleet to the task

Split work so each unit fits cleanly in one agent's context window — that, not "more agents," is
the point of decomposing. Match the number of agents to the ask: a couple for "find any bugs," a
larger pool for "audit this thoroughly." Prefer **fewer, longer-lived agents** over many
short-lived ones — each spawn re-pays its tool baseline; resume an agent's context rather than
respawning.

## Pipeline over barrier

Let items flow through stages independently — one item can be in stage 3 while another is still in
stage 1. Only force a synchronization **barrier** (wait for all of a stage before the next) when a
step genuinely needs the whole set: deduplicating across all results, early-exit on zero findings,
or a comparison that references "the others." A barrier you don't need wastes the fast items' idle
time.

## Review by lenses, not one pass

To review or audit a body of work, sweep it from several specialist perspectives rather than in one
undifferentiated pass — e.g. correctness & design, dead code & debt, docs drift — each as its own
agent or focused prompt. Each lens is blind to what the others catch, so coverage comes from their
union. Consolidate the findings, drop duplicates, and rank by severity before reporting.

## Verify — and for high stakes, verify adversarially

An orchestrator trusts a subagent's report only as far as the subagent verified it — require real
command output, not claims. For a finding that is **costly if wrong**, add independent
verification: spawn a separate agent (or a few) prompted to *refute* it, ideally through different
lenses (correctness, security, does-it-reproduce); keep the finding only if it survives. Diverse
skeptics catch what a repeated identical check misses.

## Pin the agent when you need determinism

Which skill or agent the model selects is **not** deterministic. For a repeatable or CI run, name
the exact agent (and model) rather than letting the model pick — otherwise the same prompt can
route differently between runs.

## Don't hide truncation

If a run caps coverage — top-N, sampling, no retries, a dropped modality — say so. Silent
truncation reads as "covered everything" when it didn't.

## Delegate by scope

When a task falls squarely in a specialist persona's scope, delegate to it. Handle inline for
quick lookups, single-file edits where context is already clear, or tasks that span several
personas (orchestrate directly, consulting the persona files for guidance).
