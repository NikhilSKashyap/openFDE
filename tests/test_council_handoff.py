"""Direct coverage of the POST /api/council/implementation core (server.create_council_handoff).

The handler is a thin wrapper over this module-level function (persistence-injected, like
build_boot_payload), so testing it exercises the real endpoint behavior without a live server: the
escalation/lead gate is re-validated server-side, a pending handoff is persisted with a confirmation
turn, and the endpoint is READ-ONLY with respect to repo files (it never dispatches a run)."""
import tempfile
import unittest
from pathlib import Path

from openfde.server import create_council_handoff
from openfde.persistence import Persistence

# Questions whose routing is already pinned by tests in test_council_router.py.
PRODUCT_Q = "What product direction and roadmap should we take next?"            # → architect, canStart
IMPL_Q = "How do we implement the fix for the failing test in this code path?"   # → sr_dev,    canStart
READINESS_Q = "Is it ready to ship — do the tests pass and is there any regression to verify?"  # verifier
ESCALATE_Q = "Should we force push and reset --hard to wipe the branch?"         # destructive escalation
SECTIONS = {"productDirection": "PD", "implementationPlan": "IP", "risksVerification": "RV"}


def _persistence(d):
    return Persistence(Path(d) / ".openfde")


class CreateCouncilHandoffTest(unittest.TestCase):
    def test_valid_product_question_creates_pending_handoff(self):
        with tempfile.TemporaryDirectory() as d:
            p = _persistence(d)
            status, body = create_council_handoff(
                {"question": PRODUCT_Q, "brief": {"sections": SECTIONS}}, persistence=p, agent_states={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["handoff"]["status"], "pending")          # pending, not started
            self.assertEqual(body["handoff"]["leadRole"], "architect")
            self.assertTrue(body["handoff"]["id"].startswith("handoff_"))
            self.assertIn("handoff created", body["message"].lower())       # honest copy
            self.assertNotIn("started", body["message"].lower())
            # the pending handoff is persisted …
            handoffs = p.load_council_handoffs()
            self.assertEqual(len(handoffs), 1)
            self.assertEqual(handoffs[0]["id"], body["handoff"]["id"])
            self.assertEqual(handoffs[0]["status"], "pending")
            # … and a compact confirmation turn is appended to council history.
            turns = p.load_council_chat()
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["role"], "assistant")
            self.assertIn("handoff created", turns[0]["text"].lower())

    def test_valid_implementation_question_leads_sr_dev(self):
        with tempfile.TemporaryDirectory() as d:
            p = _persistence(d)
            status, body = create_council_handoff(
                {"question": IMPL_Q, "brief": {"sections": SECTIONS}}, persistence=p, agent_states={})
            self.assertEqual(status, 200)
            self.assertEqual(body["handoff"]["leadRole"], "sr_dev")
            self.assertEqual(len(p.load_council_handoffs()), 1)

    def test_readiness_question_rejected_409_no_persist(self):
        with tempfile.TemporaryDirectory() as d:
            p = _persistence(d)
            status, body = create_council_handoff(
                {"question": READINESS_Q, "brief": {"sections": SECTIONS}}, persistence=p, agent_states={})
            self.assertEqual(status, 409)
            self.assertFalse(body["ok"])
            self.assertEqual(p.load_council_handoffs(), [])                  # nothing persisted
            self.assertEqual(p.load_council_chat(), [])                      # no confirmation turn

    def test_escalation_question_rejected_409_no_persist(self):
        with tempfile.TemporaryDirectory() as d:
            p = _persistence(d)
            status, body = create_council_handoff(
                {"question": ESCALATE_Q, "brief": {"sections": SECTIONS}}, persistence=p, agent_states={})
            self.assertEqual(status, 409)
            self.assertTrue(body["humanEscalation"]["needed"])
            self.assertEqual(p.load_council_handoffs(), [])
            self.assertEqual(p.load_council_chat(), [])

    def test_missing_question_is_400(self):
        with tempfile.TemporaryDirectory() as d:
            p = _persistence(d)
            status, body = create_council_handoff({"brief": {"sections": SECTIONS}}, persistence=p)
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])
            self.assertEqual(p.load_council_handoffs(), [])

    def test_invalid_json_body_is_400(self):
        with tempfile.TemporaryDirectory() as d:
            p = _persistence(d)
            status, body = create_council_handoff(None, persistence=p)      # None ← invalid JSON
            self.assertEqual(status, 400)
            self.assertIn("invalid", body["error"].lower())
            self.assertEqual(p.load_council_handoffs(), [])

    def test_endpoint_does_not_mutate_repo_files(self):
        """READ-ONLY w.r.t. the repo: a tracked source file is untouched and the ONLY new top-level
        entry is .openfde/ (OpenFDE's own state) — never a repo edit."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
            before_src = (root / "app.py").read_text(encoding="utf-8")
            tree_before = sorted(x.name for x in root.iterdir())            # snapshot before Persistence
            p = Persistence(root / ".openfde")
            status, _ = create_council_handoff(
                {"question": PRODUCT_Q, "brief": {"sections": SECTIONS}}, persistence=p, agent_states={})
            self.assertEqual(status, 200)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), before_src)   # unchanged
            tree_after = sorted(x.name for x in root.iterdir())
            self.assertEqual([x for x in tree_after if x != ".openfde"], tree_before)      # only .openfde added


if __name__ == "__main__":
    unittest.main()
