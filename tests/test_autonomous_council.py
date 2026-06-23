"""Tests for openfde.autonomous_council — the OpenFDE-managed autonomous relay.

Proven end-to-end with the deterministic ``echo`` adapter (no human copy-paste, no real CLI calls):
full happy path, the changes-requested fix loop, the retry-budget block, an honest
adapter-unavailable block (precise reason), parent-episode attachment, smoke runs that do NOT
pollute Story/OpenPM, the five OpenPM phase cards, and transcript role order."""

import shutil
import tempfile
import unittest
from pathlib import Path

from openfde import agent_sessions
from openfde import autonomous_council as ac
from openfde import external_council as ec
from openfde.persistence import Persistence


def _echo_factory(scripts=None):
    """A session factory whose named roles are scripted echo sessions; the rest are plain echo.
    Never builds a real CLI session, so tests never invoke codex/claude."""
    scripts = scripts or {}

    def factory(role, provider, *, run_dir=None):
        if role in scripts:
            return agent_sessions.EchoSession(role, run_dir=run_dir, responses=scripts[role])
        return agent_sessions.EchoSession(role, run_dir=run_dir)

    return factory


def _block_factory(role_to_block, reason):
    """Simulate one role's adapter being unavailable (e.g. a missing CLI) — deterministic, offline."""
    def factory(role, provider, *, run_dir=None):
        if role == role_to_block:
            return agent_sessions._UnavailableSession(role, provider, reason, run_dir=run_dir)
        return agent_sessions.EchoSession(role, run_dir=run_dir)
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

    # ── Happy path + provenance ───────────────────────────────────────────────
    def test_happy_path_full_relay_to_verified(self):
        rec = self._run(box_ids=["box_a"])
        self.assertEqual(rec["status"], ac.STATUS_READY_TO_PUSH)        # verified, autoPush off
        self.assertEqual(rec["phase"], ac.PHASE_READY_TO_PUSH)
        self.assertEqual(rec["loop"], 1)
        edges = [e["edge"] for e in rec["storyEvents"]]
        self.assertEqual(edges, ["proposed", "consulted", "decided", "implemented", "verified"])
        # ONE parent episode, landed, carrying the commit on episode truth
        eps = self.p.load_episodes()
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["episodeId"], rec["episodeId"])
        self.assertEqual(eps[0]["status"], "landed")
        self.assertIn(rec["runId"], eps[0]["runIds"])
        self.assertIn(rec["latestCommit"], eps[0]["commitShas"])
        # the council summary is mirrored onto the parent for the drawer
        self.assertEqual(eps[0]["council"]["status"], ac.STATUS_READY_TO_PUSH)
        self.assertEqual(eps[0]["council"]["latestCommit"], rec["latestCommit"])
        self.assertTrue(rec["episodeId"].startswith("episode_") and rec["runId"].startswith("run_"))

    def test_openpm_has_five_phase_cards_under_parent(self):
        rec = self._run(box_ids=["box_a"])
        tasks = self.p.load_tasks()
        self.assertEqual(len(tasks), 5)                                # exactly the phase cards, no seed
        byk = {t["phaseKey"]: t for t in tasks}
        self.assertEqual(set(byk), {"plan", "consult", "implement", "verify", "push"})
        for t in tasks:
            self.assertEqual(t["episodeId"], rec["episodeId"])
            self.assertEqual(t["source"], "external-council")
            self.assertEqual(t["column"], "done")                      # all advanced on a clean run
        self.assertEqual(byk["implement"]["commitSha"], rec["latestCommit"])   # commit on the impl card
        self.assertIsNone(byk["plan"]["commitSha"])                    # not on the others

    def test_parent_episode_attachment_creates_no_new_episode(self):
        parent = self._run(box_ids=["box_a"])
        n_eps = len(self.p.load_episodes())
        rec2 = self._run(parent_episode_id=parent["episodeId"])
        self.assertEqual(rec2["episodeId"], parent["episodeId"])       # reuses the originating episode
        self.assertEqual(len(self.p.load_episodes()), n_eps)           # NO new rail beat
        self.assertIn(rec2["runId"], self.p.get_episode(parent["episodeId"])["runIds"])
        self.assertEqual(len(self.p.load_tasks()), 5)                  # phase cards deduped, not doubled

    def test_smoke_run_does_not_pollute_story_or_openpm(self):
        rec = self._run(product=False)
        self.assertEqual(rec["status"], ac.STATUS_READY_TO_PUSH)       # the relay still runs end-to-end
        self.assertEqual(rec["episodeId"], "")
        self.assertEqual(self.p.load_episodes(), [])                   # no episode
        self.assertEqual(self.p.load_tasks(), [])                      # no OpenPM cards
        self.assertEqual(ec.load_recorded_transcript(self.root), [])   # no Orient-inbox turns
        self.assertTrue(rec["turns"])                                  # but debug turns live in run.json
        self.assertFalse(ac.load_run(self.root, rec["runId"])["product"])
        self.assertIsNone(ac.latest_run_summary(self.root))            # latest *product* run = none

    # ── Transcript ────────────────────────────────────────────────────────────
    def test_transcript_role_order(self):
        self._run()
        labels = [t["label"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(labels, ["user", "architect (Codex)", "sr dev (Claude Code)",
                                  "architect (Codex)", "sr dev (Claude Code)", "verifier (Codex)", "system"])
        kinds = [t["kind"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(kinds, ["prompt", "proposal", "consultation", "decision",
                                 "implementation", "verified", "ready_to_push"])

    def test_transcript_returned_by_external_council_builder(self):
        self._run()
        tx = ec.build_council_transcript(self.root)
        labels = [it["label"] for it in tx["items"]]
        for lbl in ("architect (Codex)", "sr dev (Claude Code)", "verifier (Codex)"):
            self.assertIn(lbl, labels)
        self.assertEqual(len(tx["items"]), len(ec.load_recorded_transcript(self.root)))
        self.assertFalse(tx["active"])                                 # VERIFIED → inactive

    # ── Loops + safety ────────────────────────────────────────────────────────
    def test_changes_requested_then_fixed_then_verified(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: add the missing test", "VERIFIED: fixed"]})
        rec = self._run(session_factory=f, max_loops=3)
        self.assertEqual(rec["status"], ac.STATUS_READY_TO_PUSH)
        self.assertEqual(rec["loop"], 2)
        edges = [e["edge"] for e in rec["storyEvents"]]
        self.assertEqual(edges, ["proposed", "consulted", "decided", "implemented",
                                 "changes_requested", "fixed", "verified"])

    def test_max_loops_exceeded_blocks_needs_human(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: still broken"]})
        rec = self._run(session_factory=f, max_loops=2)
        self.assertEqual(rec["status"], ac.STATUS_BLOCKED_NEEDS_HUMAN)
        self.assertEqual(rec["loop"], 2)
        self.assertIn("verification loops", rec["blockedReason"])
        view = ec.bus_snapshot(self.root)[rec["episodeId"]]
        self.assertEqual(view["status"], "BLOCKED_NEEDS_HUMAN")
        self.assertEqual(self.p.get_episode(rec["episodeId"])["status"], "blocked")

    def test_adapter_unavailable_blocks_with_precise_reason(self):
        reason = "codex CLI not found (looked on PATH and at /Applications/Codex.app/...)"
        rec = self._run(providers={"architect": "codex", "srDev": "echo", "verifier": "echo"},
                        session_factory=_block_factory("architect", reason))
        self.assertEqual(rec["status"], ac.STATUS_BLOCKED_ADAPTER_UNAVAILABLE)
        self.assertIn("codex", rec["blockedReason"])
        self.assertIn("not found", rec["blockedReason"])
        kinds = [t["kind"] for t in rec["turns"]]
        self.assertEqual(kinds, ["prompt", "blocked"])                 # no fabricated relay turns

    # ── Real-adapter availability (offline, monkeypatched) ────────────────────
    def test_real_codex_adapter_precise_unavailable_reason_when_cli_missing(self):
        orig = agent_sessions._codex_cli
        agent_sessions._codex_cli = lambda: None
        try:
            s = agent_sessions.CodexExecSession("architect", repo_root=self.root)
            with self.assertRaises(agent_sessions.AdapterUnavailable) as cm:
                s.start()
            self.assertIn("codex CLI not found", cm.exception.reason)
        finally:
            agent_sessions._codex_cli = orig

    def test_real_claude_adapter_precise_unavailable_reason_when_cli_missing(self):
        orig = shutil.which
        shutil.which = lambda name: None
        try:
            s = agent_sessions.ClaudeCodeSession("sr_dev", repo_root=self.root)
            with self.assertRaises(agent_sessions.AdapterUnavailable) as cm:
                s.start()
            self.assertIn("claude CLI not found", cm.exception.reason)
        finally:
            shutil.which = orig

    def test_init_run_returns_ids_immediately(self):
        rec = ac.init_run(self.p, prompt="add a healthz endpoint", box_ids=["box_x"])
        self.assertTrue(rec["episodeId"].startswith("episode_"))
        self.assertEqual(len(rec["taskIds"]), 5)
        self.assertEqual(rec["status"], ac.STATUS_RUNNING)
        self.assertEqual(ac.load_run(self.root, rec["runId"])["runId"], rec["runId"])

    def test_auto_push_hands_off_to_cc_to_push(self):
        rec = self._run(auto_push=True)
        self.assertEqual(rec["status"], ac.STATUS_VERIFIED)
        self.assertEqual([t["kind"] for t in ec.load_recorded_transcript(self.root)][-1], "push")


if __name__ == "__main__":
    unittest.main()
