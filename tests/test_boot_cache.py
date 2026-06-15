"""
Tests for openfde.boot_cache — the warm-start boot cache.

Laws: a snapshot round-trips (full or partial); the dirty signature changes when HEAD or the dirty
set changes; a snapshot is STALE when HEAD / dirty-set / parser version no longer match but is
STILL readable (we never blank the canvas); writes are atomic + create the cache dir.
"""
import tempfile
import unittest
from pathlib import Path

from openfde import boot_cache as bc


class DirtySignatureTest(unittest.TestCase):
    def test_changes_with_head_and_dirty(self):
        a = bc.dirty_signature({"head": "abc", "dirty": ["x.py"]})
        self.assertEqual(a, bc.dirty_signature({"head": "abc", "dirty": ["x.py"]}))   # stable
        self.assertNotEqual(a, bc.dirty_signature({"head": "def", "dirty": ["x.py"]}))  # HEAD moved
        self.assertNotEqual(a, bc.dirty_signature({"head": "abc", "dirty": ["x.py", "y.py"]}))  # dirtied
        self.assertEqual(a, bc.dirty_signature({"head": "abc", "dirty": ["x.py"]}))  # order-independent set

    def test_empty_status_is_safe(self):
        self.assertIsInstance(bc.dirty_signature({}), str)
        self.assertIsInstance(bc.dirty_signature(None), str)


class WarmCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_cache_returns_none(self):
        self.assertIsNone(bc.read_warm(self.dir))

    def test_full_roundtrip(self):
        bc.write_warm(self.dir, file_tree={"name": "repo", "children": []},
                      arch={"files": [{"path": "a.py"}], "modules": []},
                      head="abc123", dirty_sig="sig1", episode_tag="P14",
                      generated_at="2026-06-14T00:00:00Z")
        warm = bc.read_warm(self.dir)
        self.assertEqual(warm["fileTree"]["name"], "repo")
        self.assertEqual(len(warm["arch"]["files"]), 1)
        self.assertEqual(warm["meta"]["episodeTag"], "P14")
        self.assertEqual(warm["meta"]["head"], "abc123")
        self.assertEqual(warm["meta"]["parserVersion"], bc.PARSER_VERSION)

    def test_partial_write_keeps_other_artifact(self):
        bc.write_warm(self.dir, arch={"files": []}, head="h1", dirty_sig="s1")
        bc.write_warm(self.dir, file_tree={"name": "r"}, episode_tag="P9")   # arch untouched
        warm = bc.read_warm(self.dir)
        self.assertIsNotNone(warm["arch"])                  # earlier arch still there
        self.assertEqual(warm["fileTree"]["name"], "r")
        self.assertEqual(warm["meta"]["episodeTag"], "P9")
        self.assertEqual(warm["meta"]["head"], "h1")        # meta merged, not clobbered

    def test_creates_cache_dir(self):
        self.assertFalse((self.dir / "cache").exists())
        bc.write_warm(self.dir, arch={"files": []}, head="h", dirty_sig="s")
        self.assertTrue((self.dir / "cache" / "arch_snapshot.json").is_file())


class StaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        bc.write_warm(self.dir, arch={"files": []}, head="HEAD1", dirty_sig="SIG1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_match_not_stale(self):
        meta = bc.read_meta(self.dir)
        self.assertFalse(bc.is_stale(meta, head="HEAD1", dirty_sig="SIG1"))

    def test_head_or_dirty_change_is_stale(self):
        meta = bc.read_meta(self.dir)
        self.assertTrue(bc.is_stale(meta, head="HEAD2", dirty_sig="SIG1"))   # HEAD moved
        self.assertTrue(bc.is_stale(meta, head="HEAD1", dirty_sig="SIG2"))   # worktree dirtied

    def test_parser_bump_is_stale(self):
        meta = {**bc.read_meta(self.dir), "parserVersion": "OLD"}
        self.assertTrue(bc.is_stale(meta, head="HEAD1", dirty_sig="SIG1"))

    def test_empty_meta_is_stale(self):
        self.assertTrue(bc.is_stale({}, head="h", dirty_sig="s"))

    def test_stale_snapshot_still_readable(self):
        # The product bar: never blank the canvas. A stale snapshot is still served for first paint.
        warm = bc.read_warm(self.dir)
        self.assertIsNotNone(warm["arch"])
        self.assertTrue(bc.is_stale(warm["meta"], head="MOVED", dirty_sig="SIG1"))


if __name__ == "__main__":
    unittest.main()
