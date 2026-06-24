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
            self.assertTrue(s["title"] and s["prompt"] and s["acceptance"] and s["blastRadius"])
            self.assertEqual(s["status"], pg.SLICE_QUEUED)
            self.assertEqual(s["commits"], [])
            self.assertIsNone(s["failureReason"])

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
            self.assertTrue(sl["commits"])                          # commit receipt(s) from episode truth
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

    def test_labels_come_from_provider_config_not_hardcoded(self):
        from openfde import external_council as ec
        # Configure codex/claude-code providers but RUN via echo sessions (factory override) → the
        # transcript labels reflect the CONFIG, proving they are not hardcoded to the adapter.
        self._run("Add a /healthz endpoint",
                  providers={"architect": "codex", "srDev": "claude-code", "verifier": "codex"},
                  session_factory=_echo_factory())
        labels = {t["label"] for t in ec.load_recorded_transcript(self.root)}
        self.assertIn("architect (Codex)", labels)
        self.assertIn("sr dev (Claude Code)", labels)
        self.assertIn("verifier (Codex)", labels)
        # swap the role/provider config → labels follow it (architect now Claude Code)
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = Persistence(self.root / ".openfde")
        self._run("Add a /healthz endpoint",
                  providers={"architect": "claude-code", "srDev": "codex", "verifier": "echo"},
                  session_factory=_echo_factory())
        labels2 = {t["label"] for t in ec.load_recorded_transcript(self.root)}
        self.assertIn("architect (Claude Code)", labels2)
        self.assertIn("sr dev (Codex)", labels2)
        self.assertIn("verifier (echo)", labels2)
        self.assertNotIn("architect (Codex)", labels2)              # not hardcoded

    def test_status_bridge_is_role_specific(self):
        prog = self._run("1. Add a /healthz endpoint. 2. Add request logging.")
        for role in ("architect", "senior-dev", "verifier"):
            out = pg.program_status(prog, role)
            self.assertIn(prog["programId"], out)
            self.assertIn(f"your role: {role}", out)
            self.assertIn("session bridge", out)                   # honest: not chat injection
        self.assertEqual(pg.program_status(None, "architect"), "No active program.")

    def test_providers_from_settings_maps_to_adapters_and_blocks(self):
        self.p.save_agent_settings({"architect": {"provider": "codex-local", "enabled": True},
                                    "senior_dev": {"provider": "claude-code-local", "enabled": True},
                                    "verifier": {"provider": "echo", "enabled": True}})
        self.assertEqual(pg.providers_from_settings(self.p),
                         {"architect": "codex", "srDev": "claude-code", "verifier": "echo"})
        # a non-coding provider (anthropic) → '' for that role → start blocks honestly
        self.p.save_agent_settings({"architect": {"provider": "anthropic", "enabled": True},
                                    "senior_dev": {"provider": "claude-code-local", "enabled": True},
                                    "verifier": {"provider": "codex-local", "enabled": True}})
        prov = pg.providers_from_settings(self.p)
        self.assertEqual(prov["architect"], "")
        prog = pg.start_program(self.p, prompt="Add a /healthz endpoint", providers=prov)
        self.assertEqual(prog["blockerReason"], pg.BLOCKED_NO_PROVIDER_FOR_ROLE)

    def test_reconcile_lands_verified_slice_and_keeps_it_product(self):
        prog = self._run("1. Add a /healthz endpoint.")
        sl = prog["slices"][0]
        ep = self.p.get_episode(sl["episodeId"])
        ep["status"], ep["internal"], ep["signal"] = "open", True, "operational"   # simulate the drift
        self.p.upsert_episode(ep)
        self.assertGreaterEqual(pg.reconcile_program_slices(self.p), 1)
        healed = self.p.get_episode(sl["episodeId"])
        self.assertEqual(healed["status"], "landed")          # verified + commit → landed
        self.assertFalse(healed["internal"])
        self.assertEqual(healed["signal"], "product")         # never demoted — it's product journey

    def test_program_slice_never_classified_internal(self):
        from openfde.episode_summary import internal_council_kind
        self.assertIsNone(internal_council_kind({"title": "Implementation Prompt", "programId": "program_x"}))
        self.assertEqual(internal_council_kind({"title": "Implementation Prompt"}), "implementation_prompt")

    def test_status_shows_final_report_and_all_commits_when_complete(self):
        prog = self._run("1. Add a /healthz endpoint. 2. Add request logging.")
        out = pg.program_status(prog, "architect")
        self.assertIn("final report:", out)
        self.assertIn("commits:", out)
        for s in prog["slices"]:
            for c in s["commits"]:
                self.assertIn(c[:7], out)                     # every slice commit appears

    def test_rail_payload_carries_program_grouping_and_lands_slices(self):
        from openfde.server import build_rail_payload
        prog = self._run("1. Add a /healthz endpoint. 2. Add request logging.")
        # simulate the stale-open drift on one slice; the rail's reconcile must heal it
        ep = self.p.get_episode(prog["slices"][0]["episodeId"])
        ep["status"] = "open"
        self.p.upsert_episode(ep)
        chips = build_rail_payload(self.p)["episodes"]
        pgm = [c for c in chips if c.get("programId") == prog["programId"]]
        self.assertEqual(len(pgm), len(prog["slices"]))                 # all slices visible, none folded
        self.assertTrue(all(c.get("programTitle") and c.get("sliceTitle") for c in pgm))
        self.assertTrue(all(c["status"] == "landed" for c in pgm))      # reconciled — no stale open

    def test_program_episode_not_flagged_nonimplementation(self):
        # a Program slice that looks meta-by-effect (only a gitignored file, no commit) is NOT demoted
        self.p.upsert_episode({"episodeId": "episode_x", "programId": "program_y", "files": ["notes.md"],
                               "commitShas": [], "signal": "product", "sequence": 1,
                               "createdAt": "2026-01-01T00:00:00Z"})
        self.p.flag_nonimplementation_episodes(str(self.root), self.p.load_episodes())
        self.assertEqual(self.p.get_episode("episode_x")["signal"], "product")
        self.assertFalse(self.p.get_episode("episode_x").get("nonImplementation"))

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
