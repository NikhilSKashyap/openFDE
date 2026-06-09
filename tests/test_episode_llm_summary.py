"""
Tests for openfde.episode_llm_summary — the LLM Story Summarizer (local CLI + deterministic
fallback). The CLI is mocked via the injectable ``invoke`` — Codex/Claude need not be installed.
"""

import unittest

from openfde import episode_llm_summary as ls


def _mock(json_text):
    """An invoke() that returns the same text for any provider."""
    return lambda provider, system, user, timeout: json_text


_GOOD = ('```json\n{"title":"Auto-Land Prompt Commits","summary":"Commits scoped per prompt.",'
         '"concepts":["Auto-Land","Scoped Commits"],"decisions":["Commit per prompt"],'
         '"deferred":["Tool-settled signal"],"abandoned":["Manual Land primary"],'
         '"operational":false,"confidence":0.82}\n```')


class ParseValidateTest(unittest.TestCase):
    def test_parse_strips_fences(self):
        obj = ls.parse_summary_json(_GOOD)
        self.assertEqual(obj["title"], "Auto-Land Prompt Commits")

    def test_parse_garbage_is_none(self):
        self.assertIsNone(ls.parse_summary_json("the model refused, no json here"))
        self.assertIsNone(ls.parse_summary_json(""))

    def test_validate_good(self):
        c = ls.validate(ls.parse_summary_json(_GOOD))
        self.assertEqual(c["title"], "Auto-Land Prompt Commits")
        self.assertEqual(c["concepts"], ["Auto-Land", "Scoped Commits"])
        self.assertFalse(c["operational"])
        self.assertAlmostEqual(c["confidence"], 0.82, places=2)

    def test_validate_rejects_shell_filelist_generic(self):
        self.assertIsNone(ls.validate({"title": "curl -s localhost:7441/api/x", "summary": "x"}))
        self.assertIsNone(ls.validate({"title": "ROADMAP.md", "summary": "x"}))
        self.assertIsNone(ls.validate({"title": "Yes", "summary": "ok"}))
        self.assertIsNone(ls.validate({"title": "", "summary": "x"}))
        self.assertIsNone(ls.validate({"title": "x" * 61, "summary": "x"}))

    def test_validate_rejects_boilerplate(self):
        for t in ["Here's the CC prompt", "You are implementing the slice",
                  "IMPORTANT — OpenFDE owns version control", "Read the repo"]:
            self.assertIsNone(ls.validate({"title": t, "summary": "x"}), t)

    def test_validate_caps_arrays_and_summary(self):
        c = ls.validate({"title": "Good Title Here", "summary": "y" * 400,
                         "concepts": [f"c{i}" for i in range(20)]})
        self.assertLessEqual(len(c["summary"]), 300)
        self.assertEqual(len(c["concepts"]), 6)


class SummarizeTest(unittest.TestCase):
    def test_summarize_success(self):
        out = ls.summarize_episode({"title": "x", "prompt": "build a thing"},
                                   invoke=_mock(_GOOD), providers=["codex-local"])
        self.assertEqual(out["title"], "Auto-Land Prompt Commits")
        self.assertEqual(out["summarySource"], "codex-local")

    def test_summarize_all_fail_returns_none(self):
        out = ls.summarize_episode({"title": "x", "prompt": "p"},
                                   invoke=_mock("garbage"), providers=["codex-local", "claude-local"])
        self.assertIsNone(out)

    def test_summarize_no_providers(self):
        self.assertIsNone(ls.summarize_episode({"prompt": "p"}, invoke=_mock(_GOOD), providers=[]))


class EnrichCacheTest(unittest.TestCase):
    def test_enrich_applies_llm_and_caches(self):
        ep = {"episodeId": "e1", "prompt": "implement story graph",
              "title": "Implement story graph", "files": ["a.py"], "commitShas": [], "signal": "product"}
        calls = {"n": 0}

        def inv(provider, system, user, timeout):
            calls["n"] += 1
            self.assertIn(ls.INTERNAL_MARKER, user)         # capture-safe marker present
            return _GOOD
        self.assertTrue(ls.enrich(ep, invoke=inv, providers=["codex-local"]))
        self.assertEqual(ep["title"], "Auto-Land Prompt Commits")
        self.assertEqual(ep["summarySource"], "codex-local")
        self.assertTrue(ep["storyFacts"]["concepts"])
        self.assertEqual(calls["n"], 1)
        # Second enrich: same fingerprint → NO new LLM call.
        ls.enrich(ep, invoke=inv, providers=["codex-local"])
        self.assertEqual(calls["n"], 1)

    def test_fingerprint_change_retriggers(self):
        ep = {"episodeId": "e2", "prompt": "Build the login flow", "title": "Build login flow",
              "files": [], "commitShas": [], "signal": "product"}
        n = {"c": 0}

        def inv(*a):
            n["c"] += 1
            return _GOOD
        ls.enrich(ep, invoke=inv, providers=["codex-local"])
        self.assertEqual(n["c"], 1)
        ep["commitShas"] = ["newsha"]                       # inputs changed → fingerprint changes
        ls.enrich(ep, invoke=inv, providers=["codex-local"])
        self.assertEqual(n["c"], 2)

    def test_llm_operational_excludes_from_signal(self):
        op = ('{"title":"Status Check","summary":"Operational.","concepts":[],"decisions":[],'
              '"deferred":[],"abandoned":[],"operational":true,"confidence":0.6}')
        ep = {"episodeId": "e3", "prompt": "is the server up?", "title": "Is the server up",
              "files": [], "commitShas": [], "signal": "product"}
        ls.enrich(ep, invoke=_mock(op), providers=["codex-local"])
        self.assertEqual(ep["signal"], "operational")
        self.assertTrue(ep["storyFacts"]["operational"])

    def test_deterministic_when_no_providers(self):
        ep = {"episodeId": "e4", "prompt": "Implement Prompt Story Graph v1\nDeferred: LLM summaries.",
              "title": "Implement Prompt Story Graph", "files": [], "commitShas": [], "signal": "product"}
        ls.enrich(ep, providers=[])                          # no LLM available
        self.assertEqual(ep["summarySource"], "deterministic")
        self.assertIn("LLM summaries", " ".join(ep["storyFacts"]["deferred"]))

    def test_repairs_bad_stored_title_from_heading(self):
        # An EXISTING episode stored with a bad title is re-derived from the prompt's Goal,
        # without disturbing its identity (sequence / tag / commitShas / files / prompt).
        ep = {"episodeId": "r1", "sequence": 5, "tag": "P5",
              "prompt": "Here's the CC prompt:\n\n## Goal\n\nImplement LLM Story Summarizer v1.",
              "title": "Here's the CC prompt", "summary": "stale", "files": ["a.py"],
              "commitShas": ["s1"], "signal": "product", "summarySource": "deterministic",
              "storyFacts": {"concepts": ["Here's the CC prompt"], "operational": False}}
        ls.enrich(ep, providers=[])                      # no LLM → deterministic repair
        self.assertEqual(ep["title"], "LLM Story Summarizer")
        self.assertEqual(ep["signal"], "product")
        self.assertIn("LLM Story Summarizer", ep["storyFacts"]["concepts"])
        # Identity preserved.
        self.assertEqual(ep["sequence"], 5)
        self.assertEqual(ep["tag"], "P5")
        self.assertEqual(ep["commitShas"], ["s1"])
        self.assertEqual(ep["files"], ["a.py"])
        self.assertIn("## Goal", ep["prompt"])

    def test_repairs_bad_title_to_operational(self):
        ep = {"episodeId": "r2", "prompt": "yes", "title": "Yes", "files": [], "commitShas": [],
              "signal": "product", "summarySource": "deterministic",
              "storyFacts": {"concepts": ["Yes"], "operational": False}}
        ls.enrich(ep, providers=[])
        self.assertEqual(ep["signal"], "operational")
        self.assertTrue(ep["storyFacts"]["operational"])
        self.assertEqual(ep["storyFacts"]["concepts"], [])   # operational → no active concept

    def test_wrapper_prompt_heals_to_neutral_operational_title(self):
        # A meta wrapper prompt whose own first line is boilerplate with a CURLY apostrophe
        # ("Here's the Claude Code prompt: …") can't be skipped by the deterministic re-derive,
        # so the title must heal to a clean NEUTRAL operational label — never the raw line —
        # stay operational (out of Story), and keep the raw prompt as evidence.
        ep = {"episodeId": "r3", "sequence": 16, "tag": "P16",
              "prompt": "Here’s the Claude Code prompt:\n\nYou are acting as senior dev.\n\nDo a polish pass.",
              "title": "Here’s the Claude Code prompt", "summary": "stale",
              "files": [], "commitShas": [], "signal": "operational", "summarySource": "deterministic"}
        ls.enrich(ep, providers=[])                          # no LLM → deterministic heal
        self.assertEqual(ep["title"], "Claude Code Implementation Prompt")
        self.assertNotIn("Here", ep["title"])                 # raw wrapper line never shown
        self.assertEqual(ep["signal"], "operational")         # stays operational
        self.assertEqual(ep["storyFacts"]["concepts"], [])    # → kept out of Story
        self.assertEqual(ep["tag"], "P16")                    # identity preserved
        self.assertTrue(ep["prompt"].startswith("Here’s the Claude Code prompt"))  # evidence kept

    def test_ensure_facts_persists(self):
        import tempfile
        from pathlib import Path
        from openfde.persistence import Persistence
        with tempfile.TemporaryDirectory() as d:
            p = Persistence(Path(d))
            p.upsert_episode({"episodeId": "e", "prompt": "build x", "title": "Build x",
                              "files": [], "commitShas": [], "signal": "product"})
            ls.ensure_facts(p, allow_llm=False)              # deterministic only
            self.assertTrue(p.get_episode("e").get("storyFacts"))
            self.assertEqual(p.get_episode("e").get("summarySource"), "deterministic")


class ClusterChangesTest(unittest.TestCase):
    """Group an episode's changed files into logical commits (one commit → one OpenPM task)."""

    EP = {"title": "Multi-Commit Land", "prompt": "Make each logical change its own commit."}

    def test_llm_groups_files(self):
        files = ["openfde/autoland.py", "tests/test_autoland.py", "frontend/src/App.jsx"]
        out = ('{"commits":['
               '{"title":"Multi Commit Land","message":"Add clustered auto-land",'
               '"files":["openfde/autoland.py","tests/test_autoland.py"]},'
               '{"title":"OpenPM Tasks","message":"Show one task per commit","files":["frontend/src/App.jsx"]}]}')
        cl = ls.cluster_changes(self.EP, files, invoke=lambda *a: out, providers=["codex-local"])
        self.assertEqual([c["title"] for c in cl], ["Multi Commit Land", "OpenPM Tasks"])
        self.assertEqual(cl[0]["message"], "openfde: Add clustered auto-land")     # openfde: prefix
        self.assertEqual(sorted(f for c in cl for f in c["files"]), sorted(files))  # full coverage

    def test_llm_leftovers_go_to_misc(self):
        files = ["a.py", "b.py", "c.py"]
        out = '{"commits":[{"title":"Thing","message":"do thing","files":["a.py"]}]}'  # drops b,c
        cl = ls.cluster_changes(self.EP, files, invoke=lambda *a: out, providers=["codex-local"])
        self.assertEqual(sorted(f for c in cl for f in c["files"]), ["a.py", "b.py", "c.py"])
        self.assertTrue(any(c["title"] == "Misc Changes" for c in cl))

    def test_bad_json_falls_back_to_scope(self):
        files = ["openfde/x.py", "frontend/y.jsx"]
        cl = ls.cluster_changes(self.EP, files, invoke=lambda *a: "no json here",
                                providers=["codex-local"])
        self.assertEqual(len(cl), 2)                          # deterministic by scope
        self.assertEqual(sorted(f for c in cl for f in c["files"]), sorted(files))

    def test_deterministic_fallback_no_llm(self):
        files = ["openfde/x.py", "openfde/y.py", "frontend/z.jsx", "tests/t.py"]
        cl = ls.cluster_changes(self.EP, files, providers=[])  # force deterministic
        scopes = {tuple(sorted({f.split("/")[0] for f in c["files"]})) for c in cl}
        self.assertEqual(scopes, {("openfde",), ("frontend",), ("tests",)})   # one commit per scope
        self.assertEqual(sorted(f for c in cl for f in c["files"]), sorted(files))

    def test_single_file_single_commit(self):
        cl = ls.cluster_changes(self.EP, ["openfde/x.py"], providers=[])
        self.assertEqual(len(cl), 1)
        self.assertEqual(cl[0]["files"], ["openfde/x.py"])
        self.assertEqual(cl[0]["title"], "Multi-Commit Land")

    def test_cap_on_cluster_count(self):
        files = [f"openfde/f{i}.py" for i in range(20)]
        out = '{"commits":[' + ",".join(
            f'{{"title":"C{i}","message":"m{i}","files":["openfde/f{i}.py"]}}' for i in range(20)) + ']}'
        cl = ls.cluster_changes(self.EP, files, invoke=lambda *a: out, providers=["codex-local"])
        self.assertLessEqual(len(cl), 8)                      # _MAX_CLUSTERS


if __name__ == "__main__":
    unittest.main()
