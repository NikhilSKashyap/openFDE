"""Tests for openfde.council_bus — the external Codex+Claude-Code council shared bus.

Core guarantees: the bus binds to EXISTING OpenFDE ids and never mints a parallel council id, ids
survive a parse→render round-trip and a status update, and the commit-trailer binding reuses
OpenFDE's own parser. Fixtures are domain-neutral (no support-inbox specifics)."""

import os
import tempfile
import unittest

from openfde import council_bus as cb

# A generic work item — arbitrary ids/titles, no domain baggage.
ITEM = """---
episodeId: episode_abc123
runId: run_xyz789
taskIds:
  - task_aa
  - task_bb
boxIds:
  - box_one
  - box_two
status: READY_FOR_CC
architect: codex
seniorDev: claude-code
verifier: codex
latestCommit:
---

Acceptance:
- focused tests pass
- /api/state shows the thing
"""


class HeaderParseTest(unittest.TestCase):
    def test_preserves_existing_openfde_ids_verbatim(self):
        h, body = cb.parse_front_matter(ITEM)
        self.assertEqual(h["episodeId"], "episode_abc123")    # not normalized / regenerated
        self.assertEqual(h["runId"], "run_xyz789")
        self.assertEqual(h["taskIds"], ["task_aa", "task_bb"])
        self.assertEqual(h["boxIds"], ["box_one", "box_two"])
        self.assertEqual(h["status"], "READY_FOR_CC")
        self.assertIn("Acceptance:", body)

    def test_inline_and_block_lists_both_parse(self):
        inline = "---\nepisodeId: e1\ntaskIds: t1, t2, t3\nstatus: READY_FOR_CC\n---\n"
        h, _ = cb.parse_front_matter(inline)
        self.assertEqual(h["taskIds"], ["t1", "t2", "t3"])

    def test_no_front_matter_returns_empty_header(self):
        h, body = cb.parse_front_matter("just prose, no header")
        self.assertEqual(h, {})
        self.assertEqual(body, "just prose, no header")

    def test_round_trip_preserves_ids(self):
        h, body = cb.parse_front_matter(ITEM)
        h2, _ = cb.parse_front_matter(cb.render_front_matter(h, body))
        for k in ("episodeId", "runId", "taskIds", "boxIds", "status"):
            self.assertEqual(h2[k], h[k])

    def test_render_invents_no_ids(self):
        # A header with only a status → rendering MUST NOT fabricate episodeId/runId/taskIds.
        out = cb.render_front_matter({"status": "READY_FOR_CC"})
        self.assertNotIn("episodeId", out)
        self.assertNotIn("runId", out)
        self.assertNotIn("taskIds", out)
        h, _ = cb.parse_front_matter(out)
        self.assertEqual(h, {"status": "READY_FOR_CC"})


class StatusTest(unittest.TestCase):
    def test_status_machine_values(self):
        self.assertEqual(set(cb.STATUSES), {
            "READY_FOR_CC", "CLAUDE_WORKING", "READY_FOR_CODEX_VERIFICATION",
            "CHANGES_REQUESTED", "VERIFIED", "BLOCKED_NEEDS_ARCHITECT", "BLOCKED_NEEDS_HUMAN",
            "BLOCKED_PROVIDER_TIMEOUT"})
        self.assertIn("VERIFIED", cb.TERMINAL_STATUSES)
        self.assertIn("BLOCKED_NEEDS_HUMAN", cb.TERMINAL_STATUSES)
        self.assertIn("BLOCKED_PROVIDER_TIMEOUT", cb.TERMINAL_STATUSES)   # terminal, but not a human decision

    def test_set_status_preserves_everything_else(self):
        out = cb.set_status(ITEM, cb.STATUS_VERIFIED)
        h, body = cb.parse_front_matter(out)
        self.assertEqual(h["status"], "VERIFIED")
        self.assertEqual(h["episodeId"], "episode_abc123")    # ids untouched
        self.assertEqual(h["taskIds"], ["task_aa", "task_bb"])
        self.assertIn("Acceptance:", body)                    # body untouched

    def test_set_status_rejects_unknown(self):
        with self.assertRaises(ValueError):
            cb.set_status(ITEM, "DONE_LOL")


class WorkItemsTest(unittest.TestCase):
    def test_multiple_items_each_keep_their_ids(self):
        two = ITEM + "\n" + ITEM.replace("episode_abc123", "episode_def456").replace("box_one", "box_nine")
        items = cb.parse_work_items(two)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["header"]["episodeId"], "episode_abc123")
        self.assertEqual(items[1]["header"]["episodeId"], "episode_def456")
        self.assertEqual(items[1]["header"]["boxIds"], ["box_nine", "box_two"])


class TrailerBindingTest(unittest.TestCase):
    def test_build_trailers_binds_only_given_ids(self):
        t = cb.build_trailers(episode_id="episode_abc123", task_ids=["task_aa", "task_bb"],
                              run_id="run_xyz789", role="senior_dev",
                              handoff="ready_for_codex_verification")
        self.assertEqual(t["OpenFDE-Episode"], "episode_abc123")
        self.assertEqual(t["OpenFDE-Tasks"], "task_aa, task_bb")
        self.assertEqual(t["OpenFDE-Run"], "run_xyz789")
        self.assertEqual(t["OpenFDE-Role"], "senior_dev")
        self.assertEqual(t["OpenFDE-Handoff"], "ready_for_codex_verification")

    def test_build_trailers_omits_absent_fields(self):
        t = cb.build_trailers(episode_id="e1")
        self.assertEqual(set(t), {"OpenFDE-Episode"})         # nothing invented

    def test_binding_round_trips_through_a_commit_body(self):
        t = cb.build_trailers(episode_id="episode_abc123", task_ids=["task_aa", "task_bb"],
                              run_id="run_xyz789", role="senior_dev",
                              handoff="ready_for_codex_verification")
        body = "openfde: implement the thing\n\nSome detail.\n\n" + cb.trailer_block(t)
        b = cb.binding_from_commit(body)
        self.assertEqual(b["episodeIds"], ["episode_abc123"])  # reuses episode_commits parser
        self.assertEqual(b["taskIds"], ["task_aa", "task_bb"])
        self.assertEqual(b["runId"], "run_xyz789")
        self.assertEqual(b["role"], "senior_dev")
        self.assertEqual(b["handoff"], "ready_for_codex_verification")

    def test_binding_from_untrailed_commit_is_empty(self):
        b = cb.binding_from_commit("openfde: a plain commit\n\nNo trailers here.")
        self.assertEqual(b["episodeIds"], [])
        self.assertEqual(b["taskIds"], [])
        self.assertEqual(b["runId"], "")


class FileIOTest(unittest.TestCase):
    def test_write_read_append_under_council_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cb.write_bus_file(d, "tasks", ITEM)
            self.assertTrue(os.path.exists(os.path.join(d, ".openfde", "council", "TASKS.md")))
            self.assertIn("episode_abc123", cb.read_bus_file(d, "tasks"))
            cb.append_bus_entry(d, "claude", "R1 — handoff", "implemented; tests green")
            cb.append_bus_entry(d, "claude", "R2 — fix", "addressed the finding")
            log = cb.read_bus_file(d, "claude")
            self.assertIn("## R1 — handoff", log)
            self.assertIn("## R2 — fix", log)                 # append, never clobber
            self.assertLess(log.index("R1"), log.index("R2"))

    def test_missing_file_reads_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cb.read_bus_file(d, "codex"), "")


if __name__ == "__main__":
    unittest.main()
