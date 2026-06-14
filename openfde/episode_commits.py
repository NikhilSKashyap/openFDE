"""
openfde/episode_commits.py — prompt → commit reconciliation (many prompts, one commit).

The product model is *many prompts → one commit*: a developer works through several prompts
(P1, P2, P3) and then lands a single commit; all three prompt cards should show that commit.
A commit can declare its episodes explicitly (the ``OpenFDE-Episodes`` trailer) or, for commits
made outside the OpenFDE land path, we *infer* the link from changed-file overlap and timing —
but always with an honest **confidence** so the UI can label inferred links as such.

Confidence ladder (highest first):
  - ``explicit``           — the commit's ``OpenFDE-Episodes``/``OpenFDE-Episode`` trailer names it.
                             Always wins; no other gate applies.
  - ``high_file_overlap``  — no trailer, but a MULTI-file episode whose files the commit mostly
                             changed, inside its capture window.
  - ``time_file_inferred`` — file overlap (incl. a single file) inside the capture window.
  - ``ambiguous``          — file overlap but outside the capture window / not strong enough (NOT
                             attached — surfaced so a caller can show it faintly or ignore it).

Precision gates for every INFERRED (trailer-less) link — file overlap is necessary but NEVER
sufficient (a single common file like ``README.md``, or strong overlap with a stale episode, used
to over-attach). The commit must PROVABLY belong to the episode's turn:
  - **Same canonical repo/session** as the watched repo (``sessionCwd``): sibling repos under a
    shared parent reuse relative paths (``frontend/src/App.jsx``) that collide by coincidence.
  - Then ONE provenance signal — **strong file overlap is not a bypass**:
      (A) commit inside the episode's **capture window** (createdAt + window), OR
      (B) commit **baseline-matches** — its first parent is the episode's ``initialHead``, OR
      (C) episode is the single **latest open/reviewing work unit** with fresh (wider-window) activity.
  - **``needs_manual_land`` is NOT indefinitely active** — it can attach only via (A) or (B); an old
    one is stale historical. Only open/reviewing get the latest-active bypass (C).
  - **Timing is read from ``createdAt``, never ``updatedAt``** (summarization/hydration rewrite it).
  - A **0-file discussion episode never attaches without a trailer**; an operational/discussion
    episode needs STRONG multi-file evidence.

This module is pure (no git, no I/O) except a lazy, cached ``same_repo`` for the repo gate, so it
is fully unit-testable; ``openfde.server`` calls it and persists the verdicts onto episodes.
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

# Statuses that mark an episode as the CURRENT work (eligible for the latest-active bypass).
# NOTE: needs_manual_land is deliberately NOT here — it persists indefinitely, so an old one must
# be treated as stale historical, attaching only via the capture window or a baseline match.
_ACTIVE_STATES = frozenset({"open", "reviewing"})
# A commit belongs to an episode's CAPTURE WINDOW when it lands after the prompt was captured
# (small grace for clock skew) and within this span — the same working session. Anchored on
# createdAt (see _capture_ts), NEVER on updatedAt.
_CAPTURE_WINDOW_S = 6 * 3600
_CAPTURE_GRACE_S = 15 * 60
# The single latest open/reviewing episode (the "current work unit") gets extra latitude — a commit
# within this longer window of its capture still counts as its fresh activity.
_ACTIVE_WINDOW_S = 24 * 3600
# Fraction of an episode's files a commit must touch to count as STRONG (high) overlap. Only a
# MULTI-file episode can be "high overlap" — a single shared file is never strong on its own.
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


def _capture_ts(episode: dict) -> "datetime | None":
    """The episode's reliable CAPTURE time — ``createdAt``, set once when the prompt was captured.

    ``updatedAt`` is deliberately NOT used: it is rewritten by the LLM summarizer, by hydration,
    and by this very reconciliation pass, so a 3-day-old episode looks "active" the instant it is
    re-summarized. createdAt is the only timestamp that tracks real capture activity.
    """
    return _parse_ts(episode.get("createdAt"))


def _within_capture_window(cap_ts, commit_ts, window_s: int = _CAPTURE_WINDOW_S) -> bool:
    """True when ``commit_ts`` lands inside the episode's capture window: after the prompt was
    captured (minus a small clock-skew grace) and within ``window_s`` of it — the same working
    session. A missing/unparseable timestamp → False: we never attach on a temporal basis we
    cannot verify (the old code's permissive "assume near" was the over-attachment bug)."""
    if cap_ts is None or commit_ts is None:
        return False
    delta = (commit_ts - cap_ts).total_seconds()
    return -_CAPTURE_GRACE_S <= delta <= window_s


def _is_operational(episode: dict) -> bool:
    """True for discussion/operational/chatter episodes (hidden from the story)."""
    return (episode.get("signal") == "operational"
            or bool((episode.get("storyFacts") or {}).get("operational")))


def _same_repo(a, b) -> bool:
    """Canonical same-repo test (git root / realpath), via prompt_capture; basename is never
    evidence. Lazy import keeps this module free of git at import time; realpath is the fallback."""
    if not a or not b:
        return False
    try:
        from openfde.prompt_capture import same_repo
        return same_repo(a, b)
    except Exception:  # noqa: BLE001 — never let repo resolution crash reconciliation
        import os
        try:
            return os.path.realpath(str(a)) == os.path.realpath(str(b))
        except (OSError, ValueError):
            return False


def _baseline_match(episode: dict, commit: dict) -> bool:
    """True when the commit lands directly on the episode's captured baseline — its first parent is
    the HEAD that was current when the prompt was captured (``initialHead``). That is a stored,
    git-verifiable proof the commit IS this turn's work, independent of timing (so it rescues a
    long multi-day session whose createdAt is old). First parent only → merges don't false-match."""
    ih = episode.get("initialHead")
    parents = commit.get("parents") or []
    if not ih or not parents:
        return False
    p0 = parents[0]
    return p0 == ih or p0.startswith(ih) or ih.startswith(p0)   # tolerate short/full sha


def _latest_active_id(episodes: list, watched_root) -> "str | None":
    """episodeId of the most recently captured open/reviewing episode in the watched repo — the
    'current work unit'. Only this one episode is granted the wider active window; everything else
    must land inside the strict capture window or match a baseline."""
    best_id, best_ts = None, None
    for e in episodes:
        if e.get("status") not in _ACTIVE_STATES:
            continue
        if watched_root is not None and not _same_repo(e.get("sessionCwd"), watched_root):
            continue
        ts = _capture_ts(e)
        if ts is not None and (best_ts is None or ts > best_ts):
            best_ts, best_id = ts, e.get("episodeId")
    return best_id


def reconcile_commit(commit: dict, episodes: list, *, watched_root=None) -> list:
    """Verdicts linking one commit to the episodes it plausibly satisfies.

    Precision rules — file overlap is necessary but NEVER sufficient; the commit must provably
    belong to the episode's turn, so a single common file (``README.md``) or strong overlap with a
    stale episode no longer over-attaches:
      1. An **explicit trailer always wins** — no gating.
      2. Inference needs **file evidence** (a 0-file discussion episode never attaches without a
         trailer) and the **same canonical repo** (``sessionCwd``) as the watched repo.
      3. Then the commit must satisfy ONE provenance signal — strong file overlap is NOT a bypass:
           (A) it lands inside the episode's **capture window** (createdAt + window), OR
           (B) it **baseline-matches** — its first parent is the episode's ``initialHead`` (a stored
               marker that this commit IS that turn's work), OR
           (C) the episode is the single **latest open/reviewing work unit** and the commit is
               within the wider active window of its capture (fresh current activity).
         ``needs_manual_land`` is NOT current: it can attach only via (A) or (B), so an old one is
         treated as stale historical.
      4. **Confidence** of an attached link = file strength: multi-file strong → ``high_file_overlap``,
         else ``time_file_inferred``. Operational/discussion needs strong multi-file evidence.

    Args:
        commit: dict — {sha, files, episodeIds, timestamp, parents}.
        episodes: list[dict] — candidate episodes ({episodeId, files, createdAt, status, signal,
            sessionCwd, initialHead, …}).
        watched_root: the repo OpenFDE is watching; episodes from a different canonical repo are
            excluded. None skips the repo gate (pure unit tests).

    Returns:
        list[dict] — one verdict per *considered* (same-repo, file-sharing) episode:
            {episodeId, confidence, reason, matchedFiles, attach}. ``attach`` is True for
            explicit/high_file_overlap/time_file_inferred, False for ambiguous.
    """
    commit_files = set(commit.get("files") or [])
    explicit_ids = set(commit.get("episodeIds") or [])
    commit_ts = _parse_ts(commit.get("timestamp"))
    latest_active = _latest_active_id(episodes, watched_root)
    out = []
    for ep in episodes:
        eid = ep.get("episodeId")
        if not eid:
            continue
        ep_files = set(ep.get("files") or [])
        matched = sorted(commit_files & ep_files)

        # 1. Explicit trailer ALWAYS wins — authoritative, no gating.
        if eid in explicit_ids:
            out.append(_verdict(eid, EXPLICIT, "named in the commit's OpenFDE-Episodes trailer",
                                matched, attach=True))
            continue
        # 2. Inference needs file evidence + the same canonical repo.
        if not ep_files or not matched:
            continue
        if watched_root is not None and not _same_repo(ep.get("sessionCwd"), watched_root):
            continue

        coverage = len(matched) / len(ep_files)
        # STRONG = at least TWO shared files AND most of the episode's files. A single shared file
        # is never strong (no matter the episode size) — that is the README.md over-attachment.
        multi_strong = len(matched) >= 2 and coverage >= _HIGH_OVERLAP_FRACTION

        # Discussion/operational episodes attach ONLY on strong, repo-correct multi-file evidence.
        if _is_operational(ep) and not multi_strong:
            out.append(_verdict(eid, AMBIGUOUS,
                                "operational/discussion episode without strong file evidence",
                                matched, attach=False))
            continue

        # 3. Provenance — file overlap alone is NOT enough; the commit must belong to this turn.
        in_window = _within_capture_window(_capture_ts(ep), commit_ts)                       # (A)
        baseline = _baseline_match(ep, commit)                                               # (B)
        current = (eid == latest_active                                                      # (C)
                   and _within_capture_window(_capture_ts(ep), commit_ts, _ACTIVE_WINDOW_S))
        if not (in_window or baseline or current):
            out.append(_verdict(
                eid, AMBIGUOUS,
                f"{len(matched)} shared file(s) but the commit is outside this episode's turn "
                f"(no capture-window/baseline/active match)", matched, attach=False))
            continue

        # 4. Confidence by file strength; reason names the provenance signal that carried it.
        why = ("commit lands on the episode's baseline" if baseline
               else "within the episode's capture window" if in_window
               else "latest active work unit")
        if multi_strong:
            out.append(_verdict(
                eid, HIGH_FILE_OVERLAP,
                f"commit changed {len(matched)}/{len(ep_files)} of the episode's files — {why}",
                matched, attach=True))
        else:
            out.append(_verdict(
                eid, TIME_FILE_INFERRED,
                f"{len(matched)} shared file(s) — {why}", matched, attach=True))
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


def reconcile_episodes(commits: list, episodes: list, *, watched_root=None,
                       include_ambiguous: bool = False) -> dict:
    """Attribute a batch of commits across episodes, mutating the episodes in place.

    For each commit, ``reconcile_commit`` decides which episodes it satisfies; confident links
    (and, if ``include_ambiguous``, the uncertain ones too) are recorded via ``attach_commit``.
    This is how *one* batched commit ends up on *several* prompt cards.

    Args:
        commits: list[dict] — commit dicts (sha, files, episodeIds, timestamp), any order.
        episodes: list[dict] — episodes to attribute onto (mutated).
        watched_root: the repo OpenFDE is watching — episodes from a different canonical repo are
            never attributed (passed through to ``reconcile_commit``). None skips the repo gate.
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
        for v in reconcile_commit(commit, episodes, watched_root=watched_root):
            if not (v["attach"] or include_ambiguous):
                continue
            ep = by_id.get(v["episodeId"])
            if ep is None:
                continue
            if attach_commit(ep, sha, confidence=v["confidence"], reason=v["reason"],
                             matched_files=v["matchedFiles"]):
                changed.setdefault(v["episodeId"], []).append(v)
    return changed
