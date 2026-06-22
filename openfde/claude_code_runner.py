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
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from openfde.agent_runner import path_in_scope

logger = logging.getLogger("openfde.claude_code_runner")

_DEFAULT_TIMEOUT = 600
_DEFAULT_BUDGET_USD = 5.0
_GIT_TIMEOUT = 30
# Whitelisted to file work — Claude Code cannot run shell/network in this role.
# (Bash is disallowed, so the agent physically cannot run git; the directive
# below is belt-and-suspenders + makes the contract explicit in the transcript.)
_ALLOWED_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"]
_DISALLOWED_TOOLS = ["Bash", "WebFetch", "WebSearch"]

# Prepended to every Senior Dev prompt: OpenFDE owns commits, the agent only edits.
_NO_COMMIT_DIRECTIVE = (
    "IMPORTANT — OpenFDE owns version control. You only EDIT files. Do NOT run "
    "`git add`, `git commit`, `git push`, or stage/commit anything. Leave all "
    "changes in the working tree exactly as edited; OpenFDE reviews them and lands "
    "the commit. (Shell/git access is disabled in this role regardless.)\n\n"
)


def _norm(p: str) -> str:
    s = (p or "").strip().strip('"')
    return s[2:] if s.startswith("./") else s


def _is_openfde_owned(rel: str) -> bool:
    """True for OpenFDE's OWN metadata (``.openfde/…``). It is managed by OpenFDE,
    not the user, and churns during a run — the watcher re-assimilates the repo and
    rewrites ``semantic_graph.json`` while Senior Dev works. It is never user-authored
    work and never in a repair scope, so the scope/conflict guards must ignore it
    (otherwise an unrelated metadata write aborts the repair with a false data-loss
    conflict). Genuine user files outside scope keep their fail-safe."""
    n = _norm(rel)
    return n == ".openfde" or n.startswith(".openfde/")


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
    """Repo-relative paths that differ from HEAD or are untracked (the dirty set).

    OpenFDE-owned metadata under ``.openfde/`` is excluded: the watcher rewrites it
    (semantic_graph.json, episodes.json, …) throughout a run, so leaving it in would
    falsely trip the runner's scope/conflict guards on OpenFDE's own churn."""
    out = set()
    r1 = _git(["diff", "--name-only", "HEAD"], root)
    for ln in (r1.stdout or "").splitlines():
        if ln.strip():
            out.add(_norm(ln.strip()))
    r2 = _git(["ls-files", "--others", "--exclude-standard"], root)
    for ln in (r2.stdout or "").splitlines():
        if ln.strip():
            out.add(_norm(ln.strip()))
    return {p for p in out if not _is_openfde_owned(p)}


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


# ── Live progress streaming (stream-json) ──────────────────────────────────── #
# Map Claude Code tool-use events to the same read/write activity the in-process
# Anthropic runner emits, so the canvas glow follows the local CLI agent too.
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_READ_TOOLS = {"Read"}
_STREAM_POLL = 0.4  # seconds between cancel/timeout checks while reading stdout


def _rel_to_root(file_path, root: Path):
    """Normalize a tool's absolute file_path to a repo-relative path, or None if
    it is empty or outside the repo. Uses realpath on both sides so macOS symlinks
    (e.g. /tmp → /private/tmp) reconcile and new (not-yet-created) files still map."""
    if not file_path or not isinstance(file_path, str):
        return None
    p = file_path.strip().strip('"')
    if not p:
        return None
    ap = p if os.path.isabs(p) else os.path.join(str(root), p)
    try:
        rel = os.path.relpath(os.path.realpath(ap), os.path.realpath(str(root)))
    except (OSError, ValueError):
        return None
    if rel == "." or rel.startswith(".."):
        return None
    return _norm(rel)


def _parse_event(line: str):
    """Parse one stream-json line into a dict, or None if it is not JSON."""
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return ev if isinstance(ev, dict) else None


def _emit_tools(ev: dict, root: Path, on_read, on_write) -> None:
    """Translate an `assistant` event's tool_use blocks into on_read/on_write
    activity callbacks (best-effort — a callback must never break the run)."""
    msg = ev.get("message") or {}
    for b in (msg.get("content") or []):
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        rel = _rel_to_root((b.get("input") or {}).get("file_path"), root)
        if not rel:
            continue
        name = b.get("name")
        cb = on_write if name in _WRITE_TOOLS else on_read if name in _READ_TOOLS else None
        if not cb:
            continue
        try:
            cb(rel)
        except Exception:  # noqa: BLE001 — progress must never break the run
            logger.debug("progress callback raised for %s", rel, exc_info=True)


def _stream_claude(proc, root: Path, timeout: int, should_cancel, on_read, on_write):
    """Consume `claude --output-format stream-json` line by line.

    Reads stdout (and drains stderr) on daemon threads so the main loop can poll
    ``should_cancel`` and the wall-clock ``timeout`` between lines; emits live
    read/write activity as tool_use events arrive; captures the final `result`
    event as the CLI envelope. Kills the process on cancel/timeout (cancellation
    via Popen is preserved). Returns (cli_envelope, stderr_text, error)."""
    out_q: "queue.Queue" = queue.Queue()
    stderr_buf = []

    def _read_stdout():
        try:
            for line in proc.stdout:
                out_q.put(line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            out_q.put(None)  # EOF sentinel

    def _read_stderr():
        try:
            for line in proc.stderr:
                stderr_buf.append(line)
        except Exception:  # noqa: BLE001
            pass

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    cli, error = {}, None
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        if should_cancel and should_cancel():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            line = out_q.get(timeout=min(_STREAM_POLL, remaining))
        except queue.Empty:
            continue
        if line is None:
            break  # stdout EOF
        line = line.strip()
        if not line:
            continue
        ev = _parse_event(line)
        if not ev:
            continue
        etype = ev.get("type")
        if etype == "assistant":
            _emit_tools(ev, root, on_read, on_write)
        elif etype == "result":
            cli = ev

    if timed_out or (should_cancel and should_cancel()):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    t_err.join(timeout=2)
    stderr = "".join(stderr_buf)
    if timed_out:
        error = f"Claude Code timed out after {timeout}s."
        logger.error("Claude Code run timed out (%ss)", timeout)
    elif not cli and proc.returncode not in (0, None):
        error = f"claude exited {proc.returncode}: {stderr.strip()[:300]}"
    return cli, stderr, error


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
                    claude_bin=None, should_cancel=None, on_proc=None,
                    on_write=None, on_read=None, stream=True,
                    allow_dirty=False) -> dict:
    """Drive the Claude Code CLI as Senior Dev; return a run_agent-shaped outcome.

    Args:
        repo_root: Path | str — repository root (cwd + write-enforcement boundary).
        prompt: str — the compiled implementation prompt (the Architect brief + scope).
        editable: list[str] — editable in-scope paths (writes allowed).
        protected: list[str] — protected paths (force needs_approval).
        model: str | None — model alias/id for `--model` (default 'sonnet').
        allow_dirty: bool — permit edits to IN-SCOPE files that already carry
            uncommitted changes (the repair-hatch case: a failing file in an
            episode under review is dirty by definition). Such edits stack on
            the user's uncommitted state and count as writes — review-then-land
            still governs them. Out-of-scope dirty files keep the fail-safe.
        timeout: int — wall-clock seconds before the CLI run is aborted.
        max_budget_usd: float — hard `--max-budget-usd` spend cap.
        claude_bin: str | None — explicit binary path (default: search PATH).
        on_write: callable(rel) | None — live callback as the agent edits a file.
        on_read: callable(rel) | None — live callback as the agent reads a file.
        stream: bool — use `--output-format stream-json` for live per-file progress
            (default). False uses the single-result `json` format (no live glow).

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
    # invoked, so the user's work is left exactly as it was. The repair hatch
    # opts out (allow_dirty): there the dirty in-scope file IS the subject of
    # the run, and its edits land as reviewable writes on the episode.
    dirty_in_scope = sorted(p for p in pre_dirty if path_in_scope(p, editable_set))
    if dirty_in_scope and not allow_dirty:
        return _failed_outcome(
            "Cannot run on file(s) with uncommitted changes in scope — commit, "
            f"stash, or review first: {', '.join(dirty_in_scope)}.")

    # stream-json gives live per-file progress (the canvas glow follows the agent);
    # json is the single-result fallback (no live glow) when stream=False.
    out_fmt = (["--output-format", "stream-json", "--verbose"] if stream
               else ["--output-format", "json"])
    cmd = [
        claude_bin, "-p", _NO_COMMIT_DIRECTIVE + prompt,
        *out_fmt,
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

    if stream:
        # Read the event stream live: emit read/write glow + capture the result
        # event. Scope is still enforced from the git dirty-set below, so even if
        # stream parsing yields nothing the run stays correct (only glow is lost).
        cli, stderr, error = _stream_claude(proc, root, timeout, should_cancel, on_read, on_write)
    else:
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
        cli = _parse_cli_json(stdout)

    # Cancelled mid-run? The cancel path terminated the process. Land nothing —
    # any partial in-scope edits are left uncommitted for the user to review.
    if should_cancel and should_cancel():
        logger.info("Claude Code Sr Dev: cancelled by user")
        return _failed_outcome("Cancelled by user.")

    summary_text = (cli.get("result") or "").strip()
    cost = cli.get("total_cost_usd")
    if cli.get("is_error") and not error:
        error = summary_text[:300] or "Claude Code reported an error."

    # ── Enforce scope, but ONLY on files the Claude run actually changed. ──────
    # Pre-vs-post CONTENT is the truth, not dirty-set membership: a repair that
    # returns a dirty file to its HEAD state leaves the dirty set entirely, and
    # a run that reverts the user's dirty file to HEAD must be caught, not missed.
    post_changed = set(_dirty_set(root))
    for rel in pre_dirty:
        if _hash(root, rel) != pre_hashes.get(rel):
            post_changed.add(rel)
    writes, protected_attempts, rejected, conflicts = [], [], [], []
    for rel in post_changed:
        if rel in pre_dirty and _hash(root, rel) == pre_hashes.get(rel):
            continue  # pre-existing user change Claude did not touch — preserve it
        if rel in pre_dirty:
            if allow_dirty and path_in_scope(rel, editable_set):
                writes.append(rel)   # the requested repair, stacked on the user's
                continue             # uncommitted state — review-then-land applies
            conflicts.append(rel)  # Claude modified an already-dirty file — never revert
            continue
        # Newly changed by the Claude run → enforce dotted/solid (and workspace) scope.
        if path_in_scope(rel, protected_set):
            protected_attempts.append(rel)
            _revert(root, rel)
            rejected.append({"path": rel, "reason": "protected"})
        elif path_in_scope(rel, editable_set):
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


def run_claude_code_cli(*, repo_root, prompt, model=None, timeout=_DEFAULT_TIMEOUT,
                        max_budget_usd=_DEFAULT_BUDGET_USD, claude_bin=None) -> dict:
    """Run Claude Code **repo-wide** as an OpenFDE prompt-capture wrapper (`openfde cc`).

    Unlike :func:`run_claude_code` (council Senior Dev, scoped to canvas-selected
    editable files), this runs across the whole repo: the agent EDITS files, and the
    no-git directive + the Bash-disallowed tool set keep it from committing. Touched
    files are derived from the git dirty-set diff (before vs after), so OpenFDE can
    review and land them. Never commits.

    Args:
        repo_root: Path | str — repository root (cwd + edit boundary).
        prompt: str — the user's prompt (the no-commit directive is prepended).
        model: str | None — model alias/id (default 'sonnet').
        timeout: int — wall-clock seconds.
        max_budget_usd: float — spend cap (no-op on subscription auth).
        claude_bin: str | None — explicit binary (default: search PATH).

    Returns:
        dict — {ok: bool, touched: [str], error: str|None, summary: str, costUsd: float}.
    """
    root = Path(repo_root)
    claude_bin = claude_bin or shutil.which("claude")
    if not claude_bin:
        return {"ok": False, "touched": [], "summary": "", "costUsd": 0.0,
                "error": "Claude Code CLI not found on PATH. Install Claude Code."}

    pre = _dirty_set(root)
    pre_hashes = {p: _hash(root, p) for p in pre}

    cmd = [
        claude_bin, "-p", _NO_COMMIT_DIRECTIVE + (prompt or ""),
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--max-budget-usd", str(max_budget_usd),
        "--allowedTools", ",".join(_ALLOWED_TOOLS),
        "--disallowedTools", ",".join(_DISALLOWED_TOOLS),
        "--model", (model or "sonnet"),
    ]
    error, summary_text, cost = None, "", None
    try:
        proc = subprocess.run(cmd, cwd=str(root), shell=False, capture_output=True,
                              text=True, timeout=timeout, env=_child_env())
        cli = _parse_cli_json(proc.stdout)
        summary_text = (cli.get("result") or "").strip() if isinstance(cli, dict) else ""
        cost = cli.get("total_cost_usd") if isinstance(cli, dict) else None
        if proc.returncode != 0 and not summary_text:
            error = f"claude exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
    except subprocess.TimeoutExpired:
        error = f"Claude Code timed out after {timeout}s."
        logger.error("openfde cc: timed out (%ss)", timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"Claude Code failed to launch: {exc}"
        logger.error("openfde cc: launch failed: %s", exc)

    post = _dirty_set(root)
    touched = sorted(p for p in post if p not in pre or _hash(root, p) != pre_hashes.get(p))
    return {"ok": error is None, "touched": touched, "summary": summary_text,
            "costUsd": cost if isinstance(cost, (int, float)) else 0.0, "error": error}


def _build_contract(writes, protected_attempts, rejected, error, summary_text, cost) -> dict:
    """Honest, fail-clear Step-20 contract. No silent success: a run with no
    in-scope diff is reported as failed with an explicit reason. Cost stays a
    DATA field (costUsd on the outcome) and never enters display strings —
    OpenFDE doesn't put a meter in front of the work."""
    reject_reasons = [f"{r['path']}: {r['reason']}" for r in (rejected or [])]
    errors = []

    if protected_attempts:
        status = "needs_approval"
        report = (f"Claude Code requested changes to protected file(s): "
                  f"{', '.join(protected_attempts)}.")
    elif error and not writes:
        status = "failed"
        report = f"Claude Code did not complete: {error}"
        errors.append(error)
    elif not writes:
        status = "failed"
        if rejected:
            report = (f"Claude Code ran but only touched out-of-scope files "
                      f"(reverted): {', '.join(reject_reasons)}.")
        else:
            report = "Claude Code ran but produced no in-scope changes — no diff."
    else:
        status = "passed"
        head = summary_text.splitlines()[0] if summary_text else f"Edited {', '.join(writes)}."
        report = head[:240]

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
