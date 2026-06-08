"""
openfde/prompt_story.py — Prompt Story Graph v1 (deterministic, no LLM).

*Prompts become episodes. Episodes together become the story.* This derives a small
**product memory graph** from prompt episodes — distinct from ``story.py`` (which
narrates how a *code scope* flows). It answers: what are we building (active
concepts), which ideas were parked (deferred), which were tried and dropped
(abandoned), and which prompts/commits/files support each.

Deterministic heuristics only — no model calls, no network, no git mutation:
  - **active** concepts come from episode *titles* (already clean 3–6 word phrases),
    merged by slug and carrying their episodeIds / tags / commits / files;
  - **deferred** / **abandoned** concepts are short phrases extracted from episode
    text on *strong* signals only ("deferred", "out of scope", "superseded",
    "reverted", a line-initial "Remove …", …), capped per-episode to limit noise.

The point is a useful story scaffold, not perfect NLP. A future slice can replace
the extractor with a local-CLI summarizer (deterministic stays the fallback).
"""

import re

# Strong abandonment signals — a path tried and dropped/replaced.
_ABANDON = (
    "superseded", "supersede", "reverted", "revert ", "abandoned", "abandon ",
    "no longer", "instead of", "rolled back", "roll back", "deprecate", "backed out",
)
# Line-initial imperatives that, in a spec, mean "drop this": "Remove X", "Delete X".
_ABANDON_LEAD = ("remove ", "removed ", "delete ", "drop ", "get rid of ", "stop ")
# Deferred signals — parked for later, explicitly out of this slice.
_DEFER = (
    "deferred", "defer ", "out of scope", "not this slice", "future slice",
    "postpone", "left for later", "for later", "someday", " v2", "v2)", "next slice",
    "future:", "future ", "won't ", "wont ", "not yet",
)
# Phrases too generic to be a concept on their own.
_STOP_PHRASES = {
    "this", "that", "it", "them", "the rail", "this slice", "the card", "the ui",
    "the canvas", "the prompt", "the commit", "the file", "the files", "anything",
    "concept", "concepts", "ideas", "idea", "paths", "path", "features", "feature",
    "things", "thing", "work", "changes", "stuff", "the rest", "everything", "code",
}
# Trailing linking/function words to drop when reading a clause backwards.
_LINK_TAIL = {"is", "are", "be", "was", "were", "been", "being", "will", "would",
              "should", "can", "could", "may", "might", "to", "the", "a", "an",
              "of", "for", "that", "this", "and", "or", "but", "now", "still"}
_MAX_SIGNAL_PER_EP = 3          # cap noisy extraction per episode (per kind)
_MAX_LANE = 18                  # global cap per lane
_MAX_BRANCH_PER_EP = 4          # Story-map: cap branch boxes hung under one episode
_MAX_PARKED = 8                 # Story-map: cap the side "parked" lane


def is_operational_episode(ep: dict) -> bool:
    """True when an episode is shell/file-list/meta chatter — never a story beat."""
    sf = ep.get("storyFacts") or {}
    return ep.get("signal") == "operational" or bool(sf.get("operational"))


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return (s[:48] or "concept")


def _clean_phrase(text: str) -> str:
    """A short concept phrase: cut at clause punctuation, cap to ~6 words."""
    cand = re.split(r"[.;:(\[]", text, 1)[0]
    cand = cand.strip(" \t,-–—\"'`*").strip()
    # Drop a leading article so "the nested-beats approach" → "nested-beats approach".
    cand = re.sub(r"^(the|a|an)\s+", "", cand, flags=re.I)
    words = cand.split()
    if not words:
        return ""
    phrase = " ".join(words[:6]).strip(" ,.-")
    if len(phrase) < 4 or phrase.lower() in _STOP_PHRASES:
        return ""
    return phrase[:1].upper() + phrase[1:]


def _extract(unit: str, sig: str) -> str:
    """Extract a concept phrase around a signal: text after it, else the clause before."""
    low = unit.lower()
    i = low.find(sig)
    if i < 0:
        return ""
    after = _clean_phrase(unit[i + len(sig):].lstrip(" :,-–—"))
    if after:
        return after
    # Nothing after ("X is deferred") → read the clause immediately before the signal.
    seg = re.split(r"[,;:]", unit[:i])[-1]
    words = seg.split()
    while words and words[-1].lower() in _LINK_TAIL:
        words.pop()
    return _clean_phrase(" ".join(words[-6:]))


def _units(text: str):
    """Yield short units (lines, then sentences within a line) for signal scanning."""
    for raw in (text or "").splitlines():
        line = raw.strip(" \t-*•>#").strip()
        if not line:
            continue
        for s in re.split(r"(?<=[.!?])\s+", line):
            s = s.strip()
            if 6 <= len(s) <= 200:
                yield s


def _signals(ep: dict):
    """Yield (phrase, status) for deferred/abandoned ideas mentioned in an episode.

    Scans prompt + summary sentence-by-sentence. Only *strong* markers fire (generic
    verbs are too noisy in long spec prompts). A unit that mentions *both* categories
    is a description of the feature itself, not a decision — skipped. Capped per kind.
    """
    text = (ep.get("prompt") or "") + "\n" + (ep.get("summary") or "")
    out, seen = [], set()
    n_ab = n_df = 0
    for unit in _units(text):
        low = unit.lower()
        has_ab = any(s in low for s in _ABANDON) or any(low.startswith(s) for s in _ABANDON_LEAD)
        has_df = any(s in low for s in _DEFER)
        if has_ab and has_df:                  # "active, deferred, and abandoned concepts" → skip
            continue
        status = phrase = None
        if has_ab and n_ab < _MAX_SIGNAL_PER_EP:
            sig = next((s for s in _ABANDON if s in low), None) \
                or next((s for s in _ABANDON_LEAD if low.startswith(s)), None)
            phrase, status = _extract(unit, sig), "abandoned"
        elif has_df and n_df < _MAX_SIGNAL_PER_EP:
            sig = next((s for s in _DEFER if s in low), None)
            phrase, status = _extract(unit, sig), "deferred"
        if not status or not phrase:
            continue
        key = (status, phrase.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((phrase, status))
        if status == "abandoned":
            n_ab += 1
        else:
            n_df += 1
    return out


def _ensure(concepts: dict, title: str, status: str) -> dict:
    cid = "concept_" + _slug(title)
    c = concepts.get(cid)
    if c is None:
        c = {"id": cid, "title": title, "status": status, "summary": "",
             "episodeIds": [], "episodeTags": [], "commitShas": [], "files": [],
             "relatedConceptIds": [], "sequence": 0}
        concepts[cid] = c
    elif c["status"] != status:
        # A concept that is both an active title and a dropped/parked phrase is mixed.
        c["status"] = "mixed"
    return c


def _attach(c: dict, ep: dict) -> None:
    eid = ep.get("episodeId")
    if eid and eid not in c["episodeIds"]:
        c["episodeIds"].append(eid)
    tag = ep.get("tag")
    if tag and tag not in c["episodeTags"]:
        c["episodeTags"].append(tag)
    for sha in (ep.get("commitShas") or []):
        if sha and sha not in c["commitShas"]:
            c["commitShas"].append(sha)
    for cm in (ep.get("commits") or []):
        sha = cm.get("sha")
        if sha and sha not in c["commitShas"]:
            c["commitShas"].append(sha)
    for f in (ep.get("files") or []):
        if f and f not in c["files"]:
            c["files"].append(f)
    c["sequence"] = max(c.get("sequence") or 0, ep.get("sequence") or 0)


_LANE_RANK = {"active": 0, "mixed": 1, "deferred": 2, "abandoned": 3}


def build_prompt_graph(episodes: list) -> dict:
    """Derive the prompt story graph from enriched episodes.

    Args:
        episodes: list[dict] — episodes (newest-first) with sequence/tag/title/
            summary/prompt/status/files/commitShas (and optionally enriched commits).

    Returns:
        dict — {ok, concepts[], episodes[], edges[], counts}. Concepts are ordered
            active → mixed → deferred → abandoned, newest prompt first within a lane.
    """
    episodes = episodes or []
    concepts: dict = {}
    edges: list = []
    edge_seen = set()

    def _edge(a, b, label):
        if a == b:
            return
        k = (a, b, label)
        if k not in edge_seen:
            edge_seen.add(k)
            edges.append({"from": a, "to": b, "label": label})

    # Active concepts come from the episode's storyFacts.concepts when present (LLM or
    # deterministic), else the episode title. Deferred/abandoned come from storyFacts when
    # the episode HAS facts, else the deterministic signal extraction. Operational episodes
    # (chatter / commands / file-lists / internal summarizer prompts) never become concepts.
    for ep in episodes:
        sf = ep.get("storyFacts") or {}
        if is_operational_episode(ep):
            continue
        from openfde.episode_summary import is_bad_title
        active_titles = sf.get("concepts") if sf.get("concepts") else (
            [ep.get("title")] if (ep.get("title") or "").strip() else [])
        primary = None
        for ct in active_titles:
            ct = (ct or "").strip()
            if not ct or is_bad_title(ct):       # never promote operational/meta strings
                continue
            c = _ensure(concepts, ct, "active")
            if not c["summary"]:
                c["summary"] = ep.get("summary") or ""
            _attach(c, ep)
            if primary is None:
                primary = c
        if primary is None:
            continue
        pairs = ([(p, "deferred") for p in (sf.get("deferred") or [])]
                 + [(p, "abandoned") for p in (sf.get("abandoned") or [])]) if sf else _signals(ep)
        for phrase, status in pairs:
            dc = _ensure(concepts, phrase, status)
            _attach(dc, ep)
            if dc["id"] not in primary["relatedConceptIds"]:
                primary["relatedConceptIds"].append(dc["id"])
            _edge(primary["id"], dc["id"], "drops" if status == "abandoned" else "defers")

    # "precedes" edges chain active concepts in build order (for future edge UI).
    active_chain = sorted([c for c in concepts.values() if c["status"] in ("active", "mixed")],
                          key=lambda c: c["sequence"])
    for a, b in zip(active_chain, active_chain[1:]):
        _edge(a["id"], b["id"], "precedes")

    ordered = sorted(
        concepts.values(),
        key=lambda c: (_LANE_RANK.get(c["status"], 9), -(c.get("sequence") or 0), c["title"].lower()),
    )
    counts = {"active": 0, "deferred": 0, "abandoned": 0, "mixed": 0}
    for c in ordered:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
        c["commitCount"] = len(c["commitShas"])
        c["fileCount"] = len(c["files"])
        c["confidence"] = "deterministic"

    ep_lite = [{"episodeId": e.get("episodeId"), "tag": e.get("tag"),
                "title": e.get("title"), "status": e.get("status"),
                "sequence": e.get("sequence")} for e in episodes]

    return {"ok": True, "concepts": ordered, "episodes": ep_lite,
            "edges": edges, "counts": counts}
