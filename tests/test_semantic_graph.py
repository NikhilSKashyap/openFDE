"""
Tests for openfde.semantic_graph — the Semantic Graph Adapter Layer (Step 37a).

Builds a tiny temp repo and exercises the deterministic providers end to end:
ast structure/imports, cross-file identifier tethers, provenance on every
artifact, and the partial-tether verifier warning.
"""

import tempfile
import unittest
from pathlib import Path

from openfde import semantic_graph as sg


class SemanticGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # pkg_a imports pkg_b; both reference the shared id "thing-one"; a web
        # file references it too -> a cross-language tether across 3 files.
        (self.root / "pkg_a.py").write_text(
            'from pkg_b import beta\n\n'
            'PROVIDER = "thing-one"\n\n'
            'def alpha(x):\n'
            '    """add one"""\n'
            '    return beta(x) + 1\n')
        (self.root / "pkg_b.py").write_text(
            'def beta(y):\n'
            '    return y\n\n'
            'NAME = "thing-one"\n')
        (self.root / "app.jsx").write_text(
            "const p = 'thing-one'\nexport function ui() { return p }\n")

    def tearDown(self):
        self.tmp.cleanup()

    # 1) ast provider extracts files, functions, and imports.
    def test_ast_provider_extracts_structure(self):
        graph = sg.build_graph(self.root)
        kinds = {n["kind"] for n in graph["nodes"]}
        self.assertIn("file", kinds)
        self.assertIn("function", kinds)
        names = {n.get("name") for n in graph["nodes"] if n["kind"] == "function"}
        self.assertIn("alpha", names)
        self.assertIn("beta", names)
        # alpha records its arg + a call candidate to beta
        alpha = next(n for n in graph["nodes"] if n.get("name") == "alpha")
        self.assertEqual(alpha["args"], ["x"])
        # import edge pkg_a -> pkg_b
        imports = [e for e in graph["edges"] if e["kind"] == "import"]
        self.assertTrue(any(e["from"] == "file::pkg_a.py" and e["to"] == "module::pkg_b"
                            for e in imports), imports)
        # call candidate alpha -> beta
        self.assertTrue(any(e["kind"] == "calls-candidate" and e["to"] == "symbol::beta"
                            for e in graph["edges"]))

    # 2) tether provider finds the repeated id across files (cross-language).
    def test_tether_provider_finds_repeated_id(self):
        graph = sg.build_graph(self.root)
        teth = {t["identifier"]: t for t in graph["tethers"]}
        self.assertIn("thing-one", teth)
        t = teth["thing-one"]
        self.assertEqual(t["kind"], "identifier")
        self.assertEqual(set(t["files"]), {"pkg_a.py", "pkg_b.py", "app.jsx"})
        self.assertGreaterEqual(t["fileCount"], 3)

    # 3) every node/edge/tether/risk carries provenance with the trust fields.
    def test_every_artifact_has_provenance(self):
        graph = sg.build_graph(self.root)
        for bucket in ("nodes", "edges", "tethers", "risks"):
            for art in graph[bucket]:
                self.assertIn("provenance", art, f"{bucket} missing provenance: {art}")
                p = art["provenance"]
                for field in ("tool", "version", "command", "source", "confidence"):
                    self.assertIn(field, p, f"{bucket} provenance missing {field}")
        # contract envelope
        for field in ("schemaVersion", "repoRoot", "generatedAt", "providerRuns"):
            self.assertIn(field, graph)
        self.assertTrue(any(r["provider"] == "ast" for r in graph["providerRuns"]))

    # 4) partial-tether warning fires when only some files of a tether change.
    def test_partial_tether_warning(self):
        graph = sg.build_graph(self.root)
        # touch only one of the three files holding "thing-one"
        warns = sg.tethers_partially_touched(graph, ["pkg_a.py"])
        hit = [w for w in warns if w["identifier"] == "thing-one"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["touched"], 1)
        self.assertEqual(hit[0]["total"], 3)
        self.assertIn("only 1 of them", hit[0]["message"])
        # touching ALL files of the tether -> no partial warning
        none = sg.tethers_partially_touched(graph, ["pkg_a.py", "pkg_b.py", "app.jsx"])
        self.assertFalse([w for w in none if w["identifier"] == "thing-one"])

    # 4b) concepts_for_files maps a commit's changed files to affected tethers.
    def test_concepts_for_files(self):
        graph = sg.build_graph(self.root)
        # a commit that touched only pkg_a.py affects "thing-one" partially
        partial = sg.concepts_for_files(graph, ["pkg_a.py"])
        hit = [c for c in partial if c["identifier"] == "thing-one"]
        self.assertEqual(len(hit), 1)
        self.assertTrue(hit[0]["partial"])
        self.assertEqual(hit[0]["touched"], 1)
        self.assertEqual(hit[0]["total"], 3)
        self.assertIn("pkg_b.py", hit[0]["untouchedFiles"])
        # a commit touching every file of the concept is NOT partial
        full = sg.concepts_for_files(graph, ["pkg_a.py", "pkg_b.py", "app.jsx"])
        fhit = [c for c in full if c["identifier"] == "thing-one"]
        self.assertEqual(len(fhit), 1)
        self.assertFalse(fhit[0]["partial"])

    # 5) summary is UI-ready (counts + top tethers + provider warnings).
    def test_graph_summary(self):
        graph = sg.build_graph(self.root)
        s = sg.graph_summary(graph)
        self.assertTrue(s["exists"])
        self.assertGreaterEqual(s["counts"]["tethers"], 1)
        self.assertTrue(any(t["identifier"] == "thing-one" for t in s["topTethers"]))


if __name__ == "__main__":
    unittest.main()
