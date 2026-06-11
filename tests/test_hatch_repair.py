"""
Tests for repair-hatch artifacts v3 — failure-fingerprinted artifacts on the
OWNING episode (never a new one): the LLM runs once per failure meaning
(fingerprint), storing the same identity replaces (explicit regenerate), and
the deterministic failure FLOW derives from AST + receipts with the LLM pass
allowed to touch labels only. Plus capture-skip parity (an OpenFDE runner
prompt must not read as a human prompt on the Claude path).
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from openfde import prompt_capture as pc
from openfde.failure_flow import build_failure_flow, failure_fingerprint, humanize_flow
from openfde.persistence import Persistence

FP_ARGS = dict(episode_id="e1", check_id="unit", file="tests/t.py", line=27,
               func="T.test_a", test="test_a", failure_msg="True is not false",
               code="def test_a(): ...")


class FailureFingerprintTest(unittest.TestCase):
    def test_stable_for_same_meaning(self):
        self.assertEqual(failure_fingerprint(**FP_ARGS), failure_fingerprint(**FP_ARGS))

    def test_changes_when_meaning_changes(self):
        fp = failure_fingerprint(**FP_ARGS)
        self.assertNotEqual(fp, failure_fingerprint(**{**FP_ARGS, "line": 28}))
        self.assertNotEqual(fp, failure_fingerprint(**{**FP_ARGS, "failure_msg": "boom"}))
        self.assertNotEqual(fp, failure_fingerprint(**{**FP_ARGS, "code": "def test_a(): pass"}))


def _repo():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "lock.py").write_text(textwrap.dedent('''\
        def acquire(d, pid=None):
            """Create the lock file and return its path."""
            return d + "/.lock"
    '''))
    (root / "test_fixture.py").write_text(textwrap.dedent('''\
        import unittest
        from pathlib import Path

        from pkg.lock import acquire


        class T(unittest.TestCase):
            def test_a(self):
                lock = acquire("d", pid=1)
                self.assertFalse(Path(lock).exists())
    '''))
    return d, root


class BuildFailureFlowTest(unittest.TestCase):
    def test_deterministic_flow_from_ast(self):
        d, root = _repo()
        with d:
            flow = build_failure_flow(root, file="test_fixture.py", line=10,
                                      test="test_a", func="T.test_a")
            ids = {n["id"] for n in flow["nodes"]}
            self.assertIn("test_a", ids)            # the failing test, anchored
            self.assertIn("acquire", ids)           # the in-repo callee, resolved
            acq = next(n for n in flow["nodes"] if n["id"] == "acquire")
            self.assertEqual(acq["file"], "pkg/lock.py")
            self.assertEqual(acq["line"], 1)
            call = next(e for e in flow["edges"] if e["to"] == "acquire")
            self.assertEqual(call["from"], "test_a")
            self.assertIn("calls acquire(", call["label"])
            self.assertEqual(call["confidence"], "medium")   # resolved via import
            fails = [e for e in flow["edges"] if e["label"].startswith("fails here")]
            self.assertTrue(fails)                  # the failing line is the edge
            self.assertTrue(all(f["confidence"] == "high" for f in fails))
            self.assertIn("test_a", flow["summary"])
            self.assertIn("line 10", flow["summary"])

    def test_unparseable_file_degrades_to_minimal_flow(self):
        d, root = _repo()
        with d:
            flow = build_failure_flow(root, file="nope.py", line=5, test="test_x")
            self.assertEqual(len(flow["nodes"]), 1)
            self.assertEqual(flow["edges"], [])
            self.assertIn("test_x", flow["summary"])


class HumanizeFlowTest(unittest.TestCase):
    FLOW = {"summary": "s", "nodes": [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}],
            "edges": [{"from": "a", "to": "b", "label": "calls b() — line 3",
                       "confidence": "high"}]}

    def test_no_caller_keeps_deterministic(self):
        flow, used = humanize_flow(self.FLOW, None)
        self.assertFalse(used)
        self.assertEqual(flow, self.FLOW)

    def test_valid_llm_rewrites_labels_only(self):
        def caller(_s, _u):
            return ('{"summary": "b is created by a.", "edges": '
                    '[{"from": "a", "to": "b", "label": "creates the lock file", '
                    '"confidence": "high"}]}')
        flow, used = humanize_flow(self.FLOW, caller)
        self.assertTrue(used)
        self.assertEqual(flow["edges"][0]["label"], "creates the lock file")
        self.assertEqual(flow["summary"], "b is created by a.")
        self.assertEqual(flow["nodes"], self.FLOW["nodes"])     # structure untouched

    def test_mismatched_or_broken_llm_falls_back(self):
        flow, used = humanize_flow(self.FLOW, lambda s, u: "not json at all")
        self.assertFalse(used)
        self.assertEqual(flow, self.FLOW)
        flow, used = humanize_flow(
            self.FLOW, lambda s, u: '{"edges": [{"from": "x", "to": "b", "label": "l"}]}')
        self.assertFalse(used)                       # endpoints changed → rejected


def _store():
    d = tempfile.TemporaryDirectory()
    p = Persistence(Path(d.name) / ".openfde")
    p.upsert_episode({"episodeId": "e1", "prompt": "build", "status": "reviewing"})
    return d, p


ART = {"kind": "repair_prompt", "fingerprint": "fp1", "checkId": "unit",
       "file": "tests/t.py", "line": 27, "function": "T.test_a", "test": "test_a",
       "source": "Senior Dev · Claude Code local", "text": "fix it"}


class RepairArtifactStoreTest(unittest.TestCase):
    def test_create_assigns_identity(self):
        d, p = _store()
        with d:
            a = p.upsert_repair_artifact("e1", ART)
            self.assertTrue(a["id"].startswith("repair_"))
            self.assertTrue(a["createdAt"] and a["updatedAt"])
            self.assertEqual(len(p.get_repair_artifacts("e1")), 1)

    def test_same_kind_fp_replaces_keeps_identity(self):
        d, p = _store()
        with d:
            a1 = p.upsert_repair_artifact("e1", ART)
            a2 = p.upsert_repair_artifact("e1", {**ART, "text": "fix it BETTER"})
            self.assertEqual(a1["id"], a2["id"])
            self.assertEqual(a1["createdAt"], a2["createdAt"])
            arts = p.get_repair_artifacts("e1")
            self.assertEqual(len(arts), 1)           # replaced, not appended
            self.assertEqual(arts[0]["text"], "fix it BETTER")

    def test_kinds_and_fingerprints_are_distinct_entries(self):
        d, p = _store()
        with d:
            p.upsert_repair_artifact("e1", ART)
            p.upsert_repair_artifact("e1", {**ART, "kind": "failure_explanation"})
            p.upsert_repair_artifact("e1", {**ART, "fingerprint": "fp2"})
            self.assertEqual(len(p.get_repair_artifacts("e1")), 3)
            self.assertEqual(len(p.get_repair_artifacts("e1", "fp1")), 2)

    def test_cap_drops_oldest(self):
        d, p = _store()
        with d:
            for i in range(30):
                p.upsert_repair_artifact("e1", {**ART, "fingerprint": f"fp{i}"}, cap=24)
            arts = p.get_repair_artifacts("e1")
            self.assertEqual(len(arts), 24)
            self.assertEqual(arts[-1]["fingerprint"], "fp29")

    def test_unknown_episode_returns_none_creates_nothing(self):
        d, p = _store()
        with d:
            self.assertIsNone(p.upsert_repair_artifact("nope", ART))
            self.assertEqual(len(p.load_episodes()), 1)   # never a new episode — the law


class RunnerPromptCaptureSkipTest(unittest.TestCase):
    def _user(self, text):
        return {"type": "user", "uuid": "u9", "sessionId": "s",
                "message": {"role": "user", "content": text}}

    def test_runner_directive_is_not_a_human_prompt(self):
        txt = ("IMPORTANT — OpenFDE owns version control. You only EDIT files. "
               "Do NOT run git. Repair task — scoped to a single failing check…")
        self.assertFalse(pc.is_human_prompt(self._user(txt)))

    def test_real_prompt_still_accepted(self):
        self.assertTrue(pc.is_human_prompt(self._user("fix the login redirect")))


if __name__ == "__main__":
    unittest.main()
