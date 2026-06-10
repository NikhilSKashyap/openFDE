"""
Tests for openfde.autoland + openfde.git_timeline.git_commit_paths — scoped Auto-Land.

The core guarantee: Auto-Land commits ONLY the files attributed to an episode, never
sweeping unrelated dirty files into the prompt; ambiguous attribution stays
``needs_manual_land`` (manual Land remains the fallback).
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde import git_timeline as gt
from openfde import autoland
from openfde.persistence import Persistence


class ScopedCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._g("init", "-q")
        self._g("config", "user.email", "t@e.com")
        self._g("config", "user.name", "T")
        self._g("config", "commit.gpgsign", "false")
        (self.root / ".gitignore").write_text("\n".join(gt._IGNORE_ENTRIES) + "\n")
        (self.root / "a.py").write_text("a1\n")
        (self.root / "b.py").write_text("b1\n")
        (self.root / "del.py").write_text("d1\n")
        self._g("add", "-A")
        self._g("-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "init")
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def _g(self, *a):
        return subprocess.run(["git", *a], cwd=str(self.root), capture_output=True, text=True)

    def _porcelain(self):
        return self._g("status", "--porcelain").stdout.strip()

    def _landed_files(self):
        return self._g("show", "--name-only", "--format=", "HEAD").stdout.split()


    # 1) git_commit_paths stages only the listed paths (incl. deletion + new file).
    def test_commit_paths_isolates_listed_paths(self):
        (self.root / "a.py").write_text("a2\n")
        (self.root / "del.py").unlink()
        (self.root / "new.py").write_text("n\n")
        (self.root / "b.py").write_text("b2-unrelated\n")          # NOT listed
        res = gt.git_commit_paths(self.root, "openfde: scoped", ["a.py", "del.py", "new.py"])
        self.assertTrue(res["committed"])
        self.assertEqual(set(self._landed_files()), {"a.py", "del.py", "new.py"})
        self.assertEqual(self._porcelain(), "M b.py")              # unrelated stays dirty

    # 2) Ignored paths are dropped (never force-added).
    def test_commit_paths_drops_ignored(self):
        (self.root / "PLAN.md").write_text("plan\n")               # *.md ignored via .gitignore? add rule
        self._g("config", "core.excludesfile", "/dev/null")
        # .openfde/ is ignored by our entries; a path under it must be dropped.
        (self.root / ".openfde").mkdir(exist_ok=True)
        (self.root / ".openfde" / "x").write_text("x\n")
        res = gt.git_commit_paths(self.root, "openfde: ig", [".openfde/x"])
        self.assertFalse(res["committed"])
        self.assertIn("ignored", (res["reason"] or ""))

    # 3) auto-land happy path: episode files committed, unrelated dirty preserved.
    def test_auto_land_scoped(self):
        (self.root / "a.py").write_text("a9\n")
        (self.root / "b.py").write_text("b9-unrelated\n")
        ep = self.p.upsert_episode({"episodeId": "episode_1", "prompt": "Edit a", "kind": "claude-code",
                                    "status": "reviewing", "files": ["a.py"], "runIds": ["run_1"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, ep, auto=True)
        self.assertTrue(res["committed"])
        self.assertEqual(res["status"], "landed")
        self.assertEqual(set(self._landed_files()), {"a.py"})
        self.assertEqual(self._porcelain(), "M b.py")
        self.assertEqual(self.p.get_episode("episode_1")["status"], "landed")
        msg = self._g("log", "-1", "--pretty=%B").stdout
        self.assertIn("OpenFDE-Episode: episode_1", msg)
        # broadcasts include episode_updated + commit_created for live UI mirroring.
        types = {m["type"] for m in res["broadcasts"]}
        self.assertIn("commit_created", types)

    # 4) Ambiguous attribution → needs_manual_land (not committed).
    def test_ambiguous_overlap_needs_manual(self):
        (self.root / "a.py").write_text("aX\n")
        self.p.upsert_episode({"episodeId": "e1", "prompt": "x", "status": "reviewing", "files": ["a.py"], "commitShas": []})
        self.p.upsert_episode({"episodeId": "e2", "prompt": "y", "status": "reviewing", "files": ["a.py"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, self.p.get_episode("e1"), auto=True)
        self.assertFalse(res["committed"])
        self.assertEqual(res["status"], "needs_manual_land")
        self.assertEqual(self._porcelain(), "M a.py")              # nothing committed

    # 5) Empty file set → needs_manual_land (auto) / needsWholeTree (manual).
    def test_empty_files(self):
        ep = self.p.upsert_episode({"episodeId": "e3", "prompt": "z", "status": "reviewing", "files": [], "commitShas": []})
        auto = autoland.land_episode(self.root, self.p, dict(ep), auto=True)
        self.assertEqual(auto["status"], "needs_manual_land")
        manual = autoland.land_episode(self.root, self.p, dict(ep), auto=False)
        self.assertTrue(manual["needsWholeTree"])                  # caller falls back to whole-tree

    # 6) Episode files exist but none dirty → complete_no_changes.
    def test_no_dirty_complete(self):
        ep = self.p.upsert_episode({"episodeId": "e4", "prompt": "w", "status": "reviewing", "files": ["a.py"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, ep, auto=True)
        self.assertEqual(res["status"], "complete_no_changes")
        self.assertFalse(res["committed"])

    # 7) Clustered Auto-Land: an episode spanning two scopes lands as TWO commits, both
    #    attributed to the episode, with a durable per-commit title (commitMeta) + one
    #    commit_created broadcast each — i.e. one OpenPM task per logical change.
    def test_clustered_multi_commit(self):
        (self.root / "a.py").write_text("a-clustered\n")               # scope "." → own commit
        (self.root / "frontend").mkdir()
        (self.root / "frontend" / "App.jsx").write_text("ui\n")        # scope "frontend"
        ep = self.p.upsert_episode({"episodeId": "episode_2", "title": "Clustered Land",
                                    "prompt": "do stuff", "status": "reviewing",
                                    "files": ["a.py", "frontend/App.jsx"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, ep, auto=True)   # allow_llm=False → by-scope
        self.assertTrue(res["committed"])
        self.assertEqual(len(res["commits"]), 2)                        # one commit per scope
        saved = self.p.get_episode("episode_2")
        self.assertEqual(len(saved["commitShas"]), 2)
        titles = {m["title"] for m in saved["commitMeta"].values()}     # durable per-commit titles
        self.assertEqual(len(titles), 2)
        cc = [m for m in res["broadcasts"] if m["type"] == "commit_created"]
        self.assertEqual(len(cc), 2)                                    # → two OpenPM cards
        self.assertTrue(all(m.get("displayTitle") for m in cc))
        committed = set()
        for sha in saved["commitShas"]:
            committed |= set(self._g("show", "--name-only", "--format=", sha).stdout.split())
        self.assertEqual(committed, {"a.py", "frontend/App.jsx"})       # every file landed
        self.assertEqual(self._porcelain(), "")                         # nothing left dirty
        # each commit carries the episode trailer.
        for sha in saved["commitShas"]:
            self.assertIn("OpenFDE-Episode: episode_2",
                          self._g("log", "-1", "--pretty=%B", sha).stdout)

    # ── Verify Gate Evidence v1 ────────────────────────────────────────

    def _verify_result(self, status, summary="2 tests failed"):
        check = {"id": "unit-tests", "label": "Unit tests", "command": "python3 -m unittest",
                 "required": True, "status": "failed" if status == "failed" else "passed",
                 "summary": summary, "exitCode": 1 if status == "failed" else 0}
        return {"status": status, "checks": [] if status == "skipped" else [check],
                "ranAt": "2026-06-09T00:00:00Z", "durationMs": 5}

    # 8) Auto-land BLOCKS on a failed required check: nothing committed, episode parked
    #    for review, and the red evidence stored on the episode.
    def test_auto_land_blocked_on_failed_verify(self):
        (self.root / "a.py").write_text("a-gated\n")
        ep = self.p.upsert_episode({"episodeId": "e_gate", "prompt": "gated", "status": "reviewing",
                                    "files": ["a.py"], "commitShas": []})
        head = self._g("rev-parse", "HEAD").stdout.strip()
        res = autoland.land_episode(self.root, self.p, ep, auto=True,
                                    run_verify=lambda root: self._verify_result("failed"))
        self.assertFalse(res["committed"])
        self.assertEqual(res["status"], "needs_manual_land")
        self.assertIn("verification failed", res["reason"])
        self.assertIn("2 tests failed", res["reason"])
        self.assertEqual(self._g("rev-parse", "HEAD").stdout.strip(), head)   # no commit
        self.assertEqual(self._porcelain(), "M a.py")                          # work intact
        saved = self.p.get_episode("e_gate")
        self.assertEqual(saved["verify"]["status"], "failed")                  # receipts kept

    # 9) Manual Land is the escape hatch: it proceeds on failure, with the failure
    #    recorded on the episode (visible, never hidden).
    def test_manual_land_proceeds_on_failed_verify(self):
        (self.root / "a.py").write_text("a-manual\n")
        ep = self.p.upsert_episode({"episodeId": "e_hatch", "prompt": "hatch", "status": "reviewing",
                                    "files": ["a.py"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, ep, auto=False,
                                    run_verify=lambda root: self._verify_result("failed"))
        self.assertTrue(res["committed"])
        self.assertEqual(self.p.get_episode("e_hatch")["verify"]["status"], "failed")

    # 10) No checks configured → skipped evidence is recorded and the land proceeds —
    #     explicit "verification not configured", never silent success.
    def test_skipped_evidence_recorded_on_land(self):
        (self.root / "a.py").write_text("a-skip\n")
        ep = self.p.upsert_episode({"episodeId": "e_skip", "prompt": "skip", "status": "reviewing",
                                    "files": ["a.py"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, ep, auto=True,
                                    run_verify=lambda root: self._verify_result("skipped"))
        self.assertTrue(res["committed"])
        self.assertEqual(self.p.get_episode("e_skip")["verify"]["status"], "skipped")

    # 11b) Evidence overrides classification: landing real commits clears the
    #      operational mislabel (it hid episodes from the rail and blocked their
    #      PR readiness — a landed diff is product work by definition).
    def test_land_reclassifies_operational_episode(self):
        (self.root / "a.py").write_text("a-reclass\n")
        ep = self.p.upsert_episode({"episodeId": "e_op", "prompt": "real work", "status": "reviewing",
                                    "signal": "operational",
                                    "storyFacts": {"operational": True, "concepts": []},
                                    "files": ["a.py"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, ep, auto=True,
                                    run_verify=lambda root: self._verify_result("skipped"))
        self.assertTrue(res["committed"])
        saved = self.p.get_episode("e_op")
        self.assertEqual(saved["signal"], "product")
        self.assertFalse(saved["storyFacts"]["operational"])
        self.assertEqual(saved["reclassifiedBy"], "landed-commits")

    # 11) Passing checks ride the episode into the landed state.
    def test_passing_verify_recorded_on_land(self):
        (self.root / "a.py").write_text("a-pass\n")
        ep = self.p.upsert_episode({"episodeId": "e_pass", "prompt": "ok", "status": "reviewing",
                                    "files": ["a.py"], "commitShas": []})
        res = autoland.land_episode(self.root, self.p, ep, auto=True,
                                    run_verify=lambda root: self._verify_result("passed", "155 OK"))
        self.assertTrue(res["committed"])
        saved = self.p.get_episode("e_pass")
        self.assertEqual(saved["verify"]["status"], "passed")
        self.assertEqual(saved["status"], "landed")


if __name__ == "__main__":
    unittest.main()
