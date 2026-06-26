"""Tests for openfde.intent_router — the deterministic Orient intent router (one Run button)."""

import unittest

from openfde import intent_router as ir


class IntentRouterTest(unittest.TestCase):
    def _mode(self, prompt):
        return ir.route_intent(prompt)["mode"]

    # ── Program: clearly enumerated multi-slice / phase / part ────────────────
    def test_explicit_slices_route_to_program(self):
        res = ir.route_intent("Slice 1: update a harmless README sentence. "
                              "Slice 2: add a second harmless README sentence.")
        self.assertEqual(res["mode"], ir.MODE_PROGRAM)
        self.assertTrue(res["allowEdits"])                       # implementation route → edits default on
        self.assertGreaterEqual(len(res["detectedSlices"]), 2)
        self.assertIn("slice1", res["signals"])
        self.assertIn("slice2", res["signals"])

    def test_phases_and_parts_and_flow_route_to_program(self):
        self.assertEqual(self._mode("Phase 1: scaffold the API. Phase 2: wire the UI."), ir.MODE_PROGRAM)
        self.assertEqual(self._mode("Part 1: add the model. Part 2: add the route. Part 3: add tests."),
                         ir.MODE_PROGRAM)
        self.assertEqual(self._mode("First add a /healthz endpoint, then add request logging."),
                         ir.MODE_PROGRAM)

    # ── Council: one bounded implementation / review task ─────────────────────
    def test_single_task_routes_to_council(self):
        for p in ("fix this bug in the parser", "update README", "add a button to the toolbar",
                  "make this label clearer", "verify this change"):
            res = ir.route_intent(p)
            self.assertEqual(res["mode"], ir.MODE_COUNCIL, p)
            self.assertTrue(res["allowEdits"], p)               # council implementation → edits default on

    def test_casual_slice_mention_does_not_force_program(self):
        # the WORD "slice" without enumerated "Slice 1:/Slice 2:" markers stays a single task
        for p in ("add a slice to the pie chart legend", "fix the slice calculation in the parser",
                  "slice the toolbar into two rows"):
            self.assertEqual(self._mode(p), ir.MODE_COUNCIL, p)

    # ── Ask: question / planning only, never edits ────────────────────────────
    def test_questions_route_to_ask_with_no_edits(self):
        for p in ("what do you think of the architecture?", "should we add caching here?",
                  "explain the council loop", "how does this work?"):
            res = ir.route_intent(p)
            self.assertEqual(res["mode"], ir.MODE_ASK, p)
            self.assertFalse(res["allowEdits"], p)              # planning never edits files

    # ── Issue: report / raise an issue ────────────────────────────────────────
    def test_issue_requests_route_to_issue(self):
        for p in ("raise issue: the login button is broken", "create a GitHub issue for the flaky test",
                  "this broke when I clicked save"):
            res = ir.route_intent(p)
            self.assertEqual(res["mode"], ir.MODE_ISSUE, p)
            self.assertFalse(res["allowEdits"], p)

    # ── Clarify: only under-specified or high blast radius ────────────────────
    def test_dangerous_or_underspecified_routes_to_clarify(self):
        for p in ("rewrite the entire codebase", "migrate the whole database", "fix", "do stuff"):
            res = ir.route_intent(p)
            self.assertEqual(res["mode"], ir.MODE_CLARIFY, p)
            self.assertFalse(res["allowEdits"], p)

    def test_result_shape_is_stable(self):
        res = ir.route_intent("update README")
        self.assertEqual(set(res), {"mode", "confidence", "reason", "allowEdits", "detectedSlices", "signals"})
        self.assertIsInstance(res["confidence"], float)
        self.assertTrue(res["reason"])

    def test_two_slice_program_plans_real_slices(self):
        # the router must hand a multi-slice prompt to Program Mode (never a standalone programId:null run)
        from openfde import program as pg
        res = ir.route_intent("Slice 1: add a /healthz route. Slice 2: add request logging.")
        self.assertEqual(res["mode"], ir.MODE_PROGRAM)
        slices, block = pg.plan_program("Slice 1: add a /healthz route. Slice 2: add request logging.")
        self.assertIsNone(block)
        self.assertGreaterEqual(len(slices), 2)


if __name__ == "__main__":
    unittest.main()
