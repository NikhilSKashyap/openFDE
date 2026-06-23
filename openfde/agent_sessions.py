"""
openfde/agent_sessions.py — managed agent sessions for the autonomous council relay.

OpenFDE OWNS these sessions: it launches them, sends each role its turn, captures stdout/stderr/
transcript under ``.openfde/council/runs/<runId>/``, and routes the reply to the next role. This is
NOT native UI injection — existing native Codex/Claude chats stay manual/self-orienting; this is a
separate, OpenFDE-managed runtime.

v1 ships a deterministic ``echo`` adapter that proves the whole relay spine (and the tests). The real
``claude-code`` / ``codex`` adapters are declared honestly: the binary is detected, but until
autonomous CLI driving is wired and sandboxed they report ``adapter_unavailable`` from ``start()``.
We never fake a real agent — an unavailable adapter blocks the run with a clear reason.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
from datetime import datetime, timezone

ADAPTER_UNAVAILABLE = "adapter_unavailable"

# Known Codex.app CLI location (used only to report availability precisely — never auto-driven in v1).
_CODEX_APP_CLI = "/Applications/Codex.app/Contents/Resources/codex"
_V1_REASON = ("autonomous CLI driving is not enabled in v1 — use the echo adapter to prove the relay; "
              "real {provider} driving is the next slice")


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


def claude_code_availability() -> tuple[bool, str]:
    """Whether the claude-code adapter can be driven autonomously — honest. v1: not yet (deferred)."""
    if shutil.which("claude"):
        return False, "claude CLI found, but " + _V1_REASON.format(provider="claude-code")
    return False, "claude CLI not found; " + _V1_REASON.format(provider="claude-code")


def codex_availability() -> tuple[bool, str]:
    """Whether the codex adapter can be driven autonomously — honest. v1: not yet (deferred)."""
    if shutil.which("codex") or os.path.exists(_CODEX_APP_CLI):
        return False, "codex CLI found, but " + _V1_REASON.format(provider="codex")
    return False, "codex CLI not found; " + _V1_REASON.format(provider="codex")


def build_session(role: str, provider: str, *, run_dir=None, responses=None) -> AgentSession:
    """Construct a managed session for ``role`` from a provider id.

    ``echo`` → a deterministic :class:`EchoSession`. ``claude-code`` / ``codex`` → the real adapter if
    it can be driven, else an honest :class:`_UnavailableSession` (``start()`` raises
    :class:`AdapterUnavailable`). Unknown providers are unavailable, not silently echoed.
    """
    p = (provider or "").strip().lower()
    if p == "echo":
        return EchoSession(role, run_dir=run_dir, responses=responses)
    if p in ("claude-code", "claude", "claude_code"):
        ok, reason = claude_code_availability()
        if not ok:
            return _UnavailableSession(role, "claude-code", reason, run_dir=run_dir)
        return _UnavailableSession(role, "claude-code", reason, run_dir=run_dir)  # no real path enabled yet
    if p in ("codex", "codex-cli"):
        ok, reason = codex_availability()
        return _UnavailableSession(role, "codex", reason, run_dir=run_dir)
    return _UnavailableSession(role, p or "unknown", f"unknown provider {provider!r}", run_dir=run_dir)


def default_session_factory(role: str, provider: str, *, run_dir=None) -> AgentSession:
    """The relay's default factory — real providers via :func:`build_session` (honest availability)."""
    return build_session(role, provider, run_dir=run_dir)
