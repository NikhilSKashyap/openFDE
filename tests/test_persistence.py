"""
Tests for openfde.persistence — the torn-write regression (observed live: a FIXED
shared .tmp name let two concurrent writers interleave bytes and os.replace promoted
the splice into episodes.json). Writes must use private per-write tmps: the store
stays parseable under concurrent hammering and no tmp residue is left behind.
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from openfde.persistence import Persistence


class AtomicWriteTest(unittest.TestCase):
    def test_concurrent_writers_never_tear_the_store(self):
        # 8 threads × 40 full-list saves each; the file must parse as valid JSON at
        # the end (and at any point — last write wins, but never a byte splice).
        with tempfile.TemporaryDirectory() as d:
            p = Persistence(Path(d) / ".openfde")
            payload = [{"episodeId": f"episode_{i}", "title": "x" * 400,
                        "files": [f"f{j}.py" for j in range(20)]} for i in range(40)]

            def hammer(tid):
                for n in range(40):
                    p.save_tasks([{**e, "writer": tid, "n": n} for e in payload])

            threads = [threading.Thread(target=hammer, args=(t,)) for t in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            data = json.load(open(p.tasks_path))          # parses → no splice
            self.assertEqual(len(data), 40)
            writers = {e["writer"] for e in data}
            self.assertEqual(len(writers), 1)             # one COMPLETE write won

    def test_no_tmp_residue_after_writes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Persistence(Path(d) / ".openfde")
            for i in range(5):
                p.save_tasks([{"id": i}])
            leftovers = list((Path(d) / ".openfde").glob("*.tmp")) \
                + list((Path(d) / ".openfde").glob(".*.tmp"))
            self.assertEqual(leftovers, [])

    def test_fresh_persistence_loads_empty_task_list(self):
        # Regression: a fresh repo's OpenPM board must start EMPTY. (Observed live:
        # an instance watching the aisuite clone showed OpenFDE's own bootstrap dev
        # cards — the frontend seeded demo tasks and the debounced PUT persisted
        # them into the target repo's tasks.json.) Backend contract: no tasks.json
        # → load_tasks() == [] — never a seed, and loading must not create the file.
        with tempfile.TemporaryDirectory() as d:
            p = Persistence(Path(d) / ".openfde")
            self.assertEqual(p.load_tasks(), [])
            self.assertFalse(p.tasks_path.exists())

    def test_corrupt_store_reads_as_default(self):
        # _read_json degrades gracefully (this is what kept the server alive during
        # the live incident) — the repair/recovery happens outside, never a crash.
        with tempfile.TemporaryDirectory() as d:
            p = Persistence(Path(d) / ".openfde")
            p.save_tasks([{"id": 1}])
            p.tasks_path.write_text('[{"id": 1}, {"id":')     # torn mid-object
            self.assertEqual(p.load_tasks(), [])


if __name__ == "__main__":
    unittest.main()
