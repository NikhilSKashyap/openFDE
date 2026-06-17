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


if __name__ == "__main__":
    unittest.main()
