# gwt-ls

Inspect git worktrees alongside the Claude Code sessions that ran in them.

A single-file Python tool. Two complementary views:

- **Worktree view** (default): per-repo blocks listing every worktree, its branch, and the latest Claude session(s) — title, PR (clickable OSC 8 link), badges for `[merged]` / `[closed]` / `[draft]` / `[CI ✓ / ✗ / ⋯]` / `[approved]` / `[changes requested]`.
- **Latest-sessions feed** (`-c`): flat list of the most recent Claude sessions across all worktrees, newest first, with optional cwd-prefix filter.

## Install

```sh
git clone https://github.com/FelixDombek-TomTom/gwt-ls.git
chmod +x gwt-ls/gwt-ls.py
ln -s "$(pwd)/gwt-ls/gwt-ls.py" ~/.local/bin/gls   # or whatever short name you prefer
```

Requires Python 3.10+, `git`, and `gh` (for PR titles + badges; everything else degrades gracefully without it).

## Usage

```sh
gls                       # current dir: repo mode if inside a git repo, else folder scan
gls ~/code/navsdk         # scan a folder of repos
gls -s 3                  # show 3 session detail lines per worktree
gls -a                    # show all session details
gls -x                    # add session uuid + slug + last user prompt below each detail
gls -c                    # latest 10 Claude sessions across everything
gls -c -s 20              # 20 latest
gls -c ~/code/navsdk      # latest sessions whose cwd lives under ~/code/navsdk
gls --json                # machine-readable output
gls --no-pr-titles        # skip gh PR lookups (offline-safe / instant)
```

## What it shows

- For each worktree: directory mtime, short HEAD, branch, "N sessions" count.
- For each session detail: timestamp, JSONL size, agent name (white), AI title (gray), PR link with title and status badges.
- With `-x`: branch (from the JSONL — sometimes a different subdir than the worktree if the session was started in a parent folder), session slug + UUID, and the most recent user prompt.

PR titles and badges are fetched in parallel from `gh` and rendered progressively — the table appears instantly with `#NUM …` placeholders, then each PR's full info patches into its line as it arrives.
