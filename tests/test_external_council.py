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

    def test_render_inbox_restores_active_only(self):
        res = self._start()                                    # READY_FOR_CC → active
        inbox = ec.render_inbox(self.root)
        self.assertTrue(inbox["active"])
        self.assertEqual(inbox["event"]["direction"], "codex_to_claude")
        self.assertEqual(inbox["event"]["episodeId"], res["episodeId"])
        ec.record_codex_verdict(self.root, episode_id=res["episodeId"], commit_sha="x", status="VERIFIED")
        self.assertFalse(ec.render_inbox(self.root)["active"])  # done → no stale bubble restored

    def test_bus_snapshot_keys_by_episode(self):
        res = self._start()
        snap = ec.bus_snapshot(self.root)
        self.assertIn(res["episodeId"], snap)
        self.assertEqual(snap[res["episodeId"]]["status"], "READY_FOR_CC")

    # ── Self-orienting session inbox ──────────────────────────────────────────
    def _handback(self, commit="abc1234"):
        items = council_bus.parse_work_items(council_bus.read_bus_file(self.root, "tasks"))
        h = items[0]["header"]
        h["status"], h["latestCommit"] = "READY_FOR_CODEX_VERIFICATION", commit
        council_bus.write_bus_file(self.root, "tasks", council_bus.render_front_matter(h, items[0]["body"]))

    def test_inbox_empty_bus_is_calm(self):
        self.assertIn("No active council handoff", ec.render_session_inbox(self.root, "codex"))
        self.assertIn("Claude Code Inbox", ec.render_session_inbox(self.root, "claude"))
        self.assertIn("No active council handoff", ec.render_session_inbox(self.root, "claude"))

    def test_inbox_preserves_ids_and_invents_none(self):
        res = self._start(box_ids=["box_a"], task_titles=["t1", "t2"])
        text = ec.render_session_inbox(self.root, "claude")
        self.assertIn(res["episodeId"], text)
        for t in res["taskIds"]:
            self.assertIn(t, text)
        self.assertIn("box_a", text)
        self.assertNotIn("councilId", text)
        self.assertNotIn("workItemId", text)

    def test_codex_inbox_says_verify_on_ready_for_verification(self):
        self._start()
        self._handback(commit="abc1234")
        text = ec.render_session_inbox(self.root, "codex")
        self.assertIn("Codex Inbox", text)
        self.assertIn("Verify the latest Claude Code commit", text)
        self.assertIn("abc1234", text)                        # latestCommit shown

    def test_claude_inbox_says_implement_with_trailers_on_ready_for_cc(self):
        res = self._start(box_ids=["box_a"])
        text = ec.render_session_inbox(self.root, "claude")
        self.assertIn("Claude Code Inbox", text)
        self.assertIn("Implement this task", text)
        self.assertIn("OpenFDE-Episode: " + res["episodeId"], text)   # exact trailers to stamp
        self.assertIn("OpenFDE-Role: senior_dev", text)

    def test_claude_inbox_includes_codex_change_request(self):
        res = self._start()
        ec.record_codex_verdict(self.root, episode_id=res["episodeId"], commit_sha="abc",
                                status="CHANGES_REQUESTED", findings="add a test for the 500 path")
        text = ec.render_session_inbox(self.root, "claude")
        self.assertIn("Latest from Codex", text)
        self.assertIn("add a test for the 500 path", text)
        self.assertIn("Fix Codex findings", text)
        self.assertIn("OpenFDE-Role: senior_dev", text)       # CHANGES_REQUESTED → CC re-commits

    def test_cli_council_status_returns_inbox(self):
        import subprocess
        import sys
        self._start()
        r = subprocess.run([sys.executable, "-m", "openfde", "council", "status",
                            "--role", "codex", "--path", str(self.root)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Codex Inbox", r.stdout)


def _view(status, *, episode="ep1", commit="", tasks=("task_a",), boxes=("box_x",), run=""):
    return {"episodeId": episode, "status": status, "taskIds": list(tasks), "runId": run,
            "boxIds": list(boxes), "latestCommit": commit, "objective": "do the thing",
            "acceptance": ["tests pass"]}


class CouncilEventDetectionTest(unittest.TestCase):
    """Pure transition → live-event mapping. Each council status emits the right event type +
    direction; only MATERIAL changes emit; CC-bound handoffs carry the commit trailers."""

    def test_ready_for_cc_is_codex_to_claude_handoff_with_trailers(self):
        ev = ec.detect_council_bus_event(None, _view("READY_FOR_CC", tasks=("task_a", "task_b")))
        self.assertEqual(ev["type"], "external_council_handoff")
        self.assertEqual(ev["direction"], "codex_to_claude")
        self.assertEqual((ev["from"], ev["to"]), ("Codex", "Claude Code"))
        self.assertEqual(ev["episodeId"], "ep1")
        self.assertEqual(ev["trailers"]["OpenFDE-Episode"], "ep1")     # CC gets the exact trailers
        self.assertEqual(ev["trailers"]["OpenFDE-Tasks"], "task_a, task_b")
        self.assertEqual(ev["trailers"]["OpenFDE-Role"], "senior_dev")

    def test_ready_for_verification_is_claude_to_codex(self):
        ev = ec.detect_council_bus_event(_view("CLAUDE_WORKING"),
                                         _view("READY_FOR_CODEX_VERIFICATION", commit="abc1234"))
        self.assertEqual(ev["type"], "external_council_handoff")
        self.assertEqual(ev["direction"], "claude_to_codex")
        self.assertEqual((ev["from"], ev["to"]), ("Claude Code", "Codex"))
        self.assertEqual(ev["latestCommit"], "abc1234")
        self.assertNotIn("trailers", ev)                       # receiver is Codex, not CC

    def test_changes_requested_is_codex_to_claude_verdict(self):
        ev = ec.detect_council_bus_event(_view("READY_FOR_CODEX_VERIFICATION", commit="abc"),
                                         _view("CHANGES_REQUESTED", commit="abc"))
        self.assertEqual(ev["type"], "external_council_verdict")
        self.assertEqual(ev["direction"], "codex_to_claude")
        self.assertIn("trailers", ev)                          # CC must re-commit → gets trailers

    def test_verified_is_codex_verdict_no_trailers(self):
        ev = ec.detect_council_bus_event(_view("READY_FOR_CODEX_VERIFICATION", commit="abc"),
                                         _view("VERIFIED", commit="abc"))
        self.assertEqual(ev["type"], "external_council_verdict")
        self.assertEqual(ev["direction"], "codex_verdict")
        self.assertNotIn("trailers", ev)                       # done — no CC action

    def test_claude_working_is_status_event(self):
        ev = ec.detect_council_bus_event(_view("READY_FOR_CC"), _view("CLAUDE_WORKING"))
        self.assertEqual(ev["type"], "external_council_status")
        self.assertEqual(ev["direction"], "claude_working")

    def test_blocked_needs_architect_emits(self):
        ev = ec.detect_council_bus_event(_view("CLAUDE_WORKING"), _view("BLOCKED_NEEDS_ARCHITECT"))
        self.assertEqual(ev["type"], "external_council_status")
        self.assertEqual(ev["direction"], "claude_to_codex")

    def test_no_event_when_status_and_commit_unchanged(self):
        v = _view("CLAUDE_WORKING", commit="abc")
        v2 = {**v, "objective": "reworded objective"}          # only prose changed
        self.assertIsNone(ec.detect_council_bus_event(v, v2))

    def test_new_commit_at_same_status_is_material(self):
        ev = ec.detect_council_bus_event(_view("READY_FOR_CODEX_VERIFICATION", commit="aaa"),
                                         _view("READY_FOR_CODEX_VERIFICATION", commit="bbb"))
        self.assertIsNotNone(ev)                               # a fresh CC commit → re-handoff
        self.assertEqual(ev["latestCommit"], "bbb")

    def test_debounce_does_not_duplicate(self):
        a, b = _view("READY_FOR_CC"), _view("CLAUDE_WORKING")
        self.assertIsNotNone(ec.detect_council_bus_event(a, b))   # the transition emits once…
        self.assertIsNone(ec.detect_council_bus_event(b, b))      # …the next tick (snap==cur) is silent

    def test_unknown_status_is_ignored(self):
        self.assertIsNone(ec.detect_council_bus_event(_view("READY_FOR_CC"), _view("WAT")))


if __name__ == "__main__":
    unittest.main()
