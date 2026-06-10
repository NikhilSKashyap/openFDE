"""
Tests for openfde.instance_lock — one watcher per repo. The restart-overlap window
(old process draining while the new one boots) produced duplicate episode captures
and, with a shared tmp name, the torn episodes.json — the lock closes the class.
Also covers the cross-process capture dedup helper (store-authoritative).
"""

import os
import tempfile
import unittest
from pathlib import Path

from openfde.instance_lock import (
    LOCK_NAME,
    WatchLockHeld,
    acquire_watch_lock,
    release_watch_lock,
)
from openfde.persistence import Persistence
from openfde.prompt_capture import capture_key_exists


class InstanceLockTest(unittest.TestCase):
    def test_acquire_then_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            lock = acquire_watch_lock(d, pid=os.getpid())
            self.assertTrue(Path(lock).exists())
            with self.assertRaises(WatchLockHeld) as cm:
                acquire_watch_lock(d, pid=99999999)        # a different "process"
            self.assertEqual(cm.exception.pid, os.getpid())
            self.assertIn("already running", str(cm.exception))

    def test_stale_lock_is_swept(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / LOCK_NAME).write_text("999999999")  # dead pid
            lock = acquire_watch_lock(d, pid=os.getpid())  # takes over, no raise
            self.assertEqual((Path(d) / LOCK_NAME).read_text(), str(os.getpid()))
            release_watch_lock(lock)

    def test_release_only_when_owned(self):
        with tempfile.TemporaryDirectory() as d:
            lock = acquire_watch_lock(d, pid=os.getpid())
            release_watch_lock(lock, pid=12345)            # not the holder → kept
            self.assertTrue(Path(lock).exists())
            release_watch_lock(lock)                       # holder → released
            self.assertFalse(Path(lock).exists())
            acquire_watch_lock(d, pid=os.getpid())         # reacquirable

    def test_unreadable_lock_is_swept(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / LOCK_NAME).write_text("not-a-pid")
            lock = acquire_watch_lock(d, pid=os.getpid())
            self.assertTrue(Path(lock).exists())


class CaptureKeyDedupTest(unittest.TestCase):
    def test_store_is_the_shared_truth(self):
        # The observed duplicate pairs had IDENTICAL captureKeys from two processes
        # whose in-memory dedup sets couldn't see each other — the store check is
        # what a second process consults before inserting.
        with tempfile.TemporaryDirectory() as d:
            p = Persistence(Path(d) / ".openfde")
            self.assertFalse(capture_key_exists(p, "sess:uuid-1"))
            p.upsert_episode({"episodeId": "e1", "captureKey": "sess:uuid-1",
                              "prompt": "x", "status": "reviewing"})
            self.assertTrue(capture_key_exists(p, "sess:uuid-1"))
            self.assertFalse(capture_key_exists(p, "sess:uuid-2"))
            self.assertFalse(capture_key_exists(p, ""))


if __name__ == "__main__":
    unittest.main()
