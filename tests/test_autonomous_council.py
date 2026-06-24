"""Tests for openfde.autonomous_council — the OpenFDE-managed autonomous relay.

Proven end-to-end with the deterministic ``echo`` adapter (no human copy-paste, no real CLI calls):
full happy path, the changes-requested fix loop, the retry-budget block, an honest
adapter-unavailable block (precise reason), parent-episode attachment, smoke runs that do NOT
pollute Story/OpenPM, the five OpenPM phase cards, and transcript role order."""

import secrets
import shutil
import tempfile
import unittest
from pathlib import Path

from openfde import agent_sessions
from openfde import autonomous_council as ac
from openfde import external_council as ec
from openfde import run_control
from openfde.episode_summary import internal_council_kind
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


class _RecordingSession(agent_sessions.AgentSession):
    """Records every send (role, phase, message) and returns deterministic, parseable replies — so a
    test can see WHICH provider calls happened (was ARCHITECT_DECIDING invoked?) and inspect the exact
    implementation prompt, with no real agent. Optionally times out on one phase."""

    def __init__(self, role, *, run_dir=None, calls, consult_reply=None, timeout_on=None):
        super().__init__(role, "echo", run_dir=run_dir)
        self.calls, self.consult_reply, self.timeout_on = calls, consult_reply, timeout_on

    def start(self):
        self._started = True

    def send(self, message, metadata=None):
        phase = (metadata or {}).get("phase", "")
        self.calls.append({"role": self.role, "phase": phase, "message": message})
        if self.timeout_on and phase == self.timeout_on:
            raise run_control.ProviderTimeout("claude-code", self.role, phase, 7)
        if phase == "plan":
            return "PLAN: do X minimally.\n- task: change X\nacceptance: X works."
        if phase == "consult":
            return self.consult_reply if self.consult_reply is not None else "The plan is reasonable; minor notes."
        if phase == "decide":
            return "DECISION: implement X minimally; scope stays small."
        if phase == "implement":
            return "IMPLEMENTED: changed X commit=abc1234567 checks=did X"
        if phase == "verify":
            return "VERIFIED looks correct"
        return f"{self.role} {phase}"

    def stop(self):
        pass


def _recording_factory(calls, *, consult_reply=None, timeout_on=None, timeout_role="architect"):
    def factory(role, provider, *, run_dir=None):
        return _RecordingSession(role, run_dir=run_dir, calls=calls, consult_reply=consult_reply,
                                 timeout_on=(timeout_on if role == timeout_role else None))
    return factory


class ConsultationClassifierTest(unittest.TestCase):
    def test_clear_phrases_are_clear_to_implement(self):
        for txt in ("Looks good.", "Plan approved.", "This is reasonable.", "Minor notes only.",
                    "Keep scope small.", "Add a test for the parser.", "Watch the retry count.", "",
                    "lgtm, proceed", "The plan is reasonable; push back: keep the surface small, add a test."):
            self.assertEqual(ac.classify_consultation(txt), ac.CLEAR_TO_IMPLEMENT, txt)

    def test_blocking_phrases_need_architect_decision(self):
        for txt in ("This is blocked until we decide the schema.", "Do not proceed.",
                    "This needs an architect decision.", "There is a security concern here.",
                    "This crosses a permission boundary.", "The scope is wrong.",
                    "The architect must decide between A and B.", "This is a conflicting approach.",
                    "Major risk: this could corrupt data."):
            self.assertEqual(ac.classify_consultation(txt), ac.NEEDS_ARCHITECT_DECISION, txt)

    def test_negated_blocker_is_not_a_blocker(self):
        for txt in ("No security concern; proceed.", "There is not a blocker here.",
                    "No major risk — looks fine."):
            self.assertEqual(ac.classify_consultation(txt), ac.CLEAR_TO_IMPLEMENT, txt)


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
        # The echo consult is minor notes (no blocker) → the decision is an HONEST automatic system
        # turn, not a second architect provider call.
        self._run()
        labels = [t["label"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(labels, ["user", "architect (echo)", "sr dev (echo)",
                                  "architect decision (automatic)", "sr dev (echo)", "verifier (echo)", "system"])
        kinds = [t["kind"] for t in ec.load_recorded_transcript(self.root)]
        self.assertEqual(kinds, ["prompt", "proposal", "consultation", "decision",
                                 "implementation", "verified", "ready_to_push"])

    def test_transcript_returned_by_external_council_builder(self):
        self._run()
        tx = ec.build_council_transcript(self.root)
        labels = [it["label"] for it in tx["items"]]
        for lbl in ("architect (echo)", "sr dev (echo)", "verifier (echo)"):
            self.assertIn(lbl, labels)
        self.assertFalse(tx["active"])                                 # VERIFIED → inactive

    def test_transcript_scopes_to_latest_run_previous_below(self):
        r1 = self._run(prompt="first task")
        r2 = self._run(prompt="second task")
        tx = ec.build_council_transcript(self.root)
        self.assertEqual({it.get("runId") for it in tx["items"]}, {r2["runId"]})        # only the latest run
        self.assertEqual({it.get("runId") for it in tx["previousItems"]}, {r1["runId"]})  # older run separated
        self.assertGreaterEqual(tx["previousRunCount"], 1)
        self.assertNotIn(r1["runId"], {it.get("runId") for it in tx["items"]})           # never interleaved

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

    # ── Conditional second architect decision ─────────────────────────────────
    def test_clear_consultation_skips_the_architect_decision_call(self):
        calls = []
        rec = self._run(product=False, session_factory=_recording_factory(calls, consult_reply="Looks good — add a test."))
        self.assertEqual(rec["status"], ac.STATUS_READY_TO_PUSH)
        self.assertEqual(rec.get("decisionMode"), "automatic")
        self.assertNotIn("decide", [c["phase"] for c in calls])        # NO second architect provider call
        dec = next(t for t in rec["turns"] if t["kind"] == "decision")
        self.assertEqual(dec["role"], "system")                        # honest: not a provider response
        self.assertEqual(dec["label"], "architect decision (automatic)")
        self.assertIn("no blocking pushback", dec["summary"].lower())

    def test_blocking_consultation_invokes_the_architect_decision(self):
        calls = []
        rec = self._run(product=False, session_factory=_recording_factory(
            calls, consult_reply="Do not proceed — the architect must decide the schema first."))
        self.assertEqual(rec.get("decisionMode"), "architect")
        self.assertIn("decide", [c["phase"] for c in calls])           # architect WAS called again
        dec = next(t for t in rec["turns"] if t["kind"] == "decision")
        self.assertEqual(dec["role"], "architect")

    def test_implementation_prompt_includes_plan_and_consultation(self):
        calls = []
        self._run(product=False, session_factory=_recording_factory(calls, consult_reply="Looks good; please add a test."))
        impl = next(c for c in calls if c["phase"] == "implement")["message"]
        self.assertIn("ARCHITECT PLAN", impl)
        self.assertIn("SENIOR DEV CONSULTATION", impl)
        self.assertIn("please add a test", impl)                       # the real consultation text is present
        self.assertIn("do X minimally", impl)                          # the real architect plan text is present

    def test_timeout_reason_visible_in_run_payload_when_architect_called(self):
        calls = []
        rec = self._run(product=False,
                        provider_ids={"architect": "claude-code-local", "srDev": "echo", "verifier": "echo"},
                        session_factory=_recording_factory(
                            calls, consult_reply="Security concern — the architect must decide.", timeout_on="decide"))
        self.assertEqual(rec["status"], ac.STATUS_BLOCKED_PROVIDER_TIMEOUT)
        self.assertIn("decide", [c["phase"] for c in calls])           # it DID call the architect, which timed out
        summ = ac.run_summary(rec)
        self.assertIn("timed out", (summ["blockedReason"] or "").lower())   # the ACTUAL reason…
        self.assertNotIn("BLOCKED_NEEDS_HUMAN", summ["blockedReason"] or "")  # …not the generic bus status
        self.assertEqual(summ["timeoutInfo"]["phase"], "decide")
        self.assertEqual(summ["timeoutInfo"]["providerId"], "claude-code-local")

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

    def test_run_summary_reports_terminal_after_completion(self):
        # A run that advanced through SR_DEV_CONSULTING etc. must report a TERMINAL summary once done —
        # not an older in-flight phase/role — and survive a reload from disk (the Orient banner truth).
        rec = self._run()
        summ = ac.run_summary(rec)
        self.assertEqual((summ["status"], summ["phase"]), (ac.STATUS_READY_TO_PUSH, ac.PHASE_READY_TO_PUSH))
        self.assertFalse(summ["running"])
        self.assertIsNone(summ["activeRole"])
        self.assertEqual(summ["latestTurn"]["kind"], "ready_to_push")    # terminal turn, not in-flight
        reloaded = ac.latest_run_summary(self.root)                      # reload from run.json
        self.assertEqual(reloaded["status"], ac.STATUS_READY_TO_PUSH)
        self.assertFalse(reloaded["running"])
        self.assertIsNone(reloaded["activeRole"])

    def test_blocked_run_summary_is_terminal_not_in_flight(self):
        f = _echo_factory({"verifier": ["CHANGES_REQUESTED: nope"]})
        rec = self._run(session_factory=f, max_loops=1)
        summ = ac.run_summary(rec)
        self.assertEqual(summ["status"], ac.STATUS_BLOCKED_NEEDS_HUMAN)
        self.assertFalse(summ["running"])
        self.assertIsNone(summ["activeRole"])

    def test_auto_push_hands_off_to_cc_to_push(self):
        rec = self._run(auto_push=True)
        self.assertEqual(rec["status"], ac.STATUS_VERIFIED)
        self.assertEqual([t["kind"] for t in ec.load_recorded_transcript(self.root)][-1], "push")


class CouncilNoiseMigrationTest(unittest.TestCase):
    """Internal council artifacts (verification/review/OPS/smoke/relay machinery) must fold under the
    council — off the product rail — while real work stays product. Migration + filtering + hydration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def _ep(self, **kw):
        e = {"episodeId": "episode_" + secrets.token_hex(4), "kind": "external-council",
             "signal": "product", "createdAt": "2026-01-01T00:00:00Z", "commitShas": []}
        e.update(kw)
        return e

    def test_classifier_catches_council_machinery_by_title(self):
        cases = {
            "Autonomous Council Verification": "relay",
            "Autonomous Council Relay": "relay",
            "Council Relay Developer Note": "relay",          # managed-agent capture leak
            "Council Relay Documentation": "relay",           # managed-agent capture leak
            "Claude Code Implementation Prompt": "implementation_prompt",
            "Codex Implementation Prompt": "implementation_prompt",
            "External Agent Council": "council",
            "Council Verification Smoke Test": "smoke",
        }
        for title, kind in cases.items():
            self.assertEqual(internal_council_kind(self._ep(title=title)), kind, title)
        # real FEATURE titles are NEVER folded — even when they carry council/prompt words
        for title in ("External Council Inbox", "Council Handoff Wakeups", "Persistent Council Inbox",
                      "Passive Codex Prompt Capture", "build an agentic SaaS for insurance",
                      "add a code review feature"):
            self.assertIsNone(internal_council_kind(self._ep(title=title)), title)
        # a machinery TITLE with a commit IS folded (a council-relay implementation commit is machinery)
        self.assertEqual(internal_council_kind(self._ep(title="Autonomous Council Relay", commitShas=["abc"])), "relay")
        # only a real autonomous run (a recorded council loop) is protected
        self.assertIsNone(internal_council_kind(self._ep(title="Autonomous Council Relay",
                                                         council={"status": "verified"})))

    def test_migration_demotes_internal_and_is_reversible(self):
        noisy = self._ep(title="Autonomous Council Relay", commitShas=["face0ff"])   # machinery + commit
        real = self._ep(title="External Council Inbox", commitShas=["deadbeef"])     # real feature
        self.p.upsert_episode(noisy)
        self.p.upsert_episode(real)
        self.p.flag_internal_council_episodes()
        n = self.p.get_episode(noisy["episodeId"])
        self.assertTrue(n["internal"])
        self.assertEqual((n["internalKind"], n["signal"], n["nonImplementation"]),
                         ("relay", "operational", True))
        self.assertEqual(self.p.get_episode(real["episodeId"])["signal"], "product")
        # reversible: it becomes a real autonomous run (a recorded council loop) → product again
        n["council"] = {"status": "verified"}
        self.p.upsert_episode(n)
        self.p.flag_internal_council_episodes()
        self.assertFalse(self.p.get_episode(noisy["episodeId"]).get("internal"))

    def test_rail_excludes_internal_artifacts(self):
        from openfde.server import build_rail_payload
        self.p.upsert_episode(self._ep(title="Autonomous Council Relay", commitShas=["aaa1111"]))
        self.p.upsert_episode(self._ep(title="External Council Inbox", commitShas=["abc1234"]))
        titles = [c["title"] for c in build_rail_payload(self.p)["episodes"]]
        self.assertIn("External Council Inbox", titles)
        self.assertNotIn("Autonomous Council Relay", titles)         # folded under the council

    def test_hydrate_phase_cards_for_parent_with_council(self):
        parent = self._ep(title="build X", commitShas=["abc1234"], council={
            "status": "ready_to_push", "latestCommit": "abc1234",
            "edges": ["proposed", "consulted", "decided", "implemented", "verified"]})
        self.p.upsert_episode(parent)
        self.assertEqual(ac.hydrate_phase_cards(self.p), 5)
        byk = {t["phaseKey"]: t for t in self.p.load_tasks() if t.get("episodeId") == parent["episodeId"]}
        self.assertEqual(set(byk), {"plan", "consult", "implement", "verify", "push"})
        self.assertEqual(byk["implement"]["commitSha"], "abc1234")
        self.assertEqual(byk["implement"]["column"], "done")
        self.assertEqual(byk["push"]["column"], "done")
        self.assertEqual(ac.hydrate_phase_cards(self.p), 0)           # idempotent

    def test_hydrate_skips_internal_episodes(self):
        internal = self._ep(title="Autonomous Council Verification", internal=True,
                            council={"status": "verified", "edges": ["verified"]})
        self.p.upsert_episode(internal)
        self.assertEqual(ac.hydrate_phase_cards(self.p), 0)           # no cards for demoted machinery

    def test_parent_council_receipt_payload(self):
        rec = ac.run(self.p, prompt="ship it",
                     providers={"architect": "echo", "srDev": "echo", "verifier": "echo"},
                     session_factory=_echo_factory())
        c = self.p.get_episode(rec["episodeId"])["council"]
        self.assertEqual(c["status"], ac.STATUS_READY_TO_PUSH)
        self.assertEqual(c["edges"], ["proposed", "consulted", "decided", "implemented", "verified"])
        labels = [t["label"] for t in c["turns"]]
        for lbl in ("user", "architect (echo)", "sr dev (echo)", "verifier (echo)", "system"):
            self.assertIn(lbl, labels)


if __name__ == "__main__":
    unittest.main()
