"""
openfde/prompt_capture.py — Passive Prompt Capture v1 (Claude Code **and** Codex).

    Git tells us what landed. OpenFDE should tell us why.

Tails the local agent session transcripts for the watched repo and turns each new human
prompt into a durable OpenFDE **episode** — *no wrapper needed*. You just talk to your
agent from the repo and the prompt shows up on the OpenArchitect rail; edits it made stay
in the work tree for OpenFDE to Review and Land.

Two agents are tailed via small **provider adapters** (``_ADAPTERS``); adding an agent is
adding an adapter, the watch loop is shared:
  - **Claude Code** — ``~/.claude/projects/<encoded-cwd>/*.jsonl``; a human prompt is a
    ``{"type":"user","message":{"role":"user",...},"cwd":…,"sessionId":…}`` entry.
  - **Codex** — ``~/.codex/sessions/**/rollout-*.jsonl`` (+ ``archived_sessions/``); a human
    prompt is a ``{"type":"response_item","payload":{"type":"message","role":"user",
    "content":[{"type":"input_text","text":…}]}}`` entry. The session's **cwd / id live in a
    ``session_meta`` (and ``turn_context``) entry**, carried as per-file context. Codex's own
    injected blocks (AGENTS.md, ``<environment_context>``, user-instructions) are filtered.

Scope of v1 (honest):
  - **Capture-forward.** We baseline every transcript to end-of-file on startup, so we never
    replay history (that's the future, confidence-tagged *import* — for any agent).
  - **cwd-matched is the strong path.** A session whose cwd IS the watched repo is captured
    immediately. Claude Code is also **cwd-agnostic** (its tool_use carries clean file paths,
    so a session rooted elsewhere is captured when it edits this repo). Codex tool events are
    shell/``apply_patch`` (no clean ``file_path``), so Codex attribution is **dirty-set based**:
    cwd-matched capture + the work-tree baseline diff. Honest, not
    perfect — a Codex session rooted elsewhere isn't auto-captured in v1.
  - **Whole episodes over eager commits.** Passive episodes auto-land ONLY on the next
    prompt boundary in the same session (reliable); otherwise they stay ``reviewing`` and
    keep accumulating files until the user lands. The quiet-window idle land is opt-in
    (``PASSIVE_IDLE_AUTOLAND``, default off) — silence cannot distinguish a finished turn
    from a long tool call, and it split long agent turns twice in production use.
  - **Heuristic file attribution.** A prompt's touched files = the work-tree changes that
    appeared *after* the prompt (baseline diff), attributed to the newest open capture episode.
"""

import asyncio
import hashlib
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


def _same_dir(a, b) -> bool:
    """True when two paths point at the same directory — resilient to symlinks (e.g. macOS
    ``/tmp`` → ``/private/tmp``) and trailing slashes, so a session's cwd matches the watched
    repo even when one side is unresolved."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return str(a) == str(b)


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


def _rel_under(fp, root_real: str):
    """Repo-relative path when ``fp`` RESOLVES to a location under ``root_real`` (symlinks
    resolved on both sides), else None. Containment is applied to real FILE paths only —
    never to a session/transcript directory."""
    if not fp:
        return None
    try:
        fp_real = os.path.realpath(str(fp))
    except (OSError, ValueError, TypeError):
        return None
    if fp_real.startswith(root_real + os.sep):
        return os.path.relpath(fp_real, root_real)
    return None


def _tool_files_under(entry: dict, repo_root, tools) -> list:
    """Repo-relative files the entry's ``tools`` (file_path) calls touch under
    ``repo_root`` — canonical (symlink-resolved) containment, deduped, order-preserving."""
    msg = entry.get("message") if isinstance(entry, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    try:
        root_real = os.path.realpath(str(repo_root))
    except (OSError, ValueError):
        return []
    out, seen = [], set()
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in tools):
            continue
        rel = _rel_under((b.get("input") or {}).get("file_path"), root_real)
        if rel is not None and rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def edit_files_under(entry: dict, repo_root) -> list:
    """Repo-relative paths an entry's Edit/Write/MultiEdit tool calls touch under
    ``repo_root`` (canonical, symlink-resolved). The WRITE set — drives episode files
    and land status; empty when the turn wrote nothing under the repo."""
    return _tool_files_under(entry, repo_root, ("Edit", "Write", "MultiEdit"))


def repo_file_evidence(entry: dict, repo_root) -> list:
    """Repo-relative files an entry's Read / Edit / Write / MultiEdit calls touch under
    ``repo_root`` (canonical). The broader ATTRIBUTION signal — a cross-cwd turn belongs
    to this repo when it touched a file whose RESOLVED path is under the repo root."""
    return _tool_files_under(entry, repo_root, ("Read", "Edit", "Write", "MultiEdit"))


# ── canonical repo identity (symlink + git-root aware) ───────────────────────
# Repo identity must be canonical, NEVER a basename: /Downloads/interview and
# /Documents/Claude/Projects/interview are different repos unless git resolves them to
# the same root. Used to attribute a prompt to a repo without folder-name false matches.
_CANON_ROOT_CACHE: dict = {}


def _git_toplevel(real_path: str):
    """The git work-tree root for ``real_path`` (symlink-resolved), or None when it is
    not inside a work tree / git is unavailable."""
    try:
        r = subprocess.run(["git", "-C", real_path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    top = (r.stdout or "").strip()
    if r.returncode != 0 or not top:
        return None
    try:
        return os.path.realpath(top)
    except (OSError, ValueError):
        return top


def canonical_repo_root(path) -> str:
    """Canonical identity for a repo path: the git work-tree root when ``path`` is in one
    (so a subdir maps to its repo root), else the resolved absolute path. Symlinks are
    always resolved; '' for a falsy/unresolvable path. Cached per process."""
    if not path:
        return ""
    key = str(path)
    if key in _CANON_ROOT_CACHE:
        return _CANON_ROOT_CACHE[key]
    try:
        real = os.path.realpath(key)
    except (OSError, ValueError):
        real = ""
    val = (_git_toplevel(real) or real) if real else ""
    _CANON_ROOT_CACHE[key] = val
    return val


def same_repo(cwd, root) -> bool:
    """True when two paths are the SAME repo by canonical identity (git root when
    available, else resolved path). Basename is never evidence; symlinks are resolved."""
    a = canonical_repo_root(cwd)
    return bool(a) and a == canonical_repo_root(root)


def claude_multirepo_context_guard(prompt: dict, file_evidence, root) -> bool:
    """REMOVABLE compatibility shim. Claude Code can expose MULTIPLE repo contexts in a
    single session (especially under ~/Claude/Projects), so a prompt's cwd or a turn's
    file paths can belong to a SIBLING repo. This guard decides — per turn — whether a
    prompt is CANONICALLY the watched repo's, preventing cross-repo prompt bleed:

      • same canonical repo as the watched root → belongs (even discussion-only);
      • a different cwd → belongs ONLY if the turn touched (read/edit/write) a file whose
        resolved path is under the watched repo.

    Never matches by basename; never treats the session directory as containment. If
    Claude later emits unambiguous per-turn repo ids, delete this guard and gate on those.
    """
    if same_repo(prompt.get("cwd"), root):
        return True
    return bool(file_evidence)


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
    if "OpenFDE owns version control" in txt:       # OpenFDE-driven runner prompt (e.g.
        return False                                # a hatch repair run) — same skip the
    return True                                     # Codex path already applies


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
        "kind": "claude-code",
    }


def read_new_prompts(path: Path, start_offset: int):
    """Read a transcript from ``start_offset``, returning new human prompts only.
    ``(prompts, new_offset)`` — each prompt is ``{key, text, sessionId, uuid, …}``."""
    entries, off = read_new_lines(path, start_offset)
    return [_prompt_record(e) for e in entries if is_human_prompt(e)], off


def capture_key_exists(persistence, key: str) -> bool:
    """True when this captureKey is already known — as a real episode OR a quarantined backfill
    candidate. The cross-process dedup check (in-memory `known` sets are per-process; the store is
    the shared truth). Checking candidates too stops a re-scan from re-importing a quarantined
    transcript fragment back into episodes.json. Cheap at capture frequency (human prompts)."""
    if not key:
        return False
    if any((e.get("captureKey") or "") == key for e in persistence.load_episodes()):
        return True
    try:
        return any((c.get("captureKey") or "") == key
                   for c in persistence.load_backfill_candidates())
    except AttributeError:        # older persistence without the candidate store
        return False


def make_capture_episode(repo_root, prompt: dict, files=None, status="open",
                         kind="claude-code") -> dict:
    """Build a capture episode (Prompt Story Rail shape) from a parsed prompt.

    ``kind`` records which agent produced it (``claude-code`` / ``codex``).
    """
    now = _now()
    files = sorted(files or [])
    return {
        "episodeId": "episode_" + secrets.token_hex(6),
        "createdAt": prompt.get("timestamp") or now, "updatedAt": now,
        "prompt": prompt.get("text", ""), "kind": kind,
        "status": ("reviewing" if files else status),
        "runIds": [], "eventIds": [], "projectEntryIds": [], "commitShas": [],
        "files": files, "summary": "", "source": "openfde-capture",
        "initialHead": _head(repo_root), "captureKey": prompt.get("key"),
        "sessionId": prompt.get("sessionId"), "sessionCwd": prompt.get("cwd"),
    }


# ── Codex adapter ───────────────────────────────────────────────────────
# Codex stores one JSONL "rollout" per session under ~/.codex/sessions/** (finished ones
# move to ~/.codex/archived_sessions/). A human prompt is a ``response_item`` message with
# role "user" + ``input_text`` content; the session's cwd/id live in a ``session_meta`` (and
# ``turn_context``) entry, carried as per-file context. v1 attributes edits via the dirty-set
# (Codex tool events are shell/apply_patch, with no clean ``file_path`` to parse).
_CODEX_SKIP_STARTS = (
    "# agents.md", "<environment_context>", "<user_instructions>",
    "<persistent_instructions>", "<instructions>",
)
_CODEX_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)


def codex_sessions_root(home=None) -> Path:
    """The directory holding live Codex session rollouts (``~/.codex/sessions``)."""
    base = Path(home) if home else Path.home()
    return base / ".codex" / "sessions"


def codex_transcripts(home=None) -> list:
    """Codex rollout JSONLs safe to tail — live sessions (recursive) + finished/archived."""
    base = Path(home) if home else Path.home()
    out = []
    live = base / ".codex" / "sessions"
    if live.exists():
        out.extend(live.rglob("rollout-*.jsonl"))
    archived = base / ".codex" / "archived_sessions"
    if archived.exists():
        out.extend(archived.glob("rollout-*.jsonl"))
    return sorted(out)


def codex_session_id_from_path(path) -> str:
    """The session UUID embedded in a rollout filename (else the file stem)."""
    m = _CODEX_UUID.search(str(path))
    return m.group(1) if m else Path(path).stem


def codex_prompt_text(entry: dict) -> str:
    """Join the ``input_text`` blocks of a Codex ``response_item`` user message."""
    p = entry.get("payload") if isinstance(entry, dict) else None
    c = p.get("content") if isinstance(p, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") in ("input_text", "text"))
    return ""


def is_codex_human_prompt(entry: dict) -> bool:
    """True when a Codex entry is a real human prompt — a user ``response_item`` message that
    is NOT Codex's injected context (AGENTS.md / ``<environment_context>`` / user-instructions),
    OpenFDE's own machine prompts, or empty."""
    if not isinstance(entry, dict) or entry.get("type") != "response_item":
        return False
    p = entry.get("payload") or {}
    if p.get("type") != "message" or p.get("role") != "user":
        return False
    txt = codex_prompt_text(entry).strip()
    if not txt:
        return False
    if _INTERNAL_MARKER in txt or "OpenFDE owns version control" in txt:
        return False
    if txt.lstrip().lower().startswith(_CODEX_SKIP_STARTS):
        return False
    return True


def _sha8(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()[:8]


def _codex_cwd_of(entry: dict):
    """The cwd carried by a Codex ``session_meta``/``turn_context`` entry (else None)."""
    if isinstance(entry, dict) and entry.get("type") in ("session_meta", "turn_context"):
        cwd = (entry.get("payload") or {}).get("cwd")
        return str(Path(cwd)) if cwd else None
    return None


def _codex_init_ctx(path) -> dict:
    """Per-file context for a Codex rollout: session id (from the filename) + cwd, pre-read
    from the leading ``session_meta`` so a session already running at startup — whose meta is
    before our baseline offset — still matches its repo."""
    fctx = {"sessionId": codex_session_id_from_path(path), "cwd": None}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(8):                  # session_meta is the first line
                ln = fh.readline()
                if not ln:
                    break
                try:
                    o = json.loads(ln)
                except (json.JSONDecodeError, ValueError):
                    continue
                cwd = _codex_cwd_of(o)
                if cwd:
                    fctx["cwd"] = cwd
                    break
    except OSError:
        pass
    return fctx


def _codex_note(entry: dict, fctx: dict) -> None:
    """Track a Codex session's cwd as later ``session_meta``/``turn_context`` entries arrive."""
    cwd = _codex_cwd_of(entry)
    if cwd:
        fctx["cwd"] = cwd


def _codex_prompt_record(entry: dict, fctx: dict) -> dict:
    txt = codex_prompt_text(entry).strip()
    ts = entry.get("timestamp")
    sid = fctx.get("sessionId")
    return {
        "key": f"codex:{sid}:{ts or _sha8(txt)}", "text": txt,
        "sessionId": sid, "uuid": ts, "timestamp": ts, "cwd": fctx.get("cwd"),
        "kind": "codex",
    }


# ── Provider adapters ───────────────────────────────────────────────────
# The watch loop tails every adapter's transcripts at once, normalizing entries to the shared
# record shape. Adding an agent = adding an adapter (no loop changes).
def _claude_transcripts(home=None) -> list:
    proot = claude_projects_root(home)
    return sorted(proot.glob("*/*.jsonl")) if proot.exists() else []


_CLAUDE_ADAPTER = {
    "kind": "claude-code",
    "transcripts": _claude_transcripts,
    "init_ctx": lambda path: {},
    "note": lambda entry, fctx: None,
    "is_prompt": is_human_prompt,
    "record": lambda entry, fctx: _prompt_record(entry),
    "edits": edit_files_under,
    # The transcript filename IS the session uuid (~/.claude/projects/<slug>/<uuid>.jsonl).
    "session_of": lambda path, fctx: path.stem,
}
_CODEX_ADAPTER = {
    "kind": "codex",
    "transcripts": codex_transcripts,
    "init_ctx": _codex_init_ctx,
    "note": _codex_note,
    "is_prompt": is_codex_human_prompt,
    "record": _codex_prompt_record,
    "edits": lambda entry, root: [],          # v1: dirty-set attribution, no brittle tool parse
    "session_of": lambda path, fctx: fctx.get("sessionId"),
}
_ADAPTERS = [_CLAUDE_ADAPTER, _CODEX_ADAPTER]


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
                     home=None, on_episode=None, quiet_window=90.0, autoland=True) -> None:
    """Poll **all** Claude Code + Codex transcripts and capture prompts relevant to this repo.

    A prompt belongs to the watched repo when EITHER the session's cwd is the repo (captured
    immediately — both agents) OR, for **Claude Code**, the session edits files under the repo
    (cwd-agnostic, captured when the first such edit lands; Codex tool events have no clean file
    path, so Codex relies on cwd-match + the dirty-set). Capture-forward (every transcript
    baselined at startup); deduped by transcript key; broadcasts ``episode_updated`` live.

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
    file_ctx = {}      # transcript path → per-file context (Codex carries cwd/sessionId here)

    def _sources():
        return [(path, ad) for ad in _ADAPTERS for path in ad["transcripts"](home)]

    def _ctx(path, ad):
        key = str(path)
        fc = file_ctx.get(key)
        if fc is None:
            fc = ad["init_ctx"](path)
            file_ctx[key] = fc
        return fc

    # Capture-forward: baseline every existing transcript (Claude + Codex) to its end.
    # Globbing + statting the user's full transcript history (hundreds of files) plus the
    # episode-store read is pure I/O — do it OFF the event loop so a cold boot's /api/boot
    # (and every other request) is never starved while we baseline.
    def _baseline_sync():
        offs, ctxs = {}, {}
        for p, a in _sources():
            ctxs[str(p)] = a["init_ctx"](p)
            try:
                offs[str(p)] = p.stat().st_size
            except OSError:
                pass
        kn = {e.get("captureKey") for e in persistence.load_episodes() if e.get("captureKey")}
        return offs, ctxs, kn
    offsets, _ctx_init, known = await asyncio.get_event_loop().run_in_executor(None, _baseline_sync)
    file_ctx.update(_ctx_init)
    pending = {}       # sessionId → last human prompt not yet captured for this repo
    baselines = {}     # episodeId → dirty set at capture time (in-memory)
    last_change = {}   # episodeId → monotonic time of its last file-set change (quiet timer)
    session_activity = {}  # sessionId → time of its transcript's last append (turn liveness)
    logger.info("Prompt capture watching Claude Code + Codex transcripts "
                "(%d baselined, repo=%s, autoland=%s)", len(offsets), repo_str, autoland)

    async def _capture(rec, files):
        if not rec.get("key") or rec["key"] in known:
            return
        # Belt-and-braces cross-process dedup: the in-memory `known` set can't see a
        # sibling process's capture (restart-overlap windows created duplicate episode
        # pairs with identical captureKeys — observed live). The store is authoritative.
        if capture_key_exists(persistence, rec["key"]):
            known.add(rec["key"])
            return
        known.add(rec["key"])
        pending.pop(rec.get("sessionId"), None)
        ep = make_capture_episode(root, rec, files=files, kind=rec.get("kind") or "claude-code")
        persistence.upsert_episode(ep)
        baselines[ep["episodeId"]] = _dirty_set(root)
        last_change[ep["episodeId"]] = time.time()
        logger.info("Captured %s prompt episode %s (cwd=%s)",
                    rec.get("kind"), ep["episodeId"], rec.get("cwd"))
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
            # Glob + stat the whole transcript history OFF the loop; only transcripts that
            # actually GREW come back to be processed (usually none) — so each poll tick costs
            # the event loop ~nothing and never starves /api/boot or the live glow.
            def _scan_grown():
                grown = []
                for p, a in _sources():
                    k = str(p)
                    st0 = offsets.get(k, 0)
                    try:
                        sz = p.stat().st_size
                    except OSError:
                        continue
                    if sz <= st0:
                        offsets[k] = sz           # unchanged / new-baseline / rotated
                    else:
                        grown.append((p, a, k, st0))
                return grown
            for path, ad, key, start in await asyncio.get_event_loop().run_in_executor(None, _scan_grown):
                fctx = _ctx(path, ad)
                # The transcript grew → its session's agent is mid-turn (thinking, tool
                # calls, results), even when no repo file changes. This liveness signal
                # keeps the idle fallback from splitting a long turn (_maybe_autoland).
                sid_alive = ad["session_of"](path, fctx)
                if sid_alive:
                    session_activity[sid_alive] = time.time()
                entries, new_off = read_new_lines(path, start)
                offsets[key] = new_off
                for e in entries:
                    ad["note"](e, fctx)           # track session cwd/id (Codex)
                    if ad["is_prompt"](e):
                        rec = ad["record"](e, fctx)
                        # Turn boundary: this session's previous captured prompt is complete —
                        # land its full (clustered) edit set before opening the new one.
                        await _land_active_capture(root, persistence, manager, last_change,
                                                   session_id=rec.get("sessionId"))
                        # Canonical repo identity (symlink + git-root aware), never the
                        # session DIRECTORY by basename — Claude can expose sibling repos
                        # in one session. Same repo → capture now (even discussion-only);
                        # a different cwd waits for a real edit under THIS repo.
                        if same_repo(rec.get("cwd"), root):
                            await _capture(rec, files=[])      # canonical repo → now
                        else:
                            pending[rec.get("sessionId")] = rec  # wait for a repo edit (Claude)
                        continue
                    edited = ad["edits"](e, root)              # edits under the repo? (Claude)
                    if edited:
                        pend = pending.get(e.get("sessionId"))
                        if pend:
                            await _capture(pend, files=edited)
            # Attribute later work-tree edits to the newest open capture episode.
            if tick % 2 == 0:
                await _link_changes(root, persistence, baselines, manager, last_change)
            # Whole episodes beat eager commits: passive capture lands an episode on
            # the NEXT prompt boundary (the reliable signal, above) or by manual Land.
            # The idle quiet-window path is opt-in only (PASSIVE_IDLE_AUTOLAND) — it
            # split long agent turns twice despite the double-quiet gate.
            if autoland:
                await _maybe_autoland(root, persistence, manager, last_change, quiet_window,
                                      session_activity)
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
        # The agent started changing files — the episode's card leaves To Do.
        try:
            if persistence.move_tasks_for_episode(active["episodeId"], "doing",
                                                  from_columns=("todo",)):
                await manager.broadcast({"type": "tasks_updated"})
        except Exception:  # noqa: BLE001 — board sync must never block capture
            pass
        if last_change is not None:
            last_change[active["episodeId"]] = time.time()      # reset the quiet timer
        await _safe_broadcast(manager, {"type": "episode_updated", "episode": active})


async def _land_active_capture(root, persistence, manager, last_change, session_id=None) -> None:
    """Land the newest open/reviewing capture episode that has attributed files — scoped, and
    clustered into **one commit per logical change**. The LLM grouping is a subprocess, so the
    land is offloaded to a thread (it returns broadcasts; we emit them here). ``session_id``
    restricts to that session's episode (the turn-boundary land on a new prompt); ``None`` lands
    whichever is newest (idle fallback). Older reviewing episodes with overlapping files stay
    ``needs_manual_land`` (the scoped guardrail in ``land_episode``).
    """
    from openfde import autoland as _al
    active = next((e for e in persistence.load_episodes()
                   if e.get("source") == "openfde-capture"
                   and e.get("status") in ("reviewing", "open") and (e.get("files") or [])
                   and (session_id is None or e.get("sessionId") == session_id)), None)
    if not active:
        return
    eid = active["episodeId"]
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: _al.land_episode(root, persistence, active, auto=True, allow_llm=True))
    last_change.pop(eid, None)                  # landed / parked → stop timing it
    for msg in result.get("broadcasts", []):
        await _safe_broadcast(manager, msg)
    logger.info("Capture land for %s → %s (%d commit[s])",
                eid, result.get("status"), len(result.get("commits") or []))


# Passive idle auto-land is OFF by default: **whole episodes beat eager commits.**
# Even file-quiet + transcript-quiet misfires on long agent turns — one long tool
# call (a 5-minute test run, a build, a screenshot wait, an executor-offloaded land)
# appends NOTHING to the transcript while the agent is very much mid-turn, so
# silence structurally cannot distinguish "thinking" from "done". CC/Codex
# transcripts carry no reliable turn-complete marker mid-stream, so passive episodes
# land on the NEXT prompt boundary in the same session (reliable) or by manual Land —
# never on silence alone. Flip this constant (or pass allow_idle=True) to opt back in.
PASSIVE_IDLE_AUTOLAND = False


async def _maybe_autoland(root, persistence, manager, last_change, quiet_window,
                          session_activity=None, allow_idle=None) -> None:
    """OPT-IN idle land for the *trailing* episode — disabled by default
    (``PASSIVE_IDLE_AUTOLAND``). History: quiet-window-only landing split long agent
    turns (landed the first settled file, orphaned the rest); adding transcript-quiet
    helped but still misfired whenever a single long tool call kept the transcript
    silent past the window. With no reliable mid-stream turn-complete signal, the
    default policy is to keep the episode ``reviewing`` (files keep accumulating via
    ``_link_changes``) until the **next prompt boundary** lands it whole, or the user
    lands manually. When opted in, the old double-quiet gate applies: files quiet for
    ``quiet_window`` AND the session transcript quiet for the same window."""
    enabled = PASSIVE_IDLE_AUTOLAND if allow_idle is None else allow_idle
    if not enabled:
        return                                  # whole episodes > eager commits
    active = next((e for e in persistence.load_episodes()
                   if e.get("source") == "openfde-capture"
                   and e.get("status") == "reviewing" and (e.get("files") or [])), None)
    if not active:
        return
    if time.time() - last_change.get(active["episodeId"], 0) < quiet_window:
        return                                  # files still settling — wait
    sid = active.get("sessionId")
    if session_activity and sid and time.time() - session_activity.get(sid, 0) < quiet_window:
        return                                  # session still streaming — the turn isn't over
    await _land_active_capture(root, persistence, manager, last_change)
