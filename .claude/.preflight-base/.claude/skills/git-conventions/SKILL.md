---
name: git-conventions
description: >
  Conventions for branches, commits, and PRs. Use before creating a branch, staging changes,
  writing a commit message, or drafting a PR body/description. Not needed for read-only work or
  for subagents that only implement and leave the tree dirty.
---

# Git & commit conventions

## Branch from an up-to-date default branch

MUST NOT commit directly to the default branch. Before creating a branch:

```bash
git checkout main && git pull
git checkout -b feat/<issue-id>-short-description
```

Branch names use hyphens throughout — never slashes inside the description. MUST NOT branch from
another active feature branch unless you are deliberately stacking PRs — doing so silently carries
that branch's unmerged commits into the new PR. If you are on a feature branch when starting an
unrelated task, switch to the default branch first.

## One PR = one branch = one logical change

Each PR gets its own branch. If a plan produces several PRs, create a separate branch for each
**before** making commits. MUST NOT put two PRs' worth of work into one commit or branch.

**Stacked PRs:** when PR 2 depends on PR 1, base PR 2 on PR 1's branch; once PR 1 merges, rebase
PR 2 onto the default branch.

## Stage explicitly

Add the files a change touches by name (`git add <file> …`). MUST NOT use `git add .` or
`git commit -a` when unrelated edits are present in the tree.

## Commit & PR messages describe current behaviour + the change

Write only what is true now and what the change makes it — never the incident, the debugging
path, or how the code used to work (see the `prose-and-docs` standard's no-historical-narration
rule). One line on what was wrong, one line on the fix. Backstory, repro steps, and failing-run
links belong in a PR *comment*, not the permanent message.

- **Avoid:** "Fixed the silent-skip bug found while investigating the 2026-07-10 run failure…"
- **Prefer:** "Raise if the edge file is missing instead of silently skipping the match."

End AI-authored commits with a `Co-Authored-By:` line naming the model that made the change, per
the project/org policy.

## Remotes

Resolve owner/repo once with `git remote get-url origin` at the start of a GitHub task, then
reuse — MUST NOT guess. Push branches to `origin` and open the PR against the default branch (or
the parent branch for a stacked PR).
