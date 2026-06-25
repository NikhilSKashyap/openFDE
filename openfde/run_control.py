"""Cross-thread cancellation + live-subprocess registry for managed council/program runs.

The relay runs synchronously in an executor thread while the server's cancel endpoint runs on the
event loop. This module is the thread-safe bridge between them:

  * a managed provider call registers its subprocess here for the duration of the call;
  * ``request_cancel(run_id)`` flips a flag AND kills the whole process group, so a hung
    ``claude -p`` / ``codex exec`` dies promptly instead of blocking until its wall-clock timeout;
  * ``run_managed`` polls both the cancel flag and a deadline, raising the distinct
    :class:`ProviderCancelled` / :class:`ProviderTimeout` so the relay can mark the run with the
    right terminal status (cancelled vs blocked_provider_timeout) and a precise, role-scoped reason.

Nothing here is faked: a real subprocess is spawned, bounded, and killable. The only thing this adds
over ``subprocess.run(timeout=…)`` is *external* cancellation — the missing piece that left a hung
provider call un-stoppable and the slice/run stuck ``running``.
"""

import os
import re
import signal
import subprocess
import threading
import time


class ProviderTimeout(Exception):
    """A managed provider call exceeded its wall-clock budget. Carries role/provider/phase so the
    relay can record exactly what timed out and surface a human turn in Orient."""

    def __init__(self, provider: str, role: str, phase: str, seconds: int):
        self.provider, self.role, self.phase, self.seconds = provider, role, phase, seconds
        super().__init__(f"{provider} timed out after {seconds}s during {phase}")


class ProviderCancelled(Exception):
    """A managed provider call was cancelled (user pressed cancel). Distinct from a timeout so the run
    ends ``cancelled``, not ``blocked``."""

    def __init__(self, provider: str, role: str, phase: str):
        self.provider, self.role, self.phase = provider, role, phase
        super().__init__(f"{provider} cancelled during {phase}")


class ProviderError(Exception):
    """A managed provider returned an obvious transport/runtime error (e.g. ``API Error: Overloaded``)
    instead of a real role response. Distinct terminal handling: the run blocks with the real reason and
    the error text is NEVER recorded as plan / consult / decision / verification content."""

    def __init__(self, provider: str, role: str, phase: str, summary: str):
        self.provider, self.role, self.phase, self.summary = provider, role, phase, summary
        super().__init__(f"{provider} returned a provider error during {phase}: {summary}")


# An *ambiguous* error word (overloaded / rate limit / permission denied) counts as a provider error
# only when it DOMINATES the response — the whole reply is this short, or the error LEADS it. A real
# plan/consult/verification that merely mentions rate limits or permissions is longer and leads with its
# own content (PLAN:/VERIFIED/…), so it is never flagged.
_SOFT_MAX_LEN = 30
_SOFT_LEAD = 16

# Hard markers: machine error tokens that never appear in a legitimate role response — match anywhere.
_HARD_ERROR_PATTERNS = (
    re.compile(r"\bAPI Error\s*:", re.I),
    re.compile(r"\badapter_unavailable\b", re.I),
    re.compile(r"\bprovider (?:unavailable|error)\b", re.I),
    re.compile(r"\bnot authenticated\b|\bauthentication (?:error|failed|required)\b|\bunauthenticated\b|"
               r"\binvalid api key\b|\blogin (?:required|expired)\b", re.I),
    re.compile(r"\b(?:401 unauthorized|403 forbidden|429 too many requests|"
               r"5\d\d (?:internal server error|bad gateway|service unavailable|gateway timeout))\b", re.I),
    re.compile(r'"is_error"\s*:\s*true', re.I),
    re.compile(r"\bexecution error\b", re.I),
)
# Soft markers: words that ALSO appear in legitimate prose — only a provider error when the response is
# short enough to BE the error (not a plan that merely discusses rate limits or permissions).
_SOFT_ERROR_PAT = re.compile(
    r"\boverloaded\b|\brate[\s-]?limit|\bpermission denied\b|\bquota (?:exceeded|exhausted)\b|"
    r"\bconnection (?:refused|reset|error)\b|\bservice unavailable\b|\btoo many requests\b",
    re.I)


def _first_line(text) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")


def classify_provider_error(text):
    """Return a short error summary if ``text`` is an obvious provider/runtime error (a transport
    failure returned where a real role response was expected), else ``None``. Conservative: a
    substantive, structured role response is never flagged — hard machine-error tokens match anywhere,
    but ambiguous words (overloaded / rate limit / permission denied) only count when the whole response
    is short. Empty/whitespace-only is always an error. No LLM."""
    s = (text or "").strip()
    if not s:
        return "empty response"
    first = _first_line(s)
    for pat in _HARD_ERROR_PATTERNS:
        if pat.search(s):
            return first[:160] or "provider error"
    m = _SOFT_ERROR_PAT.search(s)
    if m and (len(s) <= _SOFT_MAX_LEN or m.start() <= _SOFT_LEAD):
        return first[:160] or "provider error"
    return None


_lock = threading.Lock()
_cancelled: set[str] = set()           # run_ids a cancel has been requested for
_procs: dict[str, set] = {}            # run_id -> live managed subprocess handles
_POLL = 0.4                            # seconds between cancel/deadline checks


def request_cancel(run_id: str) -> int:
    """Flag ``run_id`` cancelled and kill any live managed subprocess for it. Returns the number of
    processes signalled (0 if nothing was in flight — the flag still sticks so an about-to-spawn call
    aborts immediately). Safe to call from the event-loop thread while the run advances elsewhere."""
    if not run_id:
        return 0
    with _lock:
        _cancelled.add(run_id)
        procs = list(_procs.get(run_id, ()))
    for p in procs:
        _kill_group(p)
    return len(procs)


def is_cancelled(run_id: str) -> bool:
    with _lock:
        return run_id in _cancelled


def reset(run_id: str) -> None:
    """Clear a run's cancel flag + process set (call when starting a fresh run with a reused id)."""
    if not run_id:
        return
    with _lock:
        _cancelled.discard(run_id)
        _procs.pop(run_id, None)


def active_runs() -> set:
    with _lock:
        return {rid for rid, procs in _procs.items() if procs}


def _register(run_id: str, proc) -> None:
    with _lock:
        _procs.setdefault(run_id, set()).add(proc)


def _unregister(run_id: str, proc) -> None:
    with _lock:
        s = _procs.get(run_id)
        if s:
            s.discard(proc)
            if not s:
                _procs.pop(run_id, None)


def _kill_group(proc) -> None:
    """SIGTERM then SIGKILL the subprocess's whole process group (it owns one via start_new_session),
    so a provider CLI that spawned children doesn't leak an orphan. Best-effort; never raises."""
    try:
        if proc.poll() is not None:
            return
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        _signal_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    except Exception:  # noqa: BLE001 - termination is best-effort
        pass


def _signal_group(proc, sig) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), sig)
        else:                                     # pragma: no cover - non-POSIX fallback
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _drain(stream, sink) -> None:
    """Read a pipe to EOF on a reader thread (no pipe-buffer deadlock); tolerate the stream being
    closed underneath us during teardown."""
    try:
        if stream is not None:
            sink.append(stream.read())
    except (ValueError, OSError):                  # stream closed mid-read during cleanup
        pass


def _close_streams(proc) -> None:
    """Close a managed subprocess's pipe handles after its output is drained, so they don't trip a
    ResourceWarning when the Popen is garbage-collected."""
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass


def run_managed(cmd, *, run_id, provider, role, phase, timeout,
                cwd=None, env=None, input_text=None) -> subprocess.CompletedProcess:
    """Run ``cmd`` as a managed provider subprocess that is BOTH wall-clock bounded and externally
    cancellable. Drains stdout/stderr on reader threads (no pipe-buffer deadlock), polls the cancel
    registry and the deadline, and kills the process group on either.

    Raises :class:`ProviderCancelled` if a cancel was requested, :class:`ProviderTimeout` if the
    deadline passed. Otherwise returns a ``CompletedProcess`` (returncode/stdout/stderr) — the caller
    still decides whether a non-zero return is a real failure.
    """
    if is_cancelled(run_id):                      # cancelled before we even spawned
        raise ProviderCancelled(provider, role, phase)
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, text=True,
        stdin=(subprocess.PIPE if input_text is not None else subprocess.DEVNULL),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,                   # own process group → _kill_group reaps the tree
    )
    _register(run_id, proc)
    out_chunks, err_chunks = [], []
    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()
    if input_text is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except Exception:  # noqa: BLE001 - a closed stdin is not fatal to the read path
            pass

    deadline = time.monotonic() + timeout
    outcome = "ok"
    try:
        while proc.poll() is None:
            if is_cancelled(run_id):
                outcome = "cancelled"
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                break
            time.sleep(_POLL)
        # request_cancel may have killed the process between polls — the loop then exits "normally"
        # on a dead process, so consult the flag once more before declaring success.
        if outcome == "ok" and is_cancelled(run_id):
            outcome = "cancelled"
        if outcome != "ok":
            _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        t_out.join(timeout=5)
        t_err.join(timeout=5)
    finally:
        _unregister(run_id, proc)
        _close_streams(proc)          # drained → close the pipes so the Popen leaks no handles

    if outcome == "cancelled":
        raise ProviderCancelled(provider, role, phase)
    if outcome == "timeout":
        raise ProviderTimeout(provider, role, phase, timeout)
    return subprocess.CompletedProcess(cmd, proc.returncode,
                                       "".join(c for c in out_chunks if c),
                                       "".join(c for c in err_chunks if c))
