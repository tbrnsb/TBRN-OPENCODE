#!/usr/bin/env python3
"""Model health registry: 10-minute cooldowns for failed models.

    health.py check <model>     # exit 0 = usable, 1 = in cooldown
    health.py down <model> <reason>   # mark down for 600s (reasons: 429, timeout,
                                      # empty, 5xx, garbage)
    health.py up <model>        # clear early (recovered)
    health.py list              # show all entries

Storage: lib/model-health.json next to this file, or $MODEL_HEALTH_FILE.
Checked before every dispatch; the orchestrator never retries a cooled model.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

COOLDOWN = 600
REASONS = {"429", "timeout", "empty", "5xx", "garbage"}

LIB = pathlib.Path(__file__).resolve().parent
STORE = pathlib.Path(os.environ.get("MODEL_HEALTH_FILE", LIB / "model-health.json"))


def load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=1))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: health.py {check|down|up|list} [model] [reason]", file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    data = load()
    now = time.time()

    if cmd == "check":
        entry = data.get(sys.argv[2], {})
        if entry.get("down_until", 0) > now:
            left = int(entry["down_until"] - now)
            print(f"DOWN {sys.argv[2]} ({entry.get('reason','?')}, {left}s left)")
            return 1
        print(f"UP {sys.argv[2]}")
        return 0

    if cmd == "down":
        model, reason = sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "?"
        if reason not in REASONS:
            print(f"reason must be one of {sorted(REASONS)}", file=sys.stderr)
            return 2
        data[model] = {"down_until": now + COOLDOWN, "reason": reason, "at": now}
        save(data)
        print(f"marked {model} down for {COOLDOWN}s ({reason})")
        return 0

    if cmd == "up":
        data.pop(sys.argv[2], None)
        save(data)
        print(f"cleared {sys.argv[2]}")
        return 0

    if cmd == "list":
        live = {m: e for m, e in data.items() if e.get("down_until", 0) > now}
        print(json.dumps(live, indent=1) if live else "no models in cooldown")
        return 0

    print(f"unknown command {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
