"""
Tests for openfde.prompt_wrapper — the Prompt Capture Bridge.

The agent dispatcher is injected (`invoke`) so the episode lifecycle is exercised
deterministically without spawning Claude Code / Codex. Verifies the prompt is
captured as an episode regardless of outcome, and that touched files + status are
recorded so the change surfaces on the Prompt Story Rail.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde.persistence import Persistence
from openfde.prompt_wrapper import run_prompt_wrapper


class PromptWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=self.root)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.root)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.root)
        # .gitignore already at steady state so the first auto-land's ensure_git_repo()
        # doesn't add it as an untracked artifact.
        from openfde import git_timeline as gt
        (self.root / ".gitignore").write_text("\n".join(gt._IGNORE_ENTRIES) + "\n")
        (self.root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root)
        subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T",
                        "commit", "-q", "-m", "init"], cwd=self.root)
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def test_edit_auto_landed_under_episode(self):
        # Agent really edits a.py → wrapper AUTO-LANDS it (scoped commit under the episode).
        def fake(kind, root, prompt, model):
            (Path(root) / "a.py").write_text("x = 2  # edited\n")
            return {"ok": True, "touched": ["a.py"], "summary": "edited a.py", "error": None}
        out = run_prompt_wrapper("claude-code", "tweak a", str(self.root), invoke=fake)
        ep = out["episode"]
        self.assertEqual(ep["kind"], "claude-code")
        self.assertEqual(ep["status"], "landed")            # auto-landed on completion
        self.assertEqual(ep["files"], ["a.py"])
        self.assertTrue(ep["commitShas"])                   # a commit was created
        self.assertEqual(ep["source"], "openfde-wrapper")
        self.assertIn("auto-landed", out["message"])
        # The commit carries the episode trailer and the tree is clean afterwards.
        msg = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=self.root,
                             capture_output=True, text=True).stdout
        self.assertIn("OpenFDE-Episode: " + ep["episodeId"], msg)
        porc = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(porc, "")

    def test_unrelated_dirty_file_not_swept_into_episode(self):
        # A dirty file the agent did NOT touch must remain dirty after auto-land.
        (self.root / "unrelated.py").write_text("u = 1\n")   # pre-existing unrelated dirty
        def fake(kind, root, prompt, model):
            (Path(root) / "a.py").write_text("x = 9\n")
            return {"ok": True, "touched": ["a.py"], "summary": "edited a.py", "error": None}
        out = run_prompt_wrapper("claude-code", "tweak a", str(self.root), invoke=fake)
        self.assertEqual(out["episode"]["status"], "landed")
        landed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                                cwd=self.root, capture_output=True, text=True).stdout.split()
        self.assertIn("a.py", landed)
        self.assertNotIn("unrelated.py", landed)            # never swept in
        porc = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                              capture_output=True, text=True).stdout
        self.assertIn("unrelated.py", porc)                 # still dirty

    def test_no_changes_recorded_cleanly(self):
        def fake(kind, root, prompt, model):
            return {"ok": True, "touched": [], "summary": "nothing to do", "error": None}
        out = run_prompt_wrapper("codex", "noop", str(self.root), invoke=fake)
        self.assertEqual(out["episode"]["status"], "complete_no_changes")
        self.assertEqual(out["episode"]["files"], [])

    def test_failure_recorded_without_fake_edits(self):
        def fake(kind, root, prompt, model):
            return {"ok": False, "touched": [], "summary": "", "error": "Codex CLI not found."}
        out = run_prompt_wrapper("codex", "do it", str(self.root), invoke=fake)
        self.assertEqual(out["episode"]["status"], "failed")
        self.assertEqual(out["episode"]["files"], [])
        # Prompt is still captured (the "why" is never lost), just marked failed.
        self.assertEqual(self.p.get_episode(out["episode"]["episodeId"])["prompt"], "do it")
        self.assertIn("did not complete", out["message"])

    def test_prompt_persisted_before_agent_runs(self):
        # If the agent raises, the episode must already be on disk (captured first).
        def boom(kind, root, prompt, model):
            raise RuntimeError("agent crashed")
        with self.assertRaises(RuntimeError):
            run_prompt_wrapper("claude-code", "risky", str(self.root), invoke=boom)
        eps = self.p.load_episodes()
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["prompt"], "risky")
        self.assertEqual(eps[0]["status"], "open")     # never finalized → stays open


if __name__ == "__main__":
    unittest.main()
