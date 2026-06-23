"""Tests for openfde.autonomous_council — the OpenFDE-managed autonomous relay.

Proven end-to-end with the deterministic ``echo`` adapter (no human copy-paste, no real agent):
full happy path, the changes-requested fix loop, the retry-budget block, an honest
adapter-unavailable block, and the episode / OpenPM / transcript / Story side effects."""

import tempfile
import unittest
from pathlib import Path

from openfde import agent_sessions
from openfde import autonomous_council as ac
from openfde import external_council as ec
from openfde.persistence import Persistence


def _echo_factory(scripts=None):
    """A session factory whose named roles are scripted echo sessions; the rest go through the real
    build_session (so an unsupported provider honestly reports adapter_unavailable)."""
    scripts = scripts or {}

    def factory(role, provider, *, run_dir=None):
        if role in scripts:
            return agent_sessions.EchoSession(role, run_dir=run_dir, responses=scripts[role])
        return agent_sessions.build_session(role, provider, run_dir=run_dir)

    return factory


class AutonomousCouncilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **kw):
        kw.setdefault("prompt", "build an agentic SaaS for insurance")
        kw.setdefault("providers", {"architect": "echo", "srDev": "echo", "verifier": "echo"})
        kw.setdefault("session_factory", _echo_factory())
        return ac.run(self.p, **kw)

    def test_happy_path_full_relay_to_verified(self):
        rec = self._run(box_ids=["box_a"])
        self.assertEqual(rec["status"], ac.STATUS_READY_TO_PUSH)        # verified, autoPush off
        self.assertEqual(rec["phase"], ac.PHASE_READY_TO_PUSH)
        self.assertEqual(rec["loop"], 1)
        kinds = [t["kind"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(kinds, ["prompt", "proposal", "consultation", "decision",
                                 "implementation", "verified", "ready_to_push"])
        edges = [e["edge"] for e in rec["storyEvents"]]
        self.assertEqual(edges, ["proposed", "consulted", "decided", "implemented", "verified"])
        eps = self.p.load_episodes()
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["episodeId"], rec["episodeId"])
        self.assertEqual(eps[0]["status"], "landed")
        self.assertIn(rec["runId"], eps[0]["runIds"])
        self.assertIn(rec["latestCommit"], eps[0]["commitShas"])    # episode truth carries the commit
        tasks = self.p.load_tasks()
        self.assertTrue(tasks)
        for t in tasks:
            self.assertEqual(t["column"], "done")
            self.assertEqual(t["verificationStatus"], "passed")
            self.assertEqual(t["commitSha"], rec["latestCommit"])      # commit attached
        self.assertTrue(rec["episodeId"].startswith("episode_"))       # no parallel council id
        self.assertTrue(rec["runId"].startswith("run_"))
        view = ec.bus_snapshot(self.root)[rec["episodeId"]]
        self.assertEqual(view["status"], "VERIFIED")
        self.assertEqual(view["runId"], rec["runId"])
        self.assertEqual(view["latestCommit"], rec["latestCommit"])

    def test_changes_requested_then_fixed_then_verified(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: add the missing test", "VERIFIED: fixed"]})
        rec = self._run(session_factory=f, max_loops=3)
        self.assertEqual(rec["status"], ac.STATUS_READY_TO_PUSH)
        self.assertEqual(rec["loop"], 2)
        kinds = [t["kind"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(kinds, ["prompt", "proposal", "consultation", "decision", "implementation",
                                 "changes_requested", "implementation", "verified", "ready_to_push"])
        edges = [e["edge"] for e in rec["storyEvents"]]
        self.assertEqual(edges, ["proposed", "consulted", "decided", "implemented",
                                 "changes_requested", "fixed", "verified"])

    def test_max_loops_exceeded_blocks_needs_human(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: still broken"]})   # always fails
        rec = self._run(session_factory=f, max_loops=2)
        self.assertEqual(rec["status"], ac.STATUS_BLOCKED_NEEDS_HUMAN)
        self.assertEqual(rec["phase"], ac.PHASE_BLOCKED)
        self.assertEqual(rec["loop"], 2)
        self.assertIn("verification loops", rec["blockedReason"])
        view = ec.bus_snapshot(self.root)[rec["episodeId"]]
        self.assertEqual(view["status"], "BLOCKED_NEEDS_HUMAN")
        edges = [e["edge"] for e in rec["storyEvents"]]
        self.assertEqual(edges.count("changes_requested"), 2)
        self.assertEqual(edges[-1], "blocked")

    def test_adapter_unavailable_blocks_honestly(self):
        # codex provider with no script → the real adapter, which honestly reports unavailable.
        rec = self._run(providers={"architect": "codex", "srDev": "echo", "verifier": "echo"})
        self.assertEqual(rec["status"], ac.STATUS_BLOCKED_ADAPTER_UNAVAILABLE)
        self.assertIn("adapter unavailable", rec["blockedReason"])
        self.assertIn("codex", rec["blockedReason"])
        kinds = [t["kind"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(kinds, ["prompt", "blocked"])                 # no fabricated relay turns

    def test_transcript_returned_by_external_council_builder(self):
        self._run()
        tx = ec.build_council_transcript(self.root)
        labels = [it["label"] for it in tx["items"]]
        self.assertIn("architect (Codex)", labels)
        self.assertIn("sr dev (Claude Code)", labels)
        self.assertIn("verifier (Codex)", labels)
        self.assertEqual(len(tx["items"]), len(ec.load_recorded_transcript(self.root)))
        self.assertFalse(tx["active"])                                 # VERIFIED → inactive

    def test_init_run_creates_episode_tasks_and_run_record(self):
        rec = ac.init_run(self.p, prompt="add a healthz endpoint", box_ids=["box_x"])
        self.assertTrue(rec["episodeId"].startswith("episode_"))
        self.assertTrue(rec["taskIds"])
        self.assertEqual(rec["status"], ac.STATUS_RUNNING)
        self.assertEqual(rec["boxIds"], ["box_x"])
        loaded = ac.load_run(self.root, rec["runId"])
        self.assertEqual(loaded["runId"], rec["runId"])
        self.assertEqual(ac.latest_run_summary(self.root)["runId"], rec["runId"])

    def test_auto_push_hands_off_to_cc_to_push(self):
        rec = self._run(auto_push=True)
        self.assertEqual(rec["status"], ac.STATUS_VERIFIED)
        kinds = [t["kind"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(kinds[-1], "push")

    def test_commit_sha_attaches_to_implementation_task(self):
        rec = self._run()
        impl = next(t for t in ec.load_recorded_transcript(self.root) if t["kind"] == "implementation")
        self.assertTrue(impl["latestCommit"])
        self.assertEqual(impl["latestCommit"], rec["latestCommit"])
        self.assertIn("echo-suite", impl["checks"])


if __name__ == "__main__":
    unittest.main()
