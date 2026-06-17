"""
Tests for the internal plugin registry (openfde.plugins) — Plugin Registry v1-A.
The law: built-in capability providers are DESCRIBABLE as metadata, activation is
probed from cheap repo markers (the language packs' own detection), and nothing
heavy is imported or installed. The existing language-pack registry is untouched.
"""
import importlib.metadata
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from openfde import architect, plugins, verify
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

    def test_slice1_enrichment_badges_and_asset_groups(self):
        # Slice 1: richer, honest per-file badges (XR API / Three / R3F / Scene / Shader / 3D asset)
        # + shaders/textures classification + assets grouped by type (no hairball).
        s = self._summary({
            "package.json": json.dumps({"dependencies": {"three": "^0.160.0",
                                                         "@react-three/fiber": "^8"}}),
            "src/main.js": ("import * as THREE from 'three'\n"
                            "navigator.xr.requestSession('immersive-vr')\n"
                            "const cam = new PerspectiveCamera(70, 1, 0.1, 100)\n"),
            "src/app.jsx": "import { Canvas } from '@react-three/fiber'\nexport const App = () => null\n",
            "shaders/water.glsl": "void main() { gl_FragColor = vec4(1.0); }\n",
            "models/duck.glb": "GLB",
            "textures/env.hdr": "HDR",
        })
        labels = {}
        for b in s["fileBadges"]:
            labels.setdefault(b["path"], set()).add(b["label"])
        self.assertTrue({"XR API", "Three", "Scene"} <= labels.get("src/main.js", set()))
        self.assertIn("R3F", labels.get("src/app.jsx", set()))
        self.assertIn("Shader", labels.get("shaders/water.glsl", set()))
        self.assertEqual(s["shaders"], ["shaders/water.glsl"])
        self.assertIn("models/duck.glb", s["assets"])
        groups = {g["type"]: g["paths"] for g in s["assetGroups"]}
        self.assertEqual(groups.get("3D model"), ["models/duck.glb"])
        self.assertEqual(groups.get("Shader"), ["shaders/water.glsl"])
        self.assertEqual(groups.get("Texture"), ["textures/env.hdr"])
        self.assertTrue(any("no webxr runtime" in w.lower() for w in s["warnings"]))  # boundary copy


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


class ExternalPluginDiscoveryTest(unittest.TestCase):
    """v1-G: external plugins discovered via Python entry points. Discovery is metadata/probe
    ONLY (``source='external'``), defensive (a bad plugin is logged + skipped, never crashes),
    de-duped against built-ins / locals / suggestions, and never imports the pack's heavy
    analyzer (activation/runtime loading stays lazy + deferred)."""

    MODNAME = "_ofde_test_extpack"

    def setUp(self):
        # A real, lightweight provider module in sys.modules so EntryPoint.load() resolves it
        # without disk. Importing it does NOT import the (separate) heavy analyzer module.
        mod = types.ModuleType(self.MODNAME)
        mod.good = lambda: {"id": "ext-xr", "kind": "domain_pack", "displayName": "External XR",
                            "version": "9.9.9", "provides": ["xr-ext"],
                            "detects": {"dependencies": ["three"]}}
        mod.webxr_pkg = lambda: {"id": "webxr", "kind": "domain_pack",
                                 "displayName": "WebXR (packaged)", "status": "available",
                                 "provides": ["xr-entrypoints"]}
        mod.two = lambda: [{"id": "ext-a", "kind": "integration"},
                           {"id": "ext-b", "kind": "layout_engine"}]
        def _boom():
            raise RuntimeError("malformed external plugin")
        mod.boom = _boom
        mod.bad_kind = lambda: {"id": "ext-bad", "kind": "wizard", "status": "available"}
        mod.empty = lambda: None
        sys.modules[self.MODNAME] = mod

    def tearDown(self):
        sys.modules.pop(self.MODNAME, None)
        sys.modules.pop(self.MODNAME + ".heavy", None)

    def _ep(self, name, attr, group):
        return importlib.metadata.EntryPoint(name, f"{self.MODNAME}:{attr}", group)

    def _patch_eps(self, mapping):
        """Patch importlib.metadata.entry_points to yield EntryPoints per group from
        ``mapping`` = {group: [(ep_name, provider_attr), ...]}."""
        def fake_entry_points(group=None):
            return [self._ep(n, a, group) for (n, a) in mapping.get(group, [])]
        return mock.patch("importlib.metadata.entry_points", fake_entry_points)

    def test_external_plugin_is_discovered_via_entry_points(self):
        with self._patch_eps({"openfde.domain_packs": [("xr", "good")]}):
            by_id = {m["id"]: m for m in plugins.list_plugins()}
        self.assertIn("ext-xr", by_id)
        m = by_id["ext-xr"]
        self.assertEqual(m["source"], "external")
        self.assertEqual(m["kind"], "domain_pack")
        self.assertEqual(m["version"], "9.9.9")
        self.assertIn("xr-ext", m["provides"])
        self.assertFalse(m["active"])             # discovered, never activated/loaded

    def test_provider_may_return_several_manifests_across_groups(self):
        with self._patch_eps({"openfde.plugins": [("multi", "two")]}):
            by_id = {m["id"]: m for m in plugins.list_plugins()}
        self.assertEqual(by_id["ext-a"]["source"], "external")
        self.assertEqual(by_id["ext-b"]["kind"], "layout_engine")

    def test_malformed_external_plugins_are_ignored_safely(self):
        # load-fails / provider-raises / invalid-manifest / empty — none crash, none appear;
        # a valid sibling still gets through, and each failure is reported as a diagnostic record.
        mapping = {"openfde.domain_packs": [
            ("boomer", "boom"),            # provider raises
            ("bad", "bad_kind"),           # invalid kind
            ("empty", "empty"),            # returns None
            ("missing", "does_not_exist"), # attr missing → load() fails
            ("good", "good"),              # a valid one still survives
        ]}
        diags = []
        with self._patch_eps(mapping):
            with self.assertLogs("openfde.plugins", level="WARNING"):
                specs = plugins._discover_external(diagnostics=diags)
        self.assertEqual({s.id for s in specs}, {"ext-xr"})       # only the good one
        self.assertEqual({d["name"] for d in diags}, {"boomer", "bad", "empty", "missing"})
        # and the public listing never raises with bad plugins present
        with self._patch_eps(mapping):
            self.assertTrue(any(m["id"] == "ext-xr" for m in plugins.list_plugins()))

    def test_builtin_local_external_merge_and_dedup(self):
        # builtin (python/js_ts) + suggested webxr + external (webxr + ext-xr) + local webxr.
        # Precedence: local webxr wins; external webxr & the suggestion drop; ext-xr stays.
        manifest = {"id": "webxr", "kind": "domain_pack", "displayName": "WebXR (local)",
                    "status": "available", "provides": ["xr-entrypoints"]}
        d, root = _repo({".openfde/plugins/webxr.json": json.dumps(manifest)})
        mapping = {"openfde.domain_packs": [("wx", "webxr_pkg"), ("xr", "good")]}
        with d, self._patch_eps(mapping):
            rows = plugins.list_plugins(root)
        by_id = {}
        for m in rows:
            by_id.setdefault(m["id"], []).append(m)
        self.assertEqual(len(by_id["webxr"]), 1)                 # exactly one WebXR row
        self.assertEqual(by_id["webxr"][0]["source"], "local")   # local > external > suggested
        self.assertEqual(by_id["ext-xr"][0]["source"], "external")
        self.assertIn("python", by_id)                           # built-ins untouched
        self.assertIn("js_ts", by_id)
        ids = [m["id"] for m in rows]
        self.assertEqual(len(ids), len(set(ids)), "no duplicate ids in the merged listing")

    def test_external_supersedes_suggestion_when_no_local(self):
        mapping = {"openfde.domain_packs": [("wx", "webxr_pkg")]}
        d, root = _repo({"package.json": '{"dependencies":{"three":"^0.160.0"}}'})  # would suggest
        with d, self._patch_eps(mapping):
            rows = [m for m in plugins.list_plugins(root) if m["id"] == "webxr"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "external")          # external supersedes the suggestion

    def test_external_cannot_shadow_a_builtin_id(self):
        # An external declaring a core id (python) must NOT replace or duplicate the built-in.
        plugins_mod_mod = sys.modules[self.MODNAME]
        plugins_mod_mod.shadow = lambda: {"id": "python", "kind": "domain_pack",
                                          "displayName": "evil", "status": "available"}
        with self._patch_eps({"openfde.plugins": [("shadow", "shadow")]}):
            rows = [m for m in plugins.list_plugins() if m["id"] == "python"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "builtin")

    def test_discovery_does_not_import_the_heavy_analyzer(self):
        # Discovery loads only the lightweight manifest provider; the pack's heavy analyzer
        # (a separate module) must stay unimported until activation (which v1-G does not do).
        heavy = self.MODNAME + ".heavy"
        self.assertNotIn(heavy, sys.modules)
        with self._patch_eps({"openfde.domain_packs": [("xr", "good")]}):
            specs = plugins.external_specs()
        self.assertTrue(any(s.id == "ext-xr" for s in specs))
        self.assertNotIn(heavy, sys.modules)

    def test_importing_plugins_and_discovery_stay_cheap(self):
        # In a FRESH process: importing the registry must not pull in the heavy architect, and
        # neither must external discovery (entry-point scanning reads metadata, imports no package).
        import subprocess
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, openfde.plugins as p;"
             "assert 'openfde.architect' not in sys.modules, 'import pulled architect';"
             "p.external_specs();"
             "print('openfde.architect' in sys.modules)"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False",
                         f"external discovery pulled in architect:\n{out.stderr}")


class RuntimeActivationTest(unittest.TestCase):
    """v1-H: lazy runtime activation. Discovery stays metadata-only (no runtime import); a plugin's
    runtime module is imported ONLY when its probe matches the repo AND a caller asks for a
    capability. Bad imports/factories are logged + skipped, never raised; results cache per repo;
    repo-local manifests may not declare a runtime (security)."""

    PROV = "_ofde_test_rtprov"        # manifest-provider module (sys.modules, lightweight)
    RT = "_ofde_test_runtime"         # runtime module (ON DISK, imported lazily — never pre-loaded)
    RT_BAD = "_ofde_test_runtime_bad"

    def setUp(self):
        prov = types.ModuleType(self.PROV)
        # Detect on a synthetic FILE marker (no package.json / .py / .ts) so NO built-in language
        # pack or suggestion activates — this suite isolates EXTERNAL runtime mechanics from the real
        # providers (js_ts now exposes architecture/test/failure/repro hooks; webxr exposes
        # domain_summary). A dependency/source marker would conflate them.
        prov.summary_pack = lambda: {
            "id": "ext-rt", "kind": "domain_pack", "displayName": "Ext Runtime", "version": "1.0.0",
            "capabilities": ["domain_summary"], "detects": {"files": ["**/*.ofdetest"]},
            "runtime": {"module": self.RT, "factory": "make"}}
        prov.arch_pack = lambda: {
            "id": "ext-arch", "kind": "domain_pack", "capabilities": ["architecture"],
            "detects": {"files": ["**/*.ofdetest"]},
            "runtime": {"module": self.RT, "factory": "make"}}
        prov.badimport_pack = lambda: {
            "id": "ext-badimp", "kind": "domain_pack", "capabilities": ["domain_summary"],
            "detects": {"files": ["**/*.ofdetest"]},
            "runtime": {"module": self.RT_BAD, "factory": "make"}}
        prov.badfactory_pack = lambda: {
            "id": "ext-badfac", "kind": "domain_pack", "capabilities": ["domain_summary"],
            "detects": {"files": ["**/*.ofdetest"]},
            "runtime": {"module": self.RT, "factory": "boom"}}
        sys.modules[self.PROV] = prov

        # Runtime modules on DISK (temp dir on sys.path) so they are imported lazily, not pre-loaded.
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        (d / f"{self.RT}.py").write_text(
            "def make():\n"
            "    return {'domain_summary': lambda root=None: {'ok': True},\n"
            "            'architecture': lambda root=None: {'nodes': []}}\n"
            "def boom():\n"
            "    raise RuntimeError('factory exploded')\n")
        (d / f"{self.RT_BAD}.py").write_text("raise ImportError('runtime import blew up')\n")
        sys.path.insert(0, str(d))
        plugins._RUNTIME_CACHE.clear()

    def tearDown(self):
        sys.modules.pop(self.PROV, None)
        for m in (self.RT, self.RT_BAD):
            sys.modules.pop(m, None)
        try:
            sys.path.remove(str(Path(self._tmp.name)))
        except ValueError:
            pass
        self._tmp.cleanup()
        plugins._RUNTIME_CACHE.clear()

    def _patch_eps(self, attrs):
        def fake_entry_points(group=None):
            if group != "openfde.domain_packs":
                return []
            return [importlib.metadata.EntryPoint(a, f"{self.PROV}:{a}", group) for a in attrs]
        return mock.patch("importlib.metadata.entry_points", fake_entry_points)

    def _match_repo(self):
        return _repo({"marker.ofdetest": "ofde external-runtime test marker\n"})

    def test_list_plugins_does_not_import_runtime(self):
        self.assertNotIn(self.RT, sys.modules)
        d, root = self._match_repo()
        with d, self._patch_eps(["summary_pack"]):
            rows = {r["id"]: r for r in plugins.list_plugins(root)}
        self.assertIn("ext-rt", rows)
        self.assertTrue(rows["ext-rt"]["hasRuntime"])               # metadata says it HAS a runtime…
        self.assertNotIn(self.RT, sys.modules, "listing must NOT import the runtime module")

    def test_runtime_loads_only_on_request(self):
        d, root = self._match_repo()
        with d, self._patch_eps(["summary_pack"]):
            self.assertNotIn(self.RT, sys.modules)                  # …but it is not loaded yet
            providers = plugins.runtime_for_capability(root, "domain_summary")
            self.assertIn(self.RT, sys.modules)                     # imported NOW, on request
            self.assertEqual([p["id"] for p in providers], ["ext-rt"])
            hook = plugins.runtime_hook(providers[0]["runtime"], "domain_summary")
            self.assertTrue(callable(hook))
            self.assertEqual(hook(root), {"ok": True})

    def test_runtime_not_loaded_when_repo_does_not_match(self):
        d, root = _repo({"app.py": "x = 1\n"})                       # no `three` dep → probe fails
        with d, self._patch_eps(["summary_pack"]):
            self.assertEqual(plugins.runtime_for_capability(root, "domain_summary"), [])
            self.assertIsNone(plugins.load_plugin_runtime("ext-rt", root))
        self.assertNotIn(self.RT, sys.modules)                       # never imported

    def test_bad_runtime_import_is_logged_not_raised(self):
        d, root = self._match_repo()
        with d, self._patch_eps(["badimport_pack"]):
            with self.assertLogs("openfde.plugins", level="WARNING"):
                self.assertIsNone(plugins.load_plugin_runtime("ext-badimp", root))

    def test_bad_runtime_factory_is_logged_not_raised(self):
        d, root = self._match_repo()
        with d, self._patch_eps(["badfactory_pack"]):
            with self.assertLogs("openfde.plugins", level="WARNING"):
                self.assertIsNone(plugins.load_plugin_runtime("ext-badfac", root))

    def test_runtime_for_capability_filters_by_capability(self):
        d, root = self._match_repo()
        with d, self._patch_eps(["summary_pack", "arch_pack"]):
            summ = plugins.runtime_for_capability(root, "domain_summary")
            arch = plugins.runtime_for_capability(root, "architecture")
        self.assertEqual({p["id"] for p in summ}, {"ext-rt"})
        self.assertEqual({p["id"] for p in arch}, {"ext-arch"})

    def test_runtime_is_cached_no_reimport(self):
        d, root = self._match_repo()
        with d, self._patch_eps(["summary_pack"]):
            rt1 = plugins.load_plugin_runtime("ext-rt", root)
            rt2 = plugins.load_plugin_runtime("ext-rt", root)
        self.assertIsNotNone(rt1)
        self.assertIs(rt1, rt2)                                      # same object — cached, not re-imported

    def test_active_plugins_filters_by_repo_and_capability(self):
        d, root = self._match_repo()
        with d, self._patch_eps(["summary_pack", "arch_pack"]):
            active_ids = {p["id"] for p in plugins.active_plugins(root)}
            ds_ids = {p["id"] for p in plugins.active_plugins(root, capability="domain_summary")}
        self.assertEqual(active_ids, {"ext-rt", "ext-arch"})            # only the fake providers match
        self.assertEqual(ds_ids, {"ext-rt"})                            # capability narrows it

    def test_local_manifest_runtime_is_ignored(self):
        # SECURITY: a repo-local manifest is untrusted, so its runtime block is dropped — it lists as
        # metadata but provides NO runtime and never triggers a code import.
        manifest = {"id": "local-rt", "kind": "domain_pack", "status": "available",
                    "capabilities": ["domain_summary"],
                    "runtime": {"module": self.RT, "factory": "make"}}
        d, root = _repo({".openfde/plugins/local-rt.json": json.dumps(manifest)})
        with d:
            rows = {r["id"]: r for r in plugins.list_plugins(root)}
            self.assertIn("local-rt", rows)                           # still listed as metadata
            self.assertFalse(rows["local-rt"]["hasRuntime"])          # runtime dropped
            self.assertIsNone(plugins.load_plugin_runtime("local-rt", root))
        self.assertNotIn(self.RT, sys.modules)                        # never imported


class WebxrRuntimeMigrationTest(unittest.TestCase):
    """v1-H migration: WebXR ``domain_summary`` runs behind the plugin runtime hook (the first real
    capability migrated), with a guaranteed fallback to core ``webxr_summary``. Listing never imports
    the runtime; the response shape is unchanged; a repo-local manifest still cannot inject a runtime."""

    RT = "openfde.plugins_runtime.webxr"        # the built-in runtime module (imported lazily)

    def setUp(self):
        sys.modules.pop(self.RT, None)
        plugins._RUNTIME_CACHE.clear()

    def tearDown(self):
        plugins._RUNTIME_CACHE.clear()

    def _xr_repo(self):
        return _repo({"package.json": '{"dependencies":{"three":"^0.160.0"}}',
                      "src/app.js": "navigator.xr.requestSession('immersive-vr')\n"})

    def test_webxr_spec_declares_runtime_and_capability(self):
        d, root = self._xr_repo()
        with d:
            row = next(m for m in plugins.list_plugins(root) if m["id"] == "webxr")
        self.assertIn("domain_summary", row["capabilities"])
        self.assertTrue(row["hasRuntime"])                       # advertises the runtime (metadata only)

    def test_list_plugins_does_not_import_webxr_runtime(self):
        # Fresh process: listing (even on a WebXR-active repo) must NOT import the runtime module.
        import subprocess
        d, root = self._xr_repo()
        with d:
            code = ("import sys, openfde.plugins as p;"
                    f"p.list_plugins({json.dumps(str(root))});"
                    "print('openfde.plugins_runtime.webxr' in sys.modules)")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False",
                         f"listing imported the WebXR runtime:\n{out.stderr}")

    def test_resolve_loads_runtime_only_on_request_and_matches_core(self):
        d, root = self._xr_repo()
        with d:
            self.assertNotIn(self.RT, sys.modules)               # not loaded yet
            resolved = plugins.resolve_webxr_summary(root)
            self.assertIn(self.RT, sys.modules)                  # imported NOW, on summary request
            core = plugins.webxr_summary(root)
        self.assertEqual(set(resolved), set(core))               # identical shape…
        self.assertEqual(resolved["detected"], core["detected"]) # …and identical content
        self.assertEqual(resolved["frameworks"], core["frameworks"])
        self.assertIn("Three.js", resolved["frameworks"])

    def test_summary_shape_is_unchanged(self):
        d, root = self._xr_repo()
        with d:
            s = plugins.resolve_webxr_summary(root)
        for key in ("detected", "entrypoints", "assets", "frameworks", "markers", "fileBadges", "warnings"):
            self.assertIn(key, s)

    def test_local_webxr_manifest_cannot_inject_runtime(self):
        # SECURITY: a repo-local manifest declaring a runtime (here a malicious os.system) is dropped —
        # the row lists as metadata with NO runtime, and the summary still resolves via core fallback.
        manifest = {"id": "webxr", "kind": "domain_pack", "status": "available",
                    "capabilities": ["domain_summary"],
                    "runtime": {"module": "os", "factory": "system"}}
        d, root = _repo({".openfde/plugins/webxr.json": json.dumps(manifest),
                         "package.json": '{"dependencies":{"three":"^0.160.0"}}'})
        with d:
            row = next(m for m in plugins.list_plugins(root) if m["id"] == "webxr")
            self.assertEqual(row["source"], "local")             # local supersedes the suggestion
            self.assertFalse(row["hasRuntime"])                  # repo-declared runtime DROPPED
            self.assertIsNone(plugins.load_plugin_runtime("webxr", root))
            self.assertIn("detected", plugins.resolve_webxr_summary(root))   # endpoint still works

    def test_runtime_failure_falls_back_to_core(self):
        d, root = self._xr_repo()
        with d:
            with mock.patch.object(plugins, "runtime_for_capability",
                                   side_effect=RuntimeError("runtime boom")):
                summary = plugins.resolve_webxr_summary(root)   # must not raise
        self.assertTrue(summary["detected"])                     # core fallback produced a real summary
        self.assertIn("Three.js", summary["frameworks"])


class CuratedInstallPlanTest(unittest.TestCase):
    """v1-I: curated INSTALL planning — a proposal, never an execution. Known ids return a structured
    plan (argv lists / endpoints, never shell strings); unknown ids are refused; planning runs nothing;
    and the existing WebXR enable-local path is preserved."""

    def test_known_id_returns_a_plan(self):
        p = plugins.plugin_install_plan("webxr")
        self.assertTrue(p["ok"] and p["installable"])
        self.assertTrue(p["requiresApproval"])
        self.assertEqual(p["method"], "builtin-local")
        self.assertEqual(p["actions"][0]["type"], "enable-local")
        self.assertIn("no package", p["reason"].lower())

    def test_unknown_id_is_refused(self):
        for bad in ("totally-made-up", "../etc/passwd", "openfde-malware", ""):
            p = plugins.plugin_install_plan(bad)
            self.assertFalse(p["ok"])
            self.assertFalse(p["installable"])
            self.assertEqual(p["actions"], [])
            self.assertTrue(p["requiresApproval"])      # an unknown id is refused, still approval-gated

    def test_curated_registry_lists_webxr_builtin_local(self):
        by_id = {e["id"]: e for e in plugins.curated_plugins()}
        self.assertIn("webxr", by_id)
        self.assertEqual(by_id["webxr"]["method"], "builtin-local")  # honest: built-in / demo / local

    def test_plan_actions_are_structured_never_shell_strings(self):
        # the builtin-local action is a structured dict (an endpoint), not a shell command string
        for action in plugins.plugin_install_plan("webxr")["actions"]:
            self.assertIsInstance(action, dict)
            self.assertNotIn("shell", action)
            self.assertNotIn("cmd", action)
        # a curated pip pack proposes a STRUCTURED argv LIST from the registry (never a shell string)
        fake = {"id": "ext-pip", "displayName": "Ext Pip Pack", "kind": "domain_pack",
                "method": "pip", "packageName": "openfde-ext-pip", "version": ">=1.0",
                "capabilities": ["domain_summary"], "description": "x", "status": "external"}
        with mock.patch.dict(plugins._CURATED_PLUGINS, {"ext-pip": fake}):
            p = plugins.plugin_install_plan("ext-pip")
            self.assertEqual(p["method"], "pip")
            argv = p["actions"][0]["argv"]
            self.assertIsInstance(argv, list)                        # argv list, NOT a shell string
            self.assertEqual(argv[:4], [sys.executable, "-m", "pip", "install"])
            self.assertEqual(argv[-1], "openfde-ext-pip>=1.0")       # pinned spec from the registry
            self.assertTrue(p["requiresApproval"])

    def test_planning_executes_nothing(self):
        # Planning must NEVER touch subprocess — patch it to explode and confirm plans still return.
        import subprocess
        fake = {"id": "ext-pip", "displayName": "x", "kind": "domain_pack", "method": "pip",
                "packageName": "openfde-ext-pip", "version": "", "capabilities": [],
                "description": "", "status": "external"}

        def boom(*a, **k):
            raise AssertionError("planning must not execute a subprocess")

        with mock.patch.object(subprocess, "run", boom), \
             mock.patch.object(subprocess, "Popen", boom), \
             mock.patch.dict(plugins._CURATED_PLUGINS, {"ext-pip": fake}):
            self.assertTrue(plugins.plugin_install_plan("webxr")["ok"])
            self.assertTrue(plugins.plugin_install_plan("ext-pip")["ok"])

    def test_webxr_enable_local_still_works(self):
        # v1-F preserved: enabling WebXR still writes a local manifest (a JSON file, no code run).
        d, root = _repo({})
        with d:
            res = plugins.install_local(root, "webxr")
            self.assertTrue(res["ok"] and res["installed"])
            self.assertTrue((root / ".openfde" / "plugins" / "webxr.json").exists())


class TreeSitterInstallTest(unittest.TestCase):
    """L1-D default-path nudge: a JS/TS repo without tree-sitter gets an approval-gated, STRUCTURED
    install plan (proposal only — nothing runs); non-JS repos and already-installed repos get none."""

    _TS = "openfde.language_packs.js_ts_treesitter.available"

    def test_curated_registry_includes_treesitter(self):
        by_id = {e["id"]: e for e in plugins.curated_plugins()}
        self.assertIn("treesitter-js-ts", by_id)
        self.assertEqual(by_id["treesitter-js-ts"]["method"], "pip")

    def test_plan_is_structured_argv_allowlisted(self):
        p = plugins.plugin_install_plan("treesitter-js-ts")
        self.assertTrue(p["ok"] and p["installable"] and p["requiresApproval"])
        self.assertEqual(p["method"], "pip")
        argv = p["actions"][0]["argv"]
        self.assertIsInstance(argv, list)                            # argv list, NEVER a shell string
        self.assertTrue(all(isinstance(a, str) for a in argv))
        self.assertEqual(argv[:4], [sys.executable, "-m", "pip", "install"])
        self.assertTrue(any("tree-sitter" in a for a in argv[4:]))   # allowlisted grammar packages
        self.assertNotIn("shell", p["actions"][0])

    def test_unknown_install_id_refused(self):
        p = plugins.plugin_install_plan("treesitter-evil; rm -rf /")
        self.assertFalse(p["ok"])
        self.assertFalse(p["installable"])
        self.assertEqual(p["actions"], [])

    def test_recommended_for_js_ts_repo_without_treesitter(self):
        d, root = _repo({"package.json": '{"name":"x"}', "src/app.ts": "export const x = 1\n"})
        with d, mock.patch(self._TS, return_value=False):
            rec = plugins.treesitter_recommendation(root)
        self.assertTrue(rec["recommended"])
        self.assertEqual(rec["id"], "treesitter-js-ts")
        self.assertEqual(rec["plan"]["actions"][0]["argv"][:4], [sys.executable, "-m", "pip", "install"])

    def test_not_recommended_when_treesitter_present(self):
        d, root = _repo({"package.json": '{"name":"x"}', "src/app.ts": "export const x = 1\n"})
        with d, mock.patch(self._TS, return_value=True):
            self.assertFalse(plugins.treesitter_recommendation(root)["recommended"])

    def test_not_recommended_for_non_js_repo(self):
        d, root = _repo({"main.py": "def f():\n    return 1\n"})
        with d, mock.patch(self._TS, return_value=False):
            self.assertFalse(plugins.treesitter_recommendation(root)["recommended"])

    def test_planning_runs_no_subprocess(self):
        import subprocess

        def boom(*a, **k):
            raise AssertionError("planning must not execute a subprocess")

        d, root = _repo({"package.json": '{"name":"x"}', "src/app.ts": "export const x = 1\n"})
        with d, mock.patch.object(subprocess, "run", boom), \
             mock.patch.object(subprocess, "Popen", boom), \
             mock.patch(self._TS, return_value=False):
            self.assertTrue(plugins.plugin_install_plan("treesitter-js-ts")["ok"])
            self.assertTrue(plugins.treesitter_recommendation(root)["recommended"])


# Known-good Vitest failure output (mirrors tests/test_language_packs.py) for the failure_parser hook.
_VITEST_OUTPUT = (
    " ❯ src/math.test.ts (1 test | 1 failed) 12ms\n"
    "   × add > adds two numbers 5ms\n"
    "\n"
    "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯\n"
    "\n"
    " FAIL  src/math.test.ts > add > adds two numbers\n"
    "AssertionError: expected 5 to be 4 // Object.is equality\n"
    " ❯ src/math.test.ts:8:19\n"
)


class JsTsRuntimeHookTest(unittest.TestCase):
    """v1-J: the JS/TS built-in pack proves the plugin contract — its existing assimilation, test
    detection, and failure parsing run through the runtime hooks (architecture / test_detector /
    failure_parser / repro_drafter), loaded lazily and only for JS/TS repos. Regex fallback + all
    current behavior preserved; nothing is installed."""

    RT = "openfde.plugins_runtime.js_ts"
    _TS = "openfde.language_packs.js_ts_treesitter.available"

    def setUp(self):
        sys.modules.pop(self.RT, None)
        plugins._RUNTIME_CACHE.clear()

    def tearDown(self):
        plugins._RUNTIME_CACHE.clear()

    def _js_repo(self):
        return _repo({"package.json": json.dumps({"name": "x", "scripts": {"test": "vitest"}}),
                      "src/app.ts": "export function f() { return 1 }\n"})

    def _arch_runtime(self, root):
        provs = plugins.runtime_for_capability(root, "architecture")
        self.assertEqual([p["id"] for p in provs], ["js_ts"])
        return plugins.runtime_hook(provs[0]["runtime"], "architecture")

    def test_list_plugins_does_not_import_js_ts_runtime(self):
        import subprocess
        d, root = self._js_repo()
        with d:
            code = ("import sys, json, openfde.plugins as p;"
                    f"p.list_plugins({json.dumps(str(root))});"
                    "print('openfde.plugins_runtime.js_ts' in sys.modules,"
                    " 'openfde.architect' in sys.modules)")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False False", out.stderr)

    def test_js_ts_builtin_declares_runtime_metadata_only(self):
        d, root = self._js_repo()
        with d:
            row = next(r for r in plugins.list_plugins(root) if r["id"] == "js_ts")
        self.assertTrue(row["hasRuntime"])
        self.assertEqual(set(row["capabilities"]),
                         {"architecture", "test_detector", "failure_parser", "repro_drafter"})

    def test_runtime_activates_only_for_js_ts_repos(self):
        d, root = self._js_repo()
        with d:
            self.assertEqual([p["id"] for p in plugins.runtime_for_capability(root, "architecture")],
                             ["js_ts"])
        d2, pyroot = _repo({"main.py": "def f():\n    return 1\n"})
        with d2:
            self.assertEqual(plugins.runtime_for_capability(pyroot, "architecture"), [])
            self.assertIsNone(plugins.load_plugin_runtime("js_ts", pyroot))   # js_ts not detected

    def test_architecture_hook_matches_analyzer_shape(self):
        from openfde import architect
        d, root = self._js_repo()
        with d:
            hook = self._arch_runtime(root)
            via_hook = hook(root)
            via_core = architect.analyze_repo(root)
        self.assertEqual(set(via_hook), set(via_core))
        self.assertTrue({"modules", "files", "functions", "edges",
                         "flows", "fileEdges", "warnings"} <= set(via_hook))
        self.assertIn("f", {fn["name"] for fn in via_hook["functions"]})

    def test_missing_treesitter_falls_back_to_regex(self):
        d, root = self._js_repo()
        with d, mock.patch(self._TS, return_value=False):
            g = self._arch_runtime(root)(root)
        self.assertIn("f", {fn["name"] for fn in g["functions"]})        # regex still extracts
        self.assertTrue(any("regex fallback" in w for w in g["warnings"]))

    @unittest.skipUnless(__import__("openfde.language_packs.js_ts_treesitter",
                                    fromlist=["available"]).available(),
                         "tree-sitter not installed")
    def test_treesitter_preferred_when_available(self):
        d, root = self._js_repo()
        with d:
            g = self._arch_runtime(root)(root)
        self.assertTrue(any("tree-sitter" in w for w in g["warnings"]))

    def test_test_detector_hook_finds_scripts(self):
        d, root = self._js_repo()
        with d:
            rt = plugins.load_plugin_runtime("js_ts", root)
            checks = plugins.runtime_hook(rt, "test_detector")(root)
        self.assertTrue(checks)
        self.assertTrue(any("test" in " ".join(c["command"]) for c in checks))

    def test_failure_parser_hook_parses_vitest(self):
        d, root = self._js_repo()
        with d:
            rt = plugins.load_plugin_runtime("js_ts", root)
            locs = plugins.runtime_hook(rt, "failure_parser")(_VITEST_OUTPUT, root)
        self.assertEqual(len(locs), 1)
        self.assertEqual(locs[0]["file"], "src/math.test.ts")
        self.assertEqual(locs[0]["line"], 8)
        self.assertEqual(locs[0]["test"], "adds two numbers")

    def test_repro_drafter_hook_is_an_explicit_deferred_seam(self):
        d, root = self._js_repo()
        with d:
            rt = plugins.load_plugin_runtime("js_ts", root)
            out = plugins.runtime_hook(rt, "repro_drafter")(root)
        self.assertIn("context", out)
        self.assertIn("deferred", out["drafting"])                       # honest: drafting not built

    def test_bad_runtime_pointer_logs_skips_not_crash(self):
        # Point the js_ts builtin runtime at a missing module → load is logged + None, listing is fine.
        bad = {**plugins._LANGUAGE_PACK_META,
               "js_ts": {**plugins._LANGUAGE_PACK_META["js_ts"],
                         "runtime": {"module": "openfde._no_such_rt_module", "factory": "make_runtime"}}}
        d, root = self._js_repo()
        with d, mock.patch.object(plugins, "_LANGUAGE_PACK_META", bad):
            plugins._RUNTIME_CACHE.clear()
            with self.assertLogs("openfde.plugins", level="WARNING"):
                self.assertIsNone(plugins.load_plugin_runtime("js_ts", root))
            rows = {r["id"] for r in plugins.list_plugins(root)}          # listing never crashes
            self.assertIn("js_ts", rows)


class JsTsConsumptionTest(unittest.TestCase):
    """v1-K: core product paths PREFER the JS/TS plugin runtime hooks (architecture / test_detector /
    failure_parser) with safe fallback to the in-core impl. Python paths are unchanged; bad hooks log +
    fall back; no recursion (the architecture hook delegates to ``_analyze_repo_core``)."""

    def setUp(self):
        plugins._RUNTIME_CACHE.clear()

    def tearDown(self):
        plugins._RUNTIME_CACHE.clear()

    def _js_repo(self):
        return _repo({"package.json": json.dumps({"name": "x", "scripts": {"test": "vitest"}}),
                      "src/app.ts": "export function f() { return 1 }\n"})

    def _py_repo(self):
        return _repo({"pkg/calc.py": "def add(a, b):\n    return a + b\n",
                      "tests/test_calc.py": "def test_add():\n    assert add(1, 2) == 3\n"})

    def test_architecture_product_path_uses_runtime_hook(self):
        d, root = self._js_repo()
        sentinel = {"modules": ["S"], "files": [], "functions": [], "edges": [],
                    "flows": [], "fileEdges": [], "warnings": []}
        with d, mock.patch.object(plugins, "run_capability_hook", return_value=sentinel) as m:
            g = architect.analyze_repo(root)
        self.assertIs(g, sentinel)                              # used the hook's result
        self.assertEqual(m.call_args.args[1], "architecture")   # consulted the architecture capability

    def test_architecture_falls_back_to_core_when_no_hook(self):
        d, root = self._js_repo()
        with d, mock.patch.object(plugins, "run_capability_hook", return_value=plugins.NO_HOOK):
            g = architect.analyze_repo(root)
        self.assertIn("f", {fn["name"] for fn in g["functions"]})   # in-core analysis ran
        self.assertTrue({"modules", "files", "functions", "edges",
                         "flows", "fileEdges", "warnings"} <= set(g))

    def test_no_recursion_in_architecture_runtime(self):
        # On a real JS/TS repo the dispatcher → hook → _analyze_repo_core path must call the in-core
        # analyzer EXACTLY once (a recursion would re-enter the dispatcher and blow the count / stack).
        d, root = self._js_repo()
        with d, mock.patch("openfde.architect._analyze_repo_core",
                           wraps=architect._analyze_repo_core) as core:
            g = architect.analyze_repo(root)
        self.assertEqual(core.call_count, 1)
        self.assertIn("f", {fn["name"] for fn in g["functions"]})

    def test_test_detection_product_path_uses_hook(self):
        d, root = self._js_repo()
        with d, mock.patch.object(plugins, "run_capability_hook",
                                  wraps=plugins.run_capability_hook) as m:
            checks = verify._discover_via_packs(root)
        self.assertIn("test_detector", [c.args[1] for c in m.call_args_list])
        self.assertTrue(any("test" in " ".join(c["command"]) for c in checks))

    def test_failure_parsing_product_path_uses_hook(self):
        d, root = self._js_repo()
        with d, mock.patch.object(plugins, "run_capability_hook",
                                  wraps=plugins.run_capability_hook) as m:
            locs = verify._parse_via_packs(_VITEST_OUTPUT, root)
        self.assertIn("failure_parser", [c.args[1] for c in m.call_args_list])
        self.assertEqual(locs[0]["file"], "src/math.test.ts")
        self.assertEqual(locs[0]["line"], 8)

    def test_bad_architecture_hook_logs_and_falls_back(self):
        d, root = self._js_repo()
        rt = {"architecture": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))}
        prov = [{"id": "js_ts", "capability": "architecture", "runtime": rt}]
        with d, mock.patch.object(plugins, "runtime_for_capability", return_value=prov):
            with self.assertLogs("openfde.plugins", level="WARNING"):
                g = architect.analyze_repo(root)
        self.assertIn("f", {fn["name"] for fn in g["functions"]})   # fell back to in-core, no crash

    def test_python_architecture_unchanged(self):
        d, root = self._py_repo()
        with d:
            dispatched = {fn["name"] for fn in architect.analyze_repo(root)["functions"]}
            core = {fn["name"] for fn in architect._analyze_repo_core(root)["functions"]}
        self.assertEqual(dispatched, core)            # no architecture provider for Python → in-core
        self.assertIn("add", dispatched)

    def test_python_test_and_failure_paths_unchanged(self):
        # Python has no test_detector/failure_parser provider, so the per-pack seam is a PASSTHROUGH
        # to the pack's own impl — byte-identical to pre-v1-K behavior.
        d, root = self._py_repo()
        with d:
            self.assertTrue(verify._discover_via_packs(root))       # python check discovered (in-core)
            py = get_language_packs(root)[0]
            self.assertEqual(verify._pack_checks(py, root),
                             [s.as_dict() for s in py.discover_checks(root)])
            out = "tests/test_calc.py:3: in test_add\n    assert add(1, 2) == 3\nE   AssertionError\n"
            self.assertEqual(verify._pack_failures(py, out, root),
                             [loc.as_dict() for loc in py.parse_failures(out, root)])

    # ── v1-K hardening: malformed hook RETURN values fall back (not just exceptions) ──
    def _bad_provider(self, capability, value):
        rt = {capability: lambda *a, **k: value}
        return [{"id": "js_ts", "capability": capability, "runtime": rt}]

    def test_malformed_test_detector_output_logs_and_falls_back(self):
        d, root = self._js_repo()
        with d, mock.patch.object(plugins, "runtime_for_capability",
                                  return_value=self._bad_provider("test_detector", "bad-output")):
            with self.assertLogs("openfde.verify", level="WARNING"):
                checks = verify._discover_via_packs(root)
        self.assertTrue(any("test" in " ".join(c["command"]) for c in checks))   # in-core fallback

    def test_malformed_failure_parser_output_logs_and_falls_back(self):
        d, root = self._js_repo()
        with d, mock.patch.object(plugins, "runtime_for_capability",
                                  return_value=self._bad_provider("failure_parser", "bad-output")):
            with self.assertLogs("openfde.verify", level="WARNING"):
                locs = verify._parse_via_packs(_VITEST_OUTPUT, root)
        self.assertTrue(locs and locs[0]["file"] == "src/math.test.ts")          # in-core fallback

    def test_valid_hook_output_wins_over_fallback(self):
        d, root = self._js_repo()
        good = [{"id": "custom", "label": "X", "command": ["echo", "hi"], "cwd": "", "required": True}]
        with d, mock.patch.object(plugins, "runtime_for_capability",
                                  return_value=self._bad_provider("test_detector", good)):
            checks = verify._discover_via_packs(root)
        by_id = {c["id"]: c for c in checks}
        self.assertIn("custom", by_id)                       # valid hook output is used…
        self.assertEqual(by_id["custom"]["cwd"], "")         # …and extra fields are preserved

    def test_empty_hook_output_is_valid_no_fallback(self):
        d, root = self._js_repo()
        with d, mock.patch.object(plugins, "runtime_for_capability",
                                  return_value=self._bad_provider("test_detector", [])):
            checks = verify._discover_via_packs(root)
        self.assertEqual(checks, [])                         # empty is valid → honored, NOT fallback


class ReferenceExternalPluginTest(unittest.TestCase):
    """v1-L: prove the lightweight external plug-and-play story end to end with a real, on-disk
    reference package (``tests/fixtures/ofde_sample_plugin``) — put on sys.path + reached via a
    monkeypatched entry point, simulating a pip-installed package. Covers discovery, metadata-only
    listing, lazy runtime, probe-gated activation, safe skip of malformed plugins, the local-manifest
    no-runtime rule, and safe handling of hook output by the consuming seam."""

    PKG = "ofde_sample_plugin"
    RT = "ofde_sample_plugin.runtime"
    FIXTURES = str(Path(__file__).resolve().parent / "fixtures")

    def setUp(self):
        if self.FIXTURES not in sys.path:
            sys.path.insert(0, self.FIXTURES)
        plugins._RUNTIME_CACHE.clear()

    def tearDown(self):
        for m in [m for m in sys.modules if m == self.PKG or m.startswith(self.PKG + ".")]:
            sys.modules.pop(m, None)
        try:
            sys.path.remove(self.FIXTURES)
        except ValueError:
            pass
        plugins._RUNTIME_CACHE.clear()

    def _patch_eps(self, value="ofde_sample_plugin.plugin:manifest", target_group="openfde.domain_packs"):
        def fake_entry_points(group=None):   # param MUST be 'group' to match entry_points(group=…)
            return [importlib.metadata.EntryPoint("sample", value, target_group)] if group == target_group else []
        return mock.patch("importlib.metadata.entry_points", fake_entry_points)

    def _marker_repo(self):
        return _repo({"app.samplemarker": "x\n", "README.md": "# demo\n"})

    def test_discovery_sees_the_manifest(self):
        with self._patch_eps():
            rows = {r["id"]: r for r in plugins.list_plugins()}
        self.assertIn("sample-pack", rows)
        self.assertEqual(rows["sample-pack"]["source"], "external")
        self.assertEqual(rows["sample-pack"]["kind"], "domain_pack")
        self.assertIn("domain_summary", rows["sample-pack"]["capabilities"])
        self.assertTrue(rows["sample-pack"]["hasRuntime"])

    def test_listing_is_metadata_only_no_runtime_import(self):
        self.assertNotIn(self.RT, sys.modules)
        d, root = self._marker_repo()
        with d, self._patch_eps():
            rows = {r["id"] for r in plugins.list_plugins(root)}
        self.assertIn("sample-pack", rows)
        self.assertNotIn(self.RT, sys.modules, "listing must not import the plugin runtime")

    def test_runtime_loads_only_on_capability_request(self):
        d, root = self._marker_repo()
        with d, self._patch_eps():
            self.assertNotIn(self.RT, sys.modules)
            provs = plugins.runtime_for_capability(root, "domain_summary")
            self.assertIn(self.RT, sys.modules)                 # imported NOW, on request
            self.assertEqual([p["id"] for p in provs], ["sample-pack"])
            hook = plugins.runtime_hook(provs[0]["runtime"], "domain_summary")
            self.assertEqual(hook(root)["provider"], "sample-pack")

    def test_activation_requires_probe_match(self):
        d, root = _repo({"src/app.py": "x = 1\n"})              # no *.samplemarker → not detected
        with d, self._patch_eps():
            self.assertEqual(plugins.runtime_for_capability(root, "domain_summary"), [])
            self.assertIsNone(plugins.load_plugin_runtime("sample-pack", root))
        self.assertNotIn(self.RT, sys.modules)

    def test_malformed_external_plugin_skipped_safely(self):
        # entry point points at a missing attr → load() fails → logged + skipped; listing still works
        with self._patch_eps(value="ofde_sample_plugin.plugin:does_not_exist"):
            with self.assertLogs("openfde.plugins", level="WARNING"):
                rows = {r["id"] for r in plugins.list_plugins()}
        self.assertIn("python", rows)
        self.assertNotIn("sample-pack", rows)

    def test_local_manifest_cannot_import_runtime(self):
        # SECURITY: a repo-local manifest with the same runtime pointer must NOT load code.
        manifest = {"id": "sample-pack", "kind": "domain_pack", "status": "available",
                    "capabilities": ["domain_summary"],
                    "runtime": {"module": self.RT, "factory": "make_runtime"}}
        d, root = _repo({".openfde/plugins/sample-pack.json": json.dumps(manifest)})
        with d:                                                 # note: NO entry-point patch
            row = next(r for r in plugins.list_plugins(root) if r["id"] == "sample-pack")
            self.assertEqual(row["source"], "local")
            self.assertFalse(row["hasRuntime"])                # repo-declared runtime dropped
            self.assertIsNone(plugins.load_plugin_runtime("sample-pack", root))
        self.assertNotIn(self.RT, sys.modules)                 # never imported

    def test_consuming_seam_safely_handles_hook_output(self):
        d, root = self._marker_repo()
        with d, self._patch_eps():
            r = plugins.run_capability_hook(root, "domain_summary", lambda h: h(root))
            self.assertEqual(r["provider"], "sample-pack")     # valid output flows through the seam
        # a throwing external hook → NO_HOOK (logged), never raised through the consuming path
        boom = {"domain_summary": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))}
        bad = [{"id": "sample-pack", "capability": "domain_summary", "runtime": boom}]
        with mock.patch.object(plugins, "runtime_for_capability", return_value=bad):
            with self.assertLogs("openfde.plugins", level="WARNING"):
                r2 = plugins.run_capability_hook(root, "domain_summary", lambda h: h(root))
        self.assertIs(r2, plugins.NO_HOOK)


class WebxrExternalPluginTest(unittest.TestCase):
    """v1-M: WebXR as an EXTERNAL pack skeleton (``tests/fixtures/openfde_webxr_plugin``). Proves WebXR
    can ship outside core: discovered via entry points, runtime lazy, de-duped with the built-in
    suggestion / local manifest to ONE row, and ``/api/plugins/webxr/summary`` unchanged. WebXR stays
    available WITHOUT the package; local manifests still can't declare runtime; a bad runtime falls back."""

    PKG = "openfde_webxr_plugin"
    RT = "openfde_webxr_plugin.runtime"
    FIXTURES = str(Path(__file__).resolve().parent / "fixtures")

    def setUp(self):
        if self.FIXTURES not in sys.path:
            sys.path.insert(0, self.FIXTURES)
        plugins._RUNTIME_CACHE.clear()

    def tearDown(self):
        for m in [m for m in sys.modules if m == self.PKG or m.startswith(self.PKG + ".")]:
            sys.modules.pop(m, None)
        try:
            sys.path.remove(self.FIXTURES)
        except ValueError:
            pass
        plugins._RUNTIME_CACHE.clear()

    def _patch_eps(self, value="openfde_webxr_plugin.plugin:manifest", target_group="openfde.domain_packs"):
        def fake_entry_points(group=None):
            return [importlib.metadata.EntryPoint("webxr", value, target_group)] if group == target_group else []
        return mock.patch("importlib.metadata.entry_points", fake_entry_points)

    def _xr_repo(self):
        return _repo({"package.json": json.dumps({"dependencies": {"three": "^0.160.0"}})})

    def test_external_webxr_discovered_through_entry_points(self):
        with self._patch_eps():
            rows = {r["id"]: r for r in plugins.list_plugins()}
        self.assertIn("webxr", rows)
        self.assertEqual(rows["webxr"]["source"], "external")
        self.assertEqual(rows["webxr"]["kind"], "domain_pack")
        self.assertIn("domain_summary", rows["webxr"]["capabilities"])
        self.assertTrue(rows["webxr"]["hasRuntime"])

    def test_listing_does_not_import_external_webxr_runtime(self):
        self.assertNotIn(self.RT, sys.modules)
        d, root = self._xr_repo()
        with d, self._patch_eps():
            plugins.list_plugins(root)
        self.assertNotIn(self.RT, sys.modules, "listing must not import the external WebXR runtime")

    def test_runtime_loads_only_on_domain_summary(self):
        d, root = self._xr_repo()
        with d, self._patch_eps():
            self.assertNotIn(self.RT, sys.modules)
            provs = plugins.runtime_for_capability(root, "domain_summary")
            self.assertIn(self.RT, sys.modules)                # imported NOW, on request
            self.assertEqual([p["id"] for p in provs], ["webxr"])

    def test_dedupes_with_builtin_suggestion(self):
        d, root = self._xr_repo()
        with d, self._patch_eps():
            webxr_rows = [r for r in plugins.list_plugins(root) if r["id"] == "webxr"]
        self.assertEqual(len(webxr_rows), 1)
        self.assertEqual(webxr_rows[0]["source"], "external")  # external supersedes the suggestion

    def test_dedupes_with_local_manifest(self):
        local = {"id": "webxr", "kind": "domain_pack", "status": "available",
                 "capabilities": ["domain_summary"]}
        d, root = _repo({".openfde/plugins/webxr.json": json.dumps(local),
                         "package.json": json.dumps({"dependencies": {"three": "^0.160.0"}})})
        with d, self._patch_eps():
            webxr_rows = [r for r in plugins.list_plugins(root) if r["id"] == "webxr"]
        self.assertEqual(len(webxr_rows), 1)
        self.assertEqual(webxr_rows[0]["source"], "local")     # local supersedes external

    def test_webxr_summary_shape_unchanged_with_external(self):
        d, root = self._xr_repo()
        with d, self._patch_eps():
            via_external = plugins.resolve_webxr_summary(root)
            core = plugins.webxr_summary(root)
        self.assertEqual(set(via_external), set(core))
        for k in ("detected", "entrypoints", "assets", "frameworks", "markers", "fileBadges", "warnings"):
            self.assertIn(k, via_external)

    def test_webxr_available_without_external_package(self):
        # no entry-point patch → built-in suggestion + core runtime still serve WebXR
        d, root = self._xr_repo()
        with d:
            row = next(r for r in plugins.list_plugins(root) if r["id"] == "webxr")
            self.assertEqual(row["source"], "suggested")
            self.assertTrue(row["detected"])
            self.assertIn("detected", plugins.resolve_webxr_summary(root))

    def test_local_manifest_cannot_declare_runtime(self):
        local = {"id": "webxr", "kind": "domain_pack", "status": "available",
                 "capabilities": ["domain_summary"],
                 "runtime": {"module": self.RT, "factory": "make_runtime"}}
        d, root = _repo({".openfde/plugins/webxr.json": json.dumps(local)})
        with d:                                                # no entry-point patch
            row = next(r for r in plugins.list_plugins(root) if r["id"] == "webxr")
            self.assertEqual(row["source"], "local")
            self.assertFalse(row["hasRuntime"])                # repo-declared runtime dropped
            self.assertIsNone(plugins.load_plugin_runtime("webxr", root))
        self.assertNotIn(self.RT, sys.modules)

    def test_bad_external_webxr_runtime_falls_back(self):
        d, root = self._xr_repo()
        boom = {"domain_summary": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))}
        prov = [{"id": "webxr", "capability": "domain_summary", "runtime": boom}]
        with d, mock.patch.object(plugins, "runtime_for_capability", return_value=prov):
            with self.assertLogs("openfde.plugins", level="WARNING"):
                s = plugins.resolve_webxr_summary(root)
        self.assertTrue(s["detected"])                         # fell back to core webxr_summary


class PackagingTest(unittest.TestCase):
    """Release-readiness: version metadata aligned, and wheel package discovery includes subpackages."""

    _ROOT = Path(__file__).resolve().parent.parent

    def test_version_metadata_aligned(self):
        # Assert all version SOURCES agree with openfde.__version__ — no hardcoded value, so a bump
        # only needs the four sources updated (this test won't need touching).
        import openfde
        v = openfde.__version__
        self.assertRegex(v, r"^\d+\.\d+\.\d+")
        self.assertIn(f'version = "{v}"', (self._ROOT / "pyproject.toml").read_text())
        self.assertIn(f'version="{v}"', (self._ROOT / "setup.py").read_text())
        self.assertIn(f'"version": "{v}"', (self._ROOT / "frontend" / "package.json").read_text())

    def test_package_discovery_includes_subpackages(self):
        text = (self._ROOT / "pyproject.toml").read_text()
        self.assertIn("[tool.setuptools.packages.find]", text)
        self.assertIn('"openfde*"', text)


if __name__ == "__main__":
    unittest.main()
