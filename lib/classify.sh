#!/usr/bin/env bash
# lib/classify.sh — probe result classification.
#
# Pure functions. No network, no credentials, no side effects.
# Sourced by scripts/probe-model and scripts/validate-routes; exercised
# directly by tests/test-classify.sh with fixture files.
#
# The model is a STAGE LADDER. A probe stops at the first stage that fails,
# so the reported outcome says *how far* a model got, not just that it broke.
#
#   stage 0  catalog      model present in provider catalog
#   stage 1  reachable    provider answered at all
#   stage 2  basic        model produced a usable completion
#   stage 3  tool-call    model emitted a tool call and used the result
#
# Outcomes:
#   ok-tool        reached stage 3
#   ok-basic       reached stage 2
#   timeout        no answer within the deadline
#   auth-failure   credentials/billing rejected
#   model-unavailable  model retired, nonexistent, or not entitled
#   provider-error upstream 4xx/5xx that is not auth or availability
#   network-error  transport failure
#   bad-response   answered, but the expected marker was absent
#   unknown        no recognised signal

# classify_outcome <exit_code> <output_file> <expected_marker>
#
# Prints one outcome token. Never prints provider output (may echo credentials).
classify_outcome() {
  local rc="$1" file="$2" marker="${3:-}"

  # timeout wins over everything: a timeout is a timeout even if partial
  # output happens to contain provider-ish wording.
  if [[ "$rc" == "124" ]] || grep -Eqi 'timed out|operation timed out|ETIMEDOUT|TimeoutError|deadline exceeded' "$file"; then
    echo "timeout"; return 0
  fi

  # transport failures before HTTP-level signals
  if grep -Eqi 'ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|socket hang up|fetch failed|network error|getaddrinfo' "$file"; then
    echo "network-error"; return 0
  fi

  # billing / entitlement / credential rejection
  # "not available in your account" is an entitlement problem, not a retired
  # model: the model is fine, this credential is not allowed to use it. Both
  # need "do not route here", but the remedy differs, so keep them distinct.
  if grep -Eqi 'unauthorized|invalid[ _-]?api[ _-]?key|authentication (failed|error)|permission denied|insufficient balance|no resource package|quota exceeded|billing|not available in your' "$file"; then
    echo "auth-failure"; return 0
  fi

  # model retired / nonexistent for everyone
  if grep -Eqi 'no longer available|does not exist|model[ _-]?not[ _-]?found|not[ _]found|deprecat|not_found|retired|unknown model' "$file"; then
    echo "model-unavailable"; return 0
  fi

  # generic 403/forbidden that is not model-specific reads as authorization
  if grep -Eqi '(^|[^0-9])403([^0-9]|$)|forbidden' "$file"; then
    echo "auth-failure"; return 0
  fi

  # upstream throttling / capacity / generic server error.
  # ResourceExhausted and "worker local total request limit" are the strings
  # provider-side saturation actually returns (observed on poolside 2026-09-29);
  # without them the real production failure classified as "unknown".
  if grep -Eqi 'rate[ _-]?limit|too many requests|quota|capacity|overloaded|internal[ _-]server[ _-]error|bad[ _-]gateway|service unavailable|ResourceExhausted|workers are busy|request limit reached|(^|[^0-9])5[0-9][0-9]([^0-9]|$)|429' "$file"; then
    echo "provider-error"; return 0
  fi

  # answered but did not produce the expected marker
  if [[ -n "$marker" ]]; then
    if grep -qF -- "$marker" "$file"; then
      # marker present: distinguish tool stage from basic by whether a tool
      # was actually exercised. Callers pass a tool-sentinel in stage 3.
      if [[ "${PROBE_STAGE:-2}" == "3" ]] && grep -Eqi 'grep|tool' "$file"; then
        echo "ok-tool"; return 0
      fi
      echo "ok-basic"; return 0
    fi
    echo "bad-response"; return 0
  fi

  # no marker requested: fall back to exit status
  if [[ "$rc" == "0" ]]; then
    echo "ok-basic"; return 0
  fi
  echo "unknown"
}

# catalog_lookup <provider> <model_id>
#
# Is the model present in the provider's advertised catalog?
# Prints "listed" or "unlisted". Catalog presence is a claim, not proof:
# the stage ladder exists precisely because catalogs drift from reality.
catalog_lookup() {
  local provider="$1" model_id="$2" catalog
  catalog="$(opencode models "$provider" 2>/dev/null)"
  if printf '%s\n' "$catalog" | grep -Fxq "${provider}/${model_id}"; then
    echo "listed"
  else
    echo "unlisted"
  fi
}

# redact <text>
#
# Defence in depth for anything that gets written to a report. Probe output is
# captured to files and echoed, so scrub anything credential-shaped on the way
# out. This is a backstop, not a substitute for not printing secrets.
redact() {
  sed -E \
    -e 's/(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]*/\1[REDACTED]/g' \
    -e 's/([Bb]earer[[:space:]]+)[A-Za-z0-9._-]+/\1[REDACTED]/g' \
    -e 's/("(api_?key|token|secret|authorization)"[[:space:]]*:[[:space:]]*")[^"]*/\1[REDACTED]/gI' \
    -e 's/(x-api-key[[:space:]]*:[[:space:]]*)[A-Za-z0-9._-]+/\1[REDACTED]/gI'
}

# classify_failure_type <output_file>
#
# The failure TAXONOMY (as opposed to the pass/fail outcome). Emits one of:
#   rate_limit | timeout | context_limit | provider_error | auth-failure
#   model-unavailable | invalid_request | worker-crash | unknown
#
# This exists so the worker and the validator stop speaking two dialects.
# A worker's inline classifier previously emitted auth_failure while this
# module emitted auth-failure, so the same event was recorded two ways and
# nothing could match across them. There is now one vocabulary, hyphenated,
# and every consumer reads it from here.
classify_failure_type() {
  local file="$1"
  if [[ ! -r "$file" ]]; then echo "unknown"; return 0; fi
  if grep -Eqi '429|rate.?limit|too many requests|quota|capacity|ResourceExhausted|workers are busy|request limit reached' "$file"; then
    echo "rate_limit"
  elif grep -Eqi 'timeout|timed out|ETIMEDOUT|ECONNRESET|ECONNREFUSED' "$file"; then
    echo "timeout"
  elif grep -Eqi 'context.?length|context.?limit|maximum.*tokens' "$file"; then
    echo "context_limit"
  elif grep -Eqi 'unauthorized|forbidden|invalid[ _-]?api[ _-]?key|not available in your|insufficient balance|no resource package|permission denied' "$file"; then
    echo "auth-failure"
  elif grep -Eqi 'no longer available|does not exist|model[ _-]?not[ _-]?found|deprecat|retired' "$file"; then
    echo "model-unavailable"
  elif grep -Eqi 'syntax error|invalid request|malformed|bad request' "$file"; then
    echo "invalid_request"
  elif grep -Eqi '5[0-9][0-9]|internal[ _-]server[ _-]error|bad[ _-]gateway|service unavailable|overloaded' "$file"; then
    echo "provider_error"
  else
    echo "unknown"
  fi
}
