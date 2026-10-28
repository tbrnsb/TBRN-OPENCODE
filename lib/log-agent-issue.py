#!/usr/bin/env python3
"""
log-agent-issue.py — record a failed dispatch with full ladder pathway into
the sulav-global-sessions agents-issue log.

This is the ORCHESTRATOR-side complement to the worker's automatic error
detection. The worker logs what the session's messages show (models/tools/
errors); this script logs what the LADDER decided (which rungs were tried,
health transitions, cooldown state) — information only the orchestrator has.

Usage:
    python3 ~/.config/opencode/lib/log-agent-issue.py \
        --role coder \
        --session-id ses_xyz123 \
        --rungs-tried "1:google/gemini-3.5-flash:quota_exceeded,2:opencode/mimo-v2.6-flash-free:free_tier_rejected,3:mistral/codestral-latest:garbage" \
        --final-outcome "chain_exhausted_takeover" \
        --notes "chain exhausted; mastermind (agent) took over"

Environment:
    AGENT_ISSUE_REPO   defaults to ~/.local/share/session-archive/sulav-global-sessions
    AGENT_ISSUE_PUSH   set to "0" to skip git push (default: push)
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_DEFAULT = Path.home() / ".local" / "share" / "session-archive" / "sulav-global-sessions"
ISSUES_FILE = "agents-issue/ISSUES.md"

def redact(text: str) -> str:
    """Redact credential-shaped strings (from session-archive.py)."""
    patterns = [
        (r'(?i)(api[_-]?key|secret|token|password|auth|bearer)[\s:=]+[\w\-]{16,}', r'\1=***REDACTED***'),
        (r'(?i)(sk-|ghp_|gho_|ghu_|ghs_|ghr_)[\w\-]{20,}', '***REDACTED***'),
        (r'[A-Za-z0-9+/]{40,}={0,2}', '***REDACTED***'),  # base64-ish
    ]
    out = text
    for pat, repl in patterns:
        out = re.sub(pat, repl, out)
    return out

def ensure_repo(repo: Path) -> None:
    issues_dir = repo / "agents-issue"
    issues_dir.mkdir(parents=True, exist_ok=True)
    issues_file = repo / ISSUES_FILE
    if not issues_file.exists():
        issues_file.write_text("# Agents Issue Log\n\n")

def append_entry(repo: Path, entry: str) -> None:
    (repo / ISSUES_FILE).write_text((repo / ISSUES_FILE).read_text() + entry + "\n")

def git_commit_push(repo: Path, message: str, push: bool) -> None:
    if push:
        subprocess.run(["git", "add", "-A"], cwd=repo, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "push", "-q"], cwd=repo, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def main():
    p = argparse.ArgumentParser(description="Log a failed agent dispatch with ladder pathway")
    p.add_argument("--role", required=True, help="Role name (e.g., coder, copywriter)")
    p.add_argument("--session-id", required=True, help="OpenCode session ID")
    p.add_argument("--rungs-tried", required=True, help="Comma-separated rung:model:outcome (e.g., 1:model:429,2:model:ok)")
    p.add_argument("--final-outcome", required=True, help="Overall result (e.g., chain_exhausted_takeover, rung2_ok)")
    p.add_argument("--notes", default="", help="Free-form notes")
    p.add_argument("--repo", default=str(REPO_DEFAULT), help="Archive repo path")
    p.add_argument("--no-push", action="store_true", help="Do not git push")
    args = p.parse_args()

    repo = Path(args.repo).expanduser()
    if not repo.exists():
        print(f"Repository not found: {repo}", file=sys.stderr)
        sys.exit(1)

    ensure_repo(repo)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rungs_lines = []
    for part in args.rungs_tried.split(","):
        part = part.strip()
        if not part:
            continue
        # format: rung_num:model:outcome
        try:
            rung_num, model, outcome = part.split(":", 2)
            rungs_lines.append(f"- Rung {rung_num}: `{model}` → {outcome}")
        except ValueError:
            rungs_lines.append(f"- {part}")

    entry = f"""## {ts} — {args.role} (subagent) {args.final_outcome}
- **Session**: `{args.session_id}`
- **Role**: {args.role} (subagent)
- **Rungs tried**:
{chr(10).join(rungs_lines)}
- **Final outcome**: {args.final_outcome}
- **Notes**: {args.notes}
"""
    entry = redact(entry)
    append_entry(repo, entry)

    push = not args.no_push and os.environ.get("AGENT_ISSUE_PUSH") != "0"
    if push:
        git_commit_push(repo, f"chore: issue log {args.role} {args.session_id}", push)

    print(f"Logged to {repo / ISSUES_FILE}")

if __name__ == "__main__":
    main()