"""Tests for openfde.intent_graph — Sketch-First Intent compilation + attribution,
and its integration into the shared compile_spec brief."""

import tempfile
import unittest
from pathlib import Path

from openfde import prompt_story
from openfde.agent_runner import build_system_prompt, path_in_scope, run_agent
from openfde.intent_graph import (
    GENERATED_WORKSPACE,
    attribute_intent_files,
    compile_intent_graph,
    is_intent_box,
    merge_step_files,
    render_intent_brief,
    resolve_run_scope,
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


# ─── Runnable intent-only graphs (generated workspace) ────────────────────── #

def _ig(present):
    return {"present": present, "steps": [{"order": 1}] if present else []}


class ResolveRunScopeTest(unittest.TestCase):
    def test_intent_only_opens_generated_workspace(self):
        # (1) intent-only selection resolves to a runnable workspace scope —
        # it must NOT be rejected the way a fileless architecture selection is.
        self.assertEqual(resolve_run_scope([], [], _ig(True)),
                         ([GENERATED_WORKSPACE], [], True))

    def test_pure_architecture_no_files_still_returns_none(self):
        # (2) no editable files AND no intent graph → None → caller keeps the 400.
        self.assertIsNone(resolve_run_scope([], [], _ig(False)))

    def test_mixed_keeps_editable_and_does_not_widen(self):
        # (3) dotted linked files present → scope unchanged, workspace NOT added,
        # protected preserved — the permission boundary is not weakened.
        self.assertEqual(resolve_run_scope(["m/x.py"], ["s/y.py"], _ig(True)),
                         (["m/x.py"], ["s/y.py"], False))

    def test_architecture_only_with_files_unchanged(self):
        self.assertEqual(resolve_run_scope(["m/x.py"], [], _ig(False)),
                         (["m/x.py"], [], False))


class SystemPromptScopeTest(unittest.TestCase):
    def test_generated_workspace_in_system_prompt(self):
        # (4) the generated scope reaches the Senior Dev prompt as a new-build workspace.
        sp = build_system_prompt("intent workspace", [GENERATED_WORKSPACE], ["model.py"])
        self.assertIn(GENERATED_WORKSPACE, sp)
        self.assertIn("NEW build", sp)
        self.assertIn("model.py", sp)   # protected file still listed as never-write

    def test_normal_file_scope_prompt_unchanged(self):
        sp = build_system_prompt("2 files", ["a/b.py"], [])
        self.assertIn("a/b.py", sp)
        self.assertNotIn("NEW build", sp)


class PathInScopeTest(unittest.TestCase):
    def test_directory_prefix_matches_paths_beneath(self):
        self.assertTrue(path_in_scope("openfde_work/pipeline.py", {"openfde_work/"}))
        self.assertTrue(path_in_scope("openfde_work/sub/m.py", {"openfde_work/"}))

    def test_exact_file_scope_still_matches(self):
        self.assertTrue(path_in_scope("ingest/reader.py", {"ingest/reader.py"}))

    def test_outside_workspace_rejected(self):
        self.assertFalse(path_in_scope("model.py", {"openfde_work/"}))
        # No false-positive on a sibling that merely shares the prefix string.
        self.assertFalse(path_in_scope("openfde_workX/m.py", {"openfde_work/"}))


def _tool_use(name, **inp):
    return {"id": f"t_{name}", "type": "tool_use", "name": name, "input": inp}


def _resp(*blocks):
    return {"stop_reason": "tool_use", "content": list(blocks)}


class _ScriptedTransport:
    """Replays a fixed list of Anthropic-shaped responses, one per round-trip."""

    def __init__(self, responses):
        self._responses = list(responses)

    def __call__(self, request):
        if self._responses:
            return self._responses.pop(0)
        return {"stop_reason": "end_turn", "content": []}


class WorkspaceWriteEnforcementTest(unittest.TestCase):
    def test_workspace_allows_new_file_and_rejects_existing(self):
        # (5) write enforcement: a new file under the generated workspace is written;
        # a protected existing file and an out-of-scope existing file are both blocked
        # and left byte-for-byte unchanged.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "model.py").write_text("orig\n", encoding="utf-8")   # protected
            (root / "other.py").write_text("orig\n", encoding="utf-8")   # out-of-scope
            responses = [
                _resp(
                    _tool_use("write_file", path="openfde_work/pipeline.py", content="print('hi')\n"),
                    _tool_use("write_file", path="model.py", content="HACKED\n"),
                    _tool_use("write_file", path="other.py", content="HACKED\n"),
                ),
                _resp(_tool_use("submit_result", status="passed", reportSummary="built the pipeline")),
            ]
            out = run_agent(
                _ScriptedTransport(responses), model="m",
                system=build_system_prompt("intent workspace", ["openfde_work/"], ["model.py"]),
                user_prompt="build it", root=root,
                editable_files=["openfde_work/"], protected_files=["model.py"],
            )
            rejected = {r["path"]: r["reason"] for r in out["rejected"]}
            self.assertIn("openfde_work/pipeline.py", out["writes"])
            self.assertEqual((root / "openfde_work/pipeline.py").read_text(encoding="utf-8"),
                             "print('hi')\n")
            self.assertEqual(rejected.get("model.py"), "protected")
            self.assertEqual((root / "model.py").read_text(encoding="utf-8"), "orig\n")
            self.assertEqual(rejected.get("other.py"), "out-of-scope")
            self.assertEqual((root / "other.py").read_text(encoding="utf-8"), "orig\n")

    def test_workspace_scope_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "secret.py").write_text("orig\n", encoding="utf-8")
            responses = [
                _resp(_tool_use("write_file", path="openfde_work/../secret.py", content="HACKED\n")),
                _resp(_tool_use("submit_result", status="passed", reportSummary="x")),
            ]
            out = run_agent(
                _ScriptedTransport(responses), model="m", system="s",
                user_prompt="x", root=root,
                editable_files=["openfde_work/"], protected_files=[],
            )
            # The '..' canonicalises to secret.py (outside the workspace) → rejected.
            self.assertEqual((root / "secret.py").read_text(encoding="utf-8"), "orig\n")
            self.assertNotIn("secret.py", out["writes"])


class WorkspaceAttributionTest(unittest.TestCase):
    def test_generated_files_attach_to_all_intent_steps(self):
        # (6) intent link-back: files created under the workspace attach to every
        # selected intent step (the whole sketch shares them, labelled).
        boxes = [_ibox("a", "read the data"), _ibox("b", "train model")]
        changed = ["openfde_work/pipeline.py", "openfde_work/test_pipeline.py"]
        links = attribute_intent_files(boxes, changed)
        self.assertEqual(set(links["a"]["files"]), set(changed))
        self.assertEqual(set(links["b"]["files"]), set(changed))


class StorySketchTickTest(unittest.TestCase):
    """v2: an intent-graph episode carries its sketch into Story as an origin tick."""

    def _ep(self, intent_source):
        return {"episodeId": "e1", "createdAt": "2026-06-21T00:00:00+00:00",
                "updatedAt": "2026-06-21T00:00:00+00:00",
                "files": ["openfde_work/pipeline.py"], "commitShas": [],
                "intentSource": intent_source}

    def test_intent_graph_episode_emits_sketch_origin_tick(self):
        from openfde import prompt_story
        ep = self._ep({"kind": "intent-graph", "ref": "read data → clean → train",
                       "steps": [{"boxId": "a", "title": "read data"},
                                 {"boxId": "b", "title": "clean"},
                                 {"boxId": "c", "title": "train"}]})
        ticks = prompt_story._episode_ticks(ep)
        sketch = [t for t in ticks if t["kind"] == "sketch"]
        self.assertEqual(len(sketch), 1)
        self.assertIn("read data → clean → train", sketch[0]["label"])
        self.assertIn("read data", sketch[0]["detail"])

    def test_non_intent_episode_has_no_sketch_tick(self):
        from openfde import prompt_story
        ticks = prompt_story._episode_ticks(self._ep({"provider": "github", "issueNumber": 5}))
        self.assertEqual([t for t in ticks if t["kind"] == "sketch"], [])


class MergeStepFilesTest(unittest.TestCase):
    """Gap 1: an intent-graph episode stores each step's produced files (keyed by boxId),
    computed only after the run, without disturbing the step's other fields."""

    def test_attaches_files_per_step_preserving_fields(self):
        steps = [{"boxId": "a", "title": "read"}, {"boxId": "b", "title": "train"}]
        links = {"a": {"files": ["openfde_work/p.py"]},
                 "b": {"files": ["openfde_work/p.py", "openfde_work/t.py"]}}
        out = merge_step_files(steps, links)
        self.assertEqual(out[0], {"boxId": "a", "title": "read", "files": ["openfde_work/p.py"]})
        self.assertEqual(out[1]["title"], "train")
        self.assertEqual(out[1]["files"], ["openfde_work/p.py", "openfde_work/t.py"])

    def test_missing_link_yields_empty_files(self):
        out = merge_step_files([{"boxId": "a", "title": "x"}], {})
        self.assertEqual(out[0]["files"], [])

    def test_empty_steps(self):
        self.assertEqual(merge_step_files([], {"a": {"files": ["x"]}}), [])


class StoryStepFilesTest(unittest.TestCase):
    """Gap 2: Story timeline + narrative nodes carry intent.steps[].files, not just titles."""

    SRC = {"kind": "intent-graph", "ref": "read → train",
           "steps": [{"boxId": "a", "title": "read", "files": ["openfde_work/p.py"]},
                     {"boxId": "b", "title": "train",
                      "files": ["openfde_work/p.py", "openfde_work/t.py"]}]}

    def test_intent_node_carries_per_step_files(self):
        node = prompt_story._intent_node(self.SRC)
        self.assertEqual(node["sketch"], "read → train")
        self.assertEqual(node["steps"][0]["title"], "read")
        self.assertEqual(node["steps"][0]["files"], ["openfde_work/p.py"])
        self.assertEqual(node["steps"][0]["commits"], [])   # no episode → no commits
        self.assertEqual(node["steps"][1]["files"], ["openfde_work/p.py", "openfde_work/t.py"])

    def test_narrative_node_carries_per_step_files(self):
        ep = {"episodeId": "e1", "tag": "P1", "title": "t",
              "createdAt": "2026-06-21T00:00:00+00:00", "status": "landed",
              "files": ["openfde_work/p.py"], "intentSource": self.SRC}
        node = prompt_story._nv_node(ep, "now", "reason", "high")
        self.assertIsNotNone(node["intent"])
        self.assertEqual(node["intent"]["steps"][0]["files"], ["openfde_work/p.py"])

    def test_timeline_spine_node_carries_per_step_files(self):
        ep = {"episodeId": "e1", "tag": "P1", "title": "t", "summary": "",
              "createdAt": "2026-06-21T00:00:00+00:00", "updatedAt": "2026-06-21T00:00:00+00:00",
              "status": "landed", "sequence": 1, "files": ["openfde_work/p.py"],
              "commitShas": [], "intentSource": self.SRC}
        timeline = prompt_story.build_story_timeline([ep], [])
        spine = timeline["spine"]
        self.assertEqual(len(spine), 1)
        self.assertEqual(spine[0]["intent"]["steps"][0]["files"], ["openfde_work/p.py"])

    def test_non_intent_episode_has_no_intent_node(self):
        self.assertIsNone(prompt_story._intent_node({"provider": "github"}))


class StoryStepCommitsTest(unittest.TestCase):
    """v3: each Story intent step carries the commit(s) that landed its files, derived from the
    episode's per-commit matchedFiles (with an episode-level overlap fallback)."""

    SRC = {"kind": "intent-graph", "ref": "read → train",
           "steps": [{"boxId": "a", "title": "read", "files": ["openfde_work/intent_demo.py"]},
                     {"boxId": "b", "title": "train", "files": ["other.py"]}]}

    def test_commits_for_files_uses_matched_then_overlap(self):
        ep = {"files": ["a.py", "b.py"], "commitShas": ["s1", "s2"],
              "commitMeta": {"s1": {"matchedFiles": ["a.py"]}, "s2": {"matchedFiles": ["b.py"]}}}
        self.assertEqual(prompt_story._commits_for_files(ep, ["a.py"]), ["s1"])
        self.assertEqual(prompt_story._commits_for_files(ep, ["b.py"]), ["s2"])
        self.assertEqual(prompt_story._commits_for_files(ep, ["a.py", "b.py"]), ["s1", "s2"])
        self.assertEqual(prompt_story._commits_for_files(ep, []), [])
        self.assertEqual(prompt_story._commits_for_files(None, ["a.py"]), [])

    def test_per_step_commits_via_matched_files(self):
        ep = {"files": ["openfde_work/intent_demo.py", "other.py"],
              "commitShas": ["sha_demo", "sha_other"],
              "commitMeta": {"sha_demo": {"matchedFiles": ["openfde_work/intent_demo.py"]},
                             "sha_other": {"matchedFiles": ["other.py"]}}}
        steps = {s["title"]: s for s in prompt_story._intent_node(self.SRC, ep)["steps"]}
        self.assertEqual(steps["read"]["commits"], ["sha_demo"])
        self.assertEqual(steps["train"]["commits"], ["sha_other"])

    def test_fallback_to_episode_overlap_when_no_matched(self):
        ep = {"files": ["openfde_work/intent_demo.py", "other.py"],
              "commitShas": ["sha1"], "commitMeta": {}}
        steps = {s["title"]: s for s in prompt_story._intent_node(self.SRC, ep)["steps"]}
        self.assertEqual(steps["read"]["commits"], ["sha1"])
        self.assertEqual(steps["train"]["commits"], ["sha1"])

    def test_no_episode_means_no_commits(self):
        node = prompt_story._intent_node(self.SRC)
        self.assertTrue(all(s["commits"] == [] for s in node["steps"]))

    def test_narrative_node_carries_per_step_commits(self):
        ep = {"episodeId": "e1", "tag": "P1", "title": "t",
              "createdAt": "2026-06-21T00:00:00+00:00", "status": "landed",
              "files": ["openfde_work/intent_demo.py"], "commitShas": ["sha_demo"],
              "commitMeta": {"sha_demo": {"matchedFiles": ["openfde_work/intent_demo.py"]}},
              "intentSource": {"kind": "intent-graph", "ref": "read",
                               "steps": [{"title": "read", "files": ["openfde_work/intent_demo.py"]}]}}
        node = prompt_story._nv_node(ep, "now", "reason", "high")
        self.assertEqual(node["intent"]["steps"][0]["commits"], ["sha_demo"])


if __name__ == "__main__":
    unittest.main()
