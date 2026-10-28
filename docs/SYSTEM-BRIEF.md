# TBRN-OPENCODE-V2 System Brief

## 1. Overview

TBRN-OPENCODE-V2 is the multi-agent orchestration layer installed at
`~/.config/opencode` and running on OpenCode v2.2.0. One primary agent, the
`mastermind`, sizes every job and dispatches exactly one specialist subagent at
a time. Each role has a 3- or 4-model failover chain; on failure the ladder
retries once, cools the model down for about 10 minutes, and walks to the next
rung, then a global `last_resort` chain, then a mastermind takeover. This
repository (`tbrnsb/OC-Orchestrator`) is the source-of-truth package; the live
install mirrors it via `config/live/`.

## 2. Roles

15 roles: one primary agent and 14 subagents.

| Role | Kind | Purpose |
| :--- | :--- | :--- |
| `mastermind` | primary | Only point of contact. Sizes jobs, dispatches one subagent at a time, runs the ladder, keeps state. Switched with Tab. |
| `planner` | subagent | Read-only analyst. Writes ordered task plans; never product code. |
| `coder` | subagent | Implements features, refactors, fixes with known root cause. |
| `tester` | subagent | Runs tests, builds, static analysis. Edits test files only. |
| `debugger` | subagent | Root cause before fix. Max 3 debug loops per issue. |
| `critic` | subagent | Scores work /10, read-only. Pass is 9/10. Max 3 rounds. |
| `designer` | subagent | UI/UX specs and implemented-UI review. Reads screenshots. |
| `copywriter` | subagent | User-facing text only (`*.md`, `*.txt`, `*.arb`). |
| `explorer` | subagent | Read-only repository investigation. Replaces built-in `explore`. |
| `code-reviewer` | subagent | Independent diff review. Read-only. |
| `archiver` | subagent | Conventional commits, secret scan, session log. Mandatory before commits. |
| `security-reviewer` | subagent | Diff audit: secrets, injection, unsafe IO, authz gaps. Read-only. |
| `final-verifier` | subagent | Runs acceptance commands itself. Edit access denied. |
| `ui-reviewer` | subagent | Screenshots and implemented UI against the design spec. |
| `general` | subagent | Multi-step work that fits no other role. |

## 3. Rungs and generated agents

`chains.json` lists each role's models in rung order. `scripts/gen-fallbacks.py`
generates one agent file per rung (`<role>.md`, `<role>-2.md`, `<role>-3.md`,
`<role>-last.md`) into `agents/`. A 3-rung role — `security-reviewer`,
`final-verifier`, `ui-reviewer`, `general` — has no `-3` file.

- 56 agents total: 11 roles x 4 rungs + 4 roles x 3 rungs.
- 18 visible: the 15 base rungs plus the mastermind's `-2`, `-3`, `-last`.
- 38 rung agents are `hidden: true`: out of the @ menu and the agent picker,
  dispatched by id only after the previous rung fails.
- Generated files are never hand-edited. Edit `chains.json`, re-run the
  generator. `gen-fallbacks.py --check` reports "in sync: 56 agents".

## 4. Dispatch

The mastermind dispatches through the `subagent` tool (the v2.2.0 name; older
notes call it Task). Its frontmatter carries `permission.subagent` with
`"*": deny` first, then the 14 role ids and the globs `*-2`, `*-3`, `*-last`.
Last matching rule wins, so only the intended roles are dispatchable. Exactly
one subagent runs at a time; parallel dispatch is forbidden because free tiers
rate-limit.

A child session starts with no history. Every dispatch is a brief carrying the
goal, acceptance criteria, files, constraints, the process skill to load, and
the return format (`skills/tbrn/references/briefs.md`).

## 5. Failure ladder

Per role, in order: rung 1, then `-2`, then `-3` (4-rung roles only), then
`-last`. On 429, timeout, empty reply, 5xx, or garbage output:

1. Retry the same rung once.
2. Still failing: mark the model down for about 10 minutes in
   `lib/model-health.json` (`lib/health.py down <model> <reason>`), then
   dispatch the next rung.
3. Role chain exhausted: walk the global `last_resort` chain in order, skipping
   models already tried by that role and models in cooldown:
   `opencode/deepseek-v4.1-flash-free`, `mistral/codestral-latest`,
   `nvidia_secondary/nvidia/nemotron-3.5-lightning-30b-a3b`.
4. Everything dead: the mastermind takes the role over. If the mastermind's own
   chain fails, it writes `HANDOFF.md` (task, decisions, files changed, next
   step) and pauses. It also hands off at roughly 70% context.

`scripts/ladder.py --dry-run <role>` walks a chain without calling models and
exits 3 when both the role chain and `last_resort` are dead.

## 6. Caps and the critic family rule

- Max 3 debug loops per issue.
- Max 3 critic rounds. Pass is 9/10.
- A critic score alone never marks work complete. Completion needs acceptance
  criteria satisfied, tests run and passing, static analysis clean, and the
  real diff reviewed.
- Last-resort output is accepted only after tester or critic verification.
- The critic must differ BY FAMILY from every model in `author_models` in
  `.orchestrator/STATE.md` (coder, debugger, any takeover). The family map
  lives in `chains.json` under `families`: only the Nemotron group is merged
  (Nemotron NIM and Nemotron Zen are one family); every other model is its own
  family. `code-reviewer`, `security-reviewer`, and `final-verifier` follow the
  same separation rule. `ladder.py --pick-critic` prints the first eligible
  rung and refuses to pick when `author_models` is missing.

## 7. State

- `.orchestrator/STATE.md` in the project under work records the current task,
  decisions, files changed, and `author_models` — every model that edited the
  diff, appended as it happens.
- `.orchestrator/worker-state/` exists for the legacy GO worker. The directory
  name is historical and kept deliberately.
- Before re-dispatching a role that failed mid-edit, inspect `STATE.md` and the
  working diff to avoid duplicate or conflicting changes; keep retries bounded.

## 8. Logging

- Every dispatch goes through `lib/log-rung.py` (role, rung, model, outcome,
  ms) into `lib/rung-log.jsonl`.
- A failed chain is recorded with `lib/log-agent-issue.py --role <role>
  --session-id <id> --rungs-tried "1:<model>:<outcome>,..." --final-outcome
  <result> --notes "..."`, which writes `agents-issue/ISSUES.md` in the
  archive repo and pushes it.

## 9. Session archive

One private git repo, `sulav-global-sessions`, holds every session transcript
with a subfolder per project. `DISPATCHES.md` and `agents-issue/ISSUES.md` are
single files at its root, shared across projects. A `session-archive` plugin
decides when to archive; the Python worker renders and routes the transcript,
redacting credential-shaped strings. Archives are text-only: tool calls and
tool output are not stored.

## 10. Config surface

| Key | What it does |
| :--- | :--- |
| `plugins` | V2 key. Pins Superpowers at v7.0.1. `opencode plugin list` shows `session-archive` and `superpowers`. |
| `agents.explore.disabled` | Disables the built-in `explore` agent; `explorer` replaces it. |
| `permission.subagent` (mastermind) | `"*": deny`, then 14 role allows and `*-2` / `*-3` / `*-last` globs. Last matching rule wins. |
| `color` (per agent) | Role identity color in `#RRGGBB` hex, required by v2.2.0; theme names are dropped. |
| `provider` | Route definitions (nvidia, nvidia_secondary, groq, mistral, openrouter) used by the chains. |

## 11. Tests

```bash
python3 -m unittest discover -s tests -t .
```

35 offline tests: ladder walk and exit codes, health-registry cooldowns,
critic family selection, author_models parsing, generator drift and hidden
marking, agent colors, the mastermind dispatch whitelist, and the V2 config
shape. Tests write only to temp health/log paths (`MODEL_HEALTH_FILE`,
`RUNG_LOG_FILE`), make no network calls, and call no models. The 21 shell
tests in `tests/` belong to the legacy GO worker and are not part of this
suite.

## 12. Install

`install/install.sh` installs `config/live/{opencode.json, chains.json,
AGENTS.md, provider-routes.json, orchestrator-policy.json}` and every
`config/live/agents/*.md` into `~/.config/opencode`, plus the worker binaries.
Flags: `--dry-run`, `--force`, `--config-dir`.

## 13. Known limitations

1. OpenCode 2.2.0's @ autocomplete shows no description for agents, only
   skills. The "TBRN's <Role>" brand therefore appears in the Tab picker and
   the subagent list, not in `@`.
2. `opencode-validate-pins` reports 56/56 agents "unlabelled" under the default
   public pin ceiling, while exiting 0.
3. The GO worker (`go/`, `scripts/opencode-worker`, `lib/classify.sh`,
   `lib/policy.py`) is legacy and pending removal. It is not the current
   routing layer.
4. The 21 shell tests in `tests/` test that legacy worker.

## 14. Filesystem reference

| Path | Purpose |
| :--- | :--- |
| `~/.config/opencode/chains.json` | Single source of truth for role model chains and families. |
| `~/.config/opencode/AGENTS.md` | Global operating rules. |
| `~/.config/opencode/agents/` | 56 generated rung agents. |
| `~/.config/opencode/scripts/gen-fallbacks.py` | Rung generator (`--check` for drift). |
| `~/.config/opencode/scripts/ladder.py` | Dry-run ladder and critic picker. |
| `~/.config/opencode/lib/health.py` | Model health registry (`up`/`down`/`check`). |
| `~/.config/opencode/lib/model-health.json` | Cooldown state. |
| `~/.config/opencode/lib/log-rung.py` | Dispatch logger. |
| `~/.config/opencode/lib/rung-log.jsonl` | Dispatch history. |
| `~/.config/opencode/skills/tbrn/` | The @tbrn skill, routing, briefs, and ladder references. |

## 15. Day-to-day use

Start a session, press Tab to select a mastermind rung, state the goal. The
mastermind dispatches roles itself; users do not call subagents directly. A
user mentioning a role directly (`@coder`) still gets the same ladder: `coder`
fails over to `coder-2`, `-3`, `-last`, then `last_resort`.

Useful checks:

```bash
opencode debug agents                    # 56 TBRN agents; 61 entries incl. built-ins
python3 ~/.config/opencode/lib/health.py list   # models in cooldown
tail -20 ~/.config/opencode/lib/rung-log.jsonl  # recent dispatches
python3 ~/.config/opencode/scripts/gen-fallbacks.py --check
```

Final reports separate IMPLEMENTED / VERIFIED / NOT VERIFIED / REMAINING
ISSUES. VERIFIED is only for what a tester or critic actually ran.

## 16. Where the truth lives

`config/live/` in this repository mirrors the live install exactly.
`config/live/AGENTS.md` is the installed operating ruleset. `docs/SYSTEM-BRIEF.md`
(this file) is the current system reference. Anything not reflected there and
in `chains.json` should be treated as stale.
