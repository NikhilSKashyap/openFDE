"""
Tests for openfde.watch_function — inferring *which function* an external edit touched.

Pure and deterministic: a unified-diff parser (new-file line numbers of added lines) and an
enclosing-function heuristic over ArchGraph function start lines. No git or repo required.
"""

import unittest

from openfde import watch_function as wf


class ChangedLineNumbersTest(unittest.TestCase):
    def test_added_lines_across_hunks(self):
        diff = (
            "diff --git a/m.py b/m.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -1,4 +1,5 @@\n"
            " def a():\n"
            "-    return 1\n"
            "+    return 2\n"
            "+    # extra\n"
            " \n"
            " def b():\n"
            "@@ -10,2 +11,3 @@ def b():\n"
            "     pass\n"
            "+    x = 9\n"
        )
        # New-file line numbers of the added lines: 2 and 3 in the first hunk, 12 in the second.
        self.assertEqual(wf.changed_line_numbers(diff), [2, 3, 12])

    def test_removed_lines_do_not_count(self):
        diff = (
            "--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -1,3 +1,2 @@\n"
            " keep\n"
            "-gone\n"
            " keep2\n"
        )
        self.assertEqual(wf.changed_line_numbers(diff), [])

    def test_content_line_starting_with_plus_is_counted(self):
        # A real source line whose content begins with '+' renders as '++...' in the diff body;
        # it must be treated as an added line, not a header.
        diff = (
            "--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -1,1 +1,2 @@\n"
            " x = 0\n"
            "++weird\n"
        )
        self.assertEqual(wf.changed_line_numbers(diff), [2])

    def test_empty_or_garbage_diff(self):
        self.assertEqual(wf.changed_line_numbers(""), [])
        self.assertEqual(wf.changed_line_numbers(None), [])
        self.assertEqual(wf.changed_line_numbers("no hunks here\njust text\n"), [])


class InferChangedFunctionTest(unittest.TestCase):
    def setUp(self):
        # Two functions: a() at line 1, b() at line 10 (ArchGraph gives only start lines).
        self.fns = [{"name": "a", "line": 1}, {"name": "b", "line": 10}]

    def test_most_changed_lines_wins(self):
        # lines 2,3 fall in a(); line 12 falls in b() -> a() owns the most.
        self.assertEqual(wf.infer_changed_function([2, 3, 12], self.fns), "a")

    def test_picks_the_function_for_a_single_line(self):
        self.assertEqual(wf.infer_changed_function([12], self.fns), "b")

    def test_tie_breaks_toward_earlier_function(self):
        # one line each in a() and b() -> earlier (smaller start line) wins, deterministically.
        self.assertEqual(wf.infer_changed_function([2, 11], self.fns), "a")

    def test_line_before_first_function_maps_to_nothing(self):
        self.assertIsNone(wf.infer_changed_function([1], [{"name": "a", "line": 5}]))

    def test_empty_inputs(self):
        self.assertIsNone(wf.infer_changed_function([], self.fns))
        self.assertIsNone(wf.infer_changed_function([2, 3], []))

    def test_functions_without_a_line_are_ignored(self):
        self.assertIsNone(wf.infer_changed_function([2, 3], [{"name": "a"}]))

    def test_accepts_full_archgraph_dicts(self):
        # Extra keys (id/path/args/…) are tolerated — an ArchGraph function dict passes straight in.
        fns = [
            {"id": "function:m.py:a", "name": "a", "path": "m.py", "line": 1, "args": []},
            {"id": "function:m.py:b", "name": "b", "path": "m.py", "line": 10, "args": []},
        ]
        self.assertEqual(wf.infer_changed_function([11, 12], fns), "b")


if __name__ == "__main__":
    unittest.main()
