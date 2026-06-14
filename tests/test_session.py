"""
Tests for openfde.session — authoritative watched-repo identity + the port-collision
verdict that stops `openfde watch` from silently serving the wrong repo. Laws: identity
is canonical (git root / realpath, never project.json); a port held by the SAME canonical
repo → already_running, a DIFFERENT repo (even same basename) → wrong_repo.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from openfde import session


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


class SessionPayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "assignmentsignal"
        self.root.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@e.com")
        _git(self.root, "config", "user.name", "T")
        (self.root / "f.py").write_text("x = 1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_payload_is_canonical_identity(self):
        s = session.session_payload(self.root, "2026-06-14T00:00:00Z", "9.9.9")
        self.assertEqual(s["repoName"], "assignmentsignal")          # dir name, NOT metadata
        self.assertEqual(s["repoRoot"], str(self.root))
        self.assertEqual(os.path.realpath(s["gitRoot"]), os.path.realpath(str(self.root)))
        self.assertEqual(s["openfdeVersion"], "9.9.9")
        self.assertEqual(s["startedAt"], "2026-06-14T00:00:00Z")
        self.assertTrue(s["branch"])                                 # a real branch name

    def test_non_git_path_has_null_gitroot(self):
        d = Path(self.tmp.name) / "plain"
        d.mkdir()
        s = session.session_payload(d, "t", "1.0")
        self.assertIsNone(s["gitRoot"])
        self.assertEqual(s["repoName"], "plain")


class PortCollisionVerdictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-q")

    def tearDown(self):
        self.tmp.cleanup()

    def test_free_port(self):
        self.assertEqual(session.port_collision_verdict(None, self.root)[0], "free")

    def test_same_repo_already_running(self):
        v, detail = session.port_collision_verdict({"root": str(self.root), "name": "repo"}, self.root)
        self.assertEqual(v, "already_running")
        self.assertEqual(detail, "repo")

    def test_different_repo_is_wrong_repo(self):
        other = Path(self.tmp.name) / "openfde"
        other.mkdir()
        v, detail = session.port_collision_verdict({"root": str(other), "name": "openfde"}, self.root)
        self.assertEqual(v, "wrong_repo")
        self.assertIn("openfde", detail)

    def test_same_basename_different_path_is_wrong_repo(self):
        # /elsewhere/repo vs /tmp/.../repo — same folder NAME, different canonical repo.
        twin = Path(self.tmp.name) / "elsewhere" / "repo"
        twin.mkdir(parents=True)
        self.assertEqual(
            session.port_collision_verdict({"root": str(twin), "name": "repo"}, self.root)[0],
            "wrong_repo")

    def test_unknown_identity_refuses_conservatively(self):
        # A pre-/api/session OpenFDE server: we know it's OpenFDE (it answered /api/files)
        # but can't compare its repo canonically. Never guess it's safe — refuse loudly and
        # tell the user to restart it so it can prove its identity.
        v, detail = session.port_collision_verdict(
            {"root": None, "name": "mystery", "unknownIdentity": True}, self.root)
        self.assertEqual(v, "wrong_repo")
        self.assertIn("mystery", detail)
        self.assertIn("restart", detail.lower())


if __name__ == "__main__":
    unittest.main()
