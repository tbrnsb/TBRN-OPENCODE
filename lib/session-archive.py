#!/usr/bin/env python3
"""
Archive OpenCode sessions to ONE private git repo, per-project subfolders.

Reads the OpenCode SQLite database READ-ONLY and writes one Markdown file per
session, text-only (no tool calls, no tool output), chronologically ordered.

Layout — a single git repository (`sulav-global-sessions`) holds everything;
per-project content is a subfolder of it (REPO_TO_SUBFOLDER):

    sulav-global-sessions/                      <- git root (git ops, cwd)
    ├── sessions/YYYY/MM/*.md                   <- global sessions
    ├── daily-companion/sessions/YYYY/MM/*.md
    ├── runner-audio-app/sessions/YYYY/MM/*.md
    ├── opencode-global-orchestrator/sessions/YYYY/MM/*.md
    ├── DISPATCHES.md                           <- shared, repo root only
    └── agents-issue/ISSUES.md                  <- shared, repo root only

Git root and content root are deliberately different paths: `git_root()` is
the only directory a git command ever runs in, `content_dir()` is where a
logical repo's transcripts land.

Security model — two independent layers, because a leak here is permanent:
  1. Only `text` parts are read. Tool output is never touched, so the 91% of
     transcript bulk that contained command output cannot leak.
  2. Every rendered line passes through SECRET_PATTERNS. Anything matching is
     replaced with a redaction marker before it ever reaches disk.

Routing:
  Own directory wins. When a session's launch directory is uninformative
  (GSD launches subagents from $HOME), the parent session's repo is inherited.
  Content-frequency scoring was tried and rejected — see resolve_repo_full.

Subagent sessions (parent_id set) do NOT get their own transcript file: one
file + one `chore: archive` commit per Task dispatch produced 61+ commits of
churn in sulav-global-sessions alone. Instead one line is appended to
DISPATCHES.md at the SINGLE archive repo root (shared by every project), plus
— when the session shows errors — a detail block in agents-issue/ISSUES.md,
also at that one root. Neither is committed here; the parent session's own
`git add -A && commit && push` sweeps them in. Both appends are idempotent
per session id, because session.idle fires repeatedly.

Usage:
  session-archive.py backfill [--limit N] [--dry-run] [--force]
  session-archive.py archive-one <session-id> [--no-push]
  session-archive.py route <session-id>     # show which repo, and why
  session-archive.py index                  # rebuild INDEX.md + MEMORY.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.environ.get("OPENCODE_DB", Path.home() / ".local/share/opencode/opencode.db"))
ARCHIVE_ROOT = Path(
    os.environ.get("SESSION_ARCHIVE_ROOT") or (Path.home() / ".local/share/session-archive")
)

# project_id (from the `project` table) -> logical repo name. A fallback only:
# project_id changes when a repo moves, so paths are the primary signal.
PROJECT_ID_TO_REPO = {
    "8d4d68382bebd47df9f32185b7e659fc31559003": "daily-companion-sessions",
    "f050d1d283e9c1f4401c6ffb3c20176f18d25077": "opencode-global-orchestrator-sessions",
}
GLOBAL_REPO = "sulav-global-sessions"

# The archive is ONE git repo; the four former per-project repos are now
# subfolders of it. Routing still names logical repos (the "-sessions"
# spellings above) because every GLOBAL_REPO comparison in this file is a
# comparison of those names — only the on-disk destination changed.
GIT_REPO = "sulav-global-sessions"
REPO_TO_SUBFOLDER = {
    "daily-companion-sessions": "daily-companion",
    "runner-audio-app-sessions": "runner-audio-app",
    "opencode-global-orchestrator-sessions": "opencode-global-orchestrator",
    "sulav-global-sessions": "",  # global sessions stay at the repo root
}

# Both current and pre-2026-10-05 paths are listed so historical sessions and
# sessions in a moved repo both route correctly.
DIRECTORY_PREFIX_TO_REPO = {
    str(Path.home() / "Projects/runner_audio_app"): "runner-audio-app-sessions",
    str(Path.home() / "runner_audio_app"): "runner-audio-app-sessions",
    str(Path.home() / "Projects/opencode-global-orchestrator"): "opencode-global-orchestrator-sessions",
    str(Path.home() / "opencode-global-orchestrator"): "opencode-global-orchestrator-sessions",
    str(Path.home() / "Documents/flutter_application_1"): "daily-companion-sessions",
    str(Path.home() / "Documents/dc-scrub"): "daily-companion-sessions",
    # ui-ux-pro-max-skill deliberately absent: no sessions repo exists, so its
    # sessions fall through to global rather than inventing a destination.
}

# Project path markers used to attribute a session by its task prompt.
# dc-scrub is the scrubbed public mirror of the same product, so it maps to
# the same sessions repo as the dev repo.
PROMPT_MARKERS_TO_REPO = {
    "flutter_application_1": "daily-companion-sessions",
    "dc-scrub": "daily-companion-sessions",
    "opencode-global-orchestrator": "opencode-global-orchestrator-sessions",
    "runner_audio_app": "runner-audio-app-sessions",
}

# An idle can fire before the first message is persisted; archiving then would
# write a transcript stub a later turn has to overwrite.
MIN_MESSAGES_FOR_ARCHIVE = 1

# ── Secret redaction ────────────────────────────────────────────────────────
# Matched against the VALUE, not the variable name. An earlier version matched
# `KEY=` and missed `KEY_RUNNER=`, printing live NVIDIA keys into a transcript.
SECRET_PATTERNS = [
    (re.compile(r"nvapi-[A-Za-z0-9_\-]{16,}"), "[REDACTED:nvidia-api-key]"),
    (re.compile(r"AQ\.Ab[A-Za-z0-9_\-]{16,}"), "[REDACTED:gemini-api-key]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED:sk-token]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED:github-pat]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-pat]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-access-key]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[REDACTED:google-api-key]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED:private-key]"),
]
REDACTION_MARKER = "[REDACTED"


def redact(text: str) -> tuple[str, int]:
    """Replace secret-looking values. Returns (clean_text, count)."""
    if not text:
        return text, 0
    count = 0
    for pattern, replacement in SECRET_PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


# ── DB access (read-only, always) ───────────────────────────────────────────
def connect() -> sqlite3.Connection:
    if not DB.exists():
        sys.exit(f"database not found: {DB}")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def git_root() -> Path:
    """The single git repository: the only directory git commands run in.

    Everything archived — every project's transcripts, the shared
    DISPATCHES.md and agents-issue/ISSUES.md — is committed from here.
    """
    return ARCHIVE_ROOT / GIT_REPO


def content_dir(repo_name: str) -> Path:
    """Where a logical repo's content lives inside the single git repository.

    Equals `git_root()` for the global repo (content at the repo root) and
    `git_root()/<subfolder>` for a project. An unknown name never invents a
    folder: it falls back to the global content and says so on stderr.
    """
    subfolder = REPO_TO_SUBFOLDER.get(repo_name)
    if subfolder is None:
        print(
            f"session-archive: unknown repo {repo_name!r}; "
            f"falling back to {GLOBAL_REPO} content at the repo root",
            file=sys.stderr,
        )
        return git_root()
    return git_root() / subfolder if subfolder else git_root()


def ensure_repo_scaffold(repo_name: str) -> None:
    """Create the content dirs for `repo_name`, plus the shared repo-root files."""
    content = content_dir(repo_name)
    (content / "sessions").mkdir(parents=True, exist_ok=True)
    # DISPATCHES.md and issue detail blocks are SINGLE files at the single
    # git root — one log shared by every project, not one per subfolder.
    root = git_root()
    (root / "agents-issue").mkdir(parents=True, exist_ok=True)
    keep = root / ".gitignore"
    if not keep.exists():
        keep.write_text(
            "# Raw exports stay out of git: large, and they contain tool output.\n"
            "raw/\n*.json\n*.jsonl\n",
        )


def _row_get(row, key, default=None):
    """sqlite3.Row supports mapping access; plain dicts used in tests do too."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


# ── Routing ─────────────────────────────────────────────────────────────────
def resolve_repo(row) -> str:
    """Directory-based routing. Authoritative when the directory is meaningful."""
    directory = _row_get(row, "directory") or ""
    # Longest prefix wins, so a nested path is not shadowed by a shorter one.
    for prefix in sorted(DIRECTORY_PREFIX_TO_REPO, key=len, reverse=True):
        if directory == prefix or directory.startswith(prefix + os.sep):
            return DIRECTORY_PREFIX_TO_REPO[prefix]
    project_id = _row_get(row, "project_id")
    if project_id in PROJECT_ID_TO_REPO:
        return PROJECT_ID_TO_REPO[project_id]
    return GLOBAL_REPO


def repo_from_parent_chain(conn: sqlite3.Connection, row) -> str | None:
    """Attribute a session by walking up to its parent session.

    GSD launches subagents from $HOME, so a UI audit of the Flutter app was
    filed as global work purely because of the launch directory. The parent
    session records the real project, so its resolved repo is inherited.

    Content-frequency scoring was tried here and rejected: gsd subagents
    discuss the UI without naming the path (1 matching message), while a
    machine-wide audit names every project constantly (28/32/34) and got
    misfiled by a two-message margin. Directory plus parentage is unambiguous.

    Returns None when there is no informative ancestor, so the caller keeps its
    own fallback rather than guessing.
    """
    parent_id = _row_get(row, "parent_id")
    seen: set[str] = set()
    depth = 0
    while parent_id and depth < 10 and parent_id not in seen:
        seen.add(parent_id)
        parent = conn.execute(
            "SELECT id, project_id, directory, title, parent_id FROM session WHERE id = ?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            return None
        repo = resolve_repo(parent)
        if repo != GLOBAL_REPO:
            return repo
        parent_id = parent["parent_id"]
        depth += 1
    return None


# A task prompt that surveys many paths at once is not "about" any one of
# them. Machine-inventory prompts list every project, so whichever is named
# most often would otherwise win on volume alone. Detected structurally: three
# or more distinct project markers, or an explicit inventory framing.
SURVEY_MARKERS = (
    "inventory",
    "audit of ai",
    "all ai-coding-agent",
    "config sprawl",
    "every project",
    "all projects",
)
SURVEY_MIN_MARKERS = 3


def repo_from_task_prompt(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Attribute a session by the project named in its task prompt.

    Needed because the launch directory is unreliable: GSD launches subagents
    from $HOME, so a UI audit of the Flutter app looked like global work.

    Two earlier approaches failed and are worth recording:
      - Counting project-path mentions across the whole transcript scored a
        machine-wide inventory as Flutter work, because listing every directory
        mentions each project once.
      - Inheriting from the parent session found nothing to inherit: these
        subagents' parents are root sessions in $HOME with no parent of their own.

    So this reads only the FIRST user message — the instruction that defines
    scope — and ignores tool output and later prose.

    Survey prompts are rejected outright: a task that inventories the whole
    machine belongs to the global bucket, not to whichever project it happened
    to name most often.
    """
    row = conn.execute(
        """
        SELECT p.data AS d
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE p.session_id = ?
          AND json_extract(m.data, '$.role') = 'user'
          AND json_extract(p.data, '$.type') = 'text'
        ORDER BY m.time_created ASC, p.time_created ASC, p.id ASC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None

    try:
        text = (json.loads(row["d"]) or {}).get("text") or ""
    except (json.JSONDecodeError, TypeError):
        return None
    # A scope statement is at the start; cap so trailing prose cannot vote.
    prompt = text[:2500]
    lowered = prompt.lower()

    if any(marker in lowered for marker in SURVEY_MARKERS):
        return None

    hits: dict[str, int] = {}
    distinct = 0
    for marker, repo in PROMPT_MARKERS_TO_REPO.items():
        n = prompt.count(marker)
        if n:
            hits[repo] = hits.get(repo, 0) + n
            distinct += 1
    # Naming many projects means surveying them, not working on one.
    if distinct >= SURVEY_MIN_MARKERS:
        return None
    if not hits:
        return None
    best = max(hits.values())
    winners = [r for r, n in hits.items() if n == best]
    # A tie is not evidence either.
    return winners[0] if len(winners) == 1 else None


def resolve_repo_full(conn: sqlite3.Connection, row) -> tuple[str, str]:
    """Return (repo_name, reason).

    Order: own directory -> parent inheritance -> task prompt -> global.
    The directory stays authoritative for sessions opened inside a real repo;
    the prompt only decides sessions whose launch directory says nothing.
    """
    plain = resolve_repo(row)
    if plain != GLOBAL_REPO:
        return plain, "directory"

    inherited = repo_from_parent_chain(conn, row)
    if inherited:
        return inherited, "parent"

    by_prompt = repo_from_task_prompt(conn, _row_get(row, "id"))
    if by_prompt:
        return by_prompt, "prompt"

    return plain, "default"


# ── Rendering ───────────────────────────────────────────────────────────────
def fmt_ts(ms: int | None) -> str:
    if not ms:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def slugify(title: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "untitled")).strip("-").lower()
    return (slug[:limit].rstrip("-")) or "untitled"


def text_part_count_and_max(conn: sqlite3.Connection, session_id: str) -> tuple[int, int]:
    """(count, newest time_created) over this session's text parts.

    The cheap change-detector: a full render of a 2000-part session costs
    ~160 ms and allocates the whole transcript. Uses a LIKE prefilter rather
    than json_extract, which measured 95 ms for the same session because
    SQLite ran the JSON function per row. The pattern matches the stored JSON
    exactly (verified: 12,239 text parts both ways, zero false positives).
    """
    row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(MAX(time_created), 0)
        FROM part
        WHERE session_id = ?
          AND data LIKE '%"type":"text"%'
        """,
        (session_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def render_session(
    conn: sqlite3.Connection,
    row,
    part_count: int | None = None,
    newest: int | None = None,
) -> tuple[str, dict]:
    """Render one session to Markdown. Returns (markdown, stats).

    `part_count`/`newest` are the incremental-archive watermark, written to the
    flat header so a later run can tell whether the session has grown without
    re-reading the transcript.
    """
    session_id = _row_get(row, "id")
    title = _row_get(row, "title") or "Untitled session"
    stats = {"messages": 0, "chars": 0, "redactions": 0}

    # `role` lives in message.data (JSON), not as a column.
    parts = conn.execute(
        """
        SELECT p.data AS part_data,
               json_extract(m.data, '$.role') AS role
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE p.session_id = ?
          AND json_extract(p.data, '$.type') = 'text'
        ORDER BY m.time_created ASC, p.time_created ASC, p.id ASC
        """,
        (session_id,),
    ).fetchall()

    lines: list[str] = []
    current_role = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, current_role
        if not buffer or current_role is None:
            buffer = []
            return
        body = "\n".join(buffer).strip()
        if body:
            clean, n = redact(body)
            stats["redactions"] += n
            stats["chars"] += len(clean)
            label = {"user": "User", "assistant": "Assistant"}.get(
                current_role, str(current_role).title()
            )
            lines.append(f"### {label}\n")
            lines.append(clean)
            lines.append("")
        buffer = []

    for part in parts:
        try:
            text = (json.loads(part["part_data"]) or {}).get("text") or ""
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if not text.strip():
            continue
        role = part["role"] or "unknown"
        if role != current_role:
            flush()
            current_role = role
        buffer.append(text)
        stats["messages"] += 1
    flush()

    if part_count is None or newest is None:
        part_count, newest = text_part_count_and_max(conn, session_id)

    # Flat header: `key<TAB>value`, one line per field, stable order, no `---`
    # fences and no quoting. Values are emitted raw — the title loses the
    # trailing newline a DB title can carry (and the quotes the old json.dumps
    # wrapping put around it); the model is compact JSON with the runtime-only
    # `variant` key dropped.
    flat_title = title.rstrip("\n")
    raw_model = _row_get(row, "model") or "unknown"
    try:
        parsed_model = json.loads(raw_model)
    except (json.JSONDecodeError, TypeError):
        parsed_model = None
    if isinstance(parsed_model, dict):
        parsed_model.pop("variant", None)
        flat_model = json.dumps(parsed_model, separators=(",", ":"))
    else:
        flat_model = raw_model

    header = "\n".join(
        [
            f"session_id\t{session_id}",
            f"title\t{flat_title}",
            f"created\t{fmt_ts(_row_get(row, 'time_created'))}",
            f"updated\t{fmt_ts(_row_get(row, 'time_updated'))}",
            f"directory\t{_row_get(row, 'directory') or ''}",
            f"model\t{flat_model}",
            f"message_count\t{stats['messages']}",
            f"text_parts\t{part_count}",
            f"newest_text_part\t{newest}",
            f"redactions_applied\t{stats['redactions']}",
            "archived_by\tsession-archive.py",
        ]
    )
    return header + f"\n\n# {flat_title}\n" + "\n" + "\n".join(lines), stats


# ── Archive operations ──────────────────────────────────────────────────────
def session_filename(conn: sqlite3.Connection, row, session_id: str) -> str:
    created = datetime.fromtimestamp(
        (_row_get(row, "time_created") or 0) / 1000, tz=timezone.utc
    )
    seq = conn.execute(
        "SELECT COUNT(*) FROM session WHERE project_id IS ? AND time_created < ?",
        (_row_get(row, "project_id"), _row_get(row, "time_created")),
    ).fetchone()[0] + 1
    return (
        f"{created.strftime('%Y/%m')}/{seq:03d}-{created.strftime('%Y-%m-%d')}-"
        f"{slugify(_row_get(row, 'title'))}-{session_id[-6:]}.md"
    )


# ── Subagent dispatch logging ──────────────────────────────────────────────
# A subagent session (parent_id set) used to get the same treatment as a
# parent: one transcript file plus one `chore: archive ses_...` commit. That
# produced 61+ commits in sulav-global-sessions, nearly all of them a single
# Task dispatch. Subagents are logged as one line in DISPATCHES.md instead,
# and the parent session's own commit sweeps the append in.

# Error indicators, measured against the live DB (2026-10-10, 483 sessions,
# 79 subagent) rather than guessed:
#
#   Structured, high confidence:
#     part.data: {"type":"tool","tool":"bash","state":{"status":"error",
#       "error":"The bash tool was called with invalid arguments: ..."}}
#       506 tool parts across 53 sessions; state.error populated 506/506.
#     message.data: {"role":"assistant","error":{"name":"APIError",
#       "data":{"message":"Not Found: ..."}}, ...}  426 messages
#       (APIError 181, MessageAbortedError 182, UnknownError 54,
#       ContextOverflowError 7, ProviderAuthError 2); `finish:"error"`
#       on 7 more. 53 of the 79 subagent sessions carry one of these
#       (6 tool-state, 47 APIError); MessageAbortedError is parent-side
#       only — 0 subagent sessions have it.
#
#   Text, weak — deliberately narrow:
#     `Error:`, `429`, `rate limit`, `failed`, `HTTP 5xx` appear in text
#     parts mostly as DISCUSSION of errors (a subagent prompt that quotes
#     this very task matched `error:`). Restricting to assistant text plus
#     strict patterns caught 0 subagent sessions the structured signals
#     missed; bare `failed` alone flagged 8 sessions, 4 with no structured
#     error at all ("focus calls failed" in prose). So text is a fallback
#     for payloads lacking status fields, not a primary detector.
ERROR_TEXT_PATTERNS = [
    re.compile(r"\btraceback \(most recent call last\)"),
    re.compile(r"\b(?:429|5\d\d)\b[^.\n]{0,40}(?:too many requests|internal server error|bad gateway|service unavailable)", re.I),
    re.compile(r"\btoo many requests\b", re.I),
    re.compile(r"rate[ -]?limit(?:ed|ing| exceeded)?\b", re.I),
    re.compile(r"\berror:\s+\S", re.I),
]

DISPATCH_HEADER = (
    "# Subagent dispatches\n\n"
    "One line per subagent session (parent_id set), appended by "
    "session-archive.py and committed by the next parent-session archive.\n\n"
)
ISSUE_HEADER = (
    # Same H1 as log-agent-issue.py writes, so every archive repo's issue log
    # reads as one document no matter which script created the file first.
    "# Agents Issue Log\n\n"
    "Detail blocks for subagent sessions whose messages showed errors, "
    "appended by session-archive.py.\n\n"
)


def fmt_dispatch_ts(ms: int | None) -> str:
    """Minute-precision UTC stamp for log lines: 2026-10-09T16:20Z."""
    if not ms:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def fetch_session_tools(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Distinct tool names invoked, in first-use order. Names only."""
    rows = conn.execute(
        """
        SELECT json_extract(data, '$.tool') AS tool
        FROM part
        WHERE session_id = ? AND json_extract(data, '$.type') = 'tool'
        ORDER BY time_created ASC, id ASC
        """,
        (session_id,),
    ).fetchall()
    seen: list[str] = []
    for r in rows:
        name = r["tool"]
        if not name or name in seen:
            continue
        seen.append(name)
    # Stored lowercase ("bash"); display capitalized, as humans write it.
    return [t[:1].upper() + t[1:] for t in seen]


def fetch_model_pathway(conn: sqlite3.Connection, session_id: str) -> tuple[list[str], int]:
    """(distinct models in first-use order, switch count) from assistant msgs.

    A "switch" is a transition between consecutive distinct models, so a
    single-model run reports 0 switches and the pathway line reads
    `models: a, b (1 switch)`.
    """
    rows = conn.execute(
        """
        SELECT json_extract(data, '$.modelID') AS model
        FROM message
        WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
        ORDER BY time_created ASC, id ASC
        """,
        (session_id,),
    ).fetchall()
    models: list[str] = []
    for r in rows:
        m = r["model"]
        if m and (not models or models[-1] != m):
            models.append(m)
    return models, max(len(models) - 1, 0)


def collect_error_excerpts(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Short first-line excerpts of error payloads, newest signals included.

    Structured sources first (tool state.error, message error/finish), then
    the narrow text fallback. At most 5 excerpts; every one goes through
    redact() at append time.
    """
    excerpts: list[str] = []

    def add(raw) -> None:
        if not raw or len(excerpts) >= 5:
            return
        first = str(raw).strip().splitlines()[0].strip() if str(raw).strip() else ""
        if first and first not in excerpts:
            excerpts.append(first[:200])

    for r in conn.execute(
        """
        SELECT json_extract(data, '$.state.error') AS err, json_extract(data, '$.tool') AS tool
        FROM part
        WHERE session_id = ?
          AND json_extract(data, '$.type') = 'tool'
          AND json_extract(data, '$.state.status') = 'error'
        ORDER BY time_created ASC, id ASC
        """,
        (session_id,),
    ).fetchall():
        if len(excerpts) >= 5:
            break
        tool = r["tool"] or "tool"
        add(f"{tool}: {r['err']}" if r["err"] else f"{tool}: (error status, no message)")

    for r in conn.execute(
        """
        SELECT json_extract(data, '$.error.name') AS name,
               json_extract(data, '$.error.data.message') AS msg,
               json_extract(data, '$.finish') AS finish
        FROM message
        WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
          AND (json_extract(data, '$.error') IS NOT NULL
               OR json_extract(data, '$.finish') = 'error')
        ORDER BY time_created ASC, id ASC
        """,
        (session_id,),
    ).fetchall():
        if len(excerpts) >= 5:
            break
        name = r["name"] or "error"
        add(f"{name}: {r['msg']}" if r["msg"] else f"{name}: (finish={r['finish']})")

    if len(excerpts) < 5:
        # Text fallback, assistant text only: user prompts routinely quote
        # error words without the session failing.
        for r in conn.execute(
            """
            SELECT json_extract(p.data, '$.text') AS text
            FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE p.session_id = ?
              AND json_extract(p.data, '$.type') = 'text'
              AND json_extract(m.data, '$.role') = 'assistant'
            ORDER BY m.time_created ASC, p.time_created ASC, p.id ASC
            """,
            (session_id,),
        ).fetchall():
            if len(excerpts) >= 5:
                break
            text = r["text"] or ""
            for line in text.splitlines():
                if any(p.search(line) for p in ERROR_TEXT_PATTERNS):
                    add(line.strip())
                    break
    return excerpts


def _log_field(text: str, limit: int = 120) -> str:
    """Flatten a title/label so it cannot break the pipe-delimited log line."""
    flat = " ".join(str(text or "").split()).replace("|", "/")
    return flat[:limit]


def fmt_tools(tools: list[str], cap: int = 8) -> str:
    """Render a tool list, truncated so one runaway session cannot bloat a line."""
    if not tools:
        return "none"
    shown = ", ".join(tools[:cap])
    return shown + (f" +{len(tools) - cap} more" if len(tools) > cap else "")


def dispatch_line(row, models: list[str], switches: int, tools: list[str],
                  messages: int, errors: list[str]) -> str:
    ts = fmt_dispatch_ts(_row_get(row, "time_updated") or _row_get(row, "time_created"))
    title = _log_field(_row_get(row, "title") or "untitled")
    outcome = "ERROR" if errors else "OK"
    model_s = ", ".join(models) if models else "unknown"
    return (
        f"- {ts} | {_row_get(row, 'id')} | {title} | tools: {fmt_tools(tools)} "
        f"| models: {model_s} ({switches} switch{'es' if switches != 1 else ''}) "
        f"| {messages} msgs | {outcome}"
    )


def issue_block(row, models: list[str], switches: int, tools: list[str],
                messages: int, errors: list[str]) -> str:
    ts = fmt_dispatch_ts(_row_get(row, "time_updated") or _row_get(row, "time_created"))
    title = _log_field(_row_get(row, "title") or "untitled", 200)
    directory = _log_field(_row_get(row, "directory") or "", 300)
    pathway = " -> ".join(models) if models else "unknown"
    lines = [
        f"## {_row_get(row, 'id')} — {title}",
        "",
        f"- **when:** {ts}",
        f"- **directory:** {directory}",
        f"- **pathway:** {pathway} ({switches} switch{'es' if switches != 1 else ''})",
        f"- **tools:** {fmt_tools(tools, cap=16)}",
        f"- **messages:** {messages}",
        "- **outcome:** ERROR",
        "",
        "### Error excerpts",
        "",
    ]
    for i, e in enumerate(errors, 1):
        lines.append(f"{i}. {e}")
    lines.append("")
    return "\n".join(lines)


def append_once(path: Path, session_id: str, text: str, header: str) -> bool:
    """Append `text` to `path` unless `session_id` is already recorded there.

    session.idle fires repeatedly, so the same subagent session can be
    archived several times; the watermark gate does not cover this path.
    Returns True when an append happened. An existing-but-unreadable file
    raises rather than being truncated: silent data loss is worse than a
    failed archive.
    """
    try:
        existing = path.read_text(encoding="utf-8")
        if session_id in existing:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text)
        else:
            path.write_text(header + text, encoding="utf-8")
        return True
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header + text, encoding="utf-8")
        return True


def log_dispatch(conn: sqlite3.Connection, row, repo_name: str, session_id: str) -> int:
    """Log one subagent session as a DISPATCHES.md line. Never touches git.

    Both files are shared by every project and therefore live at the single
    git root, not in the routed repo's content subfolder.
    """
    root = git_root()
    models, switches = fetch_model_pathway(conn, session_id)
    tools = fetch_session_tools(conn, session_id)
    errors = collect_error_excerpts(conn, session_id)
    messages = conn.execute(
        "SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)
    ).fetchone()[0]

    line = redact(dispatch_line(row, models, switches, tools, messages, errors))[0]
    appended = append_once(root / "DISPATCHES.md", session_id, line + "\n", DISPATCH_HEADER)

    if errors:
        block = redact(issue_block(row, models, switches, tools, messages, errors))[0]
        if append_once(root / "agents-issue" / "ISSUES.md", session_id, block, ISSUE_HEADER):
            appended = True

    if appended:
        print(
            f"dispatch-logged {session_id} -> {repo_name} "
            f"({messages} msgs, {'ERROR' if errors else 'OK'}); "
            f"left uncommitted for the next parent archive"
        )
    else:
        print(f"dispatch already logged {session_id} -> {repo_name}")
    return 0


def archive_one(session_id: str, push: bool) -> int:
    """Archive a single session in place, then optionally commit+push.

    Called by the session.idle plugin. `session.idle` fires after every turn,
    and most carry nothing new, so this checks cheaply whether the session grew
    and exits before re-rendering when it did not.

    A subagent session (parent_id set) is NOT rendered to its own file and is
    NEVER committed: it becomes one DISPATCHES.md line (plus an ISSUES.md
    block when it shows errors), idempotent per session id. The parent
    session's own `git add -A` later sweeps those appends into one commit.
    """
    conn = connect()
    row = conn.execute(
        """
        SELECT id, project_id, directory, title, model,
               time_created, time_updated, parent_id
        FROM session WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        print(f"session not found: {session_id}", file=sys.stderr)
        return 1

    count = conn.execute(
        "SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    if count < MIN_MESSAGES_FOR_ARCHIVE:
        print(f"skip {session_id}: no messages yet")
        return 0

    repo_name, _reason = resolve_repo_full(conn, row)
    ensure_repo_scaffold(repo_name)

    if _row_get(row, "parent_id"):
        return log_dispatch(conn, row, repo_name, session_id)

    dest = content_dir(repo_name) / "sessions" / session_filename(conn, row, session_id)
    # Printed relative to the git root so the subfolder destination is visible.
    shown = dest.relative_to(git_root()).as_posix()

    # Cheap gate: skip the full render when the archived file already reflects
    # this many text parts with at least this new a timestamp.
    part_count, newest = text_part_count_and_max(conn, session_id)
    if dest.exists():
        try:
            head = dest.read_text(encoding="utf-8")[:2000]
        except OSError:
            head = ""
        # Header is `key<TAB>value`; the pre-reformat `key: value` form still
        # matches, so a file not yet re-rendered keeps gating correctly.
        m = re.search(r"^message_count[\s:]+(\d+)", head, re.MULTILINE)
        n = re.search(r"^newest_text_part[\s:]+(\d+)", head, re.MULTILINE)
        if m and n and int(m.group(1)) >= part_count and int(n.group(1)) >= newest:
            print(f"unchanged {session_id} -> {shown}")
            return 0

    markdown, stats = render_session(conn, row, part_count=part_count, newest=newest)
    if stats["redactions"]:
        print(f"redacted {stats['redactions']} secret(s) in {session_id}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_text(encoding="utf-8") == markdown:
        print(f"unchanged {session_id} -> {shown}")
        return 0
    dest.write_text(markdown, encoding="utf-8")
    print(f"archived {session_id} -> {shown} ({stats['messages']} msgs)")

    if push:
        # Every git command runs in the ONE repo root, never in a subfolder:
        # the subfolders are not repos, so a cwd there would fail or, worse,
        # pick up an unrelated enclosing repository.
        repo = git_root()
        subprocess.run(["git", "add", "-A"], cwd=repo, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # git commit exits 1 when nothing is staged: the expected no-op case.
        subprocess.run(["git", "commit", "-q", "-m", f"chore: archive {session_id}"],
                       cwd=repo, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = subprocess.run(["git", "push", "-q"], cwd=repo, check=False,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"push failed: {r.stderr.strip()[:200]}", file=sys.stderr)
            return 1
    return 0


def _existing_archive_targets(conn: sqlite3.Connection, limit: int | None):
    """(row, repo_name, dest) for every transcript file already in the archive.

    Keyed by the `session_id` each file records rather than by the filename the
    database would compute now, so a re-render lands on the exact path it came
    from: nothing is created, renamed or removed, even for a session whose
    title — and therefore its slug — changed after the file was written.
    """
    root = git_root()
    targets = []
    for path in sorted(p for p in root.rglob("*.md") if "sessions" in p.parts):
        rel = path.relative_to(root).as_posix()
        head = path.read_text(encoding="utf-8", errors="ignore")[:400]
        # Old `session_id: x` and new `session_id\tx` headers both match.
        m = re.search(r"^session_id[\t:]\s*(\S+)", head, re.MULTILINE)
        if not m:
            print(f"  ! {rel}: no session_id header, left untouched")
            continue
        row = conn.execute(
            """
            SELECT id, project_id, directory, title, model,
                   time_created, time_updated, parent_id
            FROM session WHERE id = ?
            """,
            (m.group(1),),
        ).fetchone()
        if row is None:
            print(f"  ! {rel}: {m.group(1)} not in the database, left untouched")
            continue
        repo_name, _ = resolve_repo_full(conn, row)
        targets.append((row, repo_name, path))
    return targets[:limit] if limit else targets


def backfill(limit: int | None, dry_run: bool, force: bool = False) -> int:
    """Render sessions into the archive.

    Default: render every session in the database (populate a fresh archive;
    it also creates files for sessions that have never been archived).

    `force` (--force) re-renders the archive that already exists instead: one
    pass over the archived .md files that ignores the text_parts /
    newest_text_part watermark, so every session is re-rendered even when its
    recorded watermark already matches the database. It creates no new file
    and removes none — that is the mode for rolling a header-format change
    across the whole archive.
    """
    conn = connect()
    if force:
        targets = _existing_archive_targets(conn, limit)
    else:
        rows = conn.execute(
            """
            SELECT id, project_id, directory, title, model,
                   time_created, time_updated, parent_id
            FROM session
            ORDER BY time_created ASC, id ASC
            """
        ).fetchall()
        if limit:
            rows = rows[:limit]
        targets = []
        for row in rows:
            repo_name, _ = resolve_repo_full(conn, row)
            dest = content_dir(repo_name) / "sessions" / session_filename(
                conn, row, row["id"]
            )
            targets.append((row, repo_name, dest))

    per_repo: dict[str, int] = {}
    total_redactions = 0
    written = 0

    for row, repo_name, dest in targets:
        session_id = row["id"]
        per_repo[repo_name] = per_repo.get(repo_name, 0) + 1
        markdown, stats = render_session(conn, row)
        total_redactions += stats["redactions"]
        if stats["redactions"]:
            print(f"  🔐 {stats['redactions']} redaction(s) in {session_id} — {row['title'][:44]}")
        if dry_run:
            written += 1
            continue
        # In force mode the destination already exists; skip the scaffold so a
        # re-render can never create a file of its own.
        if not force:
            ensure_repo_scaffold(repo_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(markdown, encoding="utf-8")
        written += 1

    print(f"\n{'[dry run] ' if dry_run else ''}wrote {written} sessions")
    for name, count in sorted(per_repo.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {name}")
    print(f"  total redactions applied: {total_redactions}")
    return total_redactions


def build_index_main() -> int:
    """Delegate to session-index.py so both entry points stay consistent."""
    index_script = Path.home() / ".local/bin/session-index.py"
    if not index_script.exists():
        print("session-index.py not found", file=sys.stderr)
        return 1
    return subprocess.run([sys.executable, str(index_script)], check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_backfill = sub.add_parser("backfill")
    p_backfill.add_argument("--limit", type=int, default=None)
    p_backfill.add_argument("--dry-run", action="store_true")
    p_backfill.add_argument(
        "--force",
        action="store_true",
        help="re-render every archived file in place, ignoring the "
        "text_parts/newest_text_part watermark; creates no new files",
    )
    p_backfill.add_argument(
        "--no-push",
        action="store_true",
        help="accepted for symmetry with archive-one: backfill never pushes",
    )

    p_route = sub.add_parser("route")
    p_route.add_argument("session_id")

    p_one = sub.add_parser("archive-one")
    p_one.add_argument("session_id")
    p_one.add_argument("--no-push", action="store_true")

    sub.add_parser("index")

    args = parser.parse_args()

    if args.command == "backfill":
        backfill(args.limit, args.dry_run, force=args.force)
        return 0
    if args.command == "index":
        return build_index_main()
    if args.command == "route":
        conn = connect()
        row = conn.execute(
            "SELECT id, project_id, directory, title, parent_id FROM session WHERE id = ?",
            (args.session_id,),
        ).fetchone()
        if not row:
            print("session not found")
            return 1
        repo, reason = resolve_repo_full(conn, row)
        print(f"  {row['id']}")
        print(f"  title     {row['title']}")
        print(f"  directory {row['directory']}")
        print(f"  parent    {row['parent_id'] or '(none)'}")
        print(f"  -> repo   {repo}  (via {reason})")
        # Content root, relative to the archive root: shows the subfolder the
        # transcript will land in ("sulav-global-sessions/opencode-global-...").
        print(f"  -> path   {content_dir(repo).relative_to(ARCHIVE_ROOT).as_posix()}")
        print(f"  -> git    {git_root().relative_to(ARCHIVE_ROOT).as_posix()}/")
        return 0
    if args.command == "archive-one":
        # The plugin translates SESSION_ARCHIVE_PUSH=0 into --no-push; honour
        # the env var here as well so direct invocations behave identically.
        push = not args.no_push and os.environ.get("SESSION_ARCHIVE_PUSH") != "0"
        return archive_one(args.session_id, push=push)
    return 1


if __name__ == "__main__":
    sys.exit(main())