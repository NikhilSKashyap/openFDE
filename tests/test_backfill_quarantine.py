"""
Tests for backfill quarantine + clean prompt numbering.

Product law: P<n> means a REAL OpenFDE prompt episode. Low-confidence backfilled transcript
fragments (source=openfde-backfill, backfillConfidence in {discussion, needs_review}) must NOT
consume a P<n>, must NOT appear as episodes, and live in backfill_candidates.json — importable
later, unnumbered until accepted.
"""
import tempfile
import unittest
from pathlib import Path

from openfde import prompt_capture
from openfde.persistence import Persistence


def _ep(eid, *, source="openfde-capture", conf=None, created="2026-06-10T00:00:00Z",
        status="landed", files=None, key=None):
    e = {"episodeId": eid, "createdAt": created, "status": status,
         "files": files or [], "source": source, "prompt": f"prompt {eid}",
         "captureKey": key or f"k-{eid}"}
    if conf:
        e["backfillConfidence"] = conf
    return e


class QuarantineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Persistence(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, episodes):
        self.p._write_json(self.p.episodes_path, episodes)

    def test_noisy_backfill_is_quarantined_and_reals_renumbered(self):
        # 3 discussion fragments (already wrongly numbered P5..P7) + 2 real prompts.
        self._seed([
            {**_ep("d3", source="openfde-backfill", conf="discussion", status="open"),
             "sequence": 7, "tag": "P7"},
            {**_ep("d2", source="openfde-backfill", conf="needs_review", status="needs_manual_land"),
             "sequence": 6, "tag": "P6"},
            {**_ep("d1", source="openfde-backfill", conf="discussion", status="open"),
             "sequence": 5, "tag": "P5"},
            {**_ep("r2", created="2026-06-11T00:00:00Z"), "sequence": 8, "tag": "P8"},
            {**_ep("r1", created="2026-06-10T00:00:00Z"), "sequence": 4, "tag": "P4"},
        ])
        res = self.p.quarantine_backfill_pollution()
        self.assertEqual(res["quarantined"], 3)
        self.assertEqual(res["real"], 2)

        eps = self.p.load_episodes()
        self.assertEqual({e["episodeId"] for e in eps}, {"r1", "r2"})       # only real episodes
        by_id = {e["episodeId"]: e for e in eps}
        self.assertEqual(by_id["r1"]["tag"], "P1")                          # oldest → P1
        self.assertEqual(by_id["r2"]["tag"], "P2")
        self.assertIn("P4", by_id["r1"]["tagAliases"])                      # old tag kept as alias
        self.assertIn("P8", by_id["r2"]["tagAliases"])

        cands = self.p.load_backfill_candidates()
        self.assertEqual(len(cands), 3)
        self.assertTrue(all("sequence" not in c and "tag" not in c for c in cands))  # never numbered
        self.assertTrue(all(c["prompt"] and c["captureKey"] for c in cands))         # raw preserved

    def test_high_confidence_backfill_stays_an_episode(self):
        self._seed([
            {**_ep("h1", source="openfde-backfill", conf="high", files=["a.py"]),
             "sequence": 1, "tag": "P1"},
            {**_ep("d1", source="openfde-backfill", conf="discussion", status="open"),
             "sequence": 2, "tag": "P2"},
        ])
        self.p.quarantine_backfill_pollution()
        eps = self.p.load_episodes()
        self.assertEqual({e["episodeId"] for e in eps}, {"h1"})            # landed backfill kept
        self.assertEqual(self.p.backfill_candidate_count(), 1)

    def test_idempotent(self):
        self._seed([{**_ep("d1", source="openfde-backfill", conf="discussion", status="open"),
                     "sequence": 1, "tag": "P1"},
                    {**_ep("r1"), "sequence": 2, "tag": "P2"}])
        self.p.quarantine_backfill_pollution()
        second = self.p.quarantine_backfill_pollution()
        self.assertEqual(second["quarantined"], 0)                         # no-op
        self.assertEqual(self.p.backfill_candidate_count(), 1)             # not duplicated

    def test_real_episode_starts_at_P1_in_fresh_repo(self):
        self.p.upsert_episode({**_ep("first", source="openfde-capture")})
        self.assertEqual(self.p.load_episodes()[0]["tag"], "P1")

    def test_add_candidate_strips_numbering_and_dedups(self):
        self.p.add_backfill_candidate({**_ep("c1", source="openfde-backfill",
                                             conf="discussion"), "sequence": 99, "tag": "P99"})
        self.p.add_backfill_candidate(_ep("c1", source="openfde-backfill", conf="discussion"))  # dup key
        cands = self.p.load_backfill_candidates()
        self.assertEqual(len(cands), 1)                                    # deduped by captureKey
        self.assertNotIn("sequence", cands[0])
        self.assertNotIn("tag", cands[0])


class CaptureDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Persistence(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_capture_key_exists_checks_candidates(self):
        # A quarantined candidate's key must read as "already known" so a re-scan never re-imports
        # it back into episodes.json.
        self.p.add_backfill_candidate(_ep("c1", source="openfde-backfill", conf="discussion",
                                          key="seen-key"))
        self.assertTrue(prompt_capture.capture_key_exists(self.p, "seen-key"))
        self.assertFalse(prompt_capture.capture_key_exists(self.p, "unseen-key"))


if __name__ == "__main__":
    unittest.main()
