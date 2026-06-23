"""
openfde/council_bus.py — the external Codex + Claude-Code council shared bus.

Codex (Architect + Verifier) and Claude Code (Senior Dev) hand work back and forth through a
repo-local coordination layer under ``.openfde/council/`` — four Markdown files, each active work
item carrying a small machine-readable header. This module reads/writes those files and parses the
header, which binds every item to **existing OpenFDE ids** (``episodeId`` / ``runId`` / ``taskIds`` /
``boxIds`` / ``latestCommit``) — never a new council id system, so the bus can later render inside
OpenFDE without migration.

**Files (single-writer per file):**
  - ``TASKS.md``     — Codex-owned: objective + machine-checkable acceptance + authoritative status.
  - ``CLAUDE.md``    — Claude-Code-owned: per-round handoff receipts (what was built + checks run).
  - ``CODEX.md``     — Codex-owned: per-round independent verdict + change requests.
  - ``DECISIONS.md`` — append-only durable record (CC proposes, Codex ratifies).

**Durability law (reconciled with the repo's "never commit .md" rule):** the bus files are LIVE
coordination — gitignored, like ``state.json``. The git-visible, durable binding rides on each
commit's ``OpenFDE-*`` trailers (reused from :mod:`openfde.git_timeline` /
:mod:`openfde.episode_commits`), NOT on committing the prose. Claude Code commits the code; Codex
verifies the commit and never commits.

Lightweight: standard library only. No daemon, no network, no external dependencies.
"""

from __future__ import annotations

import os
import re

# ── Layout ───────────────────────────────────────────────────────────────────
COUNCIL_DIRNAME = os.path.join(".openfde", "council")
TASKS_FILE, CLAUDE_FILE, CODEX_FILE, DECISIONS_FILE = (
    "TASKS.md", "CLAUDE.md", "CODEX.md", "DECISIONS.md")
_FILE_BY_KEY = {"tasks": TASKS_FILE, "claude": CLAUDE_FILE,
                "codex": CODEX_FILE, "decisions": DECISIONS_FILE}

# ── Status state machine — the single source of truth + the loop's done-detector ──
STATUS_READY_FOR_CC = "READY_FOR_CC"
STATUS_CLAUDE_WORKING = "CLAUDE_WORKING"
STATUS_READY_FOR_CODEX_VERIFICATION = "READY_FOR_CODEX_VERIFICATION"
STATUS_CHANGES_REQUESTED = "CHANGES_REQUESTED"
STATUS_VERIFIED = "VERIFIED"
STATUS_BLOCKED_NEEDS_ARCHITECT = "BLOCKED_NEEDS_ARCHITECT"
STATUS_BLOCKED_NEEDS_HUMAN = "BLOCKED_NEEDS_HUMAN"
STATUSES = (STATUS_READY_FOR_CC, STATUS_CLAUDE_WORKING, STATUS_READY_FOR_CODEX_VERIFICATION,
            STATUS_CHANGES_REQUESTED, STATUS_VERIFIED, STATUS_BLOCKED_NEEDS_ARCHITECT,
            STATUS_BLOCKED_NEEDS_HUMAN)
# Terminal: the loop stops here. VERIFIED = success; the BLOCKED_* states escalate.
TERMINAL_STATUSES = (STATUS_VERIFIED, STATUS_BLOCKED_NEEDS_HUMAN)

# ── Header schema ─────────────────────────────────────────────────────────────
# List-valued fields render as a YAML block sequence; the rest are scalars. The order is fixed so
# round-tripping a header is stable. `episodeId/runId/taskIds/boxIds/latestCommit` are OpenFDE ids
# and are ALWAYS preserved verbatim — this module never mints one.
_LIST_FIELDS = ("taskIds", "boxIds")
_FIELD_ORDER = ("episodeId", "runId", "taskIds", "boxIds", "status",
                "architect", "seniorDev", "verifier", "latestCommit")
_FENCE = "---"
_KV_RE = re.compile(r"^([A-Za-z][\w-]*):\s*(.*)$")
_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")


def _parse_header_lines(hlines) -> dict:
    header: dict = {}
    key = None
    for raw in hlines:
        if not raw.strip():
            continue
        m_item = _ITEM_RE.match(raw)
        if m_item and key in _LIST_FIELDS:
            header.setdefault(key, []).append(m_item.group(1).strip())
            continue
        m_kv = _KV_RE.match(raw)
        if not m_kv:
            continue
        k, v = m_kv.group(1), m_kv.group(2).strip()
        key = k
        if k in _LIST_FIELDS:
            header[k] = [x.strip() for x in v.split(",") if x.strip()] if v else []
        else:
            header[k] = v
    return header


def parse_front_matter(text: str):
    """Parse a leading ``---`` front-matter block → ``(header_dict, body_text)``.

    Returns ``({}, text)`` when there is no front matter. Supports scalars (``key: value``) and
    simple block/inline lists. Ids are preserved VERBATIM — never normalized or regenerated.
    """
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}, text or ""
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == _FENCE), None)
    if end is None:
        return {}, text or ""
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return _parse_header_lines(lines[1:end]), body


def render_front_matter(header: dict, body: str = "") -> str:
    """Serialize ``(header, body)`` back to a ``---`` front-matter document. Stable field order;
    list fields render as block sequences; any non-canonical keys are preserved after the known
    ones. No id is added that was not already in ``header``."""
    out = [_FENCE]
    emitted = set()

    def _emit(k, v):
        if k in _LIST_FIELDS or isinstance(v, list):
            out.append(f"{k}:")
            for x in (v or []):
                out.append(f"  - {x}")
        else:
            out.append(f"{k}: {v if v is not None else ''}")
        emitted.add(k)

    for k in _FIELD_ORDER:
        if k in header:
            _emit(k, header.get(k))
    for k, v in header.items():           # preserve any extra keys
        if k not in emitted:
            _emit(k, v)
    out.append(_FENCE)
    if (body or "").strip():
        out.append("")
        out.append(body.rstrip("\n"))
    return "\n".join(out) + "\n"


def parse_work_items(tasks_md: str):
    """Split a ``TASKS.md`` into work items — each a ``{"header", "body"}`` from one ``---`` block,
    order-preserving. Prose outside a header block is ignored. Ids are preserved verbatim."""
    lines = (tasks_md or "").splitlines()
    items, i, n = [], 0, len(lines)
    while i < n:
        if lines[i].strip() == _FENCE:
            j = i + 1
            while j < n and lines[j].strip() != _FENCE:
                j += 1
            if j < n:                                   # a closing fence exists
                header = _parse_header_lines(lines[i + 1:j])
                k = j + 1
                while k < n and lines[k].strip() != _FENCE:
                    k += 1
                body = "\n".join(lines[j + 1:k]).strip("\n")
                items.append({"header": header, "body": body})
                i = k
                continue
        i += 1
    return items


def set_status(item_md: str, new_status: str) -> str:
    """Return the work-item document with its header ``status`` set to ``new_status`` (everything
    else — ids, body, extra fields — preserved). Raises ``ValueError`` on an unknown status."""
    if new_status not in STATUSES:
        raise ValueError(f"unknown council status: {new_status!r}")
    header, body = parse_front_matter(item_md)
    header["status"] = new_status
    return render_front_matter(header, body)


# ── File I/O (the live coordination layer) ───────────────────────────────────
def council_paths(repo_root) -> dict:
    """Absolute paths for the bus under ``<repo_root>/.openfde/council/``."""
    base = os.path.join(str(repo_root), COUNCIL_DIRNAME)
    paths = {"dir": base}
    for key, name in _FILE_BY_KEY.items():
        paths[key] = os.path.join(base, name)
    return paths


def ensure_council_dir(repo_root) -> str:
    base = council_paths(repo_root)["dir"]
    os.makedirs(base, exist_ok=True)
    return base


def read_bus_file(repo_root, key: str) -> str:
    """Read one bus file (``key`` in tasks|claude|codex|decisions); ``""`` if absent."""
    try:
        with open(council_paths(repo_root)[key], encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def write_bus_file(repo_root, key: str, text: str) -> None:
    ensure_council_dir(repo_root)
    with open(council_paths(repo_root)[key], "w", encoding="utf-8") as f:
        f.write(text)


def append_bus_entry(repo_root, key: str, heading: str, body: str) -> None:
    """Append a round-marked entry (``## <heading>``) to an append-log channel
    (``claude`` / ``codex`` / ``decisions``), preserving prior entries."""
    cur = read_bus_file(repo_root, key).rstrip()
    entry = f"## {heading}\n\n{(body or '').rstrip()}\n"
    write_bus_file(repo_root, key, (cur + "\n\n" + entry) if cur else entry)


# ── Commit trailers — the git-visible, durable binding (reuse OpenFDE's parser) ──
def build_trailers(*, episode_id: str = None, task_ids=(), run_id: str = None,
                   role: str = None, handoff: str = None) -> dict:
    """An ``OpenFDE-*`` trailer dict binding a commit to existing OpenFDE ids — composes with
    ``git_timeline.git_commit_paths(..., trailers=...)``. Only the ids/fields PASSED are emitted;
    nothing is invented."""
    t: dict = {}
    if episode_id:
        t["OpenFDE-Episode"] = episode_id
    ids = [x for x in (task_ids or []) if x]
    if ids:
        t["OpenFDE-Tasks"] = ", ".join(ids)
    if run_id:
        t["OpenFDE-Run"] = run_id
    if role:
        t["OpenFDE-Role"] = role
    if handoff:
        t["OpenFDE-Handoff"] = handoff
    return t


def trailer_block(trailers: dict) -> str:
    """Render a trailer dict to ``Key: value`` lines (the tail of a commit message)."""
    return "\n".join(f"{k}: {v}" for k, v in (trailers or {}).items())


def binding_from_commit(commit_body: str) -> dict:
    """Recover the council binding from a commit body's ``OpenFDE-*`` trailers — reusing the
    existing parsers (``git_timeline._parse_openfde_trailers`` +
    ``episode_commits.episode_ids_from_trailers``). No id is generated; absent fields come back
    empty. This is what lets the external council later render inside OpenFDE."""
    from openfde.episode_commits import episode_ids_from_trailers
    from openfde.git_timeline import _parse_openfde_trailers
    tr = _parse_openfde_trailers(commit_body)
    tasks = [x.strip() for x in (tr.get("OpenFDE-Tasks") or "").split(",") if x.strip()]
    return {
        "episodeIds": episode_ids_from_trailers(tr),
        "taskIds": tasks,
        "runId": tr.get("OpenFDE-Run") or "",
        "role": tr.get("OpenFDE-Role") or "",
        "handoff": tr.get("OpenFDE-Handoff") or "",
    }
