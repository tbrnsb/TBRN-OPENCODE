# Failure Recovery

Every role in TBRN-OPENCODE-V2 has a failover ladder. A failed dispatch never
ends the task; it walks to the next model, then to a global fallback chain,
then to a mastermind takeover, and finally to an explicit handoff.

## What counts as a failure

429 rate limit, timeout, empty reply, 5xx server error, garbage output
(including context overflow), or a reply that ignores the brief.

## The rung ladder

Each role's models are listed in `chains.json` in rung order:

| Role chain length | Agent ids, in order |
| :--- | :--- |
| 4 | `<role>`, `<role>-2`, `<role>-3`, `<role>-last` |
| 3 | `<role>`, `<role>-2`, `<role>-last` |

`security-reviewer`, `final-verifier`, `ui-reviewer`, and `general` are
3-rung roles and have no `-3` file. The ladder applies to direct `@` mentions
too: a failed `@coder` run fails over to `coder-2`, `coder-3`, `coder-last`.

## Retry rules and cooldown

1. Retry the same rung once.
2. Still failing: mark the model down and dispatch the next rung.

```bash
python3 ~/.config/opencode/lib/health.py down <model> <reason>
python3 ~/.config/opencode/lib/health.py check <model>
python3 ~/.config/opencode/lib/log-rung.py <role> <rung> <model> <outcome> <ms>
```

Reasons: `429`, `timeout`, `5xx`, `empty`, `garbage`. A model marked down is
cool for about 10 minutes in `lib/model-health.json`. Cooldowns are global:
every role skips a cooled model, not just the role that hit the failure.
Cooldown expires by itself; nothing marks a model back up.

## last_resort

When a role's own rungs are exhausted, walk the global `last_resort` chain in
`chains.json`, in order:

1. `opencode/deepseek-v4.1-flash-free`
2. `mistral/codestral-latest`
3. `nvidia_secondary/nvidia/nemotron-3.5-lightning-30b-a3b`

Skip any model the role already tried and any model in cooldown. Output from
the `last_resort` chain is accepted only after a tester or critic verifies it.

## Takeover and HANDOFF

- All rungs and `last_resort` dead: the mastermind takes the role's work over
  itself (takeover mode) and records the takeover model in `author_models`.
- The mastermind's own chain failing is the end of the line: it writes
  `HANDOFF.md` — task, decisions, files changed, next step — and pauses for
  the user. It also hands off proactively at roughly 70% context.
- `scripts/ladder.py` exits 3 when both a role chain and `last_resort` are
  dead, and can write the HANDOFF file.

## Caps

- Max 3 debug loops per issue. After the third, stop and ask.
- Max 3 critic rounds. Pass is 9/10.
- Stop and ask the user whenever a requirement is ambiguous.

## Critic family rule

A model must not grade work it could have authored. The critic must differ BY
FAMILY from every model in `author_models` in `.orchestrator/STATE.md` — the
coder, the debugger, and any takeover model. `code-reviewer`,
`security-reviewer`, and `final-verifier` follow the same separation rule.

The family map lives in `chains.json` under `families`. Only the Nemotron
group is merged: Nemotron NIM and Nemotron Zen are one family. Every other
model is its own family. `scripts/ladder.py --pick-critic <STATE.md>` prints
the first critic rung whose family differs from all author models, and refuses
to pick when `author_models` is missing or blank. The mastermind applies this
rule at dispatch time; the script is an audit aid, not an automatic gate.

## Logging failures

- Every dispatch attempt is logged by `lib/log-rung.py` into
  `lib/rung-log.jsonl`: role, rung, model, outcome, duration.
- A chain that ultimately fails is recorded with `lib/log-agent-issue.py`:

```bash
python3 ~/.config/opencode/lib/log-agent-issue.py \
  --role <role> --session-id <id> \
  --rungs-tried "1:<model>:<outcome>,2:<model>:<outcome>" \
  --final-outcome <result> --notes "<what happened>"
```

This writes `agents-issue/ISSUES.md` in the `sulav-global-sessions` archive
repo and pushes it.

## Testing the ladder without burning models

```bash
python3 ~/.config/opencode/scripts/ladder.py --dry-run general
python3 ~/.config/opencode/scripts/ladder.py --dry-run coder
python3 ~/.config/opencode/scripts/ladder.py --pick-critic ~/.orchestrator/STATE.md
```

Set `MODEL_HEALTH_FILE` and `RUNG_LOG_FILE` to temp paths so a test cannot
mark real models down or pollute the dispatch log. The offline test suite
(`python3 -m unittest discover -s tests -t .`) does exactly this; it makes no
network calls and no model calls.

## Recovering a half-finished task

A provider can fail after a role edited some files but before it reported. On
re-dispatch, inspect `.orchestrator/STATE.md` and the working diff first; do
not redo work already committed; keep retries bounded; never force-push. The
`author_models` list in STATE.md stays authoritative for critic selection
throughout.
