# Failure ladder

## Rungs

Each role has 3 or 4 rungs in `~/.config/opencode/chains.json`:

| Rungs | Agent ids |
|---|---|
| 4 | `<role>`, `<role>-2`, `<role>-3`, `<role>-last` |
| 3 | `<role>`, `<role>-2`, `<role>-last` |

A 3-rung role has no `-3` file. Do not invent one.

Every rung except the base is `hidden: true`. It does not appear in the @ menu, but it
is dispatched by id. The base rung is visible and always runs first.

## On failure

A failure is: 429, timeout, empty reply, 5xx, garbage output, context overflow, or a
reply that ignores the brief.

1. Retry the same rung once.
2. Still failing: mark the model down and dispatch the next rung.

```bash
python3 ~/.config/opencode/lib/health.py down <model> <reason>
python3 ~/.config/opencode/lib/health.py check <model>
python3 ~/.config/opencode/lib/log-rung.py <role> <rung> <model> <outcome> <ms>
```

Reasons: `429`, `timeout`, `5xx`, `empty`, `garbage`.

3. Rungs exhausted: walk `last_resort` in order. Skip a model already in this role's
   chain and any model in cooldown.

```
opencode/deepseek-v4.1-flash-free
mistral/codestral-latest
nvidia_secondary/nvidia/nemotron-3.5-lightning-30b-a3b
```

4. All dead: take over the role yourself. Write `HANDOFF.md` with task, decisions,
   files changed, next step. Say so in the report.

`ladder.py` returns exit 3 when the role chain and `last_resort` are both dead. In the
TUI the same rule applies by hand.

## Direct @ mentions

When the user mentions a role directly (`@coder`) and it fails, the same ladder
applies: `coder-2`, then `coder-3` (if present), then `coder-last`, then
`last_resort`. The hidden rungs are still dispatchable by id.

## Caps

- Max 3 debug loops per issue.
- Max 3 critic rounds. Pass is 9/10.
- Stop and ask when a requirement is ambiguous.

## Testing the ladder without burning models

```bash
python3 ~/.config/opencode/scripts/ladder.py --dry-run general
python3 ~/.config/opencode/scripts/ladder.py --dry-run coder
python3 ~/.config/opencode/scripts/ladder.py --pick-critic ~/.orchestrator/STATE.md
```

Set `MODEL_HEALTH_FILE` and `RUNG_LOG_FILE` to temp paths so a test cannot mark real
models down or pollute the dispatch log.
