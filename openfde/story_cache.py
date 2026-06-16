"""
openfde/story_cache.py — the persisted Story boot cache (.openfde/cache/story_graph.json).

First paint must never wait on the full Story rebuild (backfill → ensure_facts → flag → clean →
build_prompt_graph). This module persists the last-known-good Story graph so ``/api/story/boot``
serves the recent Story **instantly** — the UI shows "Restoring Story…" only when there is no cache
yet, never "No concepts yet" — while the full graph rebuilds in the background and broadcasts
``story_updated``.

The boot payload embeds the (already-capped) graph so the UI renders the real Story shape, plus a
recent-first slice of the latest **product** episodes (operational / nonImplementation episodes are
absent — they were never on the spine) and the counts. ``confirmed`` is the law for empty states: a
boot payload is **never** authoritative-empty (``confirmed: False``); only the full endpoint returns
``confirmed: True``, so the UI may say "No concepts yet" only after that.

Atomic writes (tmp + rename), pure + dependency-light (json + pathlib): unit-testable without a
server or git.
"""
from __future__ import annotations

import json
from pathlib import Path

# Bump when the boot payload SHAPE changes so a stale cache is ignored rather than mis-rendered.
CACHE_VERSION = "1"
_STORY = "story_graph.json"
_RAIL = "rail_boot.json"


def cache_path(openfde_dir) -> Path:
    return Path(openfde_dir) / "cache" / _STORY


def rail_cache_path(openfde_dir) -> Path:
    return Path(openfde_dir) / "cache" / _RAIL


def recent_product_episodes(graph: dict, limit: int = 10) -> list:
    """The latest ``limit`` PRODUCT episodes, newest-first, as lite chips — taken from the graph's
    ``storyMap.spine``, which already excludes operational / nonImplementation episodes (they never
    reach the spine). So "recent product" and "operational hidden" are one and the same here."""
    spine = ((graph or {}).get("storyMap") or {}).get("spine") or []
    out = []
    for n in reversed(spine):                          # spine is sequence-ascending → reverse = newest-first
        out.append({
            "episodeId": n.get("episodeId"), "tag": n.get("tag"), "title": n.get("title"),
            "summary": n.get("summary"), "sequence": n.get("sequence"), "status": n.get("status"),
            "commitCount": n.get("commitCount") or 0, "fileCount": n.get("fileCount") or 0,
        })
        if len(out) >= limit:
            break
    return out


# First paint renders the concept LANES; keep the boot small by capping to the most-recently-touched
# concepts (recent-first) and dropping the heavy Tell-mode structures — those arrive with the full
# graph, which the UI gates Tell behind. On a mature repo the full graph is ~900 KB (610 concepts +
# storyTimeline + storyNarrative); the boot is ~40 KB.
_CONCEPT_CAP = 60


def build_story_boot(graph: dict, *, limit: int = 10, concept_cap: int = _CONCEPT_CAP,
                     generated_at: str = "") -> dict:
    """A LIGHTWEIGHT boot payload from a full prompt-graph result: the latest product episodes, the
    recent concept lanes (capped, newest-first), and counts — enough for first paint. The Tell-mode
    structures (storyMap / storyTimeline / storyNarrative) and the edge set are deliberately dropped;
    they load with the full graph (the UI keeps Tell gated until then). ``cached``/``confirmed``/
    ``building`` are stamped so the UI knows this is restored-not-authoritative."""
    g = graph or {}
    spine = ((g.get("storyMap") or {}).get("spine")) or []
    concepts = sorted((g.get("concepts") or []),
                      key=lambda c: c.get("sequence") or 0, reverse=True)[:concept_cap]
    return {
        "ok": True,
        "cached": True,
        "confirmed": False,                 # boot is never the authoritative "truly empty" signal
        "building": False,
        "generatedAt": generated_at,
        "cacheVersion": CACHE_VERSION,
        "productEpisodeCount": len(spine),
        "conceptCount": len(g.get("concepts") or []),    # full total, so the UI can show "+N more"
        "recentEpisodes": recent_product_episodes(g, limit),
        "concepts": concepts,
        "counts": g.get("counts") or {},
        "lifecycleCounts": g.get("lifecycleCounts") or {},
        "episodes": [],                     # the rail supplies episodes; Story reads them as a prop
        "edges": [],                        # concept edges are full-graph only
        "storyMap": {},                     # ↓ Tell-mode structures load with the full graph
        "storyTimeline": {},
        "storyNarrative": {},
    }


def empty_boot() -> dict:
    """No cache yet → a NON-authoritative empty: the UI shows "Restoring Story…", never "No concepts
    yet". ``building: True`` signals the full graph is still being computed."""
    return {
        "ok": True, "cached": False, "confirmed": False, "building": True,
        "generatedAt": "", "cacheVersion": CACHE_VERSION, "productEpisodeCount": 0,
        "recentEpisodes": [], "concepts": [], "counts": {}, "lifecycleCounts": {},
        "episodes": [], "edges": [], "storyMap": {}, "storyTimeline": {}, "storyNarrative": {},
    }


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)                                   # atomic on POSIX — never a torn read


def write_story_cache(openfde_dir, graph: dict, *, limit: int = 10, generated_at: str = "") -> dict:
    """Persist the boot payload built from ``graph``. Best-effort caller; returns the payload."""
    payload = build_story_boot(graph, limit=limit, generated_at=generated_at)
    _atomic_write(cache_path(openfde_dir), payload)
    return payload


def read_story_cache(openfde_dir) -> "dict | None":
    """The cached boot payload, or None when there is no cache / it is unreadable / shape-stale.
    Re-stamps cached/confirmed/building so a hand-written or older file can't claim authority."""
    try:
        data = json.loads(cache_path(openfde_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("cacheVersion") != CACHE_VERSION:
        return None
    data["ok"] = True
    data["cached"] = True
    data["confirmed"] = False               # a cache read is never authoritative-empty
    data["building"] = False
    return data


# ── Rail boot cache ─────────────────────────────────────────────────────────────
# The rail boot (latest ~10 chips) is TINY (~5 KB), but building it parses the whole
# episodes.json (~600 KB on a mature repo) — too heavy to do on every first-paint read,
# where it clogs the shared boot pool and starves the Story boot. So persist the tiny
# result and serve THAT: the boot read is ~5 KB, the parse is off the request path.

def write_rail_cache(openfde_dir, rail_payload: dict) -> dict:
    """Persist a built rail boot payload (latest ~10 chips). Stamps it cached + non-authoritative
    (``confirmed: False``); best-effort caller. Returns the persisted payload."""
    payload = {**(rail_payload or {}), "cacheVersion": CACHE_VERSION,
               "cached": True, "confirmed": False}
    _atomic_write(rail_cache_path(openfde_dir), payload)
    return payload


def read_rail_cache(openfde_dir) -> "dict | None":
    """The cached rail boot payload, or None (no cache / unreadable / shape-stale). Re-stamps so a
    cache read is never the authoritative empty (only the full rail confirms an empty rail)."""
    try:
        data = json.loads(rail_cache_path(openfde_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("cacheVersion") != CACHE_VERSION:
        return None
    data["ok"] = True
    data["cached"] = True
    data["confirmed"] = False
    return data


def empty_rail_boot() -> dict:
    """No rail cache yet → a NON-authoritative empty (``building: True``): the UI keeps the rail
    area quiet, never an empty rail. The full rail (/api/review/episodes) confirms an empty rail."""
    return {"ok": True, "cached": False, "confirmed": False, "building": True,
            "episodes": [], "totalCount": 0,
            "outside": {"episodeId": "outside", "kind": "manual", "status": "landed",
                        "prompt": "Outside OpenFDE", "summary": "", "commits": [],
                        "commitCount": 0, "files": [], "fileCount": 0}}
