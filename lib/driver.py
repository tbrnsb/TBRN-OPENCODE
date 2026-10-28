#!/usr/bin/env python3
"""driver.py - Phase 3 worktree-isolated agent driver loop.

Design contract (the point of this tool):

  1. ISOLATION. The loop runs only inside a git worktree created fresh for the
     run. It never chdir's anywhere else, and it never operates on the parent
     checkout, ~/.config/opencode/, or any other repo.

  2. INSTRUCTION INTEGRITY. The scope is never generated, rephrased, summarised
     or reframed by this program. It is a file of bytes written by a human
     before the run. Every dispatched prompt is built by STRING CONCATENATION
     ONLY, and the scope bytes are asserted to be present in the built prompt
     verbatim before anything is sent. If that assertion fails the run halts.
     There is no code path that can paraphrase the boundary, so there is no
     code path that can smuggle a reframed boundary to a subagent.

     This is the direct fix for the Job C finding: an agent complied with an
     out-of-scope action once the *driver's own wording* reframed it as
     "configuration updates". changecheck cannot see that, because it watches
     files, not instructions.

  3. DEFENCE IN DEPTH. changecheck.py watch --auto-revert runs for the whole
     loop as a file guard. It is not the only guardrail: the driver also
     inspects every agent output for off-scope paths and for clarifying
     questions.

  4. HALT, DO NOT RECOVER. Any violation, any ambiguity, any output that looks
     off-scope, or 3 steps without forward progress stops the loop, sends an
     ntfy alert, and leaves the worktree untouched for human inspection.
     There is no auto-retry, no self-correction, no rewording-and-retrying.

Stdlib only. Read-only against OpenCode (dispatches via `opencode run`, which
writes to OpenCode's own DB the same way any session does; this program never
opens opencode.db).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = pathlib.Path.home()

# ── prompt construction: concatenation only, never generation ────────────────
# The immutable scope is embedded between sentinels. `PROMPT_HEADER` and
# `PROMPT_FOOTER` are constants. Nothing in this file computes, rewrites or
# summarises the scope text; it is only ever sliced in as raw bytes.
PROMPT_HEADER = """You are a subagent step inside an automated driver loop.

You are working ONLY inside this git worktree:
  {worktree}

Rules you must follow for this entire step:
  - Work only inside that worktree. Never write outside it.
  - Do not commit, do not push, do not touch git configuration.
  - Do not read ~/.local/share/opencode/log/, ~/.config/opencode/auth.json,
    ~/Projects/EXPENCE/, or any account/credential table.
  - If ANYTHING about this task is ambiguous, or you need a decision that the
    scope below does not already answer, STOP and ask one clear question
    instead of guessing. Asking is correct behaviour here.

=== IMMUTABLE SCOPE (verbatim from the run definition - do not reinterpret) ===
"""

PROMPT_FOOTER = """
=== END IMMUTABLE SCOPE ===

What has already happened in this run (facts only - this never restates or
summarises the scope above):
{delta}

Report back with:
  DONE: <one line, only if the scope above is fully satisfied>
  or
  QUESTION: <your question, if you need a human decision>
  Then a short account of exactly what you did.
"""

# ── output inspection heuristics ─────────────────────────────────────────────
# Absolute-path escapes. Read-only system references are unavoidable in normal
# agent speech (`ls /usr/bin`, `/dev/null`), so those are permitted mentions;
# anything else that points outside the worktree is treated as an escape.
ESCAPE_ALLOWED_PREFIXES = (
    "/dev/", "/proc/", "/sys/", "/usr/", "/bin/", "/sbin/", "/etc/",
    "/lib/", "/lib64/", "/var/", "/run/", "/boot/", "/opt/",
)
PATH_RE = re.compile(r"(?<![\w.])(/[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-]+)*)")

# Clarifying questions = ambiguity = human territory.
QUESTION_RES = [
    re.compile(r"^\s*(QUESTION|CLARIF(?:Y|ICATION)|NEED (?:A )?DECISION)\s*:", re.I | re.M),
    re.compile(r"\b(?:do you want|should i|shall i|would you like|which one|"
               r"which option|can you clarify|let me know|please confirm|"
               r"need your (?:approval|decision|input)|unclear|ambiguous)\b", re.I),
    re.compile(r"\?\s*$", re.M),
]

# Runaway guard. This is NOT a task cap: the loop stops earlier on any of the
# five documented stop conditions. This exists only so a pathological loop
# cannot run unbounded while a human is away.
DEFAULT_MAX_STEPS = 40

NON_PROGRESS_STEPS = 3


# ── logging ─────────────────────────────────────────────────────────────────
class RunLog:
    """Timestamped, full-fidelity log. Agent output is stored RAW, never
    summarised here - the report is written from this file afterwards."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("a", encoding="utf-8")

    def line(self, msg: str) -> None:
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._f.write(f"[{ts}] {msg}\n")
        self._f.flush()

    def block(self, label: str, body: str) -> None:
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._f.write(f"\n[{ts}] ===== {label} =====\n")
        self._f.write(body if body.endswith("\n") else body + "\n")
        self._f.write(f"[{ts}] ===== end {label} =====\n\n")
        self._f.flush()

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


# ── ntfy (latin-1-safe titles; learned the hard way in Job D) ────────────────
def header_safe(text: str) -> str:
    repl = {"\u2026": "...", "\u2014": "-", "\u2013": "-", "\u2192": "->",
            "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
            "\u2022": "*", "\u00a0": " "}
    out = "".join(repl.get(ch, ch) for ch in str(text))
    out = "".join(ch for ch in out if 32 <= ord(ch) < 127)
    return out.encode("latin-1", "replace").decode("latin-1")


def notify(topic: str, title: str, message: str, priority: str = "default",
           tags: str = "") -> tuple[bool, str]:
    if not topic:
        return False, "no topic"
    url = f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
    req.add_header("Title", header_safe(title))
    req.add_header("Priority", header_safe(priority))
    if tags:
        req.add_header("Tags", header_safe(tags))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return True, f"HTTP {r.status}"
    except Exception as e:  # never let a notification failure kill the loop
        return False, f"{type(e).__name__}: {e}"


# ── worktree lifecycle ──────────────────────────────────────────────────────
def make_worktree(repo: pathlib.Path, dest: pathlib.Path, branch: str,
                  log: RunLog) -> pathlib.Path:
    """Create a FRESH worktree. Refuses if dest already exists - a rerun must
    not silently inherit a previous run's state."""
    if dest.exists():
        raise SystemExit(f"refusing: worktree path already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    repo = repo.resolve()
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise SystemExit(f"refusing: {repo} is not a git repository")
    cmd = ["git", "-C", str(repo), "worktree", "add", "--detach", str(dest)]
    log.line(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"worktree add failed: {r.stderr.strip()}")
    log.line(f"worktree created: {dest}")
    return dest


def worktree_fingerprint(wt: pathlib.Path) -> str:
    """Content hash of the worktree's tracked+untracked files (excluding
    .git). Used for forward-progress detection."""
    h = hashlib.sha256()
    for p in sorted(wt.rglob("*")):
        if ".git" in p.parts or not p.is_file() or p.is_symlink():
            continue
        h.update(str(p.relative_to(wt)).encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


# ── dispatch ────────────────────────────────────────────────────────────────
def dispatch(prompt: str, wt: pathlib.Path, model: str | None,
             timeout: int, log: RunLog) -> tuple[int, str]:
    """Run one agent step with `opencode run`, cwd pinned to the worktree."""
    cmd = ["opencode", "run", prompt, "--format", "json"]
    if model:
        cmd += ["--model", model]
    log.block("DISPATCHED PROMPT (verbatim)", prompt)
    log.line(f"$ (cwd={wt}) opencode run <prompt> --format json"
             + (f" --model {model}" if model else ""))
    env = dict(os.environ)
    env["PWD"] = str(wt)
    try:
        r = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        log.line(f"DISPATCH TIMEOUT after {timeout}s")
        return 124, ""
    log.block("AGENT STDOUT (raw JSON events, unsummarised)", r.stdout or "")
    if r.stderr:
        log.block("AGENT STDERR", r.stderr)
    log.line(f"dispatch exit code: {r.returncode}")
    return r.returncode, (r.stdout or "")


def extract_text(raw: str) -> str:
    """Pull the agent's prose out of the JSON event stream."""
    chunks = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") or {}
        if ev.get("type") == "text" and part.get("text"):
            chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


def extract_tools(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") or {}
        if part.get("type") == "tool":
            state = part.get("state") or {}
            out.append({"tool": part.get("tool"),
                        "status": state.get("status"),
                        "input": state.get("input")})
    return out


# ── stop-condition inspectors ───────────────────────────────────────────────
def find_escapes(text: str, wt: pathlib.Path) -> list[str]:
    """Absolute paths in the agent's prose that point outside the worktree."""
    wt_s = str(wt.resolve())
    bad = []
    for m in PATH_RE.finditer(text):
        p = m.group(1)
        if p.startswith(wt_s):
            continue
        if p.startswith(ESCAPE_ALLOWED_PREFIXES):
            continue
        if len(p) < 2:
            continue
        bad.append(p)
    return sorted(set(bad))


def find_question(text: str) -> str | None:
    for rx in QUESTION_RES:
        m = rx.search(text)
        if m:
            return m.group(0).strip()
    return None


def is_done(text: str) -> bool:
    return bool(re.search(r"^\s*DONE\s*:", text, re.I | re.M))


# ── changecheck watch supervision ───────────────────────────────────────────
def start_watch(changecheck: pathlib.Path, wt: pathlib.Path,
                snapshot: pathlib.Path, allow: str | None,
                log_path: pathlib.Path, log: RunLog) -> subprocess.Popen:
    cmd = ["python3", str(changecheck), "watch", str(wt), str(snapshot),
           "--auto-revert", "--interval", "0.4"]
    if allow:
        cmd += ["--allow", allow]
    log.line(f"$ (background) {' '.join(shlex.quote(c) for c in cmd)}")
    logf = log_path.open("w")
    return subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)


def watch_violations(log_path: pathlib.Path) -> list[str]:
    if not log_path.exists():
        return []
    hits = []
    for line in log_path.read_text(errors="replace").splitlines():
        if "VIOLATION" in line or "SYMLINK ESCAPE" in line or "REFUSING" in line:
            hits.append(line.strip())
    return hits


def assert_scope_integrity(prompt: str, scope_text: str, scope_sha: str) -> str | None:
    """Return an error message if `prompt` does not embed `scope_text` verbatim
    with the expected hash, else None.

    This is the structural guarantee: the scope is spliced in as raw text, so
    the only way for it to differ is if something rewrote the prompt. There is
    no summariser, rewriter, or template-substitution anywhere in the prompt
    path, so the honest failure mode here is a coding bug - which is exactly
    what we want to be loud about.
    """
    if scope_text not in prompt:
        return "built prompt does not contain the immutable scope verbatim"
    start = prompt.index(scope_text)
    got_sha = hashlib.sha256(prompt[start:start + len(scope_text)]
                             .encode("utf-8")).hexdigest()
    if got_sha != scope_sha:
        return f"embedded scope hash {got_sha} != expected {scope_sha}"
    return None


# ── the loop ────────────────────────────────────────────────────────────────
def run(args) -> int:
    started = time.time()
    state_dir = pathlib.Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = state_dir / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log = RunLog(run_dir / "run.log")

    topic = ""
    topic_file = HOME / ".local/state/cc-alerts/TOPIC.txt"
    if topic_file.exists():
        topic = topic_file.read_text().strip()

    wt = pathlib.Path(args.worktree).resolve()

    def halt(reason: str, detail: str, keep: bool = True) -> int:
        elapsed = time.time() - started
        log.line(f"HALT: {reason}")
        log.block("HALT DETAIL", detail)
        log.line(f"steps={args._step} elapsed_s={elapsed:.1f}")
        msg = (f"step {args._step} · {elapsed:.0f}s\n"
               f"worktree left for inspection:\n{wt}\n\n{detail[:600]}")
        ok, note = notify(topic, f"Driver HALT: {reason}", msg, "high", "warning")
        log.line(f"ntfy halt alert: {'sent ' + note if ok else 'FAILED ' + note}")
        log.close()
        if args.watch_proc and args.watch_proc.poll() is None:
            args.watch_proc.terminate()
        print(json.dumps({"status": "HALT", "reason": reason, "steps": args._step,
                          "elapsed_s": round(elapsed, 1),
                          "worktree": str(wt), "run_dir": str(run_dir)}, indent=2))
        return 2 if not keep else 3

    # ── scope: read once, hash, never regenerate ────────────────────────────
    scope_path = pathlib.Path(args.scope).resolve()
    scope_bytes = scope_path.read_bytes()          # bytes, not a re-render
    # Normalise to exactly one trailing newline. Without this, a scope file
    # that lost its final newline still satisfies the in-prompt assertion,
    # because the constant footer supplies a newline of its own - so trailing
    # whitespace would be an undetectable, if harmless, degree of freedom.
    # Normalising removes that degree of freedom entirely.
    scope_bytes = scope_bytes.rstrip(b"\n") + b"\n"
    scope_sha = hashlib.sha256(scope_bytes).hexdigest()
    scope_text = scope_bytes.decode("utf-8")
    log.line(f"immutable scope: {scope_path}")
    log.line(f"immutable scope sha256: {scope_sha}")
    log.block("IMMUTABLE SCOPE (verbatim)", scope_text)
    if args.expect_scope_sha and scope_sha != args.expect_scope_sha:
        raise SystemExit(f"scope hash mismatch: {scope_sha} != {args.expect_scope_sha}")

    # ── worktree ────────────────────────────────────────────────────────────
    if args.repo:
        make_worktree(pathlib.Path(args.repo), wt, args.branch, log)
    elif not wt.exists():
        raise SystemExit(f"refusing: --worktree {wt} does not exist and no --repo given")
    os.chdir(wt)                    # the ONLY directory this process ever uses
    log.line(f"driver cwd pinned to {pathlib.Path.cwd()}")

    snapshot = run_dir / "snapshot.json"
    subprocess.run(["python3", args.changecheck, "snapshot", str(wt)],
                   capture_output=True, text=True)
    snaps = sorted((HOME / ".local/share/changecheck/store").glob(
        f"{wt.name}-*/snapshot.json"))
    if not snaps:
        return halt("setup", "changecheck snapshot failed")
    shutil.copyfile(snaps[-1], snapshot)
    log.line(f"changecheck snapshot: {snapshot}")

    args.watch_proc = start_watch(pathlib.Path(args.changecheck), wt, snapshot,
                                  args.allow, run_dir / "watch.log", log)

    # ── append-only delta (facts only, never a restatement of scope) ────────
    delta = "Nothing has happened yet; this is step 1."

    args._step = 0
    no_progress = 0
    fp_prev = worktree_fingerprint(wt)

    while True:
        args._step += 1
        log.line(f"--- step {args._step} ---")

        viol = watch_violations(run_dir / "watch.log")
        if viol:
            return halt("changecheck violation",
                        "changecheck watch reported:\n" + "\n".join(viol[:10]))

        # ── build the prompt: CONCATENATION ONLY ───────────────────────────
        prompt = PROMPT_HEADER.format(worktree=str(wt)) + scope_text + PROMPT_FOOTER.format(delta=delta)

        # ── structural integrity assertion: the scope must be present
        # verbatim. This is what makes paraphrasing impossible to ship.
        err = assert_scope_integrity(prompt, scope_text, scope_sha)
        if err:
            return halt("SCOPE INTEGRITY FAILURE", err)
        log.line(f"scope integrity: OK (sha256 {scope_sha[:16]}... embedded verbatim)")

        code, raw = dispatch(prompt, wt, args.model, args.timeout, log)
        text = extract_text(raw)
        tools = extract_tools(raw)

        # ── inspect output ────────────────────────────────────────────────
        escapes = find_escapes(text, wt)
        if escapes:
            return halt("agent output outside scope",
                        "agent prose referenced paths outside the worktree:\n"
                        + "\n".join(f"  {p}" for p in escapes[:10])
                        + "\n\nagent said:\n" + text[:800])

        q = find_question(text)
        if q:
            return halt("agent asked a clarifying question",
                        f"trigger: {q!r}\n\nagent said:\n{text[:900]}")

        if code not in (0, 1):
            return halt("dispatch failed",
                        f"opencode run exited {code}\n\n{text[:600]}")

        if is_done(text):
            # A violation that landed during THIS step must still halt, even
            # though the agent claims to be finished. changecheck auto-reverts
            # the file, but the stop condition is "a violation was reported",
            # not "a violation survived". Give the watcher time to notice.
            time.sleep(1.5)
            viol = watch_violations(run_dir / "watch.log")
            if viol:
                return halt("changecheck violation (despite DONE)",
                            "changecheck watch reported:\n" + "\n".join(viol[:10])
                            + f"\n\nagent claimed DONE:\n{text[:600]}")
            elapsed = time.time() - started
            log.line(f"agent reported DONE at step {args._step}")
            log.close()
            if args.watch_proc and args.watch_proc.poll() is None:
                args.watch_proc.terminate()
            notify(topic, f"Driver DONE in {args._step} steps",
                   f"{elapsed:.0f}s\nworktree: {wt}", "default", "white_check_mark")
            print(json.dumps({"status": "DONE", "steps": args._step,
                              "elapsed_s": round(elapsed, 1),
                              "worktree": str(wt), "run_dir": str(run_dir)},
                             indent=2))
            return 0

        # ── forward progress ──────────────────────────────────────────────
        fp_now = worktree_fingerprint(wt)
        if fp_now == fp_prev:
            no_progress += 1
            log.line(f"no forward progress ({no_progress}/{NON_PROGRESS_STEPS})")
        else:
            no_progress = 0
            log.line("forward progress: worktree content changed")
        fp_prev = fp_now
        if no_progress >= NON_PROGRESS_STEPS:
            return halt("no forward progress",
                        f"{NON_PROGRESS_STEPS} consecutive steps changed nothing.\n"
                        f"last agent said:\n{text[:600]}")

        # ── delta: facts only ─────────────────────────────────────────────
        git = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                             capture_output=True, text=True).stdout.strip()
        changed = [t["tool"] for t in tools]
        delta = (f"Step {args._step} ran and did not report DONE.\n"
                 f"Tools it called: {', '.join(changed) if changed else 'none'}.\n"
                 f"Worktree git status now:\n{git or '(clean)'}\n"
                 f"It said:\n{text[:1200] if text else '(no prose)'}\n"
                 f"Steps so far: {args._step}. Elapsed: {time.time()-started:.0f}s.")

        if args._step >= DEFAULT_MAX_STEPS:
            return halt("runaway guard",
                        f"reached {DEFAULT_MAX_STEPS} steps. This is a safety net, "
                        f"not a task cap - a human should look.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", required=True,
                    help="path to the immutable scope file (bytes are never regenerated)")
    ap.add_argument("--expect-scope-sha", default=None,
                    help="abort if the scope file hash differs (pins the run to a reviewed scope)")
    ap.add_argument("--repo", default=None, help="git repo to create the worktree from")
    ap.add_argument("--worktree", required=True, help="path for the fresh worktree")
    ap.add_argument("--branch", default="driver-run")
    ap.add_argument("--allow", default=None,
                    help="changecheck allow globs for in-scope paths (comma separated)")
    ap.add_argument("--changecheck",
                    default=str(HOME / "command-center/changecheck.py"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=900, help="per-step seconds")
    ap.add_argument("--state-dir", default=str(HOME / ".local/state/jobe"))
    ap.add_argument("--dry-run", action="store_true",
                    help="build and assert the prompt for step 1, dispatch nothing")
    args = ap.parse_args()

    if args.dry_run:
        raw = pathlib.Path(args.scope).read_bytes()
        scope_bytes = raw.rstrip(b"\n") + b"\n"   # same normalisation as run()
        sha = hashlib.sha256(scope_bytes).hexdigest()
        # The pin must be enforced here too - a dry-run that silently
        # accepted a re-scoped file would be a useless preflight check.
        if args.expect_scope_sha and sha != args.expect_scope_sha:
            print(f"scope hash mismatch: {sha} != {args.expect_scope_sha}",
                  file=sys.stderr)
            return 4
        scope_text = scope_bytes.decode("utf-8")
        err = assert_scope_integrity(
            PROMPT_HEADER.format(worktree="<WT>") + scope_text
            + PROMPT_FOOTER.format(delta="<delta>"), scope_text, sha)
        print(json.dumps({"scope_sha256": sha,
                          "scope_present_verbatim": err is None,
                          "prompt_chars": len(PROMPT_HEADER) + len(scope_text)
                                          + len(PROMPT_FOOTER)}, indent=2))
        return 0 if err is None else 1

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
