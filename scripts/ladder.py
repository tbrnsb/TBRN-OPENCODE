#!/usr/bin/env python3
"""Failure ladder executor (also the dry-run harness).

Walks a role's rungs from chains.json: check health → run command → on
failure (exit != 0, marker missing, or classified 429/timeout/5xx/empty/
garbage) retry once → mark down with the right reason → next rung. If every
rung fails, exit 3 (orchestrator takes over / writes HANDOFF).

    ladder.py <role> -- <command> [args...]
    ladder.py --dry-run <role>      # fake 429 on rung 1, success on rung 2
    ladder.py --pick-critic <state> # first critic rung whose family differs
                                    # from EVERY model in STATE.md author_models
    ladder.py --write-handoff <path>  # write HANDOFF.md skeleton (takeover path)

The command's stdout must contain READY-<role> (dry run) or the caller checks
output itself; exit code is what the ladder trusts.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

CFG = pathlib.Path(os.environ.get("STAGING", pathlib.Path.home() / ".config/opencode"))
# lib/ ships next to scripts/ in both the live tree and the repo; CFG may be
# a nested config mirror (repo: config/live) that has no lib of its own.
LIBROOT = pathlib.Path(__file__).resolve().parent.parent / "lib"
HEALTH = [sys.executable, str(LIBROOT / "health.py")]
LOG = [sys.executable, str(LIBROOT / "log-rung.py")]
SUFFIXES = ["", "-2", "-3", "-last"]


def suffixes_for(count: int) -> list[str]:
    """Rung file suffixes for a 3- or 4-rung role (last rung is always -last)."""
    if count == 4:
        return list(SUFFIXES)
    if count == 3:
        return ["", "-2", "-last"]
    raise ValueError(f"need 3-4 rungs, got {count}")


def load_chains() -> dict:
    return json.loads((CFG / "chains.json").read_text())


def rung_names(role: str) -> list[str]:
    return load_chains()["roles"][role]["rungs"]


def classify(tail: str) -> str:
    """Map failure output to a health-registry reason (decision 1)."""
    t = tail.lower()
    if "429" in t or "rate" in t and "limit" in t:
        return "429"
    if "timeout" in t or "timed out" in t:
        return "timeout"
    if re.search(r"\b5\d\d\b", t) or "internal server" in t or "bad gateway" in t \
       or "service unavailable" in t or "unexpected server error" in t:
        return "5xx"
    if "context" in t and ("overflow" in t or "exceed" in t or "too long" in t):
        return "garbage"
    return "empty"


def run_cmd(cmd: list[str], timeout: int) -> tuple[bool, float, str]:
    started = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (time.time() - started) * 1000, p.stdout[-200:] + p.stderr[-200:]
    except subprocess.TimeoutExpired:
        return False, (time.time() - started) * 1000, "timeout"


def family(model: str, chains: dict) -> str:
    for fam, members in chains.get("families", {}).items():
        if fam.startswith("_"):
            continue
        if model in members:
            return fam
    return model


MODEL_TOKEN = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w./-]*$")


def _tokens(line: str) -> list[str]:
    """Model IDs on one line.

    The first whitespace token of a bullet (``- provider/model — why``) is the
    ID; any prose after it is ignored so that words like ``config/schema``
    never enter the author list.
    """
    body = line.strip().lstrip("-*").strip()
    if not body:
        return []
    first = body.split()[0].strip(",;`'\"")
    return [first] if MODEL_TOKEN.match(first) else []


def parse_author_models(state_path: str) -> list[str]:
    """Read the models that edited the diff from the LATEST turn in STATE.md.

    STATE.md is append-only, so only the last ``author_models`` section counts;
    reusing an older turn's list would silently pick a same-family critic.
    Accepts either ``author_models: a/b, c/d`` or a heading followed by bullets.
    """
    lines = pathlib.Path(state_path).read_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if "author_models" in ln]
    if not starts:
        # Legacy single-field fallback (pre-plural STATE.md files).
        for ln in lines:
            m = re.search(r"author_model:\s*(\S+)", ln)
            if m:
                return [m.group(1)]
        return []

    ids: list[str] = []
    head = lines[starts[-1]]
    inline = head.split("author_models", 1)[1].lstrip(":").strip()
    if inline and not inline.startswith("```"):
        for item in inline.split(","):
            ids += _tokens(item)
    for ln in lines[starts[-1] + 1:]:
        if ln.lstrip().startswith(("#", "```", "---")):
            break
        if not ln.strip():
            continue
        ids += _tokens(ln)

    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def pick_critic(state_path: str) -> int:
    chains = load_chains()
    authors = parse_author_models(state_path)
    if not authors:
        print("no author_models found in STATE.md — refusing to pick a critic",
              file=sys.stderr)
        return 4
    author_fams = {family(a, chains) for a in authors}
    print(f"author_models: {authors}")
    print(f"author families: {sorted(author_fams)}")
    critic_rungs = rung_names("critic")
    suffixes = suffixes_for(len(critic_rungs))
    for i, model in enumerate(critic_rungs):
        fam = family(model, chains)
        if fam in author_fams:
            print(f"[critic{suffixes[i]}] {model} ({fam}): SAME FAMILY as an "
                  f"author — refused")
            continue
        rung = suffixes[i] or "base"
        print(f"[critic{rung or 'base'}] {model} ({fam}): OK — differs from "
              f"every author family")
        return 0
    print("no critic rung differs from every author family — escalate", file=sys.stderr)
    return 4


def write_handoff(path: str) -> int:
    p = pathlib.Path(path)
    p.write_text(f"""# HANDOFF — <goal>

Created: {time.strftime('%Y-%m-%d %H:%M:%S')}

- Task: <what was being done when the chain/orchestrator failed>
- Decisions: <decisions made and why>
- Files changed: <paths>
- Next step: <the single next action for whoever picks this up>
""")
    print(f"HANDOFF written to {p}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--write-handoff" in argv:
        i = argv.index("--write-handoff")
        if len(argv) <= i + 1:
            print("usage: ladder.py --write-handoff <path>", file=sys.stderr)
            return 2
        return write_handoff(argv[i + 1])
    if "--pick-critic" in argv:
        i = argv.index("--pick-critic")
        if len(argv) <= i + 1:
            print("usage: ladder.py --pick-critic <state-file>", file=sys.stderr)
            return 2
        return pick_critic(argv[i + 1])

    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if not args:
        print("usage: ladder.py [--dry-run] <role> -- <command> [args...]", file=sys.stderr)
        return 2
    role = args[0]
    cmd = args[2:] if len(args) > 1 and args[1] == "--" else []
    rungs = rung_names(role)
    if len(rungs) not in (3, 4):
        print(f"{role}: chains.json must list 3 or 4 rungs (got {len(rungs)})",
              file=sys.stderr)
        return 2
    suffixes = suffixes_for(len(rungs))

    for rung_i, model in enumerate(rungs):
        rung = suffixes[rung_i]
        chk = subprocess.run(HEALTH + ["check", model], capture_output=True)
        if chk.returncode != 0:
            subprocess.run(LOG + [role, rung or "base", model, "down", "0"])
            print(f"[{role}{rung}] {model}: in cooldown, skipping")
            continue
        for attempt in (1, 2):
            if dry:
                ok, ms, tail = (False, 12.0, "HTTP 429 rate-limited") if rung_i == 0 else (True, 34.0, "READY")
            else:
                if not cmd:
                    print("no command given", file=sys.stderr)
                    return 2
                ok, ms, tail = run_cmd(cmd, 300)
            subprocess.run(LOG + [role, rung or "base", model,
                                  "ok" if ok else "retry", str(int(ms))])
            print(f"[{role}{rung}] {model}: attempt {attempt} -> {'ok' if ok else 'FAIL'} ({tail[:60]!r})")
            if ok:
                return 0
            if attempt == 1:
                time.sleep(2)
        reason = "429" if dry and rung_i == 0 else classify(tail)
        subprocess.run(HEALTH + ["down", model, reason])
        print(f"[{role}{rung}] {model}: marked down 10m ({reason}), next rung")

    # Global floor: try last_resort in order, skipping anything the role chain
    # already tried (and anything already cooling down).
    chains = load_chains()
    last_resort = chains.get("last_resort") or []
    if isinstance(last_resort, str):
        last_resort = [last_resort]
    if last_resort and not dry and not cmd:
        print("no command given", file=sys.stderr)
        return 2
    for i, model in enumerate(last_resort, start=1):
        if model in rungs:
            print(f"[last_resort-{i}] {model}: already in the role chain, skipping")
            continue
        chk = subprocess.run(HEALTH + ["check", model], capture_output=True)
        if chk.returncode != 0:
            subprocess.run(LOG + [role, f"last-{i}", model, "down", "0"])
            print(f"[last_resort-{i}] {model}: in cooldown, skipping")
            continue
        for attempt in (1, 2):
            if dry:
                ok, ms, tail = False, 12.0, "HTTP 429 rate-limited"
            else:
                ok, ms, tail = run_cmd(cmd, 300)
            subprocess.run(LOG + [role, f"last-{i}", model,
                                  "ok" if ok else "retry", str(int(ms))])
            print(f"[last_resort-{i}] {model}: attempt {attempt} -> "
                  f"{'ok' if ok else 'FAIL'} ({tail[:60]!r})")
            if ok:
                return 0
            if attempt == 1:
                time.sleep(2)
        reason = "429" if dry else classify(tail)
        subprocess.run(HEALTH + ["down", model, reason])
        print(f"[last_resort-{i}] {model}: marked down 10m ({reason}), next")

    print(f"[{role}] all rungs + last_resort failed -> orchestrator takeover / HANDOFF",
          file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
