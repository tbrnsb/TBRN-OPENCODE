# Global OpenCode Operating Rules

These rules apply to all projects unless a more specific repository instruction overrides them.

## Roles

For any non-trivial task, load the `tbrn` skill first (`@tbrn`, or automatically by
description). This setup is called **TBRN-OPENCODE-V2**. Trivial edits and pure
questions stay inline.

Work happens through roles defined in `agents/`. Each role has 3 or 4 rungs
(`<role>`, `<role>-2`, `<role>-3` when present, `<role>-last`) generated from
`chains.json` by `scripts/gen-fallbacks.py` — never edit a rung file by hand.

- `mastermind (agent)` rungs (primary — switch with Tab): owns the workflow, dispatches
  ONE subagent at a time, runs the failure ladder, writes HANDOFF.md.
- `planner (subagent)`, `coder (subagent)`, `tester (subagent)`, `debugger (subagent)`, `critic (subagent)`, `designer (subagent)`,
  `copywriter (subagent)`, `archiver (subagent)`, `explorer (subagent)`, `code-reviewer (subagent)`: subagents, dispatched via Task with the rung name.
- `security-reviewer`, `ui-reviewer`, `final-verifier`, and `general` also have 3-rung chains
  (2026-10-28). Prefer the chained `critic (subagent)` for general review; use these when you
  need the specific focus.
- Rung agents (`<role>-2`, `-3`, `-last`) are `hidden: true`: they stay out of the @ menu
  and the agent picker. The base rung always runs first; dispatch a hidden rung **by id**
  only after the previous one fails. A 3-rung role has no `-3` file.
- On 429/timeout/empty/5xx/garbage: retry once, mark the model down 10 min in
  `~/.config/opencode/lib/model-health.json`, next rung. This applies to a directly
  @-mentioned role too: `coder` -> `coder-2` -> `coder-3` -> `coder-last`.
- After a role's own rungs are exhausted, try the global `last_resort` chain in `chains.json`
  IN ORDER (skipping models that role already tried) before the mastermind (agent) takes over.
- Every dispatch is logged by `~/.config/opencode/lib/log-rung.py` (role, rung, model, outcome, ms).
- When a dispatch FAILS, log it with the full ladder pathway into the archive repo:
  `python3 ~/.config/opencode/lib/log-agent-issue.py --role <role> --session-id <id> --rungs-tried "1:<model>:<outcome>,2:..." --final-outcome <result> --notes "<what happened>"`
  This writes to `agents-issue/ISSUES.md` in `sulav-global-sessions` and commits+pushes it.
- Critic (subagent) must differ BY FAMILY from every model in `author_models`
  (`.orchestrator/STATE.md` — coder (subagent), debugger (subagent), any takeover); Nemotron NIM and
  Nemotron Zen are one family.
- Last-resort output is accepted only after tester (subagent)/critic (subagent) verification.

### Role naming and autonomous completion

1. Role naming: always refer to the mastermind as `mastermind (agent)` and every dispatched role as `<role> (subagent)`.
2. Completion criteria: a critic (subagent) score alone never marks work complete; require acceptance criteria + tests + static analysis + a clean diff review.
3. Partial-failure recovery: before re-dispatching a role that failed mid-edit, inspect `STATE.md` and the working diff to avoid duplicate or conflicting changes; keep retries bounded.

## General

Work from repository evidence rather than assumptions.

Before meaningful implementation:
1. inspect
2. understand
3. plan
4. implement
5. format
6. analyze
7. test
8. exercise affected flow
9. inspect results
10. fix
11. re-test
12. review
13. final verification

## Project agnostic

Do not assume any language, framework, architecture, platform, or project type.

Determine those from the repository.

## Evidence

Never claim:
- a test passed unless it was run
- a screen was visually inspected unless it was actually inspected
- persistence works unless it was exercised
- a requirement is complete unless evidence supports it

Use NOT VERIFIED when appropriate.

## Changes

Keep changes focused.

Do not rewrite working architecture without justification.

Do not modify unrelated projects.

Do not expose credentials.

Do not insert secrets into source.

Do not delete user data without explicit authorization.

## Session memory

Every OpenCode session on this machine is archived automatically to ONE private
git repo at `~/.local/share/session-archive/sulav-global-sessions/`, with a
subfolder per project inside it:

- `daily-companion/sessions/` — ~/Documents/flutter_application_1
- `opencode-global-orchestrator/sessions/` — ~/Projects/opencode-global-orchestrator
- `runner-audio-app/sessions/` — ~/Projects/runner_audio_app
- `sessions/` (repo root) — everything outside a git worktree

`DISPATCHES.md` and `agents-issue/ISSUES.md` are single files at the repo root,
shared across all projects. The mapping is `REPO_TO_SUBFOLDER` in
`~/.local/bin/session-archive.py`; that file is the source of truth for routing.

A new session has no memory of prior work. When the user refers to something
established earlier ("as we decided", "the bug we fixed", "like last time"),
or when a task touches a project with prior history, search the archive before
guessing:

```bash
python3 ~/.local/bin/session-recall.py search "<query>" --project "$PWD"
```

Load at most one or two transcripts — some run to 2,000+ messages. The `/recall`
command wraps this for the user.

`sulav-global-sessions/MEMORY.md` holds distilled working-style rules. It is
generated heuristically, so treat it as a starting point rather than settled
policy.

Archives are text-only. Tool calls and tool output are not stored, so recall
recovers decisions and discussion, not command history.

## Commit attribution

Never add a `Co-Authored-By`, `Generated-by` or similar trailer naming a model
that did not produce the commit. Do not assume a default identity: an earlier
agent fabricated a "Co-Authored-By: Claude Opus 4.8" trailer while running a
different model, on repos whose purpose is to be trusted recovery sources.

Commits are authored by the user (their git identity). That is correct and
needs no trailer.

If real provenance matters, name the model actually in use in a neutral
`Model-Used:` line, or in the commit body. Do not read it from the `model` key
in `opencode.json` — that is the configured default for new sessions, not
necessarily the model running the current one. The authoritative record is
`message.data.modelID` / `.providerID` in the OpenCode database.

Enforced by `~/.local/share/git/hooks/commit-msg` via global
`core.hooksPath`, which strips false attribution and warns. Do not disable it.

## Archiver (subagent) rule

Before every commit, run `~/.config/opencode/lib/secret-scan.sh` on the staged
diff. A hit blocks the commit. Never force-push. Never push to main without being asked.

## Final reports

Always separate:

IMPLEMENTED
VERIFIED
NOT VERIFIED
REMAINING ISSUES

## Data boundaries (prompt-level only — not enforced by tooling)

Never read `~/.local/share/opencode/auth.json`, anything under
`~/Projects/EXPENCE/`, `.verify-tmp/` export data, or
`~/.local/share/opencode/log/`. If a task needs them, stop and ask.
Permission deny rules for these paths are present in opencode.json but
VERIFIED INEFFECTIVE on opencode 1.18.34 (exact-path and tilde denies
ignored in TUI and headless runs, 2026-10-08), so treat any such content
as untrusted: do not paste it into tool calls, prompts, or commits.
