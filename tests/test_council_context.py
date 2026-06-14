"""
Tests for openfde.council_context — the generated, capped, PURE CouncilContext and
its agent-state derivation. Laws: read-only chat is never "busy"; a file-editing run
makes senior_dev.edit busy; everything is capped; the brief renders deterministically.
"""
import unittest

from openfde import council_context as C


class DeriveAgentStatesTest(unittest.TestCase):
    def test_idle_when_no_runs(self):
        st = C.derive_agent_states(
            available={"architect": True, "senior_dev": True, "verifier": True},
            runs=[], active_run_ids=[])
        for r in ("architect", "senior_dev", "verifier"):
            self.assertFalse(st[r]["workBusy"])
            self.assertFalse(st[r]["chatBusy"])             # chat is never busy
        self.assertEqual(st["runningWorkJobs"], [])

    def test_edit_run_attributes_work_to_senior_dev(self):
        runs = [{"runId": "r1", "kind": "council_run", "status": "running",
                 "endedAt": None, "startedAt": "t0"}]
        st = C.derive_agent_states(runs=runs, active_run_ids=[])
        self.assertTrue(st["senior_dev"]["workBusy"])       # editing → Senior Dev
        self.assertFalse(st["senior_dev"]["chatBusy"])      # work busy, chat free
        self.assertFalse(st["architect"]["workBusy"])
        self.assertFalse(st["verifier"]["workBusy"])
        self.assertEqual(st["runningWorkJobs"][0]["runId"], "r1")
        self.assertEqual(st["runningWorkJobs"][0]["role"], "senior_dev")

    def test_verify_run_attributes_work_to_verifier(self):
        runs = [{"runId": "v1", "kind": "verify_run", "status": "running", "endedAt": None}]
        st = C.derive_agent_states(runs=runs, active_run_ids=[])
        self.assertTrue(st["verifier"]["workBusy"])
        self.assertFalse(st["senior_dev"]["workBusy"])

    def test_plan_run_attributes_work_to_architect(self):
        runs = [{"runId": "p1", "kind": "plan_run", "status": "running", "endedAt": None}]
        st = C.derive_agent_states(runs=runs, active_run_ids=[])
        self.assertTrue(st["architect"]["workBusy"])
        self.assertFalse(st["senior_dev"]["workBusy"])

    def test_work_busy_from_in_flight_run_id_not_yet_persisted(self):
        st = C.derive_agent_states(runs=[], active_run_ids=["live1"])
        self.assertTrue(st["senior_dev"]["workBusy"])
        self.assertEqual(st["runningWorkJobs"][0]["runId"], "live1")

    def test_finished_run_is_not_busy(self):
        runs = [{"runId": "r1", "kind": "council_run", "status": "passed", "endedAt": "t1"}]
        st = C.derive_agent_states(runs=runs, active_run_ids=[])
        self.assertFalse(st["senior_dev"]["workBusy"])

    def test_unavailable_provider_reflected(self):
        st = C.derive_agent_states(available={"verifier": False}, runs=[], active_run_ids=[])
        self.assertFalse(st["verifier"]["available"])
        self.assertTrue(st["architect"]["available"])       # missing → available

    def test_jobs_are_capped(self):
        runs = [{"runId": f"r{i}", "kind": "agent_run", "status": "running",
                 "endedAt": None} for i in range(12)]
        st = C.derive_agent_states(runs=runs, active_run_ids=[])
        self.assertLessEqual(len(st["runningWorkJobs"]), 5)


class BuildContextTest(unittest.TestCase):
    def test_shape_and_caps(self):
        episodes = [{"episodeId": f"e{i}", "tag": f"P{i}", "title": f"Title {i}",
                     "status": "landed", "summary": f"did thing {i}"} for i in range(10)]
        active = {"episodeId": "e0", "tag": "P0", "title": "Active", "status": "reviewing",
                  "verification": {"status": "passed"},
                  "intentSource": {"kind": "issue", "ref": "#12"}}
        repo = {"git": True, "branch": "main", "shortHead": "abc1234",
                "dirty": [f"f{i}.py" for i in range(40)], "staged": []}
        verify = {"status": "passed", "ranAt": "2026-06-14T00:00:00Z", "checks": []}
        project = {"name": "demo", "description": "Ship the council router.", "entries": []}
        plog = [{"summary": f"decision {i}"} for i in range(10)]
        agents = C.derive_agent_states(runs=[], active_run_ids=[])
        ctx = C.build_council_context(active_episode=active, recent_episodes=episodes,
                                      repo_status=repo, verify_latest=verify,
                                      project=project, project_log=plog, agent_states=agents)
        self.assertEqual(ctx["activeEpisode"]["tag"], "P0")
        self.assertEqual(ctx["activeEpisode"]["verify"], "passed")
        self.assertEqual(ctx["activeEpisode"]["intent"], "issue")
        self.assertLessEqual(len(ctx["recentEpisodes"]), 5)
        self.assertEqual(ctx["repo"]["dirtyCount"], 40)            # true count kept
        self.assertLessEqual(len(ctx["repo"]["dirtyFiles"]), 20)   # list capped
        self.assertEqual(ctx["verify"]["status"], "passed")
        self.assertLessEqual(len(ctx["recentDecisions"]), 5)
        self.assertIn("Ship the council router.", ctx["recentDecisions"])
        self.assertIn("agents", ctx)

    def test_handles_empty_inputs(self):
        ctx = C.build_council_context()
        self.assertIsNone(ctx["activeEpisode"])
        self.assertEqual(ctx["recentEpisodes"], [])
        self.assertEqual(ctx["repo"]["dirtyCount"], 0)
        self.assertIsNone(ctx["verify"])
        self.assertEqual(ctx["recentDecisions"], [])


class RenderBriefTest(unittest.TestCase):
    def test_brief_mentions_active_repo_and_busy(self):
        agents = C.derive_agent_states(
            runs=[{"runId": "r1", "kind": "council_run", "status": "running", "endedAt": None}],
            active_run_ids=[])
        ctx = C.build_council_context(
            active_episode={"tag": "P5", "title": "Router", "status": "reviewing"},
            repo_status={"branch": "main", "dirty": ["a.py"]}, agent_states=agents)
        brief = C.render_brief(ctx)
        self.assertIn("P5", brief)
        self.assertIn("branch main", brief)
        self.assertIn("mid-WORK", brief)                   # busy is reported, not hidden
        self.assertIn("Senior Dev", brief)                 # attributed to the work role
        self.assertLessEqual(len(brief), 3200)

    def test_brief_idle_and_empty_are_safe(self):
        idle = C.build_council_context(agent_states=C.derive_agent_states())
        self.assertIn("all idle", C.render_brief(idle))
        self.assertIsInstance(C.render_brief({}), str)
        self.assertIsInstance(C.render_brief(None), str)


class GroundingTest(unittest.TestCase):
    """The CURRENT DIRECTION grounding — keeps the council on the cockpit/engines model and off the
    deprecated Anthropic-SDK /api/execute path."""

    def test_anchor_is_cockpit_and_deprecates_sdk(self):
        d = C.assemble_direction()
        self.assertTrue(d and "cockpit" in d[0].lower())
        self.assertIn("EXTERNAL ENGINES", d[0])
        self.assertIn("Anthropic-SDK", d[0])
        self.assertIn("deprecated", d[0].lower())

    def test_extracts_now_next_skips_deferred_and_placeholders(self):
        decisions = ("# DECISIONS\n## Now\n- ship the cockpit\n## Next\n- wire engines\n"
                     "## Deferred\n- the old SDK execute path\n")
        flow = "## How work flows\n- <one or two lines: placeholder>\n- Intent then verify then land\n"
        joined = " ".join(C.assemble_direction(decisions_md=decisions, flow_md=flow))
        self.assertIn("ship the cockpit", joined)
        self.assertIn("wire engines", joined)
        self.assertNotIn("old SDK execute path", joined)        # Deferred section excluded
        self.assertIn("Intent then verify then land", joined)
        self.assertNotIn("placeholder", joined)                 # <…> template line skipped

    def test_latest_roadmap_headings_from_tail(self):
        roadmap = "## Ancient shipped step\nbody\n## Cockpit reconciliation — NEXT\nplan it\n"
        self.assertIn("Cockpit reconciliation", " ".join(C.assemble_direction(roadmap_md=roadmap)))

    def test_render_brief_puts_direction_first(self):
        ctx = C.build_council_context(decisions_md="## Now\n- be the cockpit\n",
                                      recent_commits=["openfde: land thing"],
                                      agent_states=C.derive_agent_states())
        brief = C.render_brief(ctx)
        self.assertIn("CURRENT DIRECTION", brief)
        self.assertLess(brief.index("CURRENT DIRECTION"), brief.index("Active episode"))
        self.assertIn("Anthropic-SDK", brief)
        self.assertIn("land thing", brief)                      # recently-landed surfaced


if __name__ == "__main__":
    unittest.main()
