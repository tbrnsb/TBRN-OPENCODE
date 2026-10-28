# TBRN-OPENCODE

**TBRN-OPENCODE-V2** — a multi-agent orchestration layer for OpenCode v2.2.0.

One primary agent (**M.A.X.I.M.**) sizes every job and dispatches specialist subagents through per-role model failover ladders. Free models rate-limit, so the ladder retries once, cools the failing model down for ~10 minutes, and moves to the next rung.

> **Start here:** [SETUP.md](SETUP.md) — credentials, install, verification, troubleshooting.

## The Team (Marvel codenames in descriptions; internal IDs unchanged)

| Agent | Codename | Role |
|-------|----------|------|
| `mastermind` | **M.A.X.I.M.** — Master Agent for Xecutive Intelligence & Management | Only primary agent (Tab). Sizes jobs, dispatches one subagent at a time, runs the ladder, keeps state. |
| `planner` | **S.T.R.A.T.E.G.I.S.T.** — Systematic Task Roadmap & Action Tactician for Execution, Goals, Insights, & Sequenced Tracking | Read-only analyst. Writes ordered task plans; never product code. |
| `coder` | **C.I.P.H.E.R.** — Code Implementation & Programming Hierarchy Engine for Runtime | Implements features, refactors, fixes with known root cause. |
| `tester` | **V.A.L.I.D.A.T.O.R.** — Verification & Automated Logic Inspection Device for Accuracy & Test Output Review | Runs tests, builds, static analysis. Edits test files only. |
| `debugger` | **P.A.T.C.H.** — Programmatic Analyzer for Troubleshooting & Code Healing | Root cause before fix. Max 3 debug loops per issue. |
| `critic` | **J.U.D.G.E.** — Justified Unit & Design Guardian Evaluator | Scores work /10, read-only. Pass = 9/10. Max 3 rounds. |
| `designer` | **S.T.Y.L.I.S.T.** — Surface Token & You-Centered Layout Interface System Transformer | UI/UX specs and implemented-UI review. Reads screenshots. |
| `copywriter` | **I.N.K.** — Information, Narrative, & Knowledge-Crafter | User-facing text only (`*.md`, `*.txt`, `*.arb`). |
| `explorer` | **S.C.O.U.T.** — Search, Context, Overview, & Unstructured Terrain-Gatherer | Read-only repository investigation. Replaces built-in `explore`. |
| `code-reviewer` | **G.U.A.R.D.** — Git-level Unit & Architecture Refinement Directive | Independent diff review. Read-only. |
| `archiver` | **L.O.G.** — Local Output & Global-Memory Saver | Conventional commits, secret scan, session log. Mandatory before commits. |
| `security-reviewer` | — | Diff audit: secrets, injection, unsafe IO, authz gaps. Read-only. |
| `final-verifier` | — | Runs acceptance commands itself. Edit access denied. |
| `ui-reviewer` | — | Screenshots and implemented UI against the design spec. |
| `general` | — | Multi-step work that fits no other role. |

## Rungs & Visibility

`chains.json` assigns 3 or 4 models per role. `scripts/gen-fallbacks.py` generates:
- `<role>.md` (base), `<role>-2.md`, `<role>-3.md`, `<role>-last.md`
- 3-rung roles (`security-reviewer`, `final-verifier`, `ui-reviewer`, `general`) have no `-3`

**56 agents total**: 11 × 4 rungs + 4 × 3 rungs  
**18 visible**: 15 base rungs + mastermind's 3 fallbacks (primary, switched with Tab)  
**38 hidden**: dispatched by id only after the previous rung fails  
Never hand-edit generated files — edit `chains.json` and re-run the generator.  
Generated agents live in `config/live/agents/` (source of truth for installer).

## Dispatch & Ladder

- M.A.X.I.M. dispatches **one subagent at a time** via the `subagent` tool (v2.2.0 name).
- Its frontmatter carries `permission.subagent`: `"*": deny` first, then the 14 role ids + `*-2/3/last` globs. Last matching rule wins.
- On failure (429, timeout, empty, 5xx, garbage): retry once → mark model down 10 min in `lib/model-health.json` → next rung.
- After a role's rungs: global `last_resort` chain (3 models) → mastermind takeover.
- If mastermind's chain fails: writes `HANDOFF.md` and pauses.
- Caps: 3 debug loops/issue, 3 critic rounds (pass=9). Critic must differ by family from every model in `author_models` (Nemotron NIM/Zen = one family).
- Every dispatch logged by `lib/log-rung.py`; failed chains recorded by `lib/log-agent-issue.py`.

## Config Surface (v2.2.0 native keys)

| Key | Purpose |
|-----|---------|
| `plugins` | V2 key. Pins Superpowers v7.0.1 + session-archive. |
| `agents.explore.disabled` | Disables built-in `explore`; `explorer` replaces it. |
| `color: "#RRGGBB"` | Required by v2.2.0 on every agent; theme names dropped. |
| `permission.subagent` | Mastermind whitelist (deny-first, last-match-wins). |
| `chains.json` | Single source of truth for every role's model chain. |

## Where Colors Appear

Agent `color: "#RRGGBB"` (in frontmatter) renders in:
- **Tab agent picker** — press Tab in OpenCode TUI to switch primary agents (M.A.X.I.M. rungs)
- **Task tool list** — when the mastermind dispatches subagents

**Not in `@` menu** — OpenCode v2.2.0 shows only agent IDs in `@` autocomplete (no descriptions, no colors). Core limitation.

## Notifications & Archiver

- **cc-alerts** (`lib/cc-alerts.py`, `lib/driver.py`) — ntfy.sh push notifications on session end/handoff
- **session-archive** plugin (`lib/session-archive.py`) — auto-archives every session to a private git repo
- **Archiver agent** (`L.O.G.`) — conventional commits, secret-scan, PR creation via `gh`

## Browser / Web

**Not enabled.** Built-in browser skill explicitly denied (`"browser": "*": "deny"` in all agent permissions). Use `websearch` tool or custom skill if needed.

## Install

```bash
./install/install.sh --dry-run   # preview
./install/install.sh             # install with confirmation
./install/install.sh --force     # skip prompts
```

Installs `config/live/{opencode.json,chains.json,AGENTS.md,orchestrator-policy.json}` + all `config/live/agents/*.md` into `~/.config/opencode`, plus worker binaries to `~/.local/bin`. `--config-dir` overrides target.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

35 offline tests: rung ladder, health registry, critic family selection, generator drift, agent colors, dispatch whitelist, V2 config shape. Temp health/log paths, no network, no model calls.

## Quick Verify

```bash
opencode debug agents                    # 61 entries (56 TBRN + 5 builtins), no 'explore'
opencode plugin list                     # session-archive, superpowers
python3 ~/.config/opencode/scripts/gen-fallbacks.py --check
# → "in sync: 56 agents match chains.json"
```

## Limitations

- OpenCode 2.2.0's `@` autocomplete shows no description for agents (only skills). Marvel codenames appear in Tab picker and Task list, not in `@`.
- `opencode-validate-pins` reports 56/56 "unlabelled" under the default public ceiling (exits 0).
- The GO worker (`scripts/opencode-worker`, `lib/classify.sh`, `lib/policy.py`) is legacy and not the current routing layer.
- 21 shell tests in `tests/` (not present in this repo) test the legacy worker.

## License

MIT — see `LICENSE`.
