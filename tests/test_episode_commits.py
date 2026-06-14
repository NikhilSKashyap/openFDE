"""
Tests for openfde.episode_commits — prompt → commit reconciliation (many prompts, one commit).

Laws under test:
  - A commit declares episodes via OpenFDE-Episodes (plural) and/or OpenFDE-Episode (singular).
  - Confidence ladder: explicit > high_file_overlap > time_file_inferred > ambiguous.
  - A 0-file discussion episode is NEVER attached without an explicit trailer.
  - attach_commit is idempotent and never downgrades a stronger confidence.
  - One batched commit touching several episodes' files lands on all of those prompt cards.
"""
import unittest
from datetime import datetime, timedelta, timezone

from openfde import episode_commits as ec


def _iso(dt):
    return dt.isoformat()


NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)


class TrailerParseTest(unittest.TestCase):
    def test_plural_comma_separated(self):
        self.assertEqual(
            ec.episode_ids_from_trailers({"OpenFDE-Episodes": "e1, e2, e3"}),
            ["e1", "e2", "e3"])

    def test_plural_space_separated(self):
        self.assertEqual(
            ec.episode_ids_from_trailers({"OpenFDE-Episodes": "e1 e2"}), ["e1", "e2"])

    def test_singular_and_plural_dedup_order(self):
        # singular id already present in the plural list must not be duplicated
        ids = ec.episode_ids_from_trailers({"OpenFDE-Episodes": "e1,e2", "OpenFDE-Episode": "e1"})
        self.assertEqual(ids, ["e1", "e2"])

    def test_singular_only(self):
        self.assertEqual(ec.episode_ids_from_trailers({"OpenFDE-Episode": "solo"}), ["solo"])

    def test_none_when_no_trailers(self):
        self.assertEqual(ec.episode_ids_from_trailers({}), [])


class ReconcileCommitTest(unittest.TestCase):
    def test_explicit_trailer_wins_even_without_file_overlap(self):
        # Explicit declaration attaches regardless of files — including a 0-file episode.
        ep = {"episodeId": "P9", "files": []}
        commit = {"sha": "abc", "files": ["x.py"], "episodeIds": ["P9"], "timestamp": _iso(NOW)}
        [v] = ec.reconcile_commit(commit, [ep])
        self.assertEqual((v["episodeId"], v["confidence"], v["attach"]), ("P9", "explicit", True))

    def test_high_file_overlap(self):
        ep = {"episodeId": "P1", "files": ["a.py", "b.py"], "updatedAt": _iso(NOW)}
        commit = {"sha": "c1", "files": ["a.py", "b.py", "README.md"], "episodeIds": [],
                  "timestamp": _iso(NOW)}
        [v] = ec.reconcile_commit(commit, [ep])
        self.assertEqual(v["confidence"], "high_file_overlap")
        self.assertTrue(v["attach"])
        self.assertEqual(v["matchedFiles"], ["a.py", "b.py"])

    def test_time_file_inferred_weak_overlap_but_recent(self):
        # 1 of 4 files (25% < 50%) but the episode was active right at commit time.
        ep = {"episodeId": "P2", "files": ["a.py", "b.py", "c.py", "d.py"], "updatedAt": _iso(NOW)}
        commit = {"sha": "c2", "files": ["a.py"], "episodeIds": [], "timestamp": _iso(NOW)}
        [v] = ec.reconcile_commit(commit, [ep])
        self.assertEqual(v["confidence"], "time_file_inferred")
        self.assertTrue(v["attach"])

    def test_ambiguous_weak_overlap_and_far_in_time_not_attached(self):
        # 1 of 4 files AND the episode was active days before the commit → ambiguous, not attached.
        old = NOW - timedelta(days=3)
        ep = {"episodeId": "P3", "files": ["a.py", "b.py", "c.py", "d.py"], "updatedAt": _iso(old)}
        commit = {"sha": "c3", "files": ["a.py"], "episodeIds": [], "timestamp": _iso(NOW)}
        [v] = ec.reconcile_commit(commit, [ep])
        self.assertEqual(v["confidence"], "ambiguous")
        self.assertFalse(v["attach"])

    def test_zero_file_episode_never_attached_without_trailer(self):
        ep = {"episodeId": "discuss", "files": [], "updatedAt": _iso(NOW)}
        commit = {"sha": "c4", "files": ["a.py"], "episodeIds": [], "timestamp": _iso(NOW)}
        self.assertEqual(ec.reconcile_commit(commit, [ep]), [])

    def test_no_shared_file_no_link(self):
        ep = {"episodeId": "P5", "files": ["z.py"], "updatedAt": _iso(NOW)}
        commit = {"sha": "c5", "files": ["a.py"], "episodeIds": [], "timestamp": _iso(NOW)}
        self.assertEqual(ec.reconcile_commit(commit, [ep]), [])


class AttachCommitTest(unittest.TestCase):
    def test_idempotent_no_duplicate_sha(self):
        ep = {"episodeId": "P1"}
        ec.attach_commit(ep, "sha1", confidence="high_file_overlap", matched_files=["a.py"])
        ec.attach_commit(ep, "sha1", confidence="high_file_overlap", matched_files=["a.py"])
        self.assertEqual(ep["commitShas"], ["sha1"])
        self.assertEqual(ep["commitMeta"]["sha1"]["confidence"], "high_file_overlap")

    def test_stronger_confidence_upgrades(self):
        ep = {"episodeId": "P1"}
        ec.attach_commit(ep, "sha1", confidence="time_file_inferred", matched_files=["a.py"])
        ec.attach_commit(ep, "sha1", confidence="explicit", matched_files=["a.py"])
        self.assertEqual(ep["commitMeta"]["sha1"]["confidence"], "explicit")

    def test_weaker_confidence_does_not_downgrade(self):
        ep = {"episodeId": "P1"}
        ec.attach_commit(ep, "sha1", confidence="explicit", matched_files=["a.py"])
        ec.attach_commit(ep, "sha1", confidence="ambiguous", matched_files=["a.py"])
        self.assertEqual(ep["commitMeta"]["sha1"]["confidence"], "explicit")


class ReconcileEpisodesTest(unittest.TestCase):
    def test_one_commit_lands_on_three_prompt_cards(self):
        # The headline scenario: P1 touched a.py, P2 touched b.py, P3 touched c.py; the developer
        # then makes ONE commit covering all three. All three cards must show that commit.
        eps = [
            {"episodeId": "P1", "files": ["a.py"], "updatedAt": _iso(NOW)},
            {"episodeId": "P2", "files": ["b.py"], "updatedAt": _iso(NOW)},
            {"episodeId": "P3", "files": ["c.py"], "updatedAt": _iso(NOW)},
        ]
        commit = {"sha": "batched", "files": ["a.py", "b.py", "c.py"], "episodeIds": [],
                  "timestamp": _iso(NOW)}
        changed = ec.reconcile_episodes([commit], eps)
        self.assertEqual(set(changed), {"P1", "P2", "P3"})
        for ep in eps:
            self.assertEqual(ep["commitShas"], ["batched"])
            self.assertEqual(ep["commitMeta"]["batched"]["confidence"], "high_file_overlap")

    def test_ambiguous_excluded_by_default_included_on_request(self):
        old = NOW - timedelta(days=3)
        ep = {"episodeId": "P3", "files": ["a.py", "b.py", "c.py", "d.py"], "updatedAt": _iso(old)}
        commit = {"sha": "c3", "files": ["a.py"], "episodeIds": [], "timestamp": _iso(NOW)}

        ec.reconcile_episodes([commit], [ep])                       # default: surface-only
        self.assertNotIn("commitShas", ep)

        ec.reconcile_episodes([commit], [ep], include_ambiguous=True)
        self.assertEqual(ep["commitShas"], ["c3"])
        self.assertEqual(ep["commitMeta"]["c3"]["confidence"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
