#!/usr/bin/env python3
"""gwt-ls — list git worktrees plus their Claude Code sessions.

Folder mode (default): scan direct children of PATH, group by owning repo.
Repo mode: when PATH (or PWD) is inside a git repo, list that repo's worktrees only.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


@dataclass
class Session:
    uuid: str
    mtime: float
    size: int
    title: str | None = None
    last_prompt: str | None = None
    pr_number: int | None = None
    pr_repo: str | None = None
    pr_url: str | None = None
    pr_title: str | None = None
    pr_state: str | None = None         # "OPEN" / "MERGED" / "CLOSED"
    pr_is_draft: bool = False
    pr_ci: str | None = None            # "pass" / "fail" / "pending" / None
    pr_review: str | None = None        # "APPROVED" / "CHANGES_REQUESTED" / "REVIEW_REQUIRED" / None
    slug: str | None = None
    git_branch: str | None = None       # gitBranch as recorded in the JSONL
    last_cwd: str | None = None         # cwd at the last record (may differ from worktree path)
    agent_name: str | None = None       # slug-like name from `agent-name` records
    is_live: bool = False               # a claude process is currently running this session
    live_pid: int | None = None         # pid of that process (when is_live)


@dataclass
class Worktree:
    path: str
    head: str
    branch: str | None      # short branch name; None if detached
    detached: bool
    is_main: bool
    dir_mtime: float | None
    external: bool          # true if path is outside the scanned folder (folder mode only)
    current: bool = False   # cwd lives inside this worktree
    sessions: list[Session] = field(default_factory=list)


@dataclass
class Repo:
    name: str
    common_dir: str
    worktrees: list[Worktree]


def run_git(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def is_inside_worktree(path: str) -> bool:
    rc, out, _ = run_git(["-C", path, "rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


def common_dir_of(path: str) -> str | None:
    rc, out, _ = run_git(["-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if rc != 0:
        return None
    return str(Path(out.strip()).resolve())


def parse_worktrees(repo_path: str) -> list[Worktree]:
    rc, out, _ = run_git(["-C", repo_path, "worktree", "list", "--porcelain"])
    if rc != 0:
        return []
    records: list[Worktree] = []
    is_main = True
    for block in out.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if not line:
                continue
            if " " in line:
                k, v = line.split(" ", 1)
                fields[k] = v
            else:
                fields[line] = ""
        if "worktree" not in fields:
            continue
        wt_path = fields["worktree"]
        head = fields.get("HEAD", "")
        branch_ref = fields.get("branch")
        detached = "detached" in fields
        branch: str | None = None
        if branch_ref:
            branch = branch_ref.removeprefix("refs/heads/")
        try:
            dir_mtime = os.stat(wt_path).st_mtime
        except OSError:
            dir_mtime = None
        records.append(
            Worktree(
                path=wt_path,
                head=head,
                branch=branch,
                detached=detached,
                is_main=is_main,
                dir_mtime=dir_mtime,
                external=False,
            )
        )
        is_main = False
    return records


def _ci_rollup(checks: list[dict]) -> str | None:
    """Roll a list of statusCheckRollup entries into one of: 'pass'/'fail'/'pending'/None."""
    if not checks:
        return None
    failing = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    pending = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"}
    passing = {"SUCCESS", "NEUTRAL", "SKIPPED", "STALE", "EXPECTED"}
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(s in failing for s in states):
        return "fail"
    if any(s in pending for s in states):
        return "pending"
    if all(s in passing or s == "" for s in states):
        return "pass"
    return None


def fetch_pr_info(repo: str, number: int) -> dict | None:
    """Returns {title, state, is_draft, ci, review} or None on failure."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", repo,
             "--json", "title,state,isDraft,statusCheckRollup,reviewDecision"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return {
        "title": data.get("title") or None,
        "state": data.get("state") or None,
        "is_draft": bool(data.get("isDraft")),
        "ci": _ci_rollup(data.get("statusCheckRollup") or []),
        "review": data.get("reviewDecision") or None,
    }


def _apply_pr_info(s: Session, info: dict) -> None:
    if info.get("title"):
        s.pr_title = info["title"]
    if info.get("state"):
        s.pr_state = info["state"]
    s.pr_is_draft = info.get("is_draft", False)
    s.pr_ci = info.get("ci")
    s.pr_review = info.get("review")


def fetch_pr_titles(sessions: list[Session]) -> None:
    """Populate PR metadata for the given list, in parallel via `gh`."""
    from concurrent.futures import ThreadPoolExecutor
    pairs: dict[tuple[str, int], list[Session]] = {}
    for s in sessions:
        if not (s.pr_number and s.pr_repo):
            continue
        if s.pr_url and "github.com" not in s.pr_url:
            continue
        pairs.setdefault((s.pr_repo, s.pr_number), []).append(s)
    if not pairs:
        return
    with ThreadPoolExecutor(max_workers=min(10, len(pairs))) as ex:
        futs = {ex.submit(fetch_pr_info, repo, num): (repo, num) for repo, num in pairs}
        for fut in futs:
            info = fut.result()
            if not info:
                continue
            for s in pairs[futs[fut]]:
                _apply_pr_info(s, info)


def encode_project_path(p: str) -> str:
    # Claude Code's project-dir encoding: replace '/' and '.' with '-'.
    # e.g. /home/jane/.claude → -home-jane--claude.
    return p.replace("/", "-").replace(".", "-")


def load_sessions(worktree_path: str) -> list[Session]:
    proj_dir = CLAUDE_PROJECTS / encode_project_path(worktree_path)
    if not proj_dir.is_dir():
        return []
    out: list[Session] = []
    for f in proj_dir.glob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append(Session(uuid=f.stem, mtime=st.st_mtime, size=st.st_size))
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def _enrich_session_from_path(s: Session, path: Path) -> None:
    """Stream a JSONL file to populate Session metadata (last-write-wins for envelope fields)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # most records carry these envelope fields; last write wins
                if v := r.get("slug"):
                    s.slug = v
                if v := r.get("gitBranch"):
                    s.git_branch = v
                if v := r.get("cwd"):
                    s.last_cwd = v
                t = r.get("type")
                if t == "ai-title":
                    v = r.get("aiTitle")
                    if v:
                        s.title = v
                elif t == "last-prompt":
                    v = r.get("lastPrompt")
                    if v:
                        s.last_prompt = v
                elif t == "agent-name":
                    v = r.get("agentName")
                    if v:
                        s.agent_name = v
                elif t == "pr-link":
                    n = r.get("prNumber")
                    repo = r.get("prRepository")
                    url = r.get("prUrl")
                    if n is not None:
                        s.pr_number = n
                    if repo:
                        s.pr_repo = repo
                    if url:
                        s.pr_url = url
    except OSError:
        return


def enrich_session(s: Session, worktree_path: str) -> None:
    """Stream the session's JSONL to pick up title / last prompt / PR link / etc."""
    proj_dir = CLAUDE_PROJECTS / encode_project_path(worktree_path)
    _enrich_session_from_path(s, proj_dir / f"{s.uuid}.jsonl")


_TMP_PREFIXES = ("/tmp/", "/var/tmp/")


def _is_tmp_cwd(path: str) -> bool:
    return path in ("/tmp", "/var/tmp") or any(path.startswith(p) for p in _TMP_PREFIXES)


def _resume_index() -> list[Session]:
    """Lightweight uuid + cwd index across all Claude session JSONLs.

    Reads only the first line of each file to recover cwd from the envelope
    (much cheaper than full enrichment). Used by `gls -r <prefix>`.
    """
    out: list[Session] = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for proj_dir in CLAUDE_PROJECTS.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            cwd: str | None = None
            try:
                with f.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if v := r.get("cwd"):
                            cwd = v
                            break
            except OSError:
                pass
            out.append(Session(
                uuid=f.stem, mtime=st.st_mtime, size=st.st_size,
                last_cwd=cwd,
            ))
    return out


CLAUDE_ACTIVE = Path.home() / ".claude" / "active"
DEFAULT_TABS_PATH = Path.home() / ".claude" / "tabs.json"


def _default_tab_opener() -> str:
    """Build the default opener at call-time so it picks up the current $SHELL.

    The first shell skips its rc file (--norc for bash, --no-rcs for zsh) so a
    setup that exec's another shell from rc (e.g. `exec zsh` in .bashrc) can't
    swallow the `-c` argument before claude launches. The trailing `exec $SHELL`
    is a normal interactive invocation that re-loads the rc — so the post-claude
    shell behaves exactly as a fresh terminal would.
    """
    shell = os.environ.get("SHELL", "/bin/bash")
    name = Path(shell).name
    if name == "bash":
        pre = f"{shell} --norc"
    elif name == "zsh":
        pre = f"{shell} --no-rcs"
    else:
        pre = shell  # fish / other — best effort, may need GLS_TAB_OPENER override
    return (
        "gnome-terminal --tab --working-directory={cwd} -- "
        f"{pre} -ic '{{cmd}}; exec {shell}'"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _running_claude_pids() -> list[dict]:
    """Walk /proc, return one dict per running `claude` process: pid, cwd, started_at."""
    out: list[dict] = []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return out
    for name in proc_entries:
        if not name.isdigit():
            continue
        proc = f"/proc/{name}"
        try:
            with open(f"{proc}/comm") as fh:
                if fh.read().strip() != "claude":
                    continue
            cwd = os.readlink(f"{proc}/cwd")
            st = os.stat(proc)
        except (OSError, PermissionError):
            continue
        out.append({"pid": int(name), "cwd": cwd, "started_at": st.st_mtime})
    return out


def _read_active_entries() -> dict[int, dict]:
    """Read ~/.claude/active/*.json. Drop entries whose pid is no longer alive."""
    out: dict[int, dict] = {}
    if not CLAUDE_ACTIVE.is_dir():
        return out
    for f in CLAUDE_ACTIVE.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            try:
                f.unlink()
            except OSError:
                pass
            continue
        out[pid] = data
    return out


def _heuristic_uuid_for(cwd: str, started_at: float, claimed: set[str]) -> str | None:
    """Find the JSONL in `cwd`'s project dir most likely owned by a process started at
    `started_at`. Catches both fresh sessions (JSONL born after pstart) and resumed
    sessions (JSONL pre-existed but its mtime has been updated since pstart).
    Excludes uuids already claimed by an earlier hook-tracked or heuristic match."""
    proj_dir = CLAUDE_PROJECTS / encode_project_path(cwd)
    if not proj_dir.is_dir():
        return None
    # Candidates: JSONLs whose birth OR mtime is later than pstart-30s.
    # Rank by mtime descending so the most-recently-active session wins.
    candidates: list[tuple[float, str]] = []
    for f in proj_dir.glob("*.jsonl"):
        if f.stem in claimed:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        birth = getattr(st, "st_birthtime", None) or st.st_ctime
        threshold = started_at - 30
        if birth >= threshold or st.st_mtime >= threshold:
            candidates.append((st.st_mtime, f.stem))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def live_sessions() -> list[Session]:
    """Combine hook-tracked + /proc-heuristic detection into a list of running Sessions."""
    active = _read_active_entries()
    procs = _running_claude_pids()
    claimed: set[str] = set()
    sessions: list[Session] = []

    # 1) hook-tracked entries (exact)
    for pid, data in active.items():
        uuid = data.get("uuid")
        cwd = data.get("cwd")
        if not uuid or not cwd:
            continue
        claimed.add(uuid)
        jsonl = CLAUDE_PROJECTS / encode_project_path(cwd) / f"{uuid}.jsonl"
        try:
            st = jsonl.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = data.get("started_at_epoch", 0.0), 0
        s = Session(uuid=uuid, mtime=mtime, size=size, last_cwd=cwd,
                    is_live=True, live_pid=pid)
        if jsonl.is_file():
            _enrich_session_from_path(s, jsonl)
        sessions.append(s)

    # 2) heuristic for unhooked processes. Sort youngest-first so the most-recently-
    # spawned pid gets first pick of the most-recently-modified JSONL — that's the
    # right intuition for "this pid is probably writing to this JSONL right now".
    procs.sort(key=lambda p: p["started_at"], reverse=True)
    for p in procs:
        if p["pid"] in active:
            continue
        uuid = _heuristic_uuid_for(p["cwd"], p["started_at"], claimed)
        if uuid is None:
            # process is running but we can't pin a uuid — still surface it
            s = Session(uuid=f"?{p['pid']}", mtime=p["started_at"], size=0,
                        last_cwd=p["cwd"], is_live=True, live_pid=p["pid"])
            sessions.append(s)
            continue
        claimed.add(uuid)
        jsonl = CLAUDE_PROJECTS / encode_project_path(p["cwd"]) / f"{uuid}.jsonl"
        try:
            st = jsonl.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = p["started_at"], 0
        s = Session(uuid=uuid, mtime=mtime, size=size, last_cwd=p["cwd"],
                    is_live=True, live_pid=p["pid"])
        _enrich_session_from_path(s, jsonl)
        sessions.append(s)

    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def spawn_tab(cwd: str, uuid: str | None) -> None:
    """Fire-and-forget detached spawn of a new gnome-terminal tab via $GLS_TAB_OPENER."""
    template = os.environ.get("GLS_TAB_OPENER", _default_tab_opener())
    cmd_inner = f"claude -r {shlex.quote(uuid)}" if uuid else "claude"
    rendered = template.format(cwd=shlex.quote(cwd), cmd=cmd_inner, uuid=uuid or "")
    subprocess.Popen(
        rendered, shell=True, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def save_tabs(path: Path) -> int:
    """Snapshot the current live set to PATH atomically. Returns count saved."""
    sessions = live_sessions()
    doc = {
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tabs": [
            {
                "cwd": s.last_cwd,
                "uuid": s.uuid if not s.uuid.startswith("?") else None,
                "pid": s.live_pid,
                "title": s.title or s.agent_name,
            }
            for s in sessions
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, path)
    return len(doc["tabs"])


def restore_tabs(path: Path, *, dry_run: bool = False) -> int:
    """Spawn one detached tab per entry in PATH. Returns count spawned (or would-spawn)."""
    import time as _time
    try:
        doc = json.loads(path.read_text())
    except OSError as e:
        print(f"gls: cannot read {path}: {e}", file=sys.stderr)
        return 0
    tabs = doc.get("tabs", [])
    spawned = 0
    for tab in tabs:
        cwd = tab.get("cwd")
        uuid = tab.get("uuid")
        if not cwd:
            continue
        if dry_run:
            template = os.environ.get("GLS_TAB_OPENER", _default_tab_opener())
            cmd_inner = f"claude -r {shlex.quote(uuid)}" if uuid else "claude"
            rendered = template.format(cwd=shlex.quote(cwd), cmd=cmd_inner, uuid=uuid or "")
            print(f"# {rendered}")
        else:
            spawn_tab(cwd, uuid)
            _time.sleep(0.15)  # let gnome-terminal-server breathe between rapid spawns
        spawned += 1
    return spawned


def resume_claude(prefix: str, prompt: str | None, *, dry_run: bool, new_tab: bool = False) -> int:
    """Look up a Claude session by short-UUID prefix, chdir to its workdir, exec claude -r.

    With new_tab=True, spawn a detached gnome-terminal tab instead of in-place exec.
    Returns an exit code on error; on success, replaces the current process (or returns 0
    immediately if new_tab=True).
    """
    matches = [s for s in _resume_index() if s.uuid.startswith(prefix)]
    if not matches:
        print(f"gls: no session matches prefix {prefix!r}", file=sys.stderr)
        return 2
    if len(matches) > 1:
        print(f"gls: prefix {prefix!r} matches multiple sessions:", file=sys.stderr)
        matches.sort(key=lambda s: s.mtime, reverse=True)
        for s in matches[:10]:
            label = _session_label(s)
            ts = fmt_mtime(s.mtime)
            print(f"  {s.uuid[:8]}  {ts}  {label}", file=sys.stderr)
        if len(matches) > 10:
            print(f"  … ({len(matches) - 10} more)", file=sys.stderr)
        print("specify more characters to disambiguate.", file=sys.stderr)
        return 2
    s = matches[0]
    cwd = s.last_cwd
    if not cwd or not Path(cwd).is_dir():
        if cwd:
            print(f"gls: warning: recorded workdir {cwd!r} doesn't exist; using $HOME",
                  file=sys.stderr)
        else:
            print(f"gls: warning: no recorded workdir for {s.uuid[:8]}; using $HOME",
                  file=sys.stderr)
        cwd = str(Path.home())
    cmd = ["claude", "-r", s.uuid] + ([prompt] if prompt else [])
    if dry_run:
        if new_tab:
            template = os.environ.get("GLS_TAB_OPENER", _default_tab_opener())
            cmd_inner = f"claude -r {shlex.quote(s.uuid)}"
            print(f"# would spawn tab via: "
                  f"{template.format(cwd=shlex.quote(cwd), cmd=cmd_inner, uuid=s.uuid)}")
        else:
            print(f"# would chdir to: {cwd}")
            print(f"# would exec:     {' '.join(shlex.quote(a) for a in cmd)}")
        return 0
    if new_tab:
        spawn_tab(cwd, s.uuid)
        return 0
    os.chdir(cwd)
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        print(f"gls: {cmd[0]!r} not on PATH", file=sys.stderr)
        return 127
    return 0  # unreachable


def discover_latest_sessions(
    n: int, filter_path: Path | None, *, include_tmp: bool = False,
) -> list[Session]:
    """Enumerate every JSONL under ~/.claude/projects, sort by mtime desc, return top N
    (optionally filtered to sessions whose recorded cwd is under `filter_path`).

    By default sessions whose cwd lives under /tmp or /var/tmp are skipped (these are
    almost always short throwaway runs); set include_tmp=True to keep them."""
    candidates: list[tuple[float, Path, str, int]] = []
    if not CLAUDE_PROJECTS.is_dir():
        return []
    for proj_dir in CLAUDE_PROJECTS.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            candidates.append((st.st_mtime, f, f.stem, st.st_size))
    candidates.sort(key=lambda x: x[0], reverse=True)

    out: list[Session] = []
    for mtime, jsonl_path, uuid, size in candidates:
        s = Session(uuid=uuid, mtime=mtime, size=size)
        _enrich_session_from_path(s, jsonl_path)
        if filter_path is not None:
            if not s.last_cwd:
                continue
            try:
                cwd_r = Path(s.last_cwd).resolve()
            except OSError:
                continue
            if cwd_r != filter_path and not cwd_r.is_relative_to(filter_path):
                continue
        elif not include_tmp and s.last_cwd and _is_tmp_cwd(s.last_cwd):
            continue
        out.append(s)
        if len(out) >= n:
            break
    return out


def _session_label(s: Session) -> str:
    """Build a 'parent/basename' style label from the session's recorded cwd."""
    cwd = s.last_cwd
    if not cwd:
        return "(unknown cwd)"
    p = Path(cwd)
    home = str(Path.home())
    s_cwd = str(p)
    if s_cwd == home:
        return "~"
    if s_cwd.startswith(home + "/"):
        rel = s_cwd[len(home) + 1:]
        parts = rel.split("/")
    else:
        parts = s_cwd.strip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[0] if parts else s_cwd


def render_latest(
    sessions: list[Session], num: int, extras: bool, st: Style, *,
    collect_pending: bool = False,
) -> tuple[str, list[PRPending]]:
    out: list[str] = []
    pending: list[PRPending] | None = [] if collect_pending else None
    w_mt = 16   # "YYYY-MM-DD HH:MM"
    size_w = 6
    indent = "  "
    # +10 for the 8-char short uuid column + 2 spaces, so the PR / extras lines
    # align under the title column on the detail row
    pr_indent = " " * (2 + w_mt + 2 + size_w + 2 + 8 + 2)
    extras_indent = pr_indent

    for i, s in enumerate(sessions):
        if i > 0:
            out.append("")
        label = _session_label(s)
        header = st.bold(label)
        if s.git_branch and s.git_branch != "HEAD":
            header = f"{header}  {st.cyan(s.git_branch)}"
        out.append(header)

        out.append(_title_line(s, indent, w_mt, size_w, st))

        loading = (
            pending is not None
            and s.pr_number is not None
            and not s.pr_title
        )
        pr_text = _pr_line(s, pr_indent, st, loading=loading)
        if pr_text is not None:
            out.append(pr_text)
            if loading:
                pending.append(PRPending(
                    line_index=len(out) - 1,
                    session=s, pr_indent=pr_indent,
                ))

        if extras:
            slug_uuid = f"{s.slug}  {s.uuid}" if s.slug else s.uuid
            out.append(f"{extras_indent}{st.dim(slug_uuid)}")
            if s.last_prompt:
                lp = s.last_prompt.replace("\n", " ").replace("\r", " ").strip()
                if len(lp) > 100:
                    lp = lp[:97] + "…"
                out.append(st.dim(f"{extras_indent}↳ {lp}"))
    return "\n".join(out), (pending or [])


def discover_repos_in_folder(folder: Path) -> tuple[list[Repo], list[str]]:
    """Returns (repos, other_entries).

    repos includes both real git repos (grouped by common_dir) and synthetic
    "non-git but has-Claude-sessions" entries — both rendered as Repo blocks.
    other_entries holds basenames of leftover children: non-git dirs with no
    sessions, plus all files and symlinks.
    """
    groups: dict[str, list[str]] = {}
    non_git_with_sessions: list[Repo] = []
    others: list[str] = []
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name)
    except OSError:
        return [], []
    for child in children:
        name = child.name
        if not child.is_dir() or child.is_symlink():
            others.append(name)
            continue
        cd = common_dir_of(str(child))
        if cd is not None:
            groups.setdefault(cd, []).append(str(child.resolve()))
            continue
        # not a git worktree — does it have any Claude sessions?
        sess = load_sessions(str(child))
        if not sess:
            others.append(name)
            continue
        try:
            dir_mtime = child.stat().st_mtime
        except OSError:
            dir_mtime = None
        wt = Worktree(
            path=str(child.resolve()),
            head="", branch=None, detached=False,
            is_main=True, dir_mtime=dir_mtime, external=False,
            sessions=sess,
        )
        non_git_with_sessions.append(Repo(name=name, common_dir="", worktrees=[wt]))

    repos: list[Repo] = []
    folder_resolved = str(folder.resolve())
    for cd, members in groups.items():
        rep = members[0]
        worktrees = parse_worktrees(rep)
        if not worktrees:
            continue
        for w in worktrees:
            wt_parent = str(Path(w.path).resolve().parent)
            w.external = wt_parent != folder_resolved
            w.sessions = load_sessions(w.path)
        name = Path(worktrees[0].path).name  # main worktree basename = repo name
        repos.append(Repo(name=name, common_dir=cd, worktrees=worktrees))
    repos.extend(non_git_with_sessions)
    repos.sort(key=lambda r: r.name)
    return repos, others


def label_for(w: Worktree, main_path: str) -> str:
    if w.is_main:
        return "*"
    main_name = Path(main_path).name
    own_name = Path(w.path).name
    if own_name.startswith(main_name):
        suffix = own_name[len(main_name):].lstrip("-_")
        return suffix or own_name
    return own_name


def fmt_mtime(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_size(n: int) -> str:
    units = ["B", "K", "M", "G", "T"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            if u == "B":
                return f"{int(f)}{u}"
            return f"{f:.1f}{u}"
        f /= 1024
    return f"{n}B"


class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def bold(self, s): return self._w("1", s)
    def dim(self, s): return self._w("2", s)
    def cyan(self, s): return self._w("36", s)
    def yellow(self, s): return self._w("33", s)
    def magenta(self, s): return self._w("35", s)
    def green(self, s): return self._w("32", s)
    def red(self, s): return self._w("31", s)
    def white(self, s): return self._w("97", s)        # bright white
    def light_gray(self, s): return self._w("37", s)   # normal white (renders as light gray on most palettes)

    def link(self, text: str, url: str | None) -> str:
        # OSC 8 hyperlink: ESC]8;;URL ST  TEXT  ESC]8;; ST  (ST = ESC \)
        if not self.on or not url:
            return text
        return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


@dataclass
class PRPending:
    """A PR line whose title/badges are being fetched asynchronously."""
    line_index: int          # 0-based index in the final output line list
    session: Session
    pr_indent: str           # whitespace to place the PR text under the title column


def _pr_badges(s: Session, st: Style) -> str:
    """Returns a space-prefixed badge string (e.g. '  [merged] [approved]'), or '' if none."""
    parts: list[str] = []
    if s.pr_state == "MERGED":
        parts.append(st.magenta("[merged]"))
    elif s.pr_state == "CLOSED":
        parts.append(st.red("[closed]"))
    else:  # OPEN or unknown
        if s.pr_is_draft:
            parts.append(st.dim("[draft]"))
        if s.pr_ci == "pass":
            parts.append(st.green("[CI ✓]"))
        elif s.pr_ci == "fail":
            parts.append(st.red("[CI ✗]"))
        elif s.pr_ci == "pending":
            parts.append(st.yellow("[CI ⋯]"))
        if s.pr_review == "APPROVED":
            parts.append(st.green("[approved]"))
        elif s.pr_review == "CHANGES_REQUESTED":
            parts.append(st.red("[changes requested]"))
    return ("  " + " ".join(parts)) if parts else ""


def _title_line(s: Session, indent: str, w_mt: int, size_w: int, st: Style) -> str:
    title_parts: list[str] = []
    has_agent = bool(s.agent_name and s.agent_name != s.title)
    if has_agent:
        title_parts.append(st.white(s.agent_name))
    if s.title:
        title_parts.append(st.light_gray(s.title) if has_agent else s.title)
    else:
        title_parts.append(st.dim("(no title)"))
    title = "  ".join(title_parts)
    short_uuid = s.uuid.split("-", 1)[0]
    meta = st.dim(f"{fmt_mtime(s.mtime):<{w_mt}}  {fmt_size(s.size):>{size_w}}  {short_uuid}")
    # for live sessions, replace the first 2 chars of `indent` with a green bullet so
    # PR/extras lines remain aligned (those compute their offset from the original indent)
    if s.is_live and len(indent) >= 2:
        indent = st.green("● ") + indent[2:]
    return f"{indent}{meta}  {title}"


def _pr_line(s: Session, pr_indent: str, st: Style, *, loading: bool) -> str | None:
    if not s.pr_number:
        return None
    pr_text = "#" + str(s.pr_number)
    if s.pr_title:
        t = s.pr_title.replace("\n", " ").strip()
        if len(t) > 80:
            t = t[:77] + "…"
        pr_text = f"{pr_text} {t}"
    elif loading:
        pr_text = f"{pr_text} …"
    pr_url = s.pr_url or (
        f"https://github.com/{s.pr_repo}/pull/{s.pr_number}" if s.pr_repo else None
    )
    return f"{pr_indent}{st.link(st.yellow(pr_text), pr_url)}{_pr_badges(s, st)}"


def render_repo(
    repo: Repo, num: int, extras: bool, st: Style, *,
    line_offset: int = 0, pending: list[PRPending] | None = None,
) -> list[str]:
    lines: list[str] = []
    main = next((w for w in repo.worktrees if w.is_main), repo.worktrees[0])
    lines.append(f"{st.bold(repo.name)}    {st.dim(main.path)}")

    rows: list[tuple[str, str, str, str, str, Worktree]] = []
    for w in repo.worktrees:
        lbl = label_for(w, main.path)
        if w.external:
            lbl = f"{lbl} {st.yellow('[external]')}"
        mt = fmt_mtime(w.dir_mtime)
        sha = (w.head or "")[:8] or "—"
        if w.detached:
            branch_txt = f"(detached @ {sha})"
        elif w.branch:
            branch_txt = w.branch
        elif not w.head:
            branch_txt = "(non-git)"
        else:
            branch_txt = "(unknown)"
        if w.sessions:
            n = len(w.sessions)
            base = f"{n} session{'s' if n != 1 else ''}"
            # omit ", last ..." when detail lines will show the same info right below
            sess_txt = base if num > 0 else f"{base}, last {fmt_mtime(w.sessions[0].mtime)}"
        else:
            sess_txt = st.dim("0 sessions")
        rows.append((lbl, mt, sha, branch_txt, sess_txt, w))

    # column widths (computed on the *plain* label for stable alignment with ANSI off too;
    # for simplicity we just measure the rendered strings)
    w_lbl = max(len(r[0]) for r in rows)
    w_mt = max(len(r[1]) for r in rows)
    w_sha = max(len(r[2]) for r in rows)
    w_br = max(len(r[3]) for r in rows)

    for lbl, mt, sha, br, sess, w in rows:
        branch_render = st.cyan(br) if not w.detached else st.magenta(br)
        # pad on plain widths first, then splice in colored branch — ANSI codes inflate len()
        prefix = "→ " if w.current else "  "
        row = (
            prefix
            + lbl.ljust(w_lbl)
            + "  "
            + mt.ljust(w_mt)
            + "  "
            + sha.ljust(w_sha)
            + "  "
            + branch_render
            + " " * (w_br - len(br) + 2)
            + sess
        )
        lines.append(st.bold(row) if w.current else row)
        if num > 0:
            # detail rows: timestamp under worktree mtime; size under sha → title aligns with branch
            indent = " " * (2 + w_lbl + 2)
            sizes = [fmt_size(s.size) for s in w.sessions[:num]]
            size_w = max([w_sha] + [len(x) for x in sizes]) if sizes else w_sha
            # +10 for the 8-char short uuid column + 2 spaces (see _title_line)
            extras_indent = indent + " " * (w_mt + 2 + size_w + 2 + 8 + 2)
            pr_indent = extras_indent  # PR sits under the title column, same x-offset as extras
            for s in w.sessions[:num]:
                lines.append(_title_line(s, indent, w_mt, size_w, st))
                loading = (
                    pending is not None
                    and s.pr_number is not None
                    and not s.pr_title
                )
                pr_text = _pr_line(s, pr_indent, st, loading=loading)
                if pr_text is not None:
                    lines.append(pr_text)
                    if loading:
                        pending.append(PRPending(
                            line_index=line_offset + len(lines) - 1,
                            session=s, pr_indent=pr_indent,
                        ))
                if extras:
                    # branch + which subdir the session was actually checked out in
                    branch_part = ""
                    if s.git_branch and s.git_branch != "HEAD":
                        branch_part = st.cyan(s.git_branch)
                    loc_part = ""
                    if s.last_cwd:
                        try:
                            cwd_r = Path(s.last_cwd).resolve()
                            wt_r = Path(w.path).resolve()
                            if cwd_r != wt_r:
                                try:
                                    rel = str(cwd_r.relative_to(wt_r))
                                except ValueError:
                                    rel = s.last_cwd
                                loc_part = st.dim(f"@ {rel}")
                        except OSError:
                            pass
                    if branch_part and loc_part:
                        lines.append(f"{extras_indent}{branch_part}  {loc_part}")
                    elif branch_part or loc_part:
                        lines.append(f"{extras_indent}{branch_part or loc_part}")
                    slug_uuid = f"{s.slug}  {s.uuid}" if s.slug else s.uuid
                    lines.append(f"{extras_indent}{st.dim(slug_uuid)}")
                    if s.last_prompt:
                        lp = s.last_prompt.replace("\n", " ").replace("\r", " ").strip()
                        if len(lp) > 100:
                            lp = lp[:97] + "…"
                        lines.append(st.dim(f"{extras_indent}↳ {lp}"))
    return lines


def render_human(
    repos: list[Repo], others: list[str], mode: str, target: str,
    num: int, extras: bool, st: Style, *,
    collect_pending: bool = False,
) -> tuple[str, list[PRPending]]:
    out: list[str] = []
    pending: list[PRPending] | None = [] if collect_pending else None
    for i, r in enumerate(repos):
        if i > 0:
            out.append("")
        out.extend(render_repo(r, num, extras, st, line_offset=len(out), pending=pending))
    if others:
        out.append("")
        out.append(st.dim("other entries:"))
        for n in others:
            out.append(f"  {n}")
    return "\n".join(out), (pending or [])


def progressive_fill_pr_titles(pending: list[PRPending], total_lines: int, st: Style) -> None:
    """Fetch PR titles in parallel; as each lands, patch its line in place via cursor escapes.

    Assumes the table was just printed and the cursor is at column 0 of the line below it.
    Returns when all fetches have completed.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    # group lines by (repo, num) so each PR is fetched once even if it appears on several lines
    by_pr: dict[tuple[str, int], list[PRPending]] = {}
    for p in pending:
        s = p.session
        if not (s.pr_repo and s.pr_number):
            continue
        if s.pr_url and "github.com" not in s.pr_url:
            continue
        by_pr.setdefault((s.pr_repo, s.pr_number), []).append(p)
    if not by_pr:
        return

    lock = threading.Lock()

    def patch(p: PRPending) -> None:
        new_line = _pr_line(p.session, p.pr_indent, st, loading=False) or ""
        up = total_lines - p.line_index  # cursor is `total_lines` lines below line_index 0
        # \033[<n>A  cursor up n lines (stays in same column → start of line, since we just \n'd)
        # \r          to column 0 (defensive)
        # \033[2K     clear entire line
        # write line
        # \033[<n>B   cursor back down to where we started
        sys.stdout.write(f"\r\033[{up}A\033[2K{new_line}\033[{up}B\r")
        sys.stdout.flush()

    def fetch_and_patch(repo: str, num: int) -> None:
        info = fetch_pr_info(repo, num)
        if not info:
            return
        with lock:
            for p in by_pr[(repo, num)]:
                _apply_pr_info(p.session, info)
                patch(p)

    with ThreadPoolExecutor(max_workers=min(10, len(by_pr))) as ex:
        futures = [ex.submit(fetch_and_patch, repo, num) for repo, num in by_pr]
        for fut in futures:
            fut.result()


def to_json(repos: list[Repo], others: list[str], mode: str, target: str) -> str:
    def repo_dict(r: Repo) -> dict:
        return {
            "name": r.name,
            "common_dir": r.common_dir,
            "worktrees": [
                {
                    "path": w.path,
                    "is_main": w.is_main,
                    "head": w.head,
                    "branch": w.branch,
                    "detached": w.detached,
                    "dir_mtime": w.dir_mtime,
                    "external": w.external,
                    "current": w.current,
                    "sessions": {
                        "count": len(w.sessions),
                        "latest_mtime": w.sessions[0].mtime if w.sessions else None,
                        "items": [asdict(s) for s in w.sessions],
                    },  # title/last_prompt/pr_* are populated only for sessions enriched via -n
                }
                for w in r.worktrees
            ],
        }

    doc = {
        "mode": mode,
        "target": target,
        "repos": [repo_dict(r) for r in repos],
        "other_entries": others,
    }
    return json.dumps(doc, indent=2)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="gwt-ls",
        description="List git worktrees plus their Claude Code sessions.",
    )
    ap.add_argument("path", nargs="?", default=".", help="folder or git working tree (default: PWD)")
    ap.add_argument(
        "-n", "--num",
        type=int, nargs="?", const=1, default=None, metavar="N",
        help="show detail lines for the N most recent sessions per worktree "
             "(default: 1, or 10 in -c mode; -n 0 to suppress)",
    )
    ap.add_argument("-a", "--all", action="store_true", help="show all sessions (overrides -n)")
    ap.add_argument("-c", "--claude", action="store_true",
                    help="latest Claude sessions across all worktrees, sorted by recency")
    ap.add_argument("--include-tmp", action="store_true",
                    help="in -c mode, also include sessions whose cwd is under /tmp")
    ap.add_argument("-x", "--extras", action="store_true",
                    help="add full session id and last user prompt below each detail line")
    ap.add_argument("--no-pr-titles", action="store_true",
                    help="skip fetching PR titles via `gh` (faster, offline-safe)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of human view")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI color")
    ap.add_argument("-r", "--resume", metavar="PREFIX",
                    help="resume a Claude session by short-UUID prefix (chdir to its workdir, "
                         "then exec `claude -r <uuid>`); pass PATH as the initial prompt")
    ap.add_argument("--new-tab", action="store_true",
                    help="with -r: spawn the resume in a detached gnome-terminal tab "
                         "(via $GLS_TAB_OPENER) instead of replacing the current shell")
    ap.add_argument("--dry-run", action="store_true",
                    help="with -r: print the chdir + exec command without running them")
    ap.add_argument("--live", action="store_true",
                    help="list currently running Claude sessions")
    ap.add_argument("--save-tabs", nargs="?", const=str(DEFAULT_TABS_PATH), metavar="PATH",
                    help="snapshot the live session set to PATH (default: ~/.claude/tabs.json)")
    ap.add_argument("--restore-tabs", nargs="?", const=str(DEFAULT_TABS_PATH), metavar="PATH",
                    help="re-spawn one detached tab per entry in PATH (default: ~/.claude/tabs.json)")
    args = ap.parse_args(argv)

    # -r is an action mode; it short-circuits everything else
    if args.resume:
        # treat the optional positional as the initial prompt
        prompt = args.path if args.path != "." else None
        return resume_claude(args.resume, prompt, dry_run=args.dry_run, new_tab=args.new_tab)

    # --save-tabs / --restore-tabs short-circuit too
    if args.save_tabs is not None:
        n = save_tabs(Path(args.save_tabs).expanduser())
        print(f"gls: saved {n} tab(s) to {args.save_tabs}", file=sys.stderr)
        return 0
    if args.restore_tabs is not None:
        n = restore_tabs(Path(args.restore_tabs).expanduser(), dry_run=args.dry_run)
        verb = "would spawn" if args.dry_run else "spawned"
        print(f"gls: {verb} {n} tab(s) from {args.restore_tabs}", file=sys.stderr)
        return 0
    # default N depends on mode: feed default = 10, normal default = 1
    if args.num is None:
        args.num = 10 if args.claude else 1
    if args.all:
        args.num = sys.maxsize

    # path resolution differs by mode: in -c mode "." means "no filter" (use as filter only
    # if the user explicitly typed a path); in normal mode it's the folder/repo to scan
    explicit_path = args.path != "."
    target = Path(args.path).expanduser().resolve()
    if explicit_path and not target.exists():
        print(f"gwt-ls: not found: {target}", file=sys.stderr)
        return 2

    use_color = (
        not args.no_color
        and not args.json
        and sys.stdout.isatty()
        and os.environ.get("NO_COLOR", "") == ""
    )
    st = Style(use_color)

    # ---- --live: currently running Claude sessions ---------------------------------------
    if args.live:
        sessions = live_sessions()
        progressive = (
            sessions and not args.no_pr_titles and not args.json and use_color
        )
        if sessions and not args.no_pr_titles and not progressive:
            fetch_pr_titles(sessions)
        if args.json:
            print(json.dumps({
                "mode": "live",
                "sessions": [asdict(s) for s in sessions],
            }, indent=2))
        else:
            text, pending = render_latest(
                sessions, len(sessions), args.extras, st,
                collect_pending=progressive,
            )
            total_lines = text.count("\n") + 1
            print(text, flush=True)
            if pending:
                progressive_fill_pr_titles(pending, total_lines, st)
        return 0

    # ---- -c / --claude: flat feed of the latest N sessions across all projects -----------
    if args.claude:
        filter_path = target if explicit_path else None
        sessions = discover_latest_sessions(
            n=args.num if args.num > 0 else 0,
            filter_path=filter_path,
            include_tmp=args.include_tmp,
        )
        progressive = (
            sessions and not args.no_pr_titles and not args.json and use_color
        )
        if sessions and not args.no_pr_titles and not progressive:
            fetch_pr_titles(sessions)
        if args.json:
            print(json.dumps({
                "mode": "claude",
                "filter_path": str(filter_path) if filter_path else None,
                "sessions": [asdict(s) for s in sessions],
            }, indent=2))
        else:
            text, pending = render_latest(
                sessions, args.num, args.extras, st,
                collect_pending=progressive,
            )
            total_lines = text.count("\n") + 1
            print(text, flush=True)
            if pending:
                progressive_fill_pr_titles(pending, total_lines, st)
        return 0

    # ---- default: repo / folder dispatch -------------------------------------------------
    if target.is_dir() and is_inside_worktree(str(target)):
        mode = "repo"
        worktrees = parse_worktrees(str(target))
        for w in worktrees:
            w.sessions = load_sessions(w.path)
        if not worktrees:
            print(f"gwt-ls: no worktrees found in {target}", file=sys.stderr)
            return 1
        name = Path(worktrees[0].path).name
        repos = [Repo(name=name, common_dir=common_dir_of(str(target)) or "", worktrees=worktrees)]
        others: list[str] = []
    else:
        mode = "folder"
        if not target.is_dir():
            print(f"gwt-ls: not a directory: {target}", file=sys.stderr)
            return 2
        repos, others = discover_repos_in_folder(target)
        # if the folder itself was the cwd of any Claude sessions, surface them at the top
        self_sessions = load_sessions(str(target))
        if self_sessions:
            try:
                self_mtime = target.stat().st_mtime
            except OSError:
                self_mtime = None
            self_wt = Worktree(
                path=str(target), head="", branch=None, detached=False,
                is_main=True, dir_mtime=self_mtime, external=False,
                sessions=self_sessions,
            )
            repos.insert(0, Repo(name=target.name, common_dir="", worktrees=[self_wt]))

    # enrich the sessions that will actually be shown in detail (whole-file scan per session)
    shown: list[Session] = []
    if args.num > 0:
        for r in repos:
            for w in r.worktrees:
                for s in w.sessions[: args.num]:
                    enrich_session(s, w.path)
                    shown.append(s)
    # PR titles: progressive fill on TTY (renders the table immediately, patches each
    # PR line in place as `gh` finishes); synchronous block when piped or --json
    progressive = (
        shown and not args.no_pr_titles and not args.json and use_color
    )
    if shown and not args.no_pr_titles and not progressive:
        fetch_pr_titles(shown)

    # mark the worktree containing cwd (most-specific match wins)
    cwd = Path.cwd().resolve()
    best: Worktree | None = None
    best_len = -1
    for r in repos:
        for w in r.worktrees:
            try:
                wpath = Path(w.path).resolve()
            except OSError:
                continue
            if cwd == wpath or cwd.is_relative_to(wpath):
                if len(str(wpath)) > best_len:
                    best, best_len = w, len(str(wpath))
    if best is not None:
        best.current = True

    if args.json:
        print(to_json(repos, others, mode, str(target)))
    else:
        text, pending = render_human(
            repos, others, mode, str(target),
            args.num, args.extras, st,
            collect_pending=progressive,
        )
        total_lines = text.count("\n") + 1
        print(text, flush=True)
        if pending:
            progressive_fill_pr_titles(pending, total_lines, st)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
