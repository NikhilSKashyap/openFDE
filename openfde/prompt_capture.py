"""
openfde/prompt_capture.py — Passive Prompt Capture v1 (Claude Code).

    Git tells us what landed. OpenFDE should tell us why.

Tails the Claude Code session transcripts for the watched repo and turns each new
human prompt into a durable OpenFDE **episode** — *no `openfde cc` wrapper needed*.
You just talk to Claude Code from the repo and the prompt shows up on the OpenArchitect
rail; edits it made stay in the work tree for OpenFDE to Review and Land.

Scope of v1 (honest):
  - **Claude Code only.** Codex's on-disk session format differs — a follow-up.
  - **Sessions whose working directory IS the watched repo.** Claude Code stores a
    transcript dir per cwd (``~/.claude/projects/<encoded-cwd>/``); we read the dir
    for this repo. A session launched from a *different* cwd (even with this repo
    added via ``--add-dir``) lands in that other cwd's dir and is not captured here.
  - **Capture-forward.** We baseline to end-of-file on startup, so we never replay a
    repo's entire prompt history (that's the future, confidence-tagged *import*).
  - **Heuristic file attribution.** A prompt's touched files = the work-tree changes
    that appeared *after* the prompt (baseline diff), attributed to the newest open
    capture episode. Good enough to review + land; not a guarantee.

Transcript entry shape (Claude Code JSONL): one JSON object per line; a human prompt
is ``{"type":"user","message":{"role":"user","content": <str|[{type:text}]>},
"uuid":…,"sessionId":…,"cwd":…,"timestamp":…}``. Tool results, slash-command echoes,
local-command output, meta, and sidechain entries are skipped.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("openfde.prompt_capture")

_DEFAULT_INTERVAL = 2.0
_GIT_TIMEOUT = 10

# Text prefixes that mark a non-prompt user entry (slash-command plumbing, local
# command output, caveats, hook injections) — never captured as a prompt.
_SKIP_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-stdout>", "<local-command-stderr>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "<user-prompt-submit-hook>", "Caveat:", "[Request interrupted",
    # OpenFDE's own machine-injected directive (prepended by the cc/codex wrappers) —
    # it is not a human prompt and must never become a chip.
    "IMPORTANT — OpenFDE owns version control",
    # OpenFDE's own internal LLM summarizer calls (it shells out to the local CLI) —
    # never capture our own machine prompts as episodes.
    "[OpenFDE internal summarizer]",
)

# A prompt containing this marker anywhere is an internal summarizer call — dropped.
_INTERNAL_MARKER = "[OpenFDE internal summarizer]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_repo_dir(repo_root) -> str:
    """Encode a repo path the way Claude Code names its transcript directory:
    every non-alphanumeric character becomes ``-`` (e.g. ``/Users/x/openfde`` →
    ``-Users-x-openfde``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(repo_root))


def claude_projects_root(home=None) -> Path:
    """The root holding all Claude Code transcript directories (one per session cwd)."""
    base = Path(home) if home else Path.home()
    return base / ".claude" / "projects"


def claude_projects_dir(repo_root, home=None) -> Path:
    """The Claude Code transcript directory for sessions whose cwd is ``repo_root``."""
    return claude_projects_root(home) / encode_repo_dir(repo_root)


def edit_files_under(entry: dict, repo_root) -> list:
    """Repo-relative paths an entry's Edit/Write/MultiEdit tool calls touch under
    ``repo_root`` (empty if none) — the cwd-agnostic signal that links a session to a
    repo: a prompt belongs to the repo whose files it edits, wherever it's rooted."""
    msg = entry.get("message") if isinstance(entry, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    rr = str(Path(repo_root))
    out = []
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"
                and b.get("name") in ("Edit", "Write", "MultiEdit")):
            continue
        fp = (b.get("input") or {}).get("file_path") or ""
        if not fp:
            continue
        try:
            rel = os.path.relpath(fp, rr)
        except (ValueError, TypeError):
            continue
        if not rel.startswith("..") and not os.path.isabs(rel):
            out.append(rel)
    return out


def prompt_text(entry: dict) -> str:
    """Extract the human text from a transcript user entry (str or text blocks)."""
    msg = entry.get("message") if isinstance(entry, dict) else None
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            x.get("text", "") for x in c
            if isinstance(x, dict) and x.get("type") == "text"
        )
    return ""


def is_human_prompt(entry: dict) -> bool:
    """True when a transcript entry is a real human prompt (not tool/meta/command)."""
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return False
    if entry.get("isMeta") or entry.get("isSidechain"):
        return False
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    c = msg.get("content")
    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        return False
    txt = prompt_text(entry).strip()
    if not txt or txt.startswith(_SKIP_PREFIXES):
        return False
    if _INTERNAL_MARKER in txt:                     # OpenFDE's own summarizer prompt
        return False
    return True


def read_new_lines(path: Path, start_offset: int):
    """Read a transcript from ``start_offset`` bytes, returning parsed entries.

    Pure + testable. Only consumes COMPLETE lines (ending in newline), so an entry
    still mid-write is left for the next poll. Returns ``(entries, new_offset)``.
    """
    entries = []
    consumed = start_offset
    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            data = fh.read()
    except OSError:
        return entries, start_offset
    while True:
        nl = data.find(b"\n")
        if nl == -1:
            break                          # incomplete trailing line → stop here
        line = data[:nl + 1]
        data = data[nl + 1:]
        consumed += len(line)
        s = line.decode("utf-8", "replace").strip()
        if not s:
            continue
        try:
            entries.append(json.loads(s))
        except (json.JSONDecodeError, ValueError):
            continue
    return entries, consumed


def _prompt_record(entry: dict) -> dict:
    return {
        "key": f"{entry.get('sessionId')}:{entry.get('uuid')}",
        "text": prompt_text(entry).strip(),
        "sessionId": entry.get("sessionId"),
        "uuid": entry.get("uuid"),
        "timestamp": entry.get("timestamp"),
        "cwd": entry.get("cwd"),
    }


def read_new_prompts(path: Path, start_offset: int):
    """Read a transcript from ``start_offset``, returning new human prompts only.
    ``(prompts, new_offset)`` — each prompt is ``{key, text, sessionId, uuid, …}``."""
    entries, off = read_new_lines(path, start_offset)
    return [_prompt_record(e) for e in entries if is_human_prompt(e)], off


def make_capture_episode(repo_root, prompt: dict, files=None, status="open") -> dict:
    """Build a capture episode (Prompt Story Rail shape) from a parsed prompt."""
    now = _now()
    files = sorted(files or [])
    return {
        "episodeId": "episode_" + secrets.token_hex(6),
        "createdAt": prompt.get("timestamp") or now, "updatedAt": now,
        "prompt": prompt.get("text", ""), "kind": "claude-code",
        "status": ("reviewing" if files else status),
        "runIds": [], "eventIds": [], "projectEntryIds": [], "commitShas": [],
        "files": files, "summary": "", "source": "openfde-capture",
        "initialHead": _head(repo_root), "captureKey": prompt.get("key"),
        "sessionId": prompt.get("sessionId"), "sessionCwd": prompt.get("cwd"),
    }


def _git(args, root, timeout=_GIT_TIMEOUT):
    try:
        return subprocess.run(["git", *args], cwd=str(root), shell=False,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")


def _dirty_set(root: Path) -> set:
    """Repo-relative paths that differ from HEAD or are untracked."""
    out = set()
    for arg in (["diff", "--name-only", "HEAD"], ["ls-files", "--others", "--exclude-standard"]):
        r = _git(arg, root)
        for ln in (r.stdout or "").splitlines():
            if ln.strip():
                out.add(ln.strip())
    return out


def _head(root):
    r = _git(["rev-parse", "HEAD"], root)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


async def _safe_broadcast(manager, msg):
    try:
        await manager.broadcast(msg)
    except Exception:  # noqa: BLE001 — capture must never break on a bad socket
        logger.debug("episode broadcast failed", exc_info=True)


async def watch_loop(repo_root, persistence, manager, *, interval=_DEFAULT_INTERVAL,
                     home=None, on_episode=None, quiet_window=12.0, autoland=True) -> None:
    """Poll **all** Claude Code transcripts and capture prompts relevant to this repo.

    A prompt belongs to the watched repo when EITHER the session's cwd is the repo
    (captured immediately) OR the session edits files under the repo (captured when
    the first such edit lands) — so capture is **cwd-agnostic**: a session rooted
    elsewhere but editing this repo (e.g. opened on another folder with this one added)
    is still captured. Capture-forward (baselined at startup); deduped by transcript
    uuid; broadcasts ``episode_updated`` for live rail updates.

    Args:
        repo_root: Path | str — watched repository root (resolved).
        persistence: Persistence — episode store.
        manager: ConnectionManager — WebSocket broadcaster (live rail updates).
        interval: float — poll seconds.
        home: Path | str | None — override HOME (tests).
        on_episode: callable(episode) | None — test/observability hook.
    """
    root = Path(repo_root)
    repo_str = str(root)
    proot = claude_projects_root(home)

    def _transcripts():
        return sorted(proot.glob("*/*.jsonl")) if proot.exists() else []

    # Capture-forward: baseline every existing transcript to its end.
    offsets = {}
    for f in _transcripts():
        try:
            offsets[str(f)] = f.stat().st_size
        except OSError:
            pass
    known = {e.get("captureKey") for e in persistence.load_episodes() if e.get("captureKey")}
    pending = {}       # sessionId → last human prompt not yet captured for this repo
    baselines = {}     # episodeId → dirty set at capture time (in-memory)
    last_change = {}   # episodeId → monotonic time of its last file-set change (quiet timer)
    logger.info("Prompt capture watching %s (%d existing transcript[s], repo=%s, autoland=%s)",
                proot, len(offsets), repo_str, autoland)

    async def _capture(rec, files):
        if not rec.get("key") or rec["key"] in known:
            return
        known.add(rec["key"])
        pending.pop(rec.get("sessionId"), None)
        ep = make_capture_episode(root, rec, files=files)
        persistence.upsert_episode(ep)
        baselines[ep["episodeId"]] = _dirty_set(root)
        last_change[ep["episodeId"]] = time.time()
        logger.info("Captured prompt episode %s (cwd=%s)", ep["episodeId"], rec.get("cwd"))
        await _safe_broadcast(manager, {"type": "episode_updated", "episode": ep})
        if on_episode:
            try:
                on_episode(ep)
            except Exception:  # noqa: BLE001
                pass

    tick = 0
    while True:
        try:
            await asyncio.sleep(interval)
            tick += 1
            for f in _transcripts():
                key = str(f)
                start = offsets.get(key, 0)
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                if size <= start:
                    offsets[key] = size           # unchanged / new-baseline / rotated
                    continue
                entries, new_off = read_new_lines(f, start)
                offsets[key] = new_off
                for e in entries:
                    if is_human_prompt(e):
                        rec = _prompt_record(e)
                        if rec.get("cwd") == repo_str:
                            await _capture(rec, files=[])      # cwd-matched → now
                        else:
                            pending[rec.get("sessionId")] = rec  # wait for a repo edit
                        continue
                    edited = edit_files_under(e, root)         # edits under the repo?
                    if edited:
                        pend = pending.get(e.get("sessionId"))
                        if pend:
                            await _capture(pend, files=edited)
            # Attribute later work-tree edits to the newest open capture episode.
            if tick % 2 == 0:
                await _link_changes(root, persistence, baselines, manager, last_change)
            # Conservative Auto-Land: once a capture episode's files have settled for a
            # quiet window, commit them (scoped). Ambiguous sets stay needs_manual_land.
            if autoland:
                await _maybe_autoland(root, persistence, manager, last_change, quiet_window)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad tick must not kill the watcher
            logger.debug("prompt_capture tick failed", exc_info=True)


async def _link_changes(root, persistence, baselines, manager, last_change=None) -> None:
    """Flip the newest open/reviewing capture episode to 'reviewing' and attach the
    files that changed since its prompt (baseline diff) — so clicking the chip ambers
    the right files and Review/Land has something to commit. Records the change time so
    the quiet-window Auto-Land timer only fires after edits settle."""
    active = next((e for e in persistence.load_episodes()
                   if e.get("source") == "openfde-capture"
                   and e.get("status") in ("open", "reviewing")), None)
    if not active:
        return
    base = baselines.get(active["episodeId"], set())
    new_files = sorted(_dirty_set(root) - base)
    if new_files and (active.get("files") != new_files or active.get("status") != "reviewing"):
        active["status"] = "reviewing"
        active["files"] = new_files
        active["updatedAt"] = _now()
        persistence.upsert_episode(active)
        if last_change is not None:
            last_change[active["episodeId"]] = time.time()      # reset the quiet timer
        await _safe_broadcast(manager, {"type": "episode_updated", "episode": active})


async def _maybe_autoland(root, persistence, manager, last_change, quiet_window) -> None:
    """Auto-Land the newest reviewing capture episode once its files have been quiet.

    Conservative: only a capture episode with attributed files, no file-set change for
    ``quiet_window`` seconds, lands — and only the *newest* one (older reviewing episodes
    with overlapping files are left as ``needs_manual_land`` by the scoped guardrail).
    """
    from openfde import autoland as _al
    active = next((e for e in persistence.load_episodes()
                   if e.get("source") == "openfde-capture"
                   and e.get("status") == "reviewing" and (e.get("files") or [])), None)
    if not active:
        return
    eid = active["episodeId"]
    quiet_for = time.time() - last_change.get(eid, 0)
    if quiet_for < quiet_window:
        return                                  # still settling — wait
    result = _al.land_episode(root, persistence, active, auto=True)
    last_change.pop(eid, None)                  # landed / parked → stop timing it
    for msg in result.get("broadcasts", []):
        await _safe_broadcast(manager, msg)
    logger.info("Auto-land tick for %s → %s", eid, result.get("status"))
