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
    discover_checks,
    run_check,
    run_verification,
)


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
