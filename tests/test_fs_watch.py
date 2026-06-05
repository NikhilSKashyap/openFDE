"""
Tests for openfde.fs_watch — the "Watch Any Agent" filesystem watcher.

Covers the deterministic core: snapshot/diff detects modified and newly-created
source files, ignores editor temp/backup noise, and the async loop broadcasts a
`file_activity` event on an external edit while staying silent at baseline and
while a council run is "active".
"""

import asyncio
import time
import tempfile
import unittest
from pathlib import Path

from openfde import fs_watch


class FsWatchSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.py").write_text("x = 1\n")
        (self.root / "web.jsx").write_text("export const x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_diff_detects_modify_and_create(self):
        snap1 = fs_watch._snapshot(self.root)
        self.assertEqual(len(snap1), 2)
        # modify a.py (bump mtime explicitly — fast tests can share a tick)
        p = self.root / "a.py"
        p.write_text("x = 2\n")
        import os
        os.utime(p, (time.time() + 2, time.time() + 2))
        # create a new file
        (self.root / "b.py").write_text("y = 3\n")
        snap2 = fs_watch._snapshot(self.root)
        changed = fs_watch.detect_changes(snap1, snap2)
        self.assertIn(str(self.root / "a.py"), changed)
        self.assertIn(str(self.root / "b.py"), changed)
        self.assertNotIn(str(self.root / "web.jsx"), changed)

    def test_ignores_editor_temp_and_backup(self):
        (self.root / "a.py.swp").write_text("junk")
        (self.root / "a.py~").write_text("junk")
        (self.root / ".#a.py").write_text("junk")
        (self.root / "4913").write_text("")
        snap = fs_watch._snapshot(self.root)
        names = {Path(p).name for p in snap}
        self.assertEqual(names, {"a.py", "web.jsx"})

    def test_skips_non_source_dirs(self):
        (self.root / ".openfde").mkdir()
        (self.root / ".openfde" / "state.json").write_text("{}")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dep.js").write_text("//")
        snap = fs_watch._snapshot(self.root)
        names = {Path(p).name for p in snap}
        self.assertNotIn("state.json", names)
        self.assertNotIn("dep.js", names)


class _FakeManager:
    def __init__(self):
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)


class FsWatchLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_loop_emits_on_external_edit_but_not_baseline_or_during_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x = 1\n")
            mgr = _FakeManager()
            run_active = {"on": False}
            task = asyncio.create_task(fs_watch.watch_loop(
                root, mgr, is_run_active=lambda: run_active["on"], interval=0.05))
            try:
                await asyncio.sleep(0.15)
                self.assertEqual(mgr.sent, [], "baseline must emit nothing")

                # external edit -> should broadcast file_activity
                (root / "a.py").write_text("x = 2\n")
                import os
                os.utime(root / "a.py", (time.time() + 5, time.time() + 5))
                await asyncio.sleep(0.2)
                kinds = [m["type"] for m in mgr.sent]
                self.assertIn("file_activity", kinds)
                ev = next(m for m in mgr.sent if m["type"] == "file_activity")
                self.assertEqual(ev["payload"]["file"], "a.py")
                self.assertEqual(ev["payload"]["action"], "write")

                # while a run is active, edits are suppressed
                mgr.sent.clear()
                run_active["on"] = True
                (root / "a.py").write_text("x = 3\n")
                os.utime(root / "a.py", (time.time() + 9, time.time() + 9))
                await asyncio.sleep(0.2)
                self.assertEqual(mgr.sent, [], "suppressed during a council run")
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    unittest.main()
