#!/usr/bin/env python3
"""Append one rung-served record. No prompt text, no secrets, ever.

    log-rung.py <role> <rung> <model> <outcome> <ms>

outcome is one of: ok, retry, down, fail, verify. Appends one JSON line to
lib/rung-log.jsonl (or $RUNG_LOG_FILE). Used to see which rungs earn their place.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

LIB = pathlib.Path(__file__).resolve().parent
LOG = pathlib.Path(os.environ.get("RUNG_LOG_FILE", LIB / "rung-log.jsonl"))
OUTCOMES = {"ok", "retry", "down", "fail", "verify"}


def main() -> int:
    if len(sys.argv) != 6:
        print("usage: log-rung.py <role> <rung> <model> <outcome> <ms>", file=sys.stderr)
        return 2
    _, role, rung, model, outcome, ms = sys.argv
    if outcome not in OUTCOMES:
        print(f"outcome must be one of {sorted(OUTCOMES)}", file=sys.stderr)
        return 2
    try:
        ms_i = int(ms)
    except ValueError:
        print("ms must be an integer", file=sys.stderr)
        return 2
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), "role": role, "rung": rung,
                            "model": model, "outcome": outcome, "ms": ms_i}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
