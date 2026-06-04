"""
Regression tests for openfde.claude_code_runner — scope enforcement must NEVER
disturb the user's pre-existing uncommitted work (Step 31 patch).

These drive the runner through its real subprocess path using a *fake* `claude`
binary (a tiny script that edits files + prints the CLI JSON envelope), so the
pre/post dirty-state snapshot logic is exercised end to end with no network.
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde.claude_code_runner import run_claude_code

# A fake `claude` CLI: writes files named in $FAKE_CLAUDE_WRITES (JSON list of
# [relpath, content]) relative to cwd, then prints the result JSON envelope.
_FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
spec = os.environ.get("FAKE_CLAUDE_WRITES", "[]")
for rel, content in json.loads(spec):
    p = Path(rel)
    if p.parent != Path("."):
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
print(json.dumps({"type": "result", "is_error": False,
                  "result": "fake edit applied", "total_cost_usd": 0.01}))
"""


def _git(args, cwd):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@t",
                           *args], cwd=str(cwd), capture_output=True, text=True)


class ClaudeCodeRunnerScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Fake claude binary on disk, executable.
        self.fake = self.root / "fake_claude.py"
        self.fake.write_text(_FAKE_CLAUDE)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        # A committed baseline repo: ingest/reader.py + other/note.txt.
        _git(["init", "-q"], self.root)
        (self.root / "ingest").mkdir()
        (self.root / "other").mkdir()
        (self.root / "ingest" / "reader.py").write_text("ORIGINAL\n")
        (self.root / "other" / "note.txt").write_text("ORIGINAL NOTE\n")
        _git(["add", "-A"], self.root)
        _git(["commit", "-qm", "baseline"], self.root)

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_WRITES", None)
        self.tmp.cleanup()

    def _run(self, writes, editable, protected=None):
        os.environ["FAKE_CLAUDE_WRITES"] = json.dumps(writes)
        return run_claude_code(
            repo_root=self.root, prompt="do it",
            editable=editable, protected=(protected or []),
            claude_bin=str(self.fake))

    def _read(self, rel):
        return (self.root / rel).read_text()

    # 1) Clean repo + in-scope edit → passes, file changed.
    def test_clean_in_scope_edit_passes(self):
        out = self._run([["ingest/reader.py", "NEW CONTENT\n"]],
                        editable=["ingest/reader.py"])
        self.assertEqual(out["result"]["status"], "passed", out["result"]["reportSummary"])
        self.assertEqual(out["writes"], ["ingest/reader.py"])
        self.assertEqual(self._read("ingest/reader.py"), "NEW CONTENT\n")

    # 2) Pre-existing out-of-scope dirty file is preserved, not reverted.
    def test_preexisting_out_of_scope_dirty_preserved(self):
        # User has uncommitted work in other/note.txt before Execute.
        (self.root / "other" / "note.txt").write_text("USER EDIT\n")
        out = self._run([["ingest/reader.py", "NEW CONTENT\n"]],
                        editable=["ingest/reader.py"])
        self.assertEqual(out["result"]["status"], "passed", out["result"]["reportSummary"])
        self.assertEqual(out["writes"], ["ingest/reader.py"])
        # The user's dirty file must be untouched — NOT reverted to HEAD.
        self.assertEqual(self._read("other/note.txt"), "USER EDIT\n")

    # 3) Pre-existing dirty file in attempted scope → fail safe, NOT overwritten.
    def test_preexisting_dirty_in_scope_fails_safely(self):
        # User has uncommitted work in the very file we're about to scope.
        (self.root / "ingest" / "reader.py").write_text("USER WIP\n")
        out = self._run([["ingest/reader.py", "CLAUDE WOULD WRITE\n"]],
                        editable=["ingest/reader.py"])
        self.assertEqual(out["result"]["status"], "failed")
        self.assertIn("uncommitted changes in scope", out["result"]["reportSummary"])
        self.assertEqual(out["writes"], [])
        # Claude never ran → the user's WIP is exactly as they left it.
        self.assertEqual(self._read("ingest/reader.py"), "USER WIP\n")

    # 4) Claude touches a pre-dirty out-of-scope file → fail safe, NOT reverted.
    def test_claude_edits_preexisting_dirty_out_of_scope_fails_safely(self):
        (self.root / "other" / "note.txt").write_text("USER EDIT\n")
        out = self._run(
            [["ingest/reader.py", "NEW CONTENT\n"], ["other/note.txt", "CLAUDE CLOBBER\n"]],
            editable=["ingest/reader.py"])
        self.assertEqual(out["result"]["status"], "failed")
        self.assertIn("already had uncommitted changes", out["result"]["reportSummary"])
        # We must not have reverted it back to HEAD ("ORIGINAL NOTE").
        self.assertNotEqual(self._read("other/note.txt"), "ORIGINAL NOTE\n")


if __name__ == "__main__":
    unittest.main()
