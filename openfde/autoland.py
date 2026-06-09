"""
openfde/autoland.py — Auto-Land core (scoped, deterministic, broadcast-agnostic).

OpenFDE moves from *Review Then Land* to **Auto-Land on prompt completion**: when an
agent finishes and file activity has settled, OpenFDE commits the episode's reviewed
set automatically — but **only the files attributed to that episode** (via the scoped
:func:`git_commit_paths`), never sweeping unrelated dirty files into the prompt.

This module is the shared core used by all three triggers — the `openfde cc/codex`
**wrapper** (a separate process), **passive capture**, and the **council/in-app**
reconcile. It is **synchronous and has no WebSocket dependency**: it mutates the episode
store + git, and *returns* the broadcast messages so an in-process caller (with a
`manager`) can emit them while the standalone wrapper simply ignores them.

Guardrails → episode lands as ``needs_manual_land`` (manual Land stays the fallback) when:
  - the episode has no attributed files,
  - the dirty files overlap another open/reviewing episode (ambiguous attribution),
  - the scoped commit fails.
And ``complete_no_changes`` when the episode's files exist but none are currently dirty.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("openfde.autoland")

# Episode lifecycle states (normalized).
OPEN = "open"
REVIEWING = "reviewing"
AUTO_LANDING = "auto_landing"
LANDED = "landed"
FAILED = "failed"
NEEDS_MANUAL = "needs_manual_land"
COMPLETE_NO_CHANGES = "complete_no_changes"

_OPEN_STATES = (OPEN, REVIEWING, AUTO_LANDING)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def commit_message(episode: dict, files) -> tuple:
    """Deterministic episode commit (subject, body).

    Subject ``openfde: <episode title>``; body carries the prompt, summary, and the
    scoped file manifest so the commit stands on its own later.
    """
    title = (episode.get("title") or "").strip()
    if not title:
        first = (episode.get("prompt") or "").strip().splitlines()[0] if (episode.get("prompt") or "").strip() else ""
        title = first[:60] or ("Manual changes" if episode.get("kind") == "manual" else "OpenFDE change")
    subject = (f"openfde: {title}")[:78]

    lines = []
    prompt = (episode.get("prompt") or "").strip()
    if prompt:
        lines.append("Prompt: " + (prompt if len(prompt) <= 600 else prompt[:600].rstrip() + " …"))
    if episode.get("summary"):
        lines.append("Summary: " + episode["summary"])
    files = list(files or [])
    if files:
        lines.append("Files:")
        lines += [f"- {p}" for p in files[:20]]
        if len(files) > 20:
            lines.append(f"- … +{len(files) - 20} more")
    return subject, "\n".join(lines)


def _trailers(episode: dict) -> dict:
    t = {"OpenFDE-Episode": episode["episodeId"]}
    rid = next((r for r in reversed(episode.get("runIds") or []) if r), None)
    if rid:
        t["OpenFDE-Run"] = rid
    pid = next((e for e in reversed(episode.get("projectEntryIds") or []) if e), None)
    if pid:
        t["OpenFDE-Project-Entry"] = pid
    return t


def land_episode(root, persistence, episode: dict, *, auto: bool) -> dict:
    """Commit an episode's attributed files (scoped) and update its state.

    Args:
        root: Path — repository root.
        persistence: Persistence — episode + event store.
        episode: dict — the episode to land (mutated in place + re-persisted).
        auto: bool — True for automatic triggers (apply ambiguity guards), False for an
            explicit user Land (force the scoped commit for this episode).

    Returns:
        dict — {ok, committed, sha?, shortSha?, status, reason, episode, files,
                broadcasts:[…], needsWholeTree?}. ``needsWholeTree`` is set when a
                *manual* land has no attributed files, signalling the caller to fall
                back to a whole-tree commit (the "Manual changes" bucket).
    """
    from openfde.git_timeline import dirty_paths, git_commit_paths

    now = _now()
    files = list(episode.get("files") or [])
    dirty = dirty_paths(root)
    scoped = [p for p in files if p in dirty]
    broadcasts = []

    def _set(status, reason=None):
        episode["status"] = status
        episode["updatedAt"] = _now()
        persistence.upsert_episode(episode)
        broadcasts.append({"type": "episode_updated", "episode": episode})
        return {"ok": True, "committed": False, "status": status, "reason": reason,
                "episode": episode, "files": [], "broadcasts": broadcasts}

    if not files:
        if auto:
            return _set(NEEDS_MANUAL, "episode has no attributed files")
        return {"ok": True, "committed": False, "status": episode.get("status"),
                "reason": "no attributed files", "episode": episode, "files": [],
                "broadcasts": [], "needsWholeTree": True}

    if not scoped:
        return _set(COMPLETE_NO_CHANGES, "no dirty files attributed to this episode")

    if auto:
        others = [e for e in persistence.load_episodes()
                  if e.get("episodeId") != episode.get("episodeId")
                  and e.get("status") in _OPEN_STATES]
        if any(set(scoped) & set(e.get("files") or []) for e in others):
            return _set(NEEDS_MANUAL, "dirty files overlap multiple episodes — review manually")
        # Transient — shows "auto-landing" on the card while the commit runs.
        episode["status"] = AUTO_LANDING
        episode["updatedAt"] = now
        persistence.upsert_episode(episode)
        broadcasts.append({"type": "episode_updated", "episode": episode})

    subject, body = commit_message(episode, scoped)
    commit = git_commit_paths(root, subject, scoped, detail=body, trailers=_trailers(episode))
    if not commit.get("committed"):
        return _set(NEEDS_MANUAL, commit.get("reason") or "scoped commit produced no changes")

    episode["commitShas"] = list(dict.fromkeys((episode.get("commitShas") or []) + [commit["sha"]]))
    episode["files"] = sorted(set(files + (commit.get("files") or [])))
    episode["status"] = LANDED
    episode["updatedAt"] = _now()
    persistence.upsert_episode(episode)

    ce = persistence.append_event({
        "type": "commit_created",
        "payload": {"sha": commit["sha"], "shortSha": commit["shortSha"],
                    "summary": commit["summary"], "episodeId": episode["episodeId"],
                    "fileCount": len(commit.get("files", [])),
                    "detail": f"Auto-landed {commit['shortSha']} for {episode.get('tag') or episode['episodeId']}"
                              if auto else f"Landed {commit['shortSha']} for {episode.get('tag') or episode['episodeId']}"},
    })
    broadcasts.append({"type": "event_appended", "event": ce})
    broadcasts.append({"type": "episode_updated", "episode": episode})
    from openfde.episode_summary import commit_display
    _dtitle, _dsummary = commit_display(episode.get("title"), episode.get("summary"), commit["summary"])
    broadcasts.append({
        "type": "commit_created", "sha": commit["sha"], "shortSha": commit["shortSha"],
        "summary": commit["summary"], "episodeId": episode["episodeId"],
        "episodeTag": episode.get("tag"), "promptTitle": episode.get("title"),
        "sequence": episode.get("sequence"),
        "displayTitle": _dtitle, "displaySummary": _dsummary,
        "promptLabel": episode.get("title") or (episode.get("prompt") or "").split("\n")[0][:48],
        "files": commit.get("files", []),
    })
    logger.info("Auto-landed %s → %s (%d file[s])", episode["episodeId"], commit["shortSha"], len(commit.get("files", [])))
    return {"ok": True, "committed": True, "sha": commit["sha"], "shortSha": commit["shortSha"],
            "status": LANDED, "reason": None, "episode": episode,
            "files": commit.get("files", []), "broadcasts": broadcasts}
