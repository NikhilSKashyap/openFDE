"""
openfde/claude_code_runner.py — drive the Claude Code CLI as the Senior Dev (Step 31).

OpenFDE shells out to the local ``claude`` CLI in headless print mode to implement
the scoped change, then enforces dotted/solid scope on the resulting worktree
diff. It returns the SAME outcome shape as ``agent_runner.run_agent`` so the
council loop, the diff-driven Verifier, and the gated reconcile/commit path all
work unchanged — only the Senior Dev implementation step is swapped.

Trust boundary (same deliberate inversion as agent_runner, contained here):
  - Claude Code can edit anything in the working tree, so scope is enforced
    POST-HOC, but ONLY on files the Claude run itself changed:
      * before invoking Claude we snapshot the pre-run dirty state (paths +
        content hashes);
      * after Claude returns we compare — pre-existing user changes Claude did
        not touch are NEVER reverted;
      * newly-created out-of-scope edits are reverted; protected-file edits are
        reverted AND force ``needs_approval``;
      * if Claude edits a file that was ALREADY dirty before the run, we fail
        safely (no merge, no revert — the user's work is left intact);
      * if an in-scope file is already dirty before the run, we refuse to run at
        all and tell the user to commit/stash/review first.
  - the run is bounded by a wall-clock ``timeout`` and a hard ``--max-budget-usd``
    spend cap (no MiHoYo-style overnight runaway);
  - no secrets pass through: the CLI uses the user's existing Claude Code login.

Outcome shape (matches run_agent):
  {result, writes, rejected, protectedAttempts, stdout, stderr, error, touched, costUsd}
where ``result`` is a Step-20 contract accepted by workflow_result.validate_result.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("openfde.claude_code_runner")

_DEFAULT_TIMEOUT = 600
_DEFAULT_BUDGET_USD = 5.0
_GIT_TIMEOUT = 30
# Whitelisted to file work — Claude Code cannot run shell/network in this role.
_ALLOWED_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"]
_DISALLOWED_TOOLS = ["Bash", "WebFetch", "WebSearch"]


def _norm(p: str) -> str:
    s = (p or "").strip().strip('"')
    return s[2:] if s.startswith("./") else s


def _child_env() -> dict:
    """Environment for the spawned `claude` process. Strips ANTHROPIC_API_KEY /
    AUTH_TOKEN so the CLI uses the user's Claude Code LOGIN (the whole point —
    "no API key"), instead of silently billing a key inherited from the parent
    environment."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def cli_available(claude_bin: str = None) -> bool:
    """Return whether the Claude Code CLI is resolvable on PATH.

    Args:
        claude_bin: str | None — explicit binary path, or None to search PATH.

    Returns:
        bool — True if the `claude` binary can be found.
    """
    return bool(claude_bin or shutil.which("claude"))


def _git(args: list, root: Path, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(root), shell=False,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(exc))


def _dirty_set(root: Path) -> set:
    """Repo-relative paths that differ from HEAD or are untracked (the dirty set)."""
    out = set()
    r1 = _git(["diff", "--name-only", "HEAD"], root)
    for ln in (r1.stdout or "").splitlines():
        if ln.strip():
            out.add(_norm(ln.strip()))
    r2 = _git(["ls-files", "--others", "--exclude-standard"], root)
    for ln in (r2.stdout or "").splitlines():
        if ln.strip():
            out.add(_norm(ln.strip()))
    return out


def _hash(root: Path, rel: str):
    """Content hash of a working-tree file, or None if it cannot be read."""
    try:
        return hashlib.sha1((root / rel).read_bytes()).hexdigest()
    except OSError:
        return None


def _revert(root: Path, rel: str) -> None:
    """Undo a Claude-introduced out-of-scope/protected change: restore tracked
    files from HEAD, delete untracked ones. Only ever called on files the Claude
    run itself created/modified. Best-effort; never raises."""
    tracked = _git(["ls-files", "--error-unmatch", rel], root).returncode == 0
    if tracked:
        _git(["checkout", "HEAD", "--", rel], root)
    else:
        try:
            (root / rel).unlink()
        except OSError:
            pass


def _parse_cli_json(stdout: str) -> dict:
    """Best-effort parse of `claude -p --output-format json` (single result obj)."""
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for ev in reversed(data):
            if isinstance(ev, dict) and ev.get("type") == "result":
                return ev
    return {}


# Text roles (Architect/Verifier) never edit — pure completion, no tools.
_TEXT_DISALLOWED = ["Edit", "Write", "MultiEdit", "Bash", "WebFetch", "WebSearch", "Task", "TodoWrite"]


def run_claude_code_text(*, system, user, model=None, timeout=180,
                         max_budget_usd=_DEFAULT_BUDGET_USD, claude_bin=None, cwd=None) -> str:
    """Drive `claude -p` as a pure TEXT role (Architect/Verifier).

    system + user in → the model's text out. No file edits (editing tools are
    disallowed); uses the user's Claude Code login, so no API key. Returns the
    text, or "" on failure so the caller can fall back to the deterministic role.

    Args:
        system: str — the role's system prompt (replaces the default via --system-prompt).
        user: str — the user message (intent + scope, or brief + diff).
        model: str | None — model alias/id (default 'sonnet').
        timeout: int — wall-clock seconds.
        max_budget_usd: float — hard spend cap (no-op on subscription auth).
        claude_bin: str | None — explicit binary (default: search PATH).
        cwd: Path | str | None — working dir (repo root, for context).

    Returns:
        str — the model's text response, or "" on any failure.
    """
    claude_bin = claude_bin or shutil.which("claude")
    if not claude_bin:
        return ""
    cmd = [
        claude_bin, "-p", user,
        "--system-prompt", system,
        "--output-format", "json",
        "--max-budget-usd", str(max_budget_usd),
        "--disallowedTools", ",".join(_TEXT_DISALLOWED),
        "--model", (model or "sonnet"),
    ]
    try:
        proc = subprocess.run(cmd, cwd=(str(cwd) if cwd else None), shell=False,
                              capture_output=True, text=True, timeout=timeout, env=_child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Claude Code text role failed to run: %s", exc)
        return ""
    cli = _parse_cli_json(proc.stdout or "")
    if cli.get("is_error"):
        logger.error("Claude Code text role error: %s", (cli.get("result") or "")[:200])
        return ""
    return (cli.get("result") or "").strip()


def run_claude_code(*, repo_root, prompt, editable, protected, model=None,
                    timeout=_DEFAULT_TIMEOUT, max_budget_usd=_DEFAULT_BUDGET_USD,
                    claude_bin=None, should_cancel=None, on_proc=None) -> dict:
    """Drive the Claude Code CLI as Senior Dev; return a run_agent-shaped outcome.

    Args:
        repo_root: Path | str — repository root (cwd + write-enforcement boundary).
        prompt: str — the compiled implementation prompt (the Architect brief + scope).
        editable: list[str] — editable in-scope paths (writes allowed).
        protected: list[str] — protected paths (force needs_approval).
        model: str | None — model alias/id for `--model` (default 'sonnet').
        timeout: int — wall-clock seconds before the CLI run is aborted.
        max_budget_usd: float — hard `--max-budget-usd` spend cap.
        claude_bin: str | None — explicit binary path (default: search PATH).

    Returns:
        dict — {result, writes, rejected, protectedAttempts, stdout, stderr,
                error, touched, costUsd}.
    """
    root = Path(repo_root)
    editable_set = {_norm(p) for p in (editable or [])}
    protected_set = {_norm(p) for p in (protected or [])}
    claude_bin = claude_bin or shutil.which("claude")
    if not claude_bin:
        return _failed_outcome(
            "Claude Code CLI not found on PATH. Install Claude Code or pick another "
            "Senior Dev provider (Anthropic API or Echo).")

    # ── Snapshot pre-run dirty state — we must never disturb the user's work. ──
    pre_dirty = _dirty_set(root)
    pre_hashes = {p: _hash(root, p) for p in pre_dirty}

    # Refuse to run if an in-scope file is already dirty: we could not separate
    # Claude's edit from the user's uncommitted work afterwards. Claude is never
    # invoked, so the user's work is left exactly as it was.
    dirty_in_scope = sorted(pre_dirty & editable_set)
    if dirty_in_scope:
        return _failed_outcome(
            "Cannot run on file(s) with uncommitted changes in scope — commit, "
            f"stash, or review first: {', '.join(dirty_in_scope)}.")

    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--max-budget-usd", str(max_budget_usd),
        "--allowedTools", ",".join(_ALLOWED_TOOLS),
        "--disallowedTools", ",".join(_DISALLOWED_TOOLS),
        "--model", (model or "sonnet"),
    ]

    stdout, stderr, error = "", "", None
    try:
        # Popen (not run) so the cancel path can terminate the process.
        proc = subprocess.Popen(cmd, cwd=str(root), shell=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=_child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Claude Code failed to launch: %s", exc)
        return _failed_outcome(f"Claude Code failed to launch: {exc}")
    if on_proc:
        try:
            on_proc(proc)
        except Exception:  # noqa: BLE001
            pass
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        if proc.returncode != 0 and not (stdout or "").strip():
            error = f"claude exited {proc.returncode}: {(stderr or '').strip()[:300]}"
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            stdout, stderr = "", ""
        error = f"Claude Code timed out after {timeout}s."
        logger.error("Claude Code run timed out (%ss)", timeout)

    # Cancelled mid-run? The cancel path terminated the process. Land nothing —
    # any partial in-scope edits are left uncommitted for the user to review.
    if should_cancel and should_cancel():
        logger.info("Claude Code Sr Dev: cancelled by user")
        return _failed_outcome("Cancelled by user.")

    cli = _parse_cli_json(stdout)
    summary_text = (cli.get("result") or "").strip()
    cost = cli.get("total_cost_usd")
    if cli.get("is_error") and not error:
        error = summary_text[:300] or "Claude Code reported an error."

    # ── Enforce scope, but ONLY on files the Claude run actually changed. ──────
    writes, protected_attempts, rejected, conflicts = [], [], [], []
    for rel in _dirty_set(root):
        if rel in pre_dirty and _hash(root, rel) == pre_hashes.get(rel):
            continue  # pre-existing user change Claude did not touch — preserve it
        if rel in pre_dirty:
            conflicts.append(rel)  # Claude modified an already-dirty file — never revert
            continue
        # Newly changed by the Claude run → enforce dotted/solid scope.
        if rel in protected_set:
            protected_attempts.append(rel)
            _revert(root, rel)
            rejected.append({"path": rel, "reason": "protected"})
        elif rel in editable_set:
            writes.append(rel)
        else:
            _revert(root, rel)
            rejected.append({"path": rel, "reason": "out-of-scope"})

    if conflicts:
        # Claude touched files that were dirty before the run. Leave them exactly
        # as they are (no revert, no merge) and fail clearly.
        out = _failed_outcome(
            "Claude Code modified file(s) that already had uncommitted changes; left "
            "untouched to avoid data loss — commit/stash/review first: "
            f"{', '.join(sorted(conflicts))}.")
        out["touched"] = sorted(conflicts)
        out["costUsd"] = cost
        out["stdout"] = summary_text[:2000]
        return out

    result = _build_contract(writes, protected_attempts, rejected, error, summary_text, cost)
    logger.info("Claude Code Sr Dev: status=%s writes=%s rejected=%d cost=%s",
                result["status"], writes, len(rejected), cost)
    return {
        "result": result, "writes": writes, "rejected": rejected,
        "protectedAttempts": protected_attempts, "stdout": summary_text[:2000],
        "stderr": stderr[:1000], "error": error,
        "touched": sorted({*writes, *(r["path"] for r in rejected)}),
        "costUsd": cost,
    }


def _cost_note(cost) -> str:
    return f" (cost ${cost:.2f})" if isinstance(cost, (int, float)) else ""


def _build_contract(writes, protected_attempts, rejected, error, summary_text, cost) -> dict:
    """Honest, fail-clear Step-20 contract. No silent success: a run with no
    in-scope diff is reported as failed with an explicit reason."""
    reject_reasons = [f"{r['path']}: {r['reason']}" for r in (rejected or [])]
    errors = []
    note = _cost_note(cost)

    if protected_attempts:
        status = "needs_approval"
        report = (f"Claude Code requested changes to protected file(s): "
                  f"{', '.join(protected_attempts)}.{note}")
    elif error and not writes:
        status = "failed"
        report = f"Claude Code did not complete: {error}{note}"
        errors.append(error)
    elif not writes:
        status = "failed"
        if rejected:
            report = (f"Claude Code ran but only touched out-of-scope files "
                      f"(reverted): {', '.join(reject_reasons)}.{note}")
        else:
            report = f"Claude Code ran but produced no in-scope changes — no diff.{note}"
    else:
        status = "passed"
        head = summary_text.splitlines()[0] if summary_text else f"Edited {', '.join(writes)}."
        report = head[:240] + note

    errors.extend(reject_reasons)
    return {
        "status": status,
        "reportSummary": report,
        "filesChanged": [{"path": p, "status": "M"} for p in writes],
        "functionsChanged": [],
        "testsRun": [],
        "verificationResult": "",
        "suggestedCanvasUpdates": [],
        "errors": errors,
    }


def _failed_outcome(msg: str) -> dict:
    return {
        "result": {
            "status": "failed", "reportSummary": msg, "filesChanged": [],
            "functionsChanged": [], "testsRun": [], "verificationResult": "",
            "suggestedCanvasUpdates": [], "errors": [msg],
        },
        "writes": [], "rejected": [], "protectedAttempts": [],
        "stdout": "", "stderr": "", "error": msg, "touched": [], "costUsd": None,
    }
