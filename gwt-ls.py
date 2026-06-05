#!/usr/bin/env python3
"""gwt-ls — list git worktrees plus their Claude Code sessions.

Folder mode (default): scan direct children of PATH, group by owning repo.
Repo mode: when PATH (or PWD) is inside a git repo, list that repo's worktrees only.
"""
from __future__ import annotations

import argparse
import json
import os
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
    # Verified on e.g. /home/felix.dombek/.claude → -home-felix-dombek--claude.
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


def discover_latest_sessions(n: int, filter_path: Path | None) -> list[Session]:
    """Enumerate every JSONL under ~/.claude/projects, sort by mtime desc, return top N
    (optionally filtered to sessions whose recorded cwd is under `filter_path`)."""
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
    sessions: list[Session], sd: int, extras: bool, st: Style, *,
    collect_pending: bool = False,
) -> tuple[str, list[PRPending]]:
    out: list[str] = []
    pending: list[PRPending] | None = [] if collect_pending else None
    w_mt = 16   # "YYYY-MM-DD HH:MM"
    size_w = 6
    indent = "  "
    pr_indent = " " * (2 + w_mt + 2 + size_w + 2)
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
    """Returns (repos, other_entries). other_entries are basenames of non-git children."""
    groups: dict[str, list[str]] = {}
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
        if cd is None:
            others.append(name)
            continue
        groups.setdefault(cd, []).append(str(child.resolve()))

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
    meta = st.dim(f"{fmt_mtime(s.mtime):<{w_mt}}  {fmt_size(s.size):>{size_w}}")
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
    repo: Repo, sd: int, extras: bool, st: Style, *,
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
            sess_txt = base if sd > 0 else f"{base}, last {fmt_mtime(w.sessions[0].mtime)}"
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
        if sd > 0:
            # detail rows: timestamp under worktree mtime; size under sha → title aligns with branch
            indent = " " * (2 + w_lbl + 2)
            sizes = [fmt_size(s.size) for s in w.sessions[:sd]]
            size_w = max([w_sha] + [len(x) for x in sizes]) if sizes else w_sha
            extras_indent = indent + " " * (w_mt + 2 + size_w + 2)
            pr_indent = extras_indent  # PR sits under the title column, same x-offset as extras
            for s in w.sessions[:sd]:
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
    sd: int, extras: bool, st: Style, *,
    collect_pending: bool = False,
) -> tuple[str, list[PRPending]]:
    out: list[str] = []
    pending: list[PRPending] | None = [] if collect_pending else None
    for i, r in enumerate(repos):
        if i > 0:
            out.append("")
        out.extend(render_repo(r, sd, extras, st, line_offset=len(out), pending=pending))
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
                    },  # title/last_prompt/pr_* are populated only for sessions enriched via -sd
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
        "-s", "--session-details",
        type=int, nargs="?", const=1, default=None, metavar="N",
        help="show detail lines for the N most recent sessions per worktree "
             "(default: 1, or 10 in -c mode; -s 0 to suppress)",
    )
    ap.add_argument("-a", "--all", action="store_true", help="show all sessions (overrides -s)")
    ap.add_argument("-c", "--claude", action="store_true",
                    help="latest Claude sessions across all worktrees, sorted by recency")
    ap.add_argument("-x", "--extras", action="store_true",
                    help="add full session id and last user prompt below each detail line")
    ap.add_argument("--no-pr-titles", action="store_true",
                    help="skip fetching PR titles via `gh` (faster, offline-safe)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of human view")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI color")
    args = ap.parse_args(argv)
    # default N depends on mode: feed default = 10, normal default = 1
    if args.session_details is None:
        args.session_details = 10 if args.claude else 1
    if args.all:
        args.session_details = sys.maxsize

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

    # ---- -c / --claude: flat feed of the latest N sessions across all projects -----------
    if args.claude:
        filter_path = target if explicit_path else None
        sessions = discover_latest_sessions(
            n=args.session_details if args.session_details > 0 else 0,
            filter_path=filter_path,
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
                sessions, args.session_details, args.extras, st,
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
    if args.session_details > 0:
        for r in repos:
            for w in r.worktrees:
                for s in w.sessions[: args.session_details]:
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
            args.session_details, args.extras, st,
            collect_pending=progressive,
        )
        total_lines = text.count("\n") + 1
        print(text, flush=True)
        if pending:
            progressive_fill_pr_titles(pending, total_lines, st)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
