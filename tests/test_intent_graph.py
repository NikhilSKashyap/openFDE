"""Tests for openfde.intent_graph — Sketch-First Intent compilation + attribution,
and its integration into the shared compile_spec brief."""

import tempfile
import unittest
from pathlib import Path

from openfde import prompt_story
from openfde.agent_runner import build_system_prompt, path_in_scope, run_agent
from openfde.intent_graph import (
    GENERATED_WORKSPACE,
    architecturize_intent_box,
    attribute_intent_files,
    compile_intent_graph,
    is_intent_box,
    merge_step_files,
    module_title_from_file,
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

    def test_role_named_files_map_per_step(self):
        # Demo payoff: each box grounds into its OWN file when the run's filenames echo the step
        # titles (ingest customer messages -> ingest.py). Generic — by filename↔title word overlap,
        # prefix-tolerant so "log resolution" -> logging.py.
        boxes = [_ibox("inbox_ingest", "ingest customer messages"),
                 _ibox("inbox_classify", "classify issue"),
                 _ibox("inbox_log", "log resolution")]
        changed = ["openfde_work/support_inbox/__init__.py",
                   "openfde_work/support_inbox/classify.py",
                   "openfde_work/support_inbox/ingest.py",
                   "openfde_work/support_inbox/logging.py"]
        links = attribute_intent_files(boxes, changed)
        self.assertEqual(links["inbox_ingest"]["files"], ["openfde_work/support_inbox/ingest.py"])
        self.assertEqual(links["inbox_classify"]["files"], ["openfde_work/support_inbox/classify.py"])
        self.assertEqual(links["inbox_log"]["files"], ["openfde_work/support_inbox/logging.py"])
        self.assertEqual(links["inbox_ingest"]["attribution"], "matched")

    def test_unmatched_step_keeps_coarse_share(self):
        # A step whose title echoes no filename falls back to the honest whole-sketch set.
        boxes = [_ibox("a", "ingest customer messages"), _ibox("b", "do something else")]
        changed = ["openfde_work/ingest.py", "openfde_work/helpers.py"]
        links = attribute_intent_files(boxes, changed)
        self.assertEqual(links["a"]["files"], ["openfde_work/ingest.py"])      # specific match
        self.assertEqual(set(links["b"]["files"]), set(changed))              # coarse fallback


class ArchitecturizeIntentBoxTest(unittest.TestCase):
    """Intent → architecture in place: a built step with clear single-file attribution becomes a
    module box, remembering its origin; an unclear one stays a built intent box."""

    def test_module_title_from_file(self):
        self.assertEqual(module_title_from_file("openfde_work/support_inbox/ingest.py"), "ingest/")
        self.assertEqual(module_title_from_file("a/b/classify.py"), "classify/")
        self.assertEqual(module_title_from_file("x/__init__.py"), "init/")

    def test_matched_single_file_becomes_built_architecture(self):
        box = _ibox("inbox_ingest", "ingest customer messages", "pull new customer messages")
        box["runState"] = "built"
        link = {"files": ["openfde_work/support_inbox/ingest.py"], "attribution": "matched", "confidence": 0.75}
        out = architecturize_intent_box(box, link, episode_id="ep1", run_id="run1")
        self.assertIs(out, box)                                  # mutated in place
        self.assertEqual(box["title"], "ingest/")               # module-ish title
        # Honest persisted model: BUILT architecture module, not a draft intent box.
        self.assertEqual(box["kind"], "module")
        self.assertEqual(box["status"], "built")
        self.assertNotIn("runState", box)                       # no intent lifecycle
        self.assertEqual(box["type"], "dotted")                 # stays editable (FDE-refinable)
        self.assertEqual(box["linkedFiles"], ["openfde_work/support_inbox/ingest.py"])
        self.assertEqual(box["implementationFiles"], ["openfde_work/support_inbox/ingest.py"])
        # Origin preserved: id, original title/prompt, episode/run, files.
        origin = box["originIntent"]
        self.assertEqual(origin["boxId"], "inbox_ingest")
        self.assertEqual(origin["title"], "ingest customer messages")
        self.assertEqual(origin["prompt"], "pull new customer messages")
        self.assertEqual(origin["episodeId"], "ep1")
        self.assertEqual(origin["runId"], "run1")
        self.assertEqual(origin["files"], ["openfde_work/support_inbox/ingest.py"])

    def test_built_state_is_generic_across_domains(self):
        # Same planned→built transform for ANY flow — no support-inbox titles/ids/paths hardcoded.
        cases = [
            ("etl_extract", "extract raw events", "pipeline/extract.py", "extract/"),
            ("ins_quote", "quote the policy", "underwriting/quote.py", "quote/"),
            ("xr_scene", "render the scene", "webxr/scene.py", "scene/"),
        ]
        for bid, title, path, want_title in cases:
            box = _ibox(bid, title, f"do: {title}")
            box["runState"] = "built"
            out = architecturize_intent_box(box, {"files": [path], "attribution": "matched"},
                                            episode_id="epX", run_id="runX")
            self.assertIsNotNone(out)
            self.assertEqual(box["kind"], "module")             # built architecture, any domain
            self.assertEqual(box["status"], "built")
            self.assertEqual(box["title"], want_title)          # title derived from the file
            self.assertEqual(box["linkedFiles"], [path])
            self.assertEqual(box["originIntent"]["title"], title)   # provenance kept
            self.assertEqual(box["id"], bid)                    # id (position-stable) unchanged

    def test_coarse_attribution_stays_intent(self):
        box = _ibox("a", "read the data")
        link = {"files": ["openfde_work/intent_demo.py"], "attribution": "graph", "confidence": 0.4}
        self.assertIsNone(architecturize_intent_box(box, link))
        self.assertEqual(box["kind"], "intent")                 # untouched → still intent
        self.assertNotIn("originIntent", box)
        self.assertNotIn("status", box)                         # not flipped to built

    def test_multi_file_match_stays_intent(self):
        box = _ibox("a", "ingest customer messages")
        link = {"files": ["openfde_work/a.py", "openfde_work/b.py"], "attribution": "matched"}
        self.assertIsNone(architecturize_intent_box(box, link))  # not a clean single file
        self.assertEqual(box["kind"], "intent")


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


class IntentLoopStoryNodeTest(unittest.TestCase):
    """v4 (real intent Run loop): the Story node links each step back to its canvas box (boxId),
    mirrors the box lifecycle (built = grounded files), and carries the landing commit(s)."""

    def test_intent_node_carries_boxid_built_files_and_commits(self):
        from openfde import prompt_story
        ep = {"episodeId": "e9", "files": ["openfde_work/p.py"], "commitShas": ["abc123"],
              "commitMeta": {"abc123": {"matchedFiles": ["openfde_work/p.py"]}},
              "intentSource": {"kind": "intent-graph", "ref": "read → train",
                               "steps": [{"boxId": "box_read", "title": "read",
                                          "files": ["openfde_work/p.py"]},
                                         {"boxId": "box_train", "title": "train", "files": []}]}}
        node = prompt_story._intent_node(ep["intentSource"], ep)
        self.assertEqual(node["sketch"], "read → train")
        s0, s1 = node["steps"]
        # a built step: linked to its box, grounded files, marked built, owns the commit
        self.assertEqual(s0["boxId"], "box_read")
        self.assertEqual(s0["files"], ["openfde_work/p.py"])
        self.assertTrue(s0["built"])
        self.assertEqual(s0["commits"], ["abc123"])
        # an un-grounded step: still linked to its box, but not built and no commit
        self.assertEqual((s1["boxId"], s1["built"], s1["commits"]), ("box_train", False, []))

    def test_intent_node_none_for_non_intent_source(self):
        from openfde import prompt_story
        self.assertIsNone(prompt_story._intent_node({"provider": "github"}, {}))


class IntentEpisodeClassificationTest(unittest.TestCase):
    """An intent-graph run is a PRODUCT build, titled from its steps — NOT operational
    'Update <scope>'. The flattened 'Intent: read the data → …' prompt trips the
    shell/scaffolding heuristics ('read' is a scaffolding word), so the title/summary/signal
    must come from the structured steps, deterministically (no LLM required)."""

    def _intent_ep(self, **over):
        ep = {"episodeId": "e_int", "prompt": "Intent: read the data → drop nan values → train a classifier",
              "files": ["openfde_work/intent_demo.py"],
              "intentSource": {"kind": "intent-graph",
                               "ref": "read the data → drop nan values → train a classifier",
                               "steps": [{"boxId": "a", "title": "read the data"},
                                         {"boxId": "b", "title": "drop nan values"},
                                         {"boxId": "c", "title": "train a classifier"}]}}
        ep.update(over)
        return ep

    def test_intent_title_summary_from_steps(self):
        from openfde.episode_summary import intent_title_summary, is_intent_graph_episode
        title, summary = intent_title_summary(self._intent_ep())
        self.assertTrue(is_intent_graph_episode(self._intent_ep()))
        self.assertIn("Read the data", title)
        self.assertIn("train a classifier", title)
        self.assertIn("read the data", summary)
        self.assertIsNone(intent_title_summary({"prompt": "git log"}))

    def test_deterministic_story_facts_never_operational_for_intent(self):
        from openfde.episode_llm_summary import deterministic_story_facts
        self.assertFalse(deterministic_story_facts(self._intent_ep())["operational"])

    def test_enrich_corrects_a_deterministically_mislabelled_intent_episode(self):
        # The exact bad state the deterministic fallback produced before the fix.
        from openfde.episode_llm_summary import enrich
        ep = self._intent_ep(title="Update openfde_work", summary="Changes under openfde_work.",
                             signal="operational", summarySource="deterministic", summaryLlmTried=True,
                             storyFacts={"operational": True, "concepts": []})
        changed = enrich(ep, allow_llm=False)
        self.assertTrue(changed)
        self.assertEqual(ep["signal"], "product")
        self.assertNotEqual(ep["title"], "Update openfde_work")
        self.assertIn("Read the data", ep["title"])
        self.assertFalse(ep["storyFacts"]["operational"])
        self.assertEqual(ep["storyFacts"]["concepts"], [ep["title"]])   # concepts follow the corrected title
        self.assertNotIn("Update openfde_work", ep["storyFacts"]["concepts"])

    def test_enrich_preserves_a_real_llm_title(self):
        # A settled, persisted LLM upgrade (summarySource is a provider, fingerprint fresh) is
        # kept across a re-enrich — we only replace the generic deterministic fallback title.
        from openfde.episode_llm_summary import enrich, fingerprint
        ep = self._intent_ep(title="Classifier Training Pipeline", summary="A real summary.",
                             signal="product", summarySource="codex-local", summaryLlmTried=True,
                             summaryConfidence=0.9,
                             storyFacts={"operational": False, "concepts": ["Classifier Training Pipeline"]})
        ep["summaryFingerprint"] = fingerprint(ep)     # fresh → a re-enrich must not reset it
        enrich(ep, allow_llm=False)
        self.assertEqual(ep["title"], "Classifier Training Pipeline")
        self.assertEqual(ep["signal"], "product")

    def test_enrich_episode_first_paint_is_product(self):
        from openfde.episode_summary import enrich_episode
        ep = self._intent_ep()
        enrich_episode(ep, 0)
        self.assertEqual(ep["signal"], "product")
        self.assertIn("Read the data", ep["title"])

    def test_persisted_storyfacts_concepts_use_intent_title(self):
        # The PERSISTED metadata (not just the derived Story graph) must be clean: the bad-title
        # repair leaves "Update openfde_work" in storyFacts.concepts; the enrich end-guard
        # normalizes it to the intent title at the source, keeping operational False.
        from openfde.episode_summary import enrich_episode
        from openfde.episode_llm_summary import enrich
        ep = self._intent_ep(status="landed", commitShas=["abc1234"], sequence=1, tag="P1")
        enrich_episode(ep, 0)
        enrich(ep, allow_llm=False)
        sf = ep.get("storyFacts") or {}
        self.assertEqual(sf.get("concepts"), ["Read the data → drop nan values → train a classifier"])
        self.assertFalse(sf.get("operational"))
        self.assertEqual(ep["summary"], "Built from a sketch: read the data, drop nan values, train a classifier.")

    def test_story_concept_uses_intent_title_not_filepath_fallback(self):
        # The visible Story concept (column card + narrative) reads the intent title — never the
        # "Update openfde_work" file-path fallback (which build_prompt_graph would otherwise prefer
        # and then drop via is_bad_title, leaving NO concept at all).
        from openfde import prompt_story
        from openfde.episode_summary import enrich_episode
        from openfde.episode_llm_summary import enrich
        ep = self._intent_ep(status="landed", commitShas=["abc1234"], sequence=1, tag="P1")
        enrich_episode(ep, 0)
        enrich(ep, allow_llm=False)
        g = prompt_story.build_prompt_graph([ep])
        titles = [c["title"] for c in g["concepts"]]
        self.assertIn("Read the data → drop nan values → train a classifier", titles)
        self.assertNotIn("Update openfde_work", titles)
        # id is derived from the stable title (not the file fallback)
        self.assertTrue(any("read_the_data" in c["id"] for c in g["concepts"]))

    def test_story_concept_falls_back_to_intent_ref_when_no_title(self):
        from openfde import prompt_story
        ep = self._intent_ep(title="", signal="product", sequence=1, tag="P1", status="landed",
                             storyFacts={"operational": False, "concepts": ["Update openfde_work"]})
        g = prompt_story.build_prompt_graph([ep])
        titles = [c["title"] for c in g["concepts"]]
        self.assertIn("read the data → drop nan values → train a classifier", titles)
        self.assertNotIn("Update openfde_work", titles)


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


class SketchDemoFixtureTest(unittest.TestCase):
    """The deterministic Sketch-First verification fixture: a connected 3-step intent graph that drives
    the v3 ✓ BUILT / file-level BECAME / highlight surfaces. Side-effect-free — pure canvas state, no
    file write (so the empty-canvas demo loads instantly and never triggers a scan)."""

    def test_demo_state_shape(self):
        from openfde import sketch_demo
        st = sketch_demo.sketch_first_demo_state()
        intent = [b for b in st["boxes"] if b.get("kind") == "intent"]
        self.assertEqual([b["title"] for b in intent],
                         ["read the data", "drop nan values", "train a classifier"])
        for b in intent:                                   # each step is "built" (has impl files)
            self.assertEqual(b["implementationFiles"], [sketch_demo.DEMO_FILE])
        self.assertEqual(len(st["arrows"]), 2)             # connected pipeline read→clean→train
        mod = [b for b in st["boxes"] if b.get("kind") != "intent"]
        self.assertEqual(mod[0]["linkedFiles"], [sketch_demo.DEMO_FILE])   # a real node to amber

    def test_fixture_is_side_effect_free(self):
        # The fixture is pure data: no file I/O entrypoint, so loading it can never write to the repo
        # or trigger an assimilation pass — that is what keeps the empty-canvas demo sub-second.
        from openfde import sketch_demo
        self.assertFalse(hasattr(sketch_demo, "write_demo"))
        st1 = sketch_demo.sketch_first_demo_state()
        st2 = sketch_demo.sketch_first_demo_state()
        self.assertEqual(st1, st2)                          # deterministic


class SaasDemoFixtureTest(unittest.TestCase):
    """The SaaS example seed ("AI support inbox"): five connected PLANNED intent steps meant to be
    RUN — not a static showcase. Pure canvas state; the steps compile into an ordered intent graph
    so the EXISTING run machinery grounds them in place (proving the full loop on a realistic case)."""

    _TITLES = ["ingest customer messages", "classify issue", "draft response",
               "review approval", "log resolution"]

    def test_demo_state_shape(self):
        from openfde import saas_demo
        st = saas_demo.support_inbox_demo_state()
        intent = [b for b in st["boxes"] if b.get("kind") == "intent"]
        self.assertEqual([b["title"] for b in intent], self._TITLES)
        # PLANNED on purpose: NO implementationFiles up front — a Run grounds them. This is what
        # makes the example exercise the real intent→architecture loop, not fake a built state.
        for b in intent:
            self.assertNotIn("implementationFiles", b)
            self.assertTrue(b.get("prompt"))                # each step carries a plain-English prompt
        self.assertEqual(len(st["arrows"]), 4)              # ingest→classify→draft→review→log

    def test_compiles_into_ordered_intent_graph(self):
        from openfde import saas_demo
        st = saas_demo.support_inbox_demo_state()
        g = compile_intent_graph(st["boxes"], st["arrows"])
        self.assertTrue(g["present"])
        self.assertEqual([s["title"] for s in g["steps"]], self._TITLES)   # flow order preserved
        self.assertIn("ingest customer messages", g["summary"])
        self.assertIn("log resolution", g["summary"])

    def test_fixture_is_deterministic_and_side_effect_free(self):
        from openfde import saas_demo
        self.assertFalse(hasattr(saas_demo, "write_demo"))   # no file-I/O entrypoint
        self.assertEqual(saas_demo.support_inbox_demo_state(),
                         saas_demo.support_inbox_demo_state())


if __name__ == "__main__":
    unittest.main()
