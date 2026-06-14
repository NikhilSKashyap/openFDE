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
import re
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


def _default_verify(root) -> dict:
    """The standard gate: run the repo's discovered local checks (openfde.verify)."""
    from openfde.verify import run_verification
    return run_verification(root)


def land_episode(root, persistence, episode: dict, *, auto: bool, allow_llm: bool = False,
                 run_verify=None) -> dict:
    """Commit an episode's attributed files (scoped) and update its state.

    The episode's diff is clustered into 1–N **logical changes** (``cluster_changes``) and each is
    committed separately — so a prompt becomes *one commit (and one OpenPM task) per logical
    change*, not a single lump. ``allow_llm`` enables the model-based grouping (slow subprocess →
    the caller must run this in an executor); otherwise a deterministic by-scope split is used.

    Args:
        root: Path — repository root.
        persistence: Persistence — episode + event store.
        episode: dict — the episode to land (mutated in place + re-persisted).
        auto: bool — True for automatic triggers (apply ambiguity guards), False for an
            explicit user Land (force the scoped commit for this episode).
        allow_llm: bool — cluster the diff with the local LLM (offload to an executor); False
            uses the fast deterministic by-scope split.
        run_verify: optional (root) -> evidence — the Verify gate (injectable for tests);
            defaults to ``openfde.verify.run_verification`` and is slow (runs the repo's
            checks) → callers should already be in an executor. Evidence lands on
            ``episode["verify"]``. **Auto**-land blocks on a failed required check
            (→ needs_manual_land, nothing committed); an explicit user Land proceeds —
            the escape hatch — with the failure recorded and visible. No checks
            discovered → *skipped* evidence is recorded, never silent success.

    Returns:
        dict — {ok, committed, sha?, shortSha?, status, reason, episode, files, commits:[…],
                broadcasts:[…], needsWholeTree?}. ``commits`` lists each landed logical change
                ({sha, shortSha, title, files}). ``sha``/``shortSha`` are the *last* commit (back-
                compat). ``needsWholeTree`` is set when a *manual* land has no attributed files,
                signalling the caller to fall back to a whole-tree commit (the "Manual changes" bucket).
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

    # Verify gate — collect receipts before anything is committed. Runs after the cheap
    # guards (so we never pay for checks on a no-op land) and before the transient
    # auto_landing state. The evidence rides the episode into every surface.
    evidence = (run_verify or _default_verify)(root)
    episode["verify"] = evidence
    if evidence.get("status") == "failed":
        failed = "; ".join(c.get("summary") or c.get("id", "check")
                           for c in (evidence.get("checks") or [])
                           if c.get("required") and c.get("status") == "failed")[:200]
        if auto:
            # Blocked: leave the work uncommitted and park the episode for review.
            return _set(NEEDS_MANUAL, "verification failed: " + (failed or "required check failed"))
        # Explicit user Land = the escape hatch: proceed, but the failure stays
        # recorded on the episode (red evidence in Review/OpenPM), never hidden.

    if auto:
        # Transient — shows "auto-landing" on the card while the commits run.
        episode["status"] = AUTO_LANDING
        episode["updatedAt"] = now
        persistence.upsert_episode(episode)
        broadcasts.append({"type": "episode_updated", "episode": episode})

    # Cluster the scoped diff into logical changes, then commit each separately — one commit
    # (and one OpenPM task) per logical change. Each cluster's still-dirty files are committed
    # scoped; the per-commit title is stored on the episode so every surface reads it durably.
    from openfde.episode_llm_summary import cluster_changes
    clusters = cluster_changes(episode, scoped, providers=(None if allow_llm else []))

    landed = []                                    # [(commit, cluster)]
    still_dirty = set(dirty)
    for cl in clusters:
        cl_files = [f for f in cl["files"] if f in still_dirty]
        if not cl_files:
            continue
        _subj, body = commit_message(episode, cl_files)
        commit = git_commit_paths(root, cl["message"], cl_files, detail=body, trailers=_trailers(episode))
        if commit.get("committed"):
            landed.append((commit, cl))
            still_dirty -= set(commit.get("files") or [])

    if not landed:
        return _set(NEEDS_MANUAL, "scoped commit produced no changes")

    new_shas = [c["sha"] for c, _ in landed]
    committed_files = sorted({f for c, _ in landed for f in (c.get("files") or [])})
    meta = dict(episode.get("commitMeta") or {})
    for commit, cl in landed:
        meta[commit["sha"]] = {"title": cl["title"],
                               "summary": re.sub(r"^openfde:\s*", "", commit["summary"]).strip()}
    episode["commitMeta"] = meta
    episode["commitShas"] = list(dict.fromkeys((episode.get("commitShas") or []) + new_shas))
    episode["files"] = sorted(set(files + committed_files))
    episode["status"] = LANDED
    # The issue card (if any) follows its episode: landed work is Done.
    try:
        persistence.move_tasks_for_episode(episode.get("episodeId"), "done", "passed")
    except Exception:  # noqa: BLE001 — board sync must never block a land
        logger.warning("could not move cards for landed episode %s",
                       episode.get("episodeId"))
    # Evidence overrides classification: an episode that lands real commits is not
    # operational chatter, whatever the LLM summarizer guessed — the mislabel hid
    # episodes from the rail and hard-blocked their PR readiness (observed live).
    if episode.get("signal") == "operational" or (episode.get("storyFacts") or {}).get("operational"):
        episode["signal"] = "product"
        if isinstance(episode.get("storyFacts"), dict):
            episode["storyFacts"]["operational"] = False
        episode["reclassifiedBy"] = "landed-commits"
    episode["updatedAt"] = _now()
    persistence.upsert_episode(episode)

    tag = episode.get("tag") or episode["episodeId"]
    for commit, cl in landed:
        ce = persistence.append_event({
            "type": "commit_created",
            "payload": {"sha": commit["sha"], "shortSha": commit["shortSha"],
                        "summary": commit["summary"], "episodeId": episode["episodeId"],
                        "fileCount": len(commit.get("files", [])),
                        "detail": (f"Auto-landed {commit['shortSha']} for {tag}" if auto
                                   else f"Landed {commit['shortSha']} for {tag}")},
        })
        broadcasts.append({"type": "event_appended", "event": ce})
        broadcasts.append({
            "type": "commit_created", "sha": commit["sha"], "shortSha": commit["shortSha"],
            "summary": commit["summary"], "episodeId": episode["episodeId"],
            "episodeTag": episode.get("tag"), "promptTitle": episode.get("title"),
            "sequence": episode.get("sequence"),
            "displayTitle": cl["title"], "displaySummary": meta[commit["sha"]]["summary"],
            "promptLabel": episode.get("title") or (episode.get("prompt") or "").split("\n")[0][:48],
            "files": commit.get("files", []),
        })
    broadcasts.append({"type": "episode_updated", "episode": episode})
    logger.info("Auto-landed %s → %d commit[s] (%d file[s])",
                episode["episodeId"], len(landed), len(committed_files))
    last = landed[-1][0]
    return {"ok": True, "committed": True, "sha": last["sha"], "shortSha": last["shortSha"],
            "status": LANDED, "reason": None, "episode": episode,
            "commits": [{"sha": c["sha"], "shortSha": c["shortSha"], "title": cl["title"],
                         "files": c.get("files", [])} for c, cl in landed],
            "files": committed_files, "broadcasts": broadcasts}


def land_on_verify(root, persistence, episode: dict, *, run_verify=None,
                   allow_llm: bool = False) -> dict:
    """Auto-land ONLY on a deterministic GREEN verify. Product law: *green verify lands
    automatically; red verify waits.*

    This is STRICTER than land_episode's in-line gate (which blocks only ``failed``):
    here a verify that is failed / skipped / missing / anything-but-``passed`` does NOT
    land — the manual "Land changes" path stays. No LLM decides readiness; the gate is
    the deterministic verify status. On green it delegates to :func:`land_episode`
    (``auto=True``), so the scoped-ownership, multi-episode ambiguity, and
    ``.openfde``/ignored-file exclusions all apply, and a clean land syncs the OpenPM
    card to Done/passed. A failed/skipped verify leaves the card where it is (Review/
    Testing) and preserves the repair-hatch path.

    Returns:
        dict — the land_episode result on green; otherwise
        ``{ok, committed: False, status, reason, episode, files: [], broadcasts: []}``
        with the verify evidence recorded on the episode.
    """
    evidence = (run_verify or _default_verify)(root)
    episode["verify"] = evidence
    status = (evidence or {}).get("status")
    if status != "passed":
        # Not green → never auto-land. Red waits; skipped/missing waits; never guess.
        episode["updatedAt"] = _now()
        persistence.upsert_episode(episode)
        return {"ok": True, "committed": False, "status": episode.get("status"),
                "reason": f"verify {status or 'missing'} — green verify required to auto-land",
                "episode": episode, "files": [], "broadcasts": []}
    # Green: hand to the scoped lander; inject the receipt so it does not re-run verify.
    return land_episode(root, persistence, episode, auto=True, allow_llm=allow_llm,
                        run_verify=lambda _r: evidence)
