#!/usr/bin/env bash
# SessionEnd hook for `gls --live`.
# Removes the marker file written by gls-record-active.sh.
# Matches by session_id (robust to claude restarts mid-session) with PPID as fallback.

set -uo pipefail

INPUT=""
if [ ! -t 0 ]; then
  INPUT=$(cat)
fi

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)

if [ -n "$SESSION_ID" ]; then
  for f in ~/.claude/active/*.json; do
    [ -f "$f" ] || continue
    [ "$(jq -r .uuid "$f" 2>/dev/null)" = "$SESSION_ID" ] && rm -f "$f"
  done
else
  rm -f ~/.claude/active/"$PPID".json 2>/dev/null
fi

exit 0
