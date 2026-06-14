"""
Tests for openfde.backfill — reconstruct historical prompt episodes from local Claude
Code transcripts when OpenFDE starts late. Laws: human prompts import as episodes
(source/timestamp preserved); a nearby commit links at high confidence (→ landed);
edited-but-uncommitted → needs_manual_land; no evidence → discussion; OpenFDE-internal
prompts are skipped; import is idempotent (captureKey); never commits.
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

    def test_idempotent_on_rerun(self):
        self._transcript("t1.jsonl", [self._prompt("u1", "hi there", "2026-06-10T00:00:00Z")])
        r1 = backfill.backfill_historical(self.root, self.p, home=self.home)
        r2 = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(r1["imported"], 1)
        self.assertEqual(r2["imported"], 0)                    # captureKey dedup
        self.assertEqual(len(self._backfilled()), 1)

    def test_skips_openfde_internal_prompts(self):
        self._transcript("t1.jsonl", [
            self._prompt("u1", INTERNAL_MARKER + "\n\nsummarize this episode", "2026-06-10T00:00:00Z"),
            self._prompt("u2", "a real human prompt", "2026-06-10T00:01:00Z")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 1)
        ep, = self._backfilled()
        self.assertIn("a real human prompt", ep["prompt"])

    def test_no_evidence_imports_as_discussion(self):
        self._transcript("t1.jsonl", [self._prompt("u1", "what do you think we should do?", "2026-06-10T00:00:00Z")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["discussion"], 1)
        ep, = self._backfilled()
        self.assertEqual(ep["status"], "open")
        self.assertEqual(ep["backfillConfidence"], "discussion")
        self.assertEqual(ep["files"], [])
        self.assertEqual(ep["commitShas"], [])                 # never invents a commit

    def test_edited_but_uncommitted_is_needs_review(self):
        self._transcript("t1.jsonl", [
            self._prompt("u1", "edit the ghost file", "2026-06-10T00:00:00Z"),
            self._edit("s1", "ghost.py")])                     # edited, never committed
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["needsReview"], 1)
        ep, = self._backfilled()
        self.assertEqual(ep["status"], "needs_manual_land")
        self.assertEqual(ep["files"], ["ghost.py"])
        self.assertEqual(ep["commitShas"], [])

    def test_emits_quiet_receipt_event(self):
        self._transcript("t1.jsonl", [self._prompt("u1", "do a thing", "2026-06-10T00:00:00Z")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertIn("event", res)
        self.assertEqual(res["event"]["type"], "backfill_imported")
        self.assertIn("Imported 1 historical prompt", res["event"]["payload"]["detail"])

    # ── cwd attribution accuracy (the fix) ──
    def test_same_cwd_discussion_prompt_imports(self):
        # A session rooted AT the watched repo imports human prompts even with no edits.
        self._transcript("t1.jsonl", [
            self._prompt("u1", "should we split the calc module?", "2026-06-10T00:00:00Z")])
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["discussion"], 1)
        self.assertEqual(self._backfilled()[0]["status"], "open")

    def test_cross_cwd_discussion_without_edits_is_ignored(self):
        # THE BUG: a session rooted ELSEWHERE that only discussed (no edits under this
        # repo) must NOT import — unrelated /csvflow-demo, /interview chatter stays out.
        other = Path(self.tmp.name) / "other-repo"
        self._cross_transcript("x.jsonl", [
            self._cross_prompt("u9", "how should I structure this csv loader?",
                               "2026-06-10T00:00:00Z", other)], other)
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 0)
        self.assertEqual(self._backfilled(), [])

    def test_cross_cwd_prompt_with_repo_edits_imports_as_needs_review(self):
        # A session rooted elsewhere that edits a file UNDER this repo is real work here →
        # import as needs_review (high-signal cross-cwd attribution); idempotent on rerun.
        other = Path(self.tmp.name) / "other-repo"
        self._cross_transcript("x.jsonl", [
            self._cross_prompt("u8", "patch the shared util", "2026-06-10T00:00:00Z", other),
            self._edit("s2", "util.py")], other)           # file_path is under self.root
        res = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["needsReview"], 1)
        ep, = self._backfilled()
        self.assertEqual(ep["status"], "needs_manual_land")
        self.assertEqual(ep["files"], ["util.py"])
        self.assertEqual(ep["backfillConfidence"], "needs_review")
        self.assertEqual(ep["kind"], "claude-code")
        # idempotency still holds on the cross-cwd path
        res2 = backfill.backfill_historical(self.root, self.p, home=self.home)
        self.assertEqual(res2["imported"], 0)
        self.assertEqual(len(self._backfilled()), 1)


if __name__ == "__main__":
    unittest.main()
