"""
Tests for the general "Raise OpenFDE issue" path (openfde.feedback). The law:
the Architect (or the deterministic template) drafts a product-feedback issue that
is ALWAYS marker-stamped and scrubbed of the watched repo's data, label selection
prefers the seeded taxonomy, and drafting NEVER posts to GitHub (that is a separate,
explicit click).
"""
import json
import unittest

from openfde import feedback


class GeneralDraftTest(unittest.TestCase):
    def test_fallback_without_provider_has_marker_title_body(self):
        # No caller → the deterministic template; still a real, marker-stamped draft.
        out = feedback.draft_general("The Story view is confusing to navigate",
                                     "ux", {"view": "story", "openfdeVersion": "abc123"},
                                     repo_name="myrepo", caller=None)
        self.assertTrue(out["title"])
        self.assertTrue(out["body"])
        self.assertTrue(out["body"].startswith(feedback.GENERAL_MARKER))
        self.assertIn("kind=general-feedback", out["body"])
        self.assertEqual(out["source"], "OpenFDE · template")
        self.assertIn("story", out["body"])          # the curated context is included
        self.assertIn("abc123", out["body"])          # OpenFDE version, not repo data

    def test_draft_never_posts_no_url_or_subprocess(self):
        # Drafting is pure: it returns a draft to REVIEW — never a created issue.
        out = feedback.draft_general("perf is bad", "performance", {}, "r", caller=None)
        self.assertNotIn("url", out)
        self.assertEqual(set(out), {"title", "body", "source"})

    def test_repo_paths_tests_and_code_are_scrubbed(self):
        # The user pasted private repo detail into the description — none of it may
        # reach the tracker, whether the path was typed with the repo name or not.
        desc = ("Crash when I open /Users/me/secret-proj/app/models/user.py and run "
                "test_user_login; the helper in app/services/auth.py throws. "
                "Repo is secret-proj.\n```python\nsecret = 'k'\n```")
        out = feedback.draft_general(desc, "bug", {}, repo_name="secret-proj", caller=None)
        body = out["body"]
        for leak in ("/Users/me/secret-proj", "user.py", "auth.py", "test_user_login",
                     "secret-proj", "secret = 'k'"):
            self.assertNotIn(leak, body, f"leaked: {leak!r}")
        self.assertIn("<path>", body)
        self.assertIn("<test>", body)
        self.assertIn("<code omitted>", body)

    def test_llm_draft_is_scrubbed_and_sourced(self):
        # Even when the model echoes a repo path into its draft, the deterministic
        # scrub strips it; the source caption names the Architect.
        def caller(_sys, _user):
            return json.dumps({"title": "Canvas slow in src/Whiteboard.tsx",
                               "body": "The canvas at src/Whiteboard.tsx lags."})
        out = feedback.draft_general("canvas is slow", "performance",
                                     {"view": "whiteboard"}, repo_name="x", caller=caller)
        self.assertNotIn("Whiteboard.tsx", out["title"])
        self.assertNotIn("src/Whiteboard.tsx", out["body"])
        self.assertTrue(out["body"].startswith(feedback.GENERAL_MARKER))
        self.assertEqual(out["source"], "Architect · drafted from OpenFDE context")

    def test_llm_bad_json_falls_back_to_template(self):
        out = feedback.draft_general("hi", "bug", {}, "r", caller=lambda s, u: "not json")
        self.assertEqual(out["source"], "OpenFDE · template")
        self.assertTrue(out["body"].startswith(feedback.GENERAL_MARKER))

    def test_scrub_general_is_idempotent_safe_text(self):
        # Plain product feedback with no repo detail passes through unharmed.
        clean = "The Run button should show a spinner while checks execute."
        self.assertEqual(feedback.scrub_general(clean, {}), clean)


class LabelSelectionTest(unittest.TestCase):
    def test_prefers_seeded_kind_label_plus_auto_report(self):
        have = {n: d for n, d in feedback.SEED_LABELS}
        self.assertEqual(feedback.select_labels("ux", [], have), ["auto-report", "ux"])
        self.assertEqual(feedback.select_labels("performance", [], have),
                         ["auto-report", "performance"])

    def test_unknown_or_absent_labels_dropped_default_bug(self):
        have = {"auto-report": "", "bug": ""}
        # "other" has no kind label, hint not present → safe default bug.
        self.assertEqual(feedback.select_labels("other", ["nonexistent"], have),
                         ["auto-report", "bug"])

    def test_hint_and_classifier_picks_merge_filtered_and_capped(self):
        have = {n: "" for n in ("auto-report", "bug", "canvas", "openpm", "story", "council")}
        chosen = feedback.select_labels("bug", ["canvas"], have,
                                        picks=["openpm", "ghost", "story", "council"])
        self.assertEqual(chosen[0], "auto-report")
        self.assertIn("bug", chosen)
        self.assertIn("canvas", chosen)
        self.assertNotIn("ghost", chosen)            # not in `have` → dropped
        self.assertLessEqual(len(chosen), 5)          # capped
        self.assertEqual(len(chosen), len(set(chosen)))  # deduped

    def test_seed_taxonomy_covers_required_labels(self):
        names = {n for n, _ in feedback.SEED_LABELS}
        for required in ("bug", "feature", "ux", "performance", "auto-report", "canvas",
                         "openpm", "story", "council", "verify-gate", "language-pack",
                         "webxr"):
            self.assertIn(required, names, f"missing seed label: {required}")


if __name__ == "__main__":
    unittest.main()
