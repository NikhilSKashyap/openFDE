"""
Tests for the Reproduce button (openfde.issue_repro) — the law under test is
REFUSAL: feature requests are never "reproduced", signal-free bug reports come
back insufficient with the missing pieces named, drafts that don't validate are
rejected, writes outside tests/ are refused, and a repro that PASSES on current
code reverts itself. The happy path runs REAL pytest in a throwaway repo with a
mocked drafting agent.
"""

import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from openfde.issue_repro import (
    draft_repro_test,
    find_test_home,
    locate_targets,
    reproduce_issue,
    triage_issue,
)

PYTEST = shutil.which("pytest") is not None

BUG_BODY = """In mypkg/calc.py, divide() crashes when b is None.

```
TypeError: unsupported operand type(s) for /: 'int' and 'NoneType'
```

Expected: a clear ValueError instead.
"""


class TriageTest(unittest.TestCase):
    def test_feature_request_is_not_a_bug(self):
        t = triage_issue("Add support for Qwen models", "Please add Qwen.", ["enhancement"])
        self.assertEqual(t["verdict"], "not_a_bug")

    def test_question_title_is_not_a_bug(self):
        t = triage_issue("How to use a local model?", "Is there a way?", [])
        self.assertEqual(t["verdict"], "not_a_bug")

    def test_bug_with_traceback_and_file_is_candidate(self):
        t = triage_issue("divide() crashes on None", BUG_BODY, ["bug"])
        self.assertEqual(t["verdict"], "candidate")
        self.assertIn({"file": "mypkg/calc.py"}, t["targets"])

    def test_vague_bug_is_insufficient(self):
        t = triage_issue("it crashes sometimes", "Randomly stops working.", [])
        self.assertEqual(t["verdict"], "insufficient")
        self.assertTrue(t["missing"])

    def test_error_name_without_location_is_insufficient_anchor_rule(self):
        t = triage_issue("Crash", "I get an error when running.", ["bug"])
        self.assertEqual(t["verdict"], "insufficient")
        self.assertIn("code location", t["missing"][0])

    def test_llm_may_downgrade(self):
        t = triage_issue("divide() crashes on None", BUG_BODY, ["bug"],
                         caller=lambda s, u: '{"kind": "question", "reproducible": false}')
        self.assertEqual(t["verdict"], "not_a_bug")

    def test_llm_says_not_reproducible_with_missing_list(self):
        t = triage_issue("divide() crashes on None", BUG_BODY, ["bug"],
                         caller=lambda s, u: ('{"kind": "bug", "reproducible": false, '
                                              '"missing": ["the input that triggers it"]}'))
        self.assertEqual(t["verdict"], "insufficient")
        self.assertEqual(t["missing"], ["the input that triggers it"])

    def test_broken_llm_keeps_deterministic_verdict(self):
        t = triage_issue("divide() crashes on None", BUG_BODY, ["bug"],
                         caller=lambda s, u: "not json")
        self.assertEqual(t["verdict"], "candidate")


def _repo():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "mypkg").mkdir()
    (root / "mypkg" / "__init__.py").write_text("")
    (root / "mypkg" / "calc.py").write_text(textwrap.dedent('''\
        def divide(a, b):
            return a / b
    '''))
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(textwrap.dedent('''\
        import pytest

        from mypkg.calc import divide


        def test_divide_basic():
            assert divide(6, 3) == 2
    '''))
    (root / "conftest.py").write_text("")
    return d, root


class LocateTest(unittest.TestCase):
    def test_named_file_found(self):
        d, root = _repo()
        with d:
            t = triage_issue("divide() crashes", "Bug in mypkg/calc.py\nTypeError: x", ["bug"])
            locs = locate_targets(root, t, "Bug in mypkg/calc.py")
            self.assertEqual(locs[0]["file"], "mypkg/calc.py")

    def test_stale_named_file_yields_nothing(self):
        d, root = _repo()
        with d:
            t = {"targets": [{"file": "mypkg/gone.py"}], "signals": {"error_names": []}}
            self.assertEqual(locate_targets(root, t, "see mypkg/gone.py"), [])

    def test_quoted_error_string_grepped(self):
        d, root = _repo()
        with d:
            (root / "mypkg" / "calc.py").write_text(
                'def divide(a, b):\n    raise ValueError("very specific failure text")\n')
            t = {"targets": [], "signals": {"error_names": []}}
            locs = locate_targets(root, t, 'I see "very specific failure text" raised')
            self.assertEqual(locs[0]["file"], "mypkg/calc.py")


class TestHomeTest(unittest.TestCase):
    def test_importing_test_file_wins(self):
        d, root = _repo()
        with d:
            home = find_test_home(root, "mypkg/calc.py")
            self.assertEqual(home["path"], "tests/test_calc.py")
            self.assertTrue(home["exists"])
            self.assertIn("def test_divide_basic", home["excerpt"])


class TestHomeDecoyTest(unittest.TestCase):
    def test_substring_decoy_does_not_steal_the_home(self):
        # Regression (live find on aisuite): tests/mcp/test_http_transport.py
        # imported `aisuite.mcp.client` and outscored tests/client/test_client.py
        # for target aisuite/client.py via a loose '.client import' substring.
        d, root = _repo()
        with d:
            (root / "mypkg" / "mcp").mkdir()
            (root / "tests" / "mcp").mkdir()
            (root / "tests" / "mcp" / "test_http_transport.py").write_text(
                "from mypkg.mcp.calc import Transport\n\n"
                "def test_transport():\n    pass\n")
            home = find_test_home(root, "mypkg/calc.py")
            self.assertEqual(home["path"], "tests/test_calc.py")


class DraftValidationTest(unittest.TestCase):
    HOME = {"path": "tests/test_calc.py", "excerpt": "import pytest\n"}

    def test_no_caller_returns_none(self):
        self.assertIsNone(draft_repro_test(None, "ctx", {"file": "x.py"}, self.HOME))

    def test_invalid_json_rejected(self):
        self.assertIsNone(draft_repro_test(lambda s, u: "nope", "ctx",
                                           {"file": "x.py"}, self.HOME))

    def test_unparseable_code_rejected(self):
        bad = json.dumps({"name": "test_x", "code": "def test_x(:\n  pass"})
        self.assertIsNone(draft_repro_test(lambda s, u: bad, "ctx",
                                           {"file": "x.py"}, self.HOME))

    def test_multiple_defs_rejected(self):
        bad = json.dumps({"name": "test_x",
                          "code": "def helper():\n    pass\n\ndef test_x():\n    pass"})
        self.assertIsNone(draft_repro_test(lambda s, u: bad, "ctx",
                                           {"file": "x.py"}, self.HOME))


REPRO_DRAFT = json.dumps({
    "name": "test_divide_none_raises_value_error",
    "code": ("def test_divide_none_raises_value_error():\n"
             "    with pytest.raises(ValueError):\n"
             "        divide(6, None)\n"),
})
CHECK = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


@unittest.skipUnless(PYTEST, "pytest not installed")
class ReproduceEndToEndTest(unittest.TestCase):
    def test_real_bug_reproduces_and_test_is_kept(self):
        d, root = _repo()
        with d:
            v = reproduce_issue(root, title="divide() crashes on None", body=BUG_BODY,
                                labels=["bug"], caller=lambda s, u: REPRO_DRAFT,
                                check_cmd=CHECK)
            self.assertEqual(v["verdict"], "reproduced")
            self.assertEqual(v["testFile"], "tests/test_calc.py")
            self.assertIn("test_divide_none_raises_value_error",
                          (root / "tests" / "test_calc.py").read_text())

    def test_fixed_bug_does_not_reproduce_and_reverts(self):
        d, root = _repo()
        with d:
            (root / "mypkg" / "calc.py").write_text(textwrap.dedent('''\
                def divide(a, b):
                    if b is None:
                        raise ValueError("b must not be None")
                    return a / b
            '''))
            before = (root / "tests" / "test_calc.py").read_text()
            v = reproduce_issue(root, title="divide() crashes on None", body=BUG_BODY,
                                labels=["bug"], caller=lambda s, u: REPRO_DRAFT,
                                check_cmd=CHECK)
            self.assertEqual(v["verdict"], "not_reproduced")
            self.assertEqual((root / "tests" / "test_calc.py").read_text(), before)

    def test_feature_request_writes_nothing(self):
        d, root = _repo()
        with d:
            before = (root / "tests" / "test_calc.py").read_text()
            v = reproduce_issue(root, title="Add support for Qwen", body="please",
                                labels=["enhancement"], caller=lambda s, u: REPRO_DRAFT,
                                check_cmd=CHECK)
            self.assertEqual(v["verdict"], "not_a_bug")
            self.assertEqual((root / "tests" / "test_calc.py").read_text(), before)

    def test_before_write_fires_once_and_episode_rides_verdict(self):
        d, root = _repo()
        with d:
            calls = []
            v = reproduce_issue(root, title="divide() crashes on None", body=BUG_BODY,
                                labels=["bug"], caller=lambda s, u: REPRO_DRAFT,
                                check_cmd=CHECK,
                                before_write=lambda: calls.append(1) or "episode_abc")
            self.assertEqual(v["verdict"], "reproduced")
            self.assertEqual(v["episodeId"], "episode_abc")
            self.assertEqual(calls, [1])               # exactly once, write-gated
            self.assertIn("mypkg/calc.py", v["links"])  # canvas ties ride along

    def test_refusals_never_fire_the_hook_but_carry_links(self):
        d, root = _repo()
        with d:
            calls = []
            v = reproduce_issue(root, title="Add support for Qwen", body="please",
                                labels=["enhancement"], caller=lambda s, u: REPRO_DRAFT,
                                check_cmd=CHECK, before_write=lambda: calls.append(1))
            self.assertEqual(v["verdict"], "not_a_bug")
            self.assertEqual(calls, [])
            self.assertIn("links", v)

    def test_no_agent_is_an_honest_verdict(self):
        d, root = _repo()
        with d:
            v = reproduce_issue(root, title="divide() crashes on None", body=BUG_BODY,
                                labels=["bug"], caller=None, check_cmd=CHECK)
            self.assertEqual(v["verdict"], "no_agent")

    def test_non_pytest_runner_synthesizes_pytest_check(self):
        # A bare / non-pytest repo no longer refuses: the repro we draft IS pytest,
        # so reproduce synthesizes a pytest check AND persists it as
        # .openfde/verify.json (so the repo's own "Run checks" then runs the test).
        d, root = _repo()
        with d:
            v = reproduce_issue(root, title="divide() crashes on None", body=BUG_BODY,
                                labels=["bug"], caller=lambda s, u: REPRO_DRAFT,
                                check_cmd=["python3", "-m", "unittest"])
            self.assertEqual(v["verdict"], "reproduced")
            cfg = Path(root) / ".openfde" / "verify.json"
            self.assertTrue(cfg.exists(), "should pin a pytest check on a bare repo")
            self.assertIn("pytest", json.dumps(json.loads(cfg.read_text())))


if __name__ == "__main__":
    unittest.main()


class ReportScrubTest(unittest.TestCase):
    def test_known_repo_strings_are_scrubbed_longest_first(self):
        from openfde.issue_repro import report_replacements, scrub_report
        repls = report_replacements(
            ["tests/client/test_client.py", "aisuite/client.py"],
            file="tests/client/test_client.py",
            test="test_none_model[2]", repo_name="aisuite")
        drafted = ("The runner edited aisuite/client.py while the hatch sat on "
                   "tests/client/test_client.py; test_none_model[2] (also "
                   "test_none_model) failed in client.py inside aisuite.")
        out = scrub_report(drafted, repls)
        self.assertNotIn("aisuite/client.py", out)
        self.assertNotIn("test_client.py", out)
        self.assertNotIn("test_none_model", out)
        self.assertIn("<source-file>", out)
        self.assertIn("<test-file>", out)
        self.assertIn("<failing-test>", out)
        self.assertNotIn("aisuite.", out)              # repo name scrubbed too

    def test_cost_never_survives_the_scrub(self):
        from openfde.issue_repro import scrub_report
        out = scrub_report('reason verbatim: "no diff. (cost $0.04)" end', {})
        self.assertNotIn("cost", out)
        self.assertIn('"no diff."', out)

    def test_openfde_module_names_survive(self):
        from openfde.issue_repro import report_replacements, scrub_report
        repls = report_replacements(["pkg/x.py"], test="test_a")
        out = scrub_report("Look at post_hatch_run and failure_flow.chain_files.", repls)
        self.assertIn("post_hatch_run", out)           # OUR modules are the point
        self.assertIn("failure_flow.chain_files", out)

    def test_deterministic_report_is_repo_clean_by_construction(self):
        from openfde.issue_repro import deterministic_report
        title, body = deterministic_report(
            {"status": "failed", "error": "Claude Code ran but produced no "
             "in-scope changes — no diff. (cost $0.03)",
             "scope": ["tests/client/test_client.py", "aisuite/client.py"],
             "openfdeVersion": "abc1234", "source": "Senior Dev · Claude Code local"},
            {"file": "tests/client/test_client.py", "test": "test_none"})
        self.assertNotIn("test_client.py", title + body)
        self.assertNotIn("(cost", title + body)
        self.assertIn("2 files (1 test, 1 source)", body)
        self.assertIn("abc1234", body)
        self.assertIn("Suspected area", body)
