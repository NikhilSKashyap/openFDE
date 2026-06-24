"""Tests for openfde.program — Autonomous Program Mode v1 (echo adapters only).

A high-level direction → ≤3 scoped slices → each runs the autonomous council loop → auto-advance with
episode/task/commit receipts; honest blocks (clarity, blast radius, no provider, retry budget)."""

import tempfile
import unittest
from pathlib import Path

from openfde import agent_sessions
from openfde import program as pg
from openfde.persistence import Persistence


def _echo_factory(scripts=None):
    scripts = scripts or {}

    def factory(role, provider, *, run_dir=None):
        if role in scripts:
            return agent_sessions.EchoSession(role, run_dir=run_dir, responses=scripts[role])
        return agent_sessions.EchoSession(role, run_dir=run_dir)
    return factory


ECHO = {"architect": "echo", "srDev": "echo", "verifier": "echo"}


class ProgramPlannerTest(unittest.TestCase):
    def test_plans_up_to_three_scoped_slices(self):
        slices, block = pg.plan_program("1. Add a /healthz endpoint. 2. Add request logging. "
                                        "3. Add a metrics route. 4. Add a readiness probe.")
        self.assertIsNone(block)
        self.assertLessEqual(len(slices), pg.MAX_SLICES)            # max 3 — overflow folds into the last
        for s in slices:
            self.assertTrue(s["title"] and s["prompt"] and s["acceptance"] and s["risk"])
            self.assertEqual(s["status"], pg.SLICE_QUEUED)

    def test_vague_prompt_blocks_for_clarity(self):
        for vague in ("make it better", "improve everything", "do stuff", "fix"):
            slices, block = pg.plan_program(vague)
            self.assertIsNone(slices)
            self.assertEqual(block, pg.BLOCKED_NEEDS_PRODUCT_CLARITY, vague)

    def test_blast_radius_blocks(self):
        slices, block = pg.plan_program("Rewrite the entire codebase and migrate the whole database")
        self.assertIsNone(slices)
        self.assertEqual(block, pg.BLOCKED_BLAST_RADIUS)


class ProgramRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, prompt, **kw):
        kw.setdefault("providers", ECHO)
        kw.setdefault("session_factory", _echo_factory())
        return pg.run(self.p, prompt=prompt, **kw)

    def test_full_program_runs_slices_and_completes(self):
        prog = self._run("1. Add a /healthz endpoint. 2. Add structured request logging.")
        self.assertEqual(prog["status"], pg.STATUS_COMPLETE)
        self.assertGreaterEqual(len(prog["slices"]), 2)
        for sl in prog["slices"]:
            self.assertEqual(sl["status"], pg.SLICE_VERIFIED)       # every slice verified
            self.assertTrue(sl["episodeId"].startswith("episode_")) # receipts linked per slice
            self.assertTrue(sl["taskIds"])
        # exactly one episode per slice, each stamped with program + slice ids
        eps = self.p.load_episodes()
        self.assertEqual(len(eps), len(prog["slices"]))
        for ep in eps:
            self.assertEqual(ep["programId"], prog["programId"])
            self.assertTrue(ep["sliceId"])
        # OpenPM cards carry the program/slice grouping
        tasks = [t for t in self.p.load_tasks() if t.get("phaseKey")]
        self.assertTrue(tasks)
        self.assertTrue(all(t["programId"] == prog["programId"] for t in tasks))
        self.assertEqual(len({t["sliceId"] for t in tasks}), len(prog["slices"]))

    def test_second_slice_starts_automatically(self):
        events = []
        prog = self._run("1. First task here. 2. Second task here.",
                         on_event=lambda s: events.append((s["currentSliceId"], s["status"])))
        slice_ids = [s["sliceId"] for s in prog["slices"]]
        seen = [sid for sid, _ in events if sid]
        self.assertIn(slice_ids[0], seen)
        self.assertIn(slice_ids[1], seen)                          # slice 2 became current on its own

    def test_verifier_failure_blocks_program_with_retry_reason(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: nope"]})   # never verifies
        prog = self._run("1. Add a /healthz endpoint. 2. Add request logging.",
                         session_factory=f, max_loops=1)
        self.assertEqual(prog["status"], pg.STATUS_BLOCKED)
        self.assertEqual(prog["blockerReason"], pg.BLOCKED_MAX_RETRIES)
        self.assertEqual(prog["slices"][0]["status"], pg.SLICE_BLOCKED)
        self.assertEqual(prog["slices"][1]["status"], pg.SLICE_QUEUED)  # never started — program stopped

    def test_missing_provider_blocks(self):
        prog = pg.start_program(self.p, prompt="Add a /healthz endpoint",
                                providers={"architect": "echo", "srDev": "echo", "verifier": ""})
        self.assertEqual(prog["status"], pg.STATUS_BLOCKED)
        self.assertEqual(prog["blockerReason"], pg.BLOCKED_NO_PROVIDER_FOR_ROLE)

    def test_one_active_program_at_a_time(self):
        p1 = pg.start_program(self.p, prompt="Add a /healthz endpoint", providers=ECHO)
        p2 = pg.start_program(self.p, prompt="Add request logging", providers=ECHO)
        self.assertEqual(p1["programId"], p2["programId"])          # the active program is returned

    def test_status_bridge_is_role_specific(self):
        prog = self._run("1. Add a /healthz endpoint. 2. Add request logging.")
        for role in ("architect", "senior-dev", "verifier"):
            out = pg.program_status(prog, role)
            self.assertIn(prog["programId"], out)
            self.assertIn(f"your role: {role}", out)
            self.assertIn("session bridge", out)                   # honest: not chat injection
        self.assertEqual(pg.program_status(None, "architect"), "No active program.")

    def test_continue_resumes_a_blocked_program(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: nope"]})
        prog = self._run("1. Add a /healthz endpoint.", session_factory=f, max_loops=1)
        self.assertEqual(prog["status"], pg.STATUS_BLOCKED)
        resumed = pg.continue_program(self.p, prog["programId"], session_factory=_echo_factory())
        self.assertEqual(resumed["status"], pg.STATUS_COMPLETE)    # passes on retry with a good verifier


class SessionDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovers_repo_matching_claude_sessions_sorted_latest_first(self):
        import os
        from openfde import prompt_capture as pc
        home = self.root / "home"
        d = pc.claude_projects_dir(str(self.root), str(home))   # encoded-cwd dir for THIS repo
        d.mkdir(parents=True)
        (d / "sess_old.jsonl").write_text("{}\n")
        (d / "sess_new.jsonl").write_text("{}\n")
        os.utime(d / "sess_old.jsonl", (1000, 1000))
        os.utime(d / "sess_new.jsonl", (2000, 2000))            # newer
        sessions = __import__("openfde.agent_sessions", fromlist=["x"]).discover_repo_sessions(
            self.root, home=str(home))
        claude = [s for s in sessions if s["provider"] == "claude-code"]
        self.assertEqual([s["sessionId"] for s in claude], ["sess_new", "sess_old"])  # newest first
        self.assertTrue(claude[0]["selected"] and not claude[1]["selected"])          # default = latest
        self.assertEqual(claude[0]["repoRoot"], str(self.root))
        # cached to .openfde/agent_sessions.json
        self.assertTrue((self.root / ".openfde" / "agent_sessions.json").exists())

    def test_no_sessions_returns_empty_not_error(self):
        from openfde import agent_sessions
        self.assertEqual(agent_sessions.discover_repo_sessions(self.root, home=str(self.root / "empty")), [])


if __name__ == "__main__":
    unittest.main()
