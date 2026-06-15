"""
Tests for the internal plugin registry (openfde.plugins) — Plugin Registry v1-A.
The law: built-in capability providers are DESCRIBABLE as metadata, activation is
probed from cheap repo markers (the language packs' own detection), and nothing
heavy is imported or installed. The existing language-pack registry is untouched.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from openfde import plugins
from openfde.language_packs import all_language_packs, get_language_packs


def _repo(files: dict):
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d, root


class BuiltinProvidersTest(unittest.TestCase):
    def test_python_and_js_ts_are_listed_as_builtin_language_packs(self):
        by_id = {m["id"]: m for m in plugins.list_plugins()}
        self.assertIn("python", by_id)
        self.assertIn("js_ts", by_id)
        for pid in ("python", "js_ts"):
            self.assertEqual(by_id[pid]["kind"], "language_pack")
            self.assertEqual(by_id[pid]["status"], "builtin")
            self.assertTrue(by_id[pid]["provides"], "a provider should list capabilities")

    def test_manifest_shape_has_required_fields(self):
        required = {"id", "kind", "displayName", "status", "activatesOn", "provides", "active"}
        for m in plugins.list_plugins():
            self.assertGreaterEqual(set(m), required, f"missing fields in {m.get('id')}")

    def test_kinds_contract_covers_the_six_provider_kinds(self):
        for kind in ("language_pack", "domain_pack", "verify_adapter",
                     "agent_provider", "layout_engine", "integration"):
            self.assertIn(kind, plugins.PLUGIN_KINDS)


class ActivationTest(unittest.TestCase):
    def _active(self, root):
        return {m["id"]: m["active"] for m in plugins.list_plugins(root)}

    def test_package_json_activates_js_ts(self):
        d, root = _repo({"package.json": '{"name":"x"}', "src/a.ts": "export const x = 1\n"})
        with d:
            a = self._active(root)
            self.assertTrue(a["js_ts"])
            self.assertFalse(a["python"])

    def test_py_file_activates_python(self):
        d, root = _repo({"pkg/calc.py": "def add(a, b):\n    return a + b\n"})
        with d:
            a = self._active(root)
            self.assertTrue(a["python"])
            self.assertFalse(a["js_ts"])

    def test_polyglot_activates_both(self):
        d, root = _repo({"app.py": "def f(): return 1\n", "package.json": "{}"})
        with d:
            a = self._active(root)
            self.assertTrue(a["python"] and a["js_ts"])

    def test_empty_repo_activates_nothing(self):
        d, root = _repo({"README.md": "# hi\n"})
        with d:
            self.assertEqual(set(self._active(root).values()), {False})

    def test_no_root_is_metadata_only_active_false(self):
        self.assertTrue(all(m["active"] is False for m in plugins.list_plugins()))
        self.assertTrue(all(m["active"] is False for m in plugins.list_plugins(None)))


class WiringTest(unittest.TestCase):
    def test_activation_matches_get_language_packs(self):
        # The plugin probe is the single source of truth: it must agree with the
        # existing registry, not a second copy of the detection logic.
        d, root = _repo({"package.json": "{}", "lib/x.mjs": "export function g(){}\n"})
        with d:
            active = {m["id"] for m in plugins.list_plugins(root) if m["active"]}
            registry = {p.name for p in get_language_packs(root)}
            self.assertEqual(active, registry)

    def test_all_language_packs_lists_builtins_without_filtering(self):
        names = {p.name for p in all_language_packs()}
        self.assertEqual(names, {"python", "js_ts"})

    def test_importing_plugins_does_not_eagerly_import_assimilation(self):
        # v1-A is metadata-first: importing the registry and listing providers must
        # NOT pull in the heavy architect/assimilation module (probes resolve it
        # lazily, only when a root is actually probed). Checked in a FRESH process so
        # another test importing architect can't mask the regression.
        import subprocess
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, openfde.plugins as p; p.list_plugins(); "
             "print('openfde.architect' in sys.modules)"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False",
                         f"plugins import pulled in architect:\n{out.stderr}")


if __name__ == "__main__":
    unittest.main()
