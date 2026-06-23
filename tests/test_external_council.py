"""Tests for openfde.external_council — the Codex+CC council coordinator (v1).

Codex starts ONE OpenFDE episode + N OpenPM tasks bound to REAL ids (no parallel council id); the
TASKS.md header carries those ids at READY_FOR_CC; Codex records a verdict without committing.
Domain-neutral fixtures."""

import tempfile
import unittest
from pathlib import Path

from openfde import council_bus
from openfde import external_council as ec
from openfde.persistence import Persistence


class ExternalCouncilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, **kw):
        kw.setdefault("objective", "Add a widget endpoint")
        kw.setdefault("acceptance", ["focused tests pass", "lint clean"])
        return ec.create_external_council_work(self.p, **kw)

    def test_starts_one_episode_and_n_tasks(self):
        res = self._start(box_ids=["box_a"])
        eps = self.p.load_episodes()
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["episodeId"], res["episodeId"])
        self.assertEqual(eps[0]["kind"], "external-council")
        self.assertEqual(eps[0]["signal"], "product")          # joins the Story spine
        self.assertEqual(eps[0]["boxIds"], ["box_a"])
        self.assertTrue(eps[0].get("title"))
        tasks = self.p.load_tasks()
        self.assertEqual(len(tasks), 2)                        # one per acceptance bullet
        self.assertEqual([t["id"] for t in tasks], res["taskIds"])
        for t in tasks:
            self.assertEqual(t["source"], "external-council")
            self.assertEqual(t["episodeId"], res["episodeId"])
            self.assertEqual(t["column"], "todo")
            self.assertEqual(t["linkedBoxIds"], ["box_a"])

    def test_task_titles_override_acceptance(self):
        res = self._start(task_titles=["build it", "wire the route", "add a test"])
        titles = [t["title"] for t in self.p.load_tasks()]
        self.assertEqual(titles, ["build it", "wire the route", "add a test"])
        self.assertEqual(len(res["taskIds"]), 3)

    def test_tasks_md_header_uses_real_ids_and_invents_no_council_id(self):
        res = self._start(box_ids=["box_a", "box_b"])
        items = council_bus.parse_work_items(council_bus.read_bus_file(self.root, "tasks"))
        self.assertEqual(len(items), 1)
        h = items[0]["header"]
        self.assertEqual(h["episodeId"], res["episodeId"])     # the REAL OpenFDE episode id
        self.assertEqual(h["taskIds"], res["taskIds"])         # the REAL OpenPM task ids
        self.assertEqual(h["boxIds"], ["box_a", "box_b"])
        self.assertEqual(h["status"], "READY_FOR_CC")
        self.assertEqual(h["architect"], "codex")
        self.assertEqual(h["seniorDev"], "claude-code")
        self.assertEqual(h["verifier"], "codex")
        # No parallel council-id system: the only ids are OpenFDE's own.
        self.assertTrue(h["episodeId"].startswith("episode_"))
        self.assertTrue(all(t.startswith("task_") for t in h["taskIds"]))
        self.assertNotIn("councilId", h)
        self.assertNotIn("workItemId", h)

    def test_changes_requested_updates_status_and_appends_codex(self):
        res = self._start()
        out = ec.record_codex_verdict(self.root, episode_id=res["episodeId"], commit_sha="abc1234",
                                      status="CHANGES_REQUESTED", findings="missing a test for the error path")
        self.assertTrue(out["found"])
        h = council_bus.parse_work_items(council_bus.read_bus_file(self.root, "tasks"))[0]["header"]
        self.assertEqual(h["status"], "CHANGES_REQUESTED")
        self.assertEqual(h["latestCommit"], "abc1234")
        codex = council_bus.read_bus_file(self.root, "codex")
        self.assertIn("CHANGES_REQUESTED", codex)
        self.assertIn("missing a test for the error path", codex)
        self.assertIn(res["episodeId"], codex)

    def test_verified_updates_status_without_codex_committing(self):
        res = self._start()
        # No git commit is made by Codex — record_codex_verdict only touches the gitignored bus.
        out = ec.record_codex_verdict(self.root, episode_id=res["episodeId"], commit_sha="def5678",
                                      status="VERIFIED", findings="")
        self.assertEqual(out["status"], "VERIFIED")
        h = council_bus.parse_work_items(council_bus.read_bus_file(self.root, "tasks"))[0]["header"]
        self.assertEqual(h["status"], "VERIFIED")
        # The repo has no commits and Codex made none — verdict is a pure bus write.
        self.assertFalse((self.root / ".git").exists())

    def test_invalid_verdict_rejected(self):
        res = self._start()
        with self.assertRaises(ValueError):
            ec.record_codex_verdict(self.root, episode_id=res["episodeId"], commit_sha=None,
                                    status="LGTM", findings="")

    def test_read_latest_handoff_parses_claude_channel(self):
        self._start()
        council_bus.append_bus_entry(self.root, "claude", "R1 — handoff",
                                     "implemented; 3 files; tests green")
        h = ec.read_latest_handoff(self.root)
        self.assertIn("R1 — handoff", h["latestEntry"])
        self.assertIn("tests green", h["latestEntry"])
        self.assertEqual(h["binding"], {})                     # no git repo → no commit binding
        self.assertEqual(h["headSha"], "")

    def test_read_status_returns_work_items(self):
        res = self._start()
        st = ec.read_status(self.root)
        self.assertEqual(len(st["workItems"]), 1)
        self.assertEqual(st["workItems"][0]["header"]["episodeId"], res["episodeId"])
        self.assertIn("handoff", st)


if __name__ == "__main__":
    unittest.main()
