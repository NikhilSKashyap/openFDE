"""
Tests for openfde.prompt_story.build_prompt_graph — the deterministic Prompt Story
Graph (broad active / deferred / abandoned status, plus the Step-48 lifecycle lanes
now / next / watch / deferred / abandoned derived from prompt episodes).
"""

import unittest

from openfde.prompt_story import build_prompt_graph, build_story_map, _signals


def _ep(eid, seq, title, prompt="", summary="", files=None, commits=None, status="landed"):
    return {"episodeId": eid, "sequence": seq, "tag": f"P{seq}", "title": title,
            "prompt": prompt, "summary": summary, "status": status,
            "files": files or [], "commitShas": commits or []}


class PromptStoryTest(unittest.TestCase):
    def test_active_concepts_from_titles(self):
        g = build_prompt_graph([
            _ep("e2", 2, "Prompt Chapter Rail", files=["a.jsx"], commits=["sha2"]),
            _ep("e1", 1, "Passive prompt capture", files=["b.py"], commits=["sha1a", "sha1b"]),
        ])
        active = [c for c in g["concepts"] if c["status"] == "active"]
        titles = [c["title"] for c in active]
        self.assertEqual(titles, ["Prompt Chapter Rail", "Passive prompt capture"])  # newest first
        rail = next(c for c in active if c["title"] == "Prompt Chapter Rail")
        self.assertEqual(rail["episodeTags"], ["P2"])
        self.assertEqual(rail["commitCount"], 1)
        self.assertEqual(rail["fileCount"], 1)

    def test_deferred_and_abandoned_extraction(self):
        g = build_prompt_graph([
            _ep("e1", 1, "Prompt Chapter Rail",
                prompt=("Remove nested commit chips from the rail.\n"
                        "This superseded the nested-beats approach.\n"
                        "Deferred: LLM-assisted prompt summaries.\n"
                        "Out of scope: tag-grouped column headers."),
                summary="Chapters lead; commits move into the card."),
        ])
        by_status = {}
        for c in g["concepts"]:
            by_status.setdefault(c["status"], []).append(c["title"])
        self.assertIn("Nested commit chips from the rail", by_status.get("abandoned", []))
        self.assertIn("Nested-beats approach", by_status.get("abandoned", []))
        self.assertIn("LLM-assisted prompt summaries", by_status.get("deferred", []))
        self.assertIn("Tag-grouped column headers", by_status.get("deferred", []))

    def test_feature_description_line_is_not_a_decision(self):
        # A line that mentions BOTH deferred and abandoned is describing the feature,
        # not making a decision → it must not yield a concept.
        sigs = _signals(_ep("e1", 1, "Story",
                            summary="Group concepts into active, deferred, and abandoned lanes."))
        self.assertEqual(sigs, [])

    def test_signals_link_to_their_episode(self):
        g = build_prompt_graph([
            _ep("e1", 1, "Rail", prompt="Remove nested commit chips from the rail.",
                files=["x.jsx"], commits=["s1"]),
        ])
        dropped = next(c for c in g["concepts"] if c["status"] == "abandoned")
        self.assertEqual(dropped["episodeIds"], ["e1"])
        self.assertEqual(dropped["episodeTags"], ["P1"])
        self.assertEqual(dropped["commitShas"], ["s1"])
        # An edge connects the active concept to the dropped one.
        self.assertTrue(any(e["label"] == "drops" for e in g["edges"]))

    def test_ordering_active_then_deferred_then_abandoned(self):
        g = build_prompt_graph([
            _ep("e1", 1, "Thing one", prompt="Deferred: some parked idea here.\nRemove the old widget."),
        ])
        statuses = [c["status"] for c in g["concepts"]]
        # active (the title) comes first, abandoned last.
        self.assertEqual(statuses[0], "active")
        self.assertEqual(statuses[-1], "abandoned")
        self.assertEqual(g["counts"]["active"], 1)

    def test_empty(self):
        g = build_prompt_graph([])
        self.assertTrue(g["ok"])
        self.assertEqual(g["concepts"], [])
        self.assertEqual(g["edges"], [])

    def test_story_facts_drive_lanes(self):
        # When an episode carries storyFacts (LLM or deterministic), they drive the lanes.
        g = build_prompt_graph([{
            **_ep("e1", 1, "Deterministic Title"),
            "storyFacts": {"concepts": ["Auto-Land", "Scoped Commits"], "decisions": [],
                           "deferred": ["Tool-settled signal"], "abandoned": ["Manual Land primary"],
                           "operational": False},
        }])
        by = {}
        for c in g["concepts"]:
            by.setdefault(c["status"], []).append(c["title"])
        self.assertIn("Auto-Land", by.get("active", []))
        self.assertIn("Scoped Commits", by.get("active", []))
        self.assertNotIn("Deterministic Title", by.get("active", []))   # storyFacts override the title
        self.assertIn("Tool-settled signal", by.get("deferred", []))
        self.assertIn("Manual Land primary", by.get("abandoned", []))

    def test_story_facts_operational_excluded(self):
        g = build_prompt_graph([
            {**_ep("e2", 2, "Real Concept"), "storyFacts": {"concepts": ["Real Concept"], "operational": False}},
            {**_ep("e1", 1, "Curl Thing"), "storyFacts": {"concepts": [], "operational": True}},
        ])
        titles = [c["title"] for c in g["concepts"]]
        self.assertIn("Real Concept", titles)
        self.assertNotIn("Curl Thing", titles)

    def test_operational_episodes_excluded_from_concepts(self):
        # A shell/file-list chatter episode (signal=operational) must NOT become a concept.
        g = build_prompt_graph([
            {**_ep("e2", 2, "Prompt Story Graph"), "signal": "product"},
            {**_ep("e1", 1, "Curl status", prompt="curl -s localhost:7441/api/x"), "signal": "operational"},
        ])
        titles = [c["title"] for c in g["concepts"]]
        self.assertIn("Prompt Story Graph", titles)
        self.assertNotIn("Curl status", titles)
        self.assertEqual(g["counts"]["active"], 1)

    def test_edges_expose_precedes_defers_drops_for_tell(self):
        # Story Tell renders arrows straight off these edges, so guard the contract:
        # every edge is {from,to,label} over real concept ids, and all three labels
        # appear — precedes (active spine, older→newer), defers, drops.
        g = build_prompt_graph([
            _ep("e2", 2, "Auto-Land Prompt Commits",
                prompt="Deferred: tool-settled completion signal.\nRemove the manual Land button."),
            _ep("e1", 1, "Prompt Story Graph"),
        ])
        ids = {c["id"] for c in g["concepts"]}
        for e in g["edges"]:
            self.assertEqual(set(e), {"from", "to", "label"})   # exact shape the UI reads
            self.assertIn(e["from"], ids)
            self.assertIn(e["to"], ids)
        labels = {e["label"] for e in g["edges"]}
        self.assertIn("precedes", labels)
        self.assertIn("defers", labels)
        self.assertIn("drops", labels)
        # precedes chains the two active concepts in build order (older → newer).
        active = sorted((c for c in g["concepts"] if c["status"] in ("active", "mixed")),
                        key=lambda c: c["sequence"])
        self.assertTrue(any(e["label"] == "precedes" and e["from"] == active[0]["id"]
                            and e["to"] == active[1]["id"] for e in g["edges"]))


class StoryMapTest(unittest.TestCase):
    """Story Tell v2 — chronological episode map (`build_story_map`)."""

    def test_spine_is_chronological_product_episodes_with_branches(self):
        g = build_prompt_graph([
            _ep("e3", 3, "Auto-Land Prompt Commits",
                prompt="Deferred: tool-settled completion signal.\nRemove the manual Land button.",
                files=["openfde/autoland.py"], commits=["c3"]),
            _ep("e2", 2, "Passive Prompt Capture", files=["openfde/prompt_capture.py"]),
            _ep("e1", 1, "Prompt Story Rail", files=["frontend/App.jsx"], commits=["c1"]),
            {**_ep("e0", 0, "curl status", prompt="curl -s x"), "signal": "operational"},
        ])
        sm = g["storyMap"]
        # spine = product episodes, sequence ascending; the operational one is hidden + counted.
        self.assertEqual([n["tag"] for n in sm["spine"]], ["P1", "P2", "P3"])
        self.assertEqual(sm["hiddenOps"], 1)
        # the deferred + abandoned ideas from P3 hang off P3.
        p3 = next(n for n in sm["spine"] if n["tag"] == "P3")
        self.assertTrue(any("tool-settled" in d["title"].lower() for d in p3["deferred"]))
        self.assertTrue(any("manual land" in a["title"].lower() for a in p3["abandoned"]))
        # a node carries EPISODE metrics + a (capped) file list so a click is self-sufficient.
        self.assertEqual(p3["commitCount"], 1)
        self.assertEqual(p3["fileCount"], 1)
        self.assertEqual(p3["files"], ["openfde/autoland.py"])
        self.assertGreaterEqual(p3["conceptCount"], 1)

    def test_branch_attaches_to_latest_source_episode(self):
        # An idea mentioned across two episodes hangs off the newer beat (its closest cause).
        sm = build_story_map(
            [_ep("e1", 1, "First"), _ep("e2", 2, "Second")],
            [{"id": "c1", "title": "Parked idea", "status": "deferred",
              "episodeIds": ["e1", "e2"], "episodeTags": ["P1", "P2"],
              "commitCount": 0, "fileCount": 0}])
        by_tag = {n["tag"]: n for n in sm["spine"]}
        self.assertEqual(len(by_tag["P2"]["deferred"]), 1)
        self.assertEqual(len(by_tag["P1"]["deferred"]), 0)

    def test_parks_branch_when_source_not_on_spine(self):
        sm = build_story_map(
            [_ep("e1", 1, "Real Thing")],
            [{"id": "c1", "title": "Orphan idea", "status": "abandoned",
              "episodeIds": ["ghost"], "episodeTags": ["P9"], "commitCount": 0, "fileCount": 0}])
        self.assertEqual([n["tag"] for n in sm["spine"]], ["P1"])
        self.assertEqual(len(sm["parked"]), 1)
        self.assertEqual(sm["parked"][0]["title"], "Orphan idea")
        self.assertEqual(sm["parked"][0]["fromTag"], "P9")

    def test_branches_capped_with_overflow(self):
        concepts = [{"id": f"c{i}", "title": f"Deferred {i}", "status": "deferred",
                     "episodeIds": ["e1"], "episodeTags": ["P1"], "commitCount": 0, "fileCount": 0}
                    for i in range(7)]
        sm = build_story_map([_ep("e1", 1, "Big")], concepts)
        node = sm["spine"][0]
        self.assertEqual(len(node["deferred"]), 4)        # _MAX_BRANCH_PER_EP
        self.assertEqual(node["branchOverflow"], 3)       # 7 - 4

    def test_empty(self):
        sm = build_story_map([], [])
        self.assertEqual(sm["spine"], [])
        self.assertEqual(sm["parked"], [])
        self.assertEqual(sm["hiddenOps"], 0)

    def test_watch_branch_hangs_off_its_episode(self):
        # A watch concept branches off its beat in its own lane, not as deferred.
        g = build_prompt_graph([
            _ep("e2", 2, "Later Beat"),
            _ep("e1", 1, "Story Rail", prompt="Watch: tag filters for the rail."),
        ])
        p1 = next(n for n in g["storyMap"]["spine"] if n["tag"] == "P1")
        self.assertEqual(len(p1["watch"]), 1)
        self.assertEqual(p1["watch"][0]["lifecycle"], "watch")
        self.assertIn("tag filters", p1["watch"][0]["title"].lower())
        self.assertEqual(p1["deferred"], [])


class LifecycleTest(unittest.TestCase):
    """Step 48 — the now / next / watch / deferred / abandoned lanes over broad status."""

    def test_latest_episode_concepts_are_now_older_are_next(self):
        g = build_prompt_graph([
            _ep("e2", 2, "Story Lifecycle Lanes"),
            _ep("e1", 1, "Passive Codex Capture"),
        ])
        by_title = {c["title"]: c for c in g["concepts"]}
        self.assertEqual(by_title["Story Lifecycle Lanes"]["lifecycle"], "now")
        self.assertEqual(by_title["Passive Codex Capture"]["lifecycle"], "next")
        # broad status stays "active" for both (compat) and the lane counts are returned
        self.assertEqual(by_title["Story Lifecycle Lanes"]["status"], "active")
        self.assertEqual(by_title["Passive Codex Capture"]["status"], "active")
        self.assertEqual(g["lifecycleCounts"]["now"], 1)
        self.assertEqual(g["lifecycleCounts"]["next"], 1)

    def test_explicit_next_signal_queues_concept(self):
        g = build_prompt_graph([
            _ep("e2", 2, "Story Lifecycle Lanes", prompt="Next: GitHub Issues sync."),
            _ep("e1", 1, "Passive Codex Capture"),
        ])
        nxt = next(c for c in g["concepts"] if "github issues" in c["title"].lower())
        # queued even though the mention came from the latest beat
        self.assertEqual(nxt["lifecycle"], "next")
        self.assertEqual(nxt["status"], "active")     # broad class: committed direction
        self.assertTrue(any(e["label"] == "queues" for e in g["edges"]))

    def test_watch_signals_make_watch_concepts(self):
        g = build_prompt_graph([
            _ep("e1", 1, "Story Lifecycle Lanes",
                prompt="Watch: tag filters for the rail.\nMaybe a heatmap view for hotspots."),
        ])
        watch = [c for c in g["concepts"] if c["lifecycle"] == "watch"]
        titles = " | ".join(c["title"].lower() for c in watch)
        self.assertIn("tag filters", titles)
        self.assertIn("heatmap view", titles)
        for c in watch:
            self.assertEqual(c["status"], "deferred")  # broad class stays in the old set
        self.assertEqual(g["lifecycleCounts"]["watch"], 2)

    def test_watch_signal_examples_are_not_concepts(self):
        g = build_prompt_graph([
            _ep("e1", 1, "Story Lifecycle Lanes",
                prompt=("Watch: weak-interest concepts from phrases like `Watch:`, "
                        "`interesting`, `maybe`, `consider`, `explore`, `worth watching`.")),
        ])
        watch_titles = [c["title"] for c in g["concepts"] if c["lifecycle"] == "watch"]
        self.assertEqual(watch_titles, [])

    def test_deferred_concept_carries_revisit_trigger(self):
        g = build_prompt_graph([
            _ep("e1", 1, "Capture Hardening",
                prompt="Deferred: historical import until passive Codex capture lands."),
        ])
        d = next(c for c in g["concepts"] if c["lifecycle"] == "deferred")
        self.assertEqual(d["title"], "Historical import")
        self.assertEqual(d["trigger"], "until passive Codex capture lands")

    def test_trigger_language_before_the_signal(self):
        g = build_prompt_graph([
            _ep("e1", 1, "Capture Hardening",
                prompt="Once passive capture lands, defer historical import."),
        ])
        d = next(c for c in g["concepts"] if c["lifecycle"] == "deferred")
        self.assertEqual(d["trigger"], "Once passive capture lands")
        self.assertIn("historical import", d["title"].lower())

    def test_watched_then_built_is_not_watch(self):
        # Weak interest in P1, then actually built as P2's title → the build wins.
        g = build_prompt_graph([
            _ep("e2", 2, "Tag Filters"),
            _ep("e1", 1, "Story Rail", prompt="Maybe tag filters."),
        ])
        c = next(c for c in g["concepts"] if c["title"].lower() == "tag filters")
        self.assertEqual(c["lifecycle"], "now")

    def test_operational_latest_does_not_own_now(self):
        # The newest episode is operational → "now" falls to the latest PRODUCT beat.
        g = build_prompt_graph([
            {**_ep("e3", 3, "Curl Status", prompt="curl -s x"), "signal": "operational"},
            _ep("e2", 2, "Real Feature"),
            _ep("e1", 1, "Older Feature"),
        ])
        by_title = {c["title"]: c for c in g["concepts"]}
        self.assertNotIn("Curl Status", by_title)
        self.assertEqual(by_title["Real Feature"]["lifecycle"], "now")
        self.assertEqual(by_title["Older Feature"]["lifecycle"], "next")


if __name__ == "__main__":
    unittest.main()
