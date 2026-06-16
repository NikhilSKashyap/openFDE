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

    # ── file-marker hardening: markers stay repo-relative and bounded ─────────
    def _detected(self, manifest, files, pid):
        d, root = self._manifest_repo(manifest, files)
        with d:
            return self._by_id(root)[pid]["detected"]

    def test_absolute_file_marker_does_not_detect(self):
        # An absolute marker is REJECTED, not normalized into a relative scan — even though
        # the same file exists relative to the repo root (the old lstrip('/') matched it).
        m = {"id": "abs-pack", "kind": "domain_pack", "detects": {"files": ["/package.json"]}}
        self.assertFalse(self._detected(m, {"package.json": "{}"}, "abs-pack"))

    def test_parent_traversal_file_marker_does_not_detect(self):
        m = {"id": "trav-pack", "kind": "domain_pack",
             "detects": {"files": ["../package.json", "../**/*.json"]}}
        self.assertFalse(self._detected(m, {"package.json": "{}"}, "trav-pack"))

    def test_broad_glob_prunes_vendor_dirs(self):
        # **/*.glb must NOT match a file buried in node_modules — the bounded walk prunes
        # vendor dirs (the old unbounded root.glob('**/...') would have found it).
        m = {"id": "glb-pack", "kind": "domain_pack", "detects": {"files": ["**/*.glb"]}}
        self.assertFalse(self._detected(m, {"node_modules/three/model.glb": "GLB"}, "glb-pack"))

    def test_relative_glob_still_detects(self):
        m = {"id": "tsx-pack", "kind": "domain_pack", "detects": {"files": ["src/**/*.tsx"]}}
        self.assertTrue(self._detected(m, {"src/components/App.tsx": "export const x = 1\n"}, "tsx-pack"))
        # and a top-level glob still detects a top-level file
        m2 = {"id": "glb2", "kind": "domain_pack", "detects": {"files": ["**/*.glb"]}}
        self.assertTrue(self._detected(m2, {"assets/duck.glb": "GLB"}, "glb2"))


class WebxrSummaryTest(unittest.TestCase):
    """v1-E: webxr_summary() returns bounded, read-only WebXR architecture hints — frameworks,
    .glb/.gltf assets, XR entrypoints, the markers found — and ALWAYS the honest no-test-lens
    boundary. Metadata only: no install, no imports, no subprocess, no network."""

    def _summary(self, files):
        d, root = _repo(files)
        with d:
            return plugins.webxr_summary(root)

    def test_detects_three_and_r3f_dependency(self):
        s = self._summary({"package.json":
                           '{"dependencies":{"three":"^0.160.0","@react-three/fiber":"^8.15.0"}}'})
        self.assertTrue(s["detected"])
        self.assertIn("Three.js", s["frameworks"])
        self.assertIn("React Three Fiber", s["frameworks"])

    def test_detects_xr_api_in_source(self):
        s = self._summary({"src/xr.js":
                           "if (navigator.xr) s = await navigator.xr.requestSession('immersive-vr')\n"})
        self.assertTrue(s["detected"])
        self.assertIn("navigator.xr", s["markers"])
        self.assertIn("requestsession", s["markers"])
        self.assertTrue(any("src/xr.js" in e for e in s["entrypoints"]))

    def test_detects_glb_gltf_assets(self):
        s = self._summary({"models/duck.glb": "GLB", "scene/room.gltf": "{}"})
        self.assertTrue(s["detected"])
        self.assertEqual(sorted(s["assets"]), ["models/duck.glb", "scene/room.gltf"])

    def test_prunes_vendor_and_build_dirs(self):
        # XR markers buried in node_modules/dist must NOT be scanned; only the real .glb counts.
        s = self._summary({"node_modules/three/build/three.module.js": "navigator.xr",
                           "dist/bundle.js": "navigator.xr.requestSession()", "models/x.glb": "GLB"})
        self.assertEqual(s["markers"], [])              # vendor/build pruned
        self.assertEqual(s["entrypoints"], [])
        self.assertEqual(s["assets"], ["models/x.glb"])
        self.assertTrue(s["detected"])                  # via the asset only

    def test_summary_is_bounded_and_carries_honest_warning(self):
        s = self._summary({"package.json": '{"dependencies":{"three":"^0.160.0"}}'})
        self.assertTrue(any("test lens" in w.lower() or "no webxr runtime" in w.lower()
                            for w in s["warnings"]))
        for key in ("entrypoints", "assets", "frameworks", "markers"):
            self.assertLessEqual(len(s[key]), 20)

    def test_no_webxr_markers_returns_not_detected(self):
        s = self._summary({"app.py": "def f():\n    return 1\n", "README.md": "# hi\n"})
        self.assertFalse(s["detected"])
        self.assertEqual(s["frameworks"], [])
        self.assertEqual(s["assets"], [])
        self.assertEqual(s["markers"], [])
        self.assertEqual(s["fileBadges"], [])           # nothing to badge
        self.assertTrue(s["warnings"])                  # the boundary line is always present

    def test_file_badges_are_a_canvas_annotation_hook(self):
        # entrypoints + assets become a flat {path, kind, label} list the canvas can badge later.
        s = self._summary({"src/xr.js": "navigator.xr.requestSession('immersive-vr')\n",
                           "models/duck.glb": "GLB"})
        by_path = {b["path"]: b for b in s["fileBadges"]}
        self.assertEqual(by_path["models/duck.glb"]["kind"], "asset")
        ep = next(b for p, b in by_path.items() if "src/xr.js" in p)
        self.assertEqual(ep["kind"], "entrypoint")
        for b in s["fileBadges"]:                        # each badge carries a human label
            self.assertTrue(b["label"])


class InstallScaffoldingTest(unittest.TestCase):
    """install_plan is the ALLOWLIST VERDICT — no write, no download, no exec (the actual enable is
    install_local). A known pack reports installable (not yet installed); an unknown/foreign id is
    refused."""

    def test_allowlisted_pack_reports_installable_but_not_installed(self):
        plan = plugins.install_plan("webxr")
        self.assertTrue(plan["ok"] and plan["installable"])
        self.assertFalse(plan["installed"])             # the verdict itself writes nothing
        self.assertTrue(plan["provides"])               # describes what it WOULD add
        self.assertIn("local manifest", plan["reason"].lower())
        self.assertIn("no external code", plan["reason"].lower())

    def test_unknown_id_is_refused(self):
        for bad in ("totally-made-up", "../etc/passwd", "openfde-malware", ""):
            plan = plugins.install_plan(bad)
            self.assertFalse(plan["ok"])
            self.assertFalse(plan["installable"])
            self.assertFalse(plan["installed"])

    def test_install_is_pure_metadata_no_side_effects(self):
        # Calling it many times changes nothing and never installs — it is a confirmation payload.
        before = plugins.install_plan("webxr")
        for _ in range(5):
            self.assertEqual(plugins.install_plan("webxr"), before)
        self.assertFalse(before["installed"])


class LocalManifestV1FTest(unittest.TestCase):
    """v1-F additions to the local-manifest layer: version/description/capabilities surface, and a
    local manifest SUPERSEDES a same-id suggestion (one WebXR row, no duplicate)."""

    def test_manifest_carries_version_description_capabilities(self):
        d, root = _repo({".openfde/plugins/vp.json": json.dumps({
            "id": "vp", "kind": "domain_pack", "status": "available", "version": "2.1.0",
            "description": "a pack", "capabilities": ["cap-a", "cap-b"], "provides": ["p1"]})})
        with d:
            m = next(x for x in plugins.list_plugins(root) if x["id"] == "vp")
            self.assertEqual(m["version"], "2.1.0")
            self.assertEqual(m["description"], "a pack")
            self.assertEqual(m["capabilities"], ["cap-a", "cap-b"])

    def test_local_webxr_supersedes_suggested_no_duplicate(self):
        d, root = _repo({".openfde/plugins/webxr.json": json.dumps({
            "id": "webxr", "kind": "domain_pack", "displayName": "WebXR (local)",
            "status": "available", "provides": ["xr-entrypoints"]})})
        with d:
            rows = [m for m in plugins.list_plugins(root) if m["id"] == "webxr"]
            self.assertEqual(len(rows), 1)                  # exactly one WebXR — no duplicate
            self.assertEqual(rows[0]["source"], "local")    # local supersedes the suggestion
            self.assertEqual(rows[0]["status"], "available")


class InstallLocalTest(unittest.TestCase):
    """v1-F: install ENABLES a pack by WRITING its local manifest — a JSON file only, never a
    download/import/exec. Allowlisted + idempotent; the written manifest validates + supersedes the
    suggestion."""

    def _plugins_dir(self, root):
        return root / ".openfde" / "plugins"

    def test_install_webxr_writes_a_valid_local_manifest(self):
        d, root = _repo({})
        with d:
            res = plugins.install_local(root, "webxr")
            self.assertTrue(res["ok"] and res["installed"])
            self.assertIn("no external code", res["reason"].lower())
            dest = self._plugins_dir(root) / "webxr.json"
            self.assertTrue(dest.exists())
            data = json.loads(dest.read_text())
            self.assertEqual(data["id"], "webxr")
            self.assertEqual(data["status"], "available")
            # now an available LOCAL provider, superseding the suggestion (one row)
            rows = [m for m in plugins.list_plugins(root) if m["id"] == "webxr"]
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["source"], rows[0]["status"]), ("local", "available"))

    def test_install_is_idempotent(self):
        d, root = _repo({})
        with d:
            plugins.install_local(root, "webxr")
            again = plugins.install_local(root, "webxr")
            self.assertTrue(again["installed"] and again["alreadyEnabled"])
            self.assertEqual(len(list(self._plugins_dir(root).glob("*.json"))), 1)

    def test_unknown_id_refused_and_writes_nothing(self):
        d, root = _repo({})
        with d:
            res = plugins.install_local(root, "openfde-malware")
            self.assertFalse(res["ok"] or res["installed"])
            self.assertFalse(self._plugins_dir(root).exists())   # nothing written

    def test_written_manifest_has_no_code_paths(self):
        # The written file is pure data; it declares NO import/entrypoint/command/url — and the read
        # path never honors such fields. There is no code path.
        d, root = _repo({})
        with d:
            plugins.install_local(root, "webxr")
            data = json.loads((self._plugins_dir(root) / "webxr.json").read_text())
            for forbidden in ("import", "entryPoint", "entry_point", "command", "cmd",
                              "url", "package", "exec", "script", "module"):
                self.assertNotIn(forbidden, data)


if __name__ == "__main__":
    unittest.main()
