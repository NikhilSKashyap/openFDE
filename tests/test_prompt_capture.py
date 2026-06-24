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

    def test_filters_managed_council_subprocess_marker(self):
        # A managed autonomous-council agent subprocess (claude -p / codex exec OpenFDE launched) is a
        # council turn, never a standalone human prompt — for BOTH the Claude and Codex capture paths.
        from openfde.agent_sessions import _managed_prompt
        marked = _managed_prompt("run_abc123", "You are the ARCHITECT on an autonomous engineering council...")
        self.assertIn("OPENFDE_MANAGED_RUN_ID", marked)
        self.assertFalse(pc.is_human_prompt(_user(marked, "uM")))
        codex_entry = {"type": "response_item",
                       "payload": {"type": "message", "role": "user",
                                   "content": [{"type": "text", "text": marked}]}}
        self.assertFalse(pc.is_codex_human_prompt(codex_entry))
        # a genuine human prompt is still captured
        self.assertTrue(pc.is_human_prompt(_user("add a /healthz endpoint", "uH")))

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


class SessionAwareIdleTest(unittest.TestCase):
    """Idle landing is OPT-IN now (whole episodes beat eager commits). The default
    NEVER lands on silence — even file-quiet + transcript-quiet split long agent
    turns (one long tool call keeps the transcript silent past any window). The
    double-quiet gate is still tested below via allow_idle=True (the opt-in path)."""

    def _seed(self, d, session_id="sess1"):
        import time as _t
        root, g = TurnBoundaryLandTest._repo(self, d)
        p = Persistence(root / ".openfde")
        ep = {"episodeId": "episode_idle", "title": "Idle", "prompt": "long turn",
              "source": "openfde-capture", "status": "reviewing",
              "files": ["feat.py"], "commitShas": []}
        if session_id:
            ep["sessionId"] = session_id
        p.upsert_episode(ep)
        (root / "feat.py").write_text("feature\n")
        last_change = {"episode_idle": _t.time() - 999}        # files LONG quiet
        return root, p, last_change

    def test_default_never_lands_on_silence_alone(self):
        # THE Slice-A guarantee: everything quiet for ages → still reviewing.
        import os, time as _t
        with tempfile.TemporaryDirectory() as d:
            root, p, last_change = self._seed(d)
            os.environ["OPENFDE_LLM_SUMMARY"] = "0"
            try:                                               # default: idle landing disabled
                asyncio.run(pc._maybe_autoland(root, p, _NullManager(), last_change, 12,
                                               {"sess1": _t.time() - 9999}))
            finally:
                os.environ.pop("OPENFDE_LLM_SUMMARY", None)
            ep = p.get_episode("episode_idle")
            self.assertEqual(ep["status"], "reviewing")        # parked for boundary/manual Land
            self.assertEqual(ep["commitShas"], [])             # nothing committed

    def test_optin_holds_while_transcript_still_streams(self):
        import os, time as _t
        with tempfile.TemporaryDirectory() as d:
            root, p, last_change = self._seed(d)
            os.environ["OPENFDE_LLM_SUMMARY"] = "0"
            try:                                               # transcript appended 1s ago → mid-turn
                asyncio.run(pc._maybe_autoland(root, p, _NullManager(), last_change, 12,
                                               {"sess1": _t.time()}, allow_idle=True))
            finally:
                os.environ.pop("OPENFDE_LLM_SUMMARY", None)
            self.assertEqual(p.get_episode("episode_idle")["status"], "reviewing")  # held open

    def test_optin_lands_when_session_also_quiet(self):
        import os, time as _t
        with tempfile.TemporaryDirectory() as d:
            root, p, last_change = self._seed(d)
            os.environ["OPENFDE_LLM_SUMMARY"] = "0"
            try:                                               # opted in + both quiet → lands
                asyncio.run(pc._maybe_autoland(root, p, _NullManager(), last_change, 12,
                                               {"sess1": _t.time() - 999}, allow_idle=True))
            finally:
                os.environ.pop("OPENFDE_LLM_SUMMARY", None)
            self.assertEqual(p.get_episode("episode_idle")["status"], "landed")

    def test_files_accumulate_across_quiet_gaps(self):
        # The pay-off of NOT idle-landing: later edits in the same turn keep
        # attaching to the SAME open episode instead of orphaning.
        with tempfile.TemporaryDirectory() as d:
            root, g = TurnBoundaryLandTest._repo(self, d)
            p = Persistence(root / ".openfde")
            p.upsert_episode({"episodeId": "episode_acc", "title": "Long Turn",
                              "prompt": "x", "source": "openfde-capture",
                              "status": "open", "files": [], "commitShas": [],
                              "sessionId": "sess1"})
            baselines = {"episode_acc": set()}
            (root / "feat.py").write_text("first edit\n")
            asyncio.run(pc._link_changes(root, p, baselines, _NullManager(), {}))
            self.assertEqual(p.get_episode("episode_acc")["files"], ["feat.py"])
            # …a long quiet gap passes (no timer fires; nothing lands)…
            (root / "later.py").write_text("second wave\n")
            asyncio.run(pc._link_changes(root, p, baselines, _NullManager(), {}))
            ep = p.get_episode("episode_acc")
            self.assertEqual(ep["files"], ["feat.py", "later.py"])  # whole episode intact
            self.assertEqual(ep["status"], "reviewing")
            self.assertEqual(ep["commitShas"], [])


# ── Codex passive capture ───────────────────────────────────────────────
def _cdx_meta(cwd, sid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", ts="2026-06-09T00:00:00Z"):
    return {"type": "session_meta", "timestamp": ts, "payload": {"id": sid, "cwd": cwd}}


def _cdx_user(text, ts="2026-06-09T00:00:01Z"):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


class CodexParseTest(unittest.TestCase):
    def test_accepts_user_input_text(self):
        e = _cdx_user("refactor the parser to stream tokens")
        self.assertTrue(pc.is_codex_human_prompt(e))
        self.assertEqual(pc.codex_prompt_text(e), "refactor the parser to stream tokens")

    def test_filters_internal_summarizer_marker(self):
        self.assertFalse(pc.is_codex_human_prompt(
            _cdx_user("[OpenFDE internal summarizer]\n\nSummarize this prompt: build login")))

    def test_filters_injected_context_blocks(self):
        # Codex's own AGENTS.md / environment / user-instructions injections are NOT prompts.
        for inj in ("# AGENTS.md instructions for /Users/x/repo\n<INSTRUCTIONS>…",
                    "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>",
                    "<user_instructions>be concise</user_instructions>"):
            self.assertFalse(pc.is_codex_human_prompt(_cdx_user(inj)), inj[:30])

    def test_filters_non_user_and_empty(self):
        self.assertFalse(pc.is_codex_human_prompt({"type": "response_item", "payload": {"type": "reasoning"}}))
        self.assertFalse(pc.is_codex_human_prompt(_cdx_user("   ")))
        self.assertFalse(pc.is_codex_human_prompt({"type": "event_msg", "payload": {"type": "user_message"}}))

    def test_cwd_from_meta_and_turn_context(self):
        self.assertEqual(pc._codex_cwd_of(_cdx_meta("/repo")), "/repo")
        self.assertEqual(pc._codex_cwd_of({"type": "turn_context", "payload": {"cwd": "/repo2"}}), "/repo2")
        self.assertIsNone(pc._codex_cwd_of(_cdx_user("hi")))                 # a prompt entry has no cwd

    def test_session_id_from_filename(self):
        self.assertEqual(
            pc.codex_session_id_from_path(
                "/x/rollout-2026-05-25T13-26-32-019e60d1-565b-7532-8ad2-94d35615a25f.jsonl"),
            "019e60d1-565b-7532-8ad2-94d35615a25f")

    def test_record_carries_kind_session_and_stable_key(self):
        rec = pc._codex_prompt_record(_cdx_user("do x", ts="2026-06-09T00:00:09Z"),
                                      {"sessionId": "s1", "cwd": "/repo"})
        self.assertEqual(rec["kind"], "codex")
        self.assertEqual(rec["sessionId"], "s1")
        self.assertEqual(rec["cwd"], "/repo")
        self.assertEqual(rec["key"], "codex:s1:2026-06-09T00:00:09Z")        # stable across restarts


class CodexCaptureLoopTest(unittest.TestCase):
    def _rollout(self, home):
        d = pc.codex_sessions_root(home) / "2026" / "06" / "09"
        d.mkdir(parents=True, exist_ok=True)
        return d / "rollout-2026-06-09T00-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"

    def _write(self, path, entries):
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_forward_capture_new_codex_prompt(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            root = Path(repo).resolve(); cwd = str(root)
            tx = self._rollout(home)
            # Present at startup → baselined to EOF → ignored (capture-forward).
            self._write(tx, [_cdx_meta(cwd), _cdx_user("OLD prompt before watch", "2026-06-09T00-00-00Z")])
            p = Persistence(root / ".openfde")

            async def drive():
                task = asyncio.create_task(
                    pc.watch_loop(root, p, _NullManager(), interval=0.05, home=home, autoland=False))
                await asyncio.sleep(0.15)                  # baseline established
                with open(tx, "a") as fh:
                    fh.write(json.dumps(_cdx_user("make the button async", "2026-06-09T00-01-00Z")) + "\n")
                    fh.write(json.dumps(_cdx_user("<environment_context>noise</environment_context>", "x")) + "\n")
                await asyncio.sleep(0.2)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(drive())
            eps = p.load_episodes()
            self.assertEqual(len(eps), 1)                  # new prompt only (not old, not injected)
            self.assertEqual(eps[0]["prompt"], "make the button async")
            self.assertEqual(eps[0]["kind"], "codex")
            self.assertEqual(eps[0]["source"], "openfde-capture")
            self.assertTrue((eps[0]["captureKey"] or "").startswith("codex:"))

    def test_codex_prompt_then_dirty_file_attribution(self):
        import subprocess
        from openfde import git_timeline as gt
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            root = Path(repo).resolve(); cwd = str(root)

            def g(*a):
                return subprocess.run(["git", *a], cwd=str(root), capture_output=True, text=True)
            g("init", "-q"); g("config", "user.email", "t@e.com"); g("config", "user.name", "T")
            (root / ".gitignore").write_text("\n".join(gt._IGNORE_ENTRIES) + "\n")
            (root / "seed.py").write_text("x\n"); g("add", "-A")
            g("-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "init")
            tx = self._rollout(home); self._write(tx, [_cdx_meta(cwd)])
            p = Persistence(root / ".openfde")

            async def drive():
                task = asyncio.create_task(
                    pc.watch_loop(root, p, _NullManager(), interval=0.05, home=home, autoland=False))
                await asyncio.sleep(0.15)
                with open(tx, "a") as fh:                  # cwd-matched Codex prompt
                    fh.write(json.dumps(_cdx_user("edit the feature", "2026-06-09T00-02-00Z")) + "\n")
                await asyncio.sleep(0.15)                  # episode captured (open)
                (root / "feature.py").write_text("feature\n")   # the edit lands in the work tree
                await asyncio.sleep(0.3)                   # dirty-set link attaches it
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(drive())
            eps = p.load_episodes()
            self.assertEqual(len(eps), 1)
            self.assertEqual(eps[0]["kind"], "codex")
            self.assertEqual(eps[0]["status"], "reviewing")           # dirty-set linked
            self.assertIn("feature.py", eps[0]["files"])              # attributed via dirty-set


class _NullManager:
    async def broadcast(self, msg):
        return None


# ── sessionCwd attribution: trust the watched repo over the transcript cwd ────
_R = "/Users/x/Downloads/openfde"
_FOREIGN = "/Users/x/Downloads/interview"


class AttributedSessionCwdTest(unittest.TestCase):
    """attributed_session_cwd follows the WORK (files under the watched repo), not the cwd of
    the agent process (which can run from a sibling dir)."""

    def test_same_repo_keeps_cwd(self):
        self.assertEqual(pc.attributed_session_cwd(_R, _R, []), (_R, None))

    def test_foreign_cwd_with_repo_relative_files_trusts_repo(self):
        sc, src = pc.attributed_session_cwd(_R, _FOREIGN, ["frontend/src/App.jsx", "openfde/server.py"])
        self.assertEqual((sc, src), (_R, _FOREIGN))

    def test_foreign_cwd_without_files_keeps_cwd(self):
        self.assertEqual(pc.attributed_session_cwd(_R, _FOREIGN, []), (_FOREIGN, None))

    def test_foreign_cwd_with_absolute_file_keeps_cwd(self):
        self.assertEqual(pc.attributed_session_cwd(_R, _FOREIGN, ["/elsewhere/x.py"]), (_FOREIGN, None))

    def test_mixed_files_one_outside_keeps_cwd(self):
        # ANY file that escapes the repo makes it ambiguous → never guess.
        self.assertEqual(pc.attributed_session_cwd(_R, _FOREIGN, ["frontend/App.jsx", "/abs/y.py"]),
                         (_FOREIGN, None))


class HealSessionCwdTest(unittest.TestCase):
    def _ep(self, **kw):
        e = {"episodeId": "P159", "status": "needs_manual_land", "sessionCwd": _FOREIGN,
             "files": ["frontend/src/App.jsx", "openfde/server.py"],
             "createdAt": "2026-06-21T00:00:00+00:00"}
        e.update(kw)
        return e

    def test_heals_repo_relative_and_preserves_raw(self):
        ep = self._ep()
        healed = pc.heal_session_cwd([ep], _R)
        self.assertEqual([e["episodeId"] for e in healed], ["P159"])
        self.assertEqual(ep["sessionCwd"], _R)
        self.assertEqual(ep["sourceCwd"], _FOREIGN)

    def test_does_not_heal_absolute_file_episode(self):
        ep = self._ep(files=["/elsewhere/x.py"])
        self.assertEqual(pc.heal_session_cwd([ep], _R), [])
        self.assertEqual(ep["sessionCwd"], _FOREIGN)
        self.assertNotIn("sourceCwd", ep)

    def test_does_not_heal_mixed_files(self):
        ep = self._ep(files=["frontend/App.jsx", "/abs/y.py"])
        self.assertEqual(pc.heal_session_cwd([ep], _R), [])
        self.assertEqual(ep["sessionCwd"], _FOREIGN)

    def test_does_not_heal_fileless_episode(self):
        ep = self._ep(files=[])
        self.assertEqual(pc.heal_session_cwd([ep], _R), [])

    def test_leaves_already_correct_episode(self):
        ep = self._ep(sessionCwd=_R)
        self.assertEqual(pc.heal_session_cwd([ep], _R), [])
        self.assertNotIn("sourceCwd", ep)

    def test_idempotent_second_pass_no_change(self):
        ep = self._ep()
        pc.heal_session_cwd([ep], _R)
        self.assertEqual(pc.heal_session_cwd([ep], _R), [])
        self.assertEqual(ep["sourceCwd"], _FOREIGN)


class CaptureEpisodeAttributionTest(unittest.TestCase):
    def test_cross_cwd_capture_trusts_watched_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = {"text": "do it", "cwd": "/foreign/dir", "key": "k", "sessionId": "s",
                      "timestamp": "2026-06-21T00:00:00Z"}
            ep = pc.make_capture_episode(root, prompt, files=["frontend/App.jsx"])
            self.assertEqual(ep["sessionCwd"], str(root))
            self.assertEqual(ep["sourceCwd"], "/foreign/dir")

    def test_same_repo_capture_keeps_cwd_without_sourceCwd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = {"text": "x", "cwd": str(root), "key": "k2", "sessionId": "s"}
            ep = pc.make_capture_episode(root, prompt, files=[])
            self.assertEqual(ep["sessionCwd"], str(root))
            self.assertNotIn("sourceCwd", ep)


class HealThenReconcileTest(unittest.TestCase):
    """The episode reconciles ONLY because its sessionCwd was corrected — the same-repo gate in
    reconcile_manual_land is unchanged (refuses before heal, accepts after)."""

    def test_reconcile_only_succeeds_after_heal(self):
        from openfde import episode_commits as ec
        ep = {"episodeId": "P159", "status": "needs_manual_land", "sessionCwd": _FOREIGN,
              "files": ["frontend/src/App.jsx", "openfde/server.py"],
              "createdAt": "2026-06-21T00:00:00+00:00"}
        commit = {"sha": "deadbee", "timestamp": "2026-06-21T02:00:00+00:00",
                  "files": ["frontend/src/App.jsx", "openfde/server.py", "x.py"]}
        # Before healing: foreign sessionCwd → the same-repo gate correctly refuses.
        self.assertEqual(ec.reconcile_manual_land([commit], [ep], watched_root=_R), {})
        self.assertEqual(ep["status"], "needs_manual_land")
        # Heal the attribution, then reconcile — now it attaches through the SAME gate.
        pc.heal_session_cwd([ep], _R)
        changed = ec.reconcile_manual_land([commit], [ep], watched_root=_R)
        self.assertEqual(set(changed), {"P159"})
        self.assertEqual(ep["status"], "landed")
        self.assertEqual(ep["commitShas"], ["deadbee"])


if __name__ == "__main__":
    unittest.main()
