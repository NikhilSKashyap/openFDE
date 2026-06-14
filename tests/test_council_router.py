"""
Tests for openfde.council_router — deterministic v1 routing + the read-only ask
orchestrator. The laws under test: explicit targets win; auto routes by intent;
discuss triggers on "both/discuss"; a busy senior_dev.edit NEVER blocks senior_dev
chat; and run_ask only ever calls injected TEXT callers (it cannot dispatch a run).
"""
import unittest

from openfde import council_router as R


class RouteExplicitTargetTest(unittest.TestCase):
    def test_explicit_single_roles(self):
        for role in ("architect", "senior_dev", "verifier"):
            d = R.route("literally anything", role, {})
            self.assertEqual(d["mode"], "single")
            self.assertEqual(d["primaryRole"], role)
            self.assertEqual(d["confidence"], 1.0)

    def test_explicit_discuss(self):
        d = R.route("anything", "discuss", {})
        self.assertEqual(d["mode"], "discuss")
        self.assertEqual(d["roles"], ["architect", "senior_dev"])
        self.assertEqual(d["primaryRole"], "architect")

    def test_unknown_target_falls_back_to_auto(self):
        d = R.route("what is the architecture roadmap tradeoff?", "bogus", {})
        self.assertEqual(d["primaryRole"], "architect")     # auto classified


class RouteAutoTest(unittest.TestCase):
    def test_architecture_question_to_architect(self):
        d = R.route("What's the product roadmap tradeoff — what do you think?", "auto", {})
        self.assertEqual(d["mode"], "single")
        self.assertEqual(d["primaryRole"], "architect")

    def test_implementation_question_to_senior_dev(self):
        d = R.route("Why is this function failing — debug the code path and patch the bug?",
                    "auto", {})
        self.assertEqual(d["primaryRole"], "senior_dev")

    def test_verify_question_to_verifier(self):
        d = R.route("Is the PR ready — did verification and the test suite pass?", "auto", {})
        self.assertEqual(d["primaryRole"], "verifier")

    def test_no_signal_defaults_to_architect_low_confidence(self):
        d = R.route("hello there", "auto", {})
        self.assertEqual(d["primaryRole"], "architect")
        self.assertLess(d["confidence"], 0.5)


class RouteDiscussTriggerTest(unittest.TestCase):
    def test_explicit_discuss_word(self):
        self.assertEqual(R.route("Can you discuss this design choice?", "auto", {})["mode"],
                         "discuss")

    def test_both_roles_named(self):
        d = R.route("I want the architect and senior dev to look at this.", "auto", {})
        self.assertEqual(d["mode"], "discuss")

    def test_balanced_high_signal_becomes_discuss(self):
        # strong on BOTH architecture (architecture/tradeoff/should we = 3) and
        # implementation (debug/refactor = 2), within 1 → discuss the tradeoff
        q = "Should we weigh the architecture tradeoff, or just debug and refactor this?"
        self.assertEqual(R.route(q, "auto", {})["mode"], "discuss")


class BusyDoesNotBlockChatTest(unittest.TestCase):
    SD_BUSY = {"senior_dev": {"available": True, "workBusy": True}}
    VER_BUSY = {"verifier": {"available": True, "workBusy": True}}

    def test_auto_senior_dev_intent_with_work_busy_still_routes_to_senior_dev(self):
        d = R.route("debug this failing test in the code path and patch it", "auto", self.SD_BUSY)
        self.assertEqual(d["primaryRole"], "senior_dev")    # NOT rerouted away
        self.assertIn("senior_dev", d["workBusyRoles"])     # only reported

    def test_explicit_senior_dev_target_with_work_busy(self):
        d = R.route("anything", "senior_dev", self.SD_BUSY)
        self.assertEqual(d["primaryRole"], "senior_dev")
        self.assertIn("senior_dev", d["workBusyRoles"])

    def test_verifier_work_busy_does_not_block_verifier_chat(self):
        # generalized: ANY role's .work busy must not block its .chat
        d = R.route("did the test suite and verification pass?", "auto", self.VER_BUSY)
        self.assertEqual(d["primaryRole"], "verifier")
        self.assertIn("verifier", d["workBusyRoles"])

    def test_idle_when_not_busy(self):
        d = R.route("debug the code path", "senior_dev", {})
        self.assertEqual(d["workBusyRoles"], [])


class RunAskTest(unittest.TestCase):
    def _rec(self, reply):
        """A recording text caller: appends (system, user) and returns `reply`."""
        def caller(system, user):
            caller.calls.append((system, user))
            return reply
        caller.calls = []
        return caller

    def test_single_uses_primary_caller_and_is_text_only(self):
        arch = self._rec("Architecture answer.")
        d = R.route("roadmap tradeoff?", "architect", {})
        res = R.run_ask(question="roadmap tradeoff?", decision=d,
                        context={"repo": {"branch": "main"}}, callers={"architect": arch})
        self.assertEqual(res["answer"], "Architecture answer.")
        self.assertEqual(res["usedRole"], "architect")
        self.assertEqual(res["label"], "Architect")            # single-role primary label
        self.assertEqual(res["contributors"], ["Architect"])
        self.assertEqual(res["contributorsLabel"], "Architect")
        self.assertFalse(res["fallback"])
        # text-only contract: caller got (str, str); nothing else was ever invoked
        self.assertEqual(len(arch.calls), 1)
        self.assertIsInstance(arch.calls[0][0], str)
        self.assertIsInstance(arch.calls[0][1], str)

    def test_fallback_to_architect_when_primary_unavailable(self):
        arch = self._rec("Architect fallback answer.")
        d = R.route("debug the code path", "senior_dev", {})
        res = R.run_ask(question="debug the code path", decision=d, context={},
                        callers={"architect": arch, "senior_dev": None})
        self.assertEqual(res["usedRole"], "architect")
        self.assertTrue(res["fallback"])
        self.assertEqual(res["answer"], "Architect fallback answer.")

    def test_no_provider_returns_deterministic_context_answer(self):
        d = R.route("roadmap?", "architect", {})
        res = R.run_ask(question="roadmap?", decision=d,
                        context={"repo": {"branch": "main", "dirtyCount": 0}}, callers={})
        self.assertIsNone(res["usedRole"])
        self.assertEqual(res["label"], "OpenFDE")          # no provider → OpenFDE label
        self.assertIn("branch main", res["answer"])        # answered from the brief

    def test_provider_error_degrades_to_fallback(self):
        def boom(system, user):
            raise RuntimeError("provider down")
        arch = self._rec("Architect saved it.")
        d = R.route("debug the code path", "senior_dev", {})
        res = R.run_ask(question="debug the code path", decision=d, context={},
                        callers={"senior_dev": boom, "architect": arch})
        self.assertEqual(res["usedRole"], "architect")     # SD raised → Architect

    def test_discuss_synthesizes_with_architect_and_receipt(self):
        arch = self._rec("SYNTHESIZED")
        sd = self._rec("Senior Dev note")
        d = R.route("discuss this tradeoff", "discuss", {})
        res = R.run_ask(question="discuss this tradeoff", decision=d, context={},
                        callers={"architect": arch, "senior_dev": sd})
        self.assertEqual(res["mode"], "discuss")
        self.assertEqual(res["label"], "Council")              # multi-role primary label
        self.assertEqual(res["contributorsLabel"], "Architect · Senior Dev")
        self.assertEqual(res["answer"], "SYNTHESIZED")
        self.assertIn("architect", res["roleNotes"])
        self.assertIn("senior_dev", res["roleNotes"])
        self.assertEqual(len(sd.calls), 1)                 # SD note once
        self.assertEqual(len(arch.calls), 2)               # arch note + synthesis

    def test_discuss_deterministic_when_synthesis_empty(self):
        replies = iter(["Arch note", ""])                  # note, then empty synthesis
        def arch(system, user):
            return next(replies, "")
        sd = self._rec("SD note")
        d = R.route("discuss", "discuss", {})
        res = R.run_ask(question="discuss", decision=d, context={},
                        callers={"architect": arch, "senior_dev": sd})
        self.assertEqual(res["label"], "Council")
        self.assertEqual(res["contributorsLabel"], "Architect · Senior Dev")
        self.assertIn("**Architect:** Arch note", res["answer"])
        self.assertIn("**Senior Dev:** SD note", res["answer"])

    def test_discuss_single_role_when_only_one_available(self):
        sd = self._rec("SD only")
        d = R.route("discuss", "discuss", {})
        res = R.run_ask(question="discuss", decision=d, context={}, callers={"senior_dev": sd})
        self.assertEqual(res["usedRole"], "senior_dev")
        self.assertEqual(res["label"], "Senior Dev")        # 1 contributor → role label
        self.assertEqual(res["answer"], "SD only")


if __name__ == "__main__":
    unittest.main()
