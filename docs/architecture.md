# Architecture

TBRN-OPENCODE-V2 runs on OpenCode v2.2.0. This document describes the
components and the data flow between them: from configuration to generated
agents, and from a user request through dispatch, failover, and logging.

## Components

| Component | Location | Role |
| :--- | :--- | :--- |
| `chains.json` | `~/.config/opencode/chains.json` | Single source of truth: per-role model chains, the global `last_resort` list, and the model-family map. |
| Generator | `scripts/gen-fallbacks.py` | Turns `chains.json` into one agent file per rung. Idempotent; `--check` detects drift. |
| Rung agents | `~/.config/opencode/agents/*.md` | 56 generated agent files (11 roles x 4 rungs + 4 roles x 3 rungs). Never hand-edited. |
| Mastermind | `agents/mastermind.md` (plus `-2`, `-3`, `-last`) | The only primary agent. Owns the workflow and dispatches one subagent at a time. |
| `opencode.json` | `~/.config/opencode/opencode.json` | V2 config: `plugins`, provider routes, `agents.explore.disabled`, the mastermind's `permission.subagent` whitelist. |
| Health registry | `lib/health.py` + `lib/model-health.json` | Tracks models in cooldown. `up` / `down` / `check`. |
| Ladder helper | `scripts/ladder.py` | Dry-runs a role chain, computes critic pick by family, writes HANDOFF.md. |
| Dispatch log | `lib/log-rung.py` -> `lib/rung-log.jsonl` | One line per dispatch: role, rung, model, outcome, ms. |
| Issue log | `lib/log-agent-issue.py` | Records failed chains into `agents-issue/ISSUES.md` in the archive repo. |
| `tbrn` skill | `skills/tbrn/` | Routing table, brief templates, ladder rules for the mastermind. |
| Session archive | plugin + `~/.local/bin/session-archive.py` | Archives each session transcript into one private git repo, per-project subfolders. |

## Build flow: config to agents

```
chains.json  (role -> [model, model, ...], last_resort, families)
     |
     v
scripts/gen-fallbacks.py
     |
     v
agents/<role>.md, <role>-2.md, <role>-3.md, <role>-last.md
```

The generator stamps each rung with its model, the `TBRN's <Role>`
description, a hex role color, and `hidden: true` for every non-base rung.
Mastermind rungs are `mode: primary`; everything else is `mode: subagent`.
Rerunning the generator rewrites all outputs. `--check` compares the generated
set against `chains.json` and reports "in sync: 56 agents" or the drift.

The repo's `config/live/` directory is the committed mirror of the installed
state: the same `opencode.json`, `chains.json`, `AGENTS.md`, policy files, and
all 56 agent files.

## Run flow: request to result

```
User request
    |
    v
mastermind (Tab-selected primary rung)
    |-- sizes the job (tbrn skill: routing.md)
    |-- briefs ONE role (briefs.md skeleton)
    v
subagent tool  (permission.subagent whitelist)
    |
    v
<role> base rung --ok--> result checked by mastermind --> next role or report
    |
   fail (429 / timeout / empty / 5xx / garbage)
    |
    v
retry once --> still failing: health.py down <model> <reason>
    |
    v
<role>-2 --> <role>-3 (4-rung roles) --> <role>-last
    |
    v
last_resort chain, in order, skipping tried/cooled models
    |
    v
mastermind takes the role over; own-chain failure writes HANDOFF.md and pauses
```

Only one subagent runs at any moment. The mastermind checks each result
against the brief's acceptance criteria before dispatching the next role; a
critic or tester verdict is required before completion claims.

## Health registry

When a rung fails, `lib/health.py down <model> <reason>` marks the model for
roughly 10 minutes in `lib/model-health.json`. Reasons are `429`, `timeout`,
`5xx`, `empty`, `garbage`. Before picking any model, the ladder checks
`health.py check <model>` and skips cooled models — including models cooled by
a different role. Cooldowns expire on their own; nothing needs to mark a model
back up.

## Logs

Two files record what happened:

- `lib/rung-log.jsonl`: append-only JSON lines, one per dispatch attempt, with
  role, rung, model, outcome, and duration. Used to audit which model actually
  served a role.
- `agents-issue/ISSUES.md` in `sulav-global-sessions`: written by
  `log-agent-issue.py` when a dispatch chain ultimately fails, with the full
  rung pathway (`1:<model>:<outcome>,2:...`), the final outcome, and notes.

## State between dispatches

The mastermind keeps `.orchestrator/STATE.md` in the project under work: the
current task, decisions, files changed, and `author_models` — every model that
edited the diff. The critic's family check reads `author_models` from this
file. `.orchestrator/worker-state/` is a separate directory for the legacy GO
worker; the name is historical and kept deliberately.

## Session archive

A `session-archive` plugin (listed by `opencode plugin list`) triggers on
session idle and hands the transcript to `session-archive.py`, which routes it
into the single private repo `sulav-global-sessions`, under a subfolder for the
project. `DISPATCHES.md` and `agents-issue/ISSUES.md` sit at that repo's root.
Credential-shaped strings are redacted before commit. Archives are text-only.

## Legacy layer

The GO worker (`go/`, `scripts/opencode-worker`, `lib/classify.sh`,
`lib/policy.py`, and the 21 shell tests in `tests/`) predates TBRN-OPENCODE-V2.
It is pending removal and is not part of the dispatch path described here.
`docs/route-validation.md` describes tooling from that era.
