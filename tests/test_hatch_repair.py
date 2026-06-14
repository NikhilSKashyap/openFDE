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
from openfde.failure_flow import build_failure_flow, chain_files, failure_fingerprint, humanize_flow
from openfde.persistence import Persistence

FP_ARGS = dict(episode_id="e1", check_id="unit", file="tests/t.py", line=27,
               func="T.test_a", test="test_a", failure_msg="True is not false",
               code="def test_a(): ...")


class FailureFingerprintTest(unittest.TestCase):
    def test_stable_across_check_reruns(self):
        # Same failure, two runs: timings and counts differ — fingerprint must not.
        tail1 = ("tests/client/test_client.py:153: in test_none\n"
                 "E   TypeError: argument of type 'NoneType' is not iterable\n"
                 "FAILED tests/client/test_client.py::test_none - TypeError\n"
                 "3 failed, 328 passed, 1 skipped in 15.53s\n")
        tail2 = tail1.replace("15.53s", "14.83s").replace("3 failed, 328 passed",
                                                          "3 failed, 329 passed")
        a = failure_fingerprint(**{**FP_ARGS, "failure_msg": tail1})
        b = failure_fingerprint(**{**FP_ARGS, "failure_msg": tail2})
        self.assertEqual(a, b)
        c = failure_fingerprint(**{**FP_ARGS, "failure_msg":
                                   tail1.replace("TypeError", "ValueError")})
        self.assertNotEqual(a, c)                  # a DIFFERENT error still re-keys


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
            self.assertTrue(next(n for n in flow["nodes"] if n["id"] == "test_a").get("fail"))
            self.assertIn("test_a", flow["summary"])
            self.assertIn("line 10", flow["summary"])

    def test_traceback_chain_is_the_spine(self):
        # The terminal trace-back, drawn forward: caller → ✕ failing function.
        d, root = _repo()
        with d:
            tail = ("test_fixture.py:9: in test_a\n"
                    "    acquire('d', pid=1)\n"
                    "pkg/lock.py:3: in acquire\n"
                    "    return d + '/.lock'\n")
            flow = build_failure_flow(root, file="pkg/lock.py", line=3,
                                      test="test_a", output_tail=tail)
            ids = [n["id"] for n in flow["nodes"]]
            self.assertEqual(ids[:2], ["test_a", "acquire"])     # outermost first
            fail = next(n for n in flow["nodes"] if n.get("fail"))
            self.assertEqual(fail["id"], "acquire")              # ✕ on the deepest frame
            self.assertEqual(fail["file"], "pkg/lock.py")
            edge = flow["edges"][0]
            self.assertEqual((edge["from"], edge["to"]), ("test_a", "acquire"))
            self.assertIn("calls acquire() — line 9", edge["label"])
            self.assertIn("test_a → acquire", flow["summary"])

    def test_chain_files_spans_both_contract_legs(self):
        d, root = _repo()
        with d:
            tail = ("test_fixture.py:9: in test_a\n"
                    "pkg/lock.py:3: in acquire\n")
            files = chain_files(root, tail, "test_fixture.py")
            self.assertEqual(files, ["test_fixture.py", "pkg/lock.py"])
            # No frames → the hatch file alone; never empty, never repo-wide.
            self.assertEqual(chain_files(root, "", "pkg/lock.py"), ["pkg/lock.py"])

    def test_primary_path_is_a_clean_causal_sentence(self):
        # The distilled path drops the test's assertion re-entry + AST-call noise:
        # source function → failing function, last node = failure, one edge/hop.
        d, root = _repo()
        with d:
            tail = ("test_fixture.py:9: in test_a\n"
                    "    acquire('d', pid=1)\n"
                    "pkg/lock.py:3: in acquire\n"
                    "    return d + '/.lock'\n"
                    "test_fixture.py:10: in test_a\n")   # the test re-enters (assertion)
            flow = build_failure_flow(root, file="pkg/lock.py", line=3,
                                      test="test_a", output_tail=tail)
            pp = flow["primaryPath"]
            self.assertEqual([n["function"] for n in pp], ["test_a", "acquire"])  # re-entry collapsed
            self.assertEqual(pp[0]["role"], "source")
            self.assertEqual(pp[-1]["role"], "failure")           # acquire is the terminus
            self.assertEqual(pp[-1]["file"], "pkg/lock.py")
            pe = flow["primaryEdges"]
            self.assertEqual(len(pe), 1)                          # one clean hop
            self.assertEqual((pe[0]["from"], pe[0]["to"]), (pp[0]["id"], pp[1]["id"]))
            self.assertIn("acquire", pe[0]["label"])
            self.assertLessEqual(len(pe[0]["label"].split()), 3)  # short phrase

    def test_cross_module_nodes_carry_file_identity(self):
        # Failure flow is a PATH: every node the frontend must resolve to a box
        # carries file identity (test leg AND product leg), so the lens can light
        # boxes across modules — not only the crash site.
        d, root = _repo()
        with d:
            tail = ("test_fixture.py:9: in test_a\n"
                    "    acquire('d', pid=1)\n"
                    "pkg/lock.py:3: in acquire\n"
                    "    return d + '/.lock'\n")
            flow = build_failure_flow(root, file="pkg/lock.py", line=3,
                                      test="test_a", output_tail=tail)
            by_id = {n["id"]: n for n in flow["nodes"]}
            self.assertEqual(by_id["test_a"]["file"], "test_fixture.py")   # test leg
            self.assertEqual(by_id["acquire"]["file"], "pkg/lock.py")      # product leg
            self.assertTrue(by_id["acquire"].get("fail"))                  # crash site marked
            # Two distinct files on the path → the lens lights two modules.
            files = {n.get("file") for n in flow["nodes"] if n.get("file")}
            self.assertIn("test_fixture.py", files)
            self.assertIn("pkg/lock.py", files)

    def test_frames_outside_repo_are_skipped(self):
        d, root = _repo()
        with d:
            tail = ("/opt/python/site-packages/_pytest/python.py:159: in pytest_pyfunc_call\n"
                    "test_fixture.py:10: in test_a\n")
            flow = build_failure_flow(root, file="test_fixture.py", line=10,
                                      test="test_a", output_tail=tail)
            ids = {n["id"] for n in flow["nodes"]}
            self.assertNotIn("pytest_pyfunc_call", ids)

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


class MoveTasksForEpisodeTest(unittest.TestCase):
    def test_cards_follow_their_episode(self):
        d, p = _store()
        with d:
            p.save_tasks([{"id": "t1", "episodeId": "e1", "column": "todo"},
                          {"id": "t2", "episodeId": "other", "column": "todo"}])
            n = p.move_tasks_for_episode("e1", "testing", "failed")
            self.assertEqual(n, 1)
            t1 = next(t for t in p.load_tasks() if t["id"] == "t1")
            self.assertEqual((t1["column"], t1["verificationStatus"]),
                             ("testing", "failed"))
            t2 = next(t for t in p.load_tasks() if t["id"] == "t2")
            self.assertEqual(t2["column"], "todo")     # other episodes untouched

    def test_from_columns_is_monotonic(self):
        d, p = _store()
        with d:
            p.save_tasks([{"id": "t1", "episodeId": "e1", "column": "done"}])
            # passed evidence must not demote a Done card…
            self.assertEqual(p.move_tasks_for_episode(
                "e1", "testing", "passed", from_columns=("todo", "doing")), 0)
            self.assertEqual(p.load_tasks()[0]["column"], "done")
            # …but RED evidence demotes honestly (no from_columns gate).
            self.assertEqual(p.move_tasks_for_episode("e1", "testing", "failed"), 1)
            self.assertEqual(p.load_tasks()[0]["column"], "testing")

    def test_reopen_episode_only_when_landed(self):
        d, p = _store()
        with d:
            self.assertIsNone(p.reopen_episode("e1"))          # reviewing → no-op
            ep = p.get_episode("e1")
            ep["status"] = "landed"
            p.upsert_episode(ep)
            re = p.reopen_episode("e1")
            self.assertEqual(re["status"], "reviewing")        # the fix of a fix
            self.assertEqual(p.load_episodes()[0]["episodeId"], "e1")  # back to front

    def test_no_match_writes_nothing(self):
        d, p = _store()
        with d:
            p.save_tasks([{"id": "t1", "column": "todo"}])
            self.assertEqual(p.move_tasks_for_episode("ghost", "done"), 0)
