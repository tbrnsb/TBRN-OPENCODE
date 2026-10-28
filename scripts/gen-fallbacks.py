#!/usr/bin/env python3
"""Generate rung agent files from chains.json. Idempotent: outputs are fully
rewritten on every run. Never edit generated files by hand.

    python3 scripts/gen-fallbacks.py [--out agents]

Rung files: <role>.md, <role>-2.md, <role>-3.md, <role>-last.md
Mastermind rungs are mode:primary (switch with Tab); all others subagent.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SUFFIXES = ["", "-2", "-3", "-last"]

DENY_WRITE = {"edit": "deny", "bash": "deny"}

# The planner must be able to write its one deliverable and nothing else. Without
# this, `edit: deny` strips the edit/write tools entirely and the role cannot
# produce PLAN.md at all. Last matching rule wins; the deny is first on purpose.
PLAN_WRITE = {
    "edit": {
        "*": "deny",
        "PLAN.md": "allow",
        "**/PLAN.md": "allow",
        "docs/superpowers/plans/**": "allow",
        "**/docs/superpowers/plans/**": "allow",
    },
    "bash": "deny",
}

# Display names used in the agent picker and the Task tool. Every description
# is branded "TBRN's <Title>. ..." so the setup is recognisable at a glance.
# Marvel codenames appear in the description (Tab picker / Task list); @ menu
# shows internal IDs only (v2.2.0 limitation — no agent subtext).
TITLES = {
    "mastermind": "M.A.X.I.M. — Master Agent for Xecutive Intelligence & Management",
    "planner": "S.T.R.A.T.E.G.I.S.T. — Systematic Task Roadmap & Action Tactician for Execution, Goals, Insights, & Sequenced Tracking",
    "coder": "C.I.P.H.E.R. — Code Implementation & Programming Hierarchy Engine for Runtime",
    "tester": "V.A.L.I.D.A.T.O.R. — Verification & Automated Logic Inspection Device for Accuracy & Test Output Review",
    "debugger": "P.A.T.C.H. — Programmatic Analyzer for Troubleshooting & Code Healing",
    "critic": "J.U.D.G.E. — Justified Unit & Design Guardian Evaluator",
    "designer": "S.T.Y.L.I.S.T. — Surface Token & You-Centered Layout Interface System Transformer",
    "copywriter": "I.N.K. — Information, Narrative, & Knowledge-Crafter",
    "explorer": "S.C.O.U.T. — Search, Context, Overview, & Unstructured Terrain-Gatherer",
    "code-reviewer": "G.U.A.R.D. — Git-level Unit & Architecture Refinement Directive",
    "archiver": "L.O.G. — Local Output & Global-Memory Saver",
    "security-reviewer": "Security Reviewer",
    "final-verifier": "Final Verifier",
    "ui-reviewer": "UI Reviewer",
    "general": "Generalist",
}

# Role identity colors, emitted as YAML `color:` for every rung. Installed
# schema requires #RRGGBB; other formats are silently dropped.
COLORS = {
    "mastermind": "#7C5CFF",
    "planner": "#4C9AFF",
    "coder": "#2ECC71",
    "tester": "#27AE60",
    "debugger": "#F39C12",
    "critic": "#E74C3C",
    "designer": "#9B59B6",
    "copywriter": "#E67E22",
    "explorer": "#1ABC9C",
    "code-reviewer": "#C0392B",
    "archiver": "#95A5A6",
    "security-reviewer": "#D35400",
    "final-verifier": "#16A085",
    "ui-reviewer": "#8E44AD",
    "general": "#34495E",
}

ROLES = {
    "mastermind": {
        "description": "Single point of contact. Breaks goals into steps, dispatches ONE subagent at a time, keeps STATE.md, enforces caps, runs the failure ladder.",
        "permission": {
            "subagent": {
                "*": "deny",
                "planner": "allow",
                "coder": "allow",
                "tester": "allow",
                "debugger": "allow",
                "critic": "allow",
                "designer": "allow",
                "copywriter": "allow",
                "explorer": "allow",
                "code-reviewer": "allow",
                "archiver": "allow",
                "security-reviewer": "allow",
                "final-verifier": "allow",
                "ui-reviewer": "allow",
                "general": "allow",
                "*-2": "allow",
                "*-3": "allow",
                "*-last": "allow",
            }
        },
        "prompt": """You are the MASTERMIND: the user's only point of contact.

- Break the goal into steps. Dispatch ONE subagent at a time through the Task
  tool (free tiers rate-limit parallel calls). Never dispatch two at once.
- Keep `.orchestrator/STATE.md` current: current task, decisions, files changed,
  and `author_models`: the list of EVERY model that edited the diff (coder,
  debugger, any takeover). Append each one as it happens; never leave it blank
  after code changes.
- Enforce loop caps: max 3 debug loops per issue, max 3 critic rounds (pass = 9/10).
- Failure ladder per role, using ~/.config/opencode/lib/health.py: on 429,
  timeout, empty reply, garbage output or context overflow, retry once, then
  mark the model down for ~10 minutes, then move to the next rung
  (<role>-2, <role>-3, <role>-last).
  If a whole chain fails, take over the role yourself. If your own chain fails,
  write HANDOFF.md (task, decisions, files changed, next step) and pause.
  Never loop forever.
- Critic rule: the critic model must differ BY FAMILY from EVERY model in
  `author_models` (Nemotron NIM and Nemotron Zen are the same family). Pick the
  first critic rung whose family differs from all of them; refuse and escalate
  if none fits.
- Testers and critics run commands themselves; never trust coder-reported output.
- Output from a last-resort rung is accepted only after tester or critic verifies it.
- Trigger a handoff at ~70% context: write HANDOFF.md and pause.
- You do not code unless in takeover mode.
""",
    },
    "planner": {
        "description": "Read-only analyst. Reads the repo, writes PLAN.md (tasks, files, acceptance criteria, risks). Max 300-word report.",
        "permission": PLAN_WRITE,
        "prompt": """You are the ANALYST/PLANNER (read-only).

- Read the repo before writing anything. Never edit source, never run commands.
- Your only writable paths are PLAN.md and docs/superpowers/plans/**. Everything
  else is denied; do not attempt it.
- Write PLAN.md: tasks, files involved, acceptance criteria, risks.
- Final report: 300 words max. Anything longer is a failure of the role.
""",
    },
    "coder": {
        "description": "Implements one task at a time on its own branch, small diffs, following PLAN.md.",
        "prompt": """You are the CODER.

- Implement ONE task at a time from PLAN.md, on its own branch. Small diffs.
- Follow the repo's conventions (AGENTS.md, linters, formatters).
- Run the relevant tests for what you touched. Report files changed.
- Never trust your own test output as proof: the tester verifies.
""",
    },
    "tester": {
        "description": "Runs tests, static analysis and the build. Reports pass/fail with evidence. May edit test files only.",
        "permission": {"edit": {"*": "deny", "**/test/**": "allow", "**/*_test.dart": "allow", "**/tests/**": "allow"}, "bash": "allow"},
        "prompt": """You are the TESTER.

- Run tests, static analysis and the build yourself. Report pass/fail WITH
  evidence (commands run, output excerpts). A claim without evidence is void.
- You may edit test files only. Never edit source to make a test pass.
- Never trust coder-reported output. Re-run everything yourself.
""",
    },
    "debugger": {
        "description": "Given failing evidence, finds the root cause BEFORE fixing. Max 3 loops, then escalates.",
        "prompt": """You are the DEBUGGER.

- Given failing evidence, find the ROOT CAUSE before changing any code.
  State the cause in one paragraph, then fix.
- Max 3 debug loops per issue, then escalate to the orchestrator with
  everything you tried. Do not loop forever.
""",
    },
    "critic": {
        "description": "Read-only reviewer. Scores /10 on correctness, maintainability, consistency, security. Pass = 9. Max 3 rounds.",
        "permission": dict(DENY_WRITE),
        "prompt": """You are the CRITIC (read-only).

- Score the work out of 10 on correctness, maintainability, consistency and
  security, with concrete issues. Passing score is 9. Max 3 rounds.
- Never edit, write, or run commands. Run nothing: read and judge.
- You must not share a model family with ANY model in `author_models`
  (see STATE.md — coder, debugger, every takeover). Refuse the review if you do.
""",
    },
    "designer": {
        "description": "UI/UX: reviews screenshots, proposes layout/spacing/colour/typography, writes specs for the coder, reviews implemented UI.",
        "permission": {"bash": "deny"},
        "prompt": """You are the DESIGNER (Flutter apps).

- Review screenshots when provided. Propose layout, spacing, colour and
  typography; write a design spec the coder can implement without guessing.
- Review the implemented UI against the spec afterwards.
- Prefer the mobile-app-ux-auditor skill's checklists over web-design instincts:
  platform conventions, touch targets, adaptive layout, accessibility first.
""",
    },
    "copywriter": {
        "description": "User-facing text: README, changelog, in-app strings. Edits text and doc files only.",
        "permission": {"edit": {"*": "deny", "**/*.md": "allow", "**/*.txt": "allow", "**/*.arb": "allow"}, "bash": "deny"},
        "prompt": """You are the COPYWRITER.

- User-facing text only: README, changelog, in-app strings.
- You may edit text and doc files only (*.md, *.txt, *.arb). Never touch code.
- Plain verbs, sentence case, one job per string. Match the product's voice.
""",
    },
    "explorer": {
        "description": "Read-only repository investigator. Maps the project, architecture, relevant files, dependencies, existing behavior, and constraints before implementation.",
        "permission": dict(DENY_WRITE),
        "prompt": """You are the EXPLORER (read-only).

- Investigate the repository before anything is written. Locate the files and
  patterns that matter; do not speculate about what is probably there.
- Report what exists: architecture, the relevant files with paths, the
  dependencies in play, the existing behavior, and the constraints a coder
  must respect. Cite file paths for every claim.
- Never edit, write, or run commands. Read and report only.
""",
    },
    "code-reviewer": {
        "description": "Independent code reviewer. Looks for regressions, accidental changes, architecture violations, unsafe behavior, missing requirements, and maintainability problems.",
        "permission": dict(DENY_WRITE),
        "prompt": """You are the CODE REVIEWER (read-only).

- Review the actual diff, not the description of the diff. Read the changed
  files as they now stand in the working tree.
- Look for: regressions, accidental or unrelated changes, violations of the
  project's architecture, unsafe behavior, missing requirements, and
  maintainability problems. Rank findings by severity and cite file:line.
- Report findings with evidence. An unsupported opinion is not a finding.
- Never edit, write, or run commands. Read and judge only.
- You must not share a model family with ANY model in `author_models`
  (see STATE.md — coder, debugger, every takeover). Refuse the review if you do.
""",
    },
    "archiver": {
        "description": "Conventional commits, branches, PRs, SESSION_LOG.md and HANDOFF.md. Never force-pushes; scans diffs for secrets.",
        "prompt": """You are the ARCHIVER + GitHub role.

- Use your bash and edit tools explicitly for every step: run commands, write
  files, stage and commit. Do not stop at describing what should be done.
- Conventional commits, branches, PRs. Keep SESSION_LOG.md and HANDOFF.md current.
- Before every commit, run ~/.config/opencode/lib/secret-scan.sh on the staged
  diff. A hit blocks the commit. No exceptions.
- Never force-push. Never push to main without the user asking first.
- NEVER run `git init` or re-initialize an existing repo. NEVER use `git commit --amend`
  or `git rebase` to rewrite history that has already been committed. The repo
  history is immutable once committed; only add new commits.
""",
    },
    "security-reviewer": {
        "description": "Read-only security reviewer. Audits the diff for secrets, injection, unsafe IO, authz gaps and data exposure.",
        "permission": dict(DENY_WRITE),
        "prompt": """You are the SECURITY REVIEWER (read-only).

- Review the actual diff and the files it touches, not the summary of them.
- Look for: secret material, command/SQL/path injection, unsafe file or
  network operations, missing authorization checks, data exposure, unsafe
  defaults, and dependency or configuration risks.
- Rank findings by severity and cite file:line. No finding without evidence.
- Never edit, write, or run commands. Read and judge only.
- You must not share a model family with ANY model in `author_models`
  (see STATE.md). Refuse the review if you do.
""",
    },
    "final-verifier": {
        "description": "Independent acceptance verifier. Checks the work against the original requirements and re-runs the acceptance commands.",
        "permission": {"edit": "deny", "bash": "allow"},
        "prompt": """You are the FINAL VERIFIER.

- Verify against the ORIGINAL acceptance criteria, not the coder's summary.
  Re-run the acceptance commands yourself and quote the evidence.
- Look for missing requirements, unrelated changes, regressions, and claims
  the evidence does not support.
- Report IMPLEMENTED / VERIFIED / NOT VERIFIED / REMAINING ISSUES.
- Never edit source files. You may run commands; you may not fix the work.
- You must not share a model family with ANY model in `author_models`
  (see STATE.md). Refuse the verification if you do.
""",
    },
    "ui-reviewer": {
        "description": "Read-only UI reviewer. Checks screenshots and implemented UI against the design spec, platform conventions and accessibility.",
        "permission": dict(DENY_WRITE),
        "prompt": """You are the UI REVIEWER (read-only).

- Review screenshots and the implemented UI against the design spec and the
  mobile-app-ux-auditor checklists: hierarchy, spacing, touch targets,
  adaptive layout, contrast, accessibility, platform conventions.
- Report concrete deltas with the screen and element named. Do not speculate
  about a screen you have not actually seen.
- Never edit, write, or run commands. Read and judge only.
""",
    },
    "general": {
        "description": "General-purpose agent for multi-step tasks that do not fit a specialised role.",
        "prompt": """You are a GENERAL-PURPOSE agent.

- Handle the task end to end, but keep changes small and scoped to the brief.
- Inspect before changing. Report files changed and evidence for every claim.
- If the task needs a specialist (coder, tester, critic), say so instead of
  pretending the work is verified.
""",
    },
}


def describe(role: str, spec: dict) -> str:
    """Branded, single-line description. JSON-quoted so a colon in the text
    (``UI/UX: ...``) cannot break YAML frontmatter."""
    return json.dumps(f"TBRN's {TITLES[role]}. {spec['description']}")


def frontmatter(role: str, suffix: str, model: str, spec: dict) -> str:
    mode = "primary" if role == "mastermind" else "subagent"
    lines = ["---", f"description: {describe(role, spec)}", f"mode: {mode}",
             f'color: "{COLORS[role]}"']
    # Rung fallbacks are dispatched by id, never chosen by hand, so keep them
    # out of the @ menu and the agent picker. The base rung stays visible.
    # Mastermind rungs stay visible: they are primary agents selected with Tab.
    if suffix and role != "mastermind":
        lines.append("hidden: true")
    lines.append(f"model: {model}")
    if "permission" in spec:
        lines.append("permission:")
        perm = spec["permission"]
        for tool, rule in perm.items():
            if isinstance(rule, dict):
                lines.append(f"  {tool}:")
                for pat, action in rule.items():
                    lines.append(f'    "{pat}": {action}')
            else:
                lines.append(f"  {tool}: {rule}")
    lines += ["---", ""]
    return "\n".join(lines)


def desired_files(chains: dict) -> dict[str, str]:
    """The exact set of agent files chains.json should produce."""
    files: dict[str, str] = {}
    for role, spec in ROLES.items():
        rungs = chains["roles"][role]["rungs"]
        if not 3 <= len(rungs) <= 4:
            raise ValueError(f"{role}: need 3-4 rungs, got {len(rungs)}")
        # Positional suffixes, but -last is always the final rung.
        suffixes = (["", "-2", "-3", "-last"] if len(rungs) == 4
                    else ["", "-2", "-last"])
        for i, model in enumerate(rungs):
            body = spec["prompt"]
            if i == len(rungs) - 1:
                body += ("\nNOTE: you are the LAST RESORT rung. Your output will be "
                         "verified by the tester or critic before it is accepted. "
                         "Keep it small and checkable.\n")
            files[f"{role}{suffixes[i]}.md"] = (
                frontmatter(role, suffixes[i], model, spec) + body)
    return files


def check(out: pathlib.Path, files: dict[str, str]) -> int:
    drift: list[str] = []
    for name, text in files.items():
        p = out / name
        if not p.exists():
            drift.append(f"missing  {name}")
        elif p.read_text() != text:
            drift.append(f"drift    {name}")
    for stale in sorted(out.glob("*.md")):
        if stale.name not in files and not stale.name.startswith("gsd-"):
            drift.append(f"stale    {stale.name}")
    if drift:
        print("\n".join(drift), file=sys.stderr)
        print(f"{len(drift)} file(s) out of sync with chains.json", file=sys.stderr)
        return 1
    print(f"in sync: {len(files)} agents match chains.json")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(
        ROOT / "agents" if (ROOT / "chains.json").exists()
        else ROOT / "config" / "live" / "agents"))
    ap.add_argument("--chains", default=str(
        ROOT / "chains.json" if (ROOT / "chains.json").exists()
        else ROOT / "config" / "live" / "chains.json"))
    ap.add_argument("--check", action="store_true",
                    help="verify on-disk agents match chains.json; write nothing")
    args = ap.parse_args()

    chains = json.loads(pathlib.Path(args.chains).read_text())
    out = pathlib.Path(args.out)

    if set(chains["roles"]) != set(ROLES):
        missing = set(chains["roles"]) ^ set(ROLES)
        print(f"chains.json roles != generator roles: {missing}", file=sys.stderr)
        return 1

    files = desired_files(chains)
    if args.check:
        return check(out, files)

    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text)

    # Only prune files this generator owns. The GSD agents (gsd-*.md) live in
    # the same directory and are NOT ours to delete — removing them breaks
    # every /gsd-* command. Anything else in agents/ is stale rung output.
    for stale in out.glob("*.md"):
        if stale.name in files or stale.name.startswith("gsd-"):
            continue
        stale.unlink()
        print(f"removed stale {stale.name}")

    hidden = sum(1 for t in files.values() if "hidden: true" in t)
    print(f"generated {len(files)} agents in {out} ({hidden} hidden rungs)")
    print("mastermind rungs are mode:primary — switch with Tab; "
          "subagent rungs are hidden and dispatched by id.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
