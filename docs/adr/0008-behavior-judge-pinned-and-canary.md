# ADR-0008: The behavior judge is checked out twice — a pinned copy that can fail the build, and an unpinned canary that cannot

**Status:** accepted · 2026-08-04

## Context

`.github/workflows/behavior-gate.yml` records a fresh trace of this agent and
judges it against `.agents/behaviors/` using
[agent-behavior-evals](https://github.com/fctpe/agent-behavior-evals), which is a
sibling project rather than a dependency of this one. The workflow clones it.

Cloning its default branch has two problems at once. A blocking result that
depends on another repository's current `main` is not reproducible: the same job
can answer differently a week later with nothing here having changed, and there
is no way to tell a real agent regression from a judge that moved. And the job
holds both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`, so whatever that branch
happens to contain at clone time runs next to two live credentials.

Pinning alone solves both and creates a third problem: a pin nobody looks at goes
stale, and the point of the sibling project is that it keeps improving.

## Decision

Check the judge out **twice** in the same workflow.

1. **The pinned copy decides.** It is cloned at a fixed commit and its exit code
   is the gate. This is the only copy whose result can fail the build.
2. **An unpinned copy is a canary.** It is cloned at `main`, judges the same
   trace, and reports without deciding — its exit code is ignored.

When the two disagree, the pin is due an update, and the disagreement is visible
in the job log rather than discovered as a mystery red build.

## Consequences

- A red gate means the agent changed, not that another repository did.
- The credential exposure is bounded to a reviewed commit, and moving the pin is
  an explicit diff in this repository.
- The canary can be noisy and that is acceptable, because it cannot fail
  anything. It is a signal to a maintainer, not a check.
- Both halves are gated on the two keys being present and fail loudly when either
  is missing — a run without keys is a meaningless green.
- The judge is still not a dependency of this project: `make check` and `ci.yml`
  need no Node, no Anthropic key and no network.
