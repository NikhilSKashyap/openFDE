"""
JS/TS L1-C tests — symbol detection (object methods + config/test noise control),
cross-file connected-implementation resolution, Playwright test detection + failure
parsing, and the JS/TS failure FLOW (the lens's connected-implementation path). The
Python failure-flow path must stay byte-for-byte unchanged.
"""
import json
import tempfile
import unittest
from pathlib import Path

from openfde import architect, failure_flow
from openfde.language_packs import JsTsPack


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


if __name__ == "__main__":
    unittest.main()
