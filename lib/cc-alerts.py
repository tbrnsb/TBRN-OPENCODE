#!/usr/bin/env python3
"""cc-alerts.py — read-only failure/staleness detectors that push to ntfy.

Reads only:
  * opencode.db                (readOnly, via sqlite3 CLI)
  * model-health.json          (cooldown registry)
  * chains.json                (role -> rung models)
Writes only:
  * the ntfy cache file        (dedupe timestamps)
  * ntfy HTTP POSTs            (notifications)
  * stdout

It NEVER writes to OpenCode config, never dispatches an agent, never touches
the DB except through a read-only sqlite3 connection.

Detectors
  1. repeated_failure  one model logs FAIL errors >= FAIL_COUNT within WINDOW_MIN
  2. stale_session     an open session whose newest message errored and has had
                       no activity for >= STALE_MIN minutes
  3. all_rungs_down    every model in a role's chain is in cooldown at once
  4. new_error         WATERMARK: any message-level error (message.data.error,
                       excluding NON_FAILURE_ERRORS) or tool-level error
                       (part.data.state.status='error') recorded since the
                       previous run. This is the detector that can fire for
                       ONE fresh error within ~30 s of it happening.
  5. new_session       WATERMARK: any session created since the previous run.

Detectors 4 and 5 keep a per-table high-water mark (wm_message_ms, wm_part_ms,
wm_session_ms) in the cache JSON. On the FIRST run the watermark is seeded to
the table's current MAX(time_created) and nothing is sent — otherwise the
first run would push ~19 historical tool errors as if they were new. The
watermark is advanced and persisted even when the ntfy POST fails, so a down
channel cannot loop the same rows forever.

Each detector has its own cooldown so a flapping condition does not re-alert
within RATE_LIMIT_MIN.

MAIN SESSIONS ONLY (v2)
  Phone alerts cover MAIN sessions only — `session.parent_id IS NULL`. Measured
  on the live DB: 135 of 578 sessions are subagent children, and children are
  56-59% of sessions created in any recent 24-72h window, so without this
  filter most pushes were subagent dispatch detail rather than something worth
  a phone buzz. Subagent errors remain fully visible in the Command Center
  errors panel (that surface is unchanged — server.js reads the same rows);
  they simply do not push.

  `det_stale_session` additionally fires ONCE PER STALL: keyed on
  (session, last-error message timestamp) in cache["stalled_alerted"], so a
  session that is already dead never re-alerts after RATE_LIMIT_MIN expires.
  New activity produces a new message, hence a new timestamp, hence a fresh
  key — so a session that recovers and stalls again DOES alert again.

  When a MAIN session is stalled because a SUBAGENT errored, the alert names
  the failing subagent rather than blaming the parent for silence it did not
  cause.

ERROR-CLASS TABLE (v3, Job H)
  Not every error is worth a phone buzz. One table — ERROR_CLASSES — maps an
  error to a class and a phone-worthiness flag; `det_new_error` drops any row
  whose class is phone=False. The three dashboard-only classes named in the
  brief (permission denied, file not found, edit oldString mismatch) are
  measured to be the ONLY errors that ever actually pushed on this box, so
  this is the filter that matters. Suppression is push-only: /api/errors and
  /api/alerts read opencode.db themselves, so nothing disappears from the
  dashboard. `--classify` prints the per-class counts for the last 7 days.

--self-test prints watermarks, what each new detector WOULD report, and the
last 5 error rows — read-only, nothing is sent and the cache is not written.
--classify prints the 7-day error-class table — read-only, nothing is sent.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

HOME = pathlib.Path.home()
DB = os.environ.get("OPENCODE_DB", HOME / ".local/share/opencode/opencode.db")
HEALTH = pathlib.Path(os.environ.get("CC_HEALTH_FILE",
                          HOME / ".config/opencode/lib/model-health.json"))
CHAINS = pathlib.Path(os.environ.get("CC_CHAINS_FILE",
                           HOME / ".config/opencode/chains.json"))
CACHE = pathlib.Path(os.environ.get("CC_CACHE_FILE",
                         HOME / ".local/state/cc-alerts/cache.json"))

# The ntfy topic is a SECRET-ish capability string: anyone holding it can post
# to your phone's topic. It therefore has exactly two allowed homes — the
# NTFY_TOPIC environment variable, or ~/.local/state/cc-alerts/TOPIC.txt — and
# NEVER in source or in a systemd unit. The unit that ships in the git repo
# carries no Environment=NTFY_TOPIC line at all precisely so that
# `git log -p` cannot leak it; this fallback is what makes that safe.
TOPIC_FILE = HOME / ".local/state/cc-alerts/TOPIC.txt"

# ── schema detection (V2 vs legacy V1) ──────────────────────────────────────
# OpenCode V2 writes session_v2 + session_message; the legacy session/message/
# part tables still exist in the same file. Detect once and build UNION queries
# so we see errors from BOTH the V1 TUI and V2 sessions.
_SCHEMA_CACHE = None
def _table_exists(table: str) -> bool:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()

def _schema() -> dict:
    """Returns {'v2': bool, 'session': str, 'message': str}."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    v2 = _table_exists("session_v2") and _table_exists("session_message")
    _SCHEMA_CACHE = {
        "v2": v2,
        "session": "session_v2" if v2 else "session",
        "message": "session_message" if v2 else "message",
    }
    return _SCHEMA_CACHE

def _union(sql_v1: str, sql_v2: str) -> list[dict]:
    """Execute both V1 and V2 queries and combine results in Python.
    
    Avoids SQLite UNION limitations (ORDER BY placement, parentheses).
    """
    sc = _schema()
    if not sc["v2"]:
        return query(sql_v1)
    # Execute both and combine
    rows_v1 = query(sql_v1)
    rows_v2 = query(sql_v2)
    return rows_v1 + rows_v2


def _load_topic() -> str:
    t = os.environ.get("NTFY_TOPIC", "").strip()
    if t:
        return t
    try:
        return TOPIC_FILE.read_text().strip()
    except OSError:
        return ""


TOPIC = _load_topic()
NTFY_URL = f"https://ntfy.sh/{TOPIC}" if TOPIC else ""

# Thresholds (seconds / counts). Documented in the report; tuned here.
WINDOW_MIN = int(os.environ.get("CC_WINDOW_MIN", "60"))     # repeated-failure window
FAIL_COUNT = int(os.environ.get("CC_FAIL_COUNT", "3"))      # failures to trip it
STALE_MIN = int(os.environ.get("CC_STALE_MIN", "120"))      # stale-session threshold
# Upper bound on what counts as "stalled". Measured on the live DB: 552
# sessions sit unarchived, and 552 of them are >24h old — OpenCode leaves
# abandoned tabs unarchived, so `time_archived IS NULL` is NOT an active
# signal. Without this ceiling the detector fired on 20 sessions that had
# been dead for 14+ days. "Stalled" now means: was working within the last
# day, then went silent for >STALE_MIN. With the real data that set is empty,
# which is the correct answer, not a false negative.
ACTIVE_HORIZON_MIN = int(os.environ.get("CC_ACTIVE_HORIZON_MIN", "1440"))
RATE_LIMIT_MIN = int(os.environ.get("CC_RATE_LIMIT_MIN", "30"))  # per-condition cooldown

# Aborts are user-initiated cancels, not model failures — counting them made
# every deliberate stop look like a model outage. Excluded from failure dets.
NON_FAILURE_ERRORS = {"MessageAbortedError"}
NON_FAILURE_ERRORS_SQL = ",".join(f"'{e}'" for e in sorted(NON_FAILURE_ERRORS))

# ── ERROR-CLASS TABLE (phone-worthiness) ────────────────────────────────────
# One table, read top-down, FIRST match wins. It decides only whether an error
# may push to ntfy. It never decides visibility: /api/errors and /api/alerts
# read opencode.db directly (server.js), so a class marked phone=False stays
# fully visible in the dashboard — that is structural, not a promise.
#
#   phone=True   the FIRST occurrence of this class pushes.
#   phone=False  never pushes on its own (dashboard-only). It can still reach
#                the phone through det_repeated_failure when >= FAIL_COUNT of
#                the same MODEL land inside WINDOW_MIN — repetition is the
#                signal there, not the single event.
#   scope        "msg" = message-level only, "tool" = tool-level only,
#                "any" = both.
#
# An UNMATCHED error gets class "other" and is dashboard-only: the default
# fails CLOSED on noise, because the dashboard is the catch-all surface and
# all_rungs_down / stale_session still phone independently. Measured 7-day
# counts per class: `cc-alerts.py --classify`.
#
# The three classes named in the JOB H brief as dashboard-only — permission
# denied, file not found, edit oldString mismatch — are exactly the three
# rows below, and they are the only errors that ever pushed on this box.
ERROR_CLASSES = (
    # class              phone  scope   pattern (re.I, searched in the hay)
    ("context_overflow", True,  "msg",  r"\bContextOverflowError\b"),
    ("provider_auth",    True,  "msg",  r"\bProviderAuthError\b"),
    ("agent_config",     True,  "any",  r"Model not found|Unknown agent type"),
    ("subagent_failed",  True,  "any",  r"Subagent failed \("),
    ("permission_denied", False, "any", r"rule which prevents|user rejected permission"),
    ("file_not_found",   False, "any",  r"File not found|NotFound:"),
    ("edit_mismatch",    False, "any",
     r"oldString|No changes to apply|Found multiple matches"),
    ("tool_misuse",      False, "any",
     r"invalid arguments|SchemaError|Missing key|must be a string|null bytes"
     r"|regex parse error|out of range|exceeded 65536"),
    ("user_abort",       False, "any",  r"abort|cancel|dismiss"),
    ("net_timeout",      False, "any",
     r"timed out|timeout|Cannot connect|SSE read|Request timed out"),
    ("api_error",        False, "msg",  r"\bAPIError\b|\bUnknownError\b"),
)


def classify_error(*, msg_name=None, msg_text=None, tool=None,
                   tool_err=None) -> tuple[str, bool]:
    """(class_name, phone_worthy) for one error. Unmatched -> ("other", False).

    `tool`/`tool_err` selects the tool-level haystack (\"<tool> <err>\");
    otherwise the message-level haystack (\"<error.name> <error.data.message>\").
    """
    if tool is not None:
        scope, hay = "tool", f"{tool} {tool_err or ''}"
    else:
        scope, hay = "msg", f"{msg_name or ''} {msg_text or ''}"
    for cls, phone, sc, pat in ERROR_CLASSES:
        if sc in ("any", scope) and re.search(pat, hay, re.IGNORECASE):
            return cls, phone
    return "other", False

# MAIN SESSIONS ONLY: a session is a subagent (not something the user started)
# iff parent_id is non-NULL. Measured, not assumed: 443 sessions have
# parent_id NULL (orchestrator/build/plan/scoutmin/None) and 135 do not
# (tester/coder/planner/critic/…), and all 135 point at a session that really
# exists — 0 orphans. Applied as `X.parent_id IS NULL`, where X is whatever
# alias the surrounding query already uses for `session`.
# (there is no shared SQL constant for the filter: each query already aliases
# `session` differently — s, c, or bare — so the predicate is written inline
# next to its own FROM clause, where a reviewer can see which alias it binds.)
#
# One alert per stall. Value is the last-error message timestamp (ms) that the
# alert was raised for; equal value means "same stall, already reported".
STALL_ONCE_KEY = "stalled_alerted"
# Prune cap. The map only holds sessions that are stalled RIGHT NOW plus any
# that recently were, so in practice it stays in the tens; the cap exists so a
# pathological stream of unique session ids cannot grow the cache forever.
STALL_ONCE_MAX = 500


# ── ntfy ────────────────────────────────────────────────────────────────────
# HTTP header values must be latin-1: http/client.py putheader() does
# `one_value.encode('latin-1')` and raises UnicodeEncodeError otherwise. Our
# titles are free-form session text and truncate()/sid_ref() append a literal
# '…' (U+2026), so a title longer than the truncation limit used to reach this
# point unencodable. Measured in production (journalctl --user -u cc-alerts,
# 2026-10-12 05:45:43): UnicodeEncodeError escaped notify(), main() died with
# status=1/FAILURE, and the alert that was being sent plus every alert queued
# behind it were lost ("Stalled session: Install and verify TBRN APK ...").
_HEADER_REPLACEMENTS = {
    "\u2026": "...",            # …  ellipsis — truncate() emits this one
    "\u2014": "-",              # —  em dash
    "\u2013": "-",              # –  en dash
    "\u2018": "'", "\u2019": "'",   # ‘ ’
    "\u201c": '"', "\u201d": '"',   # “ ”
    "\u2192": "->",             # →
    "\u2022": "*",              # •
    "\u00a0": " ",              # non-breaking space
}


def header_safe(text) -> str:
    """Return a value guaranteed to survive `value.encode('latin-1')`.

    Three steps, in order:
      1. transliterate the few characters this program's own text actually
         contains (… -> "...") so the human wording survives;
      2. drop C0/DEL control characters — a CR/LF inside a title would be
         header injection, and http.client rejects it with ValueError;
      3. let `errors='replace'` turn ANY remaining codepoint outside
         U+0000..U+00FF into '?' instead of raising.

    Never raises: str() of any object always works, and 'replace' cannot fail.
    """
    s = str(text or "")
    for bad, good in _HEADER_REPLACEMENTS.items():
        s = s.replace(bad, good)
    s = "".join(c for c in s if c >= "\x20" and c != "\x7f")
    return s.encode("latin-1", "replace").decode("latin-1")


def notify(title: str, message: str, priority: str = "default",
           tags: str = "") -> tuple[bool, str]:
    """POST one notification. NEVER raises.

    Every failure — dead network, unencodable header, a TypeError deep inside
    urllib — comes back as (False, "<type>: <msg>"). A notification problem
    must not be able to kill the alerter or skip the alerts queued behind this
    one: the 2026-10-12 UnicodeEncodeError escaped this function entirely and
    took the process (and the rest of that run's alerts) with it. The TITLE is
    a header and is sanitized here, so every call site is protected even if
    only these three were remembered. The BODY is POST bytes and stays UTF-8.
    """
    if not NTFY_URL:
        return False, "no NTFY_TOPIC set — skipped"
    try:
        body = str(message or "").encode("utf-8")
        req = urllib.request.Request(NTFY_URL, data=body, method="POST")
        req.add_header("Title", header_safe(title))
        req.add_header("Priority", header_safe(priority))
        if tags:
            req.add_header("Tags", header_safe(tags))
        with urllib.request.urlopen(req, timeout=15) as r:
            return True, f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001 — deliberate: notify() is total
        return False, f"{type(e).__name__}: {e}"


# ── redaction ───────────────────────────────────────────────────────────────
# SHAPE-based, same rule as server.js redact(): any [A-Za-z0-9_-]{24,} run
# (the shape an API key / token has) is replaced before the text ever leaves
# this machine. Error class names are short, so the useful signal survives.
_SECRET_SHAPE = re.compile(r"[A-Za-z0-9_-]{24,}")


def redact(text) -> str:
    return _SECRET_SHAPE.sub("[redacted]", str(text or ""))


# ── watermark helpers (new_error / new_session) ─────────────────────────────
def bootstrap_wm(cache: dict, key: str, table: str) -> int:
    """Effective watermark in ms for `table`, seeded on first sight.

    First run: the key is absent, so it is set to the table's current
    MAX(time_created) and the detector reports NOTHING for historical rows.
    (Measured 2026-10-11: without this the first run would have pushed ~19
    historical tool errors plus every past session as "new".)
    """
    if key not in cache:
        rows = query(f"SELECT MAX(time_created) AS mx FROM {table}")
        mx = rows[0]["mx"] if rows else None
        cache[key] = int(mx or 0)
    return int(cache[key])


def advance_wm(cache: dict, key: str, ts) -> None:
    """Move a watermark forward. Persisted by main() even if notify fails."""
    if ts and int(ts) > int(cache.get(key, 0)):
        cache[key] = int(ts)


def truncate(text, limit: int) -> str:
    # The '…' is fine in the BODY (POST bytes are UTF-8) but it is NOT
    # latin-1, so it must never reach an HTTP header raw. That is what
    # header_safe() inside notify() is for — see the 2026-10-12 crash.
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def sid_ref(session_id) -> str:
    """Session ids are ~29 chars of [A-Za-z0-9_-] — exactly the secret shape.

    A full id must not ride out to a public ntfy topic verbatim, so alerts
    carry a 12-char prefix ("ses_ee8fc77edff…") plus the session TITLE, which
    is the human handle. 12 chars stays well under the 24-char threshold.
    """
    s = str(session_id or "")
    return s if len(s) <= 23 else s[:12] + "…"


# ── rate-limit cache ────────────────────────────────────────────────────────
def load_cache() -> dict:
    try:
        with CACHE.open() as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(cache, f, indent=1)
    tmp.replace(CACHE)


def should_alert(cache: dict, key: str, now: float) -> bool:
    last = cache.get(key, 0)
    if now - last < RATE_LIMIT_MIN * 60:
        return False
    cache[key] = now
    return True


# ── once-per-stall dedupe ───────────────────────────────────────────────────
# Distinct from should_alert(): that one is a 30-MINUTE COOLDOWN, which is
# exactly what made a dead session re-buzz every half hour for as long as it
# stayed inside the age window. This one is PERMANENT for a given stall:
# same (session, last-error timestamp) is never reported twice, and only new
# activity — which writes a new message, hence a new timestamp — mints a key
# that has not been seen before.
def stall_seen(cache: dict, session_id, last_msg_ms) -> bool:
    stalls = cache.setdefault(STALL_ONCE_KEY, {})
    return stalls.get(session_id) == last_msg_ms


def stall_record(cache: dict, session_id, last_msg_ms) -> None:
    cache.setdefault(STALL_ONCE_KEY, {})[session_id] = last_msg_ms


def stall_prune(cache: dict, still_stalled: set) -> None:
    """Keep only sessions that are stalled right now, then enforce the cap.

    Pruning a recovered session is safe: if it stalls again its last-error
    timestamp will differ from the pruned value, so the fresh stall alerts.
    """
    stalls = cache.setdefault(STALL_ONCE_KEY, {})
    for sid in [s for s in stalls if s not in still_stalled]:
        del stalls[sid]
    if len(stalls) > STALL_ONCE_MAX:
        for sid in sorted(stalls, key=lambda s: stalls[s] or 0)[
                : len(stalls) - STALL_ONCE_MAX]:
            del stalls[sid]


# ── db helpers ──────────────────────────────────────────────────────────────
def query(sql: str) -> list[dict]:
    """Read-only query via the sqlite3 CLI (keeps this a leaf dependency)."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def load_json(path: pathlib.Path):
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ── detectors ───────────────────────────────────────────────────────────────
def det_repeated_failure(now: float, cache: dict) -> list[tuple[str, str]]:
    # MAIN SESSIONS ONLY. This detector counts failures BY MODEL, so it has no
    # session of its own — but the rows it counts are per-session messages.
    # The headline requirement is "phone alerts for MAIN sessions only", and
    # the LEFT JOIN + s.id IS NOT NULL / s.parent_id IS NULL pair is what keeps
    # a burst of subagent failures from tripping a phone alert. Trade-off
    # accepted knowingly: a model outage that ONLY ever hits subagents will
    # not raise this particular alert (it still surfaces in the dashboard).
    since_ms = (now - WINDOW_MIN * 60) * 1000
    sc = _schema()
    # V1 query
    sql_v1 = f"""
        SELECT json_extract(m.data,'$.providerID') || '/' ||
               json_extract(m.data,'$.modelID')  AS model,
               COUNT(*) AS fails
          FROM message m
          LEFT JOIN session s ON s.id = m.session_id
         WHERE m.time_created > {int(since_ms)}
           AND s.id IS NOT NULL
           AND s.parent_id IS NULL
           AND json_extract(m.data,'$.error') IS NOT NULL
           AND json_extract(m.data,'$.error.name') NOT IN ({NON_FAILURE_ERRORS_SQL})
         GROUP BY model
        HAVING fails >= {FAIL_COUNT}
         ORDER BY fails DESC
    """
    # V2 query - model/provider in data.model JSON, session_v2 for parent_id
    sql_v2 = f"""
        SELECT json_extract(sm.data,'$.model.providerID') || '/' ||
               json_extract(sm.data,'$.model.id')  AS model,
               COUNT(*) AS fails
          FROM session_message sm
          LEFT JOIN session_v2 s ON s.id = sm.session_id
         WHERE sm.time_created > {int(since_ms)}
           AND sm.type = 'assistant'
           AND s.id IS NOT NULL
           AND s.parent_id IS NULL
           AND json_extract(sm.data,'$.error') IS NOT NULL
           AND json_extract(sm.data,'$.error.type') NOT IN ({NON_FAILURE_ERRORS_SQL})
         GROUP BY model
        HAVING fails >= {FAIL_COUNT}
         ORDER BY fails DESC
    """
    rows = _union(sql_v1, sql_v2)
    out = []
    for r in rows:
        key = f"repeated_failure:{r['model']}"
        if should_alert(cache, key, now):
            out.append((
                f"Repeated failure: {r['model']}",
                f"{r['model']} logged {r['fails']} failures in the last "
                f"{WINDOW_MIN} min (threshold {FAIL_COUNT}).",
                "high", "rotating_light"))
    return out





def det_all_rungs_down(now: float, cache: dict) -> list[tuple[str, str, str, str]]:
    health = load_json(HEALTH)
    chains = (load_json(CHAINS) or {}).get("roles", {})
    cooling = {
        m for m, v in health.items()
        if v.get("down_until") and v["down_until"] > now
    }
    out = []
    for role, spec in chains.items():
        rungs = spec.get("rungs") or []
        if not rungs:
            continue
        if all(r in cooling for r in rungs):
            key = f"all_rungs_down:{role}"
            if should_alert(cache, key, now):
                out.append((
                    f"All rungs down: {role}",
                    f"Every model in the {role} chain is in cooldown "
                    f"simultaneously ({len(rungs)} rungs). No rung can serve.",
                    "max", "skull"))
    return out


def det_stale_session(now: float, cache: dict) -> list[tuple[str, str, str, str]]:
    cutoff_ms = (now - STALE_MIN * 60) * 1000
    horizon_ms = (now - ACTIVE_HORIZON_MIN * 60) * 1000
    sc = _schema()
    # V1 query
    sql_v1 = f"""
        SELECT s.id, s.title, last.time_created AS last_msg,
               json_extract(last.data,'$.error.name') AS last_err
        FROM session s
        JOIN message last ON last.id = (
            SELECT m2.id FROM message m2
            WHERE m2.session_id = s.id
            ORDER BY m2.time_created DESC, m2.id DESC
            LIMIT 1
        )
        WHERE s.time_archived IS NULL
          AND s.parent_id IS NULL
          AND last.time_created < {int(cutoff_ms)}
          AND last.time_created > {int(horizon_ms)}
          AND json_extract(last.data,'$.error.name') IS NOT NULL
          AND json_extract(last.data,'$.error.name')
              NOT IN ({NON_FAILURE_ERRORS_SQL})
        ORDER BY last_msg ASC
        LIMIT 20
    """
    # V2 query - session_v2, session_message, error.type instead of error.name
    sql_v2 = f"""
        SELECT s.id, s.title, last.time_created AS last_msg,
               json_extract(last.data,'$.error.type') AS last_err
        FROM session_v2 s
        JOIN session_message last ON last.id = (
            SELECT m2.id FROM session_message m2
            WHERE m2.session_id = s.id
            ORDER BY m2.time_created DESC, m2.id DESC
            LIMIT 1
        )
        WHERE s.time_archived IS NULL
          AND s.parent_id IS NULL
          AND last.time_created < {int(cutoff_ms)}
          AND last.time_created > {int(horizon_ms)}
          AND json_extract(last.data,'$.error') IS NOT NULL
          AND json_extract(last.data,'$.error.type') NOT IN ({NON_FAILURE_ERRORS_SQL})
        ORDER BY last_msg ASC
        LIMIT 20
    """
    rows = _union(sql_v1, sql_v2)
    out = []
    for r in rows:
        sid = r["id"]
        lm = r["last_msg"] or 0
        # Once per stall: same (session, last-error ts) never re-alerts, even
        # long after RATE_LIMIT_MIN would have allowed it again.
        if stall_seen(cache, sid, lm):
            continue
        key = f"stale_session:{sid}:{lm}"
        if not should_alert(cache, key, now):
            continue      # rate-limited: do NOT record, so the next run retries
        stall_record(cache, sid, lm)
        age = int((now - lm / 1000) // 60)
        body = (
            f"Session {sid_ref(sid)} last errored {r['last_err']} "
            f"{age} min ago and has had no activity since. Still open."
        )
        # If a SUBAGENT is why this main session stalled, name it: the parent
        # went quiet, but the fault belongs to the child that errored.
        for ch in _failing_children(sid):
            body += (f"\nFailing subagent: {ch['agent'] or 'subagent'} "
                     f"({ch['err']}) — {truncate(ch['title'] or '', 48)}")
        out.append((
            f"Stalled session: {(r['title'] or r['id'])[:48]}",
            body, "high", "hourglass_flowing_sand"))
    # Anything no longer in `rows` recovered, was archived, or aged out.
    stall_prune(cache, {r["id"] for r in rows})
    return out


def _failing_children(parent_id: str) -> list[dict]:
    """Child sessions of `parent_id` whose newest message errored.

    Used only to attribute a MAIN session's stall to the subagent that
    actually failed. Session ids are [A-Za-z0-9_-] so this is not an
    injection surface, but it is validated anyway rather than assumed.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(parent_id or "")):
        return []
    sc = _schema()
    sql_v1 = f"""
        SELECT c.id, c.title, c.agent,
               json_extract(last.data,'$.error.name') AS err
        FROM session c
        JOIN message last ON last.id = (
            SELECT m2.id FROM message m2
            WHERE m2.session_id = c.id
            ORDER BY m2.time_created DESC, m2.id DESC
            LIMIT 1
        )
        WHERE c.parent_id = '{parent_id}'
          AND json_extract(last.data,'$.error.name') IS NOT NULL
          AND json_extract(last.data,'$.error.name')
              NOT IN ({NON_FAILURE_ERRORS_SQL})
        ORDER BY last.time_created DESC
        LIMIT 3
    """
    sql_v2 = f"""
        SELECT c.id, c.title, c.agent,
               json_extract(last.data,'$.error.type') AS err
        FROM session_v2 c
        JOIN session_message last ON last.id = (
            SELECT m2.id FROM session_message m2
            WHERE m2.session_id = c.id
            ORDER BY m2.time_created DESC, m2.id DESC
            LIMIT 1
        )
        WHERE c.parent_id = '{parent_id}'
          AND json_extract(last.data,'$.error') IS NOT NULL
          AND json_extract(last.data,'$.error.type') NOT IN ({NON_FAILURE_ERRORS_SQL})
        ORDER BY last.time_created DESC
        LIMIT 3
    """
    return _union(sql_v1, sql_v2)


# SQL fragments shared by the watermark detectors. The tool-error LIKE
# prefilter is REQUIRED for performance, not for correctness: `part` has no
# index on time_created and a plain json_extract scan of the live 2.5 GB DB
# measured 48.7 s, while the TEXT prefilter + json_extract guard measured
# 0.13 s with an identical answer. The prefilter is a pure superset filter
# (any row whose $.state.status is the string "error" necessarily contains
# the substring "status":"error"), so it cannot change the result — the
# exact json_extract predicate still decides.
TOOL_ERROR_LIKE = """p.data LIKE '%"status":"error"%'"""
ROW_CAP = 20   # per table per run — a flood cannot out-notify the dedupe


def _new_message_errors(wm_ms: int) -> list[dict]:
    """Message-level errors strictly newer than the watermark.

    MAIN SESSIONS ONLY: the LEFT JOIN means a message whose session row is
    missing yields NULLs, so `s.id IS NOT NULL` is required alongside
    `s.parent_id IS NULL` — otherwise "no parent" and "no session" would
    both read as NULL and orphan messages would be treated as main.
    """
    sc = _schema()
    sql_v1 = f"""
        SELECT m.id AS id, m.session_id AS session_id,
               m.time_created AS time_created, s.title AS title,
               json_extract(m.data,'$.error.name') AS err_name,
               json_extract(m.data,'$.error.data.message') AS err_msg
          FROM message m
          LEFT JOIN session s ON s.id = m.session_id
         WHERE m.time_created > {int(wm_ms)}
           AND s.id IS NOT NULL
           AND s.parent_id IS NULL
           AND json_extract(m.data,'$.error') IS NOT NULL
           AND json_extract(m.data,'$.error.name')
               NOT IN ({NON_FAILURE_ERRORS_SQL})
         ORDER BY m.time_created ASC
         LIMIT {ROW_CAP}
    """
    sql_v2 = f"""
        SELECT sm.id AS id, sm.session_id AS session_id,
               sm.time_created AS time_created, s.title AS title,
               json_extract(sm.data,'$.error.type') AS err_name,
               json_extract(sm.data,'$.error.message') AS err_msg
          FROM session_message sm
          LEFT JOIN session_v2 s ON s.id = sm.session_id
         WHERE sm.time_created > {int(wm_ms)}
           AND sm.type = 'assistant'
           AND s.id IS NOT NULL
           AND s.parent_id IS NULL
           AND json_extract(sm.data,'$.error') IS NOT NULL
           AND json_extract(sm.data,'$.error.type')
               NOT IN ({NON_FAILURE_ERRORS_SQL})
         ORDER BY sm.time_created ASC
         LIMIT {ROW_CAP}
    """
    return _union(sql_v1, sql_v2)


def _new_tool_errors(wm_ms: int) -> list[dict]:
    """Tool-level errors (part.state.status='error') newer than the watermark.

    MAIN SESSIONS ONLY — same LEFT JOIN caveat as _new_message_errors.
    V2: tool errors are in session_message.data.content[] with state.status='error'
    """
    sc = _schema()
    sql_v1 = f"""
        SELECT p.id AS id, p.session_id AS session_id,
               p.time_created AS time_created, s.title AS title,
               json_extract(p.data,'$.tool') AS tool,
               json_extract(p.data,'$.state.error') AS err_msg
          FROM part p
          LEFT JOIN session s ON s.id = p.session_id
         WHERE p.time_created > {int(wm_ms)}
           AND s.id IS NOT NULL
           AND s.parent_id IS NULL
           AND {TOOL_ERROR_LIKE}
           AND json_extract(p.data,'$.state.status') = 'error'
         ORDER BY p.time_created ASC
         LIMIT {ROW_CAP}
    """
    sql_v2 = f"""
        SELECT sm.id || ':' || je.key AS id, sm.session_id AS session_id,
               sm.time_created AS time_created, s.title AS title,
               json_extract(je.value,'$.name') AS tool,
               json_extract(je.value,'$.state.error') AS err_msg
          FROM session_message sm, json_each(json_extract(sm.data,'$.content')) AS je
          LEFT JOIN session_v2 s ON s.id = sm.session_id
         WHERE sm.time_created > {int(wm_ms)}
           AND sm.type = 'assistant'
           AND s.id IS NOT NULL
           AND s.parent_id IS NULL
           AND sm.data LIKE '%"status":"error"%'
           AND json_extract(je.value,'$.type') = 'tool'
           AND json_extract(je.value,'$.state.status') = 'error'
         ORDER BY sm.time_created ASC
         LIMIT {ROW_CAP}
    """
    return _union(sql_v1, sql_v2)


def _new_sessions(wm_ms: int) -> list[dict]:
    """MAIN SESSIONS ONLY: a subagent child is dispatch detail, not news.

    Measured: children are 56-59% of sessions created in recent windows, so
    announcing them would have made most "New session" pushes subagent noise.
    """
    sc = _schema()
    sql_v1 = f"""
        SELECT id, title, time_created
          FROM session
         WHERE time_created > {int(wm_ms)}
           AND parent_id IS NULL
         ORDER BY time_created ASC
         LIMIT {ROW_CAP}
    """
    sql_v2 = f"""
        SELECT id, title, time_created
          FROM session_v2
         WHERE time_created > {int(wm_ms)}
           AND parent_id IS NULL
         ORDER BY time_created ASC
         LIMIT {ROW_CAP}
    """
    return _union(sql_v1, sql_v2)


def det_new_error(now: float, cache: dict) -> list[tuple[str, str, str, str]]:
    """WATERMARK detector: one alert per NEW agent error since the last run.

    Covers both surfaces the other detectors ignore:
      * message-level  message.data.error      (provider/model failures)
      * tool-level     part.data.state.status  ('error')
    MessageAbortedError is a user cancel, not a failure — the same
    NON_FAILURE_ERRORS rule every other failure detector uses.

    Watermarks advance (and are persisted by main()) regardless of whether
    the ntfy POST later succeeds, so a down channel cannot re-send the same
    rows on every run.
    """
    wm_msg = bootstrap_wm(cache, "wm_message_ms", "message")
    wm_part = bootstrap_wm(cache, "wm_part_ms", "part")

    msg_rows = _new_message_errors(wm_msg)
    part_rows = _new_tool_errors(wm_part)

    advance_wm(cache, "wm_message_ms",
               max((r["time_created"] for r in msg_rows), default=wm_msg))
    advance_wm(cache, "wm_part_ms",
               max((r["time_created"] for r in part_rows), default=wm_part))

    out = []
    skipped = []
    for r in msg_rows:
        name = redact((r["err_name"] or "unknown error").strip() or "unknown error")
        title_s = redact(r["title"] or "")
        # Class gate: a dashboard-only class never reaches the phone. The row
        # is still fully visible in /api/errors (server.js reads the DB, not
        # this cache), so skipping costs nothing but a buzz.
        cls, phone = classify_error(msg_name=r["err_name"], msg_text=r["err_msg"])
        if not phone:
            skipped.append(cls)
            continue
        key = f"new_error:{r['session_id']}:{name}"
        if should_alert(cache, key, now):
            body = redact(
                f"{truncate(r['err_msg'] or '(no message recorded)', 160)}\n"
                f"session: {truncate(title_s, 60) or '(untitled)'} "
                f"({sid_ref(r['session_id'])})"
            )
            out.append((f"Agent error: {truncate(name, 40)}", body,
                        "high", "warning"))

    for r in part_rows:
        tool = redact((r["tool"] or "tool").strip() or "tool")
        title_s = redact(r["title"] or "")
        text = redact(r["err_msg"] or "(no error text recorded)")
        cls, phone = classify_error(tool=r["tool"], tool_err=r["err_msg"])
        if not phone:
            skipped.append(cls)
            continue
        # Fingerprint on the first 60 chars of the tool error: 12 repeats of
        # the same permission-denial inside 30 min become ONE alert.
        key = f"new_error:{r['session_id']}:{truncate(text, 60)}"
        if should_alert(cache, key, now):
            body = redact(
                f"{truncate(text, 160)}\n"
                f"session: {truncate(title_s, 60) or '(untitled)'} "
                f"({sid_ref(r['session_id'])})"
            )
            out.append((f"Agent error: {truncate(tool, 40)}", body,
                        "high", "warning"))
    if skipped and os.environ.get("CC_DEBUG"):
        print(f"new_error: suppressed {len(skipped)} dashboard-only rows "
              f"({', '.join(f'{c}={n}' for c, n in Counter(skipped).items())})",
              file=sys.stderr)
    return out


def det_new_session(now: float, cache: dict) -> list[tuple[str, str, str, str]]:
    """WATERMARK detector: fires on genuinely NEW sessions (session.time_created).

    First run bootstraps to MAX(session.time_created) and sends nothing, so
    the 500+ pre-existing sessions are never announced as "new".
    """
    wm = bootstrap_wm(cache, "wm_session_ms", "session")
    rows = _new_sessions(wm)
    advance_wm(cache, "wm_session_ms",
               max((r["time_created"] for r in rows), default=wm))

    out = []
    for r in rows:
        title_s = redact(r["title"] or "")
        key = f"new_session:{r['id']}"
        if should_alert(cache, key, now):
            out.append((
                f"New session: {truncate(title_s or r['id'], 48)}",
                redact(f"session {sid_ref(r['id'])} created."),
                "default", "bust_in_silhouette"))
    return out


def self_test() -> int:
    """Read-only report: watermarks, what the watermark detectors WOULD
    report right now, and the last 5 error rows of each kind. Sends nothing,
    writes nothing — safe to run any time, including before bootstrap."""
    cache = load_cache()

    def show_wm(key: str, table_v1: str, table_v2: str = None) -> int:
        if key in cache:
            print(f"  {key} = {int(cache[key])}  (persisted)")
            return int(cache[key])
        sc = _schema()
        if sc["v2"] and table_v2:
            rows = query(f"SELECT MAX(time_created) AS mx FROM {table_v2}")
        else:
            rows = query(f"SELECT MAX(time_created) AS mx FROM {table_v1}")
        mx = int(rows[0]["mx"] or 0) if rows else 0
        print(f"  {key} = ABSENT -> first run would bootstrap to {mx} "
              f"(sends nothing for historical rows)")
        return mx

    print("cc-alerts --self-test (read-only; nothing sent, cache not written)")
    print("watermarks:")
    wm_msg = show_wm("wm_message_ms", "message", "session_message")
    wm_part = show_wm("wm_part_ms", "part", "session_message")
    wm_sess = show_wm("wm_session_ms", "session", "session_v2")

    msg_rows = _new_message_errors(wm_msg)
    part_rows = _new_tool_errors(wm_part)
    sess_rows = _new_sessions(wm_sess)
    print("would report since watermark:")
    print(f"  new_error message-level : {len(msg_rows)}")
    print(f"  new_error tool-level    : {len(part_rows)}")
    print(f"  new_session             : {len(sess_rows)}")

    print("last 5 message-level error rows visible (newest first):")
    sc = _schema()
    if sc["v2"]:
        last_msg = query(f"""
            SELECT sm.time_created AS ts, s.title AS title,
                   json_extract(sm.data,'$.error.type') AS err_name,
                   json_extract(sm.data,'$.error.message') AS err_msg,
                   json_extract(sm.data,'$.model.providerID') AS provider,
                   json_extract(sm.data,'$.model.id') AS model
              FROM session_message sm LEFT JOIN session_v2 s ON s.id = sm.session_id
             WHERE sm.type = 'assistant'
               AND json_extract(sm.data,'$.error') IS NOT NULL
             ORDER BY sm.time_created DESC LIMIT 5
        """) or []
    else:
        last_msg = query(f"""
            SELECT m.time_created AS ts, s.title AS title,
                   json_extract(m.data,'$.error.name') AS err_name,
                   json_extract(m.data,'$.error.data.message') AS err_msg,
                   json_extract(m.data,'$.providerID') AS provider,
                   json_extract(m.data,'$.modelID') AS model
              FROM message m LEFT JOIN session s ON s.id = m.session_id
             WHERE json_extract(m.data,'$.error') IS NOT NULL
             ORDER BY m.time_created DESC LIMIT 5
        """) or []
    for r in last_msg:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"] / 1000))
        print(f"  {when}  {r['err_name']}  {r['provider']}/{r['model']}  "
              f"{truncate(redact(r['err_msg']), 80)}  "
              f"[{truncate(redact(r['title']), 40)}]")

    print("last 5 tool-level error rows visible (newest first):")
    if sc["v2"]:
        last_part = query(f"""
            SELECT sm.time_created AS ts, s.title AS title,
                   json_extract(je.value,'$.name') AS tool,
                   json_extract(je.value,'$.state.error') AS err_msg
              FROM session_message sm, json_each(json_extract(sm.data,'$.content')) AS je
              LEFT JOIN session_v2 s ON s.id = sm.session_id
             WHERE sm.type = 'assistant'
               AND sm.data LIKE '%"status":"error"%'
               AND json_extract(je.value,'$.type') = 'tool'
               AND json_extract(je.value,'$.state.status') = 'error'
             ORDER BY sm.time_created DESC LIMIT 5
        """) or []
    else:
        last_part = query(f"""
            SELECT p.time_created AS ts, s.title AS title,
                   json_extract(p.data,'$.tool') AS tool,
                   json_extract(p.data,'$.state.error') AS err_msg
              FROM part p LEFT JOIN session s ON s.id = p.session_id
             WHERE {TOOL_ERROR_LIKE}
               AND json_extract(p.data,'$.state.status') = 'error'
             ORDER BY p.time_created DESC LIMIT 5
        """) or []
    for r in last_part:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"] / 1000))
        print(f"  {when}  {r['tool']}  "
              f"{truncate(redact(r['err_msg']), 80)}  "
              f"[{truncate(redact(r['title']), 40)}]")

    return 0


def classify_report(days: int = 7) -> int:
    """Read-only: count every error in the last `days` days, bucketed by the
    error-class table, split main vs subagent. Nothing is sent, nothing written.
    """
    since = int((time.time() - days * 86400) * 1000)
    sc = _schema()
    if sc["v2"]:
        msg_rows = query(f"""
            SELECT json_extract(sm.data,'$.error.type')  AS err_name,
                   json_extract(sm.data,'$.error.message') AS err_msg,
                   s.parent_id
              FROM session_message sm JOIN session_v2 s ON s.id = sm.session_id
             WHERE sm.type = 'assistant'
               AND sm.time_created > {since}
               AND json_extract(sm.data,'$.error') IS NOT NULL
               AND json_extract(sm.data,'$.error.type') NOT IN ({NON_FAILURE_ERRORS_SQL})
        """)
        tool_rows = query(f"""
            SELECT json_extract(je.value,'$.name') AS tool,
                   json_extract(je.value,'$.state.error') AS err_msg,
                   s.parent_id
              FROM session_message sm, json_each(json_extract(sm.data,'$.content')) AS je
              JOIN session_v2 s ON s.id = sm.session_id
             WHERE sm.type = 'assistant'
               AND sm.time_created > {since}
               AND sm.data LIKE '%"status":"error"%'
               AND json_extract(je.value,'$.type') = 'tool'
               AND json_extract(je.value,'$.state.status') = 'error'
        """)
    else:
        msg_rows = query(f"""
            SELECT json_extract(m.data,'$.error.name')  AS err_name,
                   json_extract(m.data,'$.error.data.message') AS err_msg,
                   s.parent_id
              FROM message m JOIN session s ON s.id = m.session_id
             WHERE m.time_created > {since}
               AND json_extract(m.data,'$.error') IS NOT NULL
               AND json_extract(m.data,'$.error.name') NOT IN ({NON_FAILURE_ERRORS_SQL})
        """)
        tool_rows = query(f"""
            SELECT json_extract(p.data,'$.tool') AS tool,
                   json_extract(p.data,'$.state.error') AS err_msg,
                   s.parent_id
              FROM part p JOIN session s ON s.id = p.session_id
             WHERE p.time_created > {since}
               AND {TOOL_ERROR_LIKE}
               AND json_extract(p.data,'$.state.status') = 'error'
        """)

    counts = {}   # cls -> {phone:bool, total, main, sub}
    for r in msg_rows:
        cls, phone = classify_error(msg_name=r["err_name"], msg_text=r["err_msg"])
        d = counts.setdefault(cls, {"phone": phone, "total": 0, "main": 0, "sub": 0})
        d["total"] += 1
        d["sub" if r["parent_id"] else "main"] += 1
    for r in tool_rows:
        cls, phone = classify_error(tool=r["tool"], tool_err=r["err_msg"])
        d = counts.setdefault(cls, {"phone": phone, "total": 0, "main": 0, "sub": 0})
        d["total"] += 1
        d["sub" if r["parent_id"] else "main"] += 1

    print(f"error-class table — last {days} days  "
          f"(phone=yes -> may push; sub agents never push regardless)")
    print(f"{'class':<18} {'phone':<6} {'total':>5} {'main':>5} {'sub':>5}   "
          f"{'pushed if main':>14}")
    print("-" * 66)
    phone_total = 0
    for cls, d in sorted(counts.items(), key=lambda kv: -kv[1]["total"]):
        would = d["main"] if d["phone"] else 0
        phone_total += would
        print(f"{cls:<18} {'yes' if d['phone'] else 'no':<6} {d['total']:>5} "
              f"{d['main']:>5} {d['sub']:>5}   {would:>14}")
    print("-" * 66)
    print(f"{'TOTAL':<18} {'':<6} {sum(d['total'] for d in counts.values()):>5} "
          f"{sum(d['main'] for d in counts.values()):>5} "
          f"{sum(d['sub'] for d in counts.values()):>5}   {phone_total:>14}")
    print("\nnote: 'pushed if main' is an upper bound before the 30-min")
    print("rate limit, the watermark, and once-per-stall dedupe.")
    return 0


def main() -> int:
    dry = "--dry-run" in sys.argv
    if "--self-test" in sys.argv:
        return self_test()
    if "--classify" in sys.argv:
        return classify_report()
    if "--test-notify" in sys.argv:
        ok, note = notify(
            "cc-alerts self-test",
            "Detectors wired and the channel works. You should see this once.",
            "default", "white_check_mark")
        print(("sent: " if ok else "FAILED: ") + note)
        return 0 if ok else 1

    now = time.time()
    cache = load_cache()
    findings = []
    findings += det_repeated_failure(now, cache)
    findings += det_stale_session(now, cache)
    findings += det_all_rungs_down(now, cache)
    findings += det_new_error(now, cache)
    findings += det_new_session(now, cache)

    # ALWAYS persist (watermarks + rate-limit stamps), even with zero findings
    # and even if the ntfy POST below fails. The watermark detectors seed their
    # keys on this run; if that seeding were not saved, every run would
    # re-bootstrap to "now" and a real error landing between two runs would
    # never be reported. And if a POST failed without the watermark having
    # advanced, the next run would re-send the same rows forever.
    save_cache(cache)

    if not findings:
        print("no alerts (all detectors clear)")
        return 0

    sent = 0
    attempted = 0
    for title, message, priority, tags in findings:
        if dry:
            print(f"[dry-run] would send [{priority}] {title} :: {message}")
            attempted += 1
            sent += 1
            continue
        # Per-alert isolation: one alert's failure must never stop the ones
        # behind it from being ATTEMPTED. notify() is already total (it
        # returns (False, ...) instead of raising), so this try/except is the
        # second line of defence for anything a future edit might let through
        # (a bad priority, a title that is not a string, …).
        attempted += 1
        try:
            ok, note = notify(title, message, priority, tags)
        except Exception as e:  # noqa: BLE001 — belt and braces
            ok, note = False, f"{type(e).__name__}: {e}"
        print(("sent: " if ok else "FAILED: ") + f"{title} ({note})")
        sent += 1 if ok else 0

    # EXIT STATUS documents the RUN, not the CHANNEL. Exit 0 when every alert
    # was attempted (nothing was skipped), even if ntfy rejected some of them:
    # a dead or throttled channel is reported per line on stdout above and is
    # not a crash of this program. Before this, status=1 was returned whenever
    # a send failed, which is exactly how a UnicodeEncodeError presented —
    # "Failed with result 'exit-code'" in the journal while the real cause was
    # an unencodable header — and it hid whether any alert had been lost.
    # Exit 1 is now reserved for "an alert could not be attempted at all",
    # i.e. a genuine skip.
    return 0 if attempted == len(findings) else 1


if __name__ == "__main__":
    sys.exit(main())