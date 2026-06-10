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

    def test_story_facts_noisy_concepts_are_filtered(self):
        # Existing persisted episodes can already contain local-LLM noise. The graph
        # boundary should self-heal those without rewriting episodes.json.
        g = build_prompt_graph([{
            **_ep("e1", 1, "GitHub Issue Intents"),
            "storyFacts": {
                "concepts": ["GitHub issue intent source", "Store"],
                "deferred": ["/ `next slice` / `next up`", "Full OAuth support"],
                "abandoned": [],
                "operational": False,
            },
        }])
        titles = [c["title"] for c in g["concepts"]]
        self.assertIn("GitHub issue intent source", titles)
        self.assertIn("Full OAuth support", titles)
        self.assertNotIn("Store", titles)
        self.assertNotIn("/ `next slice` / `next up`", titles)

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


class StoryTimelineTest(unittest.TestCase):
    """Story Timeline v3 — the merged Story+Timeline structure (`storyTimeline`)."""

    def _rich_ep(self, eid, seq, title, **over):
        base = _ep(eid, seq, title)
        base["createdAt"] = f"2026-06-09T0{seq}:00:00+00:00"
        base["updatedAt"] = f"2026-06-09T0{seq}:30:00+00:00"
        base.update(over)
        return base

    def test_spine_is_chronological_and_ops_excluded(self):
        g = build_prompt_graph([
            self._rich_ep("e3", 3, "Land As PR"),
            self._rich_ep("e1", 1, "Verify Gate"),
            {**self._rich_ep("e2", 2, "curl status"), "signal": "operational"},
        ])
        tl = g["storyTimeline"]
        self.assertEqual([n["tag"] for n in tl["spine"]], ["P1", "P3"])
        self.assertEqual(tl["hiddenOps"], 1)
        self.assertEqual(len(tl["bridges"]), 1)               # N-1 bridges

    def test_branches_split_above_and_below(self):
        g = build_prompt_graph([
            self._rich_ep("e1", 1, "Shipping Panel",
                          prompt=("Watch: heatmap view someday maybe.\n"
                                  "Deferred: PR state sync until webhooks land.\n"
                                  "Next: auto ship mode.\n"
                                  "Remove the global clean-tree rule.")),
        ])
        node = g["storyTimeline"]["spine"][0]
        ups = {b["lifecycle"] for b in node["branchesAbove"]}
        downs = {b["lifecycle"] for b in node["branchesBelow"]}
        self.assertIn("deferred", ups)
        self.assertIn("next", ups)                            # explicitly queued → above
        self.assertEqual(downs, {"abandoned"})                # dropped → below
        up_titles = " ".join(b["title"] for b in node["branchesAbove"]).lower()
        self.assertIn("auto ship mode", up_titles)

    def test_bridge_carries_commit_verify_pr_issue_and_files(self):
        sha = "c" * 40
        a = self._rich_ep("e1", 1, "Verify Gate",
                          files=["openfde/verify.py"], commitShas=[sha],
                          commitMeta={sha: {"title": "Gate Receipts"}},
                          verify={"status": "failed", "ranAt": "2026-06-09T01:10:00+00:00",
                                  "checks": [
                                      {"id": "unit-tests", "label": "Unit tests",
                                       "status": "passed", "summary": "OK"},
                                      {"id": "frontend-lint", "label": "Frontend lint",
                                       "status": "failed", "summary": "2 problems"}]},
                          pr={"number": 7, "url": "https://github.com/a/r/pull/7",
                              "state": "OPEN", "createdAt": "2026-06-09T01:20:00+00:00"},
                          intentSource={"provider": "github", "issueNumber": 42,
                                        "url": "https://github.com/a/r/issues/42"})
        b = self._rich_ep("e2", 2, "Land As PR")
        g = build_prompt_graph([b, a])
        bridge = g["storyTimeline"]["bridges"][0]
        self.assertEqual(bridge["fromEpisodeId"], "e1")
        by_kind = {}
        for t in bridge["events"]:
            by_kind.setdefault(t["kind"], []).append(t)
        self.assertEqual(by_kind["commit"][0]["label"], f"commit {sha[:7]}")
        self.assertEqual(by_kind["commit"][0]["detail"], "Gate Receipts")
        verify_labels = {t["label"] for t in by_kind["verify"]}
        self.assertIn("tests ✓", verify_labels)               # pass + fail both tick
        self.assertIn("lint ✕", verify_labels)
        self.assertEqual(by_kind["pr"][0]["label"], "PR #7")
        self.assertEqual(by_kind["pr"][0]["url"], "https://github.com/a/r/pull/7")
        self.assertEqual(by_kind["issue"][0]["label"], "issue #42")
        self.assertEqual(by_kind["files"][0]["label"], "1 file")   # everything displays
        self.assertEqual(bridge["overflow"], 0)
        # spine node carries the lite verify/pr/issue too
        n = g["storyTimeline"]["spine"][0]
        self.assertEqual(n["pr"]["number"], 7)
        self.assertEqual(n["issue"]["number"], 42)
        self.assertEqual(n["verify"]["status"], "failed")

    def test_raw_events_bucket_between_beats(self):
        a = self._rich_ep("e1", 1, "First")
        b = self._rich_ep("e2", 2, "Second")
        events = [
            {"type": "task_moved", "payload": {"title": "Card to Done"},
             "timestamp": "2026-06-09T01:45:00+00:00"},          # between e1 and e2
            {"type": "task_moved", "payload": {"title": "Too late"},
             "timestamp": "2026-06-09T09:00:00+00:00"},          # after e2 → no bridge
            {"type": "commit_created", "payload": {"shortSha": "abc"},
             "timestamp": "2026-06-09T01:50:00+00:00"},          # derived elsewhere → skipped
        ]
        g = build_prompt_graph([b, a], events=events)
        bridge = g["storyTimeline"]["bridges"][0]
        labels = [t["label"] for t in bridge["events"]]
        self.assertIn("Card to Done", labels)
        self.assertNotIn("Too late", labels)
        self.assertFalse(any(t.get("type") == "commit_created" for t in bridge["events"]))
        self.assertEqual(len(g["storyTimeline"]["rawEvents"]), 3)   # Events layer keeps all

    def test_none_createdat_skips_bucketing_but_keeps_derived_ticks(self):
        sha = "d" * 40
        a = self._rich_ep("e1", 1, "Reconstructed", commitShas=[sha])
        a["createdAt"] = None                                   # recovered episodes can lack it
        b = self._rich_ep("e2", 2, "Next Beat")
        events = [{"type": "task_moved", "payload": {"title": "X"},
                   "timestamp": "2026-06-09T01:45:00+00:00"}]
        g = build_prompt_graph([b, a], events=events)
        bridge = g["storyTimeline"]["bridges"][0]
        kinds = {t["kind"] for t in bridge["events"]}
        self.assertIn("commit", kinds)                          # derived ticks survive
        self.assertNotIn("event", kinds)                        # bucketing safely skipped

    def test_derived_evidence_is_never_trimmed(self):
        # Product rule: the storyline displays EVERYTHING — all 9 commits AND the
        # files tick render (the old cap would have trimmed to 5); only bucketed
        # raw event-log items are capped (the Events layer holds their tail).
        shas = [f"{i:040x}" for i in range(9)]
        a = self._rich_ep("e1", 1, "Busy", commitShas=shas, files=["x.py", "y.py"])
        b = self._rich_ep("e2", 2, "After")
        g = build_prompt_graph([b, a])
        bridge = g["storyTimeline"]["bridges"][0]
        self.assertEqual(len([t for t in bridge["events"] if t["kind"] == "commit"]), 9)
        kinds = [t["kind"] for t in bridge["events"]]
        self.assertIn("files", kinds)                           # nothing trimmed away
        self.assertEqual(len(bridge["events"]), 10)             # 9 commits + files
        self.assertEqual(bridge["overflow"], 0)

    def test_branches_are_never_capped(self):
        prompt = "\n".join(f"Deferred: parked idea number {i} here." for i in range(6))
        g = build_prompt_graph([self._rich_ep("e1", 1, "Busy Beat", prompt=prompt)])
        node = g["storyTimeline"]["spine"][0]
        self.assertGreaterEqual(len(node["branchesAbove"]), 3)  # signal-cap only, no UI cap
        self.assertEqual(node["branchOverflow"], 0)             # nothing hidden behind "+N"

    def test_receipts_lead_the_bridge(self):
        # The cap trims from the tail — verify/PR must survive a many-commit episode.
        shas = [f"{i:040x}" for i in range(8)]
        a = self._rich_ep("e1", 1, "Busy", commitShas=shas,
                          verify={"status": "passed", "checks": [
                              {"id": "unit-tests", "label": "Unit tests",
                               "status": "passed", "summary": "OK"}]},
                          pr={"number": 9, "url": "https://github.com/a/r/pull/9",
                              "state": "OPEN"})
        b = self._rich_ep("e2", 2, "After")
        g = build_prompt_graph([b, a])
        kinds = [t["kind"] for t in g["storyTimeline"]["bridges"][0]["events"]]
        self.assertEqual(kinds[0], "verify")
        self.assertEqual(kinds[1], "pr")
        self.assertIn("commit", kinds[2:])

    def test_empty_graph_safe(self):
        tl = build_prompt_graph([])["storyTimeline"]
        self.assertEqual(tl["spine"], [])
        self.assertEqual(tl["bridges"], [])
        self.assertEqual(tl["rawEvents"], [])


class NarrativeGraphTest(unittest.TestCase):
    """Narrative Graph v1 — spine vs branch lanes, parents, explaining edges."""

    def _ep(self, eid, seq, title, **over):
        base = _ep(eid, seq, title)
        base["createdAt"] = f"2026-06-09T0{seq}:00:00+00:00"
        base.update(over)
        return base

    def test_spine_skips_exploration_between_beats(self):
        # P69 mainline, P70 keyword-exploration sharing files, P71 continuation:
        # spine = 69 -> 71; 69 --explores--> 70 hangs off it.
        eps = [
            self._ep("e69", 1, "Story Board", files=["story.py"], commitShas=["a" * 40]),
            self._ep("e70", 2, "Explore an alternative spine layout",
                     prompt="Let's prototype an alternative approach.",
                     files=["story.py"], commitShas=["b" * 40]),
            self._ep("e71", 3, "Story Board Polish", files=["story.py"], commitShas=["c" * 40]),
        ]
        nv = build_prompt_graph(list(reversed(eps)))["storyNarrative"]
        self.assertEqual(nv["spineEpisodeIds"], ["e69", "e71"])
        self.assertEqual(nv["branchEpisodeIds"], ["e70"])
        n70 = next(n for n in nv["nodes"] if n["episodeId"] == "e70")
        self.assertEqual(n70["lane"], "explore")
        self.assertEqual(n70["parentEpisodeId"], "e69")
        ex = next(e for e in nv["edges"] if e["kind"] == "explores")
        self.assertEqual((ex["fromEpisodeId"], ex["toEpisodeId"]), ("e69", "e70"))
        cont = next(e for e in nv["edges"] if e["kind"] == "continues")
        self.assertEqual((cont["fromEpisodeId"], cont["toEpisodeId"]), ("e69", "e71"))
        self.assertTrue(any(t["kind"] == "commit" for t in cont["events"]))

    def test_superseded_episode_drops_below_and_returns(self):
        # A later revert beat marks its overlap-neighbour as dropped (high
        # confidence) and the dropped path points back at the returning beat.
        eps = [
            self._ep("e1", 1, "Evidence Ladder", files=["story.jsx"], commitShas=["a" * 40]),
            self._ep("e2", 2, "Persistent Canvas Tools", files=["story.jsx"], commitShas=["b" * 40]),
            self._ep("e3", 3, "Collaborative Layout",
                     prompt="i liked it before - revert back to the previous implementation",
                     files=["story.jsx"], commitShas=["c" * 40]),
        ]
        nv = build_prompt_graph(list(reversed(eps)))["storyNarrative"]
        self.assertEqual(nv["spineEpisodeIds"], ["e1", "e3"])
        n2 = next(n for n in nv["nodes"] if n["episodeId"] == "e2")
        self.assertEqual(n2["lane"], "abandoned")
        self.assertEqual(n2["confidence"], "high")
        self.assertIn("superseded", n2["narrativeReason"])
        drop = next(e for e in nv["edges"] if e["kind"] == "drops" and e["toEpisodeId"] == "e2")
        self.assertEqual(drop["fromEpisodeId"], "e1")
        ret = next(e for e in nv["edges"] if e["kind"] == "returns")
        self.assertEqual((ret["fromEpisodeId"], ret["toEpisodeId"]), ("e2", "e3"))

    def test_concept_branches_become_forward_edges(self):
        eps = [
            self._ep("e1", 1, "Build Auth",
                     prompt="Build login.\nDeferred: full OAuth integration until launch lands."),
            self._ep("e2", 2, "Polish Auth"),
        ]
        nv = build_prompt_graph(list(reversed(eps)))["storyNarrative"]
        defers = [e for e in nv["edges"] if e["kind"] == "defers"]
        self.assertTrue(defers)
        self.assertEqual(defers[0]["fromEpisodeId"], "e1")        # parent = its episode
        self.assertIsNone(defers[0]["toEpisodeId"])               # concept, not episode
        self.assertTrue(defers[0]["toConceptId"])
        self.assertEqual(defers[0]["label"], "deferred")

    def test_weak_evidence_falls_back_to_previous_episode(self):
        eps = [
            self._ep("e1", 1, "Backend Work", files=["api.py"], commitShas=["a" * 40]),
            self._ep("e2", 2, "Compare frontend frameworks",
                     prompt="comparison spike", files=["notes.md"]),   # zero overlap
            self._ep("e3", 3, "Backend Work II", files=["api.py"], commitShas=["b" * 40]),
        ]
        nv = build_prompt_graph(list(reversed(eps)))["storyNarrative"]
        n2 = next(n for n in nv["nodes"] if n["episodeId"] == "e2")
        self.assertEqual(n2["lane"], "explore")
        self.assertEqual(n2["parentEpisodeId"], "e1")             # previous product ep
        ex = next(e for e in nv["edges"] if e["kind"] == "explores")
        self.assertEqual(ex["evidence"].get("fallback"), "previous product episode")

    def test_latest_episode_always_on_spine(self):
        # Even with exploration wording, the current beat is "now", never a branch.
        eps = [self._ep("e1", 1, "Mainline", commitShas=["a" * 40]),
               self._ep("e2", 2, "Explore narrative graph prototypes",
                        prompt="explore alternatives, prototype")]
        nv = build_prompt_graph(list(reversed(eps)))["storyNarrative"]
        n2 = next(n for n in nv["nodes"] if n["episodeId"] == "e2")
        self.assertEqual(n2["lane"], "spine")
        self.assertEqual(n2["narrativeReason"], "current beat")
        self.assertEqual(nv["spineEpisodeIds"], ["e1", "e2"])



if __name__ == "__main__":
    unittest.main()
