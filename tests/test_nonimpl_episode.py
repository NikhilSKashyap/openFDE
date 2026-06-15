"""
Meta-by-effect episode classification — keep docs-only / no-commit turns off the Story spine.

The law: an episode that landed NO commits and whose every file is gitignored (demo
scripts, ROADMAP/FLOW, .openfde/**) is a real prompt that changed nothing in the codebase.
It stays in the Events layer but must not be a Story beat. The verdict is stamped by
``persistence.flag_nonimplementation_episodes`` (needs git) and honored by
``prompt_story.is_operational_episode`` (pure). Any commit, or any tracked file (code, tests,
README), keeps the episode on the spine.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde import git_timeline as gt
from openfde.persistence import Persistence
from openfde.prompt_story import build_story_map, is_operational_episode


class NonImplementationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._g("init", "-q")
        self._g("config", "user.email", "t@e.com")
        self._g("config", "user.name", "T")
        # *.md is gitignored except README.md — mirrors the real repo's docs policy.
        (self.root / ".gitignore").write_text(".openfde/\n*.md\n!README.md\n")
        self.p = Persistence(self.root / ".openfde")

    def tearDown(self):
        self.tmp.cleanup()

    def _g(self, *a):
        return subprocess.run(["git", *a], cwd=str(self.root), capture_output=True, text=True)

    # ── the git seam ────────────────────────────────────────────────────────
    def test_ignored_paths_separates_docs_from_code(self):
        got = gt.ignored_paths(self.root,
                               ["DEMO1.md", "ROADMAP.md", "README.md", "openfde/x.py", ".openfde/y"])
        self.assertEqual(got, {"DEMO1.md", "ROADMAP.md", ".openfde/y"})

    def test_ignored_paths_is_fail_open_off_git(self):
        with tempfile.TemporaryDirectory() as d:          # not a git repo
            self.assertEqual(gt.ignored_paths(Path(d), ["DEMO1.md"]), set())
        self.assertEqual(gt.ignored_paths(self.root, []), set())

    # ── the verdict ─────────────────────────────────────────────────────────
    def _eps(self):
        return [
            {"episodeId": "demo", "sequence": 1, "signal": "product", "files": ["DEMO1.md"]},
            {"episodeId": "docs", "sequence": 2, "signal": "product",
             "files": ["ROADMAP.md", "FLOW.md"]},
            {"episodeId": "code", "sequence": 3, "signal": "product", "files": ["openfde/x.py"]},
            {"episodeId": "mixed", "sequence": 4, "signal": "product",
             "files": ["DEMO1.md", "openfde/x.py"]},
            {"episodeId": "readme", "sequence": 5, "signal": "product", "files": ["README.md"]},
            {"episodeId": "committed", "sequence": 6, "signal": "product",
             "files": ["DEMO1.md"], "commitShas": ["abc123"]},
            {"episodeId": "nofiles", "sequence": 7, "signal": "product", "files": []},
            {"episodeId": "chatter", "sequence": 8, "signal": "operational", "files": ["DEMO1.md"]},
        ]

    def test_flag_marks_only_docs_only_no_commit(self):
        eps = self.p.flag_nonimplementation_episodes(self.root, self._eps())
        flag = {e["episodeId"]: e.get("nonImplementation") for e in eps}
        self.assertTrue(flag["demo"])                      # all gitignored, no commit
        self.assertTrue(flag["docs"])                      # ROADMAP + FLOW, no commit
        self.assertFalse(flag["code"])                     # tracked .py
        self.assertFalse(flag["mixed"])                    # not ALL ignored
        self.assertFalse(flag["readme"])                   # README is tracked
        self.assertFalse(flag["committed"])                # committed → implementation
        self.assertFalse(flag["nofiles"])                  # nothing touched
        # operational-by-content is left entirely untouched (already hidden, no git spent)
        chatter = next(e for e in eps if e["episodeId"] == "chatter")
        self.assertNotIn("nonImplementation", chatter)

    def test_flag_is_idempotent(self):
        once = self.p.flag_nonimplementation_episodes(self.root, self._eps())
        snap = {e["episodeId"]: e.get("nonImplementation") for e in once}
        twice = self.p.flag_nonimplementation_episodes(self.root, once)
        self.assertEqual({e["episodeId"]: e.get("nonImplementation") for e in twice}, snap)

    def test_recompute_flips_when_a_commit_lands(self):
        eps = self.p.flag_nonimplementation_episodes(self.root, self._eps())
        demo = next(e for e in eps if e["episodeId"] == "demo")
        self.assertTrue(demo["nonImplementation"])
        demo["commitShas"] = ["deadbee"]                   # the docs episode later commits
        eps = self.p.flag_nonimplementation_episodes(self.root, eps)
        self.assertFalse(next(e for e in eps if e["episodeId"] == "demo")["nonImplementation"])

    # ── the predicate + the spine ───────────────────────────────────────────
    def test_is_operational_honors_the_flag(self):
        self.assertTrue(is_operational_episode({"nonImplementation": True}))
        self.assertFalse(is_operational_episode({"nonImplementation": False}))
        self.assertFalse(is_operational_episode({}))
        # existing contract preserved
        self.assertTrue(is_operational_episode({"signal": "operational"}))
        self.assertTrue(is_operational_episode({"storyFacts": {"operational": True}}))

    def test_spine_excludes_meta_episodes(self):
        eps = self.p.flag_nonimplementation_episodes(self.root, self._eps())
        spine = {n["episodeId"] for n in build_story_map(eps, [])["spine"]}
        self.assertEqual(spine, {"code", "mixed", "readme", "committed", "nofiles"})
        self.assertNotIn("demo", spine)
        self.assertNotIn("docs", spine)
        self.assertEqual(build_story_map(eps, [])["hiddenOps"], 3)  # demo + docs + chatter


if __name__ == "__main__":
    unittest.main()
