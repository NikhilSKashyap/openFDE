"""
openfde/instance_lock.py — one watcher per repo, enforced.

Two openfde processes on the same repo is the root of an observed incident class:
overlapping watchers during a restart window each captured the same prompt
(duplicate episode pairs with identical captureKeys), and earlier, two writers
interleaved on a shared tmp file and tore episodes.json. The graceful-shutdown lag
makes the overlap easy to hit: the old process is draining while the new one boots.

`acquire_watch_lock` is a pidfile with stale-detection: atomic O_EXCL create of
``.openfde/watch.lock`` holding our pid; an existing lock whose pid is dead is
swept and retried once; a live holder raises with a clear message. Pure stdlib,
no daemons, testable with fake pids.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("openfde.lock")

LOCK_NAME = "watch.lock"


class WatchLockHeld(RuntimeError):
    """Another live openfde watcher owns this repo."""

    def __init__(self, pid: int, lock_path: Path):
        self.pid, self.lock_path = pid, lock_path
        super().__init__(
            f"another openfde watch (pid {pid}) is already running for this repo — "
            f"stop it first, or remove {lock_path} if that pid is not openfde")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:               # exists, owned by someone else
        return True
    except OSError:
        return False


def acquire_watch_lock(openfde_dir, *, pid: int = None) -> Path:
    """Take the single-instance lock for this repo (or raise WatchLockHeld).

    Args:
        openfde_dir: Path — the repo's .openfde directory (created if needed).
        pid: optional pid override (tests).

    Returns:
        Path — the lock file path (pass to :func:`release_watch_lock`).

    Raises:
        WatchLockHeld — a live process already holds the lock.
    """
    openfde_dir = Path(openfde_dir)
    openfde_dir.mkdir(parents=True, exist_ok=True)
    lock = openfde_dir / LOCK_NAME
    me = pid if pid is not None else os.getpid()
    for attempt in (1, 2):                # second attempt after sweeping a stale lock
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(me))
            return lock
        except FileExistsError:
            try:
                holder = int((lock.read_text() or "0").strip() or "0")
            except (ValueError, OSError):
                holder = 0
            if holder != me and _pid_alive(holder):
                raise WatchLockHeld(holder, lock)
            # Stale (dead pid / unreadable) or our own leftover → sweep and retry.
            logger.info("Sweeping stale watch lock (pid %s)", holder or "?")
            try:
                lock.unlink()
            except OSError:
                pass
            if attempt == 2:
                raise WatchLockHeld(holder or -1, lock)
    return lock                            # unreachable; keeps type-checkers calm


def release_watch_lock(lock_path, *, pid: int = None) -> None:
    """Release the lock if WE hold it (a newer holder's file is left alone)."""
    lock = Path(lock_path)
    me = pid if pid is not None else os.getpid()
    try:
        holder = int((lock.read_text() or "0").strip() or "0")
        if holder == me:
            lock.unlink()
    except (ValueError, OSError):
        pass
