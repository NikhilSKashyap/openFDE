"""
Tests for openfde.verify — Verify Gate Evidence v1: deterministic check discovery,
evidence shape for passing/failing checks, output-tail capping, and the overall
passed / failed / skipped verdict. No real subprocesses — the runner is injected.
"""

import json
import tempfile
import unittest
from pathlib import Path

from openfde.verify import (
    _TAIL_CAP,
    _parse_via_packs,
    discover_checks,
    parse_failure_locations,
    run_check,
    run_verification,
)
from openfde.language_packs.python_pack import resolve_pytest_cmd


class FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def fake_runner(stdout="", rc=0, stderr=""):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw.get("cwd")))
        return FakeProc(stdout=stdout, returncode=rc, stderr=stderr)

    run.calls = calls
    return run


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_repo_discovers_nothing(self):
        self.assertEqual(discover_checks(self.root), [])

    def test_python_tests_discovered(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("# t\n")
        checks = discover_checks(self.root)
        self.assertEqual([c["id"] for c in checks], ["unit-tests"])
        self.assertEqual(checks[0]["command"][:3], ["python3", "-m", "unittest"])
        self.assertTrue(checks[0]["required"])

    def test_tests_dir_without_test_files_ignored(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "helper.py").write_text("# not a test\n")
        self.assertEqual(discover_checks(self.root), [])

    def test_frontend_lint_discovered(self):
        (self.root / "frontend").mkdir()
        (self.root / "frontend" / "package.json").write_text(
            json.dumps({"scripts": {"lint": "eslint ."}}))
        checks = discover_checks(self.root)
        self.assertEqual([c["id"] for c in checks], ["frontend-lint"])
        self.assertEqual(checks[0]["cwd"], "frontend")

    def test_config_file_overrides_heuristics(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("# t\n")     # would be discovered…
        (self.root / ".openfde").mkdir()
        (self.root / ".openfde" / "verify.json").write_text(json.dumps([
            {"id": "smoke", "label": "Smoke", "command": ["true"], "required": False},
            {"command": "not-a-list"},                               # malformed → skipped
        ]))
        checks = discover_checks(self.root)                          # …but config wins
        self.assertEqual([c["id"] for c in checks], ["smoke"])
        self.assertFalse(checks[0]["required"])

    def test_invalid_config_falls_back_to_heuristics(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("# t\n")
        (self.root / ".openfde").mkdir()
        (self.root / ".openfde" / "verify.json").write_text("{nope")
        self.assertEqual([c["id"] for c in discover_checks(self.root)], ["unit-tests"])


CHECK = {"id": "unit-tests", "label": "Unit tests",
         "command": ["python3", "-m", "unittest"], "cwd": "", "required": True}


class RunCheckTest(unittest.TestCase):
    def test_passing_evidence_shape(self):
        run = fake_runner(stdout="Ran 155 tests in 11.5s\n\nOK\n", rc=0)
        ev = run_check("/tmp", CHECK, runner=run)
        self.assertEqual(ev["status"], "passed")
        self.assertEqual(ev["exitCode"], 0)
        self.assertEqual(ev["summary"], "Ran 155 tests in 11.5s — OK")   # terse OK folded in
        self.assertEqual(ev["command"], "python3 -m unittest")
        for key in ("startedAt", "finishedAt", "durationMs", "outputTail", "label"):
            self.assertIn(key, ev)

    def test_failing_evidence_shape(self):
        run = fake_runner(stdout="", rc=1,
                          stderr="FAIL: test_x\n\nFAILED (failures=2)\n")
        ev = run_check("/tmp", CHECK, runner=run)
        self.assertEqual(ev["status"], "failed")
        self.assertEqual(ev["exitCode"], 1)
        self.assertEqual(ev["summary"], "FAILED (failures=2)")
        self.assertIn("FAIL: test_x", ev["outputTail"])

    def test_output_tail_truncated(self):
        run = fake_runner(stdout="x" * (_TAIL_CAP * 3), rc=0)
        ev = run_check("/tmp", CHECK, runner=run)
        self.assertLessEqual(len(ev["outputTail"]), _TAIL_CAP)
        self.assertTrue(ev["outputTail"].startswith("…"))           # tail keeps the END

    def test_missing_command_records_failure(self):
        def boom(cmd, **kw):
            raise FileNotFoundError(cmd[0])
        ev = run_check("/tmp", CHECK, runner=boom)
        self.assertEqual(ev["status"], "failed")
        self.assertIn("command not found", ev["summary"])


class RunVerificationTest(unittest.TestCase):
    def test_all_required_pass(self):
        out = run_verification("/tmp", checks=[CHECK], runner=fake_runner("OK line", rc=0))
        self.assertEqual(out["status"], "passed")
        self.assertEqual(len(out["checks"]), 1)
        self.assertIn("ranAt", out)

    def test_required_failure_fails_overall(self):
        out = run_verification("/tmp", checks=[CHECK], runner=fake_runner("boom", rc=2))
        self.assertEqual(out["status"], "failed")

    def test_optional_failure_still_passes(self):
        optional = {**CHECK, "id": "advisory", "required": False}
        out = run_verification("/tmp", checks=[optional], runner=fake_runner("meh", rc=1))
        self.assertEqual(out["status"], "passed")
        self.assertEqual(out["checks"][0]["status"], "failed")      # failure still recorded

    def test_no_checks_is_explicit_skipped(self):
        out = run_verification("/tmp", checks=[])
        self.assertEqual(out["status"], "skipped")
        self.assertIn("not configured", out["note"])
        self.assertEqual(out["checks"], [])


if __name__ == "__main__":
    unittest.main()


# ── Pytest failure parsing — fixtures are VERBATIM pytest 8.3.3 output captured
# from a throwaway repo (src/calc.py + tests/test_calc.py), one per --tb style. ──

_PYTEST_SHORT_TB = """FFF.F.                                                                   [100%]
=================================== FAILURES ===================================
______________________________ test_simple_assert ______________________________
tests/test_calc.py:5: in test_simple_assert
    assert divide(6, 3) == 3
E   assert 2.0 == 3
E    +  where 2.0 = divide(6, 3)
___________________________ TestGroup.test_in_class ____________________________
tests/test_calc.py:10: in test_in_class
    assert result == 4
E   assert 5.0 == 4
_______________________________ test_via_helper ________________________________
tests/test_calc.py:13: in test_via_helper
    deep_helper("z")
src/calc.py:5: in deep_helper
    raise ValueError("boom %s" % x)
E   ValueError: boom z
________________________________ test_param[2] _________________________________
tests/test_calc.py:17: in test_param
    assert v == 1
E   assert 2 == 1
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_simple_assert - assert 2.0 == 3
FAILED tests/test_calc.py::TestGroup::test_in_class - assert 5.0 == 4
FAILED tests/test_calc.py::test_via_helper - ValueError: boom z
FAILED tests/test_calc.py::test_param[2] - assert 2 == 1
4 failed, 2 passed in 0.04s
"""

_PYTEST_LONG_TB = """=================================== FAILURES ===================================
______________________________ test_simple_assert ______________________________

    def test_simple_assert():
>       assert divide(6, 3) == 3
E       assert 2.0 == 3
E        +  where 2.0 = divide(6, 3)

tests/test_calc.py:5: AssertionError
_______________________________ test_via_helper ________________________________

    def test_via_helper():
>       deep_helper("z")

tests/test_calc.py:13: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

x = 'z'

    def deep_helper(x):
>       raise ValueError("boom %s" % x)
E       ValueError: boom z

src/calc.py:5: ValueError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_simple_assert - assert 2.0 == 3
FAILED tests/test_calc.py::test_via_helper - ValueError: boom z
========================= 2 failed in 0.06s ==============================
"""

_PYTEST_COLLECT_ERR = """==================================== ERRORS ====================================
____________________ ERROR collecting tests/test_broken.py _____________________
ImportError while importing test module '/private/tmp/pytraw/tests/test_broken.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/Users/nikhilkashyap/miniconda3/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/test_broken.py:1: in <module>
    import nonexistent_pkg_xyz
E   ModuleNotFoundError: No module named 'nonexistent_pkg_xyz'
=========================== short test summary info ============================
ERROR tests/test_broken.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.06s
"""

_PYTEST_NATIVE_TB = """=================================== FAILURES ===================================
_______________________________ test_via_helper ________________________________
Traceback (most recent call last):
  File "{site}/_pytest/python.py", line 159, in pytest_pyfunc_call
    result = testfunction(**testargs)
  File "{root}/tests/test_calc.py", line 13, in test_via_helper
    deep_helper("z")
  File "{root}/src/calc.py", line 5, in deep_helper
    raise ValueError("boom %s" % x)
ValueError: boom z
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_via_helper - ValueError: boom z
1 failed in 0.04s
"""


_PYTEST_LINE_TB = """FFF.F.                                                                   [100%]
=================================== FAILURES ===================================
{root}/tests/test_calc.py:5: assert 2.0 == 3
{root}/tests/test_calc.py:10: assert 5.0 == 4
{root}/src/calc.py:5: ValueError: boom z
{root}/tests/test_calc.py:17: assert 2 == 1
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_simple_assert - assert 2.0 == 3
FAILED tests/test_calc.py::TestGroup::test_in_class - assert 5.0 == 4
FAILED tests/test_calc.py::test_via_helper - ValueError: boom z
FAILED tests/test_calc.py::test_param[2] - assert 2 == 1
4 failed, 2 passed in 0.04s
"""


class PytestFailureParseTest(unittest.TestCase):
    def test_short_tb_deepest_in_repo_frame_wins(self):
        fails = parse_failure_locations(_PYTEST_SHORT_TB, "/repo")
        self.assertEqual(len(fails), 4)
        self.assertEqual(fails[0], {"test": "test_simple_assert",
                                    "file": "tests/test_calc.py", "line": 5,
                                    "func": "test_simple_assert"})
        self.assertEqual(fails[1]["test"], "test_in_class")     # class stripped
        # The helper failure: the DEEPEST in-repo frame (the raise site) wins.
        self.assertEqual(fails[2], {"test": "test_via_helper",
                                    "file": "src/calc.py", "line": 5,
                                    "func": "deep_helper"})
        self.assertEqual(fails[3]["test"], "test_param[2]")     # param id kept
        self.assertEqual(fails[3]["line"], 17)

    def test_long_tb_recovers_func_from_def_lines(self):
        fails = parse_failure_locations(_PYTEST_LONG_TB, "/repo")
        self.assertEqual(len(fails), 2)
        self.assertEqual(fails[0]["func"], "test_simple_assert")
        # No "in func" in long tb — recovered from the "def deep_helper(" line.
        self.assertEqual(fails[1], {"test": "test_via_helper",
                                    "file": "src/calc.py", "line": 5,
                                    "func": "deep_helper"})

    def test_native_tb_filters_site_packages(self):
        with tempfile.TemporaryDirectory() as d:
            out = _PYTEST_NATIVE_TB.format(root=str(Path(d).resolve()),
                                           site="/opt/python/site-packages")
            fails = parse_failure_locations(out, d)
            self.assertEqual(len(fails), 1)
            self.assertEqual(fails[0], {"test": "test_via_helper",
                                        "file": "src/calc.py", "line": 5,
                                        "func": "deep_helper"})

    def test_line_tb_pairs_locations_with_summary_names(self):
        with tempfile.TemporaryDirectory() as d:
            out = _PYTEST_LINE_TB.format(root=str(Path(d).resolve()))
            fails = parse_failure_locations(out, d)
        self.assertEqual(len(fails), 4)
        self.assertEqual(fails[0]["test"], "test_simple_assert")
        self.assertEqual(fails[0]["line"], 5)
        # The raise-site location pairs with ITS summary entry, not the test file.
        self.assertEqual(fails[2], {"test": "test_via_helper",
                                    "file": "src/calc.py", "line": 5,
                                    "func": "test_via_helper"})
        self.assertEqual(fails[3]["test"], "test_param[2]")
        self.assertEqual(fails[3]["func"], "test_param")   # params stripped

    def test_collection_error_points_at_the_import(self):
        fails = parse_failure_locations(_PYTEST_COLLECT_ERR, "/repo")
        self.assertEqual(fails, [{"test": "tests/test_broken.py",
                                  "file": "tests/test_broken.py", "line": 1,
                                  "func": "<module>"}])

    def test_unittest_output_still_takes_the_unittest_path(self):
        # The trailing "FAILED (failures=1)" is unittest's REAL closing line — it
        # must not be mistaken for a pytest summary entry (no .py path on it).
        out = ('FAIL: test_x (tests.test_m.T.test_x)\n'
               'Traceback (most recent call last):\n'
               '  File "/repo/tests/test_m.py", line 9, in test_x\n'
               '    self.assertTrue(False)\n'
               'AssertionError: True is not false\n'
               '----------------------------------------------------------------------\n'
               'Ran 5 tests in 0.1s\n'
               '\n'
               'FAILED (failures=1)\n')
        fails = parse_failure_locations(out, "/repo")
        self.assertEqual(fails, [{"test": "test_x", "file": "tests/test_m.py",
                                  "line": 9, "func": "test_x"}])


class PytestDiscoveryTest(unittest.TestCase):
    def _disc(self, build):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            build(root)
            return discover_checks(root)

    def test_conftest_marks_pytest(self):
        def build(root):
            (root / "conftest.py").write_text("")
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("def test_a(): pass\n")
        checks = self._disc(build)
        # The runner prefix is resolved per-environment (pytest CLI vs python3 -m
        # pytest), so assert "this is a pytest command with OpenFDE's flags" rather
        # than a fixed `python3 -m pytest` prefix.
        self.assertIn("pytest", " ".join(checks[0]["command"]))
        self.assertIn("--tb=short", checks[0]["command"])
        self.assertIn("no:cacheprovider", checks[0]["command"])

    def test_pyproject_pytest_section_marks_pytest(self):
        def build(root):
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
        checks = self._disc(build)
        self.assertIn("pytest", " ".join(checks[0]["command"]))
        self.assertIn("--tb=short", checks[0]["command"])
        self.assertIn("no:cacheprovider", checks[0]["command"])

    def test_pyproject_without_pytest_section_stays_unittest(self):
        def build(root):
            (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("def test_a(): pass\n")
        checks = self._disc(build)
        self.assertEqual(checks[0]["command"][:4],
                         ["python3", "-m", "unittest", "discover"])


class RecheckSingleTestTest(unittest.TestCase):
    def _repo(self, body):
        import tempfile
        from pathlib import Path
        d = tempfile.TemporaryDirectory()
        root = Path(d.name)
        (root / "tests").mkdir()
        (root / "tests" / "test_one.py").write_text(body)
        (root / "conftest.py").write_text("")
        return d, root

    # Resolve the working pytest runner for THIS environment (pytest CLI vs
    # python3 -m pytest) — a fixed `python3 -m pytest` makes recheck fail on a host
    # where the interpreter can't import pytest even though the CLI is on PATH.
    CMD = resolve_pytest_cmd()

    def test_failing_then_fixed(self):
        from openfde.verify import recheck_single_test
        d, root = self._repo("def test_alpha():\n    assert 1 == 2\n")
        with d:
            self.assertEqual(recheck_single_test(root, self.CMD, "test_alpha")["status"],
                             "failed")
            import os
            import time
            fixed = root / "tests" / "test_one.py"
            fixed.write_text("def test_alpha():\n    assert 1 == 1\n")
            # Same-second rewrites hide from pytest's mtime-keyed pyc cache —
            # real repairs take minutes; the test must bump the clock itself.
            t = time.time() + 3
            os.utime(fixed, (t, t))
            self.assertEqual(recheck_single_test(root, self.CMD, "test_alpha")["status"],
                             "passed")

    def test_param_id_stripped_and_no_match_is_error(self):
        from openfde.verify import recheck_single_test
        d, root = self._repo("def test_alpha():\n    assert True\n")
        with d:
            self.assertEqual(recheck_single_test(root, self.CMD, "test_alpha[2]")["status"],
                             "passed")
            self.assertEqual(recheck_single_test(root, self.CMD, "test_missing")["status"],
                             "error")

    def test_non_pytest_command_is_error(self):
        from openfde.verify import recheck_single_test
        self.assertEqual(recheck_single_test("/tmp", ["python3", "-m", "unittest"],
                                             "test_x")["status"], "error")


class RunFaultDomainTest(unittest.TestCase):
    def test_failed_run_is_openfde_fault(self):
        from openfde.verify import run_fault_domain
        self.assertEqual(run_fault_domain({"status": "failed", "error": "no diff"}),
                         "openfde")
        self.assertEqual(run_fault_domain({"status": None}), "openfde")

    def test_clean_run_failing_recheck_is_repo_fault(self):
        from openfde.verify import run_fault_domain
        self.assertEqual(run_fault_domain({"status": "passed", "recheck": "failed"}),
                         "repo")

    def test_clean_green_is_nobodys_failure(self):
        from openfde.verify import run_fault_domain
        self.assertEqual(run_fault_domain({"status": "passed", "recheck": "passed"}), "")


class ParseViaPacksPolyglotTest(unittest.TestCase):
    """A polyglot repo (Python + JS/TS) must still read JS/TS failure locations.

    get_language_packs returns Python FIRST, so the old packs[0]-only parse lost
    Vitest/Jest locations (PythonPack saw unknown output, JsTsPack was never tried).
    _parse_via_packs now tries each detected pack and takes the first non-empty.
    """

    def _polyglot_repo(self):
        d = tempfile.TemporaryDirectory()
        root = Path(d.name)
        (root / "package.json").write_text('{"name":"demo","scripts":{"test":"vitest"}}')
        (root / "service.py").write_text("def add(a, b):\n    return a + b\n")
        return d, root

    def test_js_failure_parsed_when_python_is_first(self):
        # Vitest output: PythonPack returns [] for it, JsTsPack parses file+line.
        vitest = (" FAIL  src/math.test.ts > add > adds two numbers\n"
                  "AssertionError: expected 5 to be 4\n"
                  " ❯ src/math.test.ts:8:19\n")
        d, root = self._polyglot_repo()
        with d:
            from openfde.language_packs import get_language_packs
            self.assertEqual([p.name for p in get_language_packs(root)],
                             ["python", "js_ts"])          # Python first, JS/TS second
            locs = _parse_via_packs(vitest, root)
            self.assertEqual(len(locs), 1)
            self.assertEqual(locs[0]["file"], "src/math.test.ts")
            self.assertEqual(locs[0]["line"], 8)

    def test_python_failure_still_wins_when_python_parses(self):
        # A pytest traceback: PythonPack (first) parses it; JsTsPack would return [].
        pytest_out = (
            "=================================== FAILURES ===================================\n"
            "_________________________________ test_add _________________________________\n"
            "service.py:3: in test_add\n"
            "    assert add(1, 2) == 4\n"
            "E   AssertionError\n")
        d, root = self._polyglot_repo()
        with d:
            locs = _parse_via_packs(pytest_out, root)
            self.assertEqual(len(locs), 1)
            self.assertEqual(locs[0]["file"], "service.py")
            self.assertEqual(locs[0]["line"], 3)
            self.assertEqual(locs[0]["test"], "test_add")
