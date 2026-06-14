"""Read/write .openfde/ state files.

Files managed under .openfde/:
    state.json        — canvas boxes and arrows (serializable subset of frontend canvasState)
    tasks.json        — OpenPM task list
    events.jsonl      — append-only design/code event log (one JSON object per line)
    project.json      — project metadata and freeform entries
    project_log.jsonl — append-only human/agent conversation ledger (one entry per line)

Generated files at the repo root:
    PLAN.md          — generated plan (see plan.py / server.py)
    PROJECT_META.md  — generated project metadata (legacy; see _render_project_md)
    project.md       — generated conversation ledger (see _render_project_log_md)

All writes are atomic (write-to-temp then os.replace) to avoid partial-write corruption.
Events are normalized and sanitized before storage (see normalize_event()).
"""

import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("openfde.persistence")

# ------------------------------------------------------------------ #
#  Event normalization constants                                        #
# ------------------------------------------------------------------ #

# Top-level payload keys whose values are replaced with "[redacted]"
_REDACT_KEYS: frozenset = frozenset({
    "token", "api_key", "apikey", "authorization", "password",
    "secret", "credential", "credentials", "env", "key",
})
_MAX_STRING_LEN: int = 512      # characters; longer strings are truncated
_MAX_LIST_LEN: int = 50         # items; longer lists are sliced
_MAX_PAYLOAD_DEPTH: int = 3     # nesting levels; deeper values become "[truncated]"


# ------------------------------------------------------------------ #
#  Event normalization helpers                                         #
# ------------------------------------------------------------------ #

def _sanitize_value(value: Any, depth: int = 0) -> Any:
    """Recursively sanitize a value for safe event storage.

    Truncates long strings, caps list length, and prevents deep nesting.

    Args:
        value: Any — value to sanitize
        depth: int — current nesting depth (0 = direct payload field)

    Returns:
        Any — sanitized value safe for JSON storage
    """
    if depth >= _MAX_PAYLOAD_DEPTH:
        return "[truncated]"
    if isinstance(value, str):
        return value[:_MAX_STRING_LEN] if len(value) > _MAX_STRING_LEN else value
    if isinstance(value, dict):
        return {
            k: "[redacted]" if k.lower() in _REDACT_KEYS
               else _sanitize_value(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item, depth + 1) for item in value[:_MAX_LIST_LEN]]
    return value


def normalize_event(event: dict) -> dict:
    """Normalize and sanitize an event dict before writing to events.jsonl.

    Ensures required envelope fields are present, sanitizes the payload
    (truncates strings, caps lists, redacts sensitive keys), and returns a
    clean copy.  The original dict is never mutated.

    Args:
        event: dict — raw event dict from the frontend or an internal source;
                      expected keys: id, timestamp, type, payload

    Returns:
        dict — normalized event with guaranteed fields:
               id (str), timestamp (ISO-8601 str), type (str),
               payload (dict, sanitized), traceId (str)
    """
    evt = dict(event)
    now = datetime.now(timezone.utc).isoformat()

    if not evt.get("id"):
        evt["id"] = secrets.token_hex(6)          # 12-char opaque hex
    if not evt.get("timestamp"):
        evt["timestamp"] = now
    if not evt.get("type"):
        evt["type"] = "unknown"
    if not isinstance(evt.get("payload"), dict):
        # Wrap scalar or missing payload in a container
        raw = evt.get("payload")
        evt["payload"] = {"data": raw} if raw is not None else {}
    if not evt.get("traceId"):
        evt["traceId"] = secrets.token_hex(4)     # 8-char trace correlation ID

    evt["payload"] = _sanitize_value(evt["payload"], depth=0)
    return evt


# ------------------------------------------------------------------ #
#  Persistence defaults                                                #
# ------------------------------------------------------------------ #

_DEFAULT_STATE: dict = {"boxes": [], "arrows": []}
_DEFAULT_PROJECT: dict = {"name": "", "description": "", "entries": []}

# ------------------------------------------------------------------ #
#  Project-log (conversation ledger) constants                         #
# ------------------------------------------------------------------ #

# Valid ledger roles; anything else is coerced to "human".
_LEDGER_ROLES: frozenset = frozenset({"architect", "sr_dev", "verifier", "human"})
# Body text is preserved (it holds compiled specs) but capped to avoid runaway.
_MAX_LEDGER_BODY_LEN: int = 200_000
_MAX_LEDGER_SHORT_LEN: int = 2_000     # title / summary
_MAX_LEDGER_REF_LIST: int = 500        # boxIds / arrowIds / filePaths

# Human-readable label per role, used when rendering project.md.
_LEDGER_LABEL: dict = {
    "architect": "architect",
    "sr_dev":    "sr dev",
    "verifier":  "verifier",
    "human":     "human",
}


def normalize_ledger_entry(entry: dict) -> dict:
    """Normalize a conversation-ledger entry before writing to project_log.jsonl.

    Unlike events, the ``body`` field is preserved (it may carry a full compiled
    spec) — it is only capped at a generous limit. Required envelope fields are
    injected when missing and the role is constrained to a known set.

    Args:
        entry: dict — raw ledger entry; recognised keys: id, timestamp, role,
                      title, summary, body, eventId, boxIds, arrowIds,
                      filePaths, metadata.

    Returns:
        dict — normalized entry with guaranteed fields.
    """
    src = dict(entry)
    now = datetime.now(timezone.utc).isoformat()

    def _short(v: Any) -> str:
        s = "" if v is None else str(v)
        return s[:_MAX_LEDGER_SHORT_LEN]

    def _strlist(v: Any) -> list:
        if not isinstance(v, list):
            return []
        return [str(x) for x in v[:_MAX_LEDGER_REF_LIST]]

    role = src.get("role")
    if role not in _LEDGER_ROLES:
        role = "human"

    body = src.get("body")
    body = "" if body is None else str(body)
    if len(body) > _MAX_LEDGER_BODY_LEN:
        body = body[:_MAX_LEDGER_BODY_LEN] + "\n\n…[truncated]"

    meta = src.get("metadata")
    if not isinstance(meta, dict):
        meta = {}

    return {
        "id":        src.get("id") or f"entry_{secrets.token_hex(6)}",
        "timestamp": src.get("timestamp") or now,
        "role":      role,
        "title":     _short(src.get("title")),
        "summary":   _short(src.get("summary")),
        "body":      body,
        "eventId":   (str(src["eventId"]) if src.get("eventId") else None),
        "boxIds":    _strlist(src.get("boxIds")),
        "arrowIds":  _strlist(src.get("arrowIds")),
        "filePaths": _strlist(src.get("filePaths")),
        "metadata":  _sanitize_value(meta, depth=0),
    }


# ------------------------------------------------------------------ #
#  Persistence class                                                   #
# ------------------------------------------------------------------ #

class Persistence:
    """Manages .openfde/ state files for a single watched repository.

    Args:
        openfde_dir: Path — the .openfde/ directory path inside the watched repo
    """

    def __init__(self, openfde_dir: Path) -> None:
        """Initialize the persistence layer.

        Args:
            openfde_dir: Path — path to the .openfde/ directory (must exist)

        Returns:
            None
        """
        self.dir = openfde_dir
        self.state_path       = openfde_dir / "state.json"
        self.tasks_path       = openfde_dir / "tasks.json"
        self.events_path      = openfde_dir / "events.jsonl"
        self.project_path     = openfde_dir / "project.json"
        self.project_log_path = openfde_dir / "project_log.jsonl"
        self.box_specs_path   = openfde_dir / "box_specs.json"
        self.runs_path        = openfde_dir / "runs.json"
        self.run_events_path  = openfde_dir / "run_events.jsonl"
        self.execution_path   = openfde_dir / "execution.json"
        self.workflows_dir    = openfde_dir / "workflows"
        self.approvals_path   = openfde_dir / "approvals.json"
        self.agent_settings_path = openfde_dir / "agent_settings.json"
        self.concept_cards_path = openfde_dir / "concept_cards.json"
        self.episodes_path = openfde_dir / "episodes.json"
        self.council_chat_path = openfde_dir / "council_chat.json"
        # Latest worktree-level verification evidence (.openfde/verify.json stays
        # reserved for the user's check CONFIG — see openfde/verify.py).
        self.verify_latest_path = openfde_dir / "verify_latest.json"
        self._ensure_excluded()

    def _ensure_excluded(self) -> None:
        """Keep OpenFDE's OWN footprint out of the watched repo — without touching
        any TRACKED file. We append ``.openfde/`` to ``.git/info/exclude`` (git's
        local, uncommitted ignore), NOT the repo's ``.gitignore`` (which would
        itself show up as a change and leak into the user's PR). Idempotent; silent
        on any error (no-git / read-only must never break startup)."""
        try:
            info = self.dir.parent / ".git" / "info"
            if not info.is_dir():
                return                       # not a standard git repo — nothing to do
            excl = info / "exclude"
            existing = excl.read_text(encoding="utf-8") if excl.exists() else ""
            if any(ln.strip().rstrip("/") == ".openfde" for ln in existing.splitlines()):
                return
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            excl.write_text(existing + sep + ".openfde/\n", encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _read_json(self, path: Path, default: Any) -> Any:
        """Read a JSON file, returning default on missing or corrupt file.

        Args:
            path: Path — file to read
            default: Any — value returned when file is absent or unparseable

        Returns:
            Any — parsed JSON value, or default
        """
        if not path.exists():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            logger.error("JSON parse error in %s: %s", path.name, exc)
            return default
        except OSError as exc:
            logger.error("Failed to read %s: %s", path.name, exc)
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        """Atomically write data to a JSON file.

        Writes to a .tmp sibling first, then os.replace() to ensure the
        target is never left in a partially-written state.

        Args:
            path: Path — destination file path
            data: Any — JSON-serializable value to write

        Returns:
            None

        Raises:
            OSError — if the temp write or rename fails
        """
        # Unique tmp per write: a FIXED tmp name ("episodes.tmp") let two writers —
        # a dying and a starting server, or two executor threads — interleave bytes
        # in the SAME tmp file, and os.replace() then promoted the splice (observed
        # live: torn episodes.json). pid+tid+nonce makes each write's tmp private;
        # os.replace stays the atomic publish.
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
        try:
            # Self-heal: recreate the .openfde dir if it was removed mid-session.
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("Failed to write %s: %s", path.name, exc)
            try:
                tmp.unlink(missing_ok=True)        # never leave private tmps behind
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    #  Canvas state                                                        #
    # ------------------------------------------------------------------ #

    def load_state(self) -> dict:
        """Load canvas state (boxes and arrows).

        Returns:
            dict with keys:
                boxes: list[dict] — canvas box objects
                arrows: list[dict] — canvas arrow objects
        """
        raw = self._read_json(self.state_path, _DEFAULT_STATE)
        return {
            "boxes":  raw.get("boxes", []),
            "arrows": raw.get("arrows", []),
        }

    def save_state(self, state: dict) -> None:
        """Persist canvas state (boxes and arrows; selection state is ephemeral).

        Args:
            state: dict — must contain 'boxes' (list) and 'arrows' (list);
                          Set-based selection fields are silently dropped

        Returns:
            None
        """
        clean = {
            "boxes":  state.get("boxes", []),
            "arrows": state.get("arrows", []),
        }
        self._write_json(self.state_path, clean)
        box_count   = len(clean["boxes"])
        arrow_count = len(clean["arrows"])
        logger.info("State saved (%d box(es), %d arrow(s))", box_count, arrow_count)

    # ------------------------------------------------------------------ #
    #  Tasks                                                               #
    # ------------------------------------------------------------------ #

    def load_tasks(self) -> list:
        """Load the OpenPM task list.

        Returns:
            list[dict] — task objects; empty list if no tasks file exists
        """
        return self._read_json(self.tasks_path, [])

    def move_tasks_for_episode(self, episode_id: str, column: str,
                               verification: str = None,
                               from_columns: tuple = None) -> int:
        """Move every card tied to an episode to a board column (the card
        lifecycle rides the EPISODE's real transitions: created → doing,
        checks run → testing, landed → done).

        Returns:
            int — number of cards moved.
        """
        tasks = self.load_tasks()
        n = 0
        for t in tasks:
            if isinstance(t, dict) and t.get("episodeId") == episode_id:
                if from_columns is not None and t.get("column") not in from_columns:
                    continue                      # monotonic: never demote silently
                t["column"] = column
                if verification:
                    t["verificationStatus"] = verification
                n += 1
        if n:
            self.save_tasks(tasks)
        return n

    def reopen_episode(self, episode_id: str):
        """Reopen a LANDED episode for follow-up repair — the fix of a fix
        belongs to the SAME story, never a new episode. Moves it back to
        'reviewing' and to the front of the store (the watcher attributes
        subsequent edits to the newest active episode).

        Returns:
            dict | None — the reopened episode, or None (unknown / not landed).
        """
        ep = self.get_episode(episode_id)
        if not ep or ep.get("status") != "landed":
            return None
        ep["status"] = "reviewing"
        ep["updatedAt"] = datetime.now(timezone.utc).isoformat()
        return self.upsert_episode(ep)

    def update_task(self, task_id: str, patch: dict):
        """Merge ``patch`` into one task by id and persist.

        Returns:
            dict | None — the updated task, or None when the id is unknown.
        """
        tasks = self.load_tasks()
        for t in tasks:
            if isinstance(t, dict) and t.get("id") == task_id:
                t.update(patch)
                self.save_tasks(tasks)
                return t
        return None

    def save_tasks(self, tasks: list) -> None:
        """Persist the OpenPM task list.

        Args:
            tasks: list[dict] — full task list to persist

        Returns:
            None
        """
        self._write_json(self.tasks_path, tasks)
        logger.info("Tasks saved (%d task(s))", len(tasks))

    # ------------------------------------------------------------------ #
    #  Events                                                              #
    # ------------------------------------------------------------------ #

    def load_events(self) -> list:
        """Load all design/code events from events.jsonl, oldest-first.

        Returns:
            list[dict] — event objects in chronological order (oldest first)
        """
        if not self.events_path.exists():
            return []
        events: list = []
        try:
            with open(self.events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError as exc:
                            logger.warning("Skipping corrupt event line: %s", exc)
        except OSError as exc:
            logger.error("Failed to read events: %s", exc)
        return events

    def append_event(self, event: dict) -> dict:
        """Normalize, sanitize, and append a single event to events.jsonl.

        Args:
            event: dict — raw event dict; must have 'type'; all other
                          required fields are injected if absent

        Returns:
            dict — the normalized event that was written to disk
        """
        normalized = normalize_event(event)
        try:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(normalized) + "\n")
            logger.info("Event appended (type=%s id=%s)", normalized["type"], normalized["id"])
        except OSError as exc:
            logger.error("Failed to append event: %s", exc)
        return normalized

    # ------------------------------------------------------------------ #
    #  Project                                                             #
    # ------------------------------------------------------------------ #

    def load_project(self) -> dict:
        """Load project metadata.

        Returns:
            dict with keys:
                name: str — project name
                description: str — one-line description
                entries: list[dict] — freeform project log entries
        """
        raw = self._read_json(self.project_path, _DEFAULT_PROJECT)
        return {
            "name":        raw.get("name", ""),
            "description": raw.get("description", ""),
            "entries":     raw.get("entries", []),
        }

    def save_project(self, data: dict, repo_root: Path) -> None:
        """Persist project metadata and regenerate PROJECT.md at the repo root.

        Args:
            data: dict — project data with 'name', 'description', 'entries'
            repo_root: Path — repository root where PROJECT.md is written

        Returns:
            None
        """
        self._write_json(self.project_path, data)
        logger.info("Project saved (name=%r)", data.get("name") or "")
        md = _render_project_md(data)
        # NOTE: written as PROJECT_META.md (not PROJECT.md) so it cannot collide
        # with the lowercase conversation ledger project.md on case-insensitive
        # filesystems (macOS APFS/HFS+ default).
        # OpenFDE's own artifact → inside .openfde/ (git-excluded), never the repo
        # root, so watching a foreign repo leaves its tree untouched.
        project_md_path = self.dir / "PROJECT_META.md"
        tmp = self.dir / ".project_meta_md.tmp"
        try:
            tmp.write_text(md, encoding="utf-8")
            os.replace(tmp, project_md_path)
            logger.info("PROJECT_META.md written")
        except OSError as exc:
            logger.error("Failed to write PROJECT_META.md: %s", exc)

    # ------------------------------------------------------------------ #
    #  Box specs (prompt provenance)                                       #
    # ------------------------------------------------------------------ #

    def load_box_specs(self) -> dict:
        """Load the box-specs map (boxId → spec).

        Returns:
            dict — box-specs keyed by boxId; empty dict when no file exists.
        """
        raw = self._read_json(self.box_specs_path, {})
        return raw if isinstance(raw, dict) else {}

    def save_box_specs(self, specs: dict) -> None:
        """Persist the box-specs map.

        Args:
            specs: dict — box-specs keyed by boxId.

        Returns:
            None
        """
        self._write_json(self.box_specs_path, specs)
        logger.info("Box specs saved (%d box(es))", len(specs or {}))

    # ------------------------------------------------------------------ #
    #  Execution runs + trace events (Step 17)                             #
    # ------------------------------------------------------------------ #

    def load_runs(self) -> list:
        """Load execution run records (latest-first).

        Returns:
            list[dict] — run records; empty when no file exists.
        """
        raw = self._read_json(self.runs_path, [])
        return raw if isinstance(raw, list) else []

    def upsert_run(self, run: dict, cap: int = 50) -> dict:
        """Insert or update a run record by runId, keeping the most recent `cap`.

        Args:
            run: dict — run record; must contain 'runId'.
            cap: int — maximum number of run records to retain.

        Returns:
            dict — the stored run record.
        """
        runs = self.load_runs()
        rid = run.get("runId")
        runs = [r for r in runs if r.get("runId") != rid]
        runs.insert(0, run)
        self._write_json(self.runs_path, runs[:cap])
        return run

    # ------------------------------------------------------------------ #
    #  Concept cards (Step 37a — short notes on a concept/commit)          #
    # ------------------------------------------------------------------ #

    def load_concept_cards(self) -> list:
        """Load saved concept cards (newest-first).

        Returns:
            list[dict] — cards; empty when no file exists.
        """
        raw = self._read_json(self.concept_cards_path, [])
        return raw if isinstance(raw, list) else []

    def load_verify_latest(self) -> dict:
        """Latest worktree-level verification evidence ({} when never run).

        Returns:
            dict — {status, checks[], ranAt, durationMs, note?} from openfde.verify.
        """
        raw = self._read_json(self.verify_latest_path, {})
        return raw if isinstance(raw, dict) else {}

    def save_verify_latest(self, evidence: dict) -> None:
        """Persist the latest worktree-level verification evidence.

        Args:
            evidence: dict — run_verification() result.
        """
        self._write_json(self.verify_latest_path, evidence or {})

    # ------------------------------------------------------------------ #
    #  Council chat thread (read-only Q&A; survives a browser refresh)     #
    # ------------------------------------------------------------------ #

    def load_council_chat(self) -> list:
        """Recent council chat turns (oldest-first), so a refresh restores the thread.

        Returns:
            list[dict] — each {role: 'user'|'assistant', text, label?, provider?,
                contributorsLabel?, ts}.
        """
        raw = self._read_json(self.council_chat_path, [])
        return raw if isinstance(raw, list) else []

    def append_council_chat(self, turns, cap: int = 80) -> list:
        """Append one or more chat turns, keeping the most recent ``cap``.

        Args:
            turns: dict | list[dict] — turn(s) to append (see load_council_chat shape).
            cap: int — maximum turns retained.

        Returns:
            list[dict] — the full thread after appending (trimmed to ``cap``).
        """
        thread = self.load_council_chat()
        thread.extend(turns if isinstance(turns, list) else [turns])
        thread = thread[-cap:]
        self._write_json(self.council_chat_path, thread)
        return thread

    def add_concept_card(self, card: dict, cap: int = 200) -> dict:
        """Prepend a concept card, keeping the most recent `cap`.

        Args:
            card: dict — {id, title, summary, tetherId?, commitSha?, files?, createdAt}.
            cap: int — maximum number of cards to retain.

        Returns:
            dict — the stored card.
        """
        cards = self.load_concept_cards()
        cards.insert(0, card)
        self._write_json(self.concept_cards_path, cards[:cap])
        return card

    # ------------------------------------------------------------------ #
    #  Prompt episodes (OpenFDE-owned commits — Land·Watch·Review)         #
    # ------------------------------------------------------------------ #
    #  A durable "prompt turn": the user's intent + the runs/events it
    #  spawned + the commit(s) OpenFDE lands for it. The Prompt Story Rail
    #  renders these newest-first; commits become evidence under a prompt.

    def load_episodes(self) -> list:
        """Load prompt episodes (newest-first).

        Returns:
            list[dict] — episode records; empty when no file exists.
        """
        raw = self._read_json(self.episodes_path, [])
        return raw if isinstance(raw, list) else []

    def upsert_episode(self, episode: dict, cap: int = 200) -> dict:
        """Insert or update an episode by episodeId, keeping the most recent `cap`.

        Assigns story metadata (sequence/tag/title/summary) on first write and
        leaves it untouched thereafter, so a prompt keeps its number/label for life.

        Args:
            episode: dict — episode record; must contain 'episodeId'.
            cap: int — maximum episodes retained.

        Returns:
            dict — the stored episode (enriched in place).
        """
        from openfde.episode_summary import enrich_episode
        episodes = self.load_episodes()
        eid = episode.get("episodeId")
        others = [e for e in episodes if e.get("episodeId") != eid]
        max_seq = max((e.get("sequence") or 0) for e in others) if others else 0
        enrich_episode(episode, max_seq)            # assigns sequence/tag/title/summary if absent
        others.insert(0, episode)
        self._write_json(self.episodes_path, others[:cap])
        return episode

    def backfill_episode_meta(self) -> list:
        """Lazily assign story metadata to any episode missing it; persist if changed.

        Covers episodes created before the story-meta upgrade (no sequence/tag/title/
        summary). New numbers are assigned oldest-first (by ``createdAt``) above the
        current max, so ordering stays monotonic. Idempotent — a no-op once every
        episode is enriched.

        Returns:
            list[dict] — episodes newest-first (enriched).
        """
        from openfde.episode_summary import enrich_episode
        eps = self.load_episodes()
        if not eps:
            return eps
        missing = [e for e in eps if not (e.get("sequence") and e.get("tag")
                                          and e.get("title") and e.get("summary"))]
        if not missing:
            return eps
        max_seq = max((e.get("sequence") or 0) for e in eps)
        for e in sorted(missing, key=lambda x: x.get("createdAt") or ""):
            max_seq = enrich_episode(e, max_seq)
        self._write_json(self.episodes_path, eps)
        return eps

    def get_episode(self, episode_id: str) -> dict:
        """Return a single episode by id, or None.

        Args:
            episode_id: str — episode identifier.

        Returns:
            dict | None — the episode, or None.
        """
        for e in self.load_episodes():
            if e.get("episodeId") == episode_id:
                return e
        return None

    def upsert_repair_artifact(self, episode_id: str, artifact: dict,
                               cap: int = 24) -> dict:
        """Store a repair-hatch artifact on its OWNING episode — never a new one.

        Artifacts (failure_explanation | repair_prompt | failure_flow |
        repair_run) live in ``episode["repairArtifacts"]``, identified by
        (kind, fingerprint): the LLM runs once per failure meaning, and storing
        the same identity again REPLACES the artifact (explicit regenerate),
        preserving its id/createdAt and bumping updatedAt.

        Args:
            episode_id: str — owning episode.
            artifact: dict — {kind, fingerprint, checkId, file, line, function,
                test, source, text, summary, nodes, edges, …}.
            cap: int — max artifacts kept per episode (oldest dropped).

        Returns:
            dict | None — the stored artifact (with id/timestamps), or None
            when the episode id is unknown.
        """
        ep = self.get_episode(episode_id)
        if not ep:
            return None
        arts = [a for a in (ep.get("repairArtifacts") or []) if isinstance(a, dict)]
        kind, fp = artifact.get("kind"), artifact.get("fingerprint")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        old = next((a for a in arts if a.get("kind") == kind
                    and a.get("fingerprint") == fp), None)
        if old is not None:
            stored = {**old, **artifact, "id": old.get("id"),
                      "createdAt": old.get("createdAt") or now, "updatedAt": now}
            arts[arts.index(old)] = stored
        else:
            aid = "repair_" + hashlib.sha256(f"{kind}:{fp}".encode()).hexdigest()[:10]
            stored = {**artifact, "id": aid, "createdAt": now, "updatedAt": now}
            arts.append(stored)
            arts = arts[-cap:]
        ep["repairArtifacts"] = arts
        self.upsert_episode(ep)
        return stored

    def get_repair_artifacts(self, episode_id: str, fingerprint: str = None) -> list:
        """Repair artifacts on an episode, optionally only one fingerprint's.

        Args:
            episode_id: str — episode identifier.
            fingerprint: str | None — filter to one failure meaning.

        Returns:
            list[dict] — artifacts (possibly empty).
        """
        ep = self.get_episode(episode_id)
        arts = [a for a in ((ep or {}).get("repairArtifacts") or []) if isinstance(a, dict)]
        if fingerprint:
            arts = [a for a in arts if a.get("fingerprint") == fingerprint]
        return arts

    def latest_active_episode(self) -> dict:
        """Return the newest episode still awaiting review/land (open|reviewing).

        Used by the Land flow and the prompt-capture wrappers to find the episode
        a fresh worktree change should attach to.

        Returns:
            dict | None — the newest open/reviewing episode, or None.
        """
        for e in self.load_episodes():                 # newest-first
            if e.get("status") in ("open", "reviewing"):
                return e
        return None

    def get_open_episode_for_run(self, run_id: str) -> dict:
        """Return the episode already linked to a run id, or None.

        Args:
            run_id: str — run identifier.

        Returns:
            dict | None — the linked episode, or None.
        """
        if not run_id:
            return None
        for e in self.load_episodes():
            if run_id in (e.get("runIds") or []):
                return e
        return None

    def get_run(self, run_id: str) -> dict:
        """Return a single run record by id, or None.

        Args:
            run_id: str — run identifier.

        Returns:
            dict | None — the run record, or None.
        """
        for r in self.load_runs():
            if r.get("runId") == run_id:
                return r
        return None

    def load_run_events(self, run_id: str = None) -> list:
        """Load trace events, oldest-first, optionally filtered by run.

        Args:
            run_id: str | None — when set, only events for this run.

        Returns:
            list[dict] — trace events in chronological order.
        """
        if not self.run_events_path.exists():
            return []
        events: list = []
        try:
            with open(self.run_events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError as exc:
                        logger.warning("Skipping corrupt run-event line: %s", exc)
                        continue
                    if run_id is None or evt.get("runId") == run_id:
                        events.append(evt)
        except OSError as exc:
            logger.error("Failed to read run events: %s", exc)
        return events

    def append_run_event(self, event: dict) -> dict:
        """Summarize/redact payloads and append a trace event to run_events.jsonl.

        Args:
            event: dict — raw trace event; payload fields (input/output/error/
                          intermediate) are summarized + redacted before storage.

        Returns:
            dict — the normalized event that was written.
        """
        from openfde.trace import summarize_trace_event

        evt = dict(event)
        if not evt.get("id"):
            evt["id"] = secrets.token_hex(6)
        if not evt.get("timestamp"):
            evt["timestamp"] = datetime.now(timezone.utc).isoformat()
        if not evt.get("type"):
            evt["type"] = "trace"
        evt = summarize_trace_event(evt)
        try:
            with open(self.run_events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(evt) + "\n")
        except OSError as exc:
            logger.error("Failed to append run event: %s", exc)
        return evt

    # ------------------------------------------------------------------ #
    #  Execution backend config + workflow artifacts (Step 19)            #
    # ------------------------------------------------------------------ #

    def load_execution_config(self) -> dict:
        """Load execution backend configuration.

        Returns:
            dict — {"activeBackend": str}; defaults applied by the caller.
        """
        raw = self._read_json(self.execution_path, {})
        return raw if isinstance(raw, dict) else {}

    def save_execution_config(self, data: dict) -> None:
        """Persist execution backend configuration.

        Args:
            data: dict — e.g. {"activeBackend": "claude-code-workflow"}.

        Returns:
            None
        """
        self._write_json(self.execution_path, data)
        logger.info("Execution config saved (backend=%s)", data.get("activeBackend"))

    # ------------------------------------------------------------------ #
    #  Agent role settings (Step 21)                                       #
    # ------------------------------------------------------------------ #

    def sanitize_agent_settings(self, settings: dict) -> dict:
        """Normalize an agent-settings map into the safe internal shape.

        Delegates to agent_settings.normalize so the stored file always has all
        three roles with valid mode/provider/strings. Raw apiKey values are
        preserved (this is the on-disk representation, never the API response).

        Args:
            settings: dict — candidate settings.

        Returns:
            dict — normalized internal settings.
        """
        from openfde import agent_settings
        return agent_settings.normalize(settings)

    def load_agent_settings(self) -> dict:
        """Load agent role settings, normalized (defaults when absent).

        Returns:
            dict — internal settings {architect, senior_dev, verifier}.
        """
        raw = self._read_json(self.agent_settings_path, {})
        return self.sanitize_agent_settings(raw)

    def save_agent_settings(self, settings: dict) -> dict:
        """Persist agent role settings (raw keys live only in this file).

        Logs only providers — never the secret. Writing is atomic.

        Args:
            settings: dict — settings to store (normalized before write).

        Returns:
            dict — the normalized settings that were written.
        """
        clean = self.sanitize_agent_settings(settings)
        self._write_json(self.agent_settings_path, clean)
        providers = {r: c.get("provider") for r, c in clean.items()}
        logger.info("Agent settings saved (%s)", providers)
        return clean

    def save_workflow_artifact(self, workflow: dict) -> dict:
        """Write a workflow artifact under .openfde/workflows/<id>.json.

        Args:
            workflow: dict — artifact; must contain 'workflowId'.

        Returns:
            dict — the stored artifact.
        """
        self.workflows_dir.mkdir(exist_ok=True)
        wid = workflow.get("workflowId") or f"wf_{secrets.token_hex(5)}"
        workflow["workflowId"] = wid
        self._write_json(self.workflows_dir / f"{wid}.json", workflow)
        logger.info("Workflow artifact saved (%s, backend=%s, status=%s)",
                    wid, workflow.get("backend"), workflow.get("status"))
        return workflow

    def load_workflow_artifact(self, workflow_id: str) -> dict:
        """Load a single workflow artifact by id, or None.

        Args:
            workflow_id: str — workflow id.

        Returns:
            dict | None — the artifact, or None.
        """
        path = self.workflows_dir / f"{workflow_id}.json"
        if not path.exists():
            return None
        return self._read_json(path, None)

    def list_workflow_artifacts(self) -> list:
        """List workflow artifacts (newest-first by createdAt).

        Returns:
            list[dict] — workflow artifacts.
        """
        if not self.workflows_dir.exists():
            return []
        out: list = []
        for p in self.workflows_dir.glob("*.json"):
            data = self._read_json(p, None)
            if isinstance(data, dict):
                out.append(data)
        out.sort(key=lambda w: w.get("createdAt", ""), reverse=True)
        return out

    # ------------------------------------------------------------------ #
    #  Approvals (protected-scope gate, Step 20)                          #
    # ------------------------------------------------------------------ #

    def load_approvals(self) -> list:
        """Load approval requests (newest-first).

        Returns:
            list[dict] — approval records.
        """
        raw = self._read_json(self.approvals_path, [])
        return raw if isinstance(raw, list) else []

    def upsert_approval(self, approval: dict, cap: int = 100) -> dict:
        """Insert or update an approval by approvalId, keeping the most recent.

        Args:
            approval: dict — approval record; must contain 'approvalId'.
            cap: int — maximum approvals retained.

        Returns:
            dict — the stored approval.
        """
        approvals = self.load_approvals()
        aid = approval.get("approvalId")
        approvals = [a for a in approvals if a.get("approvalId") != aid]
        approvals.insert(0, approval)
        self._write_json(self.approvals_path, approvals[:cap])
        return approval

    def get_approval(self, approval_id: str) -> dict:
        """Return a single approval by id, or None.

        Args:
            approval_id: str — approval id.

        Returns:
            dict | None — the approval, or None.
        """
        for a in self.load_approvals():
            if a.get("approvalId") == approval_id:
                return a
        return None

    # ------------------------------------------------------------------ #
    #  Project log (conversation ledger)                                   #
    # ------------------------------------------------------------------ #

    def load_project_log(self) -> list:
        """Load all conversation-ledger entries, oldest-first.

        Returns:
            list[dict] — normalized ledger entries in chronological order.
        """
        if not self.project_log_path.exists():
            return []
        entries: list = []
        try:
            with open(self.project_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError as exc:
                            logger.warning("Skipping corrupt ledger line: %s", exc)
        except OSError as exc:
            logger.error("Failed to read project log: %s", exc)
        return entries

    def render_project_md(self) -> str:
        """Render the current ledger (+ project metadata) as project.md markdown.

        Returns:
            str — generated markdown document.
        """
        return _render_project_log_md(self.load_project_log(), self.load_project())

    def append_project_log_entry(self, entry: dict, repo_root: Path) -> dict:
        """Normalize and append a ledger entry, then regenerate project.md.

        The structured entry is appended to project_log.jsonl and the repo-root
        project.md is regenerated atomically from the full structured ledger
        (never hand-edited / mixed).

        Args:
            entry: dict — raw ledger entry (see normalize_ledger_entry).
            repo_root: Path — repository root where project.md is written.

        Returns:
            dict — the normalized entry that was written.
        """
        normalized = normalize_ledger_entry(entry)
        try:
            with open(self.project_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(normalized) + "\n")
            logger.info("Ledger entry appended (role=%s id=%s)", normalized["role"], normalized["id"])
        except OSError as exc:
            logger.error("Failed to append ledger entry: %s", exc)
            return normalized

        # Regenerate project.md from the full structured ledger.
        md = _render_project_log_md(self.load_project_log(), self.load_project())
        project_md_path = repo_root / "project.md"
        tmp = repo_root / ".project_ledger_md.tmp"
        try:
            tmp.write_text(md, encoding="utf-8")
            os.replace(tmp, project_md_path)
            logger.info("project.md written")
        except OSError as exc:
            logger.error("Failed to write project.md: %s", exc)

        return normalized


# ------------------------------------------------------------------ #
#  PROJECT.md renderer                                                 #
# ------------------------------------------------------------------ #

def _render_project_md(data: dict) -> str:
    """Render project data as a Markdown document.

    Args:
        data: dict — project data with 'name', 'description', 'entries'

    Returns:
        str — Markdown-formatted document body
    """
    name    = data.get("name") or "OpenFDE Project"
    desc    = data.get("description") or ""
    entries: list = data.get("entries") or []

    lines = [f"# {name}", ""]
    if desc:
        lines += [desc, ""]

    if entries:
        lines += ["## Project Entries", ""]
        for e in entries:
            ts   = (e.get("timestamp") or "")[:10]
            kind = e.get("type") or "note"
            text = e.get("text") or e.get("content") or ""
            lines.append(f"- **{kind}** ({ts}): {text}")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  project.md (conversation ledger) renderer                           #
# ------------------------------------------------------------------ #

def _render_project_log_md(entries: list, project: dict) -> str:
    """Render the conversation ledger as the repo-root project.md document.

    Output style (matches the working-conversation format)::

        # Project Ledger

        Problem Statement:
        <project description>

        architect: <title>
        <body>

        sr dev: <title>
        <body>

    Args:
        entries: list[dict] — normalized ledger entries, oldest-first.
        project: dict — project metadata ({name, description, entries}).

    Returns:
        str — generated markdown document.
    """
    name = (project.get("name") or "").strip()
    desc = (project.get("description") or "").strip()

    lines: list = []
    a = lines.append

    a(f"# {name} — Project Ledger" if name else "# Project Ledger")
    a("")
    a("*Generated by OpenFDE from `.openfde/project_log.jsonl`. Do not edit by hand.*")
    a("")
    a("Problem Statement:")
    a(desc if desc else "_(not set — describe the project in OpenFDE)_")
    a("")

    if not entries:
        a("---")
        a("")
        a("_No ledger entries yet. Run Execute to record the first architect/sr-dev exchange._")
        a("")
        return "\n".join(lines)

    for e in entries:
        a("---")
        a("")
        label = _LEDGER_LABEL.get(e.get("role"), "human")
        title = (e.get("title") or "").strip()
        a(f"{label}: {title}" if title else f"{label}:")
        a("")

        summary = (e.get("summary") or "").strip()
        if summary:
            a(f"*{summary}*")
            a("")

        body = (e.get("body") or "").rstrip()
        if body:
            a(body)
            a("")

        # Traceability footer — refs that link this entry back to the model.
        refs: list = []
        ts = (e.get("timestamp") or "")[:19].replace("T", " ")
        if ts:
            refs.append(ts)
        if e.get("eventId"):
            refs.append(f"event {e['eventId']}")
        boxes = e.get("boxIds") or []
        if boxes:
            refs.append(f"{len(boxes)} box(es)")
        files = e.get("filePaths") or []
        if files:
            refs.append(f"{len(files)} file(s)")
        if refs:
            a(f"_{' · '.join(refs)}_")
            a("")

    return "\n".join(lines)
