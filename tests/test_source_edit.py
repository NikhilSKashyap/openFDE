"""
Tests for the repair hatch — failure-location parsing (verify) and the
function-scoped source read/splice (source_edit). The hatch only opens FROM a
failure receipt; these prove the receipt knows WHERE and the hands are safe.
"""

import tempfile
import unittest
from pathlib import Path

from openfde.source_edit import SourceEditError, read_slice, splice_lines
from openfde.verify import parse_failure_locations


UNITTEST_OUTPUT = '''
======================================================================
FAIL: test_acquire_then_conflict (tests.test_instance_lock.InstanceLockTest)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "{root}/tests/test_instance_lock.py", line 27, in test_acquire_then_conflict
    self.assertFalse(Path(lock).exists())
AssertionError: True is not false

======================================================================
ERROR: test_other (tests.test_x.XTest)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/usr/lib/python3.11/unittest/case.py", line 59, in testPartExecutor
    yield
  File "{root}/tests/test_x.py", line 9, in test_other
    helper()
  File "{root}/openfde/helper.py", line 4, in helper
    raise ValueError("boom")
ValueError: boom

----------------------------------------------------------------------
Ran 2 tests in 0.01s

FAILED (failures=1, errors=1)
'''


class ParseFailureLocationsTest(unittest.TestCase):
    def test_deepest_in_repo_frame_wins(self):
        with tempfile.TemporaryDirectory() as d:
            root = str(Path(d).resolve())
            locs = parse_failure_locations(UNITTEST_OUTPUT.format(root=root), root)
        self.assertEqual(len(locs), 2)
        self.assertEqual(locs[0], {"test": "test_acquire_then_conflict",
                                   "file": "tests/test_instance_lock.py",
                                   "line": 27, "func": "test_acquire_then_conflict"})
        # stdlib frame skipped; the APP frame (deepest in-repo) wins over the test frame
        self.assertEqual(locs[1]["file"], "openfde/helper.py")
        self.assertEqual(locs[1]["line"], 4)

    def test_no_frames_no_failures(self):
        self.assertEqual(parse_failure_locations("all good\nOK\n", "/tmp"), [])


class SourceEditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "mod.py").write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_slice_clamps_and_numbers(self):
        s = read_slice(self.root, "mod.py", 4, 99)
        self.assertEqual(s["code"], "def b():\n    return 2")
        self.assertEqual((s["start"], s["end"], s["total"]), (4, 5, 5))

    def test_splice_replaces_range_and_keeps_trailing_newline(self):
        splice_lines(self.root, "mod.py", 4, 5, "def b():\n    return 22")
        text = (self.root / "mod.py").read_text()
        self.assertIn("return 22", text)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("return 1", text)                  # untouched neighbour

    def test_path_escape_refused(self):
        with self.assertRaises(SourceEditError):
            read_slice(self.root, "../etc/passwd", 1, 2)
        with self.assertRaises(SourceEditError):
            read_slice(self.root, "/etc/passwd", 1, 2)

    def test_bad_range_refused(self):
        with self.assertRaises(SourceEditError):
            splice_lines(self.root, "mod.py", 99, 100, "x")

    def test_trailing_blank_line_in_draft_is_preserved(self):
        # The hatch's range often ends on the blank line between functions; a
        # draft ending with a newline must keep that blank (splitlines() ate it
        # once — caught by the first live dogfood of the hatch).
        (self.root / "two.py").write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")
        splice_lines(self.root, "two.py", 1, 3, "def a():\n    return 11\n")
        text = (self.root / "two.py").read_text()
        self.assertIn("return 11\n\ndef b():", text)


if __name__ == "__main__":
    unittest.main()
