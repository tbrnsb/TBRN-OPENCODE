#!/usr/bin/env bash
# fix-gsd-frontmatter.sh — make the GSD agents loadable by opencode.
#
# WHY THIS EXISTS
#   The GSD agents in ~/.config/opencode/agents/ are copies of
#   ~/.config/opencode/gsd-core/agents/*.md. The canonical GSD copies carry
#   `tools:` and `color:` frontmatter written for a different runtime. OpenCode
#   1.18.34 REJECTS the whole agent registry when it meets them:
#
#     Configuration is invalid at .../agents/gsd-verifier.md
#     Expected object | undefined, got "Read, Write, Bash, Grep, Glob, Skill" tools
#
#   That failure is global, not per-agent: `opencode agent list` returns nothing,
#   so the 36 rung agents stop loading too. After any GSD update or reinstall
#   those lines come back and the entire system goes dark.
#
# WHAT IT DOES
#   Deletes only `tools:` and `color:` lines from gsd-*.md in the agents dir.
#   Idempotent. Touches nothing else. Reports what it changed.
#
# NOTE
#   GSD's own agent-install-check.cjs derives a Codex sandbox_mode from the
#   `tools:` value, so with the line removed GSD derives `read-only` for those
#   roles. That path is inert for the opencode runtime; see SYSTEM-BRIEF.md.
#
# USAGE
#   bash ~/.config/opencode/scripts/fix-gsd-frontmatter.sh [--check]
#
#   --check   report what would change, change nothing. Exit 1 if a repair is
#             needed, so it can be used as a test.

set -uo pipefail

AGENTS_DIR="${GSD_LIVE_AGENTS_DIR:-$HOME/.config/opencode/agents}"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

if [[ ! -d "$AGENTS_DIR" ]]; then
  echo "fix-gsd-frontmatter: no agents dir at $AGENTS_DIR" >&2
  exit 2
fi

shopt -s nullglob
targets=("$AGENTS_DIR"/gsd-*.md)
if (( ${#targets[@]} == 0 )); then
  echo "fix-gsd-frontmatter: no gsd-*.md in $AGENTS_DIR — nothing to do"
  exit 0
fi

dirty=()   # full paths
names=()   # basenames, for display
for f in "${targets[@]}"; do
  if grep -qE '^(tools|color):' "$f"; then
    dirty+=("$f")
    names+=("$(basename "$f")")
  fi
done

if (( ${#dirty[@]} == 0 )); then
  echo "fix-gsd-frontmatter: clean — ${#targets[@]} gsd agents, no tools:/color: lines"
  exit 0
fi

if (( CHECK_ONLY )); then
  echo "fix-gsd-frontmatter: NEEDS REPAIR — ${#dirty[@]} of ${#targets[@]} files:"
  printf '  %s\n' "${names[@]}"
  echo "Run without --check to repair."
  exit 1
fi

for f in "${dirty[@]}"; do
  sed -i '/^tools:/d;/^color:/d' "$f"
done

echo "fix-gsd-frontmatter: repaired ${#dirty[@]} of ${#targets[@]} gsd agents"
printf '  %s\n' "${names[@]}" | head -5
(( ${#dirty[@]} > 5 )) && echo "  ... and $(( ${#dirty[@]} - 5 )) more"

# Verify the registry actually loads now. Without this, the strip can succeed
# and the system still be broken for some other frontmatter reason.
if command -v opencode >/dev/null 2>&1; then
  if out="$(opencode agent list 2>&1)"; then
    n="$(grep -cE '^[a-z0-9.-]+ \((primary|subagent)\)' <<<"$out")"
    echo "fix-gsd-frontmatter: registry OK — $n agents load"
  else
    echo "fix-gsd-frontmatter: WARNING — opencode agent list still fails:" >&2
    head -5 <<<"$out" >&2
    exit 1
  fi
fi