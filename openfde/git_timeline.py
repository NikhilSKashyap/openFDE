"""
openfde/git_timeline.py — local git history service for the watched repo (Step 18).

Backs OpenFDE's Timeline with the watched repo's real git history and lets
OpenFDE work units create local commits. All git calls go through subprocess
with an argument list (never a shell string). This module:

  - never adds remotes,
  - never pushes,
  - never checks out old commits (playback is story-only, not time travel),
  - never stages OpenFDE-internal state (.openfde/) or common build dirs.

Step 19 will reuse `git_commit()` after real agent work units.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("openfde.git_timeline")

# Unit separator used to delimit fields in git --pretty output.
_US = "\x1f"

# Paths never committed by OpenFDE (also written into .gitignore).
_IGNORE_ENTRIES = (".openfde/", "node_modules/", "dist/", "__pycache__/")

_MAX_PATCH_CHARS = 20_000
_GIT_TIMEOUT = 20
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

# Local identity used for OpenFDE commits when the repo has none configured.
_GIT_NAME = "OpenFDE"
_GIT_EMAIL = "openfde@localhost"


# ─── Subprocess helper ────────────────────────────────────────────────────── #

def _run(args: list, cwd: Path, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a git command with an argument list (no shell).

    Args:
        args: list[str] — the full command, e.g. ["git", "status", "--porcelain"].
        cwd: Path — working directory the command runs in.
        timeout: int — seconds before the call is aborted.

    Returns:
        subprocess.CompletedProcess — completed process with text stdout/stderr;
        on failure to even launch, a synthetic result with returncode 1.
    """
    try:
        return subprocess.run(
            args, cwd=str(cwd), shell=False,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("git command failed (%s): %s", " ".join(args[:3]), exc)
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(exc))


def _valid_sha(sha: str) -> bool:
    """Return True if a string is a plausible git object id (4–40 hex chars).

    Args:
        sha: str — candidate commit id.

    Returns:
        bool — True when safe to pass to git as a revision.
    """
    return bool(sha) and bool(_SHA_RE.match(sha))


# ─── Repo lifecycle ───────────────────────────────────────────────────────── #

def is_git_repo(root: Path) -> bool:
    """Return whether `root` is inside a git work tree.

    Args:
        root: Path — repository root to test.

    Returns:
        bool — True if a git work tree is present.
    """
    res = _run(["git", "rev-parse", "--is-inside-work-tree"], root)
    return res.returncode == 0 and res.stdout.strip() == "true"


def _ensure_gitignore(root: Path) -> None:
    """Ensure `.gitignore` excludes OpenFDE-internal and build directories.

    Args:
        root: Path — repository root.

    Returns:
        None
    """
    path = root / ".gitignore"
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [e for e in _IGNORE_ENTRIES if e not in present]
    if not missing:
        return
    block = ("" if existing.endswith("\n") or existing == "" else "\n")
    block += "\n# OpenFDE internal state\n" + "\n".join(missing) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
        logger.info(".gitignore updated (%d entries)", len(missing))
    except OSError as exc:
        logger.error("Failed to update .gitignore: %s", exc)


def ensure_git_repo(root: Path) -> dict:
    """Initialize a local git repo if absent and ensure exclusions exist.

    Safe: only `git init` + local identity config + `.gitignore`. No remotes,
    no push, no checkout.

    Args:
        root: Path — repository root.

    Returns:
        dict — {"git": bool, "initialized": bool}.
    """
    initialized = False
    if not is_git_repo(root):
        res = _run(["git", "init"], root)
        if res.returncode != 0:
            logger.error("git init failed: %s", res.stderr.strip())
            return {"git": False, "initialized": False}
        _run(["git", "config", "user.name", _GIT_NAME], root)
        _run(["git", "config", "user.email", _GIT_EMAIL], root)
        initialized = True
        logger.info("Initialized local git repo at %s", root)
    _ensure_gitignore(root)
    return {"git": is_git_repo(root), "initialized": initialized}


def ensure_baseline(root: Path, summary: str = "openfde: baseline before workflow") -> dict:
    """Ensure the watched repo is a git repo with at least one commit.

    Used at workflow-prepare time so later result intake can diff reported source
    files against a real tree. Creates the repo (and `.gitignore`, which keeps
    `.openfde/` out of commits) when absent, and — only when there is no commit
    history yet — makes a baseline commit. Never commits over existing history:
    if HEAD already exists this only ensures repo/.gitignore setup.

    Args:
        root: Path — repository root.
        summary: str — commit subject used for the baseline commit.

    Returns:
        dict — {"git": bool, "baselineCreated": bool, "head": str|None}.
    """
    ensure_git_repo(root)
    head = _run(["git", "rev-parse", "--verify", "HEAD"], root)
    if head.returncode == 0 and head.stdout.strip():
        return {"git": True, "baselineCreated": False, "head": head.stdout.strip()}

    commit = git_commit(root, summary)
    new_head = _run(["git", "rev-parse", "--verify", "HEAD"], root)
    return {
        "git": is_git_repo(root),
        "baselineCreated": bool(commit.get("committed")),
        "head": new_head.stdout.strip() if new_head.returncode == 0 else None,
    }


# ─── Status / timeline / diff ─────────────────────────────────────────────── #

def git_status(root: Path) -> dict:
    """Report repo git status: branch, head, dirty and staged files.

    Args:
        root: Path — repository root.

    Returns:
        dict — {"git": bool, "branch": str|None, "head": str|None,
                "shortHead": str|None, "dirty": list[str], "staged": list[str]}.
    """
    if not is_git_repo(root):
        return {"git": False, "branch": None, "head": None, "shortHead": None, "dirty": [], "staged": []}

    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip() or None
    head_res = _run(["git", "rev-parse", "HEAD"], root)
    head = head_res.stdout.strip() if head_res.returncode == 0 else None

    dirty, staged = [], []
    porc = _run(["git", "status", "--porcelain"], root)
    for line in porc.stdout.splitlines():
        if len(line) < 3:
            continue
        x, y, path = line[0], line[1], line[3:]
        if x != " " and x != "?":
            staged.append(path)
        if y != " " or x == "?":
            dirty.append(path)
    return {
        "git": True, "branch": branch, "head": head,
        "shortHead": head[:7] if head else None,
        "dirty": dirty, "staged": staged,
    }


def changed_paths(root: Path, candidates: list) -> list:
    """Return the subset of ``candidates`` git reports as changed in the work tree.

    Used to gate commit eligibility: only paths that are actually modified, added,
    deleted, renamed, or untracked count. Matching is by repo-relative path
    (porcelain reports the same form a workflow reports for filesChanged).

    Args:
        root: Path — repository root.
        candidates: list[str] — repo-relative paths to test.

    Returns:
        list[str] — candidates (original spelling) that git reports as changed.
    """
    if not candidates or not is_git_repo(root):
        return []
    res = _run(["git", "status", "--porcelain", "--untracked-files=all"], root)
    if res.returncode != 0:
        return []

    def _norm(p: str) -> str:
        s = str(p or "").strip().strip('"')
        return s[2:] if s.startswith("./") else s

    changed = set()
    for line in res.stdout.splitlines():
        if len(line) < 4:
            continue
        seg = line[3:]
        if " -> " in seg:               # rename: "old -> new"
            seg = seg.split(" -> ", 1)[1]
        changed.add(_norm(seg))
    return [c for c in candidates if _norm(c) in changed]


def git_timeline(root: Path, limit: int = 100) -> list:
    """Return commit history newest-first in a frontend-friendly shape.

    Args:
        root: Path — repository root.
        limit: int — maximum number of commits to return.

    Returns:
        list[dict] — each: {"sha", "shortSha", "author", "email",
                            "timestamp" (ISO-8601), "summary"}.
    """
    if not is_git_repo(root):
        return []
    fmt = _US.join(["%H", "%h", "%an", "%ae", "%aI", "%s"])
    res = _run(["git", "log", f"--max-count={int(limit)}", f"--pretty=format:{fmt}"], root)
    if res.returncode != 0:
        return []
    commits = []
    for line in res.stdout.splitlines():
        parts = line.split(_US)
        if len(parts) != 6:
            continue
        sha, short, author, email, ts, summary = parts
        commits.append({
            "sha": sha, "shortSha": short, "author": author,
            "email": email, "timestamp": ts, "summary": summary,
        })
    return commits


def git_diff(root: Path, sha: str, max_patch: int = _MAX_PATCH_CHARS) -> Optional[dict]:
    """Return commit metadata, changed files, stat summary, and a capped patch.

    Args:
        root: Path — repository root.
        sha: str — commit id (validated against a hex pattern).
        max_patch: int — maximum characters of patch text to return.

    Returns:
        dict | None — {"sha","shortSha","author","email","timestamp","summary",
                       "files":[{"path","status","additions","deletions"}],
                       "stat":{"files","additions","deletions"},
                       "patch":str, "patchTruncated":bool}; None if not found.
    """
    if not is_git_repo(root) or not _valid_sha(sha):
        return None

    fmt = _US.join(["%H", "%h", "%an", "%ae", "%aI", "%s"])
    meta = _run(["git", "show", "-s", f"--format={fmt}", sha, "--"], root)
    if meta.returncode != 0 or not meta.stdout.strip():
        return None
    m = meta.stdout.strip().split(_US)
    if len(m) != 6:
        return None
    full, short, author, email, ts, summary = m

    # Per-file numstat (additions / deletions / path)
    files_by_path: dict = {}
    nums = _run(["git", "show", "--numstat", "--format=", sha, "--"], root)
    add_total = del_total = 0
    for line in nums.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) != 3:
            continue
        a, d, p = cols
        adds = 0 if a == "-" else int(a or 0)
        dels = 0 if d == "-" else int(d or 0)
        add_total += adds
        del_total += dels
        files_by_path[p] = {"path": p, "status": "M", "additions": adds, "deletions": dels}

    # Name-status (A/M/D/R…) merged onto numstat entries
    nst = _run(["git", "show", "--name-status", "--format=", sha, "--"], root)
    for line in nst.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        status = cols[0][0]
        path = cols[-1]
        if path in files_by_path:
            files_by_path[path]["status"] = status
        else:
            files_by_path[path] = {"path": path, "status": status, "additions": 0, "deletions": 0}

    patch_res = _run(["git", "show", "--format=", "--patch", "--no-color", sha, "--"], root)
    patch = patch_res.stdout
    truncated = len(patch) > max_patch
    if truncated:
        patch = patch[:max_patch] + "\n… [diff truncated]"

    return {
        "sha": full, "shortSha": short, "author": author, "email": email,
        "timestamp": ts, "summary": summary,
        "files": list(files_by_path.values()),
        "stat": {"files": len(files_by_path), "additions": add_total, "deletions": del_total},
        "patch": patch, "patchTruncated": truncated,
    }


# ─── Commit ───────────────────────────────────────────────────────────────── #

def git_commit(root: Path, summary: str, detail: str = "", trailers: dict = None) -> dict:
    """Stage meaningful repo files and create a commit if anything changed.

    Initializes the repo if needed. Staging respects `.gitignore` (so
    `.openfde/` and build dirs are excluded). Commits only when the staged set
    is non-empty.

    Args:
        root: Path — repository root.
        summary: str — commit subject line.
        detail: str — optional commit body.
        trailers: dict | None — optional key→value trailers appended to the body
                  (e.g. {"OpenFDE-Run": "run_abc"}).

    Returns:
        dict — {"committed": bool, "sha": str|None, "shortSha": str|None,
                "summary": str, "files": list[str], "reason": str|None}.
    """
    ensure_git_repo(root)

    add = _run(["git", "add", "-A"], root)
    if add.returncode != 0:
        return {"committed": False, "sha": None, "shortSha": None, "summary": summary, "files": [], "reason": "stage failed"}

    staged = [p for p in _run(["git", "diff", "--cached", "--name-only"], root).stdout.splitlines() if p]
    if not staged:
        return {"committed": False, "sha": None, "shortSha": None, "summary": summary, "files": [], "reason": "no meaningful changes"}

    body = detail or ""
    if trailers:
        trailer_lines = "\n".join(f"{k}: {v}" for k, v in trailers.items() if v)
        body = (body + "\n\n" + trailer_lines).strip() if body else trailer_lines

    args = ["git", "-c", f"user.name={_GIT_NAME}", "-c", f"user.email={_GIT_EMAIL}",
            "commit", "-m", summary]
    if body:
        args += ["-m", body]
    res = _run(args, root)
    if res.returncode != 0:
        logger.error("git commit failed: %s", res.stderr.strip())
        return {"committed": False, "sha": None, "shortSha": None, "summary": summary, "files": staged, "reason": "commit failed"}

    sha = _run(["git", "rev-parse", "HEAD"], root).stdout.strip()
    logger.info("Committed %s (%d file(s)): %s", sha[:7], len(staged), summary)
    return {"committed": True, "sha": sha, "shortSha": sha[:7], "summary": summary, "files": staged, "reason": None}
