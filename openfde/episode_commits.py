"""
openfde/episode_commits.py — prompt → commit reconciliation (many prompts, one commit).

The product model is *many prompts → one commit*: a developer works through several prompts
(P1, P2, P3) and then lands a single commit; all three prompt cards should show that commit.
A commit can declare its episodes explicitly (the ``OpenFDE-Episodes`` trailer) or, for commits
made outside the OpenFDE land path, we *infer* the link from changed-file overlap and timing —
but always with an honest **confidence** so the UI can label inferred links as such.

Confidence ladder (highest first):
  - ``explicit``           — the commit's ``OpenFDE-Episodes``/``OpenFDE-Episode`` trailer names it.
  - ``high_file_overlap``  — no trailer, but the commit changed most/all of the episode's files.
  - ``time_file_inferred`` — weaker file overlap, but the episode was active near the commit time.
  - ``ambiguous``          — some file overlap, but neither strong nor temporally clear (NOT attached
                             by default — surfaced so a caller can show it faintly or ignore it).

Hard rule: a **0-file discussion episode is never attached without an explicit trailer.** Pure
discussion turns (no edits) must not vacuum up unrelated commits by coincidence.

This module is pure (no git, no I/O) so it is fully unit-testable; ``openfde.server`` /
``openfde.autoland`` call it and persist the verdicts onto episodes.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Confidence levels, strongest → weakest. Order matters: a stronger verdict wins.
EXPLICIT = "explicit"
HIGH_FILE_OVERLAP = "high_file_overlap"
TIME_FILE_INFERRED = "time_file_inferred"
AMBIGUOUS = "ambiguous"

_RANK = {EXPLICIT: 3, HIGH_FILE_OVERLAP: 2, TIME_FILE_INFERRED: 1, AMBIGUOUS: 0}

# An episode counts as "active near" a commit if the commit lands within this window of the
# episode's last activity. Batched commits can trail the prompts by a while, so be generous.
_NEAR_WINDOW_S = 6 * 3600
# Fraction of an episode's files a commit must touch to count as high overlap.
_HIGH_OVERLAP_FRACTION = 0.5


def episode_ids_from_trailers(trailers: dict) -> list:
    """Episode ids a commit declares, from ``OpenFDE-Episodes`` (plural, comma/space-separated)
    and/or ``OpenFDE-Episode`` (singular). De-duplicated, order-preserving.

    Args:
        trailers: dict — parsed ``OpenFDE-*`` commit trailers.

    Returns:
        list[str] — declared episode ids (possibly empty).
    """
    ids: list = []
    plural = (trailers or {}).get("OpenFDE-Episodes")
    if plural:
        ids += [t.strip() for t in re.split(r"[,\s]+", plural) if t.strip()]
    singular = (trailers or {}).get("OpenFDE-Episode")
    if singular and singular not in ids:
        ids.append(singular)
    # de-dup while preserving order
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _parse_ts(value) -> "datetime | None":
    """Best-effort parse of an ISO-8601 timestamp to an aware UTC datetime."""
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _episode_activity_ts(episode: dict) -> "datetime | None":
    """The episode's most recent activity time (updatedAt, else createdAt)."""
    return _parse_ts(episode.get("updatedAt")) or _parse_ts(episode.get("createdAt"))


def _temporally_near(episode: dict, commit: dict, window_s: int = _NEAR_WINDOW_S) -> bool:
    """True when the commit landed within ``window_s`` of the episode's last activity. If either
    timestamp is missing we can't disprove proximity, so we treat it as near (the file overlap
    that got us here is the real signal; timing only *down*grades, never gates out, evidence)."""
    ep_ts = _episode_activity_ts(episode)
    c_ts = _parse_ts(commit.get("timestamp"))
    if ep_ts is None or c_ts is None:
        return True
    return abs((c_ts - ep_ts).total_seconds()) <= window_s


def reconcile_commit(commit: dict, episodes: list) -> list:
    """Verdicts linking one commit to the episodes it plausibly satisfies.

    Args:
        commit: dict — {sha, files: [...], episodeIds: [...], timestamp}. ``episodeIds`` are the
            explicitly-declared ids (see ``episode_ids_from_trailers``); ``files`` are the commit's
            changed paths (repo-relative).
        episodes: list[dict] — candidate episodes, each {episodeId, files: [...], createdAt,
            updatedAt}.

    Returns:
        list[dict] — one verdict per *linked* episode:
            {episodeId, confidence, reason, matchedFiles, attach}. ``attach`` is True for
            explicit/high_file_overlap/time_file_inferred and False for ambiguous, so callers can
            persist confident links and merely surface the uncertain ones. Episodes with no
            evidence at all are omitted.
    """
    commit_files = set(commit.get("files") or [])
    explicit_ids = set(commit.get("episodeIds") or [])
    out = []
    for ep in episodes:
        eid = ep.get("episodeId")
        if not eid:
            continue
        ep_files = set(ep.get("files") or [])
        matched = sorted(commit_files & ep_files)

        if eid in explicit_ids:
            out.append(_verdict(eid, EXPLICIT, "named in the commit's OpenFDE-Episodes trailer",
                                matched, attach=True))
            continue
        # No trailer beyond here → we must have FILE evidence. A 0-file discussion episode
        # (ep_files empty) or a commit that shares no file with the episode is never attached.
        if not ep_files or not matched:
            continue

        coverage = len(matched) / len(ep_files)
        if coverage >= _HIGH_OVERLAP_FRACTION:
            out.append(_verdict(
                eid, HIGH_FILE_OVERLAP,
                f"commit changed {len(matched)}/{len(ep_files)} of the episode's files",
                matched, attach=True))
        elif _temporally_near(ep, commit):
            out.append(_verdict(
                eid, TIME_FILE_INFERRED,
                f"{len(matched)} shared file(s); episode was active near the commit",
                matched, attach=True))
        else:
            out.append(_verdict(
                eid, AMBIGUOUS,
                f"{len(matched)} shared file(s) but weak overlap and not time-adjacent",
                matched, attach=False))
    return out


def _verdict(episode_id, confidence, reason, matched_files, *, attach):
    return {"episodeId": episode_id, "confidence": confidence, "reason": reason,
            "matchedFiles": list(matched_files), "attach": attach}


def attach_commit(episode: dict, sha: str, *, confidence: str, reason: str = "",
                  matched_files=None) -> bool:
    """Idempotently record a commit on an episode (``commitShas`` + ``commitMeta[sha]``).

    Mutates ``episode`` in place. Safe to call repeatedly: the sha appears once in
    ``commitShas`` and the metadata is upgraded only by a *stronger* confidence — re-running
    reconciliation never duplicates a commit, an explicit trailer arriving later overwrites a
    weaker inferred link, and a weaker verdict never downgrades a stronger record. A same-or-weaker
    re-stamp is a no-op, so steady-state polls don't churn the store.

    Args:
        episode: dict — the episode to update.
        sha: str — the commit hash.
        confidence: str — one of the confidence constants.
        reason: str — short human explanation, shown as the "why" behind an inferred link.
        matched_files: list | None — the changed files shared with the episode.

    Returns:
        bool — True iff the episode actually changed (new sha or upgraded confidence).
    """
    changed = False
    shas = list(episode.get("commitShas") or [])
    if sha not in shas:
        shas.append(sha)
        changed = True
    episode["commitShas"] = shas

    meta = dict(episode.get("commitMeta") or {})
    entry = dict(meta.get(sha) or {})
    prev = entry.get("confidence")
    # Overwrite confidence/reason/matchedFiles only on a STRICTLY stronger verdict, so a later
    # weak inference can't clobber an explicit trailer (or a landed commit's record), and an
    # unchanged re-stamp doesn't report a spurious change.
    if prev is None or _RANK.get(confidence, 0) > _RANK.get(prev, -1):
        entry["confidence"] = confidence
        entry["reason"] = reason
        entry["matchedFiles"] = list(matched_files or [])
        meta[sha] = entry
        episode["commitMeta"] = meta
        changed = True
    else:
        meta[sha] = entry
        episode["commitMeta"] = meta
    return changed


def reconcile_episodes(commits: list, episodes: list, *, include_ambiguous: bool = False) -> dict:
    """Attribute a batch of commits across episodes, mutating the episodes in place.

    For each commit, ``reconcile_commit`` decides which episodes it satisfies; confident links
    (and, if ``include_ambiguous``, the uncertain ones too) are recorded via ``attach_commit``.
    This is how *one* batched commit ends up on *several* prompt cards.

    Args:
        commits: list[dict] — commit dicts (sha, files, episodeIds, timestamp), any order.
        episodes: list[dict] — episodes to attribute onto (mutated).
        include_ambiguous: bool — also persist ``ambiguous`` links (default False: surface-only).

    Returns:
        dict — {episodeId: [verdict, ...]} for every link that was *attached*, for callers that
        want to broadcast or log what changed.
    """
    by_id = {e.get("episodeId"): e for e in episodes if e.get("episodeId")}
    changed: dict = {}
    for commit in commits:
        sha = commit.get("sha")
        if not sha:
            continue
        for v in reconcile_commit(commit, episodes):
            if not (v["attach"] or include_ambiguous):
                continue
            ep = by_id.get(v["episodeId"])
            if ep is None:
                continue
            if attach_commit(ep, sha, confidence=v["confidence"], reason=v["reason"],
                             matched_files=v["matchedFiles"]):
                changed.setdefault(v["episodeId"], []).append(v)
    return changed
