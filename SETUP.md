# TBRN-OPENCODE Setup Guide

## Quick Install

```bash
git clone https://github.com/tbrnsb/TBRN-OPENCODE.git
cd TBRN-OPENCODE
./install/install.sh --dry-run    # preview
./install/install.sh              # install to ~/.config/opencode
```

This installs:
- `config/live/opencode.json` → `~/.config/opencode/opencode.json`
- `config/live/chains.json` → `~/.config/opencode/chains.json`
- `config/live/AGENTS.md` → `~/.config/opencode/AGENTS.md`
- `config/live/agents/*.md` (56 agents) → `~/.config/opencode/agents/`
- Worker binaries → `~/.local/bin/`

## Setup Wizard (Menu-Driven)

```bash
python3 scripts/setup-wizard.py          # interactive menu
python3 scripts/setup-wizard.py --non-interactive --validate         # validate only
python3 scripts/setup-wizard.py --non-interactive --refresh-agents   # regenerate agents
python3 scripts/setup-wizard.py --non-interactive --set-archive /path/to/archive  # set archive dir
```

The interactive wizard provides a menu to:
- **Set Session Archive Directory** — defaults to `~/.local/share/session-archive/global-sessions/`
- **Configure API Provider Keys** — add/edit NVIDIA, OpenRouter, Groq, Anthropic routes
- **Validate Current Configuration** — checks V2 config, provider routes, chains
- **Refresh Generated Agents** — regenerates 56 agents from `chains.json`

## Model Configuration (Required)

**You must add your own API credentials.** The repo ships a sanitized example at `config/provider-routes.example.json`. Copy it and fill in your keys:

```bash
cp ~/.config/opencode/provider-routes.example.json ~/.config/opencode/provider-routes.json
# edit provider-routes.json with your credentials
```

### Required Credentials

| Provider | Models Used | Get Key At |
|----------|-------------|------------|
| NVIDIA | `nemotron-3-ultra-550b-a55b`, `nemotron-3.5-lightning-30b-a3b` | https://build.nvidia.com |
| OpenRouter | `google/gemini-2.5-flash`, `openai/gpt-oss-120b` | https://openrouter.ai/keys |
| Groq | `llama-3.3-70b-versatile` | https://console.groq.com/keys |

### Provider Routes Schema

Each route in `provider-routes.json` needs:
```json
{
  "credential": "your-api-key-here",
  "model": "provider/model-name",
  "family": "model-family-for-critic-selection",
  "data_policy": "private-safe | training-optin | trial-logged | training-explicit"
}
```

- `credential` — your actual API key (never commit this)
- `model` — must match the model IDs in `chains.json` rungs
- `family` — used by the critic family-separation rule (Nemotron NIM + Zen = same family)
- `data_policy` — safety ceiling; `private-safe` required for private code

## Verify Installation

```bash
opencode debug agents                    # 61 entries (56 TBRN + 5 builtins), no 'explore'
opencode plugin list                     # session-archive, superpowers
python3 ~/.config/opencode/scripts/gen-fallbacks.py --check
# → "in sync: 56 agents match chains.json"
```

## Where Colors Appear

Agent `color: "#RRGGBB"` (in frontmatter) is rendered in:
- **Tab agent picker** — press Tab in OpenCode TUI to switch primary agents (M.A.X.I.M. rungs)
- **Task tool list** — when the mastermind dispatches subagents, the list shows colors

**Not in `@` menu** — OpenCode v2.2.0 shows only agent IDs in `@` autocomplete (no descriptions, no colors). This is a core limitation.

## Notifications (cc-alerts)

Phone/desktop notifications via ntfy.sh:

```bash
# 1. Create a topic at https://ntfy.sh (e.g., "my-opencode-alerts")
# 2. Set in shell rc or ~/.config/opencode/cc-alerts.env:
export NTFY_TOPIC="my-opencode-alerts"
export NTFY_SERVER="https://ntfy.sh"  # or self-hosted
# 3. The archiver agent runs cc-alerts.py on session end; driver.py runs it on handoff
```

Files: `lib/cc-alerts.py` (sender), `lib/driver.py` (hooks into mastermind handoff).

## Browser / Web Access

**Not enabled.** The built-in browser skill is explicitly denied in all agent permissions (`"browser": "*": "deny"`). If you need web search, use the `websearch` tool (allowed) or configure a custom skill.

## Archiver / GitHub

- **Archiver agent** (`L.O.G.`) handles conventional commits, secret scanning, session logs, PR creation
- **session-archive plugin** (`lib/session-archive.py`) auto-archives every session to a private git repo at `~/.local/share/session-archive/global-sessions/`
- **GitHub CLI** (`gh`) used for PR creation — must be authenticated (`gh auth status`)

No GitHub Actions/workflows in this repo; the archiver runs at commit time.

## Project Structure

```
TBRN-OPENCODE/
├── config/
│   ├── opencode.json              # V2 config (plugins, agents.explore.disabled)
│   ├── chains.json                # 15 roles × 3-4 model rungs (source of truth)
│   ├── AGENTS.md                  # Global operating rules
│   ├── orchestrator-policy.json   # Data-policy ceiling
│   ├── provider-routes.example.json  # Sanitized template — copy + fill keys
│   └── live/                      # Mirror used by installer
│       ├── agents/                # 56 generated agents (source of truth)
│       ├── opencode.json
│       ├── chains.json
│       ├── AGENTS.md
│       ├── orchestrator-policy.json
│       └── provider-routes.json   # Your filled-in copy goes here
├── scripts/
│   ├── gen-fallbacks.py           # Generates agents from chains.json
│   ├── ladder.py                  # Dry-run ladder + critic picker
│   ├── validate-*                 # Route/pin/role validation
│   ├── opencode-worker            # Legacy GO worker (not current router)
│   ├── probe-model                # Model probe utility
│   └── setup-wizard.py            # Interactive & non-interactive setup
├── lib/
│   ├── health.py                  # Model health registry (cooldown)
│   ├── log-rung.py                # Dispatch telemetry
│   ├── log-agent-issue.py         # Failed chain archive
│   ├── secret-scan.sh             # Blocks commits with secrets
│   ├── policy.py                  # Data-policy ceiling enforcement
│   ├── session-archive.py         # Session archival to private repo
│   ├── cc-alerts.py               # ntfy.sh notifications
│   ├── driver.py                  # Mastermind handoff hook
│   ├── classify.sh                # Legacy worker classification
│   └── stateio.py                 # Legacy worker state
├── skills/tbrn/                   # @tbrn skill + references
├── tests/test_ladder.py           # 35 offline tests
├── docs/                          # SYSTEM-BRIEF, architecture, etc.
└── install/install.sh             # Non-destructive installer
```

## Regenerating Agents

After editing `config/live/chains.json`:

```bash
cd ~/.config/opencode
python3 scripts/gen-fallbacks.py --out config/live/agents --chains config/live/chains.json
python3 scripts/gen-fallbacks.py --check --out config/live/agents --chains config/live/chains.json
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `opencode debug agents` shows `explore` | Ensure `opencode.json` has `"agents": {"explore": {"disabled": true}}` |
| Colors not showing | Terminal must support 24-bit color; v2.2.0 requires hex `#RRGGBB` |
| Critic refuses review | Check `author_models` in `.orchestrator/STATE.md` — critic must differ by family |
| Model not found | Verify model ID in `chains.json` matches provider-routes exactly |
| Permission denied dispatching | Mastermind whitelist only allows the 14 TBRN roles + `*-2/3/last` globs |

## License

MIT — see `LICENSE`.
