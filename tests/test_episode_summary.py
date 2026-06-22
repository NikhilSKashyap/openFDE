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
        self.assertTrue(t.startswith("Prompt Chapter Rail"))   # leading "Implement" distilled off
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
        self.assertTrue(t.startswith("Auto-Land"))             # "Move to" distilled off, "Here's" skipped
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


class BadTitleTest(unittest.TestCase):
    def test_is_bad_title_patterns(self):
        for t in ["Yes", "ok", "Here's the CC prompt", "ROADMAP.md", "`openfde/server.py`",
                  "curl -s localhost:7441/api/x", "IMPORTANT — OpenFDE owns version control",
                  "Read the repo", "You are implementing the slice", "Restart the server", ""]:
            self.assertTrue(es.is_bad_title(t), t)
        for t in ["LLM Story Summarizer", "Auto-Land Prompt Commits", "Prompt Chapter Rail"]:
            self.assertFalse(es.is_bad_title(t), t)

    def test_is_bad_title_robust_to_curly_quotes(self):
        # The real-repo persisted titles use a curly apostrophe — must still be caught.
        self.assertTrue(es.is_bad_title("Here’s the CC prompt"))   # curly '
        self.assertTrue(es.is_bad_title("Here's the CC prompt"))       # straight ' (preserved)
        self.assertTrue(es.is_bad_title("You’re implementing the slice"))

    def test_is_bad_title_catches_code_fence_openers(self):
        # A prompt starting with a markdown fence ("```text") produced a commit/card
        # literally titled "text" (observed live, commit 0f7a653) — fence language
        # tokens are never titles, with or without the backticks.
        for t in ["```text", "```bash", "```", "text", "Text", "json", "`python`"]:
            self.assertTrue(es.is_bad_title(t), t)
        # …but real multi-word titles that merely CONTAIN such a word stay valid.
        for t in ["Text rendering pipeline", "JSON schema validation", "Bash wrapper capture"]:
            self.assertFalse(es.is_bad_title(t), t)
        self.assertFalse(es.is_bad_title("LLM Story Summarizer"))      # clean unaffected

    def test_distill_strips_implement_and_version(self):
        self.assertEqual(es.derive_title_summary("## Goal\nImplement LLM Story Summarizer v1")[0],
                         "LLM Story Summarizer")
        self.assertEqual(es.derive_title_summary("## Goal\nDesign and implement Prompt Story Graph v1")[0],
                         "Prompt Story Graph")

    def test_filelist_prompt_titles_from_heading(self):
        p = ("ROADMAP.md\nFLOW.md\nopenfde/server.py\n\n## Product Change\n\n"
             "Move from Review Then Land to Auto-Land on prompt completion.")
        t, _ = es.derive_title_summary(p)
        self.assertNotIn(".md", t)                       # never titled from the file list
        self.assertTrue(t.lower().startswith("auto-land"))

    def test_commit_display_prefers_clean_episode(self):
        # A noisy raw commit subject is replaced by the cleaned episode title/summary.
        t, s = es.commit_display("LLM Story Summarizer", "Summarizes prompts into concepts.",
                                 "openfde: Here's the CC prompt")
        self.assertEqual(t, "LLM Story Summarizer")
        self.assertEqual(s, "Summarizes prompts into concepts.")

    def test_commit_display_falls_back_to_clean_commit(self):
        # No episode title → use the de-`openfde:`-ed commit subject when it's clean.
        t, s = es.commit_display("", "", "openfde: wire prompt story rail")
        self.assertEqual(t, "wire prompt story rail")
        self.assertEqual(s, "wire prompt story rail")

    def test_commit_display_both_bad_is_neutral(self):
        t, s = es.commit_display("Yes", "", "openfde: `ROADMAP.md`")
        self.assertEqual(t, "Landed change")
        self.assertEqual(s, "")


class RepairTasksTest(unittest.TestCase):
    EPS = [
        {"episodeId": "e9", "tag": "P9", "sequence": 9, "title": "LLM Story Summarizer",
         "summary": "Summarizes prompts into product concepts.", "commitShas": ["a651bad"]},
        {"episodeId": "e5", "tag": "P5", "sequence": 5, "title": "Prompt Chapter Rail",
         "summary": "Prompt chips are chapters.", "commitShas": ["d5fa56a"]},
    ]

    def _noisy(self, tid, eid, sha):
        return {"id": tid, "source": "openfde-episode", "episodeId": eid, "commitSha": sha,
                "shortSha": sha, "title": "Here's the CC prompt", "description": "",
                "files": [tid + ".py"], "column": "done", "verificationStatus": "passed"}

    def test_repairs_noisy_episode_card_from_episode(self):
        tasks = [self._noisy("t9", "e9", "a651bad")]
        out, changed = es.repair_episode_tasks(tasks, self.EPS)
        self.assertTrue(changed)
        t = out[0]
        self.assertEqual(t["title"], "LLM Story Summarizer")        # from the episode
        self.assertEqual(t["description"], "Summarizes prompts into product concepts.")
        self.assertEqual(t["episodeTag"], "P9")
        # Identity preserved.
        self.assertEqual(t["commitSha"], "a651bad")
        self.assertEqual(t["shortSha"], "a651bad")
        self.assertEqual(t["files"], ["t9.py"])
        self.assertEqual(t["episodeId"], "e9")

    def test_links_by_commit_sha_when_episode_id_missing(self):
        t = self._noisy("t5", None, "d5fa56a")
        out, changed = es.repair_episode_tasks([t], self.EPS)
        self.assertTrue(changed)
        self.assertEqual(out[0]["title"], "Prompt Chapter Rail")     # matched via commitSha

    def test_clean_card_and_non_episode_task_untouched(self):
        tasks = [
            {"id": "c", "source": "openfde-episode", "episodeId": "e9", "commitSha": "a651bad",
             "title": "LLM Story Summarizer", "description": "ok"},          # already clean
            {"id": "demo", "title": "Whiteboard canvas", "column": "done"},  # not an episode card
        ]
        out, changed = es.repair_episode_tasks(tasks, self.EPS)
        self.assertFalse(changed)
        self.assertEqual(out, tasks)

    def test_idempotent(self):
        out1, c1 = es.repair_episode_tasks([self._noisy("t9", "e9", "a651bad")], self.EPS)
        out2, c2 = es.repair_episode_tasks(out1, self.EPS)
        self.assertTrue(c1)
        self.assertFalse(c2)                                          # second pass is a no-op

    def test_repairs_curly_apostrophe_title(self):
        # The actual real-repo failure: the persisted title used a curly apostrophe.
        t = self._noisy("t9", "e9", "a651bad")
        t["title"] = "Here’s the CC prompt"                            # curly '
        out, changed = es.repair_episode_tasks([t], self.EPS)
        self.assertTrue(changed)
        self.assertEqual(out[0]["title"], "LLM Story Summarizer")
        self.assertEqual(out[0]["commitSha"], "a651bad")              # identity preserved


class ReconcileTaskStatusTest(unittest.TestCase):
    """OpenPM cards must mirror their episode's CURRENT verify state — the live
    split-brain bug was a card stuck on FAILED next to a passed episode."""

    def _ep(self, eid, verify_status=None, status=None):
        ep = {"episodeId": eid, "tag": "P1", "title": "Add Thing"}
        if verify_status is not None:
            ep["verify"] = {"status": verify_status}
        if status is not None:
            ep["status"] = status
        return ep

    def _task(self, eid, column, vstatus):
        return {"id": "t-" + (eid or "x"), "source": "openfde-episode",
                "episodeId": eid, "column": column, "verificationStatus": vstatus}

    def test_passed_episode_clears_stale_failed_card(self):
        # The exact live bug: card sat in Testing/FAILED while the episode passed.
        tasks = [self._task("e1", "testing", "failed")]
        changed = es.reconcile_task_status(tasks, [self._ep("e1", "passed")])
        self.assertTrue(changed)
        self.assertEqual(tasks[0]["verificationStatus"], "passed")
        self.assertEqual(tasks[0]["column"], "testing")   # not todo/doing → not promoted

    def test_failed_episode_clears_stale_passed_card(self):
        tasks = [self._task("e1", "testing", "passed")]
        changed = es.reconcile_task_status(tasks, [self._ep("e1", "failed")])
        self.assertTrue(changed)
        self.assertEqual(tasks[0]["verificationStatus"], "failed")

    def test_result_promotes_todo_or_doing_to_testing(self):
        tasks = [self._task("e1", "doing", None), self._task("e2", "todo", None)]
        changed = es.reconcile_task_status(
            tasks, [self._ep("e1", "passed"), self._ep("e2", "failed")])
        self.assertTrue(changed)
        self.assertEqual(tasks[0]["column"], "testing")
        self.assertEqual(tasks[0]["verificationStatus"], "passed")
        self.assertEqual(tasks[1]["column"], "testing")
        self.assertEqual(tasks[1]["verificationStatus"], "failed")

    def test_landed_episode_forces_done_passed(self):
        tasks = [self._task("e1", "testing", "failed")]
        changed = es.reconcile_task_status(tasks, [self._ep("e1", "failed", status="landed")])
        self.assertTrue(changed)
        self.assertEqual(tasks[0]["column"], "done")
        self.assertEqual(tasks[0]["verificationStatus"], "passed")

    def test_user_shipped_card_left_alone(self):
        # column=done but episode NOT landed (manual move) → never reopened here.
        tasks = [self._task("e1", "done", "passed")]
        changed = es.reconcile_task_status(tasks, [self._ep("e1", "failed")])
        self.assertFalse(changed)
        self.assertEqual(tasks[0]["column"], "done")
        self.assertEqual(tasks[0]["verificationStatus"], "passed")

    def test_pending_verify_untouched(self):
        tasks = [self._task("e1", "testing", "failed")]
        changed = es.reconcile_task_status(tasks, [self._ep("e1", "running")])
        self.assertFalse(changed)
        self.assertEqual(tasks[0]["verificationStatus"], "failed")

    def test_non_episode_and_unlinked_cards_untouched(self):
        tasks = [
            {"id": "demo", "title": "Whiteboard", "column": "doing"},   # no episodeId
            self._task("ghost", "testing", "failed"),                    # episode missing
        ]
        changed = es.reconcile_task_status(tasks, [self._ep("e1", "passed")])
        self.assertFalse(changed)

    def test_idempotent(self):
        tasks = [self._task("e1", "testing", "failed")]
        eps = [self._ep("e1", "passed")]
        self.assertTrue(es.reconcile_task_status(tasks, eps))
        self.assertFalse(es.reconcile_task_status(tasks, eps))   # second pass no-ops

    def test_non_list_is_safe(self):
        self.assertFalse(es.reconcile_task_status(None, []))
        self.assertFalse(es.reconcile_task_status({}, []))


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


class StoryNoiseHelpersTest(unittest.TestCase):
    def test_junk_paths(self):
        self.assertTrue(es.is_junk_path(".DS_Store"))
        self.assertTrue(es.is_junk_path("frontend/.DS_Store"))
        self.assertFalse(es.is_junk_path("README.md"))
        self.assertFalse(es.is_junk_path("openfde/server.py"))

    def test_demo_plan_concepts_flagged(self):
        for s in ("NanoGPT live demo", "Tailwind action demo", "live run in this demo",
                  "separate action demos", "OpenFDE self walkthrough", "explanatory demo flow",
                  "demo 2", "feature walkthrough"):
            self.assertTrue(es.is_demo_plan_concept(s), s)

    def test_real_concepts_survive_demo_filter(self):
        # bare "demo" and real OpenFDE features must NOT be mistaken for demo planning.
        for s in ("Plugin Registry", "Echo demo provider", "no-key demo", "verification gate",
                  "Story Layout", "dotted and solid scope", "failure lens"):
            self.assertFalse(es.is_demo_plan_concept(s), s)

    def test_demo_prompt_detection(self):
        self.assertTrue(es.is_demo_prompt("let's prep for demo 1 - walkthru of openFDE"))
        self.assertTrue(es.is_demo_prompt("demo2 is nanogpt, demo3 is tailwind, give a walkthru"))
        self.assertFalse(es.is_demo_prompt("update the README install section"))
        self.assertFalse(es.is_demo_prompt("add a demo provider called Echo"))   # product, not demo-prep

    def test_product_title_from_change(self):
        t, _s = es.product_title_from_change(
            ["frontend/src/App.css", "frontend/src/components/Story/Story.jsx"],
            "openfde: update walkthrough demo narrative and styling")
        self.assertIn("Story", t)                          # named by the component, not the demo prompt
        # a clean, non-demo commit subject is preferred verbatim
        t2, _ = es.product_title_from_change(["openfde/verify.py"], "openfde: tighten the verify gate")
        self.assertIn("verify gate", t2.lower())
        self.assertIsNone(es.product_title_from_change(["DEMO1.md"], ""))   # no real source to name


class RepairTaskCommitShasTest(unittest.TestCase):
    """OpenPM cards heal stale commit mappings from episode truth (episode commitShas win)."""

    def test_clears_stale_commit_when_episode_has_none(self):
        # The P162 case: card shows a commit its episode no longer lists → clear it.
        tasks = [{"id": "t", "episodeId": "P162", "commitSha": "2131a38", "shortSha": "2131a3"}]
        out, changed = es.repair_task_commit_shas(tasks, [{"episodeId": "P162", "commitShas": []}])
        self.assertTrue(changed)
        self.assertIsNone(out[0]["commitSha"])
        self.assertIsNone(out[0]["shortSha"])

    def test_adopts_episodes_single_commit(self):
        # The P163 case: card has a wrong/old sha; the episode now has exactly one → adopt it.
        tasks = [{"id": "t", "episodeId": "P163", "commitSha": "old", "shortSha": "old"}]
        out, changed = es.repair_task_commit_shas(tasks, [{"episodeId": "P163", "commitShas": ["5773a91"]}])
        self.assertTrue(changed)
        self.assertEqual(out[0]["commitSha"], "5773a91")
        self.assertEqual(out[0]["shortSha"], "5773a91")

    def test_valid_mapping_is_untouched(self):
        tasks = [{"id": "t", "episodeId": "P1", "commitSha": "abc", "shortSha": "abc"}]
        out, changed = es.repair_task_commit_shas(tasks, [{"episodeId": "P1", "commitShas": ["abc"]}])
        self.assertFalse(changed)
        self.assertEqual(out, tasks)

    def test_unknown_episode_is_left_alone(self):
        # A card whose episode isn't in the store could be a load-order blip — never destroy data.
        tasks = [{"id": "t", "episodeId": "GONE", "commitSha": "abc"}]
        out, changed = es.repair_task_commit_shas(tasks, [{"episodeId": "P1", "commitShas": ["x"]}])
        self.assertFalse(changed)
        self.assertEqual(out[0]["commitSha"], "abc")

    def test_ambiguous_multi_commit_episode_clears_rather_than_guess(self):
        tasks = [{"id": "t", "episodeId": "P1", "commitSha": "stale"}]
        out, changed = es.repair_task_commit_shas(tasks, [{"episodeId": "P1", "commitShas": ["x", "y"]}])
        self.assertTrue(changed)
        self.assertIsNone(out[0]["commitSha"])

    def test_non_commit_cards_untouched(self):
        tasks = [{"id": "t", "episodeId": "P1", "title": "a todo"}]   # no commitSha
        out, changed = es.repair_task_commit_shas(tasks, [{"episodeId": "P1", "commitShas": []}])
        self.assertFalse(changed)


class SyncIntentTasksTest(unittest.TestCase):
    """Server-durable OpenPM cards for an intent-graph run — the source of truth (tasks.json)."""

    STEPS = [{"boxId": "b1", "title": "read"}, {"boxId": "b2", "title": "train"}]

    def test_start_opens_doing_tasks_one_per_step(self):
        tasks, changed = es.sync_intent_tasks([], episode_id="ep1", run_id="r1",
                                              tag="read -> train", steps=self.STEPS)
        self.assertTrue(changed)
        self.assertEqual([t["title"] for t in tasks], ["read", "train"])
        self.assertTrue(all(t["column"] == "doing" and t["verificationStatus"] == "pending"
                            and t["source"] == "intent-graph" for t in tasks))
        self.assertEqual([t["linkedBoxIds"] for t in tasks], [["b1"], ["b2"]])

    def test_landed_marks_done_with_commit_and_files_no_dup(self):
        tasks, _ = es.sync_intent_tasks([], episode_id="ep1", run_id="r1", tag="x", steps=self.STEPS)
        landed = [{"boxId": "b1", "title": "read", "files": ["openfde_work/p.py"]},
                  {"boxId": "b2", "title": "train", "files": ["openfde_work/p.py"]}]
        out, changed = es.sync_intent_tasks(tasks, episode_id="ep1", run_id="r1", tag="x",
                                            steps=landed, committed=True, commit_sha="abc123")
        self.assertTrue(changed)
        self.assertEqual(len(out), 2)                                  # updated in place, never duplicated
        self.assertTrue(all(t["column"] == "done" and t["verificationStatus"] == "passed"
                            and t["commitSha"] == "abc123" and t["files"] == ["openfde_work/p.py"]
                            for t in out))

    def test_rerun_is_idempotent(self):
        t1, _ = es.sync_intent_tasks([], episode_id="ep1", run_id="r1", tag="x",
                                     steps=self.STEPS, committed=True, commit_sha="abc")
        t2, changed = es.sync_intent_tasks(t1, episode_id="ep1", run_id="r1", tag="x",
                                           steps=self.STEPS, committed=True, commit_sha="abc")
        self.assertFalse(changed)                                      # no change, no new cards
        self.assertEqual(len(t2), 2)

    def test_failed_run_marks_testing_failed(self):
        out, _ = es.sync_intent_tasks([], episode_id="ep2", run_id="r2", tag="x",
                                      steps=self.STEPS, failed=True)
        self.assertTrue(all(t["column"] == "testing" and t["verificationStatus"] == "failed"
                            for t in out))

    def test_episode_id_keys_idempotency_across_runid(self):
        # The start call and the land call share the SAME episode key, so a second runId never
        # spawns a duplicate card for the same step (the slice's no-dup invariant).
        t1, _ = es.sync_intent_tasks([], episode_id="ep1", run_id="r1", tag="x", steps=self.STEPS)
        t2, _ = es.sync_intent_tasks(t1, episode_id="ep1", run_id="r2", tag="x",
                                     steps=self.STEPS, committed=True, commit_sha="z")
        self.assertEqual(len(t2), 2)


if __name__ == "__main__":
    unittest.main()
