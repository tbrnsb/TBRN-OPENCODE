# Known Limitations

## Current limitations (TBRN-OPENCODE-V2, OpenCode v2.2.0)

1. OpenCode 2.2.0's @ autocomplete shows no description for agents, only
   skills. The "TBRN's <Role>" brand therefore appears in the Tab picker and
   the subagent list, not in `@`.
2. `opencode-validate-pins` reports 56/56 agents "unlabelled" under the default
   public pin ceiling, while exiting 0.
3. The GO worker (`go/`, `scripts/opencode-worker`, `lib/classify.sh`,
   `lib/policy.py`) is legacy and pending removal. It is not the current
   routing layer.
4. The 21 shell tests in `tests/` test that legacy worker, not the current
   rung ladder.

## Pre-TBRN history

Everything below describes the pre-TBRN GO worker system and is kept
unchanged for reference.

## Critical: OpenCode 1.18.29 Specialist/Subagent Limitation

### Problem
OpenCode 1.18.29 does not support independent specialist agent invocation as expected.

### Observed Behavior
```bash
$ opencode run --agent coder "Implement JWT authentication"
agent 'coder' is a subagent, not a primary agent. Falling back to default agent
```

### Impact
| Expected | Actual |
|----------|--------|
| Specialist model used (e.g., `mistral/codestral-latest`) | Falls back to primary agent model (`nvidia/poolside/laguna-xs-2.1`) |
| Specialist prompt applied | Primary agent prompt used |
| Independent specialist context | Shared with primary agent |

### Workaround in This System
1. **Provider routing still works**: `--model` flag is honored, so `opencode-worker` routes to correct provider
2. **Orchestrator embeds specialist instructions**: Task prompts include specialist-specific guidance
3. **Honest detection**: `test-specialist-invocation.sh` reports actual behavior

### Status
**NOT SOLVED** - This is an OpenCode core limitation. Track upstream for fixes.

---

## Provider/Model Drift

### Observation
OpenCode's provider catalog/config can disagree with actual provider APIs.

### Documented Examples

| Source | Model ID | Actual Provider API |
|--------|----------|---------------------|
| OpenCode config/catalog | `groq/qwen/qwen3.6-27b` | `qwen/qwen3.8-27b` |
| OpenCode config/catalog | `google/gemini-3.8-flash` | `gemini-2.5-flash`, `gemini-flash-latest`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite` |

### Root Cause
- Provider catalogs updated independently of provider APIs
- Model versioning differs between catalog and API
- Regional/rollout differences in model availability

### Mitigation in This System
1. **Configurable Model IDs**: All model identifiers in `provider-routes.json` (not hard-coded)
2. **Observed Drift Documentation**: Recorded here for awareness
3. **No Permanent Truth Claims**: Treat as observations, not guarantees
4. **Easy Updates**: Modify JSON config, no code changes needed

### Recommendation
When configuring routes:
- Test actual model availability before committing
- Use generic identifiers where possible
- Monitor provider changelogs for model updates

---

## Orchestrator Self-Recovery (Requirement #20)

### Status: SIMULATED ONLY - NOT LIVE VERIFIED

### What's Implemented
- State persistence on worker failure (`.opencode/orchestration-state.md`)
- Route rotation logic in `opencode-worker` script
- Orchestrator preference chain in `agent_preferences.orchestrator`
- Circuit breaker with exponential backoff
- Request budget tracking with configurable limits
- Resume from interrupted state with handoff preservation

### What's NOT Verified
- Actual `ResourceExhausted` recovery with live provider limits
- Orchestrator instance handoff with state preservation
- Multi-hop orchestrator fallback (A→B→C→D)
- Continuation from saved state without work repetition

### Simulation Test
`tests/test-orchestrator-recovery.sh` verifies:
1. State persistence on ResourceExhausted
2. Route rotation to next orchestrator preference
3. Handoff preservation through recovery
4. Objective and completed work preservation
5. Independent fallback chain from specialist routes

### Honest Assessment
**Do NOT claim this works in production.** Document as SIMULATED.

---

## State Persistence Scope

### Current Implementation
- Worker-level state: `.opencode/orchestration-state.md` (per-task)
- Updated by `opencode-worker` script
- Contains: objective, status, attempt, route, detail, handoff
- Circuit breaker: `.opencode/circuit-breaker.json`
- Attempt tracking: `.opencode/attempt-count.json`

### Limitations
- No cross-session persistence (state lost if directory cleaned)
- No distributed state (single machine only)
- No structured handoff schema enforcement (markdown, human-readable)
- Orchestrator state not separately persisted from worker state

---

## Validation Gaps

### Not Automatically Verified
| Area | Status |
|------|--------|
| End-to-end multi-agent task completion | MANUAL ONLY |
| Orchestrator self-recovery under real load | SIMULATED |
| Specialist independent invocation | BROKEN (OpenCode limitation) |
| Provider fallback with real quota exhaustion | NOT TESTED (cost/risk) |
| Cross-repository state sharing | NOT SUPPORTED |
| Concurrent task orchestration | NOT SUPPORTED |

### Verified (Structural)
| Test | Status |
|------|--------|
| JSON config syntax | ✅ `jq` validation |
| Route structure completeness | ✅ `test-provider-registration.sh` |
| Worker script logic (mock) | ✅ `test-worker-success.sh` |
| Fallback rotation logic | ✅ `test-provider-fallback.sh` |
| Specialist invocation detection | ✅ `test-specialist-invocation.sh` |
| State file format | ✅ `test-state-preservation.sh` |
| Circuit breaker / cooldown | ✅ `test-circuit-breaker.sh` |
| Request budget / retry exhaustion | ✅ `test-retry-exhaustion.sh` |
| Resume / recovery from state | ✅ `test-resume-recovery.sh` |
| Handoff preservation & deduplication | ✅ `test-handoff-dedup.sh` |
| Malformed worker handling | ✅ `test-malformed-worker.sh` |
| Orchestrator self-recovery (simulated) | ✅ `test-orchestrator-recovery.sh` |

---

## Security Considerations

### Credential Handling
- **No credentials in repo**: All configs use `{env:VAR}` placeholders
- **Live auth.json excluded**: `.gitignore` prevents accidental commits
- **Installer creates backups**: Before modifying live config

### Data Exposure Risk
- State files may contain file paths, code snippets, error messages
- `.opencode/` directory should be in project `.gitignore`
- No secrets written to state (workers don't have access)

---

## Platform Dependencies

### Required
- `bash` (GNU bash 4+)
- `python3` (for route parsing in worker script)
- `timeout` (GNU coreutils)
- `opencode` CLI (v1.18.29+)
- `jq` (for config validation in tests)

### Tested On
- Linux (Arch-based, Omarchy)
- Not tested on macOS, Windows, BSD

### Path Assumptions
- Config: `${HOME}/.config/opencode/`
- Binary: `${HOME}/.local/bin/`
- State: `${PWD}/.opencode/`

---

## Version Compatibility

### OpenCode Version
- Developed against: **1.18.29**
- May work with: 1.18.x
- Breaking changes likely in: 1.19+, 2.0+

### Provider SDK
- Uses `@ai-sdk/openai-compatible` via OpenCode
- Provider API compatibility depends on OpenCode's provider implementation

---

## Summary: What Works vs. What Doesn't

| Feature | Status | Evidence |
|---------|--------|----------|
| Provider fallback (Groq→Google→OpenRouter) | ✅ VERIFIED | Live test: ROUTE_TEST_OK |
| Mistral/Codestral coding route | ✅ VERIFIED | Live test: coding task completed |
| Worker state persistence | ✅ VERIFIED | Script implements state machine |
| Route configuration structure | ✅ VERIFIED | Structural tests pass |
| Circuit breaker / cooldown | ✅ VERIFIED | `test-circuit-breaker.sh` passes |
| Request budget / retry exhaustion | ✅ VERIFIED | `test-retry-exhaustion.sh` passes |
| Resume from interrupted state | ✅ VERIFIED | `test-resume-recovery.sh` passes |
| Handoff preservation | ✅ VERIFIED | `test-handoff-dedup.sh` passes |
| Malformed worker handling | ✅ VERIFIED | `test-malformed-worker.sh` passes |
| Specialist independent invocation | ❌ BROKEN | OpenCode 1.18.29 limitation |
| Orchestrator self-recovery | 🟡 SIMULATED | Not live-tested |
| End-to-end multi-agent workflow | 🟡 MANUAL | Not automated |
| Cross-provider model drift handling | ✅ DESIGNED | Configurable, documented |

---

## Honest Reporting Requirement

**This system MUST report honestly:**
- Never claim specialist invocation works
- Never claim orchestrator self-recovery is production-verified
- Never hide provider/model drift
- Always distinguish VERIFIED vs SIMULATED vs BROKEN
- Test file `test-specialist-invocation.sh` MUST detect and report fallback behavior