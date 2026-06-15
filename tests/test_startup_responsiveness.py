"""
Tests for the startup-responsiveness contract.

Law: /api/boot serves CACHED/cheap state only — it must NEVER run analyze_repo, the backfill scan,
or the semantic graph (those are background jobs on a worker process). The heavy worker function
must stay top-level/picklable so it can run on the ProcessPoolExecutor.
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
        with mock.patch("openfde.architect.analyze_repo",
                        side_effect=AssertionError("boot must not analyze")) as analyze, \
             mock.patch("openfde.backfill.backfill_historical",
                        side_effect=AssertionError("boot must not backfill")) as backfill, \
             mock.patch("openfde.semantic_graph.build_graph",
                        side_effect=AssertionError("boot must not build the semantic graph")) as sem:
            payload = server.build_boot_payload(self.root, self.p, "started", "9.9.9")
        analyze.assert_not_called()
        backfill.assert_not_called()
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

    def test_heavy_worker_is_picklable_for_the_process_pool(self):
        from openfde.architect import analyze_repo
        self.assertTrue(pickle.dumps(analyze_repo))             # top-level → runnable in a worker process

    def test_latest_terminal_tag_picks_newest_terminal(self):
        self.p.upsert_episode({"episodeId": "old", "status": "open",
                               "createdAt": "2026-06-09T00:00:00Z"})
        self.p.upsert_episode({"episodeId": "done", "status": "needs_manual_land",
                               "createdAt": "2026-06-10T00:00:00Z"})
        self.assertEqual(server.latest_terminal_tag(self.p), "P2")   # 'done' (newest terminal)


if __name__ == "__main__":
    unittest.main()
