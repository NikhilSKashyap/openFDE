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
    "postpone", "left for later", "for later", "someday", " v2", "v2)",
    "future:", "future ", "won't ", "wont ", "not yet",
)
# Next signals — committed near-term direction (the next 1–3 slices), not parked.
_NEXT = ("next:", "next slice", "next up")
# Watch signals — interesting but explicitly not committed. The loose words fire only
# on word boundaries so they don't match inside other tokens.
_WATCH = ("watch:", "worth watching", "not committed", "keep an eye on")
_WATCH_RE = re.compile(r"\b(maybe|consider|explore|exploring|interesting)\b", re.I)
# Revisit-trigger language inside a deferred unit: "until X lands", "once Y ships".
_TRIGGER_RE = re.compile(r"\b(when|once|after|until|as soon as)\b", re.I)
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


def _trigger_clause(text: str):
    """Short revisit clause starting at trigger language: 'until passive capture lands'."""
    clause = re.split(r"[.;!?,]", text, 1)[0]
    words = clause.split()
    out = " ".join(words[:8]).strip(" ,.-–—")
    return out if len(words) >= 2 else None


def _sf_trigger(phrase: str, det: list):
    """Carry a deterministically-extracted trigger onto the matching storyFacts
    deferred phrase (the LLM usually echoes the prompt's wording, so a shared
    significant word is enough). None when nothing lines up."""
    pl = (phrase or "").lower()
    for ph, kind, trig in det:
        if kind != "deferred" or not trig:
            continue
        phl = ph.lower()
        w = next((x for x in phl.split() if len(x) >= 4), None)
        if (w and w in pl) or phl in pl or pl in phl:
            return trig
    return None


def _signals(ep: dict):
    """Yield (phrase, kind, trigger) for lifecycle ideas mentioned in an episode.

    kind ∈ ``abandoned | deferred | next | watch``; ``trigger`` is a short revisit
    clause and only ever set for deferred. Scans prompt + summary sentence-by-sentence.
    Only *strong* markers fire (generic verbs are too noisy in long spec prompts), with
    per-unit precedence abandoned > deferred > next > watch. A unit that mentions both
    abandon and defer categories is a description of the feature itself, not a
    decision — skipped. Capped per kind.
    """
    text = (ep.get("prompt") or "") + "\n" + (ep.get("summary") or "")
    out, seen = [], set()
    caps = {"abandoned": 0, "deferred": 0, "next": 0, "watch": 0}
    for unit in _units(text):
        low = unit.lower()
        has_ab = any(s in low for s in _ABANDON) or any(low.startswith(s) for s in _ABANDON_LEAD)
        has_df = any(s in low for s in _DEFER)
        if has_ab and has_df:                  # "active, deferred, and abandoned concepts" → skip
            continue
        kind = sig = None
        if has_ab:
            kind = "abandoned"
            sig = next((s for s in _ABANDON if s in low), None) \
                or next((s for s in _ABANDON_LEAD if low.startswith(s)), None)
        elif has_df:
            kind, sig = "deferred", next(s for s in _DEFER if s in low)
        elif any(s in low for s in _NEXT):
            kind, sig = "next", next(s for s in _NEXT if s in low)
        elif any(s in low for s in _WATCH):
            kind, sig = "watch", next(s for s in _WATCH if s in low)
        else:
            wm = _WATCH_RE.search(low)
            if wm:
                kind, sig = "watch", wm.group(0)
        if not kind or caps[kind] >= _MAX_SIGNAL_PER_EP:
            continue
        trigger, ph_src = None, unit
        if kind == "deferred":
            m = _TRIGGER_RE.search(unit)
            if m:
                trigger = _trigger_clause(unit[m.start():])
                # Keyword after the signal → it ends the phrase ("…import until X lands");
                # keyword before it ("Once X ships, defer …") leaves the phrase intact.
                if m.start() > low.find(sig):
                    ph_src = unit[:m.start()]
        phrase = _extract(ph_src, sig)
        if not phrase:
            continue
        key = (kind, phrase.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((phrase, kind, trigger))
        caps[kind] += 1
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


# Lifecycle lanes (Step 48): the UI vocabulary layered over the broad ``status``.
_LIFE_RANK = {"now": 0, "next": 1, "watch": 2, "deferred": 3, "abandoned": 4}
# Broad semantic class each signal kind maps to — ``status`` keeps its existing
# value set (active/mixed/deferred/abandoned) so older consumers keep working.
_KIND_STATUS = {"next": "active", "watch": "deferred"}
_KIND_EDGE = {"abandoned": "drops", "deferred": "defers", "next": "queues", "watch": "watches"}


def build_prompt_graph(episodes: list) -> dict:
    """Derive the prompt story graph from enriched episodes.

    Args:
        episodes: list[dict] — episodes (newest-first) with sequence/tag/title/
            summary/prompt/status/files/commitShas (and optionally enriched commits).

    Returns:
        dict — {ok, concepts[], episodes[], edges[], counts, lifecycleCounts, storyMap}.
            Each concept carries the broad ``status`` (active/mixed/deferred/abandoned —
            unchanged contract) plus a ``lifecycle`` lane for the UI: ``now`` (tied to
            the latest product episode) / ``next`` (committed, queued) / ``watch``
            (interesting, not committed) / ``deferred`` (parked, optional revisit
            ``trigger``) / ``abandoned``. Ordered now → … → abandoned, newest prompt
            first within a lane.
    """
    episodes = episodes or []
    concepts: dict = {}
    edges: list = []
    edge_seen = set()
    active_eids: dict = {}      # concept id -> episode ids where it was a build TITLE
    next_ids: set = set()       # concept ids explicitly queued via a "Next:" signal
    watch_only: dict = {}       # concept id -> True while its only parked signal is watch

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
            watch_only[c["id"]] = False
            active_eids.setdefault(c["id"], set()).add(ep.get("episodeId"))
            if primary is None:
                primary = c
        if primary is None:
            continue
        # storyFacts (LLM) drive the deferred/abandoned lanes when present, but next/watch
        # vocabulary isn't in storyFacts yet — the deterministic scan supplies those (and
        # the revisit trigger) either way.
        det = _signals(ep)
        if sf:
            pairs = [(p, "deferred", _sf_trigger(p, det)) for p in (sf.get("deferred") or [])]
            pairs += [(p, "abandoned", None) for p in (sf.get("abandoned") or [])]
            pairs += [t for t in det if t[1] in ("next", "watch")]
        else:
            pairs = det
        for phrase, kind, trigger in pairs:
            dc = _ensure(concepts, phrase, _KIND_STATUS.get(kind, kind))
            if kind == "watch":
                watch_only.setdefault(dc["id"], True)
            else:
                watch_only[dc["id"]] = False
            if kind == "next":
                next_ids.add(dc["id"])
            if trigger and not dc.get("trigger"):
                dc["trigger"] = trigger
            _attach(dc, ep)
            if dc is not primary and dc["id"] not in primary["relatedConceptIds"]:
                primary["relatedConceptIds"].append(dc["id"])
            _edge(primary["id"], dc["id"], _KIND_EDGE[kind])

    # "precedes" edges chain active concepts in build order (for future edge UI).
    active_chain = sorted([c for c in concepts.values() if c["status"] in ("active", "mixed")],
                          key=lambda c: c["sequence"])
    for a, b in zip(active_chain, active_chain[1:]):
        _edge(a["id"], b["id"], "precedes")

    # Lifecycle lane (Step 48), layered over the broad status. "now" requires being a
    # build *title* of the latest product episode — a defer/watch mention there doesn't
    # qualify — and an explicit "Next:" mark queues a concept even when the latest
    # episode is where it was mentioned.
    prod = [e for e in episodes if not is_operational_episode(e)]
    latest_eid = max(prod, key=lambda e: e.get("sequence") or 0).get("episodeId") if prod else None
    for c in concepts.values():
        if c["status"] == "abandoned":
            c["lifecycle"] = "abandoned"
        elif c["status"] == "deferred":
            c["lifecycle"] = "watch" if watch_only.get(c["id"]) else "deferred"
        else:                                   # active / mixed
            on_now = latest_eid is not None and latest_eid in active_eids.get(c["id"], ())
            if c["id"] in next_ids and not on_now:
                c["lifecycle"] = "next"
            else:
                c["lifecycle"] = "now" if on_now else "next"

    ordered = sorted(
        concepts.values(),
        key=lambda c: (_LIFE_RANK.get(c.get("lifecycle"), 9), -(c.get("sequence") or 0), c["title"].lower()),
    )
    counts = {"active": 0, "deferred": 0, "abandoned": 0, "mixed": 0}
    life_counts = {"now": 0, "next": 0, "watch": 0, "deferred": 0, "abandoned": 0}
    for c in ordered:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
        life_counts[c["lifecycle"]] = life_counts.get(c["lifecycle"], 0) + 1
        c["commitCount"] = len(c["commitShas"])
        c["fileCount"] = len(c["files"])
        c["confidence"] = "deterministic"

    ep_lite = [{"episodeId": e.get("episodeId"), "tag": e.get("tag"),
                "title": e.get("title"), "status": e.get("status"),
                "sequence": e.get("sequence")} for e in episodes]

    return {"ok": True, "concepts": ordered, "episodes": ep_lite,
            "edges": edges, "counts": counts, "lifecycleCounts": life_counts,
            "storyMap": build_story_map(episodes, ordered)}


def build_story_map(episodes: list, concepts: list) -> dict:
    """Chronological **episode** story map for Story Tell mode.

    The unit is the *episode*, not the concept. Product (non-operational) episodes form a
    left→right spine ordered by ``sequence`` ascending; each deferred/abandoned concept hangs
    as a branch off the episode that produced it (its latest ``episodeIds`` member that is on
    the spine), else lands in a side ``parked`` lane. Operational/meta episodes are hidden from
    the spine (only counted). Pure + deterministic — no measurement, no model calls — so the
    frontend renders a fixed set of episode boxes instead of measuring 80+ concept cards.

    Args:
        episodes: list[dict] — enriched episodes (any order).
        concepts: list[dict] — concepts from :func:`build_prompt_graph` (carry status,
            episodeIds, episodeTags, commitCount, fileCount).

    Returns:
        dict — ``{spine[], parked[], parkedOverflow, hiddenOps}``. Each spine node carries the
            episode's own metrics (commit / file / concept counts) plus its deferred/abandoned
            branch lists (capped); it never embeds concept cards.
    """
    episodes = episodes or []
    concepts = concepts or []

    spine_eps = sorted((e for e in episodes if not is_operational_episode(e)),
                       key=lambda e: e.get("sequence") or 0)
    hidden_ops = sum(1 for e in episodes if is_operational_episode(e))
    seq_of = {e.get("episodeId"): (e.get("sequence") or 0) for e in spine_eps}

    def _branch(c: dict) -> dict:
        return {"conceptId": c.get("id"), "title": c.get("title") or "",
                "status": c.get("status"), "commitCount": c.get("commitCount") or 0,
                "fileCount": c.get("fileCount") or 0}

    active_count: dict = {}
    slots: dict = {}             # episodeId -> {"deferred": [...], "abandoned": [...]}
    parked: list = []
    for c in concepts:
        status = c.get("status")
        eids = c.get("episodeIds") or []
        if status in ("active", "mixed"):
            for eid in eids:
                if eid in seq_of:
                    active_count[eid] = active_count.get(eid, 0) + 1
            continue
        if status not in ("deferred", "abandoned"):
            continue
        on_spine = [eid for eid in eids if eid in seq_of]
        if on_spine:
            host = max(on_spine, key=lambda eid: seq_of[eid])    # the latest beat that touched it
            slots.setdefault(host, {"deferred": [], "abandoned": []})[status].append(_branch(c))
        else:
            parked.append({**_branch(c), "fromTag": (c.get("episodeTags") or [None])[0]})

    spine = []
    for e in spine_eps:
        eid = e.get("episodeId")
        slot = slots.get(eid) or {"deferred": [], "abandoned": []}
        deferred = slot["deferred"][:_MAX_BRANCH_PER_EP]
        abandoned = slot["abandoned"][:_MAX_BRANCH_PER_EP]
        overflow = (len(slot["deferred"]) - len(deferred)) + (len(slot["abandoned"]) - len(abandoned))
        commit_count = len(e.get("commitShas") or []) or len(e.get("commits") or [])
        files = list(e.get("files") or [])
        spine.append({
            "episodeId": eid, "tag": e.get("tag") or "", "title": e.get("title") or "",
            "summary": e.get("summary") or "", "sequence": e.get("sequence") or 0,
            "status": e.get("status") or "", "commitCount": commit_count,
            "fileCount": len(files), "conceptCount": active_count.get(eid, 0),
            # A capped file list so clicking a beat can amber its files even before the
            # heavier /api/review/episodes payload has loaded (self-sufficient node).
            "files": files[:20],
            "deferred": deferred, "abandoned": abandoned, "branchOverflow": overflow,
        })

    return {"spine": spine, "parked": parked[:_MAX_PARKED],
            "parkedOverflow": max(0, len(parked) - _MAX_PARKED), "hiddenOps": hidden_ops}
