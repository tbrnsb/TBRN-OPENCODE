# Routing table

Pick the smallest route that finishes the job. A role that is not listed for the
domain does not run.

## By job

| Job | Roles, in order | Notes |
|---|---|---|
| Trivial: typo, one file, one-line answer | none | Do it inline. |
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

- A design-only or writing-only job never touches `coder`, `tester` or `planner`.
- A backend job never touches `designer` or `ui-reviewer`.
- A job spanning domains runs each domain's roles in order, then stops. Example:
  a Flutter feature with a new screen runs `designer` -> `coder` -> `tester` ->
  `ui-reviewer`, not `planner` -> `designer` -> `coder` -> `tester` -> `critic` ->
  `ui-reviewer`.
- Reviewing existing work never starts a `coder`. If the review finds problems, the
  user decides whether to fix them.
- `archiver` is the only mandatory role. It runs before a commit, not before a review.

## Process skills by stage

| Stage | Skill to load |
|---|---|
| Vague or creative ask | `brainstorming` |
| Multi-step feature | `writing-plans` |
| Implementation | `test-driven-development` |
| Any bug | `systematic-debugging` |
| Claiming completion | `verification-before-completion` |
| Requesting review | `requesting-code-review` |
| Receiving review | `receiving-code-review` |
| Finishing a branch | `finishing-a-development-branch` |
| Isolated feature work | `using-git-worktrees` |

Name the skill in the child's brief. The Superpowers bootstrap is injected only into
top-level sessions, so a child does not know the skill exists unless the brief says so.

## Conflicts

- `dispatching-parallel-agents` says to parallelize. Do not. Free tiers rate-limit;
  `AGENTS.md` forbids two dispatches at once.
- `subagent-driven-development` and `executing-plans` define their own executors.
  TBRN replaces them, because it knows the rungs, health cooldowns and role ids.
  Keep their discipline: fresh context per task, a review after each task, one broad
  final review.
- `brainstorming` wins over speed. A new feature with no agreed design gets a design
  pass before `planner` runs.
