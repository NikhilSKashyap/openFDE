"""
openfde/fs_watch.py — "Watch Any Agent" (Land · Watch · Review).

A zero-dependency polling filesystem watcher. It detects external edits to repo
source files — from Cursor, Claude Code, Copilot, a terminal, or a human editor —
and broadcasts a ``file_activity`` WebSocket event so the canvas glows **live**, with
no OpenFDE council run involved. This is the "Watch" moment of the comprehension
spine: OpenFDE is the lens over *any* agent's work, not just its own.

Design (matches the approved plan):
  - poll source-file mtimes every ~1.2s (reusing ``semantic_graph._iter_files`` +
    ``_SKIP_DIRS`` for pruning); watchdog/inotify is a later upgrade;
  - the **baseline snapshot on startup emits nothing** (launching must not light the
    whole repo);
  - **ignore editor temp/backup/lock files** (``*.swp``, ``*~``, ``*.tmp``, ``.#*``,
    vim's ``4913`` probe) — atomic saves churn these;
  - **suppress while a council run is active** so the council's own glow isn't doubled;
  - cap fan-out per tick so a bulk checkout/format doesn't flood the canvas.

Broadcast shape (consumed by the frontend glow pipeline):
    {"type": "file_activity", "payload": {"file": "<repo-rel>", "action": "write", "ts": <float>}}
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from openfde.semantic_graph import _iter_files

logger = logging.getLogger("openfde.fs_watch")

# Source files worth watching (a superset of code — docs/config edits are activity too).
_WATCH_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
              ".css", ".html", ".json", ".md", ".yaml", ".yml", ".toml"}
# Editor temp / backup / lock churn to ignore (on top of semantic_graph._SKIP_DIRS).
_IGNORE_SUFFIXES = (".swp", ".swo", ".swn", ".tmp", "~")
_IGNORE_EXACT = {"4913"}              # vim's writability probe file

_DEFAULT_INTERVAL = 1.2
_MAX_FANOUT = 40                       # cap events per tick (bulk checkout/format)
_RESOLVE_MAX_TICK = 5                  # only infer the function for small (real-edit) ticks


def _ignored_name(name: str) -> bool:
    return (name.startswith(".#") or name in _IGNORE_EXACT
            or name.endswith(_IGNORE_SUFFIXES))


def _snapshot(root: Path) -> dict:
    """Map repo-relative? no — absolute path -> mtime for every watched source file."""
    snap = {}
    for f in _iter_files(root, _WATCH_EXT):
        if _ignored_name(f.name):
            continue
        try:
            snap[str(f)] = f.stat().st_mtime
        except OSError:
            pass
    return snap


async def watch_loop(root, manager, *, is_run_active=None,
                     interval: float = _DEFAULT_INTERVAL, on_event=None,
                     resolve_function=None) -> None:
    """Poll ``root`` for external source edits and broadcast ``file_activity``.

    Args:
        root: Path | str — repository root being watched.
        manager: ConnectionManager — WebSocket broadcaster (``await manager.broadcast``).
        is_run_active: callable() -> bool | None — when True, suppress broadcasts
            (a council run is glowing those files already).
        interval: float — seconds between polls.
        on_event: callable(rel) | None — test/observability hook per emitted file.
        resolve_function: async callable(rel) -> dict | None — optional per-file enrichment
            (e.g. ``{"function": "<name>"}``) merged into the payload so the glow can pulse the
            edited function, not just the file. Called only for small ticks (real edits, not
            bulk checkouts) and never allowed to break the loop.
    """
    root = Path(root)
    prev = _snapshot(root)             # baseline — emit nothing for what already exists
    logger.info("Watching filesystem for external edits (%d files)", len(prev))
    while True:
        try:
            await asyncio.sleep(interval)
            cur = _snapshot(root)
            # A council run is glowing its own files — don't double up. Re-baseline so
            # edits made during the run aren't replayed as a burst once it ends.
            if is_run_active and is_run_active():
                prev = cur
                continue
            changed = [p for p, m in cur.items() if prev.get(p) != m]
            prev = cur
            # Infer the touched function only on a small tick — a single coding edit, not a
            # bulk checkout/format (which we'd never want N function rings from anyway).
            do_resolve = resolve_function is not None and 0 < len(changed) <= _RESOLVE_MAX_TICK
            for p in changed[:_MAX_FANOUT]:
                rel = os.path.relpath(p, str(root))
                payload = {"file": rel, "action": "write", "ts": time.time()}
                if do_resolve:
                    try:
                        enrich = await resolve_function(rel)
                        if enrich:
                            payload.update(enrich)
                    except Exception:  # noqa: BLE001 — enrichment is best-effort
                        logger.debug("resolve_function failed for %s", rel, exc_info=True)
                try:
                    await manager.broadcast({"type": "file_activity", "payload": payload})
                except Exception:  # noqa: BLE001 — activity must never break the loop
                    logger.debug("file_activity broadcast failed for %s", rel, exc_info=True)
                if on_event:
                    try:
                        on_event(rel)
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad tick must not kill the watcher
            logger.debug("fs_watch tick failed", exc_info=True)


def detect_changes(prev: dict, cur: dict) -> list:
    """Pure helper (testable): repo files whose mtime changed or appeared."""
    return sorted(p for p, m in cur.items() if prev.get(p) != m)
