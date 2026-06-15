"""
Tests for the LanguagePack slice (openfde.language_packs). The law under test:
extracting the Python seams behind a pack changes NOTHING — the pack must produce
the same checks and the same failure shape as calling verify directly, and the
registry must detect Python where Python files exist.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from openfde import verify
from openfde.language_packs import (
    FailureLocation,
    JsTsPack,
    PythonPack,
    VerifyCheckSpec,
    get_language_packs,
    get_pack_for_file,
)

_PYTEST_TB = (
    "=================================== FAILURES ===================================\n"
    "_________________________________ test_thing __________________________________\n"
    "tests/test_thing.py:4: in test_thing\n"
    "    assert add(1, 2) == 4\n"
    "E   AssertionError\n"
)


def _py_repo():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "pkg").mkdir()
    (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_thing.py").write_text("def test_thing():\n    assert True\n")
    return d, root


class RegistryTest(unittest.TestCase):
    def test_detects_python_pack_when_py_files_exist(self):
        d, root = _py_repo()
        with d:
            packs = get_language_packs(root)
            self.assertEqual([p.name for p in packs], ["python"])
            self.assertTrue(PythonPack().detects(root))

    def test_no_pack_for_empty_repo(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "README.md").write_text("# hi\n")
            self.assertEqual(get_language_packs(d), [])

    def test_get_pack_for_file_by_extension(self):
        self.assertEqual(get_pack_for_file("a/b/model.py").name, "python")
        # JS/TS files now resolve to the js_ts pack (Pack #2).
        for f in ("a/b/app.ts", "a/b/App.tsx", "a/b/util.js", "a/b/Box.jsx"):
            self.assertEqual(get_pack_for_file(f).name, "js_ts", f)
        self.assertIsNone(get_pack_for_file("Cargo.toml"))     # no Rust pack yet
        self.assertIsNone(get_pack_for_file("go/main.go"))


class PythonPackParityTest(unittest.TestCase):
    """The pack output, normalized back to dicts, must equal the raw verify output."""

    def test_discover_checks_matches_raw(self):
        d, root = _py_repo()
        with d:
            raw = verify.discover_checks(root)
            via_pack = [s.as_dict() for s in PythonPack().discover_checks(root)]
            self.assertEqual(via_pack, raw)

    def test_parse_failures_matches_raw_and_shape(self):
        d, root = _py_repo()
        with d:
            raw = verify.parse_failure_locations(_PYTEST_TB, root)
            via_pack = [f.as_dict() for f in PythonPack().parse_failures(_PYTEST_TB, root)]
            self.assertEqual(via_pack, raw)
            # the existing OpenFDE failure shape: {test, file, line, func}
            self.assertTrue(raw and set(raw[0]) >= {"file", "line", "func", "test"})

    def test_ensure_check_config_pins_pytest(self):
        d, root = _py_repo()
        with d:
            PythonPack().ensure_check_config(root)
            cfg = root / ".openfde" / "verify.json"
            self.assertTrue(cfg.exists())
            self.assertIn("pytest", cfg.read_text())
            # idempotent: a second call must not overwrite
            before = cfg.read_text()
            PythonPack().ensure_check_config(root)
            self.assertEqual(cfg.read_text(), before)

    def test_repro_context_is_pytest(self):
        ctx = PythonPack().repro_context()
        self.assertEqual(ctx["framework"], "pytest")
        self.assertIn("pytest", " ".join(ctx["test_command"]))


class DataclassRoundTripTest(unittest.TestCase):
    def test_failure_location_round_trip_omits_empty_message(self):
        d = {"test": "t", "file": "m.py", "line": 7, "func": "f"}
        self.assertEqual(FailureLocation.from_dict(d).as_dict(), d)        # no message key
        with_msg = FailureLocation.from_dict({**d, "message": "boom"}).as_dict()
        self.assertEqual(with_msg["message"], "boom")

    def test_check_spec_round_trip_excludes_reporter(self):
        d = {"id": "unit-tests", "label": "Unit tests", "command": ["pytest"],
             "cwd": "", "required": True}
        spec = VerifyCheckSpec.from_dict(d)
        self.assertEqual(spec.reporter, "text")          # default groundwork
        self.assertEqual(spec.as_dict(), d)              # reporter NOT serialized


# ── JS/TS pack (L1-A) — real Vitest / Jest output captured from runs ──────────

_VITEST_OUTPUT = (
    " ❯ src/math.test.ts (1 test | 1 failed) 12ms\n"
    "   × add > adds two numbers 5ms\n"
    "\n"
    "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯\n"
    "\n"
    " FAIL  src/math.test.ts > add > adds two numbers\n"
    "AssertionError: expected 5 to be 4 // Object.is equality\n"
    " ❯ src/math.test.ts:8:19\n"
    "      6|   it('adds two numbers', () => {\n"
    "      7|     const r = add(2, 3)\n"
    "      8|     expect(r).toBe(4)\n"
    "       |                   ^\n"
    "\n"
    " Test Files  1 failed (1)\n"
    "      Tests  1 failed (1)\n"
)

_JEST_OUTPUT = (
    " FAIL  src/math.test.js\n"
    "  add\n"
    "    ✕ adds two numbers (3 ms)\n"
    "\n"
    "  ● add › adds two numbers\n"
    "\n"
    "    expect(received).toBe(expected) // Object.is equality\n"
    "\n"
    "    Expected: 4\n"
    "    Received: 5\n"
    "\n"
    "    >  8 |     expect(r).toBe(4);\n"
    "         |               ^\n"
    "\n"
    "      at Object.toBe (src/math.test.js:8:15)\n"
    "      at processTicksAndRejections (node_modules/internal/task_queues.js:95:5)\n"
    "\n"
    "Test Suites: 1 failed, 1 total\n"
    "Tests:       1 failed, 1 total\n"
)

# A real but OUT-OF-SCOPE format (mocha): no Vitest FAIL-chain, no Jest ● bullet.
_MOCHA_OUTPUT = (
    "  1) MyThing renders correctly:\n"
    "     Error: expected true to be false\n"
    "      at Context.<anonymous> (test/foo.spec.js:12:20)\n"
)

# A Jest failure whose only stack frame is inside node_modules — no in-repo site.
_VENDOR_ONLY_OUTPUT = (
    " FAIL  src/widget.test.js\n"
    "  ● Widget › throws on bad input\n"
    "\n"
    "    TypeError: Cannot read properties of undefined\n"
    "\n"
    "      at validate (node_modules/some-lib/dist/index.js:42:11)\n"
    "\n"
    "Tests:       1 failed, 1 total\n"
)


def _node_repo(scripts=None, lock=None, files=None):
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    pkg = {"name": "demo", "version": "1.0.0"}
    if scripts is not None:
        pkg["scripts"] = scripts
    (root / "package.json").write_text(json.dumps(pkg))
    if lock:
        (root / lock).write_text("")
    for rel, content in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d, root


class JsTsRegistryTest(unittest.TestCase):
    def test_detects_node_repo_by_package_json(self):
        d, root = _node_repo(scripts={"test": "vitest"})
        with d:
            self.assertTrue(JsTsPack().detects(root))
            self.assertEqual([p.name for p in get_language_packs(root)], ["js_ts"])

    def test_detects_by_source_file_without_package_json(self):
        with tempfile.TemporaryDirectory() as dd:
            root = Path(dd)
            (root / "src").mkdir()
            (root / "src" / "app.tsx").write_text("export const x = 1\n")
            self.assertTrue(JsTsPack().detects(root))

    def test_vendor_dirs_do_not_trigger_detection(self):
        with tempfile.TemporaryDirectory() as dd:
            root = Path(dd)
            (root / "node_modules" / "left-pad").mkdir(parents=True)
            (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
            (root / "README.md").write_text("# hi\n")
            self.assertFalse(JsTsPack().detects(root))      # only vendor JS → no
            self.assertEqual(get_language_packs(root), [])

    def test_polyglot_repo_returns_both_packs_python_first(self):
        d, root = _node_repo(scripts={"test": "vitest"})
        with d:
            (root / "service.py").write_text("def f():\n    return 1\n")
            # Python stays first so packs[0] owns failure parsing (no regression).
            self.assertEqual([p.name for p in get_language_packs(root)],
                             ["python", "js_ts"])


class JsTsDiscoveryTest(unittest.TestCase):
    def _cmd(self, scripts, lock=None):
        d, root = _node_repo(scripts=scripts, lock=lock)
        with d:
            specs = JsTsPack().discover_checks(root)
            return specs[0].command if specs else None, (specs[0].id if specs else None)

    def test_npm_vitest_forces_single_run(self):
        cmd, cid = self._cmd({"test": "vitest"})
        self.assertEqual(cmd, ["npm", "run", "test", "--", "--run"])
        self.assertEqual(cid, "js-tests")

    def test_pnpm_jest_runs_script_as_is(self):
        cmd, _ = self._cmd({"test": "jest"}, lock="pnpm-lock.yaml")
        self.assertEqual(cmd, ["pnpm", "run", "test"])          # jest needs no flag

    def test_test_unit_priority_and_yarn_and_no_double_run_flag(self):
        # test:unit beats test; "vitest run" already pins run-mode → no extra flag.
        cmd, _ = self._cmd({"test": "vitest", "test:unit": "vitest run --coverage"},
                           lock="yarn.lock")
        self.assertEqual(cmd, ["yarn", "run", "test:unit"])

    def test_bun_vitest_uses_bare_run_flag(self):
        cmd, _ = self._cmd({"test": "vitest"}, lock="bun.lockb")
        self.assertEqual(cmd, ["bun", "run", "test", "--run"])

    def test_no_test_script_yields_no_check(self):
        d, root = _node_repo(scripts={"build": "tsc"})
        with d:
            self.assertEqual(JsTsPack().discover_checks(root), [])

    def test_explicit_verify_json_config_wins(self):
        d, root = _node_repo(scripts={"test": "vitest"})
        with d:
            (root / ".openfde").mkdir()
            (root / ".openfde" / "verify.json").write_text(json.dumps(
                [{"id": "custom", "label": "Custom", "command": ["echo", "hi"]}]))
            specs = JsTsPack().discover_checks(root)
            self.assertEqual([s.id for s in specs], ["custom"])
            self.assertEqual(specs[0].command, ["echo", "hi"])


class JsTsReproContextTest(unittest.TestCase):
    def test_context_infers_framework_language_and_command(self):
        d, root = _node_repo(scripts={"test": "vitest"})
        with d:
            (root / "tsconfig.json").write_text("{}")
            ctx = JsTsPack().repro_context(root)
            self.assertEqual(ctx["framework"], "vitest")
            self.assertEqual(ctx["language"], "typescript")
            self.assertEqual(ctx["test_command"], ["npm", "run", "test", "--", "--run"])
            self.assertIn("*.test.ts", ctx["test_conventions"])

    def test_context_has_sane_default_without_root(self):
        ctx = JsTsPack().repro_context()
        self.assertEqual(ctx["language"], "javascript")
        self.assertTrue(ctx["framework"])
        self.assertNotIn("pytest", " ".join(ctx["test_command"]))   # never Python
        self.assertIn("*.spec.ts", ctx["test_conventions"])

    def test_ensure_check_config_pins_js_check(self):
        d, root = _node_repo(scripts={"test": "vitest"}, lock="pnpm-lock.yaml")
        with d:
            JsTsPack().ensure_check_config(root)
            cfg = root / ".openfde" / "verify.json"
            self.assertTrue(cfg.exists())
            data = json.loads(cfg.read_text())
            self.assertEqual(data[0]["command"], ["pnpm", "run", "test", "--", "--run"])
            before = cfg.read_text()
            JsTsPack().ensure_check_config(root)            # idempotent
            self.assertEqual(cfg.read_text(), before)


class JsTsFailureParsingTest(unittest.TestCase):
    def _parse(self, output, root=None):
        with tempfile.TemporaryDirectory() as dd:
            return [f.as_dict() for f in JsTsPack().parse_failures(output, root or dd)]

    def test_vitest_failure_file_line_test(self):
        out = self._parse(_VITEST_OUTPUT)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file"], "src/math.test.ts")
        self.assertEqual(out[0]["line"], 8)
        self.assertEqual(out[0]["test"], "adds two numbers")
        self.assertTrue(set(out[0]) >= {"file", "line", "func", "test"})

    def test_jest_failure_file_line_test_and_func(self):
        out = self._parse(_JEST_OUTPUT)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file"], "src/math.test.js")
        self.assertEqual(out[0]["line"], 8)
        self.assertEqual(out[0]["test"], "adds two numbers")
        self.assertEqual(out[0]["func"], "Object.toBe")

    def test_unknown_format_degrades_to_no_locations(self):
        self.assertEqual(self._parse(_MOCHA_OUTPUT), [])

    def test_vendor_only_stack_yields_no_location(self):
        # honest: the only frame is in node_modules → no in-repo site → nothing
        self.assertEqual(self._parse(_VENDOR_ONLY_OUTPUT), [])

    def test_empty_output_is_empty(self):
        self.assertEqual(self._parse(""), [])


# ── JS/TS architecture assimilation (L1-B) — a real fixture repo ──────────────

# Exercises every required form: function declaration, exported function, an arrow
# with a TS return-type annotation, a class, a class method, a class-field arrow, and
# a default-exported function. `Calculator.add` calls the module-level `add` (a
# same-file flow that must stay distinct from the method of the same name).
_MATH_TS = """export function add(a: number, b: number): number {
  return a + b
}

export const multiply = (a: number, b: number): number => {
  return a * b
}

export class Calculator {
  private total: number = 0
  add(n: number): number {
    this.total = add(this.total, n)
    return this.total
  }
  reset = (): void => {
    this.total = 0
  }
}

export default function makeCalculator(): Calculator {
  return new Calculator()
}
"""

# Imports from ./math three ways (named, default) and calls them across the file
# boundary — the cross-file flows L1-B must resolve.
_SERVICE_TS = """import { add, multiply } from './math'
import makeCalc from './math'

export function compute(x: number): number {
  const c = makeCalc()
  return add(multiply(x, 2), 10)
}

export const summarize = (items: number[]): string => {
  const total = items.reduce((acc, n) => add(acc, n), 0)
  return `total=${total}`
}
"""

# A React component (default-exported function returning JSX) in a .tsx file.
_BUTTON_TSX = """export default function Button(props: { label: string }) {
  return <button>{props.label}</button>
}
"""


def _ts_fixture_repo():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "src").mkdir()
    (root / "package.json").write_text(json.dumps(
        {"name": "demo", "version": "1.0.0", "scripts": {"test": "vitest"}}))
    (root / "src" / "math.ts").write_text(_MATH_TS)
    (root / "src" / "service.ts").write_text(_SERVICE_TS)
    (root / "src" / "Button.tsx").write_text(_BUTTON_TSX)
    return d, root


class JsTsArchGraphTest(unittest.TestCase):
    """L1-B: build_arch_graph routes to the repo's JS/TS assimilation and returns a
    real, non-empty ArchGraph (the canvas's source of truth)."""

    def _graph(self, root):
        return JsTsPack().build_arch_graph(root)

    def test_pack_detects_repo(self):
        d, root = _ts_fixture_repo()
        with d:
            self.assertTrue(JsTsPack().detects(root))
            self.assertEqual([p.name for p in get_language_packs(root)], ["js_ts"])

    def test_graph_is_real_archgraph_shape_not_empty(self):
        d, root = _ts_fixture_repo()
        with d:
            g = self._graph(root)
            # the architect ArchGraph shape (canvas source of truth), not the
            # L1-A empty stub
            self.assertGreaterEqual(
                set(g), {"modules", "files", "functions", "edges", "flows", "warnings"})
            self.assertTrue(g["files"], "expected JS/TS files")
            self.assertTrue(g["functions"], "expected extracted functions")
            self.assertTrue(g["flows"], "expected function-level flows")
            # NOT the honest-empty L1-A shape anymore
            self.assertNotEqual(g.get("nodes", "absent"), [])

    def test_functions_cover_required_forms(self):
        d, root = _ts_fixture_repo()
        with d:
            names = {f["name"] for f in self._graph(root)["functions"]}
            self.assertIn("add", names)              # function declaration (exported)
            self.assertIn("multiply", names)         # arrow with TS return annotation
            self.assertIn("compute", names)          # exported function
            self.assertIn("summarize", names)        # exported arrow
            self.assertIn("makeCalculator", names)   # export default function
            self.assertIn("Button", names)           # React component (default fn, .tsx)
            self.assertIn("Calculator", names)       # class declaration
            self.assertIn("Calculator.add", names)   # class method
            self.assertIn("Calculator.reset", names)  # class-field arrow

    def test_mjs_cts_extensions_are_assimilated(self):
        # .mjs / .cts were previously dropped (not in the language map) — they must
        # now be collected and parsed like any other JS/TS file.
        d, root = _ts_fixture_repo()
        with d:
            (root / "src" / "esm.mjs").write_text("export function helper() { return 1 }\n")
            (root / "src" / "node.cts").write_text("export const ping = (): number => 2\n")
            g = self._graph(root)
            langs = {f["language"] for f in g["files"]}
            self.assertIn("JavaScript", langs)
            self.assertIn("TypeScript", langs)
            names = {f["name"] for f in g["functions"]}
            self.assertIn("helper", names)
            self.assertIn("ping", names)

    def test_same_file_flow_is_high_confidence(self):
        d, root = _ts_fixture_repo()
        with d:
            flows = self._graph(root)["flows"]
            same = [fl for fl in flows if fl["fromFile"] == fl["toFile"]]
            self.assertTrue(same, "expected at least one same-file flow")
            self.assertTrue(all(fl["confidence"] == "high" for fl in same),
                            "same-file flows must be high-confidence")
            # Calculator.add() → the module-level add() (distinct nodes, same file)
            self.assertTrue(any(
                fl["fromFunctionId"].endswith("math.ts:Calculator.add")
                and fl["toFunctionId"].endswith("math.ts:add") for fl in same),
                "expected Calculator.add() → add() same-file flow")

    def test_cross_file_flow_present_and_medium(self):
        d, root = _ts_fixture_repo()
        with d:
            flows = self._graph(root)["flows"]
            cross = [fl for fl in flows if fl["fromFile"] != fl["toFile"]]
            self.assertTrue(cross, "expected cross-file flows when imports resolve")
            self.assertTrue(all(fl["confidence"] == "medium" for fl in cross),
                            "resolved-import flows must be medium (never high)")
            # service.ts compute() → math.ts add() through `import { add }`
            self.assertTrue(any(
                fl["fromFunctionId"].endswith("service.ts:compute")
                and fl["toFunctionId"].endswith("math.ts:add") for fl in cross),
                "expected compute() → add() across the import boundary")
            # default import `makeCalc` resolves to math's default export
            self.assertTrue(any(
                fl["toFunctionId"].endswith("math.ts:makeCalculator") for fl in cross),
                "expected the default import to resolve to makeCalculator()")

    def test_warnings_name_the_heuristic_boundary(self):
        d, root = _ts_fixture_repo()
        with d:
            warns = self._graph(root)["warnings"]
            self.assertTrue(any("tree-sitter" in w for w in warns),
                            "warnings must honestly name the regex/tree-sitter boundary")

    def test_unrelated_imports_make_no_cross_module_noise(self):
        # A bare-package import must NOT produce a cross-file flow (no fake edges).
        d, root = _ts_fixture_repo()
        with d:
            (root / "src" / "lonely.ts").write_text(
                "import { readFile } from 'fs'\n"
                "export function load(): void { readFile('x', () => {}) }\n")
            flows = self._graph(root)["flows"]
            # `readFile` is from a bare package → never resolves to an in-repo node
            self.assertFalse(any(fl["toFunctionId"].endswith(":readFile") for fl in flows))

    def test_one_line_class_method_call_flow(self):
        # Regression: the definition guard must skip only the def TOKEN, not every
        # call on its line. A ONE-LINE method body whose call shares the signature's
        # line — and names a module-level function with the SAME short name — must
        # still produce the same-file flow (it was dropped with the signature).
        src = ("export function add(a: number, b: number): number { return a + b }\n"
               "\n"
               "export class Calc {\n"
               "  add(n: number): number { return add(n, 1) }\n"
               "}\n")
        d, root = _node_repo(scripts={"test": "vitest"}, files={"src/calc.ts": src})
        with d:
            g = self._graph(root)
            names = {f["name"] for f in g["functions"]}
            self.assertIn("Calc.add", names)        # the one-line method is a node
            self.assertIn("add", names)
            flow = [fl for fl in g["flows"]
                    if fl["fromFunctionId"].endswith("calc.ts:Calc.add")
                    and fl["toFunctionId"].endswith("calc.ts:add")]
            self.assertTrue(flow, "Calc.add() → add() same-file flow must be detected")
            self.assertEqual(flow[0]["confidence"], "high")

    def test_single_param_arrow_without_parens(self):
        # `const f = x => …` (and exported / async) was missed by the v1 patterns.
        # The existing scrubber keeps comment/string decoys from being extracted.
        src = ("export const double = x => x * 2\n"
               "const triple = n => n * 3\n"
               "const wrap = async v => v\n"
               "// const fake = z => z\n"
               'const decoy = "a => b"\n')
        d, root = _node_repo(scripts={"test": "vitest"}, files={"src/arrows.ts": src})
        with d:
            names = {f["name"] for f in self._graph(root)["functions"]}
            self.assertIn("double", names)          # exported single-param arrow
            self.assertIn("triple", names)          # non-exported single-param arrow
            self.assertIn("wrap", names)            # async single-param arrow
            self.assertNotIn("fake", names)         # inside a comment → scrubbed out
            self.assertNotIn("decoy", names)        # value is a string, not an arrow


if __name__ == "__main__":
    unittest.main()
