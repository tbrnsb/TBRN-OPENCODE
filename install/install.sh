#!/usr/bin/env bash
set -euo pipefail

# OpenCode Global Orchestrator Installer
# Non-destructive, with dry-run, backups, and explicit confirmation

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${HOME}/.config/opencode"
BIN_DIR="${HOME}/.local/bin"
STATE_DIR="${REPO_ROOT}/.orchestrator/worker-state"

DRY_RUN=false
FORCE=false
WITH_UNITS=false
CONFIG_DIR_OVERRIDE=""
BIN_DIR_OVERRIDE=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Install OpenCode Global Orchestrator configuration.

OPTIONS:
  --dry-run              Show what would change without making changes
  --force                Skip confirmation prompts (use with caution)
  --config-dir DIR       Target config directory (default: ~/.config/opencode)
  --bin-dir DIR          Target binary directory (default: ~/.local/bin)
  --with-units           Install + enable the cc-alerts systemd user units
  -h, --help             Show this help

EXAMPLES:
  $(basename "$0") --dry-run
  $(basename "$0") --config-dir /custom/path --bin-dir /custom/bin
  $(basename "$0") --with-units --force

SAFETY:
  - Never overwrites files without explicit confirmation (unless --force)
  - Creates timestamped backups of existing files
  - Validates JSON syntax before installing
  - Does not modify live OpenCode auth.json or credentials
EOF
}

log() { echo "[install] $*"; }
warn() { echo "[install] WARNING: $*" >&2; }
err() { echo "[install] ERROR: $*" >&2; exit 1; }

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --force) FORCE=true; shift ;;
    --config-dir) CONFIG_DIR_OVERRIDE="$2"; shift 2 ;;
    --bin-dir) BIN_DIR_OVERRIDE="$2"; shift 2 ;;
    --with-units) WITH_UNITS=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown option: $1" ;;
  esac
done

[[ -n "$CONFIG_DIR_OVERRIDE" ]] && CONFIG_DIR="$CONFIG_DIR_OVERRIDE"
[[ -n "$BIN_DIR_OVERRIDE" ]] && BIN_DIR="$BIN_DIR_OVERRIDE"
# Worker runtime libraries live next to the worker ($WORKER_ROOT/lib) because the
# worker resolves them relative to its own path.
LIB_DIR="${LIB_DIR_OVERRIDE:-${BIN_DIR}/lib}"

# Files to install
declare -A FILES=(
  ["${REPO_ROOT}/config/live/opencode.json"]="${CONFIG_DIR}/opencode.json"
  ["${REPO_ROOT}/config/provider-routes.example.json"]="${CONFIG_DIR}/provider-routes.example.json"
  ["${REPO_ROOT}/config/live/chains.json"]="${CONFIG_DIR}/chains.json"
  ["${REPO_ROOT}/config/live/AGENTS.md"]="${CONFIG_DIR}/AGENTS.md"
  ["${REPO_ROOT}/config/live/orchestrator-policy.json"]="${CONFIG_DIR}/orchestrator-policy.json"
  ["${REPO_ROOT}/config/orchestrator-system.md"]="${CONFIG_DIR}/orchestrator-system.md"
  ["${REPO_ROOT}/scripts/opencode-worker"]="${BIN_DIR}/opencode-worker"
  ["${REPO_ROOT}/scripts/probe-model"]="${BIN_DIR}/opencode-probe-model"
  ["${REPO_ROOT}/scripts/validate-routes"]="${BIN_DIR}/opencode-validate-routes"
  ["${REPO_ROOT}/lib/policy.py"]="${LIB_DIR}/policy.py"
  ["${REPO_ROOT}/lib/stateio.py"]="${LIB_DIR}/stateio.py"
  ["${REPO_ROOT}/lib/classify.sh"]="${LIB_DIR}/classify.sh"
  # Preview/notification/archive helpers spawned BY the OpenCode plugins:
  # both must live in BIN_DIR because the plugins spawn them by absolute path.
  ["${REPO_ROOT}/lib/session-archive.py"]="${BIN_DIR}/session-archive.py"
  ["${REPO_ROOT}/lib/agent-finished-notify.py"]="${BIN_DIR}/agent-finished-notify.py"
)

# Agent rung files from the synced live mirror (source: sync/tbrn-* branches).
declare -A AGENT_FILES=()
shopt -s nullglob
for src in "${REPO_ROOT}"/config/live/agents/*.md; do
  AGENT_FILES["${src}"]="${CONFIG_DIR}/agents/$(basename "$src")"
done
shopt -u nullglob

# V2 plugins (auto-loaded from the plugin directory at startup).
declare -A PLUGIN_FILES=()
shopt -s nullglob
for src in "${REPO_ROOT}"/plugins/*.js; do
  PLUGIN_FILES["${src}"]="${CONFIG_DIR}/plugins/$(basename "$src")"
done
shopt -u nullglob

# Agent runtime helpers. AGENTS.md and the systemd unit reference these by
# absolute path under ${CONFIG_DIR}/lib, so they must live there — not in the
# worker's ${LIB_DIR}. ladder.py resolves health.py/log-rung.py relative to
# its own parent, and gen-fallbacks.py defaults to ROOT/chains.json, so both
# work unmodified once they sit in ${CONFIG_DIR}/lib.
declare -A HELPER_FILES=(
  ["${REPO_ROOT}/lib/health.py"]="${CONFIG_DIR}/lib/health.py"
  ["${REPO_ROOT}/lib/log-rung.py"]="${CONFIG_DIR}/lib/log-rung.py"
  ["${REPO_ROOT}/lib/log-agent-issue.py"]="${CONFIG_DIR}/lib/log-agent-issue.py"
  ["${REPO_ROOT}/lib/secret-scan.sh"]="${CONFIG_DIR}/lib/secret-scan.sh"
  ["${REPO_ROOT}/lib/cc-alerts.py"]="${CONFIG_DIR}/lib/cc-alerts.py"
  ["${REPO_ROOT}/scripts/ladder.py"]="${CONFIG_DIR}/lib/ladder.py"
  ["${REPO_ROOT}/scripts/gen-fallbacks.py"]="${CONFIG_DIR}/lib/gen-fallbacks.py"
)

declare -A UNIT_FILES=()
if [[ "$WITH_UNITS" == "true" ]]; then
  UNIT_FILES["${REPO_ROOT}/install/units/cc-alerts.service"]="${HOME}/.config/systemd/user/cc-alerts.service"
  UNIT_FILES["${REPO_ROOT}/install/units/cc-alerts.timer"]="${HOME}/.config/systemd/user/cc-alerts.timer"
fi

# Check source files exist
for src in "${!FILES[@]}" "${!HELPER_FILES[@]}" "${!UNIT_FILES[@]}"; do
  [[ -f "$src" ]] || err "Source file not found: $src"
done

# Validate JSON syntax
validate_json() {
  local file="$1"
  if command -v jq >/dev/null 2>&1; then
    if ! jq empty "$file" 2>/dev/null; then
      err "Invalid JSON: $file"
    fi
  else
    warn "jq not installed, skipping JSON validation for $file"
  fi
}

# Create backup
backup_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    local backup="${file}.bak.$(date +%Y%m%d-%H%M%S)"
    if [[ "$DRY_RUN" == "true" ]]; then
      log "Would backup: $file -> $backup"
    else
      cp "$file" "$backup"
      log "Backed up: $file -> $backup"
    fi
  fi
}

# Show diff
show_diff() {
  local src="$1" dst="$2"
  if [[ -f "$dst" ]] && command -v diff >/dev/null 2>&1; then
    echo "--- $dst (current)"
    echo "+++ $src (new)"
    diff -u "$dst" "$src" || true
  else
    log "New file: $dst"
    cat "$src"
  fi
}

# Install file
install_file() {
  local src="$1" dst="$2"
  local dir="$(dirname "$dst")"
  
  if [[ "$DRY_RUN" == "true" ]]; then
    log "Would create directory: $dir"
    log "Would install: $src -> $dst"
    show_diff "$src" "$dst"
    return
  fi
  
  mkdir -p "$dir"
  backup_file "$dst"
  cp "$src" "$dst"
  log "Installed: $src -> $dst"
}

# Make executable
make_executable() {
  local file="$1"
  if [[ "$DRY_RUN" == "true" ]]; then
    log "Would chmod +x: $file"
  else
    chmod +x "$file"
    log "Made executable: $file"
  fi
}

# Write a small executable wrapper (used for the installed commands).
write_wrapper() {
  local dst="$1" body="$2"
  if [[ "$DRY_RUN" == "true" ]]; then
    log "Would write wrapper: $dst"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  backup_file "$dst"
  printf '#!/usr/bin/env bash\nset -euo pipefail\n%s\n' "$body" > "$dst"
  chmod +x "$dst"
  log "Wrote wrapper: $dst"
}

# Install only when absent: used for provider-routes.json so a reinstall can
# never replace a real credential file with the sanitized template.
install_once() {
  local src="$1" dst="$2"
  if [[ -e "$dst" ]]; then
    log "Keeping existing: $dst"
    return
  fi
  install_file "$src" "$dst"
}

# Main installation flow
main() {
  log "OpenCode Global Orchestrator Installer"
  log "Repo: $REPO_ROOT"
  log "Config dir: $CONFIG_DIR"
  log "Bin dir: $BIN_DIR"
  [[ "$DRY_RUN" == "true" ]] && log "DRY RUN MODE - No changes will be made"
  echo

  # Validate source JSON files
  log "Validating source JSON..."
  validate_json "${REPO_ROOT}/config/live/opencode.json"
  validate_json "${REPO_ROOT}/config/provider-routes.example.json"
  validate_json "${REPO_ROOT}/config/live/chains.json"
  validate_json "${REPO_ROOT}/config/live/orchestrator-policy.json"
  log "JSON validation passed"
  echo

  # Show what will change
  log "Files to install:"
  for src in "${!FILES[@]}"; do
    dst="${FILES[$src]}"
    echo "  $src -> $dst"
  done
  for src in "${!PLUGIN_FILES[@]}"; do
    dst="${PLUGIN_FILES[$src]}"
    echo "  $src -> $dst"
  done
  for src in "${!HELPER_FILES[@]}"; do
    dst="${HELPER_FILES[$src]}"
    echo "  $src -> $dst"
  done
  for src in "${!UNIT_FILES[@]}"; do
    dst="${UNIT_FILES[$src]}"
    echo "  $src -> $dst"
  done
  echo

  # Confirmation
  if [[ "$DRY_RUN" == "false" && "$FORCE" == "false" ]]; then
    read -rp "Proceed with installation? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { log "Aborted by user"; exit 0; }
    echo
  fi

  # Install config files
  log "Installing configuration files..."
  install_file "${REPO_ROOT}/config/live/opencode.json" "${CONFIG_DIR}/opencode.json"
  install_file "${REPO_ROOT}/config/provider-routes.example.json" "${CONFIG_DIR}/provider-routes.example.json"
  install_once "${REPO_ROOT}/config/provider-routes.example.json" "${CONFIG_DIR}/provider-routes.json"
  install_file "${REPO_ROOT}/config/live/chains.json" "${CONFIG_DIR}/chains.json"
  install_file "${REPO_ROOT}/config/live/AGENTS.md" "${CONFIG_DIR}/AGENTS.md"
  install_file "${REPO_ROOT}/config/live/orchestrator-policy.json" "${CONFIG_DIR}/orchestrator-policy.json"
  install_file "${REPO_ROOT}/config/orchestrator-system.md" "${CONFIG_DIR}/orchestrator-system.md"
  echo

  # Install agent rung files from the synced live mirror
  log "Installing agent rung files..."
  for src in "${!AGENT_FILES[@]}"; do
    install_file "$src" "${AGENT_FILES[$src]}"
  done
  echo

  # Install worker script
  log "Installing worker script..."
  install_file "${REPO_ROOT}/scripts/opencode-worker" "${BIN_DIR}/opencode-worker"
  make_executable "${BIN_DIR}/opencode-worker"
  install_file "${REPO_ROOT}/scripts/probe-model" "${BIN_DIR}/opencode-probe-model"
  make_executable "${BIN_DIR}/opencode-probe-model"
  install_file "${REPO_ROOT}/scripts/validate-routes" "${BIN_DIR}/opencode-validate-routes"
  make_executable "${BIN_DIR}/opencode-validate-routes"

  # Worker runtime libraries. The worker fails closed without policy.py, so a
  # partial install must be impossible: install all three or fail loudly.
  log "Installing worker runtime libraries..."
  install_file "${REPO_ROOT}/lib/policy.py" "${LIB_DIR}/policy.py"
  install_file "${REPO_ROOT}/lib/stateio.py" "${LIB_DIR}/stateio.py"
  install_file "${REPO_ROOT}/lib/classify.sh" "${LIB_DIR}/classify.sh"
  if [[ "$DRY_RUN" == "false" ]]; then
    # classify.sh is SOURCED by the worker, never executed directly, so 0644 is
    # intentional; it does not need the executable bit.
    chmod 0644 "${LIB_DIR}/policy.py" "${LIB_DIR}/stateio.py" "${LIB_DIR}/classify.sh"
  fi
  echo

  # Helper scripts spawned BY the V2 plugins. Both must be executable in
  # BIN_DIR: the plugins spawn them by absolute path (~/.local/bin/...).
  log "Installing plugin helper scripts..."
  install_file "${REPO_ROOT}/lib/session-archive.py" "${BIN_DIR}/session-archive.py"
  make_executable "${BIN_DIR}/session-archive.py"
  install_file "${REPO_ROOT}/lib/agent-finished-notify.py" "${BIN_DIR}/agent-finished-notify.py"
  make_executable "${BIN_DIR}/agent-finished-notify.py"
  echo

  # Agent runtime helpers referenced by AGENTS.md and the systemd unit.
  log "Installing agent runtime helpers..."
  for src in "${!HELPER_FILES[@]}"; do
    install_file "$src" "${HELPER_FILES[$src]}"
  done
  make_executable "${CONFIG_DIR}/lib/health.py"
  make_executable "${CONFIG_DIR}/lib/log-rung.py"
  make_executable "${CONFIG_DIR}/lib/log-agent-issue.py"
  make_executable "${CONFIG_DIR}/lib/secret-scan.sh"
  make_executable "${CONFIG_DIR}/lib/cc-alerts.py"
  echo

  log "Installing command wrappers..."
  write_wrapper "${BIN_DIR}/opencode-ladder" \
    "exec python3 \"${CONFIG_DIR}/lib/ladder.py\" \"\$@\""
  write_wrapper "${BIN_DIR}/opencode-gen-agents" \
    "exec python3 \"${CONFIG_DIR}/lib/gen-fallbacks.py\" --out \"${CONFIG_DIR}/agents\" --chains \"${CONFIG_DIR}/chains.json\" \"\$@\""
  echo

  if [[ "$WITH_UNITS" == "true" ]]; then
    log "Installing systemd user units..."
    for src in "${!UNIT_FILES[@]}"; do
      install_file "$src" "${UNIT_FILES[$src]}"
    done
    if [[ "$DRY_RUN" == "false" ]]; then
      systemctl --user daemon-reload || warn "systemctl --user daemon-reload failed; enable the timer manually"
      systemctl --user enable --now cc-alerts.timer || warn "could not enable cc-alerts.timer"
    fi
    echo
  fi

  # V2 plugins — auto-loaded from ${CONFIG_DIR}/plugins/ at OpenCode startup.
  log "Installing V2 plugins..."
  for src in "${!PLUGIN_FILES[@]}"; do
    install_file "$src" "${PLUGIN_FILES[$src]}"
  done
  echo

  # Verify installation
  log "Verifying installation..."
  for src in "${!FILES[@]}"; do
    dst="${FILES[$src]}"
    if [[ "$DRY_RUN" == "false" ]]; then
      [[ -f "$dst" ]] || err "Installation verification failed: $dst not found"
      if [[ "$dst" == *.json ]]; then
        validate_json "$dst"
      fi
    fi
  done
  for src in "${!PLUGIN_FILES[@]}" "${!HELPER_FILES[@]}" "${!UNIT_FILES[@]}"; do
    if [[ -n "${PLUGIN_FILES[$src]:-}" ]]; then dst="${PLUGIN_FILES[$src]}"
    elif [[ -n "${HELPER_FILES[$src]:-}" ]]; then dst="${HELPER_FILES[$src]}"
    else dst="${UNIT_FILES[$src]}"; fi
    if [[ "$DRY_RUN" == "false" ]]; then
      [[ -f "$dst" ]] || err "Installation verification failed: $dst not found"
    fi
  done

  if [[ "$DRY_RUN" == "false" ]]; then
    log "Checking generated agents match chains.json..."
    python3 "${CONFIG_DIR}/lib/gen-fallbacks.py" --check \
      --out "${CONFIG_DIR}/agents" --chains "${CONFIG_DIR}/chains.json" \
      || err "agents are out of sync with chains.json"
    log "Checking the failure ladder can resolve a role..."
    MODEL_HEALTH_FILE="$(mktemp)" RUNG_LOG_FILE="$(mktemp)" \
      python3 "${CONFIG_DIR}/lib/ladder.py" --dry-run coder >/dev/null \
      || err "ladder dry-run failed"
  fi
  log "Installation verification passed"
  echo

  # Post-install notes
  cat <<EOF
========================================
Installation Complete
========================================

NEXT STEPS:

1. Set up API credentials (environment variables):
   export NVIDIA_API_KEY="your-key"
   export OPENROUTER_API_KEY="your-key"
   export GOOGLE_API_KEY="your-key"
   export GROQ_API_KEY="your-key"
   export MISTRAL_API_KEY="your-key"
   export ZAI_API_KEY="your-key"

   Or use OpenCode's native credential management:
   opencode auth login <provider>

2. Test the installation:
   opencode-worker general "echo hello world"

3. Run structural tests:
   cd $REPO_ROOT && ./tests/test-provider-registration.sh

4. Read documentation:
   $REPO_ROOT/docs/architecture.md
   $REPO_ROOT/docs/known-limitations.md

IMPORTANT NOTES:
- This installer does NOT modify ~/.local/share/opencode/auth.json
- Credentials must be configured separately
- The live OpenCode installation was NOT modified
- Runtime state (.orchestrator/worker-state/) is created per-project
- Specialist independent invocation is limited in this OpenCode build
  (see docs/known-limitations.md)

EOF

  if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY RUN COMPLETE - No changes were made"
  fi
}

main "$@"