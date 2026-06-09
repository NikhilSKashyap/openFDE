"""
Tests for openfde.issue_intents — GitHub Issues as durable OpenPM intent (v1):
normalization, stable ids, idempotent import, closed-state preservation, and the
gh-CLI runners with an injected fake runner (no network, no real gh).
"""

import json
import unittest

from openfde.issue_intents import (
    GH_ISSUE_FIELDS,
    gh_issue_list,
    gh_issue_view,
    intent_id,
    intent_task_fields,
    normalize_issue,
    upsert_intent_task,
)

URL = "https://github.com/NikhilSKashyap/openFDE/issues/42"


def _issue(**over):
    base = {"number": 42, "title": "Glow misses backend files", "url": URL,
            "state": "OPEN", "labels": [{"name": "bug"}, {"name": "openfde"}],
            "body": "Watch glow only fires for scaffold-era files.\nMore detail here."}
    base.update(over)
    return base


class FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def fake_runner(payload, rc=0, err=""):
    """A subprocess.run stand-in that records the command and returns canned JSON."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return FakeProc(stdout=json.dumps(payload), returncode=rc, stderr=err)

    run.calls = calls
    return run


class NormalizeTest(unittest.TestCase):
    def test_gh_shape_normalizes(self):
        n = normalize_issue(_issue(state="open", number="42"))
        self.assertEqual(n["provider"], "github")
        self.assertEqual(n["issueNumber"], 42)          # numeric-ish coerced
        self.assertEqual(n["state"], "OPEN")            # uppercased
        self.assertEqual(n["labels"], ["bug", "openfde"])
        self.assertEqual(n["url"], URL)

    def test_plain_labels_and_html_url(self):
        n = normalize_issue({"number": 7, "title": "T", "html_url": URL,
                             "labels": ["bug", "", None]})
        self.assertEqual(n["labels"], ["bug"])
        self.assertEqual(n["url"], URL)                  # html_url fallback
        self.assertEqual(n["state"], "OPEN")             # default

    def test_malformed_rejected_cleanly(self):
        for bad in (None, [], "x",
                    {"title": "no number"},
                    {"number": "abc", "title": "t"},
                    {"number": 3, "title": "   "}):
            with self.assertRaises(ValueError):
                normalize_issue(bad)

    def test_intent_id(self):
        self.assertEqual(intent_id(URL, 42), "github:NikhilSKashyap/openFDE#42")
        self.assertEqual(intent_id("", 7), "github:#7")  # URL-less raw import


class TaskFieldsTest(unittest.TestCase):
    def test_card_is_planned_work_with_intent_source(self):
        f = intent_task_fields(normalize_issue(_issue()))
        self.assertEqual(f["column"], "todo")            # intent = planned, not done
        self.assertEqual(f["verificationStatus"], "pending")
        self.assertEqual(f["source"], "github-issue")
        self.assertEqual(f["title"], "Glow misses backend files")
        # description = first body line only
        self.assertEqual(f["description"], "Watch glow only fires for scaffold-era files.")
        self.assertEqual(f["intentId"], "github:NikhilSKashyap/openFDE#42")
        self.assertEqual(set(f["intentSource"]), {"provider", "issueNumber", "url",
                                                  "title", "state", "labels"})

    def test_long_body_first_line_capped(self):
        f = intent_task_fields(normalize_issue(_issue(body="x" * 400)))
        self.assertLessEqual(len(f["description"]), 160)
        self.assertTrue(f["description"].endswith("…"))


class UpsertTest(unittest.TestCase):
    def test_import_creates_todo_card(self):
        tasks, task, created = upsert_intent_task([], normalize_issue(_issue()),
                                                  make_id=lambda: "t1")
        self.assertTrue(created)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(task["id"], "t1")
        self.assertEqual(task["column"], "todo")
        self.assertEqual(task["intentSource"]["issueNumber"], 42)

    def test_repeated_import_is_idempotent(self):
        tasks, _, _ = upsert_intent_task([], normalize_issue(_issue()), make_id=lambda: "t1")
        tasks, task, created = upsert_intent_task(tasks, normalize_issue(_issue()))
        self.assertFalse(created)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(task["id"], "t1")               # same card, not a duplicate

    def test_reimport_refreshes_surface_but_preserves_board_state(self):
        tasks, _, _ = upsert_intent_task([], normalize_issue(_issue()), make_id=lambda: "t1")
        # user moves the card and links a box; issue later gets retitled + closed
        tasks[0] = {**tasks[0], "column": "doing", "verificationStatus": "passed",
                    "linkedBoxIds": ["box:module:openfde"]}
        updated_issue = normalize_issue(_issue(title="Glow: link all repo files",
                                               state="closed", labels=[{"name": "fixed"}]))
        tasks, task, created = upsert_intent_task(tasks, updated_issue)
        self.assertFalse(created)
        self.assertEqual(task["title"], "Glow: link all repo files")
        self.assertEqual(task["intentSource"]["state"], "CLOSED")
        self.assertEqual(task["intentSource"]["labels"], ["fixed"])
        self.assertEqual(task["column"], "doing")        # board state preserved
        self.assertEqual(task["verificationStatus"], "passed")
        self.assertEqual(task["linkedBoxIds"], ["box:module:openfde"])

    def test_closed_issue_keeps_card(self):
        tasks, _, _ = upsert_intent_task([], normalize_issue(_issue(state="CLOSED")))
        self.assertEqual(len(tasks), 1)                  # imported closed → still a card
        tasks, task, created = upsert_intent_task(tasks, normalize_issue(_issue(state="CLOSED")))
        self.assertEqual(len(tasks), 1)                  # and re-import never deletes
        self.assertFalse(created)
        self.assertEqual(task["intentSource"]["state"], "CLOSED")

    def test_other_tasks_untouched(self):
        other = {"id": "x", "title": "Manual card", "column": "doing", "linkedBoxIds": []}
        tasks, _, _ = upsert_intent_task([other], normalize_issue(_issue()))
        self.assertEqual(tasks[0], other)
        self.assertEqual(len(tasks), 2)


class GhRunnerTest(unittest.TestCase):
    def test_view_normalizes_and_shapes_command(self):
        run = fake_runner(_issue())
        intent = gh_issue_view(42, cwd="/tmp", runner=run)
        self.assertEqual(intent["issueNumber"], 42)
        self.assertEqual(run.calls[0][:4], ["gh", "issue", "view", "42"])
        self.assertIn(GH_ISSUE_FIELDS, run.calls[0])

    def test_list_normalizes_each(self):
        run = fake_runner([_issue(), _issue(number=43, title="Second")])
        intents = gh_issue_list(cwd="/tmp", runner=run)
        self.assertEqual([i["issueNumber"] for i in intents], [42, 43])
        self.assertEqual(run.calls[0][:3], ["gh", "issue", "list"])

    def test_gh_failure_raises_runtime_error(self):
        run = fake_runner({}, rc=1, err="gh: Not Found (HTTP 404)")
        with self.assertRaises(RuntimeError):
            gh_issue_view(999, cwd="/tmp", runner=run)


if __name__ == "__main__":
    unittest.main()
