"""
openfde/external_council.py — coordinator for the external Codex + Claude-Code council (v1).

Codex (Architect + Verifier) turns product/architecture intent into a TRACKED OpenFDE work item;
Claude Code (Senior Dev) implements and commits; Codex verifies the commit. This module is the thin
coordinator that binds the council bus (``.openfde/council/`` — see :mod:`openfde.council_bus`) to
OpenFDE's OWN episode + OpenPM stores. It never mints a parallel council id: the work item is keyed
on a real ``episodeId`` + ``taskIds`` (+ ``boxIds``), so it can render inside OpenFDE unchanged.

Ownership (law): Codex starts the episode/tasks here and records verdicts; **Codex never commits**.
Claude Code makes every commit, stamped with ``OpenFDE-*`` trailers (see
:func:`openfde.council_bus.build_trailers`). Durability rides those trailers, not committed bus prose.

v1 is pure helpers (+ thin API wrappers in the server). No daemon: CC is triggered by a human/loop
reading the bus, not auto-fired.
"""

from __future__ import annotations

import secrets
import subprocess
from datetime import datetime, timezone

from openfde import council_bus
from openfde.episode_summary import enrich_episode

EXTERNAL_COUNCIL_KIND = "external-council"
EXTERNAL_COUNCIL_SOURCE = "external-council"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title_from_objective(objective: str) -> str:
    line = next((ln.strip() for ln in (objective or "").splitlines() if ln.strip()), "")
    if not line:
        return "External council work"
    return (line[:59].rstrip() + "…") if len(line) > 60 else line


def acceptance_lines(acceptance) -> list:
    """Acceptance criteria as a clean bullet list — accepts a list/tuple, or a newline/bullet string."""
    if isinstance(acceptance, (list, tuple)):
        return [str(a).strip() for a in acceptance if str(a).strip()]
    out = []
    for ln in str(acceptance or "").splitlines():
        s = ln.strip().lstrip("-*•").strip()
        if s:
            out.append(s)
    return out


def _work_item_body(objective: str, acceptance: list, architecture_notes) -> str:
    parts = []
    if (objective or "").strip():
        parts.append("## Objective\n\n" + objective.strip())
    if acceptance:
        parts.append("## Acceptance criteria\n\n" + "\n".join(f"- {a}" for a in acceptance))
    if (architecture_notes or "").strip() if isinstance(architecture_notes, str) else architecture_notes:
        parts.append("## Architecture notes\n\n" + str(architecture_notes).strip())
    return "\n\n".join(parts)


def _upsert_task_item(repo_root, *, episode_id, task_ids, box_ids, status, run_id="",
                      objective="", acceptance=None, architecture_notes=None, latest_commit=""):
    """Insert or update the work item for ``episode_id`` in ``TASKS.md`` (other items preserved)."""
    header = {
        "episodeId": episode_id, "runId": run_id or "",
        "taskIds": list(task_ids or []), "boxIds": list(box_ids or []),
        "status": status, "architect": "codex", "seniorDev": "claude-code",
        "verifier": "codex", "latestCommit": latest_commit or "",
    }
    new_item = council_bus.render_front_matter(header, _work_item_body(objective, acceptance or [],
                                                                       architecture_notes)).rstrip("\n")
    items = council_bus.parse_work_items(council_bus.read_bus_file(repo_root, "tasks"))
    rebuilt, replaced = [], False
    for it in items:
        if it["header"].get("episodeId") == episode_id:
            rebuilt.append(new_item)
            replaced = True
        else:
            rebuilt.append(council_bus.render_front_matter(it["header"], it["body"]).rstrip("\n"))
    if not replaced:
        rebuilt.append(new_item)
    council_bus.write_bus_file(repo_root, "tasks", "\n\n".join(rebuilt) + "\n")


def create_external_council_work(persistence, *, objective, acceptance,
                                 architecture_notes=None, box_ids=None, task_titles=None) -> dict:
    """Codex starts a tracked external-council work item.

    Creates ONE OpenFDE episode (kind ``external-council``) + N OpenPM tasks (from ``task_titles``,
    else the acceptance bullets), then writes a ``.openfde/council/TASKS.md`` work item bound to the
    REAL ids with ``status: READY_FOR_CC``. Never invents a council id.

    Returns ``{episodeId, taskIds, boxIds, status, title}``.
    """
    box_ids = [b for b in (box_ids or []) if b]
    acc = acceptance_lines(acceptance)
    objective = (objective or "").strip()
    titles = [str(t).strip() for t in (task_titles or acc) if str(t).strip()] or [_title_from_objective(objective)]
    now = _now()

    # 1) Episode — OpenFDE's own id scheme; product signal so it lands on the Story spine + rail as
    #    soon as it exists (before any commit). enrich_episode adds sequence/tag without overwriting.
    episode_id = "episode_" + secrets.token_hex(6)
    summary = ("Acceptance: " + "; ".join(acc)) if acc else (objective[:200] or "External council work.")
    episode = {
        "episodeId": episode_id, "createdAt": now, "updatedAt": now,
        "prompt": objective or "External council work", "kind": EXTERNAL_COUNCIL_KIND,
        "status": "open", "runIds": [], "eventIds": [], "projectEntryIds": [], "commitShas": [],
        "files": [], "boxIds": list(box_ids),
        "title": _title_from_objective(objective), "summary": summary, "signal": "product",
        "externalCouncil": {"architect": "codex", "seniorDev": "claude-code", "verifier": "codex",
                            "acceptance": acc, "architectureNotes": architecture_notes or ""},
    }
    seqs = [e.get("sequence") or 0 for e in persistence.load_episodes()]
    enrich_episode(episode, max(seqs) if seqs else 0)
    persistence.upsert_episode(episode)

    # 2) OpenPM tasks — To Do, source-tagged, bound to the episode (+ boxes). Shown immediately.
    tasks = persistence.load_tasks()
    task_ids = []
    for title in titles:
        tid = "task_" + secrets.token_hex(5)
        task_ids.append(tid)
        tasks.append({
            "id": tid, "title": title, "description": "", "column": "todo",
            "verificationStatus": "pending", "source": EXTERNAL_COUNCIL_SOURCE,
            "episodeId": episode_id, "linkedBoxIds": list(box_ids), "files": [], "commitSha": None,
            "episodeTag": episode.get("tag", ""), "promptTitle": episode.get("title", ""),
            "promptLabel": episode.get("title", ""),
        })
    persistence.save_tasks(tasks)

    # 3) TASKS.md work item → READY_FOR_CC, bound to the real ids.
    _upsert_task_item(persistence.openfde_dir.parent, episode_id=episode_id, task_ids=task_ids,
                      box_ids=box_ids, status=council_bus.STATUS_READY_FOR_CC, objective=objective,
                      acceptance=acc, architecture_notes=architecture_notes)

    return {"episodeId": episode_id, "taskIds": task_ids, "boxIds": list(box_ids),
            "status": council_bus.STATUS_READY_FOR_CC, "title": episode["title"]}


def record_codex_verdict(repo_root, *, episode_id, commit_sha, status, findings=""):
    """Codex records its independent verdict on the latest CC commit.

    Appends to ``CODEX.md`` and sets the ``TASKS.md`` work item's status to ``VERIFIED`` or
    ``CHANGES_REQUESTED`` (+ its ``latestCommit``). Touches only the gitignored bus — **Codex never
    commits**. Returns ``{status, episodeId, commitSha, found}``.
    """
    if status not in (council_bus.STATUS_VERIFIED, council_bus.STATUS_CHANGES_REQUESTED):
        raise ValueError(f"verdict must be VERIFIED or CHANGES_REQUESTED, got {status!r}")
    short = (commit_sha or "")[:7]
    heading = f"{episode_id} · {status}" + (f" · {short}" if short else "")
    body = f"commit: {commit_sha or '(none)'}\n\n{(findings or '').strip()}".rstrip()
    council_bus.append_bus_entry(repo_root, "codex", heading, body)

    items = council_bus.parse_work_items(council_bus.read_bus_file(repo_root, "tasks"))
    rebuilt, found = [], False
    for it in items:
        h = it["header"]
        if h.get("episodeId") == episode_id:
            h = dict(h)
            h["status"] = status
            if commit_sha:
                h["latestCommit"] = commit_sha
            found = True
        rebuilt.append(council_bus.render_front_matter(h, it["body"]).rstrip("\n"))
    if found:
        council_bus.write_bus_file(repo_root, "tasks", "\n\n".join(rebuilt) + "\n")
    return {"status": status, "episodeId": episode_id, "commitSha": commit_sha, "found": found}


def _head_commit(repo_root):
    """(message, sha) of HEAD, or ('', '') when not a git repo / no commits."""
    try:
        sha = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        msg = subprocess.run(["git", "-C", str(repo_root), "log", "-1", "--pretty=%B"],
                             capture_output=True, text=True, timeout=5)
        if sha.returncode == 0 and msg.returncode == 0:
            return msg.stdout, sha.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "", ""


def _last_entry(channel_md: str) -> str:
    """The most recent ``## …`` entry block in an append-log channel (``""`` if none)."""
    text = channel_md or ""
    idx = text.rfind("\n## ")
    if idx != -1:
        return text[idx:].strip()
    return text.strip() if text.strip().startswith("## ") else ""


def read_latest_handoff(repo_root) -> dict:
    """The latest CC handoff: the tail of ``CLAUDE.md`` plus the HEAD commit's ``OpenFDE-*`` trailer
    binding (when a git repo). Returns ``{latestEntry, binding, headSha, claude}``."""
    claude = council_bus.read_bus_file(repo_root, "claude")
    msg, head_sha = _head_commit(repo_root)
    return {
        "latestEntry": _last_entry(claude),
        "binding": council_bus.binding_from_commit(msg) if msg else {},
        "headSha": head_sha,
        "claude": claude.strip(),
    }


def read_status(repo_root) -> dict:
    """Bus state for ``GET /api/external-council/status``: the ``TASKS.md`` work items + the latest
    handoff. Pure read of the gitignored bus."""
    items = council_bus.parse_work_items(council_bus.read_bus_file(repo_root, "tasks"))
    return {
        "workItems": [{"header": it["header"], "body": it["body"]} for it in items],
        "handoff": read_latest_handoff(repo_root),
    }
