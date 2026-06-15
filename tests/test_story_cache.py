"""
Story + rail progressive boot caching.

Laws under test:
  • The Story boot payload serves the latest ~10 PRODUCT episodes, newest-first, with operational /
    nonImplementation episodes absent (they were never on the spine).
  • A boot payload is NEVER authoritative-empty: ``confirmed`` is False on every cache read and on the
    "no cache yet" placeholder (``building: True``) — so the UI shows "Restoring Story…", not "No
    concepts yet". Only the full endpoint stamps ``confirmed: True``.
  • The rail boot (``build_rail_payload``) is cache-only and lite: recent prompt chips, no readiness /
    reconciliation / worktree — so first paint never waits on the heavy full view, and Review changes
    (a separate worktree signal) is always available.
"""
import json
import tempfile
import unittest
from pathlib import Path

from openfde import story_cache as sc
from openfde.prompt_story import build_prompt_graph


def _episode(seq, *, operational=False, nonimpl=False):
    e = {"episodeId": f"e{seq}", "tag": f"P{seq}", "sequence": seq,
         "title": f"Concept {seq}", "summary": f"summary {seq}",
         "status": "landed", "commitShas": [f"sha{seq}"], "files": [f"f{seq}.py"],
         "signal": "operational" if operational else "product",
         "storyFacts": {"concepts": [f"Concept {seq}"], "deferred": [], "abandoned": [],
                        "operational": operational}}
    if nonimpl:
        e["nonImplementation"] = True
    return e


class StoryBootCacheTest(unittest.TestCase):
    def _graph(self):
        # 12 product episodes + an operational one + a nonImplementation one.
        eps = [_episode(s) for s in range(1, 13)]
        eps.append(_episode(13, operational=True))
        eps.append(_episode(14, nonimpl=True))
        return build_prompt_graph(eps)

    def test_boot_returns_latest_ten_product_newest_first(self):
        boot = sc.build_story_boot(self._graph(), limit=10)
        recent = boot["recentEpisodes"]
        self.assertEqual(len(recent), 10)
        self.assertEqual([r["sequence"] for r in recent], list(range(12, 2, -1)))  # 12..3, newest first
        self.assertEqual(recent[0]["tag"], "P12")

    def test_operational_and_nonimplementation_are_absent_from_boot(self):
        boot = sc.build_story_boot(self._graph(), limit=20)
        ids = {r["episodeId"] for r in boot["recentEpisodes"]}
        self.assertNotIn("e13", ids)                       # operational
        self.assertNotIn("e14", ids)                       # nonImplementation
        self.assertEqual(boot["productEpisodeCount"], 12)  # spine = product only

    def test_boot_is_never_authoritative_empty(self):
        boot = sc.build_story_boot(self._graph())
        self.assertFalse(boot["confirmed"])
        self.assertTrue(boot["cached"])
        empty = sc.empty_boot()
        self.assertFalse(empty["confirmed"])               # "no cache yet" ≠ "truly empty"
        self.assertTrue(empty["building"])
        self.assertEqual(empty["recentEpisodes"], [])

    def test_round_trip_write_then_read(self):
        with tempfile.TemporaryDirectory() as d:
            od = Path(d) / ".openfde"
            sc.write_story_cache(od, self._graph(), limit=10, generated_at="2026-06-15T00:00:00Z")
            self.assertTrue(sc.cache_path(od).exists())
            got = sc.read_story_cache(od)
            self.assertEqual(len(got["recentEpisodes"]), 10)
            self.assertEqual(got["generatedAt"], "2026-06-15T00:00:00Z")
            self.assertTrue(got["cached"])
            self.assertFalse(got["confirmed"])             # re-stamped on read — a cache is never authoritative

    def test_read_missing_or_shape_stale_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            od = Path(d) / ".openfde"
            self.assertIsNone(sc.read_story_cache(od))     # no cache
            p = sc.cache_path(od)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"cacheVersion": "0", "recentEpisodes": [{"tag": "Pold"}]}))
            self.assertIsNone(sc.read_story_cache(od))      # version mismatch → ignored, not mis-rendered

    def test_boot_is_lightweight_tell_structures_deferred(self):
        boot = sc.build_story_boot(self._graph(), concept_cap=5)
        # The concept lanes render immediately, capped recent-first — but the heavy Tell-mode
        # structures load with the full graph, so the boot stays small.
        self.assertTrue(boot["concepts"])
        self.assertLessEqual(len(boot["concepts"]), 5)
        self.assertEqual(max(c["sequence"] for c in boot["concepts"]), 12)   # newest concepts kept
        self.assertGreaterEqual(min(c["sequence"] for c in boot["concepts"]), 8)
        self.assertEqual(boot["conceptCount"], 12)             # the full total is still reported
        self.assertEqual(boot["storyMap"], {})
        self.assertEqual(boot["storyTimeline"], {})
        self.assertEqual(boot["storyNarrative"], {})
        self.assertEqual(boot["edges"], [])


class RailBootTest(unittest.TestCase):
    def _persistence(self, eps):
        from openfde.persistence import Persistence
        d = tempfile.TemporaryDirectory()
        od = Path(d.name) / ".openfde"
        od.mkdir(parents=True)
        (od / "episodes.json").write_text(json.dumps(eps))
        return d, Persistence(od)

    def test_rail_boot_is_lite_and_carries_recent_prompts(self):
        from openfde.server import build_rail_payload
        eps = [_episode(s) for s in range(1, 6)]
        d, p = self._persistence(eps)
        with d:
            rail = build_rail_payload(p)
        self.assertTrue(rail["ok"])
        tags = {c["tag"] for c in rail["episodes"]}
        self.assertEqual(tags, {"P1", "P2", "P3", "P4", "P5"})    # recent prompt episodes present
        for chip in rail["episodes"]:
            self.assertIsNone(chip["prReadiness"])               # lite: readiness is the FULL view's job
        # Review changes is a SEPARATE worktree signal — the rail boot never carries it, so it stays
        # available the instant a worktree is dirty, independent of episode loading.
        self.assertNotIn("worktree", rail)
        self.assertNotIn("dirty", rail)


if __name__ == "__main__":
    unittest.main()
