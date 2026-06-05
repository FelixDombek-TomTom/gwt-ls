# gwt-ls

Inspect git worktrees alongside the Claude Code sessions that ran in them.

A single-file Python tool. Two complementary views:

- **Worktree view** (default): per-repo blocks listing every worktree, its branch, and the latest Claude session(s), incl. PRs and their status.
- **Latest-sessions feed** (`-c`): list of the most recent Claude sessions across all worktrees, newest first, optionally filtered by dir.

PR titles and badges are fetched in parallel from `gh` and rendered progressively — the table appears instantly with `#NUM …` placeholders; each PR's full info then patches into its line as it arrives.

## Install

```sh
git clone https://github.com/FelixDombek-TomTom/gwt-ls.git
chmod +x gwt-ls/gwt-ls.py
ln -s "$(pwd)/gwt-ls/gwt-ls.py" ~/.local/bin/gls   # or whatever short name you prefer
```

Requires Python 3.10+, `git`, and `gh` (for PR titles + badges; everything else degrades gracefully without it).

## Flags & sample output

Examples below use a fictional org `example-org` and a hypothetical `~/code/multi-repo` layout with one main repo and a few worktrees. ANSI styling (bold repo names, cyan branches, dim metadata, colored badges) is stripped here; on a TTY there is also color.

### `gls [PATH]` — default (folder or repo mode)

When `PATH` (or `$PWD`) is inside a git repo, lists that repo's worktrees. Otherwise scans `PATH`'s children for repos and groups them. Implicitly enables `-s 1`.

```
$ gls ~/code/multi-repo
multi-repo    /home/user/code/multi-repo
  *  2026-04-01 17:22  —  (non-git)  1 session
     2026-04-08 11:55  4.5M  Evaluate options for new prototype
                             #842 feat(experiment): add prototype playground  [draft] [CI ✗]

frontend    /home/user/code/multi-repo/frontend
  *    2026-04-09 16:25  aee2268b  main                                          5 sessions
       2026-04-02 00:51      1.9M  Plan backwards-compatible constructor refactor
                                   #341 refactor(api): tidy up the public surface  [merged]
  wt1  2026-04-03 13:35  4cf09001  feature/metadata-provider                      2 sessions
       2026-04-08 15:29      5.1M  add-metadata-feature-toggle
                                   #347 feat(api): set new flag in MetadataProvider behind toggle  [merged]
```

The `*` marks the main worktree; secondary worktrees show their suffix (`wt1`, `wt2`…). A `→` prefix marks the worktree containing your current `$PWD`. Non-git folders that ran Claude sessions still appear (with `—  (non-git)` instead of sha/branch).

### `-s N`, `--session-details N` — how many session detail lines

Default `1`. `-s 0` collapses each worktree to a one-line summary with `last <date>`.

```
$ gls -s 0 ~/code/multi-repo
multi-repo    /home/user/code/multi-repo
  *  2026-04-01 17:22  —  (non-git)  1 session, last 2026-04-08 11:55

frontend    /home/user/code/multi-repo/frontend
  *    2026-04-09 16:25  aee2268b  main                       5 sessions, last 2026-04-02 00:51
  wt1  2026-04-03 13:35  4cf09001  feature/metadata-provider  2 sessions, last 2026-04-08 15:29
```

```
$ gls -s 3 ~/code/multi-repo
…
frontend    /home/user/code/multi-repo/frontend
  *    2026-04-09 16:25  aee2268b  main  5 sessions
       2026-04-02 00:51      1.9M  Plan backwards-compatible constructor refactor
                                   #341 refactor(api): tidy up the public surface  [merged]
       2026-03-26 04:53      1.0M  Fix CI failures in pull request
                                   #310 feat(api): add new override params  [merged]
       2026-03-25 18:18      1.0M  Review tradeoff analysis table
```

### `-a`, `--all` — every session, no cap

Overrides `-s`.

```
$ gls -a ~/code/multi-repo
…
       2026-04-02 00:51      1.9M  Plan backwards-compatible constructor refactor
                                   #341 refactor(api): tidy up the public surface  [merged]
       2026-03-26 04:53      1.0M  Fix CI failures in pull request
                                   #310 feat(api): add new override params  [merged]
       2026-03-25 18:18      1.0M  Review tradeoff analysis table
       2026-03-17 16:08     61.5K  Inspect frozen Docker container
       2026-03-11 12:27     21.4K  Add line break in statusline
```

### `-x`, `--extras` — branch from JSONL, full uuid, slug, last prompt

Adds three indented lines beneath each detail row. Useful when a session was started in a parent folder and worked in a subdir — the `@ subdir` annotation tells you which child repo it touched.

```
$ gls -x ~/code/multi-repo
multi-repo    /home/user/code/multi-repo
  *  2026-04-01 17:22  —  (non-git)  1 session
     2026-04-08 11:55  4.5M  Evaluate options for new prototype
                             #842 feat(experiment): add prototype playground  [draft] [CI ✗]
                             @ frontend
                             5806e16b-4127-4123-81be-868100926c27
                             ↳ try a different approach

frontend    /home/user/code/multi-repo/frontend
  *    2026-04-09 16:25  aee2268b  main  5 sessions
       2026-04-02 00:51      1.9M  Plan backwards-compatible constructor refactor
                                   #341 refactor(api): tidy up the public surface  [merged]
                                   main
                                   483166e0-cb02-4520-8cb9-4f33ea2f25ce
                                   ↳ Check the CI failures
```

### `-c`, `--claude` — latest Claude sessions across everything

A flat feed of the most recent sessions, regardless of which repo or worktree they belong to. Bumps the default of `-s` to `10`. Combines with `-s N` and `-a`.

```
$ gls -c
projects/dotfiles
  2026-04-10 15:59    1.8M  fix-prompt-formatting

multi-repo/frontend
  2026-04-10 15:54    1.9M  Wire up new builder API
                            #348 refactor(api): migrate construction to builder pattern  [CI ✗]

multi-repo/backend-wt4  main
  2026-04-10 10:12    4.5M  Review failing CI on feature branch
                            #356 feat(demo): expose new field in the demo app  [merged]
…
```

With a `PATH`, only sessions whose recorded `cwd` lives under it are kept:

```
$ gls -c ~/code/multi-repo -s 3
multi-repo/frontend
  2026-04-10 15:54    1.9M  Wire up new builder API
                            #348 refactor(api): migrate construction to builder pattern  [CI ✗]

multi-repo/backend
  2026-04-10 14:04  680.4K  Inspect ticket state and changes

multi-repo/backend-wt4  main
  2026-04-10 10:12    4.5M  Review failing CI on feature branch
                            #356 feat(demo): expose new field in the demo app  [merged]
```

### `--no-pr-titles` — skip the `gh` calls

Faster, offline-safe. Drops PR titles and all status badges; the `#NUM` link stays.

```
$ gls --no-pr-titles ~/code/multi-repo
multi-repo    /home/user/code/multi-repo
  *  2026-04-01 17:22  —  (non-git)  1 session
     2026-04-08 11:55  4.5M  Evaluate options for new prototype
                             #842

frontend    /home/user/code/multi-repo/frontend
  *    2026-04-09 16:25  aee2268b  main  5 sessions
       2026-04-02 00:51      1.9M  Plan backwards-compatible constructor refactor
                                   #341
```

### `--no-color` — disable ANSI

Forces plain output even on a TTY. Useful when piping to a pager or capturing for docs (as on this page). PR `gh` lookups still happen synchronously and the titles still render — but as plain text, no progressive-fill animation.

### `--json` — machine-readable

`mode` is `"folder"`, `"repo"`, or `"claude"` (with `-c`). Sessions appear flat under `sessions` in claude mode, nested under repos/worktrees in folder/repo mode. All known fields surface, including `pr_state`, `pr_ci`, `slug`, `git_branch`, `last_cwd`, `agent_name`.

```
$ gls -c -s 1 --json
{
    "mode": "claude",
    "filter_path": null,
    "sessions": [
        {
            "uuid": "773ed2c6-7240-46bd-964f-0fdef111ada0",
            "mtime": 1780667943.27,
            "size": 1935307,
            "title": "fix-prompt-formatting",
            "last_prompt": "let's run the tests again",
            "pr_number": null,
            "slug": "shimmying-cuddling-lighthouse",
            "git_branch": "main",
            "last_cwd": "/home/user/projects/dotfiles",
            "agent_name": "fix-prompt-formatting"
        }
    ]
}
```

## What the columns mean

- **Worktree row:** label (`*` for main, suffix for secondaries) · directory mtime · 8-char HEAD · branch · `N sessions[, last <date>]`.
- **Session detail row:** session mtime · JSONL size (rough proxy for transcript length, not tokens) · agent name (when distinct from the AI title) · AI title · PR link + title + badges.
- **`-x` extras:** session's recorded `gitBranch` and `@ subdir` (when it worked in a child of a non-git parent) · `slug uuid` · most recent user prompt.

## Notes

- Session data lives in `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. The encoding is `/` and `.` → `-`. gwt-ls reads these directly.
- PR titles and statuses come from `gh pr view --json title,state,isDraft,statusCheckRollup,reviewDecision`. No network requests are made for non-PR sessions, and `--no-pr-titles` skips all of them.
- All output is read-only; the tool never mutates worktrees, git state, or session files.
