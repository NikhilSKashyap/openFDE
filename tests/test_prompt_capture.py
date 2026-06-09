"""
Tests for openfde.prompt_capture — passive Claude Code prompt capture.

Covers the pure parse/filter helpers and one full loop tick against a synthetic
transcript in a temp HOME: a new human prompt becomes a capture episode, and noise
(slash commands, tool results, meta) is ignored.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from openfde import prompt_capture as pc
from openfde.persistence import Persistence


def _user(text, uuid, sid="sess1", cwd="/repo"):
    return {"type": "user", "uuid": uuid, "sessionId": sid, "cwd": cwd,
            "timestamp": "2026-06-07T00:00:00Z",
            "message": {"role": "user", "content": text}}


def _assistant_edit(file_path, uuid, sid="sess1"):
    return {"type": "assistant", "uuid": uuid, "sessionId": sid,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": file_path}}]}}


class ParseTest(unittest.TestCase):
    def test_encode_repo_dir(self):
        self.assertEqual(pc.encode_repo_dir("/Users/x/Downloads/openfde"),
                         "-Users-x-Downloads-openfde")

    def test_is_human_prompt_accepts_real_prompt(self):
        self.assertTrue(pc.is_human_prompt(_user("add login to auth", "u1")))
        self.assertEqual(pc.prompt_text(_user("hello", "u2")), "hello")

    def test_filters_internal_summarizer_marker(self):
        # OpenFDE's own LLM summarizer prompts must never be captured as episodes.
        self.assertFalse(pc.is_human_prompt(
            _user("[OpenFDE internal summarizer]\n\nSummarize this prompt: build login", "uX")))

    def test_filters_command_and_tool_noise(self):
        self.assertFalse(pc.is_human_prompt(_user("<command-name>/login</command-name>", "u3")))
        self.assertFalse(pc.is_human_prompt(_user("<local-command-stdout>ok</local-command-stdout>", "u4")))
        self.assertFalse(pc.is_human_prompt({"type": "user", "isMeta": True,
                                             "message": {"role": "user", "content": "x"}}))
        # tool_result content is an agent turn, not a human prompt
        tool = {"type": "user", "uuid": "u5", "sessionId": "s",
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "done"}]}}
        self.assertFalse(pc.is_human_prompt(tool))

    def test_read_new_prompts_offset_and_partial_line(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            f.write_text(json.dumps(_user("first prompt", "a1")) + "\n")
            prompts, off = pc.read_new_prompts(f, 0)
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["text"], "first prompt")
            # Append a complete line + a partial (no newline) line.
            with open(f, "a") as fh:
                fh.write(json.dumps(_user("second", "a2")) + "\n")
                fh.write('{"partial":')
            prompts2, off2 = pc.read_new_prompts(f, off)
            self.assertEqual([p["text"] for p in prompts2], ["second"])
            # Offset stops before the partial line (re-read next time).
            self.assertLess(off2, f.stat().st_size)


class CaptureLoopTest(unittest.TestCase):
    def test_cwd_matched_prompt_becomes_episode(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            root = Path(repo).resolve()
            cwd = str(root)                                    # session rooted AT the repo
            proj = pc.claude_projects_dir(root, home=home)
            proj.mkdir(parents=True, exist_ok=True)
            tx = proj / "session.jsonl"
            tx.write_text(json.dumps(_user("OLD prompt before watch", "old1", cwd=cwd)) + "\n")
            p = Persistence(root / ".openfde")
            captured = []

            async def drive():
                task = asyncio.create_task(
                    pc.watch_loop(root, p, _NullManager(), interval=0.05, home=home,
                                  on_episode=captured.append))
                await asyncio.sleep(0.15)                      # baseline established
                with open(tx, "a") as fh:
                    fh.write(json.dumps(_user("make the button async", "new1", cwd=cwd)) + "\n")
                    fh.write(json.dumps(_user("<command-name>/clear</command-name>", "noise1", cwd=cwd)) + "\n")
                await asyncio.sleep(0.2)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(drive())
            eps = p.load_episodes()
            self.assertEqual(len(eps), 1)                      # new prompt only (not old, not noise)
            self.assertEqual(eps[0]["prompt"], "make the button async")
            self.assertEqual(eps[0]["source"], "openfde-capture")

    def test_cross_cwd_capture_by_edited_file(self):
        # The whole point: a session rooted ELSEWHERE that edits THIS repo is captured.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            root = Path(repo).resolve()
            # Session lives in a different project dir, cwd is some other folder.
            proj = pc.claude_projects_root(home) / "-Users-someone-elsewhere"
            proj.mkdir(parents=True, exist_ok=True)
            tx = proj / "session.jsonl"
            tx.write_text("")                                  # baseline empty
            p = Persistence(root / ".openfde")

            async def drive():
                task = asyncio.create_task(
                    pc.watch_loop(root, p, _NullManager(), interval=0.05, home=home))
                await asyncio.sleep(0.15)
                with open(tx, "a") as fh:
                    # prompt with a FOREIGN cwd → goes pending, not captured yet
                    fh.write(json.dumps(_user("refactor the parser", "u1", cwd="/Users/someone/elsewhere")) + "\n")
                    # …then the session edits a file UNDER our repo → prompt captured
                    fh.write(json.dumps(_assistant_edit(str(root / "parser.py"), "a1")) + "\n")
                await asyncio.sleep(0.25)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(drive())
            eps = p.load_episodes()
            self.assertEqual(len(eps), 1)
            self.assertEqual(eps[0]["prompt"], "refactor the parser")
            self.assertEqual(eps[0]["files"], ["parser.py"])   # the edited repo file
            self.assertEqual(eps[0]["status"], "reviewing")

    def test_foreign_session_not_touching_repo_is_ignored(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            root = Path(repo).resolve()
            proj = pc.claude_projects_root(home) / "-Users-someone-other"
            proj.mkdir(parents=True, exist_ok=True)
            tx = proj / "s.jsonl"
            tx.write_text("")
            p = Persistence(root / ".openfde")

            async def drive():
                task = asyncio.create_task(
                    pc.watch_loop(root, p, _NullManager(), interval=0.05, home=home))
                await asyncio.sleep(0.15)
                with open(tx, "a") as fh:
                    fh.write(json.dumps(_user("do unrelated work", "u9", cwd="/Users/someone/other")) + "\n")
                    fh.write(json.dumps(_assistant_edit("/Users/someone/other/x.py", "a9")) + "\n")
                await asyncio.sleep(0.25)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(drive())
            self.assertEqual(len(p.load_episodes()), 0)        # never edited our repo → ignored


class TurnBoundaryLandTest(unittest.TestCase):
    """The turn-boundary / idle land (`_land_active_capture`) commits a reviewing capture
    episode's FULL dirty set, clustered into one commit per logical change."""

    def _repo(self, d):
        import subprocess
        from openfde import git_timeline as gt
        root = Path(d).resolve()

        def g(*a):
            return subprocess.run(["git", *a], cwd=str(root), capture_output=True, text=True)
        g("init", "-q"); g("config", "user.email", "t@e.com"); g("config", "user.name", "T")
        (root / ".gitignore").write_text("\n".join(gt._IGNORE_ENTRIES) + "\n")
        (root / "seed.py").write_text("x\n"); g("add", "-A")
        g("-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "init")
        return root, g

    def test_lands_full_set_clustered(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            root, g = self._repo(d)
            p = Persistence(root / ".openfde")
            p.upsert_episode({"episodeId": "episode_tb", "title": "Turn Boundary", "prompt": "do it",
                              "source": "openfde-capture", "status": "reviewing", "sessionId": "sess1",
                              "files": ["feat.py", "frontend/ui.jsx"], "commitShas": []})
            (root / "feat.py").write_text("feature\n")                 # scope "."
            (root / "frontend").mkdir(); (root / "frontend" / "ui.jsx").write_text("ui\n")  # scope frontend
            os.environ["OPENFDE_LLM_SUMMARY"] = "0"                    # deterministic — no CLI subprocess
            try:
                asyncio.run(pc._land_active_capture(root, p, _NullManager(), {}, session_id="sess1"))
            finally:
                os.environ.pop("OPENFDE_LLM_SUMMARY", None)
            ep = p.get_episode("episode_tb")
            self.assertEqual(ep["status"], "landed")
            self.assertGreaterEqual(len(ep["commitShas"]), 2)         # clustered by scope
            self.assertEqual(len(ep.get("commitMeta") or {}), len(ep["commitShas"]))
            committed = set()
            for sha in ep["commitShas"]:
                committed |= set(g("show", "--name-only", "--format=", sha).stdout.split())
            self.assertEqual(committed, {"feat.py", "frontend/ui.jsx"})  # FULL set landed
            self.assertEqual(g("status", "--porcelain").stdout.strip(), "")

    def test_respects_session_filter(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            root, g = self._repo(d)
            p = Persistence(root / ".openfde")
            p.upsert_episode({"episodeId": "episode_tb", "title": "T", "prompt": "x",
                              "source": "openfde-capture", "status": "reviewing", "sessionId": "sess1",
                              "files": ["feat.py"], "commitShas": []})
            (root / "feat.py").write_text("feature\n")
            os.environ["OPENFDE_LLM_SUMMARY"] = "0"
            try:                                                       # a DIFFERENT session → no land
                asyncio.run(pc._land_active_capture(root, p, _NullManager(), {}, session_id="other"))
            finally:
                os.environ.pop("OPENFDE_LLM_SUMMARY", None)
            self.assertEqual(p.get_episode("episode_tb")["status"], "reviewing")  # left for its own turn


class _NullManager:
    async def broadcast(self, msg):
        return None


if __name__ == "__main__":
    unittest.main()
