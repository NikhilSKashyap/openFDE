"""
Tests for the LanguagePack slice (openfde.language_packs). The law under test:
extracting the Python seams behind a pack changes NOTHING — the pack must produce
the same checks and the same failure shape as calling verify directly, and the
registry must detect Python where Python files exist.
"""
import os
import tempfile
import unittest
from pathlib import Path

from openfde import verify
from openfde.language_packs import (
    FailureLocation,
    PythonPack,
    VerifyCheckSpec,
    get_language_packs,
    get_pack_for_file,
)

_PYTEST_TB = (
    "=================================== FAILURES ===================================\n"
    "_________________________________ test_thing __________________________________\n"
    "tests/test_thing.py:4: in test_thing\n"
    "    assert add(1, 2) == 4\n"
    "E   AssertionError\n"
)


def _py_repo():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "pkg").mkdir()
    (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_thing.py").write_text("def test_thing():\n    assert True\n")
    return d, root


class RegistryTest(unittest.TestCase):
    def test_detects_python_pack_when_py_files_exist(self):
        d, root = _py_repo()
        with d:
            packs = get_language_packs(root)
            self.assertEqual([p.name for p in packs], ["python"])
            self.assertTrue(PythonPack().detects(root))

    def test_no_pack_for_empty_repo(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "README.md").write_text("# hi\n")
            self.assertEqual(get_language_packs(d), [])

    def test_get_pack_for_file_by_extension(self):
        self.assertEqual(get_pack_for_file("a/b/model.py").name, "python")
        self.assertIsNone(get_pack_for_file("a/b/app.ts"))
        self.assertIsNone(get_pack_for_file("Cargo.toml"))


class PythonPackParityTest(unittest.TestCase):
    """The pack output, normalized back to dicts, must equal the raw verify output."""

    def test_discover_checks_matches_raw(self):
        d, root = _py_repo()
        with d:
            raw = verify.discover_checks(root)
            via_pack = [s.as_dict() for s in PythonPack().discover_checks(root)]
            self.assertEqual(via_pack, raw)

    def test_parse_failures_matches_raw_and_shape(self):
        d, root = _py_repo()
        with d:
            raw = verify.parse_failure_locations(_PYTEST_TB, root)
            via_pack = [f.as_dict() for f in PythonPack().parse_failures(_PYTEST_TB, root)]
            self.assertEqual(via_pack, raw)
            # the existing OpenFDE failure shape: {test, file, line, func}
            self.assertTrue(raw and set(raw[0]) >= {"file", "line", "func", "test"})

    def test_ensure_check_config_pins_pytest(self):
        d, root = _py_repo()
        with d:
            PythonPack().ensure_check_config(root)
            cfg = root / ".openfde" / "verify.json"
            self.assertTrue(cfg.exists())
            self.assertIn("pytest", cfg.read_text())
            # idempotent: a second call must not overwrite
            before = cfg.read_text()
            PythonPack().ensure_check_config(root)
            self.assertEqual(cfg.read_text(), before)

    def test_repro_context_is_pytest(self):
        ctx = PythonPack().repro_context()
        self.assertEqual(ctx["framework"], "pytest")
        self.assertIn("pytest", " ".join(ctx["test_command"]))


class DataclassRoundTripTest(unittest.TestCase):
    def test_failure_location_round_trip_omits_empty_message(self):
        d = {"test": "t", "file": "m.py", "line": 7, "func": "f"}
        self.assertEqual(FailureLocation.from_dict(d).as_dict(), d)        # no message key
        with_msg = FailureLocation.from_dict({**d, "message": "boom"}).as_dict()
        self.assertEqual(with_msg["message"], "boom")

    def test_check_spec_round_trip_excludes_reporter(self):
        d = {"id": "unit-tests", "label": "Unit tests", "command": ["pytest"],
             "cwd": "", "required": True}
        spec = VerifyCheckSpec.from_dict(d)
        self.assertEqual(spec.reporter, "text")          # default groundwork
        self.assertEqual(spec.as_dict(), d)              # reporter NOT serialized


if __name__ == "__main__":
    unittest.main()
