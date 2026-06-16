"""
Tests for the internal plugin registry (openfde.plugins) — Plugin Registry v1-A.
The law: built-in capability providers are DESCRIBABLE as metadata, activation is
probed from cheap repo markers (the language packs' own detection), and nothing
heavy is imported or installed. The existing language-pack registry is untouched.
"""
import json
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


class WebXrSuggestionTest(unittest.TestCase):
    """v1-B Lite: the WebXR domain pack is a deterministic SUGGESTION — surfaced as
    metadata when cheap repo markers match, never active, never installed/loaded."""

    def _webxr(self, root):
        for m in plugins.list_plugins(root):
            if m["id"] == "webxr":
                return m
        self.fail("webxr provider was not listed")

    def test_webxr_is_always_listed_as_a_domain_pack(self):
        d, root = _repo({"app.py": "x\n"})
        with d:
            w = self._webxr(root)
            self.assertEqual(w["kind"], "domain_pack")

    def test_dependency_hint_suggests_webxr(self):
        d, root = _repo({"package.json": '{"dependencies":{"three":"^0.160.0"}}'})
        with d:
            w = self._webxr(root)
            self.assertTrue(w["detected"])
            self.assertEqual(w["status"], "suggested")

    def test_glb_asset_suggests_webxr(self):
        d, root = _repo({"index.html": "<html></html>", "models/duck.glb": "GLB",
                         "main.js": "console.log(1)\n"})
        with d:
            self.assertEqual(self._webxr(root)["status"], "suggested")

    def test_html_plus_xr_api_marker_suggests_webxr(self):
        d, root = _repo({"index.html":
                         '<script>navigator.xr.requestSession("immersive-vr")</script>'})
        with d:
            w = self._webxr(root)
            self.assertTrue(w["detected"])
            self.assertEqual(w["status"], "suggested")

    def test_non_xr_repo_marks_webxr_missing(self):
        d, root = _repo({"app.py": "def f():\n    return 1\n",
                         "pkg/util.py": "x = 1\n"})
        with d:
            w = self._webxr(root)
            self.assertFalse(w["detected"])
            self.assertEqual(w["status"], "missing")

    def test_suggestion_is_never_active_even_when_detected(self):
        # Read-only: a suggestion describes a pack the repo *could* use; v1-B Lite
        # loads/installs nothing, so it must never report itself active.
        d, root = _repo({"package.json": '{"dependencies":{"aframe":"^1.5.0"}}'})
        with d:
            w = self._webxr(root)
            self.assertTrue(w["detected"])
            self.assertFalse(w["active"])

    def test_every_manifest_exposes_detected(self):
        d, root = _repo({"package.json": "{}", "a.ts": "export const x = 1\n"})
        with d:
            for m in plugins.list_plugins(root):
                self.assertIn("detected", m)
                self.assertIsInstance(m["detected"], bool)


class LocalManifestProvidersTest(unittest.TestCase):
    """v1-C: repo-local manifests are read-only provider declarations. They prove
    plugins can exist outside hardcoded built-ins without installing or executing code."""

    def _manifest_repo(self, manifest: dict, extra=None, name="webxr-local.json"):
        files = {f".openfde/plugins/{name}": json.dumps(manifest)}
        files.update(extra or {})
        return _repo(files)

    def _by_id(self, root):
        return {m["id"]: m for m in plugins.list_plugins(root)}

    def test_local_manifest_is_listed_as_available_metadata(self):
        manifest = {
            "id": "webxr-local",
            "kind": "domain_pack",
            "displayName": "WebXR Local Lens",
            "status": "available",
            "activatesOn": "navigator.xr or Three.js",
            "provides": ["xr-scene-tags", "asset-hints"],
        }
        d, root = self._manifest_repo(manifest)
        with d:
            p = self._by_id(root)["webxr-local"]
            self.assertEqual(p["source"], "local")
            self.assertEqual(p["status"], "available")
            self.assertEqual(p["kind"], "domain_pack")
            self.assertTrue(p["detected"])        # no detects block = declared for this repo
            self.assertFalse(p["active"])         # v1-C is metadata only, no code loaded
            self.assertIn("xr-scene-tags", p["provides"])

    def test_local_manifest_marker_probe_can_detect_repo(self):
        manifest = {
            "id": "react-pack",
            "kind": "domain_pack",
            "displayName": "React",
            "detects": {"dependencies": ["react"], "files": ["src/**/*.tsx"]},
            "provides": ["react-components"],
        }
        d, root = self._manifest_repo(
            manifest,
            {"package.json": '{"dependencies":{"react":"latest"}}',
             "src/App.tsx": "export function App(){ return <div/> }\n"})
        with d:
            p = self._by_id(root)["react-pack"]
            self.assertTrue(p["detected"])
            self.assertEqual(p["status"], "available")

    def test_local_manifest_disabled_stays_disabled_and_inactive(self):
        manifest = {"id": "semgrep-local", "kind": "verify_adapter",
                    "status": "disabled", "provides": ["security-scan"]}
        d, root = self._manifest_repo(manifest)
        with d:
            p = self._by_id(root)["semgrep-local"]
            self.assertEqual(p["status"], "disabled")
            self.assertFalse(p["active"])

    def test_invalid_local_manifest_is_ignored(self):
        manifest = {"id": "../bad", "kind": "domain_pack", "status": "available"}
        d, root = self._manifest_repo(manifest)
        with d:
            self.assertNotIn("../bad", self._by_id(root))

    def test_no_root_does_not_read_local_manifests(self):
        self.assertNotIn("webxr-local", self._by_id(None))

    def test_local_manifest_content_marker_detects_repo(self):
        manifest = {"id": "xr-content", "kind": "domain_pack",
                    "detects": {"content": ["navigator.xr", "XRFrame"]},
                    "provides": ["xr-hints"]}
        d, root = self._manifest_repo(manifest, {"main.js": "if (navigator.xr) start()\n"})
        with d:
            p = self._by_id(root)["xr-content"]
            self.assertTrue(p["detected"])              # a content marker matched a repo file
            self.assertEqual(p["status"], "available")

    def test_unknown_kind_and_status_are_rejected(self):
        for bad in ({"id": "x1", "kind": "wizard", "status": "available"},      # bad kind
                    {"id": "x2", "kind": "domain_pack", "status": "enabled"}):   # bad status
            d, root = self._manifest_repo(bad, name=f"{bad['id']}.json")
            with d:
                self.assertNotIn(bad["id"], self._by_id(root))

    def test_source_tags_separate_local_from_builtin_and_suggested(self):
        d, root = self._manifest_repo(
            {"id": "intg-x", "kind": "integration", "status": "available"})
        with d:
            by = self._by_id(root)
            self.assertEqual(by["intg-x"]["source"], "local")
            self.assertEqual(by["python"]["source"], "builtin")
            self.assertEqual(by["webxr"]["source"], "suggested")


if __name__ == "__main__":
    unittest.main()
