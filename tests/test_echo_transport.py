"""Tests for the offline echo transport — generic marker + the support-inbox SaaS demo scaffold."""

import ast
import unittest

from openfde.echo_transport import make_echo_transport, support_inbox_plan, _match_inbox_role


INBOX_STEPS = [
    {"boxId": "inbox_ingest", "title": "ingest customer messages"},
    {"boxId": "inbox_classify", "title": "classify issue"},
    {"boxId": "inbox_draft", "title": "draft response"},
    {"boxId": "inbox_review", "title": "review approval"},
    {"boxId": "inbox_log", "title": "log resolution"},
]


def _writes(transport):
    """Drive the transport one turn and return its write_file {path: content} map."""
    resp = transport({})
    return {b["input"]["path"]: b["input"]["content"]
            for b in resp["content"] if b.get("name") == "write_file"}


class SupportInboxPlanTest(unittest.TestCase):
    def test_role_match_by_title_keyword(self):
        self.assertEqual(_match_inbox_role("ingest customer messages")["file"], "ingest.py")
        self.assertEqual(_match_inbox_role("classify issue")["file"], "classify.py")
        self.assertEqual(_match_inbox_role("draft response")["file"], "draft.py")
        self.assertEqual(_match_inbox_role("review approval")["file"], "review.py")
        self.assertEqual(_match_inbox_role("log resolution")["file"], "logging.py")
        self.assertIsNone(_match_inbox_role("render the dashboard"))   # unrelated → no role

    def test_plan_maps_each_step_to_its_own_file(self):
        plan = support_inbox_plan(INBOX_STEPS, "openfde_work/")
        paths = [p["path"] for p in plan]
        self.assertEqual(paths, [
            "openfde_work/support_inbox/__init__.py",
            "openfde_work/support_inbox/ingest.py",
            "openfde_work/support_inbox/classify.py",
            "openfde_work/support_inbox/draft.py",
            "openfde_work/support_inbox/review.py",
            "openfde_work/support_inbox/logging.py",
        ])
        # Every generated file is valid, parseable Python (deterministic, no network).
        for p in plan:
            ast.parse(p["content"])

    def test_plan_requires_enough_inbox_roles(self):
        self.assertIsNone(support_inbox_plan([{"title": "draw a chart"}], "openfde_work/"))
        self.assertIsNone(support_inbox_plan(INBOX_STEPS, ""))      # no workspace dir → no plan
        self.assertIsNone(support_inbox_plan(None, "openfde_work/"))

    def test_plan_is_deterministic(self):
        self.assertEqual(support_inbox_plan(INBOX_STEPS, "openfde_work/"),
                         support_inbox_plan(INBOX_STEPS, "openfde_work/"))

    def test_subset_of_three_still_scaffolds(self):
        plan = support_inbox_plan(INBOX_STEPS[:3], "openfde_work/")     # ingest, classify, draft
        self.assertEqual([p["path"].rsplit("/", 1)[-1] for p in plan],
                         ["__init__.py", "ingest.py", "classify.py", "draft.py"])


class EchoTransportTest(unittest.TestCase):
    def test_inbox_sketch_writes_per_step_saas_files(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = make_echo_transport(d, ["openfde_work/"], steps=INBOX_STEPS)
            writes = _writes(t)
        names = sorted(p.rsplit("/", 1)[-1] for p in writes)
        self.assertEqual(names, ["__init__.py", "classify.py", "draft.py",
                                 "ingest.py", "logging.py", "review.py"])
        self.assertIn("def ingest_messages", writes["openfde_work/support_inbox/ingest.py"])
        self.assertIn("def classify", writes["openfde_work/support_inbox/classify.py"])

    def test_non_inbox_sketch_falls_back_to_generic_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = make_echo_transport(d, ["openfde_work/"],
                                    steps=[{"title": "read the data"}, {"title": "train a model"}])
            writes = _writes(t)
        self.assertEqual(list(writes), ["openfde_work/intent_demo.py"])   # unchanged behavior

    def test_no_steps_keeps_generic_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = make_echo_transport(d, ["openfde_work/"])
            writes = _writes(t)
        self.assertEqual(list(writes), ["openfde_work/intent_demo.py"])


if __name__ == "__main__":
    unittest.main()
