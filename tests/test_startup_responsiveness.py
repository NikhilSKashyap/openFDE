"""
Tests for the startup-responsiveness contract.

Law: /api/boot serves CACHED/cheap state only — it must NEVER run analyze_repo, the backfill scan,
or the semantic graph (those run in the background). analyze_repo runs off the event loop on a
THREAD executor — a ProcessPool was deferred for shutdown reliability — but analyze_repo is kept a
top-level function so process isolation stays an option if we revisit it.
"""
import pickle
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfde import boot_cache, server
from openfde.persistence import Persistence


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


class BootContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@e.com")
        _git(self.root, "config", "user.name", "T")
        (self.root / "a.py").write_text("x = 1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def test_boot_never_invokes_analyze_backfill_or_semantic_graph(self):
        # Seed restorable state, then patch every heavy function to explode if touched.
        self.p.upsert_episode({"episodeId": "e1", "source": "openfde-capture", "status": "landed",
                               "createdAt": "2026-06-10T00:00:00Z"})
        self.p.add_backfill_candidate({"episodeId": "c1", "source": "openfde-backfill",
                                       "backfillConfidence": "discussion", "captureKey": "k1"})
        ident = {"repoName": "r", "branch": "main", "gitRoot": str(self.root)}
        with mock.patch("openfde.architect.analyze_repo",
                        side_effect=AssertionError("boot must not analyze")) as analyze, \
             mock.patch("openfde.backfill.backfill_historical",
                        side_effect=AssertionError("boot must not backfill")) as backfill, \
             mock.patch("openfde.server.git_status",
                        side_effect=AssertionError("boot must be cache-only (no git)")) as gitst, \
             mock.patch("openfde.semantic_graph.build_graph",
                        side_effect=AssertionError("boot must not build the semantic graph")) as sem:
            # identity is precomputed at server start, so the cache-only boot spawns NO git subprocess
            payload = server.build_boot_payload(self.root, self.p, "started", "9.9.9", identity=ident)
        analyze.assert_not_called()
        backfill.assert_not_called()
        gitst.assert_not_called()
        sem.assert_not_called()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["episodeCount"], 1)            # real episode restored
        self.assertEqual(payload["candidateCount"], 1)          # quarantined candidate counted
        self.assertEqual(payload["taskCount"], 0)
        self.assertEqual(payload["restoredFrom"], "P1")         # the landed episode's clean tag

    def test_boot_restore_path_cheap_then_warm(self):
        cold = server.build_boot_payload(self.root, self.p, "s", "v")
        self.assertEqual(cold["restorePath"], "cheap-scan")     # no cache yet → 3ms fs scan
        self.assertFalse(cold["hasSnapshot"])

        boot_cache.write_warm(self.p.openfde_dir, file_tree={"name": "r", "children": []},
                              arch={"files": [{"path": "a.py"}]}, head="h", dirty_sig="s")
        warm = server.build_boot_payload(self.root, self.p, "s", "v")
        self.assertEqual(warm["restorePath"], "warm-cache")     # served from disk snapshot
        self.assertTrue(warm["hasSnapshot"])
        self.assertIsNone(warm["canvasSnapshot"])               # tiny by default (no ?canvas=1)

    def test_analyze_repo_stays_top_level_picklable(self):
        # Runs on a thread today; kept top-level/picklable so process isolation remains an option
        # (ProcessPool deferred for shutdown reliability).
        from openfde.architect import analyze_repo
        self.assertTrue(pickle.dumps(analyze_repo))

    def test_rail_payload_is_cheap_and_never_touches_git(self):
        # The default /api/review/episodes is the prompt-rail poll — it must serve persisted state
        # only. Patch every heavy seam (git, reconciliation, readiness) to explode and prove the
        # rail builds without them; the 49s git-heavy detail is the separate /full endpoint.
        self.p.upsert_episode({
            "episodeId": "e1", "source": "openfde-capture", "status": "landed",
            "title": "Add login", "files": ["a.py", "b.py"], "commitShas": ["abc1234def0"],
            "createdAt": "2026-06-10T00:00:00Z",
            "prompt": "X" * 4000,                                # big fields that must NOT ship on the rail
            "storyFacts": {"operational": False, "concepts": [{"name": "c"}] * 50},
            "commitMeta": {"abc1234def0": {"title": "feat: login", "summary": "the thing"}}})
        with mock.patch("openfde.server.git_status",
                        side_effect=AssertionError("rail must not git_status")), \
             mock.patch("openfde.server.git_timeline",
                        side_effect=AssertionError("rail must not git_timeline")), \
             mock.patch("openfde.server.commit_files",
                        side_effect=AssertionError("rail must not `git show`")), \
             mock.patch("openfde.server.pr_readiness",
                        side_effect=AssertionError("rail must not compute PR readiness")), \
             mock.patch("openfde.server.episode_commits_mod.reconcile_episodes",
                        side_effect=AssertionError("rail must not reconcile")):
            payload = server.build_rail_payload(self.p)
        eps = payload["episodes"]
        self.assertEqual(len(eps), 1)
        chip = eps[0]
        self.assertEqual(chip["commitCount"], 1)
        self.assertEqual(chip["fileCount"], 2)                   # the COUNT, not the files array
        self.assertIsNone(chip["prReadiness"])                   # readiness loads on demand, not on the rail
        # TINY chip: the heavy per-episode fields must NOT be shipped on the 15s poll.
        self.assertNotIn("prompt", chip)                         # was ~4KB of prompt text
        self.assertNotIn("files", chip)                          # array dropped; fileCount kept
        self.assertEqual(chip["storyFacts"], {"operational": False})   # operational flag only, no concepts
        import json as _json
        self.assertLess(len(_json.dumps(chip)), 600)             # a chip is small + fixed-size
        self.assertEqual(eps[0]["commits"][0]["displayTitle"], "feat: login")  # cached title, no `git show`
        self.assertEqual(payload["outside"]["commits"], [])      # Outside bucket is the /full endpoint's job

    def test_boot_canvas_first_paint_is_cache_only(self):
        # First paint hydrates from ONE call: the persisted canvas boxes (the modules — the canvas
        # is blank without them), the warm arch snapshot, and the cached file tree. Patch git /
        # analyze / file-tree-scan to explode and prove first paint never touches them.
        self.p.save_state({"boxes": [{"id": "box:module:api", "title": "api"}], "arrows": []})
        boot_cache.write_warm(self.p.openfde_dir, file_tree={"name": "root", "children": []},
                              arch={"files": [{"path": "a.py"}]}, head="h", dirty_sig="s")
        with mock.patch("openfde.server.git_status",
                        side_effect=AssertionError("first paint must not git")), \
             mock.patch("openfde.architect.analyze_repo",
                        side_effect=AssertionError("first paint must not analyze")), \
             mock.patch("openfde.server.build_file_tree",
                        side_effect=AssertionError("first paint must not scan the file tree")):
            payload = server.build_boot_canvas(self.p)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["boxes"]), 1)               # the persisted module box → modules render
        self.assertTrue(payload["hasCanvas"])
        self.assertNotIn("arch", payload)                        # the ~1.5MB arch is NOT shipped on first paint
        self.assertTrue(payload["hasSnapshot"])                  # …but the UI knows one exists (loads via /api/archgraph)
        self.assertEqual(payload["fileTree"]["name"], "root")    # cached Explorer tree

    def test_boot_canvas_empty_before_first_scan(self):
        # A never-scanned repo (no persisted canvas, no warm cache) returns empties — the only case
        # where "Scan repo → canvas" is legitimately shown.
        payload = server.build_boot_canvas(self.p)
        self.assertEqual(payload["boxes"], [])
        self.assertFalse(payload["hasCanvas"])
        self.assertFalse(payload["hasSnapshot"])

    def test_latest_terminal_tag_picks_newest_terminal(self):
        self.p.upsert_episode({"episodeId": "old", "status": "open",
                               "createdAt": "2026-06-09T00:00:00Z"})
        self.p.upsert_episode({"episodeId": "done", "status": "needs_manual_land",
                               "createdAt": "2026-06-10T00:00:00Z"})
        self.assertEqual(server.latest_terminal_tag(self.p), "P2")   # 'done' (newest terminal)


if __name__ == "__main__":
    unittest.main()
