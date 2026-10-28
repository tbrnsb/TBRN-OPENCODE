# Routing

Routing decides which roles run for a given job, and in what order. The source
of truth is `skills/tbrn/references/routing.md`, which the mastermind reads
before dispatching. The rule is simple: pick the smallest route that finishes
the job. A role not listed for a domain does not run.

## By job

| Job | Roles, in order | Notes |
| :--- | :--- | :--- |
| Trivial: typo, one file, one-line answer | none | Do it inline, no dispatch. |
| Understand code or data | `explorer` | Read-only. Returns a map, not a fix. |
| Bug, crash, failing test | `debugger` -> `coder` -> `tester` -> `critic` | `critic` only if the fix is non-trivial. |
| Feature or refactor | `planner` -> `coder` -> `tester` -> `critic` | `explorer` first only if the area is unfamiliar. |
| Design or UI spec only | `designer` -> `ui-reviewer` | No `coder`, no `tester`, no `planner`. |
| Design plus implementation | `designer` -> `coder` -> `tester` -> `ui-reviewer` | The designer brief must precede the coder brief. |
| Text: README, docs, in-app strings | `copywriter` | No `coder`, no `tester`, no `planner`. |
| Review only, no changes wanted | `code-reviewer` -> `critic` | `critic` only if a score is needed. |
| Security-sensitive diff | `security-reviewer` before commit | Read-only. Blocks on findings. |
| Acceptance against the original ask | `final-verifier` | Runs the acceptance commands itself. |
| Commit or PR | `archiver` | Required before any commit. |
| Misc multi-step | `general` | Last resort for work that fits nothing else. |

## Domain rules

- A design-only or writing-only job never touches `coder`, `tester`, or
  `planner`.
- A backend job never touches `designer` or `ui-reviewer`.
- A job spanning domains runs each domain's roles in order, then stops. A
  Flutter feature with a new screen runs `designer` -> `coder` -> `tester` ->
  `ui-reviewer`, not the full six-role chain.
- Reviewing existing work never starts a `coder`. If the review finds
  problems, the user decides whether to fix them.
- `archiver` is the only mandatory role. It runs before a commit, not before a
  review.

## Process skills by stage

The brief must name the process skill a child loads; a child session starts
empty and does not inherit the parent's skills.

| Stage | Skill |
| :--- | :--- |
| Vague or creative ask | `brainstorming` |
| Multi-step feature | `writing-plans` |
| Implementation | `test-driven-development` |
| Any bug | `systematic-debugging` |
| Claiming completion | `verification-before-completion` |
| Requesting review | `requesting-code-review` |
| Receiving review | `receiving-code-review` |
| Finishing a branch | `finishing-a-development-branch` |
| Isolated feature work | `using-git-worktrees` |

## Conflicts with other guidance

- `dispatching-parallel-agents` recommends parallelizing. Do not: free tiers
  rate-limit, and the system runs one subagent at a time.
- `subagent-driven-development` and `executing-plans` define their own
  executors. TBRN replaces them because it knows the rungs, health cooldowns,
  and role ids. Their discipline still applies: fresh context per task, a
  review after each task, one broad final review.
- `brainstorming` wins over speed: a new feature with no agreed design gets a
  design pass before `planner` runs.

## Routing is not model selection

Routing picks roles. Models come from `chains.json`: each role has its own
ordered chain, walked by the failure ladder on failure (see
`docs/failure-recovery.md`). Routing never picks a model directly.
