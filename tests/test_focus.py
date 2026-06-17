"""
L2-A focused path (openfde.focus) — neighborhood selection + scoped-verify choice.

Additive + conservative: whole-repo assimilation and the verify gate are unchanged; these are opt-in
helpers. Neighborhood tests use a synthetic ArchGraph (no repo needed); scoped-verify tests use a temp
repo so the existing verify discovery runs for real.
"""
import json
import tempfile
import unittest
from pathlib import Path

from openfde import focus

# a -> b -> c (import chain); f() in a.py flows to g() in d.py.
GRAPH = {
    "files": [{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}, {"path": "d.py"}],
    "fileEdges": [{"fromFile": "a.py", "toFile": "b.py", "type": "import"},
                  {"fromFile": "b.py", "toFile": "c.py", "type": "import"}],
    "flows": [{"fromId": "fn:a:f", "toId": "fn:d:g"}],
    "functions": [{"id": "fn:a:f", "name": "f", "path": "a.py"},
                  {"id": "fn:d:g", "name": "g", "path": "d.py"}],
}


def _repo(files: dict):
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d, root


class NeighborhoodTest(unittest.TestCase):
    def test_seed_file_returns_itself_first(self):
        r = focus.neighborhood(None, ["a.py"], graph=GRAPH, hops=1)
        self.assertEqual(r["mode"], "focused")
        self.assertEqual(r["files"][0], "a.py")          # seed first
        self.assertEqual(r["seeds"], ["a.py"])

    def test_direct_import_neighbor_included(self):
        r = focus.neighborhood(None, ["a.py"], graph=GRAPH, hops=1)
        self.assertIn("b.py", r["files"])                # a -> b import neighbor
        self.assertNotIn("c.py", r["files"])             # 2 hops away → excluded at hops=1

    def test_two_hops_reaches_further(self):
        r = focus.neighborhood(None, ["a.py"], graph=GRAPH, hops=2)
        self.assertIn("c.py", r["files"])                # a -> b -> c

    def test_function_flow_neighbor_included(self):
        r = focus.neighborhood(None, ["a.py"], graph=GRAPH, hops=1)
        self.assertIn("d.py", r["files"])                # flow f(a) -> g(d)
        self.assertIn("fn:d:g", {fn["id"] for fn in r["functions"]})
        self.assertTrue(any(e["type"] == "flow" for e in r["edges"]))

    def test_unknown_seed_warns_not_crash(self):
        r = focus.neighborhood(None, ["zzz.py"], graph=GRAPH, hops=1)
        self.assertTrue(r["ok"])
        self.assertEqual(r["files"], ["zzz.py"])         # seed still returned, no error
        self.assertTrue(any("seed not in repo graph" in w for w in r["warnings"]))

    def test_cap_is_enforced_deterministically(self):
        r = focus.neighborhood(None, ["a.py"], graph=GRAPH, hops=2, max_files=1)
        self.assertEqual(r["files"], ["a.py"])           # cap respected, seed kept first

    def test_no_graph_returns_seeds_with_warning(self):
        r = focus.neighborhood(None, ["a.py"], graph={})
        self.assertEqual(r["files"], ["a.py"])
        self.assertTrue(any("No ArchGraph edges" in w for w in r["warnings"]))

    def test_primary_path_files_are_included(self):
        r = focus.neighborhood(None, ["a.py"], graph=GRAPH, hops=0, primary_path=["impl.py"])
        self.assertIn("impl.py", r["files"])

    def test_additive_does_not_mutate_graph(self):
        before = json.dumps(GRAPH, sort_keys=True)
        focus.neighborhood(None, ["a.py"], graph=GRAPH, hops=2)
        self.assertEqual(json.dumps(GRAPH, sort_keys=True), before)  # whole-repo data untouched

    def test_provided_graph_is_used_without_reparse(self):
        # PERFORMANCE GUARD (L2-B): a provided graph must be used verbatim — analyze_repo is NEVER run.
        # A bogus root would crash a re-parse; passing graph= bypasses it entirely, so focus stays
        # bounded + deterministic (this is what the endpoint does with the server's cached ArchGraph).
        r = focus.neighborhood("/no/such/repo/anywhere", ["a.py"], graph=GRAPH, hops=1)
        self.assertEqual(r["mode"], "focused")
        self.assertIn("b.py", r["files"])                # derived from the provided graph, not a re-parse


class ScopedVerifyTest(unittest.TestCase):
    def test_picks_explicit_repro_check(self):
        d, root = _repo({"app.py": "x = 1\n"})
        with d:
            repro = {"id": "repro", "label": "Repro", "command": ["python", "-m", "pytest", "t.py"]}
            r = focus.scoped_verify(root, repro_check=repro)
        self.assertEqual(r["mode"], "scoped")
        self.assertEqual(r["checks"], [repro])

    def test_picks_pinned_verify_json(self):
        cfg = json.dumps([{"id": "custom", "command": ["echo", "hi"]}])
        d, root = _repo({".openfde/verify.json": cfg, "app.py": "x = 1\n"})
        with d:
            r = focus.scoped_verify(root)
        self.assertEqual(r["mode"], "scoped")
        self.assertTrue(r["checks"])

    def test_picks_obvious_tests_for_touched(self):
        d, root = _repo({"pkg/foo.py": "def f():\n    return 1\n",
                         "tests/test_foo.py": "def test_f():\n    assert True\n"})
        with d:
            r = focus.scoped_verify(root, touched_files=["pkg/foo.py"])
        self.assertEqual(r["mode"], "scoped")
        self.assertIn("tests/test_foo.py", r["checks"][0]["command"])
        self.assertTrue(r["warnings"])                   # honest: coverage not proven

    def test_falls_back_honestly_when_uncertain(self):
        d, root = _repo({"pkg/foo.py": "def f():\n    return 1\n",
                         "tests/test_other.py": "def test_o():\n    assert True\n"})
        with d:
            r = focus.scoped_verify(root, touched_files=["pkg/foo.py"])   # no test_foo.py
        self.assertEqual(r["mode"], "fallback")
        self.assertTrue(any("could not prove coverage" in w for w in r["warnings"]))


class CoerceRequestTest(unittest.TestCase):
    """Part 0: POST /api/focus/neighborhood body coercion — malformed input must NEVER 500."""

    def test_defaults_on_empty_or_bad_body(self):
        for bad in ({}, None, "nope", 5, []):
            self.assertEqual(focus.coerce_request(bad),
                             {"seeds": [], "hops": 1, "max_files": 40, "primary_path": None})

    def test_hops_default_and_clamp(self):
        self.assertEqual(focus.coerce_request({"hops": "abc"})["hops"], 1)   # garbage → default
        self.assertEqual(focus.coerce_request({"hops": 99})["hops"], 3)      # clamp high
        self.assertEqual(focus.coerce_request({"hops": -5})["hops"], 0)      # clamp low
        self.assertEqual(focus.coerce_request({"hops": "2"})["hops"], 2)     # numeric string ok

    def test_max_files_default_and_clamp(self):
        self.assertEqual(focus.coerce_request({"maxFiles": "x"})["max_files"], 40)
        self.assertEqual(focus.coerce_request({"maxFiles": 9999})["max_files"], 200)
        self.assertEqual(focus.coerce_request({"maxFiles": 0})["max_files"], 1)
        self.assertEqual(focus.coerce_request({"maxFiles": 10})["max_files"], 10)

    def test_seeds_only_list_of_strings(self):
        self.assertEqual(focus.coerce_request({"seeds": "a.py"})["seeds"], [])   # non-list → []
        self.assertEqual(focus.coerce_request({"seeds": ["a.py", 3, "", "b.py"]})["seeds"],
                         ["a.py", "b.py"])

    def test_primary_path_only_list_of_strings(self):
        self.assertIsNone(focus.coerce_request({"primaryPath": "x"})["primary_path"])
        self.assertEqual(focus.coerce_request({"primaryPath": ["impl.py", 1]})["primary_path"],
                         ["impl.py"])

    def test_coerced_garbage_runs_without_crashing(self):
        a = focus.coerce_request({"hops": "abc", "maxFiles": "x", "seeds": "nope"})
        r = focus.neighborhood(None, a["seeds"], hops=a["hops"], max_files=a["max_files"],
                               primary_path=a["primary_path"], graph=GRAPH)
        self.assertTrue(r["ok"])     # focused response, never a crash


class CoerceVerifyRequestTest(unittest.TestCase):
    """L2-B: POST /api/focus/verify-plan body coercion — malformed input must NEVER 500."""

    def test_defaults_on_empty_or_bad_body(self):
        for bad in ({}, None, "nope", 5, []):
            self.assertEqual(focus.coerce_verify_request(bad),
                             {"touched_files": [], "repro_check": None})

    def test_touched_files_only_list_of_strings(self):
        self.assertEqual(focus.coerce_verify_request({"touchedFiles": "a.py"})["touched_files"], [])
        self.assertEqual(
            focus.coerce_verify_request({"touchedFiles": ["a.py", 7, "", "b.py"]})["touched_files"],
            ["a.py", "b.py"])

    def test_repro_check_only_dict(self):
        self.assertIsNone(focus.coerce_verify_request({"reproCheck": "x"})["repro_check"])
        chk = {"id": "repro", "command": ["pytest", "-k", "x"]}
        self.assertEqual(focus.coerce_verify_request({"reproCheck": chk})["repro_check"], chk)

    def test_coerced_request_feeds_scoped_verify(self):
        d, root = _repo({"foo.py": "x = 1\n", "tests/test_foo.py": "def test_f():\n    assert True\n"})
        with d:
            a = focus.coerce_verify_request({"touchedFiles": ["foo.py", 3]})
            plan = focus.scoped_verify(root, touched_files=a["touched_files"], repro_check=a["repro_check"])
            self.assertEqual(plan["mode"], "scoped")            # obvious test matched
            self.assertTrue(any("not proven" in w for w in plan["warnings"]))   # honest, no overclaim


if __name__ == "__main__":
    unittest.main()
