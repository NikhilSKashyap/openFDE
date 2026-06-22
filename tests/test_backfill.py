"""
Tests for openfde.backfill — reconstruct historical prompt episodes from local Claude Code
transcripts when OpenFDE starts late. Laws: a human prompt with a nearby commit imports at HIGH
confidence as a real (numbered) episode; edited-but-uncommitted and no-evidence prompts are
QUARANTINED as backfill candidates (no P<n>, not in the rail/Story/OpenPM) — importable later;
OpenFDE-internal prompts are skipped; import is idempotent (captureKey, including candidates);
never commits.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde import backfill
from openfde.persistence import Persistence
from openfde.prompt_capture import claude_projects_dir
from openfde.episode_llm_summary import INTERNAL_MARKER


def _git(root, *args, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=e)


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir(parents=True)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@e.com")
        _git(self.root, "config", "user.name", "T")
        (self.root / "seed.py").write_text("x = 1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    # ── transcript builders ──
    def _transcript(self, name, entries):
        d = claude_projects_dir(self.root, self.home)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def _prompt(self, uuid, text, ts, sid="s1"):
        return {"type": "user", "uuid": uuid, "sessionId": sid, "timestamp": ts,
                "cwd": str(self.root), "message": {"role": "user", "content": text}}

    def _edit(self, sid, rel):
        return {"type": "assistant", "sessionId": sid, "uuid": "a-" + rel,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Write",
                     "input": {"file_path": str(self.root / rel)}}]}}

    def _commit(self, rel, content, date):
        (self.root / rel).write_text(content)
        _git(self.root, "add", rel)
        _git(self.root, "commit", "-qm", f"add {rel}",
             env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()

    def _backfilled(self):
        return [e for e in self.p.load_episodes() if e.get("source") == "openfde-backfill"]

    def _candidates(self):
        return self.p.load_backfill_candidates()

    # ── cross-cwd transcript builders (a session rooted in ANOTHER repo) ──
    def _cross_transcript(self, name, entries, other):
        d = claude_projects_dir(other, self.home)        # transcript lives in other cwd's dir
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def _cross_prompt(self, uuid, text, ts, other, sid="s2"):
        return {"type": "user", "uuid": uuid, "sessionId": sid, "timestamp": ts,
                "cwd": str(other),                       # rooted elsewhere, NOT the watched repo
                "message": {"role": "user", "content": text}}

    # ── tests ──
    def test_imports_and_links_commit_high_confidence(self):
        # A commit-linked prompt is the ONLY thing that becomes a real numbered episode.
        self._transcript("t1.jsonl", [
            self._prompt("u1", "build the calc module", "2026-06-10T00:00:00Z"),
            self._edit("s1", "calc.py")])
        sha = self._commit("calc.py", "def add():\n    return 1\n", "2026-06-10T00:05:00Z")
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["landed"], 1)
        ep, = self._backfilled()
        self.assertEqual(ep["status"], "landed")
        self.assertEqual(ep["commitShas"], [sha])              # high-confidence link
        self.assertEqual(ep["kind"], "claude-code")
        self.assertEqual(ep["createdAt"], "2026-06-10T00:00:00Z")   # timestamp preserved
        self.assertTrue(ep["historical"])
        self.assertEqual(ep["backfillConfidence"], "high")
        self.assertTrue(ep.get("tag"))                         # gets a real P<n>

    def test_idempotent_on_rerun(self):
        self._transcript("t1.jsonl", [self._prompt("u1", "hi there", "2026-06-10T00:00:00Z")])
        r1 = backfill.backfill_historical(self.root, self.p, home=self.home)
        r2 = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(r1["candidates"], 1)                  # discussion → quarantined candidate
        self.assertEqual(r2["candidates"], 0)                  # captureKey dedup (incl. candidates)
        self.assertEqual(len(self._candidates()), 1)
        self.assertEqual(self._backfilled(), [])               # never a numbered episode

    def test_skips_openfde_internal_prompts(self):
        self._transcript("t1.jsonl", [
            self._prompt("u1", INTERNAL_MARKER + "\n\nsummarize this episode", "2026-06-10T00:00:00Z"),
            self._prompt("u2", "a real human prompt", "2026-06-10T00:01:00Z")])
        backfill.backfill_historical(self.root, self.p, home=self.home)
        cands = self._candidates()
        self.assertEqual(len(cands), 1)                        # internal skipped; real one quarantined
        self.assertIn("a real human prompt", cands[0]["prompt"])

    def test_no_evidence_is_quarantined_as_discussion_candidate(self):
        self._transcript("t1.jsonl", [self._prompt("u1", "what do you think we should do?", "2026-06-10T00:00:00Z")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["discussion"], 1)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(self._backfilled(), [])               # NOT an episode → no P<n>
        cand, = self._candidates()
        self.assertEqual(cand["backfillConfidence"], "discussion")
        self.assertEqual(cand["files"], [])
        self.assertNotIn("sequence", cand)                     # never numbered
        self.assertNotIn("tag", cand)

    def test_edited_but_uncommitted_is_needs_review_candidate(self):
        self._transcript("t1.jsonl", [
            self._prompt("u1", "edit the ghost file", "2026-06-10T00:00:00Z"),
            self._edit("s1", "ghost.py")])                     # edited, never committed
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["needsReview"], 1)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(self._backfilled(), [])               # quarantined, not numbered
        cand, = self._candidates()
        self.assertEqual(cand["backfillConfidence"], "needs_review")
        self.assertEqual(cand["files"], ["ghost.py"])
        self.assertEqual(cand["commitShas"], [])

    def test_emits_quiet_receipt_event(self):
        self._transcript("t1.jsonl", [self._prompt("u1", "do a thing", "2026-06-10T00:00:00Z")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertIn("event", res)
        self.assertEqual(res["event"]["type"], "backfill_imported")
        self.assertIn("quarantined 1 transcript candidate", res["event"]["payload"]["detail"])

    # ── cwd attribution accuracy (the fix) ──
    def test_same_cwd_discussion_prompt_is_quarantined(self):
        # A session rooted AT the watched repo with no edits is still discussion → a candidate,
        # never a numbered episode.
        self._transcript("t1.jsonl", [
            self._prompt("u1", "should we split the calc module?", "2026-06-10T00:00:00Z")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(res["discussion"], 1)
        self.assertEqual(self._backfilled(), [])
        self.assertEqual(len(self._candidates()), 1)

    def test_cross_cwd_discussion_without_edits_is_ignored(self):
        # THE BUG: a session rooted ELSEWHERE that only discussed (no edits under this
        # repo) must NOT import at all — unrelated /csvflow-demo, /interview chatter stays out.
        other = Path(self.tmp.name) / "other-repo"
        self._cross_transcript("x.jsonl", [
            self._cross_prompt("u9", "how should I structure this csv loader?",
                               "2026-06-10T00:00:00Z", other)], other)
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 0)
        self.assertEqual(res["candidates"], 0)
        self.assertEqual(self._backfilled(), [])
        self.assertEqual(self._candidates(), [])

    def test_cross_cwd_prompt_with_repo_edits_is_needs_review_candidate(self):
        # A session rooted elsewhere that edits a file UNDER this repo is real work here, but
        # without a commit it stays a needs_review CANDIDATE (no P<n>); idempotent on rerun.
        other = Path(self.tmp.name) / "other-repo"
        self._cross_transcript("x.jsonl", [
            self._cross_prompt("u8", "patch the shared util", "2026-06-10T00:00:00Z", other),
            self._edit("s2", "util.py")], other)           # file_path is under self.root
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(res["needsReview"], 1)
        self.assertEqual(self._backfilled(), [])
        cand, = self._candidates()
        self.assertEqual(cand["files"], ["util.py"])
        self.assertEqual(cand["backfillConfidence"], "needs_review")
        self.assertEqual(cand["kind"], "claude-code")
        res2 = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res2["candidates"], 0)                # dedup vs candidates
        self.assertEqual(len(self._candidates()), 1)

    # ── canonical repo identity: basename / sibling / subdir / Codex ──
    def test_same_basename_different_path_is_ignored(self):
        # Same folder NAME, different path → a different repo. Basename is never evidence.
        twin = Path(self.tmp.name) / "elsewhere" / self.root.name   # same basename, other path
        self._cross_transcript("b.jsonl", [
            self._cross_prompt("u7", "what's our plan here?", "2026-06-10T00:00:00Z", twin)], twin)
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 0)
        self.assertEqual(self._backfilled(), [])
        self.assertEqual(self._candidates(), [])

    def test_claude_projects_sibling_repo_does_not_bleed(self):
        # A sibling repo in the same workspace (shared parent) is a different repo.
        sibling = self.root.parent / "sibling-proj"
        self._cross_transcript("s.jsonl", [
            self._cross_prompt("u6", "discuss the sibling roadmap", "2026-06-10T00:00:00Z", sibling)], sibling)
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 0)
        self.assertEqual(self._candidates(), [])

    def test_subdir_session_is_same_repo(self):
        # A session rooted in a SUBDIR of the watched repo is the same git repo → considered,
        # but discussion-only → quarantined candidate (canonical git-root match, not exact path).
        sub = self.root / "pkg"
        sub.mkdir()
        self._cross_transcript("sub.jsonl", [
            self._cross_prompt("u5", "how is pkg structured?", "2026-06-10T00:00:00Z", sub)], sub)
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(len(self._candidates()), 1)
        self.assertEqual(self._backfilled(), [])

    # ── Codex fixtures (cwd-exact) ──
    @staticmethod
    def _cdx_user(text, pid="p1"):
        return {"type": "response_item", "id": pid, "timestamp": "2026-06-09T00:00:01Z",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": text}]}}

    def _codex_transcript(self, uuid, cwd, entries):
        from openfde.prompt_capture import codex_sessions_root
        d = codex_sessions_root(self.home) / "2026" / "06" / "09"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"rollout-2026-06-09T00-00-00-{uuid}.jsonl"
        meta = {"type": "session_meta", "timestamp": "2026-06-09T00:00:00Z",
                "payload": {"id": uuid, "cwd": str(cwd)}}
        path.write_text("\n".join(json.dumps(e) for e in ([meta] + entries)) + "\n")
        return path

    def test_codex_cross_cwd_is_ignored(self):
        # Codex stays cwd-exact (canonical): another repo with no file evidence → out.
        other = Path(self.tmp.name) / "other-repo"
        self._codex_transcript("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", other,
                               [self._cdx_user("codex discussion in another repo")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 0)
        self.assertEqual(res["candidates"], 0)

    def test_codex_same_cwd_discussion_is_candidate(self):
        # Codex rooted at the watched repo with no edits → quarantined candidate (cwd-exact match).
        self._codex_transcript("11111111-2222-3333-4444-555555555555", self.root,
                               [self._cdx_user("codex work in the watched repo", pid="p2")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(self._candidates()[0]["kind"], "codex")
        self.assertEqual(self._backfilled(), [])


class BackfillAttributionTest(unittest.TestCase):
    """A backfilled episode follows the same cross-cwd attribution as live capture: edits under
    the watched repo win over a foreign transcript cwd, raw cwd preserved as sourceCwd."""

    def test_cross_cwd_backfill_trusts_watched_repo(self):
        from openfde.backfill import _historical_episode
        prompt = {"text": "x", "cwd": "/foreign", "key": "k", "sessionId": "s",
                  "timestamp": "2026-06-21T00:00:00Z"}
        ep = _historical_episode("/repo", prompt, ["frontend/App.jsx"], None, "claude-code")
        self.assertEqual(ep["sessionCwd"], "/repo")
        self.assertEqual(ep["sourceCwd"], "/foreign")
        self.assertEqual(ep["status"], "needs_manual_land")

    def test_absolute_file_backfill_keeps_cwd(self):
        from openfde.backfill import _historical_episode
        prompt = {"text": "x", "cwd": "/foreign", "key": "k", "sessionId": "s"}
        ep = _historical_episode("/repo", prompt, ["/elsewhere/x.py"], None, "claude-code")
        self.assertEqual(ep["sessionCwd"], "/foreign")
        self.assertNotIn("sourceCwd", ep)


if __name__ == "__main__":
    unittest.main()
