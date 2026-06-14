"""
Tests for openfde.memory_kit — the `.openfde/` markdown memory room. Laws under test:
bootstrap creates the kit under `.openfde/` (never the repo root, never the tracked
tree); user-editable files are created once and never overwritten; generated files
(COUNCIL/BRIEF) are rewritten; COUNCIL groups the ledger by role.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde import memory_kit as M
from openfde.persistence import Persistence


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True).stdout.strip()


def _repo():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "x@x.com")
    _git(root, "config", "user.name", "t")
    (root / "code.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    # Mimic the server: exclude .openfde from git locally (never the tracked .gitignore).
    (root / ".git" / "info" / "exclude").write_text(".openfde/\n")
    return d, root, Persistence(root / ".openfde")


class RenderCouncilTest(unittest.TestCase):
    def test_groups_entries_by_role_with_all_buckets(self):
        log = [{"role": "user", "text": "build the thing"},
               {"role": "architect", "summary": "scope it small"},
               {"role": "senior_dev", "text": "implemented it"},
               {"role": "verifier", "text": "tests pass"},
               {"role": "weird", "text": "unknown role lands in council"}]
        md = M.render_council_md(log)
        for header in ("## user", "## architect", "## sr dev", "## verifier", "## council"):
            self.assertIn(header, md)
        self.assertIn("build the thing", md)
        self.assertIn("scope it small", md)            # summary used when no text
        self.assertIn("unknown role lands in council", md)   # unknown → council bucket

    def test_empty_log_still_renders_all_headers(self):
        md = M.render_council_md([])
        self.assertIn("## user", md)
        self.assertIn("_(nothing yet)_", md)


class RenderBriefTest(unittest.TestCase):
    def test_brief_includes_flow_taste_and_decisions_now(self):
        md = M.render_brief_md(
            brief_text="Active episode: P3 (reviewing)",
            flow_md="# FLOW\n\n## How work flows\n- ship small\n",
            taste_md="# TASTE\n\n## Code\n- terse names\n",
            decisions_md="# DECISIONS\n\n## Now\nuse pytest\n\n## Next\nlater\n")
        self.assertIn("Active episode: P3", md)
        self.assertIn("ship small", md)
        self.assertIn("terse names", md)
        self.assertIn("use pytest", md)                # Decisions → Now extracted
        self.assertNotIn("later", md)                  # Next is not the Now section


class BootstrapTest(unittest.TestCase):
    def test_creates_kit_under_openfde_only_and_keeps_tracked_repo_clean(self):
        d, root, p = _repo()
        with d:
            (p.dir).mkdir(parents=True, exist_ok=True)
            (p.dir / "project_log.jsonl").write_text(
                json.dumps({"role": "user", "text": "do the work"}) + "\n")
            res = M.bootstrap_memory_kit(p, root)

            for name in ("FLOW.md", "TASTE.md", "DECISIONS.md", "COUNCIL.md", "BRIEF.md"):
                self.assertTrue((p.dir / name).exists(), f".openfde/{name} missing")
                self.assertFalse((root / name).exists(), f"{name} leaked to repo root")
            self.assertEqual(set(res["created"]), {"FLOW.md", "TASTE.md", "DECISIONS.md"})
            self.assertEqual(set(res["regenerated"]), {"COUNCIL.md", "BRIEF.md"})
            # The tracked repo must be completely clean (.openfde is excluded).
            self.assertEqual(_git(root, "status", "--porcelain"), "")
            self.assertIn("do the work", (p.dir / "COUNCIL.md").read_text())

    def test_does_not_overwrite_user_edits_but_regenerates_generated(self):
        d, root, p = _repo()
        with d:
            M.bootstrap_memory_kit(p, root)
            (p.dir / "FLOW.md").write_text("# my custom flow\n")          # user edit
            (p.dir / "project_log.jsonl").write_text(
                json.dumps({"role": "architect", "text": "fresh decision"}) + "\n")
            res2 = M.bootstrap_memory_kit(p, root)
            self.assertEqual(res2["created"], [])                          # nothing re-created
            self.assertEqual((p.dir / "FLOW.md").read_text(), "# my custom flow\n")  # preserved
            self.assertIn("fresh decision", (p.dir / "COUNCIL.md").read_text())      # regenerated


if __name__ == "__main__":
    unittest.main()
