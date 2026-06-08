"""
Tests for openfde.episode_summary — deterministic prompt → story metadata, and the
persistence enrichment (monotonic sequence/tag + lazy backfill) that backs the
Prompt Chapter Rail.
"""

import tempfile
import unittest
from pathlib import Path

from openfde import episode_summary as es
from openfde.persistence import Persistence


class DeriveTest(unittest.TestCase):
    def test_strips_meta_prefixes(self):
        t, s = es.derive_title_summary("Please can you refactor the parser to stream tokens")
        self.assertEqual(t, "Refactor the parser to stream tokens")
        self.assertTrue(s.startswith("Refactor the parser"))

    def test_simple_intent_titlecased(self):
        t, _ = es.derive_title_summary("make the button async")
        self.assertEqual(t, "Make the button async")

    def test_prefers_goal_header_over_boilerplate(self):
        prompt = ("You are implementing the next OpenFDE slice in /tmp/x.\n\n"
                  "Read the repo first.\n\n## Goal\n\n"
                  "Implement Prompt Chapter Rail and Episode Detail Card.\n\n"
                  "Everything should be intuitive.")
        t, s = es.derive_title_summary(prompt)
        self.assertTrue(t.startswith("Implement Prompt Chapter Rail"))
        # Boilerplate ("You are implementing…", "Read the repo…") is not in the summary.
        self.assertNotIn("You are implementing", s)
        self.assertNotIn("Read the repo", s)

    def test_title_is_capped(self):
        t, _ = es.derive_title_summary("add a really long winded description " * 5)
        self.assertLessEqual(len(t), 46)

    def test_falls_back_to_file_scope_when_empty(self):
        t, s = es.derive_title_summary("", files=["openfde/server.py", "openfde/persistence.py"])
        self.assertEqual(t, "Update openfde")               # shared top-level scope
        self.assertIn("openfde", s)

    def test_empty_everything(self):
        t, s = es.derive_title_summary("", files=[])
        self.assertEqual(t, "Prompt")
        self.assertTrue(s)


class OperationalTest(unittest.TestCase):
    """Summary cleanup: shell/status/file-list chatter is flagged operational and never
    becomes a title; product prompts under headings title cleanly."""

    def test_file_list_is_operational(self):
        self.assertTrue(es.is_operational("ROADMAP.md\nFLOW.md\nopenfde/server.py"))

    def test_curl_is_operational(self):
        self.assertTrue(es.is_operational("curl -s localhost:7441/api/review/episodes | head"))

    def test_openfde_directive_is_operational(self):
        self.assertTrue(es.is_operational("IMPORTANT — OpenFDE owns version control. You only EDIT files."))

    def test_heres_the_cc_prompt_is_skipped(self):
        prompt = "Here's the CC prompt:\n\n## Product Change\n\nMove to Auto-Land on prompt completion."
        self.assertFalse(es.is_operational(prompt))
        t, _ = es.derive_title_summary(prompt)
        self.assertTrue(t.startswith("Move to Auto-Land"))
        self.assertNotIn("Here", t)

    def test_product_prompt_not_operational(self):
        self.assertFalse(es.is_operational("make the button async"))
        self.assertFalse(es.is_operational("Implement Prompt Story Graph v1"))

    def test_command_word_sentence_not_shell(self):
        # An English sentence starting with a command word is NOT shell chatter.
        self.assertFalse(es.is_operational("make the button async"))
        self.assertEqual(es.derive_title_summary("make the button async")[0], "Make the button async")

    def test_enrich_sets_signal(self):
        ep = {"episodeId": "e", "prompt": "curl -s localhost:7441/api/x"}
        es.enrich_episode(ep, 0)
        self.assertEqual(ep["signal"], "operational")
        ep2 = {"episodeId": "e2", "prompt": "Add login to the auth module"}
        es.enrich_episode(ep2, 0)
        self.assertEqual(ep2["signal"], "product")


class EnrichTest(unittest.TestCase):
    def test_assigns_and_is_idempotent(self):
        ep = {"episodeId": "e1", "prompt": "fix the login bug"}
        nxt = es.enrich_episode(ep, 11)
        self.assertEqual(nxt, 12)
        self.assertEqual(ep["sequence"], 12)
        self.assertEqual(ep["tag"], "P12")
        self.assertEqual(ep["title"], "Fix the login bug")
        # Re-running never overwrites or bumps an already-numbered episode.
        again = es.enrich_episode(ep, 99)
        self.assertEqual(again, 99)
        self.assertEqual(ep["sequence"], 12)
        self.assertEqual(ep["tag"], "P12")


class PersistenceMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Persistence(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_assigns_monotonic_sequence(self):
        a = self.p.upsert_episode({"episodeId": "a", "prompt": "first thing"})
        b = self.p.upsert_episode({"episodeId": "b", "prompt": "second thing"})
        self.assertEqual(a["sequence"], 1)
        self.assertEqual(a["tag"], "P1")
        self.assertEqual(b["sequence"], 2)
        self.assertEqual(b["tag"], "P2")
        # Updating an existing episode keeps its number.
        a2 = self.p.upsert_episode({**a, "status": "landed"})
        self.assertEqual(a2["sequence"], 1)

    def test_backfill_fills_legacy_episodes(self):
        # Write a legacy episode (no sequence/tag/title/summary) straight to disk.
        self.p._write_json(self.p.episodes_path, [
            {"episodeId": "old", "prompt": "add auth module", "createdAt": "2026-01-01T00:00:00Z"},
        ])
        eps = self.p.backfill_episode_meta()
        e = eps[0]
        self.assertEqual(e["sequence"], 1)
        self.assertEqual(e["tag"], "P1")
        self.assertEqual(e["title"], "Add auth module")
        self.assertTrue(e["summary"])
        # Idempotent: a second pass writes nothing new / keeps the same numbers.
        again = self.p.backfill_episode_meta()
        self.assertEqual(again[0]["sequence"], 1)


if __name__ == "__main__":
    unittest.main()
