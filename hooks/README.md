# gls hooks

Two Claude Code hooks that let `gls --live` attribute running sessions exactly (no `/proc` + JSONL-birth-time heuristic):

- `gls-record-active.sh` — runs on `SessionStart`, writes `~/.claude/active/<pid>.json` with `{uuid, cwd, pid, started_at}`.
- `gls-remove-active.sh` — runs on `SessionEnd`, removes that file. Matches by `session_id` (robust to claude restarts mid-session) with PPID as a fallback.

## Install

```sh
mkdir -p ~/.claude/hooks
cp hooks/gls-*.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/gls-*.sh
```

Then add to `~/.claude/settings.json` (merging with any existing `hooks` key):

```jsonc
"hooks": {
  "SessionStart": [
    {"hooks": [{"type": "command", "command": "bash $HOME/.claude/hooks/gls-record-active.sh"}]}
  ],
  "SessionEnd": [
    {"hooks": [{"type": "command", "command": "bash $HOME/.claude/hooks/gls-remove-active.sh"}]}
  ]
}
```

Restart any running `claude` sessions so they pick up the SessionStart hook. (Sessions still alive from before the hook was installed will continue to rely on the heuristic until they exit and you restart them.)

## Optional: cron snapshot

Adds a 5-minute snapshot of the live set to `~/.claude/tabs.json` so reboot recovery via `gls --restore-tabs` always has a recent manifest:

```cron
*/5 * * * * /home/<you>/.local/bin/gls --save-tabs >/dev/null 2>&1
```

Install via `crontab -e`.

## Requirements

`jq` (for reading the hook's stdin JSON). Already a dependency of the existing `remember` plugin's hooks if you have those installed.
