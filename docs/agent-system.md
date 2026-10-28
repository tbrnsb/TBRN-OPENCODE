# Agent System

TBRN-OPENCODE-V2 defines 15 roles. `scripts/gen-fallbacks.py` expands them
into 56 agent files: 11 roles with 4 rungs, 4 roles (`security-reviewer`,
`final-verifier`, `ui-reviewer`, `general`) with 3 rungs and no `-3` file.

## Roles and colors

Every rung of a role carries that role's identity color. OpenCode v2.2.0
requires `#RRGGBB` hex; theme names are dropped by the schema.

| Role | Color | Mode | Purpose |
| :--- | :--- | :--- | :--- |
| `mastermind` | `#7C5CFF` | primary | Workflow owner. Sizes jobs, dispatches one subagent at a time, runs the ladder, keeps state. |
| `planner` | `#4C9AFF` | subagent | Read-only analyst. Writes ordered task plans only. |
| `coder` | `#2ECC71` | subagent | Implementation, refactors, fixes. |
| `tester` | `#27AE60` | subagent | Runs tests, builds, static analysis; may edit test files only. |
| `debugger` | `#F39C12` | subagent | Root-cause analysis before fixing. 3-loop cap. |
| `critic` | `#E74C3C` | subagent | Scores /10, read-only. Pass = 9/10. Family-separated from authors. |
| `designer` | `#9B59B6` | subagent | UI/UX specs, reviews screenshots and implemented UI. |
| `copywriter` | `#E67E22` | subagent | User-facing text (`*.md`, `*.txt`, `*.arb`) only. |
| `explorer` | `#1ABC9C` | subagent | Read-only repository investigation. Replaces the built-in `explore` agent. |
| `code-reviewer` | `#C0392B` | subagent | Independent diff review, read-only. |
| `archiver` | `#95A5A6` | subagent | Conventional commits, secret scan, session log. Mandatory before commits. |
| `security-reviewer` | `#D35400` | subagent | Diff audit for secrets, injection, unsafe IO, authz gaps. Read-only. |
| `final-verifier` | `#16A085` | subagent | Runs acceptance commands itself. Edit access denied. |
| `ui-reviewer` | `#8E44AD` | subagent | Checks screenshots and UI against the design spec. |
| `general` | `#34495E` | subagent | Multi-step work that fits no other role. |

Descriptions are branded `TBRN's <Role>.` so the setup is recognisable in the
Tab picker and the subagent list.

## Visible vs hidden

| Kind | Count | Where it appears |
| :--- | :--- | :--- |
| Base rung of each role | 15 | @ menu, agent picker, subagent list |
| Mastermind `-2`, `-3`, `-last` | 3 | Agent picker (all mastermind rungs are primary) |
| Other rungs (`<role>-2`, `-3`, `-last`) | 38 | Nowhere. `hidden: true`. |

Hidden rungs are dispatched by id, and only after the previous rung of that
role fails. The base rung always runs first. A 3-rung role has no `-3` file;
do not invent one.

## Dispatch whitelist

The mastermind's frontmatter carries `permission.subagent`. The v2.2.0
dispatch tool is named `subagent` (older notes call it Task):

```yaml
permission:
  subagent:
    "*": deny
    "planner": allow
    "coder": allow
    "tester": allow
    "debugger": allow
    "critic": allow
    "designer": allow
    "copywriter": allow
    "explorer": allow
    "code-reviewer": allow
    "archiver": allow
    "security-reviewer": allow
    "final-verifier": allow
    "ui-reviewer": allow
    "general": allow
    "*-2": allow
    "*-3": allow
    "*-last": allow
```

The deny comes first; the 14 role ids and the rung globs follow. Last matching
rule wins, so the mastermind can dispatch exactly the 56 generated agents and
nothing else. Rung globs cover the hidden fallbacks without listing every id.

## Per-role permission boundaries

Read-only roles (`planner`, `critic`, `code-reviewer`, `explorer`,
`security-reviewer`, `final-verifier`) deny `edit` and `bash` in frontmatter.
The planner gets a narrow `edit` exception so it can write its one
deliverable, `PLAN.md`. `tester` may edit only test files. `copywriter` may
edit only `*.md`, `*.txt`, `*.arb` and denies `bash`. Implementer roles
(`coder`, `debugger`, `archiver`) run with full tools.

## The built-in `explore` agent

`opencode.json` disables the built-in agent:

```json
"agents": { "explore": { "disabled": true } }
```

The TBRN `explorer` role replaces it, with its own chain and ladder behavior.

## Adding or changing roles

1. Edit the role's chain or add a role in `chains.json`.
2. Run `python3 scripts/gen-fallbacks.py` to regenerate all agents.
3. Confirm with `--check` ("in sync: 56 agents") and the offline test suite
   (`python3 -m unittest discover -s tests -t .`), which covers generator
   drift, colors, hidden marking, and the dispatch whitelist.
4. Update the mirror under `config/live/` so the repo matches the install.

Never hand-edit a generated file in `agents/`; the next generator run will
overwrite it.
