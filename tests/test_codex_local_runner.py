"""
Tests for openfde.codex_local_runner — the local Codex CLI as a TEXT role
(Architect / Verifier), Day 3B Slice 1.

These drive the runner through its real subprocess path using a *fake* `codex`
binary (a tiny script that mimics `codex exec ... -o <file>`: reads the prompt
from stdin, writes a "last message" to the -o file, and can simulate nonzero
exits or a misbehaving file mutation), so the read-only/dirty-check guarantees
are exercised end to end with no network and no real Codex.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde.codex_local_runner import run_codex_local, run_codex_local_text

# A fake `codex` CLI. Recognizes `codex exec - ... -o <out>`, reads the prompt
# from stdin, and:
#   - writes $FAKE_CODEX_OUTPUT (default: the prompt it received) to the -o file;
#   - if $FAKE_CODEX_WRITE = "relpath::content", also writes that file in cwd
#     (simulates a misbehaving mutation the read-only guard must catch);
#   - prints $FAKE_CODEX_STDERR to stderr and exits $FAKE_CODEX_EXIT (default 0).
_FAKE_CODEX = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
argv = sys.argv[1:]
out_path = None
for i, a in enumerate(argv):
    if a == "-o" and i + 1 < len(argv):
        out_path = argv[i + 1]
prompt = sys.stdin.read()
exit_code = int(os.environ.get("FAKE_CODEX_EXIT", "0"))
stderr = os.environ.get("FAKE_CODEX_STDERR", "")
if stderr:
    sys.stderr.write(stderr)
mutate = os.environ.get("FAKE_CODEX_WRITE", "")
if mutate and "::" in mutate:
    rel, content = mutate.split("::", 1)
    Path(rel).write_text(content)
if exit_code == 0 and out_path:
    text = os.environ.get("FAKE_CODEX_OUTPUT")
    Path(out_path).write_text(prompt if text is None else text)
sys.exit(exit_code)
"""


def _git(args, cwd):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@t",
                           *args], cwd=str(cwd), capture_output=True, text=True)


class CodexLocalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake_codex.py"
        self.fake.write_text(_FAKE_CODEX)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        # Committed baseline so the pre/post dirty-set check has a clean tree.
        _git(["init", "-q"], self.root)
        (self.root / "app.py").write_text("print('hi')\n")
        _git(["add", "-A"], self.root)
        _git(["commit", "-qm", "baseline"], self.root)

    def tearDown(self):
        for k in ("FAKE_CODEX_OUTPUT", "FAKE_CODEX_EXIT", "FAKE_CODEX_STDERR", "FAKE_CODEX_WRITE"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    # 1) Architect-style call → returns the model's text output.
    def test_text_role_returns_output(self):
        os.environ["FAKE_CODEX_OUTPUT"] = "BRIEF: make ingest async."
        out = run_codex_local(system="You are the Architect.", user="Intent: async ingest",
                              cwd=self.root, codex_bin=str(self.fake))
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["text"], "BRIEF: make ingest async.")
        self.assertEqual(out["touched"], [])
        # Text wrapper returns the same string.
        os.environ["FAKE_CODEX_OUTPUT"] = "BRIEF: make ingest async."
        self.assertEqual(
            run_codex_local_text(system="You are the Architect.", user="x",
                                 cwd=self.root, codex_bin=str(self.fake)),
            "BRIEF: make ingest async.")

    # 2) System + user are combined into the single prompt fed on stdin.
    def test_prompt_combines_system_and_user(self):
        # No FAKE_CODEX_OUTPUT → fake echoes the received prompt back as the answer.
        out = run_codex_local(system="SYS-MARKER", user="USER-MARKER",
                              cwd=self.root, codex_bin=str(self.fake))
        self.assertTrue(out["ok"], out.get("error"))
        self.assertIn("SYS-MARKER", out["text"])
        self.assertIn("USER-MARKER", out["text"])

    # 3) Missing CLI → clear provider error; text wrapper degrades to "".
    def test_missing_cli_returns_clear_error(self):
        out = run_codex_local(system="s", user="u", cwd=self.root,
                              codex_bin=str(self.root / "does_not_exist"))
        self.assertFalse(out["ok"])
        self.assertIn("Codex CLI not found", out["error"])
        self.assertEqual(
            run_codex_local_text(system="s", user="u", cwd=self.root,
                                 codex_bin=str(self.root / "does_not_exist")),
            "")

    # 4) Nonzero exit → surfaces returncode + stderr summary.
    def test_nonzero_exit_surfaces_stderr(self):
        os.environ["FAKE_CODEX_EXIT"] = "2"
        os.environ["FAKE_CODEX_STDERR"] = "auth required"
        out = run_codex_local(system="s", user="u", cwd=self.root, codex_bin=str(self.fake))
        self.assertFalse(out["ok"])
        self.assertIn("exited 2", out["error"])
        self.assertIn("auth required", out["error"])

    # 5) A read-only role that somehow mutates the tree → fail, do NOT revert.
    def test_unexpected_file_change_fails_without_revert(self):
        os.environ["FAKE_CODEX_OUTPUT"] = "ok"
        os.environ["FAKE_CODEX_WRITE"] = "sneaky.txt::i should not exist"
        out = run_codex_local(system="s", user="u", cwd=self.root, codex_bin=str(self.fake))
        self.assertFalse(out["ok"])
        self.assertIn("unexpectedly modified", out["error"])
        self.assertIn("sneaky.txt", out["touched"])
        # We surface it; we never delete/revert (no data loss).
        self.assertTrue((self.root / "sneaky.txt").exists())


if __name__ == "__main__":
    unittest.main()
