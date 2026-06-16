"""
JS/TS L1-C (shipped, regex / dependency-free) + L1-D-A tests.

L1-C: symbol detection (object methods + config/test noise control), cross-file
connected-implementation resolution, Playwright test detection + failure parsing, and
the JS/TS failure FLOW (the lens's connected-implementation path). The Python
failure-flow path must stay byte-for-byte unchanged.

L1-D-A: HTML / web-app entrypoint mapping — edges from HTML pages to the JS/TS
modules they load/import (architecture only; conservative — only real repo files).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openfde import architect, failure_flow
from openfde.language_packs import JsTsPack
from openfde.language_packs import js_ts_treesitter as _ts_adapter


def _repo(files: dict, pkg=None):
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "package.json").write_text(json.dumps(pkg or {"name": "demo"}))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d, root


_MATH = "export function add(a: number, b: number): number {\n  return a - b\n}\n"
_TEST = ("import { add } from './math'\n"
         "import { describe, it, expect } from 'vitest'\n"
         "\n"
         "describe('add', () => {\n"
         "  it('adds two numbers', () => {\n"
         "    const r = add(2, 3)\n"
         "    expect(r).toBe(5)\n"
         "  })\n"
         "})\n")


# ── 1. symbol detection: object methods + noise control ───────────────────────

class SymbolTest(unittest.TestCase):
    def test_object_methods_extracted(self):
        src = ("export function add(a, b) { return a + b }\n"
               "export const api = {\n"
               "  total(xs) { return xs.reduce((a, b) => add(a, b), 0) },\n"
               "  label: (n) => String(n),\n"
               "  async fetchOne(id) { return id },\n"
               "}\n")
        d, root = _repo({"src/api.js": src})
        with d:
            names = {f["name"] for f in architect.analyze_repo(root)["functions"]}
            self.assertIn("add", names)
            self.assertIn("api.total", names)     # shorthand object method
            self.assertIn("api.label", names)     # arrow property
            self.assertIn("api.fetchOne", names)  # async shorthand method

    def test_config_and_dts_files_are_not_mined(self):
        d, root = _repo({
            "src/app.ts": "export function run() { return 1 }\n",
            "vite.config.ts": "export default { build() { return 1 }, plugins: [] }\n",
            "src/types.d.ts": "export function ghost(): void\n",
        })
        with d:
            names = {f["name"] for f in architect.analyze_repo(root)["functions"]}
            self.assertIn("run", names)
            self.assertNotIn("build", names)      # config object method → not app code
            self.assertNotIn("ghost", names)      # .d.ts declaration → not code

    def test_test_and_noise_file_predicates(self):
        self.assertTrue(architect.is_js_test_file("src/math.test.ts"))
        self.assertTrue(architect.is_js_test_file("src/__tests__/a.ts"))
        self.assertTrue(architect.is_js_test_file("e2e/home.spec.tsx"))
        self.assertFalse(architect.is_js_test_file("src/math.ts"))
        self.assertTrue(architect.is_js_noise_file("vite.config.ts"))
        self.assertTrue(architect.is_js_noise_file("src/env.d.ts"))
        self.assertTrue(architect.is_js_noise_file("playwright.config.ts"))
        self.assertFalse(architect.is_js_noise_file("src/app.tsx"))


# ── 2. connected-implementation resolution (js_call_context) ──────────────────

class CallContextTest(unittest.TestCase):
    def test_resolves_imported_implementation_from_failing_line(self):
        d, root = _repo({"src/math.ts": _MATH, "src/math.test.ts": _TEST})
        with d:
            ctx = architect.js_call_context(root, "src/math.test.ts", 7)   # the expect line
            calls = ctx["calls"]
            self.assertTrue(calls, "expected the test's call to resolve to add()")
            self.assertEqual(calls[0]["name"], "add")
            self.assertTrue(calls[0]["file"].endswith("math.ts"))
            self.assertEqual(calls[0]["confidence"], "medium")   # resolved relative import

    def test_no_calls_when_nothing_resolvable(self):
        d, root = _repo({"src/a.ts": "export function f() {\n  return globalThing()\n}\n"})
        with d:
            ctx = architect.js_call_context(root, "src/a.ts", 2)
            self.assertEqual(ctx["function"], "f")     # enclosing fn still found
            self.assertEqual(ctx["calls"], [])         # unresolved global → no edge


# ── 3. Playwright test detection + failure parsing ────────────────────────────

_PW_OUT = (
    "Running 1 test using 1 worker\n"
    "\n"
    "  1) [chromium] › e2e/home.spec.ts:2:1 › has title ============================\n"
    "\n"
    "    Error: expect(page).toHaveTitle(expected)\n"
    "      at e2e/home.spec.ts:3:18\n"
    "\n"
    "  1 failed\n"
)


class PlaywrightTest(unittest.TestCase):
    def _pw_repo(self):
        return _repo(
            {"e2e/home.spec.ts": "import { test, expect } from '@playwright/test'\n"
                                 "test('has title', async ({ page }) => {\n"
                                 "  await expect(page).toHaveTitle('x')\n})\n"},
            pkg={"name": "demo", "scripts": {"test:e2e": "playwright test"},
                 "devDependencies": {"@playwright/test": "^1.40.0"}})

    def test_framework_and_check_detected(self):
        d, root = self._pw_repo()
        with d:
            self.assertEqual(JsTsPack().repro_context(root)["framework"], "playwright")
            self.assertEqual(JsTsPack().discover_checks(root)[0].command,
                             ["npm", "run", "test:e2e"])

    def test_failure_maps_to_file_line_and_test(self):
        d, root = self._pw_repo()
        with d:
            locs = [f.as_dict() for f in JsTsPack().parse_failures(_PW_OUT, root)]
            self.assertEqual(len(locs), 1)
            self.assertEqual(locs[0]["file"], "e2e/home.spec.ts")
            self.assertEqual(locs[0]["line"], 3)        # the in-repo `at` frame (assertion)
            self.assertEqual(locs[0]["test"], "has title")   # padding `=` stripped

    def test_unknown_output_still_degrades_to_nothing(self):
        d, root = self._pw_repo()
        with d:
            self.assertEqual(JsTsPack().parse_failures("just some logs\n", root), [])


# ── 4. JS/TS failure FLOW (the lens path) ─────────────────────────────────────

class FailureFlowTest(unittest.TestCase):
    def test_assertion_failure_connects_test_to_implementation(self):
        # Vitest assertion failure: the product fn isn't in the stack, so the
        # resolved import call is what lights up as the failure terminus.
        d, root = _repo({"src/math.ts": _MATH, "src/math.test.ts": _TEST})
        with d:
            out = (" FAIL  src/math.test.ts > add > adds two numbers\n"
                   "AssertionError: expected -1 to be 5\n"
                   " ❯ src/math.test.ts:7:15\n")
            flow = failure_flow.build_failure_flow(
                root, file="src/math.test.ts", line=7, test="adds two numbers", output_tail=out)
            pp = flow["primaryPath"]
            self.assertGreaterEqual(len(pp), 2)
            self.assertEqual(pp[0]["role"], "source")
            fail = pp[-1]
            self.assertEqual(fail["role"], "failure")
            self.assertEqual(fail["function"], "add")
            self.assertTrue(fail["file"].endswith("math.ts"))     # connected implementation
            self.assertTrue(any(n.get("fail") and n.get("file", "").endswith("math.ts")
                                for n in flow["nodes"]))
            self.assertIn("add", flow["summary"])

    def test_thrown_error_uses_stack_chain_without_duplicate_impl(self):
        # A thrown error already reaches the implementation in the stack; the flow
        # uses the chain and does not add a second `add` node.
        d, root = _repo({"src/math.ts": _MATH, "src/math.test.ts": _TEST})
        with d:
            out = ("TypeError: boom\n"
                   "    at add (src/math.ts:2:3)\n"
                   "    at src/math.test.ts:6:15\n")
            flow = failure_flow.build_failure_flow(
                root, file="src/math.test.ts", line=6, test="adds two numbers", output_tail=out)
            add_nodes = [n for n in flow["nodes"] if n["label"] == "add"]
            self.assertEqual(len(add_nodes), 1, "no duplicate implementation node")
            self.assertEqual(flow["primaryPath"][-1]["function"], "add")
            self.assertEqual(flow["primaryPath"][-1]["role"], "failure")

    def test_no_output_still_yields_the_failing_node(self):
        d, root = _repo({"src/math.ts": _MATH, "src/math.test.ts": _TEST})
        with d:
            flow = failure_flow.build_failure_flow(
                root, file="src/math.test.ts", line=7, test="adds two numbers", output_tail="")
            self.assertTrue(flow["nodes"])
            self.assertTrue(flow["primaryPath"])
            # even with no stack, the resolved import gives the implementation node
            self.assertTrue(any(n.get("file", "").endswith("math.ts") for n in flow["nodes"]))

    def test_python_flow_path_unchanged(self):
        # A .py failing file must still go through the AST path (regression guard).
        d, root = _repo({"pkg/calc.py": "def add(a, b):\n    return a - b\n",
                         "tests/test_calc.py": "from pkg.calc import add\n"
                                               "def test_add():\n    assert add(1, 2) == 3\n"})
        with d:
            out = ("tests/test_calc.py:3: in test_add\n"
                   "    assert add(1, 2) == 3\n"
                   "E   AssertionError\n")
            flow = failure_flow.build_failure_flow(
                root, file="tests/test_calc.py", line=3, func="test_add", output_tail=out)
            self.assertTrue(flow["nodes"])
            self.assertTrue(any(n["label"] == "test_add" for n in flow["nodes"]))


# ── 5. HTML / web-app entrypoint mapping (L1-D-A) ─────────────────────────────

class HtmlEntryTest(unittest.TestCase):
    def _entry_edges(self, g):
        return [e for e in g["fileEdges"] if e.get("type") == "entry"]

    def test_external_module_and_classic_src_link_to_js(self):
        d, root = _repo({
            "js/app.js": "export function start() { return 1 }\n",
            "index.html": ('<!doctype html><html><head>\n'
                           '<script type="module" src="./js/app.js"></script>\n'
                           '</head><body></body></html>\n'),
        })
        with d:
            g = architect.analyze_repo(root)
            edges = self._entry_edges(g)
            self.assertTrue(any(e["fromFile"] == "index.html" and e["toFile"] == "js/app.js"
                                and e["label"] == "loads" and e["confidence"] == "high"
                                for e in edges))
            # the page is a module box, with a module-level entry arrow to js
            self.assertIn("index.html", {m["name"] for m in g["modules"]})
            self.assertTrue(any(e.get("type") == "entry"
                                and e["from"] == "module:index.html" for e in g["edges"]))

    def test_inline_module_import_links_to_js(self):
        d, root = _repo({
            "js/render/scene.js": "export function draw() { return 2 }\n",
            "samples/immersive-ar.html": (
                '<!doctype html><html><body>\n'
                '<script type="module">\n'
                "  import { draw } from '../js/render/scene.js'\n"
                "  draw()\n"
                '</script>\n</body></html>\n'),
        })
        with d:
            edges = self._entry_edges(architect.analyze_repo(root))
            self.assertTrue(any(e["fromFile"] == "samples/immersive-ar.html"
                                and e["toFile"] == "js/render/scene.js"
                                and e["label"] == "imports" for e in edges))

    def test_external_vendor_and_bare_refs_are_ignored(self):
        # An edge is drawn ONLY to a real repo file — never a CDN URL, a bare npm
        # specifier, or a path that doesn't exist (missing is okay; wrong is not).
        d, root = _repo({
            "js/app.js": "export function start() { return 1 }\n",
            "index.html": (
                '<!doctype html><html><head>\n'
                '<script type="module" src="./js/app.js"></script>\n'
                '<script src="https://cdn.example.com/three.min.js"></script>\n'
                '<script src="js/missing.js"></script>\n'
                '<script type="module">import * as THREE from "three"</script>\n'
                '</head></html>\n'),
        })
        with d:
            edges = self._entry_edges(architect.analyze_repo(root))
            tos = {e["toFile"] for e in edges}
            self.assertEqual(tos, {"js/app.js"})         # only the real file
            self.assertFalse(any("three" in t or "cdn" in t or "missing" in t for t in tos))

    def test_no_html_means_no_entry_edges(self):
        d, root = _repo({"src/a.ts": "export function f() { return 1 }\n"})
        with d:
            self.assertEqual(self._entry_edges(architect.analyze_repo(root)), [])


# ── 6. L1-D tree-sitter adapter (optional, behind the regex fallback) ─────────

@unittest.skipUnless(_ts_adapter.available(), "tree-sitter grammars not installed")
class TreeSitterAdapterTest(unittest.TestCase):
    """L1-D: when tree-sitter is installed, analyze_repo extracts JS/TS symbols + imports from a real
    AST; output shape is unchanged and the warning names the parser path. The regex path remains the
    fallback (covered by the rest of this file, which now runs with tree-sitter active)."""

    def _analyze(self, files):
        d, root = _repo(files)
        with d:
            g = architect.analyze_repo(root)
        return {f["name"] for f in g["functions"]}, g

    def test_extracts_core_symbol_forms(self):
        src = ("export function decl() { return 1 }\n"
               "const arrowConst = (x) => x * 2\n"
               "export const expr = function () { return 3 }\n"
               "export default function main() {}\n"
               "class Box {\n  constructor() {}\n  draw() { return 1 }\n  onClick = () => 2\n}\n"
               "export const store = { load() { return 1 }, save: () => 2 }\n")
        names, g = self._analyze({"src/app.ts": src})
        for want in ("decl", "arrowConst", "expr", "main",            # decl / arrow / func-expr / default
                     "Box", "Box.draw", "Box.onClick",                # class + method + field-arrow
                     "store.load", "store.save"):                     # named-object methods
            self.assertIn(want, names)
        self.assertNotIn("Box.constructor", names)                    # constructor skipped (parity)
        self.assertTrue(any("tree-sitter" in w for w in g["warnings"]))

    def test_react_component_tsx(self):
        # a React component is a function / arrow returning JSX — extracted via the tsx grammar
        src = ("export function Hello({ name }) { return <div>{name}</div> }\n"
               "export const Card = ({ x }) => <section>{x}</section>\n")
        names, _ = self._analyze({"src/Hello.tsx": src})
        self.assertIn("Hello", names)
        self.assertIn("Card", names)

    def test_import_specifier_feeds_edge_resolution(self):
        names, g = self._analyze({
            "lib/util.ts": "export function help() { return 1 }\n",
            "src/app.ts": ("import { help } from '../lib/util'\n"
                           "export async function go() { await import('../lib/util'); return help() }\n"),
        })
        self.assertTrue({"go", "help"} <= names)
        # the tree-sitter-extracted specifier ('../lib/util') resolved to a real module edge
        # (import → upgraded to dataflow once the help() call resolves cross-module)
        self.assertIn(("module:src", "module:lib"), {(e["from"], e["to"]) for e in g["edges"]})

    def test_shape_is_unchanged(self):
        _, g = self._analyze({"src/app.ts": "export function f() { return 1 }\n"})
        for key in ("modules", "files", "functions", "edges", "flows", "fileEdges", "warnings"):
            self.assertIn(key, g)

    def test_html_entrypoint_mapping_still_works_with_treesitter(self):
        # L1-D-A must remain intact while tree-sitter drives symbol/import extraction.
        d, root = _repo({
            "js/app.js": "export function start() { return 1 }\n",
            "index.html": ('<!doctype html><html><head>\n'
                           '<script type="module" src="./js/app.js"></script>\n'
                           '</head></html>\n'),
        })
        with d:
            g = architect.analyze_repo(root)
        self.assertTrue(any(e.get("type") == "entry" and e["toFile"] == "js/app.js"
                            for e in g["fileEdges"]))

    def test_warning_names_regex_when_adapter_unavailable(self):
        # Force the adapter unavailable → regex path + an honest warning; object methods still found.
        with mock.patch.object(_ts_adapter, "available", return_value=False):
            names, g = self._analyze({"src/app.js": ("export function add(a, b) { return a + b }\n"
                                                     "export const api = { total() { return 1 } }\n")})
        self.assertEqual({"add", "api.total"}, names)
        self.assertTrue(any("regex fallback" in w for w in g["warnings"]))

    def test_parse_failure_falls_back_to_regex(self):
        # Adapter returns None per file (simulated parse failure) → regex path used, no crash.
        with mock.patch.object(_ts_adapter, "extract", return_value=None):
            names, g = self._analyze({"src/ok.ts": "export function ok() { return 1 }\n"})
        self.assertIn("ok", names)                       # the forgiving regex path caught it
        self.assertTrue(any("regex fallback" in w for w in g["warnings"]))


class TreeSitterLazyImportTest(unittest.TestCase):
    """tree-sitter is OPTIONAL and lazy: importing the JS/TS pack, the architect, or the adapter
    module must not import tree-sitter — it loads only when a JS/TS repo is actually analyzed."""

    def test_imports_do_not_eagerly_load_tree_sitter(self):
        import subprocess
        import sys as _sys
        for mod in ("openfde.language_packs.js_ts_pack", "openfde.architect",
                    "openfde.language_packs.js_ts_treesitter"):
            out = subprocess.run(
                [_sys.executable, "-c",
                 f"import sys, {mod}; print('tree_sitter' in sys.modules)"],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(out.stdout.strip(), "False",
                             f"{mod} eagerly imported tree_sitter:\n{out.stderr}")


if __name__ == "__main__":
    unittest.main()
