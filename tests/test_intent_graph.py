"""Tests for openfde.intent_graph — Sketch-First Intent compilation + attribution,
and its integration into the shared compile_spec brief."""

import unittest

from openfde.intent_graph import (
    attribute_intent_files,
    compile_intent_graph,
    is_intent_box,
    render_intent_brief,
)
from openfde.spec import compile_spec


def _ibox(bid, title, prompt="", **extra):
    return {"id": bid, "title": title, "prompt": prompt, "kind": "intent",
            "type": "dotted", "linkedFiles": [], **extra}


def _module(bid, title, files=None):
    return {"id": bid, "title": title, "prompt": "a module", "type": "dotted",
            "linkedFiles": files or []}


def _arrow(aid, f, t, label=""):
    return {"id": aid, "fromBox": f, "toBox": t, "label": label}


class CompileIntentGraphTest(unittest.TestCase):
    def test_linear_chain_produces_ordered_steps(self):
        boxes = [_ibox("a", "read the data"), _ibox("b", "drop nans"),
                 _ibox("c", "train model")]
        arrows = [_arrow("1", "a", "b"), _arrow("2", "b", "c")]
        g = compile_intent_graph(boxes, arrows)
        self.assertTrue(g["present"])
        self.assertFalse(g["ambiguous"])
        self.assertEqual(g["ambiguityReason"], "")
        self.assertEqual([s["title"] for s in g["steps"]],
                         ["read the data", "drop nans", "train model"])
        self.assertEqual([s["order"] for s in g["steps"]], [1, 2, 3])
        self.assertIn("read the data → drop nans → train model", g["summary"])

    def test_arrows_override_canvas_order(self):
        # Boxes drawn out of order; arrows define the true sequence.
        boxes = [_ibox("c", "train model"), _ibox("a", "read the data"),
                 _ibox("b", "drop nans")]
        arrows = [_arrow("1", "a", "b"), _arrow("2", "b", "c")]
        g = compile_intent_graph(boxes, arrows)
        self.assertFalse(g["ambiguous"])
        self.assertEqual([s["title"] for s in g["steps"]],
                         ["read the data", "drop nans", "train model"])

    def test_no_arrows_is_ambiguous_and_falls_back_to_canvas_order(self):
        boxes = [_ibox("a", "read the data"), _ibox("b", "train model")]
        g = compile_intent_graph(boxes, [])
        self.assertTrue(g["ambiguous"])
        self.assertIn("no arrows", g["ambiguityReason"])
        self.assertEqual([s["title"] for s in g["steps"]],
                         ["read the data", "train model"])

    def test_cycle_is_ambiguous_with_honest_warning(self):
        boxes = [_ibox("a", "step a"), _ibox("b", "step b")]
        arrows = [_arrow("1", "a", "b"), _arrow("2", "b", "a")]
        g = compile_intent_graph(boxes, arrows)
        self.assertTrue(g["ambiguous"])
        self.assertIn("cycle", g["ambiguityReason"])
        self.assertEqual(len(g["steps"]), 2)   # still lists every step

    def test_branching_is_ambiguous_but_still_topo_ordered(self):
        # A fans out to B and C — a real order can't be inferred from arrows.
        boxes = [_ibox("a", "load"), _ibox("b", "branch one"), _ibox("c", "branch two")]
        arrows = [_arrow("1", "a", "b"), _arrow("2", "a", "c")]
        g = compile_intent_graph(boxes, arrows)
        self.assertTrue(g["ambiguous"])
        self.assertIn("branch", g["ambiguityReason"])
        self.assertEqual(g["steps"][0]["title"], "load")   # root still first

    def test_single_intent_step_is_not_ambiguous(self):
        g = compile_intent_graph([_ibox("a", "do the thing")], [])
        self.assertTrue(g["present"])
        self.assertFalse(g["ambiguous"])
        self.assertEqual(len(g["steps"]), 1)

    def test_mixed_architecture_and_intent_boxes_does_not_crash(self):
        # A real module + two intent steps; an arrow from the module to an intent
        # box must be ignored (only intent→intent edges define the sketch order).
        boxes = [_module("m", "Data module", ["data/io.py"]),
                 _ibox("a", "read the data"), _ibox("b", "clean it")]
        arrows = [_arrow("1", "m", "a"), _arrow("2", "a", "b")]
        g = compile_intent_graph(boxes, arrows)
        self.assertTrue(g["present"])
        self.assertFalse(g["ambiguous"])
        self.assertEqual([s["title"] for s in g["steps"]], ["read the data", "clean it"])
        self.assertEqual(len(g["edges"]), 1)   # only a→b, not m→a

    def test_no_intent_boxes_means_absent(self):
        g = compile_intent_graph([_module("m", "A module", ["x.py"])], [])
        self.assertFalse(g["present"])
        self.assertEqual(g["steps"], [])
        self.assertEqual(render_intent_brief(g), "")

    def test_prompt_falls_back_to_title_when_blank(self):
        g = compile_intent_graph([_ibox("a", "read the data", prompt="")], [])
        self.assertEqual(g["steps"][0]["prompt"], "read the data")


class RenderIntentBriefTest(unittest.TestCase):
    def test_brief_lists_steps_dataflow_and_acceptance(self):
        boxes = [_ibox("a", "read the data"), _ibox("b", "train model")]
        arrows = [_arrow("1", "a", "b", label="rows")]
        md = render_intent_brief(compile_intent_graph(boxes, arrows))
        self.assertIn("## Intent Graph (sketch)", md)
        self.assertIn("read the data", md)
        self.assertIn("train model", md)
        self.assertIn("Acceptance criteria", md)
        self.assertIn("link", md.lower())

    def test_ambiguous_brief_carries_the_warning(self):
        md = render_intent_brief(compile_intent_graph(
            [_ibox("a", "x"), _ibox("b", "y")], []))
        self.assertIn("best-effort", md)
        self.assertIn("no arrows", md)


class AttributeIntentFilesTest(unittest.TestCase):
    def test_named_step_flagged_higher_confidence(self):
        boxes = [_ibox("a", "read the data"), _ibox("b", "train model")]
        links = attribute_intent_files(boxes, ["pipeline.py"],
                                       named_text="implemented the read the data loader")
        self.assertEqual(links["a"]["attribution"], "named")
        self.assertEqual(links["b"]["attribution"], "graph")
        self.assertGreater(links["a"]["confidence"], links["b"]["confidence"])
        self.assertEqual(links["a"]["files"], ["pipeline.py"])

    def test_no_changed_files_returns_empty(self):
        self.assertEqual(attribute_intent_files([_ibox("a", "x")], []), {})

    def test_modules_are_ignored(self):
        self.assertEqual(attribute_intent_files([_module("m", "mod", ["x.py"])], ["x.py"]), {})


class SpecIntegrationTest(unittest.TestCase):
    EMPTY_GRAPH = {"files": [], "functions": [], "warnings": []}

    def test_compile_spec_includes_intent_graph(self):
        canvas = {
            "boxes": [_ibox("a", "read the data"), _ibox("b", "train model")],
            "arrows": [_arrow("1", "a", "b")],
        }
        out = compile_spec(canvas, [], {}, self.EMPTY_GRAPH, ["a", "b"], [], "")
        ig = out["context"]["intentGraph"]
        self.assertTrue(ig["present"])
        self.assertFalse(ig["ambiguous"])
        self.assertEqual([s["title"] for s in ig["steps"]], ["read the data", "train model"])
        self.assertIn("## Intent Graph (sketch)", out["markdown"])

    def test_compile_spec_without_intent_falls_back(self):
        # Pure architecture selection: intentGraph is absent and the markdown
        # carries no intent section, but the existing spec sections still render.
        canvas = {"boxes": [_module("m", "A module", ["x.py"])], "arrows": []}
        out = compile_spec(canvas, [], {}, self.EMPTY_GRAPH, ["m"], [], "")
        self.assertFalse(out["context"]["intentGraph"]["present"])
        self.assertNotIn("## Intent Graph (sketch)", out["markdown"])
        self.assertIn("## Selected Architecture", out["markdown"])
        self.assertIn("## Permission Boundaries", out["markdown"])


class PredicateTest(unittest.TestCase):
    def test_is_intent_box(self):
        self.assertTrue(is_intent_box({"kind": "intent"}))
        self.assertFalse(is_intent_box({"kind": "module"}))
        self.assertFalse(is_intent_box({}))
        self.assertFalse(is_intent_box(None))


if __name__ == "__main__":
    unittest.main()
