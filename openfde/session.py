"""
openfde/session.py — authoritative watched-repo identity (runtime, not user metadata).

A server's identity is the repo it was started on, resolved CANONICALLY (git root /
realpath), NEVER ``.openfde/project.json.name`` (that is user-editable metadata). Exposed
at ``GET /api/session`` so the UI shows the right repo from the first frame, and used by
``openfde watch`` to refuse a port already held by a DIFFERENT repo instead of silently
serving the wrong one.

Pure + dependency-light (no aiohttp), so it is unit-testable on its own.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def _git(args, cwd) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def session_payload(repo_root, started_at: str, version: str) -> dict:
    """Canonical runtime identity for the watched repo.

    Returns:
        dict — {repoRoot, repoName, gitRoot, branch, startedAt, openfdeVersion}. repoName
        is the canonical repo basename (git root if any, else the resolved dir) — never
        project metadata. gitRoot/branch are None when the path is not a git work tree.
    """
    p = Path(repo_root)
    try:
        real = os.path.realpath(str(p))
    except (OSError, ValueError):
        real = str(p)
    top = _git(["rev-parse", "--show-toplevel"], real)
    git_root = os.path.realpath(top) if top else None
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], real) or None
    return {
        "repoRoot": str(p),
        "repoName": Path(git_root or real).name,
        "gitRoot": git_root,
        "branch": branch,
        "startedAt": started_at,
        "openfdeVersion": version,
    }


def probe_openfde_repo(port: int):
    """The repo a server already on ``port`` is watching (its canonical-ish root + name),
    or None when the port is free / not an OpenFDE server.

    Tries the new ``/api/session`` first; falls back to ``/api/files`` (its root node's
    path) so a server started BEFORE /api/session existed is still identified — which is
    exactly the wrong-repo-already-on-the-port case we must catch.
    """
    def _get(suffix, timeout=5):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{suffix}", timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    s = _get("/api/session")
    if isinstance(s, dict) and (s.get("gitRoot") or s.get("repoRoot")):
        return {"root": s.get("gitRoot") or s.get("repoRoot"), "name": s.get("repoName")}
    # Pre-/api/session OpenFDE server: /api/files proves it's OpenFDE, but its root node
    # path is RELATIVE (".") and can't be compared canonically — flag unknown identity so
    # the verdict refuses conservatively (never compare a relative path, never guess).
    f = _get("/api/files")
    if isinstance(f, dict) and f.get("name"):
        return {"root": None, "name": f.get("name"), "unknownIdentity": True}
    return None


def port_collision_verdict(existing, repo_root) -> tuple:
    """Decide what ``openfde watch`` should do about a port that may already be held.

    Args:
        existing: dict | None — probe_openfde_repo() result for the requested port.
        repo_root: the repo we want to watch.

    Returns:
        (verdict, detail) — verdict is 'free' (port open → bind), 'already_running' (the
        SAME canonical repo already serves it → detail is its name) or 'wrong_repo' (a
        DIFFERENT repo holds it → detail is that repo's root; refuse loudly).
    """
    if not existing:
        return ("free", "")
    from openfde.prompt_capture import same_repo
    other = existing.get("root")
    if other and same_repo(other, repo_root):
        return ("already_running", existing.get("name") or Path(str(repo_root)).name)
    if existing.get("unknownIdentity"):
        # An OpenFDE server we can't canonically identify (started before /api/session).
        # Refuse rather than risk serving the wrong repo; restart it to get exact identity.
        nm = existing.get("name") or "another repo"
        return ("wrong_repo", f"an older OpenFDE server (repo '{nm}'; restart it to confirm)")
    return ("wrong_repo", other or "another repo")
