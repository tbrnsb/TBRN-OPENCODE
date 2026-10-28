# Route Validation

> Historical: this document describes the pre-TBRN GO worker and its route
> probing tooling. It is kept for reference only and is not the current
> routing layer. The current reference is `docs/SYSTEM-BRIEF.md`.

A configured route is not a working route.

Provider model IDs get retired. Accounts lose entitlement. Balances run out.
Provider catalogs advertise models the API then rejects. None of that shows up
in a JSON file, and all of it silently breaks the orchestrator at runtime.

`validate-routes` exists to make those distinctions measurable.

## The stage ladder

Every route is walked up a ladder and reports **how far it got**, not merely
that it broke:

| Stage | Question | Pass signal |
|---|---|---|
| 0 `catalog` | Is the model advertised by the provider? | appears in `opencode models <provider>` |
| 1 `reachable` | Does the endpoint answer at all? | any structured response |
| 2 `basic` | Does the model produce a usable completion? | expected sentinel echoed |
| 3 `tool-call` | Can it emit a tool call and use the result? | `TOOL_OK_<line>` after a real `grep` |

Stage 3 is the one that matters for this system. A model that answers prose but
cannot call `grep`/`read` is useless as a specialist regardless of how well it
scores on benchmarks.

## Outcomes

| Outcome | Meaning | Typical remedy |
|---|---|---|
| `ok-tool` | reached stage 3 | none |
| `ok-basic` | reached stage 2, tool stage failed or skipped | check tool support |
| `model-unavailable` | retired, nonexistent, unknown to the API | change the model ID |
| `auth-failure` | credential, billing, quota, or entitlement refused | fix key, top up, or change plan |
| `provider-error` | 429/5xx/capacity/saturation | retry later or route elsewhere |
| `timeout` | no answer inside the deadline | may be slow rather than down; retest |
| `network-error` | DNS, connection reset, refused | local network or DNS |
| `bad-response` | answered but no expected marker | model quality or prompt issue |
| `unknown` | no recognised signal | inspect raw output |

`model-unavailable` and `auth-failure` are deliberately distinct. Both refuse
routing, but the remedy differs: a retired model needs a *different model*, an
entitlement failure needs a *different credential or plan*.

## Catalog presence is a claim, not proof

Observed 2026-09-29 on this machine:

```
$ opencode models groq | grep qwen3.6
groq/qwen/qwen3.6-27b          <- catalog advertises it

$ probe-model groq/qwen/qwen3.6-27b
  catalog=listed  stage=1  model-unavailable
  The model `qwen/qwen3.6-27b` does not exist on this endpoint
```

Also: `google/gemini-2.5-flash` is `catalog=listed` and still returns
`model-unavailable`. A clean catalog proves nothing about availability.

## Usage

Always start with a dry run. A full sweep costs real requests.

```bash
# see what would be probed, and how many real failure domains exist
./scripts/validate-routes --dry-run

# probe one model
./scripts/probe-model mistral/codestral-latest

# probe one model, JSON, no tool stage (cheaper)
./scripts/probe-model opencode/space-bunny-free --json --skip-tool

# full sweep against a non-default config
./scripts/validate-routes --routes ./config/provider-routes.example.json
```

`validate-routes` exit codes: `0` all routes reached `ok-basic` or better,
`1` at least one failed, `2` usage or config problem.

## Failure domains

The report collapses routes by endpoint host before probing, because N routes
is not N independent fallbacks:

```
=== failure domains ===
  2 api.integrate.nvidia.com
  4 generativelanguage.googleapis.com
  2 api.groq.com
```

A second API key on the same host is a *quota* route, not a *redundancy*
route. It helps against per-key rate limits and does nothing about an upstream
outage. Reading the six-entry chain as six fallbacks is how a system ends up
with one real domain and no escape.

## Safety properties

- **Never reads credentials.** Authentication happens inside the `opencode`
  process. There is no key material in this code path to leak.
- **Read-only on configuration.** Reads route definitions, calls `opencode run`,
  writes nothing to `~/.config/opencode/`.
- **Redaction backstop.** `lib/classify.sh:redact` scrubs credential-shaped
  strings from any diagnostic output.
- **Disposable fixtures.** Probes run in a temp dir against a generated file.
  No repository content is read or sent.

## What this does not prove

A route that reaches stage 3 is *reachable and can call tools*. It is not
automatically *suitable* for a given specialist. Judging whether
`codestral-latest` is right for `code-reviewer` is a separate decision, and one
a benchmark table settles faster than a reachability probe.

## Extending it

Stage definitions and outcome patterns live in `lib/classify.sh` as pure
functions, with no network access. To support a new provider, add its error
strings to `classify_outcome` and add fixtures to `tests/test-classify.sh`
before adding it to the router.
