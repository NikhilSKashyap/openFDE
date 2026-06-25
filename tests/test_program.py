"""Tests for openfde.program — Autonomous Program Mode v1 (echo adapters only).

A high-level direction → ≤3 scoped slices → each runs the autonomous council loop → auto-advance with
episode/task/commit receipts; honest blocks (clarity, blast radius, no provider, retry budget)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde import agent_sessions, run_control
from openfde import autonomous_council as ac
from openfde import program as pg
from openfde.persistence import Persistence


def _git_init_commit(root, filename, content):
    """Init a git repo at ``root`` + commit one file; return the real commit sha."""
    g = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
    g("init")
    g("config", "user.email", "t@t.co")
    g("config", "user.name", "t")
    (root / filename).write_text(content)
    g("add", filename)
    g("commit", "-m", f"add {filename}")
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _echo_factory(scripts=None):
    scripts = scripts or {}

    def factory(role, provider, *, run_dir=None):
        if role in scripts:
            return agent_sessions.EchoSession(role, run_dir=run_dir, responses=scripts[role])
        return agent_sessions.EchoSession(role, run_dir=run_dir)
    return factory


class _TimeoutSession(agent_sessions.AgentSession):
    """A managed session that raises ProviderTimeout for one role — simulates a hung provider without
    spawning a real subprocess (the run_control kill path is covered in test_run_control)."""

    def __init__(self, role, *, run_dir=None, fail_role="architect", provider="claude-code"):
        super().__init__(role, provider, run_dir=run_dir)
        self.fail_role = fail_role

    def start(self):
        self._started = True

    def send(self, message, metadata=None):
        if self.role == self.fail_role:
            raise run_control.ProviderTimeout(self.provider, self.role, (metadata or {}).get("phase", "plan"), 5)
        return f"ok {self.role}"

    def stop(self):
        pass


def _timeout_factory(fail_role="architect", provider="claude-code"):
    def factory(role, prov, *, run_dir=None):
        if role == fail_role:
            return _TimeoutSession(role, run_dir=run_dir, fail_role=fail_role, provider=provider)
        return agent_sessions.EchoSession(role, run_dir=run_dir)
    return factory


class _SleepSession(agent_sessions.AgentSession):
    """Drives a REAL `sleep` subprocess through run_control for one role — so a test can prove cancel
    kills the managed process AND propagates, with no external agent. The blocking call is genuinely
    registered in the process registry (run_control.active_runs)."""

    def __init__(self, role, *, run_dir=None, seconds=20, provider="claude-code"):
        super().__init__(role, provider, run_dir=run_dir)
        self.seconds = seconds

    def start(self):
        self._started = True

    def send(self, message, metadata=None):
        run_id = (metadata or {}).get("runId")
        run_control.run_managed(["sleep", str(self.seconds)], run_id=run_id, provider=self.provider,
                                role=self.role, phase=(metadata or {}).get("phase", "plan"), timeout=120)
        return f"ok {self.role}"

    def stop(self):
        pass


def _sleep_factory(block_role="architect", seconds=20):
    def factory(role, prov, *, run_dir=None):
        if role == block_role:
            return _SleepSession(role, run_dir=run_dir, seconds=seconds)
        return agent_sessions.EchoSession(role, run_dir=run_dir)
    return factory


class _PlanErrorSession(agent_sessions.EchoSession):
    """Architect returns a provider error ('API Error: Overloaded') on its plan; echo otherwise."""
    def send(self, message, metadata=None):
        if self.role == "architect" and (metadata or {}).get("phase") == "plan":
            return "API Error: Overloaded"
        return super().send(message, metadata)


def _plan_error_factory():
    def factory(role, prov, *, run_dir=None):
        if role == "architect":
            return _PlanErrorSession(role, run_dir=run_dir)
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

    def test_provider_error_blocks_slice_and_program(self):
        prog = self._run("1. Add a /healthz endpoint.", session_factory=_plan_error_factory())
        self.assertEqual(prog["status"], pg.STATUS_BLOCKED)
        self.assertEqual(prog["blockerReason"], pg.BLOCKED_PROVIDER_ERROR)   # propagated to the program
        sl = prog["slices"][0]
        self.assertEqual(sl["status"], pg.SLICE_BLOCKED)
        self.assertEqual(sl["failureReason"], pg.BLOCKED_PROVIDER_ERROR)
        self.assertIsNone(sl["latestCommit"])                               # no commit on a provider error
        # the slice episode is blocked and its transcript carries the real provider error
        ep = self.p.get_episode(sl["episodeId"])
        self.assertEqual(ep["status"], "blocked")
        from openfde import external_council as ec
        tx = ec.load_recorded_transcript(self.root)
        blocked = [t for t in tx if t["kind"] == "blocked"]
        self.assertTrue(blocked and "API Error: Overloaded" in blocked[-1]["summary"])

    def test_terminal_program_slice_and_run_have_completed_at(self):
        prog = self._run("1. Add a /healthz endpoint. 2. Add request logging.")
        self.assertEqual(prog["status"], pg.STATUS_COMPLETE)
        self.assertTrue(prog["completedAt"])                       # complete program timestamped
        for sl in prog["slices"]:
            self.assertEqual(sl["status"], pg.SLICE_VERIFIED)
            self.assertTrue(sl["completedAt"])                     # each verified slice timestamped
            self.assertTrue(ac.load_run(self.root, sl["runId"])["completedAt"])  # and its run
        self.assertTrue(pg.program_summary(prog)["completedAt"])   # exposed in the summary

    def test_blocked_program_has_completed_at(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: still broken"]})
        prog = self._run("1. Add a /healthz endpoint.", session_factory=f, max_loops=1)
        self.assertEqual(prog["status"], pg.STATUS_BLOCKED)
        self.assertTrue(prog["completedAt"])                       # blocked terminal also timestamped

    def test_hydrate_program_episode_files_from_commit(self):
        sha = _git_init_commit(self.root, "feature.py", "z = 3\n")
        self.p.upsert_episode({"episodeId": "episode_p", "status": "landed", "files": [], "commitShas": [sha]})
        pg.upsert_program(self.root, {"programId": "program_x", "title": "P", "status": "complete", "slices": [
            {"sliceId": "slice_a", "status": "verified", "episodeId": "episode_p", "commits": [sha], "runId": ""}]})
        self.assertGreaterEqual(pg.hydrate_program_episode_files(self.p), 1)
        self.assertEqual(self.p.get_episode("episode_p")["files"], ["feature.py"])  # named from the diff

    def test_successful_receipt_keeps_commits_and_tasks(self):
        prog = self._run("1. Add a /healthz endpoint.")                # regression: receipts intact
        sl = prog["slices"][0]
        self.assertTrue(self.p.get_episode(sl["episodeId"])["commitShas"])  # commit receipt still attached
        cards = [t for t in self.p.load_tasks() if t.get("phaseKey") and t["episodeId"] == sl["episodeId"]]
        self.assertEqual(len(cards), 5)                            # OpenPM phase cards still created

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

    def test_provider_timeout_blocks_run_slice_and_program(self):
        prog = pg.run(self.p, prompt="Add a /healthz endpoint",
                      providers={"architect": "claude-code", "srDev": "echo", "verifier": "echo"},
                      provider_ids={"architect": "claude-code-local", "srDev": "echo", "verifier": "echo"},
                      session_factory=_timeout_factory("architect", "claude-code"), max_loops=1)
        self.assertEqual(prog["status"], pg.STATUS_BLOCKED)
        self.assertEqual(prog["blockerReason"], pg.BLOCKED_PROVIDER_TIMEOUT)
        sl = prog["slices"][0]
        self.assertEqual(sl["status"], pg.SLICE_BLOCKED)
        self.assertEqual(sl["failureReason"], pg.BLOCKED_PROVIDER_TIMEOUT)
        rec = ac.load_run(self.root, sl["runId"])
        self.assertEqual(rec["status"], ac.STATUS_BLOCKED_PROVIDER_TIMEOUT)
        self.assertEqual(rec["timeoutInfo"]["role"], "architect")
        self.assertEqual(rec["timeoutInfo"]["providerId"], "claude-code-local")   # exact id, not adapter
        self.assertEqual(rec["timeoutInfo"]["displayLabel"], "Claude Code local")
        msg = [t["summary"] for t in rec["turns"] if "timed out while using" in (t.get("summary") or "")]
        self.assertEqual(msg, ["Architect timed out while using Claude Code local."])

    def test_cancel_program_propagates_to_slice_run_and_task(self):
        prog = pg.start_program(self.p, prompt="Add a /healthz endpoint. Add request logging.", providers=ECHO)
        sl = prog["slices"][0]
        rec = ac.init_run(self.p, prompt=sl["prompt"], providers=prog["roleAssignments"],
                          program_id=prog["programId"], slice_id=sl["sliceId"], slice_title=sl["title"])
        sl["status"], sl["runId"], sl["episodeId"] = pg.SLICE_RUNNING, rec["runId"], rec["episodeId"]
        prog["status"], prog["currentSliceId"] = pg.STATUS_RUNNING, sl["sliceId"]
        pg.upsert_program(self.root, prog)

        out = pg.cancel_program(self.p, prog["programId"])
        self.assertEqual(out["status"], pg.STATUS_CANCELLED)
        self.assertEqual(out["slices"][0]["status"], pg.SLICE_CANCELLED)
        self.assertNotIn(pg.SLICE_RUNNING, [s["status"] for s in out["slices"]])   # nothing left running
        self.assertEqual(ac.load_run(self.root, rec["runId"])["status"], ac.STATUS_CANCELLED)
        self.assertTrue(run_control.is_cancelled(rec["runId"]))                    # subprocess poll will abort
        tasks = [t for t in self.p.load_tasks() if t.get("episodeId") == rec["episodeId"]]
        self.assertTrue(any(t.get("cancelled") for t in tasks))                    # active phase task cancelled
        run_control.reset(rec["runId"])

    def _terminal_program(self, status, *, blocker=None, slice_extra=None):
        """A persisted program in a TERMINAL state with one stale RUNNING child slice (the pre-fix bug)."""
        prog = pg.start_program(self.p, prompt="Add a /healthz endpoint. Add request logging.", providers=ECHO)
        prog["status"], prog["blockerReason"] = status, blocker
        prog["currentSliceId"] = prog["slices"][0]["sliceId"]
        prog["slices"][0]["status"] = pg.SLICE_RUNNING        # stale: running under a terminal parent
        prog["slices"][0].pop("runId", None)                  # pre-fix slices had no runId
        if slice_extra:
            prog["slices"][0].update(slice_extra)
        return pg.upsert_program(self.root, prog)

    def test_repair_cancelled_parent_marks_running_slice_cancelled(self):
        self._terminal_program(pg.STATUS_CANCELLED)
        n = pg.repair_stale_program_slices(self.p)
        self.assertGreaterEqual(n, 1)
        prog = pg.active_program(self.root) or pg.latest_program(self.root)
        self.assertEqual(prog["slices"][0]["status"], pg.SLICE_CANCELLED)
        self.assertIn("repaired", prog["slices"][0].get("repairNote", ""))
        self.assertIsNone(prog["currentSliceId"])                  # no live baton on a finished arc
        self.assertNotIn(pg.SLICE_RUNNING, [s["status"] for s in prog["slices"]])

    def test_repair_blocked_parent_marks_running_slice_blocked_with_reason(self):
        self._terminal_program(pg.STATUS_BLOCKED, blocker=pg.BLOCKED_PROVIDER_TIMEOUT)
        pg.repair_stale_program_slices(self.p)
        sl = (pg.latest_program(self.root))["slices"][0]
        self.assertEqual(sl["status"], pg.SLICE_BLOCKED)
        self.assertEqual(sl["failureReason"], pg.BLOCKED_PROVIDER_TIMEOUT)

    def test_repair_blocked_parent_without_reason_does_not_fabricate(self):
        self._terminal_program(pg.STATUS_BLOCKED, blocker=None)
        pg.repair_stale_program_slices(self.p)
        sl = (pg.latest_program(self.root))["slices"][0]
        self.assertEqual(sl["status"], pg.SLICE_RUNNING)           # left as-is — no reason to fabricate
        self.assertNotIn("repairNote", sl)

    def test_repair_never_rewrites_verified_history(self):
        prog = self._terminal_program(pg.STATUS_CANCELLED)
        prog["slices"][1]["status"] = pg.SLICE_VERIFIED
        prog["slices"][1]["commits"] = ["abc1234"]
        pg.upsert_program(self.root, prog)
        pg.repair_stale_program_slices(self.p)
        sl1 = (pg.latest_program(self.root))["slices"][1]
        self.assertEqual(sl1["status"], pg.SLICE_VERIFIED)         # success untouched
        self.assertEqual(sl1["commits"], ["abc1234"])

    def test_repair_complete_parent_running_slice_with_commit_becomes_verified(self):
        self._terminal_program(pg.STATUS_COMPLETE, slice_extra={"commits": ["dead123"], "latestCommit": "dead123"})
        pg.repair_stale_program_slices(self.p)
        self.assertEqual((pg.latest_program(self.root))["slices"][0]["status"], pg.SLICE_VERIFIED)

    def test_repair_is_idempotent_and_runs_inside_reconcile(self):
        self._terminal_program(pg.STATUS_CANCELLED)
        pg.reconcile_program_slices(self.p)                        # reconcile invokes the repair on load
        first = (pg.latest_program(self.root))["slices"][0]["status"]
        self.assertEqual(first, pg.SLICE_CANCELLED)
        self.assertEqual(pg.repair_stale_program_slices(self.p), 0)  # nothing left to repair

    def test_cancel_propagates_through_a_live_managed_provider(self):
        import threading
        import time
        prog = pg.start_program(self.p, prompt="Add a /healthz endpoint",
                                providers={"architect": "claude-code", "srDev": "echo", "verifier": "echo"})
        pid = prog["programId"]
        th = threading.Thread(target=lambda: pg.advance_program(self.p, prog, session_factory=_sleep_factory("architect")))
        th.start()
        try:
            run_id = None                                     # wait until the architect's sleep is LIVE
            for _ in range(60):
                cur = pg.get_program(self.root, pid)["slices"][0]
                run_id = cur.get("runId")
                if run_id and run_id in run_control.active_runs():
                    break
                time.sleep(0.1)
            self.assertTrue(run_id, "slice never recorded a runId")
            self.assertIn(run_id, run_control.active_runs(), "managed subprocess never registered")

            out = pg.cancel_program(self.p, pid)              # cancel as the UI would
            th.join(timeout=15)
            self.assertFalse(th.is_alive(), "advance_program did not return after cancel")

            fresh = pg.get_program(self.root, pid)
            self.assertEqual(out["status"], pg.STATUS_CANCELLED)                          # program
            self.assertEqual(fresh["status"], pg.STATUS_CANCELLED)
            self.assertNotIn(pg.SLICE_RUNNING, [s["status"] for s in fresh["slices"]])    # slice: none running
            self.assertEqual(ac.load_run(self.root, run_id)["status"], ac.STATUS_CANCELLED)  # autonomous run
            self.assertNotIn(run_id, run_control.active_runs())                           # process registry cleared
            tasks = [t for t in self.p.load_tasks() if t.get("episodeId") == fresh["slices"][0].get("episodeId")]
            self.assertTrue(any(t.get("cancelled") for t in tasks))                       # tasks no longer active
        finally:
            run_control.request_cancel(run_id) if run_id else None
            th.join(timeout=10)
            if run_id:
                run_control.reset(run_id)

    def test_terminal_run_status_is_sticky_against_inflight_save(self):
        rec = ac.init_run(self.p, prompt="x", providers=ECHO, product=False)
        ac.cancel_run(self.root, rec["runId"])                       # external cancel → cancelled on disk
        self.assertEqual(ac.load_run(self.root, rec["runId"])["status"], ac.STATUS_CANCELLED)
        rec["status"], rec["phase"] = ac.STATUS_RUNNING, "ARCHITECT_PLANNING"  # a late relay turn
        ac.save_run(self.root, rec)
        self.assertEqual(ac.load_run(self.root, rec["runId"])["status"], ac.STATUS_CANCELLED)  # not resurrected

    def test_one_file_docs_prompt_makes_a_single_slice(self):
        for prompt in ("Add exactly one sentence to the README. Also mention the license. And the author.",
                       "README only: add a build badge. Then add a logo. Then a table of contents.",
                       "Make the smallest change to fix the header typo. Then reformat. Then rename the file."):
            slices, block = pg.plan_program(prompt)
            self.assertIsNone(block)
            self.assertEqual(len(slices), 1, prompt)        # one-change intent → one slice, not per-sentence
        # a genuinely multi-part direction still decomposes
        slices, _ = pg.plan_program("Add a /healthz endpoint. Add request logging. Add a metrics route.")
        self.assertGreater(len(slices), 1)

    def test_provider_ids_preserved_distinct_from_display_labels(self):
        prog = pg.start_program(self.p, prompt="Add a /healthz endpoint",
                                providers={"architect": "claude-code", "srDev": "codex", "verifier": "echo"},
                                provider_ids={"architect": "claude-code-local", "srDev": "codex-local",
                                              "verifier": "echo"})
        self.assertEqual(prog["roleProviderIds"]["architect"], "claude-code-local")   # exact id kept
        self.assertEqual(prog["roleAssignments"]["architect"], "claude-code")         # adapter distinct
        arch = next(r for r in prog["roleProviders"] if r["role"] == "architect")
        self.assertEqual(arch["providerId"], "claude-code-local")
        self.assertEqual(arch["displayLabel"], "Claude Code local")
        self.assertEqual(arch["adapter"], "claude-code")
        # the CLI status shows the REAL provider id, not just the adapter
        status = pg.program_status(prog, "architect")
        self.assertIn("claude-code-local", status)

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
