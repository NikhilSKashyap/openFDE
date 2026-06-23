"""Tests for openfde.handoff_broker — event-driven session wakeup / agent handoff resume.

Each council transition creates exactly one durable delivery for the receiving role (deduped,
superseded, closed on VERIFIED); native wakeup is reported honestly; existing ids are preserved and
no new council id is invented."""

import tempfile
import unittest

from openfde import handoff_broker as hb


def _view(status, *, episode="ep1", commit="", tasks=("task_a", "task_b"),
          boxes=("box_x",), run="run_1"):
    return {"episodeId": episode, "status": status, "taskIds": list(tasks), "runId": run,
            "boxIds": list(boxes), "latestCommit": commit, "objective": "do the thing",
            "acceptance": ["tests pass"]}


class HandoffBrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self):
        return [d for d in hb.load_deliveries(self.root)
                if not d.get("closedAt") and not d.get("supersededAt")]

    def test_ready_for_cc_creates_one_claude_delivery_preserving_ids(self):
        d = hb.process_transition(self.root, _view("READY_FOR_CC"))
        self.assertEqual(d["toRole"], "claude")
        self.assertEqual(d["fromRole"], "codex")
        self.assertEqual(d["status"], "READY_FOR_CC")
        self.assertEqual(len(self._open()), 1)
        # existing ids preserved; deliveryId is the delivery's own id, not a council id
        self.assertEqual(d["episodeId"], "ep1")
        self.assertEqual(d["taskIds"], ["task_a", "task_b"])
        self.assertEqual(d["runId"], "run_1")
        self.assertEqual(d["boxIds"], ["box_x"])
        self.assertTrue(d["deliveryId"].startswith("delivery_"))
        self.assertNotIn("councilId", d)

    def test_ready_for_verification_creates_codex_delivery(self):
        d = hb.process_transition(self.root, _view("READY_FOR_CODEX_VERIFICATION", commit="abc1234"))
        self.assertEqual(d["toRole"], "codex")
        self.assertEqual(d["fromRole"], "claude")
        self.assertEqual(d["latestCommit"], "abc1234")
        self.assertEqual(len(self._open()), 1)

    def test_changes_requested_creates_claude_delivery(self):
        d = hb.process_transition(self.root, _view("CHANGES_REQUESTED"))
        self.assertEqual(d["toRole"], "claude")

    def test_role_mapping(self):
        self.assertEqual(hb.receiving_role("BLOCKED_NEEDS_ARCHITECT"), "codex")
        self.assertEqual(hb.receiving_role("BLOCKED_NEEDS_HUMAN"), "human")
        self.assertIsNone(hb.receiving_role("VERIFIED"))
        self.assertIsNone(hb.receiving_role("CLAUDE_WORKING"))   # not a wakeup → no-op

    def test_human_block_has_no_agent_native_adapter(self):
        d = hb.process_transition(self.root, _view("BLOCKED_NEEDS_HUMAN"))
        self.assertEqual(d["toRole"], "human")
        self.assertNotIn("codex-session", [w["adapter"] for w in d["wake"]])
        self.assertNotIn("claude-session", [w["adapter"] for w in d["wake"]])

    def test_native_wakeup_reported_unavailable_not_faked(self):
        d = hb.process_transition(self.root, _view("READY_FOR_CC"))
        adapters = {w["adapter"]: w["status"] for w in d["wake"]}
        self.assertEqual(adapters["session-inbox"], "pending")    # the durable guarantee
        self.assertEqual(adapters["openfde-ui"], "delivered")
        self.assertEqual(adapters["claude-session"], "native_unavailable")   # honest

    def test_verified_closes_active_and_creates_no_wakeup(self):
        hb.process_transition(self.root, _view("READY_FOR_CODEX_VERIFICATION", commit="abc"))
        self.assertIsNotNone(hb.active_delivery(hb.load_deliveries(self.root)))
        out = hb.process_transition(self.root, _view("VERIFIED", commit="abc"))
        self.assertIsNone(out)                                    # no active wakeup created
        self.assertIsNone(hb.active_delivery(hb.load_deliveries(self.root)))   # prior closed
        self.assertEqual(self._open(), [])

    def test_claude_working_is_a_noop(self):
        hb.process_transition(self.root, _view("READY_FOR_CC"))
        out = hb.process_transition(self.root, _view("CLAUDE_WORKING"))
        self.assertIsNone(out)
        self.assertEqual(len(self._open()), 1)                    # the READY_FOR_CC delivery survives

    def test_repeated_events_do_not_duplicate(self):
        d1 = hb.process_transition(self.root, _view("READY_FOR_CC"))
        d2 = hb.process_transition(self.root, _view("READY_FOR_CC"))   # same transition replayed
        self.assertEqual(d1["deliveryId"], d2["deliveryId"])           # deduped — one record
        self.assertEqual(len(self._open()), 1)

    def test_new_transition_supersedes_prior(self):
        hb.process_transition(self.root, _view("READY_FOR_CC"))
        d2 = hb.process_transition(self.root, _view("READY_FOR_CODEX_VERIFICATION", commit="x"))
        self.assertEqual(len(self._open()), 1)                    # only the latest is open
        self.assertEqual(self._open()[0]["deliveryId"], d2["deliveryId"])
        self.assertEqual(self._open()[0]["toRole"], "codex")

    def test_acknowledge_marks_delivery(self):
        d = hb.process_transition(self.root, _view("READY_FOR_CC"))
        self.assertIsNotNone(hb.pending_delivery(self.root, "claude"))
        acked = hb.acknowledge_delivery(self.root, "claude")
        self.assertEqual(acked["deliveryId"], d["deliveryId"])
        self.assertTrue(acked["acknowledgedAt"])
        self.assertIsNone(hb.pending_delivery(self.root, "claude"))   # no longer pending


if __name__ == "__main__":
    unittest.main()
