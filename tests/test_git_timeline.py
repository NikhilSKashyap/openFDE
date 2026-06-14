"""
Tests for openfde.git_timeline.worktree_impact — the non-mutating "Review Delta"
data source (the Review leg of Land · Watch · Review).

Builds a tiny temp git repo and asserts the helper surfaces the uncommitted work
tree as an architecture delta **without ever staging** (the core safety guarantee):
git's staging area must be byte-identical before and after the call.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde import git_timeline as gt
from openfde import semantic_graph as sg


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
    ).stdout


class WorktreeImpactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "Test")
        _git(self.root, "config", "commit.gpgsign", "false")
        # A .gitignore already at steady state (every entry OpenFDE would add) so the
        # first git_commit's ensure_git_repo() doesn't modify it and add commit noise.
        (self.root / ".gitignore").write_text("\n".join(gt._IGNORE_ENTRIES) + "\n")
        # Two tracked files committed at HEAD.
        (self.root / "alpha.py").write_text("def a():\n    return 1\n")
        (self.root / "beta.py").write_text("def b():\n    return 2\n")
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=Test",
             "commit", "-q", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def _porcelain(self) -> str:
        return _git(self.root, "status", "--porcelain", "--untracked-files=all")

    def _staged(self) -> str:
        return _git(self.root, "diff", "--cached", "--name-only")

    # 1) A tracked-file edit appears with a patch and per-file stat.
    def test_tracked_edit_has_patch_and_stat(self):
        (self.root / "alpha.py").write_text("def a():\n    return 11\n")
        imp = gt.worktree_impact(self.root)
        self.assertTrue(imp["dirty"])
        paths = [f["path"] for f in imp["files"]]
        self.assertIn("alpha.py", paths)
        entry = next(f for f in imp["files"] if f["path"] == "alpha.py")
        self.assertEqual(entry["status"], "M")
        self.assertGreaterEqual(entry["additions"], 1)
        self.assertIn("alpha.py", imp["patch"])
        self.assertEqual(imp["stat"]["files"], imp["fileCount"])

    # 2) An untracked file appears as status "?" and is NOT staged (no patch body).
    def test_untracked_file_listed_without_staging(self):
        (self.root / "gamma.py").write_text("def g():\n    return 3\n")
        before_staged = self._staged()
        imp = gt.worktree_impact(self.root)
        self.assertIn("gamma.py", imp["untracked"])
        entry = next(f for f in imp["files"] if f["path"] == "gamma.py")
        self.assertEqual(entry["status"], "?")
        # Untracked content must not be emitted as a patch (that would need staging).
        self.assertNotIn("def g()", imp["patch"])
        # Staging area unchanged by the call.
        self.assertEqual(self._staged(), before_staged)
        self.assertEqual(before_staged.strip(), "")

    # 3) THE GUARANTEE: git state (porcelain + staging) is identical before/after.
    def test_helper_does_not_mutate_git_state(self):
        (self.root / "alpha.py").write_text("def a():\n    return 99\n")
        (self.root / "delta.py").write_text("x = 1\n")          # untracked too
        before_porc, before_staged = self._porcelain(), self._staged()
        gt.worktree_impact(self.root)
        gt.worktree_impact(self.root)                            # idempotent / repeatable
        self.assertEqual(self._porcelain(), before_porc)
        self.assertEqual(self._staged(), before_staged)
        self.assertEqual(before_staged.strip(), "")             # nothing ever staged

    # 4) Clean tree → not dirty, no files, stable empty signature semantics.
    def test_clean_tree_not_dirty(self):
        imp = gt.worktree_impact(self.root)
        self.assertFalse(imp["dirty"])
        self.assertEqual(imp["files"], [])
        self.assertEqual(imp["fileCount"], 0)

    # 5) Signature changes with content and is stable for identical states.
    def test_signature_tracks_dirty_state(self):
        clean = gt.worktree_impact(self.root)["signature"]
        (self.root / "alpha.py").write_text("def a():\n    return 7\n")
        dirty1 = gt.worktree_impact(self.root)["signature"]
        dirty2 = gt.worktree_impact(self.root)["signature"]
        self.assertNotEqual(clean, dirty1)
        self.assertEqual(dirty1, dirty2)                        # stable → debounce key

    # 6) File-count cap reported via fileCount > shownCount (no silent truncation).
    def test_file_count_cap_reported(self):
        for i in range(5):
            (self.root / f"extra_{i}.py").write_text(f"v = {i}\n")
        imp = gt.worktree_impact(self.root, max_files=3)
        self.assertEqual(imp["shownCount"], 3)
        self.assertGreater(imp["fileCount"], imp["shownCount"])

    # 7) Full Review-Delta data path: changed files map to a semantic-graph concept.
    def test_changed_files_resolve_to_concepts(self):
        # A cross-file tether: both files reference the same identifier.
        (self.root / "alpha.py").write_text('TOKEN = "shared-thing"\ndef a():\n    return 1\n')
        (self.root / "beta.py").write_text('NAME = "shared-thing"\ndef b():\n    return 2\n')
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=Test",
             "commit", "-q", "-m", "tether")
        graph = sg.build_graph(self.root)
        # Edit one side only → a partial touch of the concept.
        (self.root / "alpha.py").write_text('TOKEN = "shared-thing"\ndef a():\n    return 100\n')
        imp = gt.worktree_impact(self.root)
        files = [f["path"] for f in imp["files"]]
        concepts = sg.concepts_for_files(graph, files)
        self.assertTrue(any(c["identifier"] == "shared-thing" for c in concepts))

    # 8) OpenFDE-Episode trailer round-trips: git_commit writes it, git_timeline reads it.
    def test_episode_trailer_round_trips(self):
        (self.root / "alpha.py").write_text("def a():\n    return 5\n")
        res = gt.git_commit(self.root, "openfde: add login",
                            detail="Implements login.",
                            trailers={"OpenFDE-Episode": "episode_abc123",
                                      "OpenFDE-Run": "council_xyz"})
        self.assertTrue(res["committed"])
        tl = gt.git_timeline(self.root, limit=5)
        landed = tl[0]
        self.assertEqual(landed["episodeId"], "episode_abc123")
        self.assertEqual(landed["trailers"].get("OpenFDE-Run"), "council_xyz")
        self.assertEqual(landed["summary"], "openfde: add login")

    # 9) A plain commit (no trailer) has no episodeId → it buckets under Outside OpenFDE.
    def test_plain_commit_has_no_episode(self):
        (self.root / "beta.py").write_text("def b():\n    return 9\n")
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=Test",
             "commit", "-q", "-m", "manual edit")
        tl = gt.git_timeline(self.root, limit=5)
        self.assertIsNone(tl[0]["episodeId"])

    # 10) commit_files lists exactly the paths a commit touched (rail nested chips).
    def test_commit_files_lists_touched_paths(self):
        (self.root / "alpha.py").write_text("def a():\n    return 3\n")
        (self.root / "gamma.py").write_text("g = 1\n")              # new file in same commit
        res = gt.git_commit(self.root, "openfde: touch two files",
                            trailers={"OpenFDE-Episode": "episode_ff"})
        self.assertTrue(res["committed"])
        files = gt.commit_files(self.root, res["sha"])
        self.assertEqual(set(files), {"alpha.py", "gamma.py"})
        # Capping is honoured and never errors on a bad sha.
        self.assertEqual(len(gt.commit_files(self.root, res["sha"], cap=1)), 1)
        self.assertEqual(gt.commit_files(self.root, "nothex"), [])

    # 11) A Land-shaped commit encapsulates the work: subject + file manifest in body.
    def test_land_commit_subject_and_body_manifest(self):
        (self.root / "alpha.py").write_text("def a():\n    return 4\n")
        (self.root / "beta.py").write_text("def b():\n    return 5\n")
        body = ("Scope: (repo) · 2 files reviewed and landed\n"
                "- alpha.py\n- beta.py")
        res = gt.git_commit(self.root, "openfde: wire prompt story rail", detail=body,
                            trailers={"OpenFDE-Episode": "episode_land"})
        self.assertTrue(res["committed"])
        msg = _git(self.root, "log", "-1", "--pretty=%B")
        self.assertTrue(msg.startswith("openfde: "))
        self.assertIn("- alpha.py", msg)                            # manifest encapsulated
        self.assertIn("OpenFDE-Episode: episode_land", msg)         # provenance trailer
        self.assertEqual(set(gt.commit_files(self.root, res["sha"])), {"alpha.py", "beta.py"})

    # 12) Plural OpenFDE-Episodes trailer → episodeIds list; episodeId is the first (back-compat).
    def test_plural_episodes_trailer_parses(self):
        (self.root / "alpha.py").write_text("def a():\n    return 6\n")
        res = gt.git_commit(self.root, "openfde: batched change",
                            trailers={"OpenFDE-Episodes": "e1, e2, e3"})
        self.assertTrue(res["committed"])
        landed = gt.git_timeline(self.root, limit=5)[0]
        self.assertEqual(landed["episodeIds"], ["e1", "e2", "e3"])
        self.assertEqual(landed["episodeId"], "e1")             # primary, for single-episode consumers

    # 13) Many prompts → one commit, end-to-end against REAL git: a trailer-less commit touching two
    #     episodes' files reconciles onto BOTH (the same enrich-with-commit_files path the server uses).
    def test_one_commit_reconciles_onto_many_episodes(self):
        from openfde import episode_commits as ec
        (self.root / "alpha.py").write_text("def a():\n    return 21\n")
        (self.root / "beta.py").write_text("def b():\n    return 22\n")
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=Test",
             "commit", "-q", "-m", "manual: two changes in one commit")
        commit = gt.git_timeline(self.root, limit=5)[0]
        self.assertIsNone(commit["episodeId"])                  # no trailer → inference territory
        enriched = {**commit, "files": gt.commit_files(self.root, commit["sha"])}
        # Episodes captured in THIS repo (sessionCwd == watched root), at the commit's moment so
        # they're inside the capture window. Single-file → honest time_file_inferred confidence.
        ts = commit["timestamp"]
        episodes = [{"episodeId": "P1", "files": ["alpha.py"], "createdAt": ts,
                     "status": "reviewing", "sessionCwd": str(self.root)},
                    {"episodeId": "P2", "files": ["beta.py"], "createdAt": ts,
                     "status": "reviewing", "sessionCwd": str(self.root)}]
        changed = ec.reconcile_episodes([enriched], episodes, watched_root=str(self.root))
        self.assertEqual(set(changed), {"P1", "P2"})
        for ep in episodes:
            self.assertIn(commit["sha"], ep["commitShas"])
            self.assertEqual(ep["commitMeta"][commit["sha"]]["confidence"], "time_file_inferred")


class EpisodeStoreTest(unittest.TestCase):
    """The durable prompt-episode store (Prompt Story Rail backing)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from openfde.persistence import Persistence
        d = Path(self.tmp.name)
        d.mkdir(exist_ok=True)
        self.p = Persistence(d)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_get_and_link_by_run(self):
        ep = {"episodeId": "episode_1", "prompt": "add auth", "kind": "council",
              "status": "reviewing", "runIds": ["council_1"], "files": ["a.py"],
              "commitShas": [], "eventIds": [], "projectEntryIds": [], "summary": ""}
        self.p.upsert_episode(ep)
        self.assertEqual(self.p.get_episode("episode_1")["prompt"], "add auth")
        self.assertEqual(self.p.get_open_episode_for_run("council_1")["episodeId"], "episode_1")
        # Update keeps a single record (upsert by id), newest-first.
        ep["status"] = "landed"
        self.p.upsert_episode(ep)
        eps = self.p.load_episodes()
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["status"], "landed")


if __name__ == "__main__":
    unittest.main()
