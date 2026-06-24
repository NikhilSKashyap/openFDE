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
    t_out = threading.Thread(target=lambda: out_chunks.append(proc.stdout.read() if proc.stdout else ""), daemon=True)
    t_err = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read() if proc.stderr else ""), daemon=True)
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
    finally:
        _unregister(run_id, proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    t_out.join(timeout=5)
    t_err.join(timeout=5)

    if outcome == "cancelled":
        raise ProviderCancelled(provider, role, phase)
    if outcome == "timeout":
        raise ProviderTimeout(provider, role, phase, timeout)
    return subprocess.CompletedProcess(cmd, proc.returncode,
                                       "".join(c for c in out_chunks if c),
                                       "".join(c for c in err_chunks if c))
