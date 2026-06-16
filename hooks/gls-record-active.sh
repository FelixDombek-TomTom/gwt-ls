#!/usr/bin/env bash
# SessionStart hook for `gls --live`.
# Writes ~/.claude/active/<pid>.json so gls can map a live claude process to its
# session uuid + cwd without heuristic guesswork.
#
# Claude Code passes a JSON payload on stdin (includes session_id), and the
# parent process of this script is the claude binary itself — so $PPID is the
# pid that `gls --live` is trying to attribute.

set -uo pipefail
mkdir -p ~/.claude/active

INPUT=""
if [ ! -t 0 ]; then
  INPUT=$(cat)
fi

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[ -z "$SESSION_ID" ] && exit 0

CWD="${CLAUDE_PROJECT_DIR:-$PWD}"
PID="$PPID"
NOW=$(date -Iseconds)

jq -n \
  --arg uuid "$SESSION_ID" \
  --arg cwd  "$CWD" \
  --argjson pid "$PID" \
  --arg ts   "$NOW" \
  '{uuid:$uuid, cwd:$cwd, pid:$pid, started_at:$ts}' \
  > ~/.claude/active/"$PID".json

# Hook must not block session startup.
exit 0
