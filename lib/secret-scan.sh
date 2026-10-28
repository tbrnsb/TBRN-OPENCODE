#!/usr/bin/env bash
# secret-scan.sh — block commits whose staged diff contains secret material.
# Usage: secret-scan.sh [diff-file]
#   No arg  -> scans `git diff --cached` in the current repo (archiver use).
#   With arg -> scans that file (tests, dry runs).
# Exit 0 = clean, 1 = secret found (blocks the commit).
set -uo pipefail

INPUT="${1:-}"
if [[ -z "$INPUT" ]]; then
  DATA="$(git diff --cached)"
else
  DATA="$(cat "$INPUT")"
fi

PAT='sk-or-[A-Za-z0-9_-]{8,}|nvapi-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{8,}|mstrl_[A-Za-z0-9_-]{16,}|AQ\.Ab[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}|[Bb]earer [A-Za-z0-9_.\-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|-----END [A-Z ]*PRIVATE KEY-----'

HITS="$(printf '%s' "$DATA" | grep -oE "$PAT" | sort -u)"
if [[ -n "$HITS" ]]; then
  echo "secret-scan: BLOCKED — secret-like strings in diff:" >&2
  # Print shapes, not values: first 8 chars only, so the report itself is safe.
  printf '%s\n' "$HITS" | cut -c1-8 | sed 's/^/  pattern: /' >&2
  exit 1
fi
echo "secret-scan: clean"
