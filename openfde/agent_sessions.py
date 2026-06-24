"""
openfde/agent_sessions.py — managed agent sessions for the autonomous council relay.

OpenFDE OWNS these sessions: it launches them, sends each role its turn, captures stdout/stderr/
transcript under ``.openfde/council/runs/<runId>/``, and routes the reply to the next role. This is
NOT native UI injection — existing native Codex/Claude chats stay manual/self-orienting; this is a
separate, OpenFDE-managed runtime.

Adapters:
  - ``echo`` — deterministic; proves the relay spine and backs the tests.
  - ``codex`` — real, via ``codex exec`` (read-only sandbox, project-scoped) for the architect's
    plan/decision and the verifier's verdict.
  - ``claude-code`` — real, via ``claude -p`` (project-scoped) for the senior dev; text turns run
    read-only, the implement turn edits files (``acceptEdits``, Bash disallowed) only when opted in.
Honest availability: a missing or unauthenticated CLI makes that role's ``start()`` raise
:class:`AdapterUnavailable` with a precise reason — we never fake a real agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timezone

ADAPTER_UNAVAILABLE = "adapter_unavailable"

# Known Codex.app CLI location (the `codex` binary often is not on PATH but ships inside the app).
_CODEX_APP_CLI = "/Applications/Codex.app/Contents/Resources/codex"
_CODEX_TIMEOUT = 300        # seconds — read-only plan/verify turns are quick; cap runaway calls
_CLAUDE_TIMEOUT = 900       # seconds — an implement turn can edit several files


class SessionError(Exception):
    """Base error for managed agent sessions."""


class AdapterUnavailable(SessionError):
    """Raised when a provider's adapter cannot be driven autonomously — surfaced honestly, never faked."""

    def __init__(self, provider: str, role: str, reason: str):
        self.provider, self.role, self.reason = provider, role, reason
        super().__init__(f"{provider} adapter unavailable for {role}: {reason}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")


class AgentSession:
    """A managed agent session for one council role.

    Contract: ``start()`` makes the session callable (or raises :class:`AdapterUnavailable`);
    ``send(message, metadata)`` returns the agent's reply text; ``stop()`` releases it. Subclasses
    capture their I/O under ``run_dir`` so every turn is local and linked to the run.
    """

    role: str
    provider: str

    def __init__(self, role: str, provider: str, *, run_dir=None):
        self.role = role
        self.provider = provider
        self.run_dir = str(run_dir) if run_dir else None
        self.available = True
        self._started = False

    def start(self) -> None:  # pragma: no cover - overridden
        self._started = True

    def send(self, message: str, metadata: dict | None = None) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - overridden
        self._started = False

    def _capture(self, label: str, message: str, reply: str) -> None:
        """Append a turn to ``run_dir/<role>.log`` — keeps all session output local and linked."""
        if not self.run_dir:
            return
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            line = (f"\n=== {_now()} {self.provider}/{self.role} [{label}] ===\n"
                    f">> {message}\n<< {reply}\n")
            with open(os.path.join(self.run_dir, f"{self.role}.log"), "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # capture is best-effort; never break the relay on a log write


def _echo_sha(run_id: str, loop) -> str:
    """A deterministic 12-char 'commit' for the echo implementer — stable per (run, loop)."""
    return hashlib.sha1(f"{run_id}:{loop}".encode()).hexdigest()[:12]


def _default_echo(role: str, message: str, metadata: dict, n: int) -> str:
    """Deterministic, parseable per-(role, phase) reply that drives the relay without a real agent."""
    phase = (metadata or {}).get("phase", "")
    msg1 = _first_line(message)
    if phase == "plan":
        return ("PLAN: deliver the request as a few small modules.\n"
                f"- module: core — for: {msg1[:80]}\n"
                "- task: implement the core change\n"
                "- task: add one regression check\n"
                "acceptance: meets the stated request; checks pass.")
    if phase == "consult":
        return ("CONSULT (senior dev): the plan is reasonable. Push back — keep the surface small, "
                "add one regression check, and confirm error handling before implementing.")
    if phase == "decide":
        return ("DECISION: proceed — implement the core change plus one regression check; "
                "scope stays within the selected boxes.")
    if phase == "implement":
        sha = _echo_sha((metadata or {}).get("runId", ""), (metadata or {}).get("loop", 0))
        return f"IMPLEMENTED commit={sha} checks=echo-suite: ok (deterministic adapter)"
    if phase == "verify":
        return "VERIFIED: the commit meets the stated acceptance (deterministic adapter)."
    return f"ECHO[{role}/{phase}]: {msg1[:120]}"


class EchoSession(AgentSession):
    """Deterministic adapter — proves the relay spine and backs the tests.

    With no ``responses`` it returns a sensible per-(role, phase) reply (see :func:`_default_echo`).
    Pass ``responses`` to script a sequence (the last entry repeats) — e.g. a verifier that returns
    ``CHANGES_REQUESTED`` then ``VERIFIED`` to exercise the fix loop.
    """

    def __init__(self, role: str, *, run_dir=None, responses=None):
        super().__init__(role, "echo", run_dir=run_dir)
        self._responses = list(responses or [])
        self._sends = 0

    def start(self) -> None:
        self._started = True

    def send(self, message: str, metadata: dict | None = None) -> str:
        self._sends += 1
        if self._responses:
            reply = self._responses[min(self._sends - 1, len(self._responses) - 1)]
        else:
            reply = _default_echo(self.role, message, metadata or {}, self._sends)
        self._capture((metadata or {}).get("phase", "send"), message, reply)
        return reply

    def stop(self) -> None:
        self._started = False


class _UnavailableSession(AgentSession):
    """A real provider whose autonomous driving isn't wired yet — honest, never faked."""

    def __init__(self, role: str, provider: str, reason: str, *, run_dir=None):
        super().__init__(role, provider, run_dir=run_dir)
        self.available = False
        self.reason = reason

    def start(self) -> None:
        raise AdapterUnavailable(self.provider, self.role, self.reason)

    def send(self, message: str, metadata: dict | None = None) -> str:
        raise AdapterUnavailable(self.provider, self.role, self.reason)

    def stop(self) -> None:
        pass


# ── Real adapters — supported non-interactive CLI surfaces only (no native chat injection) ──────────
def _codex_cli() -> str | None:
    """The codex executable — PATH first, then the Codex.app bundle, or None."""
    return shutil.which("codex") or (_CODEX_APP_CLI if os.path.exists(_CODEX_APP_CLI) else None)


def codex_availability() -> tuple[bool, str]:
    cli = _codex_cli()
    if not cli:
        return False, f"codex CLI not found (looked on PATH and at {_CODEX_APP_CLI})"
    if not os.path.exists(os.path.expanduser("~/.codex/auth.json")):
        return False, f"codex CLI at {cli} is present but not authenticated (~/.codex/auth.json missing) — run `codex login`"
    return True, ""


def claude_code_availability() -> tuple[bool, str]:
    cli = shutil.which("claude")
    if not cli:
        return False, "claude CLI not found on PATH — install Claude Code to drive the senior-dev role"
    return True, ""


def _parse_claude_json(stdout: str) -> str:
    """Pull the assistant text out of `claude -p --output-format json`; fall back to raw stdout."""
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            return (obj.get("result") or obj.get("text") or "").strip() or stdout.strip()
    except (ValueError, TypeError):
        pass
    return (stdout or "").strip()


def _managed_prompt(run_id: str, message: str) -> str:
    """Prefix a managed-agent prompt with the OPENFDE_MANAGED_RUN_ID marker, so OpenFDE's passive
    prompt capture recognizes the subprocess as a council turn — not a human prompt — and never mints
    a standalone episode for it. The marker rides in the prompt text (the agent treats the bracketed
    line as metadata and proceeds with the task)."""
    return (f"[OPENFDE_MANAGED_RUN_ID:{run_id or 'unknown'} — OpenFDE-managed autonomous-council agent "
            f"call; not a user prompt. Proceed with the task below.]\n\n{message}")


def _managed_env(run_id: str) -> dict:
    """Subprocess env carrying OPENFDE_MANAGED_RUN_ID — belt-and-suspenders alongside the prompt marker."""
    return {**os.environ, "OPENFDE_MANAGED_RUN_ID": str(run_id or "")}


class CodexExecSession(AgentSession):
    """Codex driven via `codex exec` — read-only sandbox, project-scoped. Used for the architect's
    plan/decision and the verifier's verdict (Codex plans + verifies; it never edits or commits)."""

    def __init__(self, role: str, *, repo_root, run_dir=None, model=None, run_id=None):
        super().__init__(role, "codex", run_dir=run_dir)
        self.repo_root = str(repo_root)
        self.model = model
        self.run_id = run_id

    def start(self) -> None:
        ok, reason = codex_availability()
        if not ok:
            raise AdapterUnavailable("codex", self.role, reason)
        self._cli = _codex_cli()
        self._started = True

    def send(self, message: str, metadata: dict | None = None) -> str:
        cmd = [self._cli, "exec", "-s", "read-only", "--skip-git-repo-check", "-C", self.repo_root]
        if self.model:
            cmd += ["-m", self.model]
        run_id = self.run_id or (metadata or {}).get("runId")
        cmd.append(_managed_prompt(run_id, message))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=_CODEX_TIMEOUT,
                               env=_managed_env(run_id))
        except subprocess.TimeoutExpired as exc:
            raise AdapterUnavailable("codex", self.role, f"codex exec timed out after {_CODEX_TIMEOUT}s") from exc
        out = (r.stdout or "").strip()
        self._capture((metadata or {}).get("phase", "send"), message,
                      out + (f"\n[stderr] {r.stderr[:400]}" if r.returncode else ""))
        if r.returncode != 0 and not out:
            raise AdapterUnavailable("codex", self.role, f"codex exec failed (rc={r.returncode}): {(r.stderr or '')[:200]}")
        return out

    def stop(self) -> None:
        pass


class ClaudeCodeSession(AgentSession):
    """Claude Code driven via `claude -p` — project-scoped. Text roles run read-only (edit tools
    disallowed); the implement role runs with `--permission-mode acceptEdits` (file edits only, Bash
    disallowed) so the relay — not the model — makes the commit. Real edits are gated by ``allow_edits``."""

    def __init__(self, role: str, *, repo_root, run_dir=None, model=None, allow_edits=False, run_id=None):
        super().__init__(role, "claude-code", run_dir=run_dir)
        self.repo_root = str(repo_root)
        self.model = model
        self.allow_edits = bool(allow_edits)
        self.run_id = run_id

    def start(self) -> None:
        ok, reason = claude_code_availability()
        if not ok:
            raise AdapterUnavailable("claude-code", self.role, reason)
        self._cli = shutil.which("claude")
        self._started = True

    def edits_this_turn(self, metadata: dict | None) -> bool:
        return (metadata or {}).get("phase") == "implement" and self.allow_edits

    def send(self, message: str, metadata: dict | None = None) -> str:
        editing = self.edits_this_turn(metadata)
        run_id = self.run_id or (metadata or {}).get("runId")
        cmd = [self._cli, "-p", _managed_prompt(run_id, message), "--output-format", "json",
               "--add-dir", self.repo_root]
        if self.model:
            cmd += ["--model", self.model]
        if editing:
            cmd += ["--permission-mode", "acceptEdits",
                    "--allowedTools", "Read", "Edit", "Write",
                    "--disallowedTools", "Bash"]              # the RELAY commits, not the model
        else:
            cmd += ["--permission-mode", "plan",
                    "--disallowedTools", "Edit", "Write", "Bash"]   # read-only text turn
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=_CLAUDE_TIMEOUT,
                               cwd=self.repo_root, env=_managed_env(run_id))
        except subprocess.TimeoutExpired as exc:
            raise AdapterUnavailable("claude-code", self.role, f"claude -p timed out after {_CLAUDE_TIMEOUT}s") from exc
        text = _parse_claude_json(r.stdout)
        self._capture((metadata or {}).get("phase", "send"), message,
                      text + (f"\n[stderr] {r.stderr[:400]}" if r.returncode else ""))
        if not text and r.returncode != 0:
            raise AdapterUnavailable("claude-code", self.role, f"claude -p failed (rc={r.returncode}): {(r.stderr or '')[:200]}")
        return text

    def stop(self) -> None:
        pass


def build_session(role: str, provider: str, *, run_dir=None, responses=None,
                  repo_root=None, allow_edits=False, model=None, run_id=None) -> AgentSession:
    """Construct a managed session for ``role`` from a provider id.

    ``echo`` → a deterministic :class:`EchoSession`. ``codex`` → :class:`CodexExecSession`,
    ``claude-code`` → :class:`ClaudeCodeSession` (both real; ``start()`` raises
    :class:`AdapterUnavailable` with a precise reason if the CLI is missing/unauthed). Unknown
    providers are honestly unavailable, never silently echoed. ``run_id`` marks managed subprocess
    prompts so passive capture never mints a standalone episode for them.
    """
    p = (provider or "").strip().lower()
    if p == "echo":
        return EchoSession(role, run_dir=run_dir, responses=responses)
    if p in ("claude-code", "claude", "claude_code"):
        return ClaudeCodeSession(role, repo_root=repo_root, run_dir=run_dir, model=model,
                                 allow_edits=allow_edits, run_id=run_id)
    if p in ("codex", "codex-cli"):
        return CodexExecSession(role, repo_root=repo_root, run_dir=run_dir, model=model, run_id=run_id)
    return _UnavailableSession(role, p or "unknown", f"unknown provider {provider!r}", run_dir=run_dir)


def session_factory_for(repo_root, *, allow_edits=False, models=None, run_id=None):
    """A relay session factory bound to a repo — real providers drive `codex exec` / `claude -p`
    scoped to ``repo_root``; the implement turn edits only when ``allow_edits``. ``run_id`` tags every
    managed subprocess so passive capture folds them under the run instead of minting episodes."""
    models = models or {}

    def factory(role: str, provider: str, *, run_dir=None) -> AgentSession:
        return build_session(role, provider, run_dir=run_dir, repo_root=repo_root,
                             allow_edits=allow_edits, model=models.get(role), run_id=run_id)
    return factory


def default_session_factory(role: str, provider: str, *, run_dir=None) -> AgentSession:
    """Factory for echo-only / test use (no repo binding). Real providers still report availability
    via :func:`build_session`, but the relay should prefer :func:`session_factory_for` for real runs."""
    return build_session(role, provider, run_dir=run_dir)
