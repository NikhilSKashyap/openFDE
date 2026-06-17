"""
openfde/plugins.py — internal capability-provider registry (Plugin Registry v1-A/B/C/G/H).

OpenFDE's capabilities — language packs, domain packs, verify adapters, agent
providers, layout engines, integrations — should be DESCRIBABLE without importing
their heavy code. This module is the manifest/probe layer: it lists every provider as
lightweight METADATA and computes per-repo activation by probing cheap markers.

Four sources, one ``PluginSpec`` contract (the ``source`` field):
  • **builtin**   (v1-A) — the built-in language packs (Python, JS/TS), wired from the
    existing registry; their probe is each pack's own ``detects(root)``, so activation
    stays the single source of truth.
  • **suggested** (v1-B Lite) — deterministic domain-pack SUGGESTIONS (WebXR) surfaced
    when cheap repo markers match; never active, never loaded.
  • **local**     (v1-C) — read-only manifests from ``.openfde/plugins/*.json`` in the
    watched repo, so a provider can exist outside the hardcoded built-ins.
  • **external**  (v1-G) — manifests contributed by INSTALLED packages via Python entry
    points (``openfde.plugins`` / ``openfde.language_packs`` / ``openfde.domain_packs``),
    so a language/domain pack can ship outside core. Discovery loads only the lightweight
    manifest provider — never the heavy analyzer — and a bad external plugin is logged +
    skipped, never crashing the listing.

Two phases. **Discovery** (``list_plugins`` / ``/api/plugins``) is **metadata + probe ONLY** — it
lists providers and probes cheap markers, importing NO plugin code. **Activation** (v1-H,
``active_plugins`` / ``load_plugin_runtime`` / ``runtime_for_capability``) answers "what code can I
use for THIS repo right now?" — it imports a plugin's runtime LAZILY, only when the plugin's probe
matches the repo AND a caller asks for a capability. A runtime factory returns hooks
(:data:`RUNTIME_HOOKS`: architecture / test_detector / failure_parser / repro_drafter /
domain_summary); a bad import/factory is logged + skipped, never raised, and results cache per repo.

Honest boundary: NO install/download, NO network, NO subprocess. Only an **external** (pip-installed,
already-trusted) plugin may declare a ``runtime`` — a repo-local manifest is untrusted, so its runtime
is dropped (opening a repo never imports code). Built-in language packs are **not** migrated onto the
runtime contract yet (their analysis still lives in core), and an install/download MARKETPLACE is still
deferred. Every source flows through the one ``PluginSpec`` contract with no special case.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
import re

logger = logging.getLogger("openfde.plugins")

# The kinds a capability provider can be (the manifest contract). v1-A populates
# language_pack from the built-in registry; the rest are reserved for v1-B+.
PLUGIN_KINDS = ("language_pack", "domain_pack", "verify_adapter",
                "agent_provider", "layout_engine", "integration")

# Install/availability state (distinct from per-repo activation):
#   builtin   — ships with core (always present)
#   suggested — a known optional pack whose markers ARE present in this repo, shown
#               so the architecture is visible. v1-B Lite is read-only: NOT installed.
#   available — an external plugin is installed/loadable (v1-C+)
#   missing   — a known optional pack whose markers are NOT in this repo (or, later,
#               a suggested pack the user could install)
#   disabled  — present but turned off by the user
PLUGIN_STATUSES = ("builtin", "suggested", "available", "missing", "disabled")


@dataclass(frozen=True)
class PluginSpec:
    """One capability provider, described as metadata + a cheap activation probe.

    ``probe(root) -> bool`` is the per-repo activation check (built-ins only); it must
    stay cheap and must NOT import heavy assimilation code eagerly. ``manifest(root)``
    serializes the spec for the API, computing ``active`` from the probe.
    """
    id: str
    kind: str
    displayName: str
    activatesOn: str                  # human summary of the detection markers
    provides: tuple = ()
    status: str = "builtin"
    probe: object = field(default=None, repr=False)   # callable(root) -> bool | None
    source: str = "builtin"            # builtin | suggested | local | external
    version: str = ""                  # provider version ("" for built-ins)
    description: str = ""              # optional one-line description
    capabilities: tuple = ()           # optional richer capability list (alongside provides)
    runtime: object = field(default=None, repr=False)  # {module, factory} | None — LAZY (v1-H);
    #   a metadata pointer to runtime code, NEVER imported during listing. Only external
    #   (entry-point) plugins may carry one; repo-local manifests never load code.

    def detect(self, root) -> bool:
        """Raw activation probe for ``root`` — does the repo show this provider's
        markers? Cheap, marker-only; never raises (a bad probe logs and is False)."""
        if root is None or not callable(self.probe):
            return False
        try:
            return bool(self.probe(root))
        except Exception as exc:  # noqa: BLE001 — a probe must never break the listing
            logger.warning("plugin probe failed for %s: %s", self.id, exc)
            return False

    def manifest(self, root=None) -> dict:
        """Serialize for the API. ``detected`` is the raw probe result for ``root``;
        ``status`` and ``active`` are derived from it per the provider's base status:
          • ``builtin``   → status stays 'builtin'; ``active`` == detected (a built-in
            language pack provides for this repo when its language is present).
          • ``suggested`` → status is 'suggested' when detected, else 'missing'; and
            ``active`` is always False — v1-B Lite is read-only, a suggestion is
            metadata only, nothing is loaded or installed.
        """
        detected = self.detect(root)
        if self.status == "suggested":
            status, active = ("suggested" if detected else "missing"), False
        else:                                   # builtin (and future installed kinds)
            status = self.status
            active = detected if self.status == "builtin" else False
        return {
            "id": self.id,
            "kind": self.kind,
            "displayName": self.displayName,
            "status": status,
            "source": self.source,
            "version": self.version,
            "description": self.description,
            "activatesOn": self.activatesOn,
            "provides": list(self.provides),
            "capabilities": list(self.capabilities),
            "active": active,
            "detected": detected,
            "hasRuntime": bool(self.runtime),   # declares a lazy runtime (v1-H); still NOT loaded here
        }


# Per-pack display metadata (keyed by pack.name). The packs supply detection +
# file globs; this adds the human-facing label, marker summary, and capability list.
_LANGUAGE_PACK_META = {
    "python": {
        "displayName": "Python",
        "activatesOn": "*.py files",
        "provides": ("architecture", "verify:pytest", "failure-lens", "repro-drafting"),
    },
    "js_ts": {
        "displayName": "JavaScript / TypeScript",
        "activatesOn": "package.json, or *.ts/*.tsx/*.js/*.jsx/*.mjs/*.cjs/*.mts/*.cts",
        "provides": ("architecture", "html-entrypoints", "verify:vitest",
                     "verify:jest", "verify:playwright", "failure-lens"),
        # v1-J: the JS/TS pack proves the runtime contract — its EXISTING assimilation, test
        # detection, and failure parsing are exposed as TRUSTED built-in runtime hooks, loaded
        # lazily only when a JS/TS repo asks for a capability. No new intelligence; regex fallback
        # and all current behavior are preserved.
        "capabilities": ("architecture", "test_detector", "failure_parser", "repro_drafter"),
        "runtime": {"module": "openfde.plugins_runtime.js_ts", "factory": "make_runtime"},
    },
}


def _language_pack_specs() -> list:
    """Built-in language packs, wired from the existing registry into PluginSpecs.

    The probe is each pack's own ``detects`` — so activation stays the single source
    of truth (``get_language_packs(root)``), just surfaced as metadata. Lazy import
    keeps ``import openfde.plugins`` free of heavy assimilation code."""
    from openfde.language_packs.registry import all_language_packs
    specs = []
    for pack in all_language_packs():
        meta = _LANGUAGE_PACK_META.get(pack.name, {})
        specs.append(PluginSpec(
            id=pack.name,
            kind="language_pack",
            displayName=meta.get("displayName", pack.name),
            activatesOn=meta.get("activatesOn", ", ".join(pack.file_globs)),
            provides=tuple(meta.get("provides", ())),
            status="builtin",
            source="builtin",
            probe=pack.detects,
            capabilities=tuple(meta.get("capabilities", ())),   # v1-J: runtime hooks (js_ts)
            runtime=meta.get("runtime"),                        # trusted built-in pointer | None
        ))
    return specs


def builtin_specs() -> list:
    """Every built-in capability provider as ``PluginSpec``. v1-A: language packs.
    Future kinds (domain packs, verify adapters, agent providers, layout engines,
    integrations) register here too — built-ins through the same contract as
    external plugins, so tests prove there is no special case."""
    return _language_pack_specs()


# ── WebXR / immersive-web domain pack (deterministic SUGGESTION, v1-B Lite) ───────
#
# A domain pack that would ride on top of JS/TS for immersive-web repos. v1-B Lite
# does NOT ship, load, or install it — it only SUGGESTS it when the watched repo
# shows cheap markers, so the plugin architecture is visible (and credible) without
# any install/download path. The probe below is marker-only and bounded.
_XR_DEP_HINTS = ("three", "@babylonjs/core", "babylonjs", "aframe",
                 "@react-three/fiber", "@react-three/drei", "webxr-polyfill")
_XR_ASSET_EXTS = (".glb", ".gltf")
_XR_API_MARKERS = ("navigator.xr", "xrframe", "requestsession", "xrsession",
                   "immersive-vr", "immersive-ar", "xr-standard")
_XR_HTML_EXTS = (".html", ".htm")
_XR_TEXT_EXTS = (".html", ".htm", ".js", ".mjs", ".jsx", ".ts", ".tsx")
_XR_SCAN_MAX_FILES = 300        # bound the content reads (markers, not assimilation)
_XR_SCAN_MAX_BYTES = 200_000    # skip large/minified bundles
_XR_WALK_MAX = 20_000           # hard backstop on directory entries walked
# Slice-1 enrichment: shaders + 3D-specific textures as readable asset nodes (NOT generic png/jpg, to
# avoid a hairball), and per-file Three / R3F / Scene markers found in the SAME bounded text scan.
_XR_SHADER_EXTS = (".glsl", ".vert", ".frag", ".wgsl")
_XR_TEXTURE_EXTS = (".ktx", ".ktx2", ".basis", ".hdr", ".exr")
_XR_FILE_MARKERS = (
    ("Three", ("from 'three'", 'from "three"', "import * as three", "require('three')", 'require("three")')),
    ("R3F",   ("@react-three/fiber", "@react-three/drei")),
    ("Scene", ("new scene(", "perspectivecamera", "new three.scene")),
)


def _detect_webxr(root) -> bool:
    """Cheap, bounded WebXR/3D markers — any one is enough to SUGGEST the pack:
      • a dependency hint (Three / Babylon / A-Frame / R3F) in package.json,
      • a 3D asset (``.glb`` / ``.gltf``),
      • an HTML entry plus an XR API call (``navigator.xr`` / ``XRFrame`` /
        ``requestSession`` / ``immersive-*``) in a bounded content scan.
    Marker-only: reads at most a few hundred small HTML/JS files, walks at most
    ``_XR_WALK_MAX`` entries (pruning vendor/build dirs), and NEVER assimilates or
    imports the architect."""
    import os
    from pathlib import Path

    try:
        from openfde.language_packs.js_ts_pack import _read_package_json, _SKIP_DIRS
    except Exception:  # noqa: BLE001 — never let a probe break the listing
        return False

    root = Path(root)
    # (a) dependency hint — one file read, the cheapest and strongest signal.
    pkg = _read_package_json(root)
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if any(h in deps for h in _XR_DEP_HINTS):
        return True

    # (b) bounded walk: a 3D asset, or an HTML entry + an XR API marker (tracked
    # independently so order of discovery doesn't matter).
    has_html = has_xr_api = False
    walked = scanned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                walked += 1
                if walked > _XR_WALK_MAX:
                    return False                        # generous backstop on size
                low = fn.lower()
                if low.endswith(_XR_ASSET_EXTS):
                    return True                         # a 3D asset is signal enough
                if low.endswith(_XR_HTML_EXTS):
                    has_html = True
                if (not has_xr_api and scanned < _XR_SCAN_MAX_FILES
                        and low.endswith(_XR_TEXT_EXTS)):
                    p = Path(dirpath) / fn
                    try:
                        if p.stat().st_size <= _XR_SCAN_MAX_BYTES:
                            scanned += 1
                            text = p.read_text(encoding="utf-8", errors="ignore").lower()
                            if any(m in text for m in _XR_API_MARKERS):
                                has_xr_api = True
                    except OSError:
                        pass
                if has_html and has_xr_api:
                    return True
    except OSError:
        return False
    return False


# Framework deps → friendly labels for the WebXR detail summary (v1-E).
_XR_FRAMEWORK_LABELS = {
    "three": "Three.js",
    "@react-three/fiber": "React Three Fiber",
    "@react-three/drei": "React Three Fiber (drei)",
    "@babylonjs/core": "Babylon.js",
    "babylonjs": "Babylon.js",
    "aframe": "A-Frame",
    "webxr-polyfill": "WebXR Polyfill",
}
_WEBXR_SUMMARY_CAP = 20         # cap each list so the detail payload stays small


def webxr_summary(root) -> dict:
    """Bounded, READ-ONLY WebXR architecture hints for ``/api/plugins/webxr/summary`` (v1-E):
    ``{detected, entrypoints, assets, frameworks, markers, warnings}``.

    Metadata + architecture enrichment ONLY — there is NO WebXR runtime/test lens, and no imports,
    subprocess, or network. One bounded walk (vendor/build dirs pruned, capped files + bytes), each
    list capped at ``_WEBXR_SUMMARY_CAP``; ``warnings`` ALWAYS carries the honest boundary so the UI
    can never imply a test lens.

      • frameworks  — Three / R3F / Babylon / A-Frame hints from package.json deps.
      • assets      — ``.glb`` / ``.gltf`` 3D assets (repo-relative paths).
      • entrypoints — HTML / JS / TS files that call an XR API (``navigator.xr`` / ``requestSession``
                      / ``XRFrame`` / ``immersive-*``) — the likely XR starters.
      • markers     — the distinct XR API markers actually found.
    """
    import os
    try:
        from openfde.language_packs.js_ts_pack import _read_package_json, _SKIP_DIRS
    except Exception:  # noqa: BLE001 — never let a scan failure break the endpoint
        return {"detected": False, "entrypoints": [], "assets": [], "shaders": [], "textures": [],
                "frameworks": [], "markers": [], "fileBadges": [], "assetGroups": [],
                "warnings": ["WebXR scan unavailable."]}

    root = Path(root)
    frameworks, assets, entrypoints = [], [], []
    shaders, textures = [], []
    three_files, r3f_files, scene_files = [], [], []
    seen_markers: set = set()
    truncated = False

    pkg = _read_package_json(root)
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    for dep, label in _XR_FRAMEWORK_LABELS.items():
        if dep in deps and label not in frameworks:
            frameworks.append(label)

    def _cap_add(bucket, rel):
        nonlocal truncated
        if rel in bucket:
            return
        if len(bucket) < _WEBXR_SUMMARY_CAP:
            bucket.append(rel)
        else:
            truncated = True

    walked = scanned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            if walked > _XR_WALK_MAX:
                truncated = True
                break
            rel_dir = os.path.relpath(dirpath, root)
            for fn in filenames:
                walked += 1
                low = fn.lower()
                rel = fn if rel_dir == "." else f"{rel_dir}/{fn}".replace(os.sep, "/")
                if low.endswith(_XR_ASSET_EXTS):
                    _cap_add(assets, rel)
                    continue
                if low.endswith(_XR_SHADER_EXTS):
                    _cap_add(shaders, rel)
                    continue
                if low.endswith(_XR_TEXTURE_EXTS):
                    _cap_add(textures, rel)
                    continue
                if low.endswith(_XR_TEXT_EXTS) and scanned < _XR_SCAN_MAX_FILES:
                    p = Path(dirpath) / fn
                    try:
                        if p.stat().st_size <= _XR_SCAN_MAX_BYTES:
                            scanned += 1
                            text = p.read_text(encoding="utf-8", errors="ignore").lower()
                            hits = [m for m in _XR_API_MARKERS if m in text]
                            if hits:
                                seen_markers.update(hits)
                                _cap_add(entrypoints, rel)
                            for label, needles in _XR_FILE_MARKERS:   # per-file framework / scene hints
                                if any(n in text for n in needles):
                                    _cap_add({"Three": three_files, "R3F": r3f_files,
                                              "Scene": scene_files}[label], rel)
                    except OSError:
                        pass
    except OSError:
        pass

    warnings = ["Architecture hints only — no WebXR runtime or test lens is installed."]
    if truncated:
        warnings.append("Scan bounded — results may be partial on a large repo.")

    # Canvas/Explorer annotation HOOK (Slice 1): a flat {path, kind, label} list the frontend matches
    # to files. Honest, repo-relative file hints from the scan above — NO extra walk, NO runtime/test
    # lens. A file may carry several badges (e.g. an XR-API file that also uses Three and a Scene).
    file_badges = (
        [{"path": p, "kind": "entrypoint", "label": "XR API"}   for p in entrypoints]
        + [{"path": p, "kind": "scene",     "label": "Scene"}   for p in scene_files]
        + [{"path": p, "kind": "framework", "label": "Three"}   for p in three_files]
        + [{"path": p, "kind": "framework", "label": "R3F"}     for p in r3f_files]
        + [{"path": p, "kind": "shader",    "label": "Shader"}  for p in shaders]
        + [{"path": p, "kind": "asset",     "label": "3D asset"} for p in assets]
    )
    # Assets grouped by TYPE so the canvas/details read as a few nodes, not a hairball of arrows.
    asset_groups = [g for g in (
        {"type": "3D model", "exts": list(_XR_ASSET_EXTS),   "paths": assets},
        {"type": "Shader",   "exts": list(_XR_SHADER_EXTS),  "paths": shaders},
        {"type": "Texture",  "exts": list(_XR_TEXTURE_EXTS), "paths": textures},
    ) if g["paths"]]
    return {
        "detected": bool(frameworks or assets or seen_markers),
        "entrypoints": entrypoints,
        "assets": assets,
        "shaders": shaders,
        "textures": textures,
        "frameworks": frameworks,
        "markers": sorted(seen_markers),
        "fileBadges": file_badges,
        "assetGroups": asset_groups,
        "warnings": warnings,
    }


def _suggested_specs() -> list:
    """Deterministic domain-pack SUGGESTIONS (v1-B Lite): metadata only, surfaced when the repo shows
    cheap markers. A suggestion's ``status`` resolves to 'suggested' when its probe matches, else
    'missing'.

    v1-H: WebXR is the FIRST suggestion to carry a built-in ``runtime`` — a TRUSTED, code-defined
    pointer (NOT a repo-declared one) to ``openfde.plugins_runtime.webxr`` — so its ``domain_summary``
    capability runs behind the activation hook. It is still never auto-loaded: the runtime module is
    imported only when the summary is actually requested for a WebXR-active repo."""
    return [PluginSpec(
        id="webxr",
        kind="domain_pack",
        displayName="WebXR / Immersive Web",
        activatesOn="HTML entry + an XR signal — navigator.xr / XRFrame / "
                    "requestSession, .glb/.gltf assets, or Three / Babylon / A-Frame",
        provides=("xr-entrypoints", "scene-graph-hints", "device-frame-lens"),
        capabilities=("domain_summary",),
        status="suggested",
        source="suggested",
        probe=_detect_webxr,
        runtime={"module": "openfde.plugins_runtime.webxr", "factory": "make_runtime"},
    )]


# ── Install = ENABLE A LOCAL MANIFEST (allowlist-gated; writes JSON, runs NO code) ──────
# "Install" here writes a known pack's LOCAL MANIFEST into .openfde/plugins/{id}.json — a JSON file,
# never a package/download/import/exec. Allowlisted ids only; an id outside this set is refused, so
# there is no arbitrary package name / path / command. The manifest the install writes is the same
# shape a hand-authored local manifest uses, so it flows through the exact validated read path.
_INSTALLABLE_IDS = frozenset({"webxr"})

# The local manifest each allowlisted pack writes on install. Pure metadata + cheap marker probes —
# it MUST validate through ``_local_spec_from_manifest`` (id/kind/status), and it carries id=="webxr"
# so it SUPERSEDES the suggested WebXR row (one provider, no duplicate).
_INSTALL_MANIFESTS = {
    "webxr": {
        "id": "webxr",
        "kind": "domain_pack",
        "displayName": "WebXR / Immersive Web",
        "version": "1.0.0-local",
        "status": "available",
        "activatesOn": "Three / @react-three/fiber deps, .glb/.gltf assets, or navigator.xr / "
                       "requestSession / XRFrame in source",
        "provides": ["xr-entrypoints", "scene-graph-hints", "device-frame-lens"],
        "capabilities": ["webxr-summary"],
        "description": "WebXR architecture hints — entrypoints, assets, frameworks, markers. "
                       "Read-only; no runtime or test lens.",
        "detects": {
            "dependencies": ["three", "@react-three/fiber", "@react-three/drei", "babylonjs", "aframe"],
            "files": ["**/*.glb", "**/*.gltf"],
            "content": ["navigator.xr", "requestSession", "XRFrame"],
        },
    },
}


def install_plan(plugin_id: str) -> dict:
    """The allowlist verdict for an install — ALLOWLIST-GATED, NO write/download/exec. Reports whether
    ``plugin_id`` is a known OpenFDE pack and what enabling it would add. An unknown id is refused
    (``installable: False``). The actual enable (writing the local manifest) is :func:`install_local`."""
    pid = str(plugin_id or "").strip()
    if pid not in _INSTALLABLE_IDS:
        return {"ok": False, "id": pid, "installable": False, "installed": False,
                "reason": "unknown plugin id — install is allowlisted to known OpenFDE packs"}
    meta = next((s for s in _suggested_specs() if s.id == pid), None)
    return {
        "ok": True, "id": pid, "installable": True, "installed": False,
        "displayName": meta.displayName if meta else pid,
        "provides": list(meta.provides) if meta else [],
        "reason": "installable — enabling writes a local manifest (.openfde/plugins/{}.json); "
                  "no external code is downloaded or executed".format(pid),
    }


def install_local(root, plugin_id: str) -> dict:
    """ENABLE a known optional pack by WRITING its local manifest into ``.openfde/plugins/{id}.json``.

    This is the safe "install": a JSON FILE is written — **nothing is downloaded, imported, or
    executed**, no network, no subprocess. Allowlist-gated (an unknown id is refused, reusing
    :func:`install_plan`), and IDEMPOTENT — a valid same-id manifest already present is left as-is.
    The written manifest validates through the same ``_local_spec_from_manifest`` read path and carries
    ``id == plugin_id``, so it supersedes a same-id suggestion (no duplicate row).
    """
    plan = install_plan(plugin_id)
    if not plan.get("installable"):
        return {**plan, "installed": False}                  # unknown id → refused, no write
    pid = plan["id"]
    template = _INSTALL_MANIFESTS.get(pid)
    if root is None or template is None:
        return {**plan, "installed": False,
                "reason": "cannot enable — no watched repo / no manifest for this pack"}

    dest = Path(root) / _LOCAL_PLUGIN_DIR / f"{pid}.json"
    existing = _local_spec_from_manifest(dest) if dest.exists() else None
    already = existing is not None and existing.id == pid    # idempotent: same-id manifest present
    if not already:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.parent / (dest.name + ".tmp")
            tmp.write_text(json.dumps(template, indent=2), encoding="utf-8")
            tmp.replace(dest)                                # atomic; .tmp never matches *.json glob
        except OSError as exc:
            return {"ok": False, "id": pid, "installed": False,
                    "reason": f"could not write the local manifest: {exc}"}
    return {
        "ok": True, "id": pid, "installed": True, "alreadyEnabled": already,
        "source": "local", "kind": template["kind"], "displayName": template["displayName"],
        "version": template.get("version", ""), "provides": list(template.get("provides") or []),
        "path": f"{_LOCAL_PLUGIN_DIR}/{pid}.json",
        "reason": f"enabled the local manifest at {_LOCAL_PLUGIN_DIR}/{pid}.json — "
                  "no external code was downloaded or executed",
    }


# ── Curated INSTALL registry + plan (v1-I) ──────────────────────────────────────────────
# A small, in-code allowlist of KNOWN packs and how OpenFDE would obtain each — the ONLY source of
# installable ids + package specs. There is NO marketplace, NO search, and NO user-supplied package
# name. v1-I is PLAN-ONLY: ``plugin_install_plan`` describes the proposed, approval-gated action as
# STRUCTURED actions (argv lists / endpoints — never a shell string); it downloads/imports/runs NOTHING.
#
#   method "builtin-local" — the pack ships in core; "installing" it means ENABLE a local manifest
#       (writes .openfde/plugins/{id}.json — a JSON file, no package, no code run). WebXR today.
#   method "pip"           — a curated external package contributing entry points; the plan proposes a
#       pinned argv (python -m pip install <spec>), approval-required, NEVER auto-run. (none yet)
_CURATED_PLUGINS = {
    "webxr": {
        "id": "webxr",
        "displayName": "WebXR / Immersive Web",
        "kind": "domain_pack",
        "method": "builtin-local",
        "packageName": None,
        "version": None,
        "capabilities": ["domain_summary"],
        "description": "WebXR architecture hints — entrypoints, assets, frameworks, markers. Built "
                       "into OpenFDE today (demo / local-capable); enabling writes a local manifest, "
                       "no package install.",
        "status": "builtin-local",
    },
    "treesitter-js-ts": {
        "id": "treesitter-js-ts",
        "displayName": "Tree-sitter JS/TS parser",
        "kind": "language_pack",
        "method": "pip",
        # EXPLICIT allowlisted packages (not the `openfde[treesitter]` extra, which is unreliable for a
        # source/editable install). These match pyproject's optional `treesitter` extra.
        "packages": ["tree-sitter>=0.22", "tree-sitter-javascript>=0.21", "tree-sitter-typescript>=0.21"],
        "packageName": None,
        "version": None,
        "capabilities": ["architecture"],
        "description": "Precise JS/TS architecture parsing (tree-sitter AST). Optional — OpenFDE uses "
                       "its built-in regex parser without it; install to make the precise path the "
                       "default for JS/TS repos.",
        "status": "recommended",
    },
}


def curated_plugins() -> list:
    """The curated install registry (v1-I) as plain metadata dicts — the allowlist of KNOWN packs and
    how each is obtained. Read-only; no marketplace, no search, no execution."""
    return [dict(entry) for entry in _CURATED_PLUGINS.values()]


def plugin_install_plan(plugin_id: str) -> dict:
    """v1-I curated INSTALL plan for ``plugin_id`` — a PROPOSAL, never an execution.

    Distinct from :func:`install_plan` (v1-D, the local-ENABLE verdict): this answers "how would
    OpenFDE obtain this pack as a package?" purely from the curated allowlist. Returns ``ok``,
    ``installable``, ``requiresApproval`` (always True), a human ``reason``, and STRUCTURED ``actions``
    (argv lists / endpoints — NEVER shell strings, never a user-supplied package name). An id outside
    the curated registry is refused. It downloads/imports/runs NOTHING; actual package install stays
    approval-gated and deferred."""
    pid = str(plugin_id or "").strip()
    entry = _CURATED_PLUGINS.get(pid)
    if entry is None:
        return {"ok": False, "id": pid, "installable": False, "requiresApproval": True, "actions": [],
                "reason": "unknown plugin id — install is limited to OpenFDE's curated registry "
                          "(no marketplace, no arbitrary package names)"}
    method = entry.get("method")
    plan = {
        "ok": True, "id": pid, "installable": True, "requiresApproval": True, "method": method,
        "displayName": entry["displayName"], "kind": entry["kind"],
        "capabilities": list(entry.get("capabilities") or []),
        "description": entry.get("description", ""), "status": entry.get("status", ""),
    }
    if method == "builtin-local":
        plan["actions"] = [{
            "type": "enable-local",
            "endpoint": f"POST /api/plugins/{pid}/install",
            "writes": f"{_LOCAL_PLUGIN_DIR}/{pid}.json",
        }]
        plan["reason"] = (f"'{entry['displayName']}' is built into OpenFDE — enable it locally "
                          f"(writes {_LOCAL_PLUGIN_DIR}/{pid}.json, a JSON file only; no package is "
                          "downloaded or run)")
    elif method == "pip":
        specs = list(entry.get("packages") or [])
        if not specs and entry.get("packageName"):
            specs = [entry["packageName"] + (entry.get("version") or "")]
        if specs:
            plan["actions"] = [{
                "type": "pip-install",
                # STRUCTURED argv from the curated allowlist — never a shell string, never shell=True,
                # never a user-supplied package name.
                "argv": [sys.executable, "-m", "pip", "install", *specs],
            }]
            plan["reason"] = ("installs the curated package(s) {} (approval required; a proposed argv "
                              "only — OpenFDE never runs it automatically)".format(", ".join(specs)))
        else:
            plan.update(installable=False, actions=[], reason="curated entry has no install spec")
    else:
        plan.update(installable=False, actions=[],
                    reason="curated entry has no supported install method")
    return plan


def treesitter_recommendation(root) -> dict:
    """Recommend the tree-sitter JS/TS parser when ``root`` is a JS/TS repo and the optional grammars
    are NOT installed — so the precise AST path becomes the default experience, gated by approval.

    Returns ``{recommended: bool, id, reason, plan?}``. NOTHING is installed here; ``plan`` (present
    only when recommended) is the approval-gated v1-I curated install plan (a PROPOSAL — structured
    argv, ``requiresApproval: true``). The regex path remains the fallback regardless."""
    out = {"recommended": False, "id": "treesitter-js-ts"}
    if root is None:
        return out
    try:
        from openfde.language_packs import js_ts_treesitter
        from openfde.language_packs.registry import get_language_packs
    except Exception:  # noqa: BLE001 — an optional-path probe must never break
        return out
    is_js_ts = any(getattr(p, "name", "") == "js_ts" for p in (get_language_packs(root) or []))
    if not is_js_ts:
        return {**out, "reason": "not a JS/TS repo — tree-sitter parsing is not applicable"}
    if js_ts_treesitter.available():
        return {**out, "reason": "tree-sitter already installed — precise JS/TS parsing is active"}
    return {
        "recommended": True,
        "id": "treesitter-js-ts",
        "reason": "JS/TS repo detected without tree-sitter — installing it enables precise architecture "
                  "parsing (OpenFDE keeps working with the regex fallback until then)",
        "plan": plugin_install_plan("treesitter-js-ts"),
    }


_LOCAL_PLUGIN_DIR = ".openfde/plugins"
_LOCAL_PLUGIN_MAX = 50
_LOCAL_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_LOCAL_SKIP_DIRS = {".git", ".openfde", "node_modules", "dist", "build", ".next", "out",
                    "coverage", ".nuxt", ".svelte-kit", ".turbo", ".cache", ".parcel-cache",
                    ".vercel", ".output", "__pycache__"}
_LOCAL_TEXT_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
                    ".html", ".htm", ".css", ".json", ".md", ".toml", ".yaml", ".yml")
_LOCAL_SCAN_MAX_FILES = 250
_LOCAL_SCAN_MAX_BYTES = 160_000
_LOCAL_WALK_MAX = 20_000


def _as_short_text(v, fallback="") -> str:
    s = str(v or fallback).strip()
    return s[:180]


def _as_string_tuple(v) -> tuple:
    if isinstance(v, str):
        return (v[:80],) if v.strip() else ()
    if not isinstance(v, list):
        return ()
    out = []
    for item in v[:40]:
        text = str(item or "").strip()
        if text:
            out.append(text[:80])
    return tuple(out)


def _safe_rel_path(p: str) -> str:
    """A repo-relative path/glob, or ``''`` to REJECT it. An absolute path (POSIX ``/…``,
    Windows ``C:\\…``, UNC ``//…``), a home ref (``~``), parent traversal (``..``), or empty
    is rejected — NEVER normalized into a relative path. (Stripping a leading ``/`` would turn
    ``/etc/passwd`` into a relative scan; an absolute marker must simply not match.)"""
    s = str(p or "").replace("\\", "/").strip()
    if not s or s[0] in "/~" or (len(s) > 1 and s[1] == ":"):     # absolute / home → reject
        return ""
    if any(part == ".." for part in s.split("/")):               # parent traversal → reject
        return ""
    return s


def _package_has_dep(root: Path, names: tuple) -> bool:
    if not names:
        return False
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return any(n in deps for n in names)


def _glob_to_regex(pattern: str) -> str:
    """Anchored regex for a repo-relative glob. ``**`` matches across path separators (and an
    optional trailing ``/`` so it can also match zero directories); ``*`` / ``?`` do NOT cross
    ``/``; ``[...]`` is a character class (``[!`` → negation). Used to match a glob against a
    BOUNDED walk instead of an unbounded ``Path.glob``."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < n and pattern[i] == "/":            # "**/": also match zero dirs
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:                                     # unterminated class → literal '['
                out.append(re.escape("["))
                i += 1
            else:
                inner = pattern[i + 1:j]
                inner = ("^" + inner[1:]) if inner[:1] in "!^" else inner
                out.append("[" + inner.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "^" + "".join(out) + "$"


def _file_marker_detects(root: Path, patterns: tuple) -> bool:
    """True when a cleaned (repo-relative, non-traversing) pattern matches a file under ``root``.

    A literal path is a direct existence check; a glob is matched against a BOUNDED, vendor-pruned
    walk — the SAME ``_LOCAL_SKIP_DIRS`` and ``_LOCAL_WALK_MAX`` as content markers — so a broad
    ``**/*.x`` never scans node_modules/dist or runs away. No imports/subprocess/network."""
    import os
    literals, regexes = [], []
    for raw in patterns:
        pat = _safe_rel_path(raw)
        if not pat:                                        # absolute / .. / ~ / empty → dropped
            continue
        if any(ch in pat for ch in "*?["):
            regexes.append(re.compile(_glob_to_regex(pat)))
        else:
            literals.append(pat)
    for lit in literals:
        if (root / lit).exists():
            return True
    if not regexes:
        return False
    walked = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _LOCAL_SKIP_DIRS and not d.startswith(".")]
            rel_dir = os.path.relpath(dirpath, root)
            prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
            for fn in filenames:
                walked += 1
                if walked > _LOCAL_WALK_MAX:
                    return False
                if any(rx.match(prefix + fn) for rx in regexes):
                    return True
    except OSError:
        return False
    return False


def _content_marker_detects(root: Path, markers: tuple) -> bool:
    if not markers:
        return False
    lows = tuple(m.lower() for m in markers if m)
    walked = scanned = 0
    try:
        for dirpath, dirnames, filenames in __import__("os").walk(root):
            dirnames[:] = [d for d in dirnames if d not in _LOCAL_SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                walked += 1
                if walked > _LOCAL_WALK_MAX:
                    return False
                if scanned >= _LOCAL_SCAN_MAX_FILES or not fn.lower().endswith(_LOCAL_TEXT_EXTS):
                    continue
                p = Path(dirpath) / fn
                try:
                    if p.stat().st_size > _LOCAL_SCAN_MAX_BYTES:
                        continue
                    scanned += 1
                    text = p.read_text(encoding="utf-8", errors="ignore").lower()
                    if any(m in text for m in lows):
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def _const_probe(value: bool):
    """A marker-free probe — a local manifest with no ``detects`` block is simply DECLARED
    present for the repo (or explicitly opted out via ``"detected": false``)."""
    def probe(_root) -> bool:
        return value
    return probe


def _manifest_probe(detects: dict):
    """A local manifest's cheap marker probe. Metadata only; never imports plugin code."""
    if not isinstance(detects, dict):
        return None
    files = _as_string_tuple(detects.get("files") or detects.get("paths"))
    deps = _as_string_tuple(detects.get("dependencies") or detects.get("deps"))
    markers = _as_string_tuple(detects.get("content") or detects.get("markers"))
    if not (files or deps or markers):
        return None

    def probe(repo_root) -> bool:
        r = Path(repo_root)
        hits = [
            _file_marker_detects(r, files),
            _package_has_dep(r, deps),
            _content_marker_detects(r, markers),
        ]
        return all(hits) if detects.get("all") is True else any(hits)
    return probe


# Default "activatesOn" summary per manifest source (when the manifest omits one).
_DEFAULT_ACTIVATES = {
    "local": "declared in .openfde/plugins",
    "external": "declared by an installed plugin package",
}


# ── Lazy runtime contract (v1-H) ──────────────────────────────────────────────
# The optional hooks a plugin's runtime factory may expose. Metadata discovery never touches any
# of this; a hook is resolved only when the activation API loads a repo-matching plugin's runtime.
RUNTIME_HOOKS = ("architecture", "test_detector", "failure_parser", "repro_drafter", "domain_summary")

_RUNTIME_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_RUNTIME_ATTR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _runtime_descriptor(raw_runtime):
    """Validate a manifest's optional ``runtime`` block into ``{module, factory}`` — STRINGS parsed as
    metadata, NEVER imported here. A dotted ``module`` path + an identifier ``factory``; anything else
    (or a missing field) → ``None`` (with a warning when malformed). The activation API (v1-H) is the
    only place that imports ``module`` and calls ``factory`` — and only for a repo-matching ACTIVE
    plugin, on request."""
    if not isinstance(raw_runtime, dict):
        return None
    module = _as_short_text(raw_runtime.get("module"))
    factory = _as_short_text(raw_runtime.get("factory"))
    if not module and not factory:
        return None
    if not _RUNTIME_MODULE_RE.match(module) or not _RUNTIME_ATTR_RE.match(factory):
        logger.warning("plugin runtime descriptor ignored: invalid module/factory (%r:%r)", module, factory)
        return None
    return {"module": module, "factory": factory}


def _spec_from_manifest_dict(raw, *, source: str, id_fallback: str = ""):
    """Normalize a manifest dict into a ``PluginSpec``, or ``None`` when invalid.

    The ONE manifest contract, shared by repo-local JSON (``source='local'``) and external
    entry-point providers (``source='external'``) so there is no special case. A manifest may
    only DECLARE metadata + cheap markers: no import path, entry point, package name, command, or
    download field is ever honored. Strict validation (id/kind/status); an invalid manifest
    returns ``None`` and the CALLER logs (it knows the path / entry-point name).

    The optional ``runtime`` block (v1-H) is parsed as metadata — module/factory STRINGS, never
    imported here. SECURITY: only an **external** (pip-installed, already-trusted) plugin may declare
    a runtime; a repo-local manifest is untrusted (opening a repo must never trigger a code import),
    so its ``runtime`` is silently dropped — exactly like the import/command fields v1-C never honors."""
    if not isinstance(raw, dict):
        return None
    pid = _as_short_text(raw.get("id") or id_fallback)
    kind = _as_short_text(raw.get("kind"))
    status = _as_short_text(raw.get("status") or "available")
    if not _LOCAL_ID_RE.match(pid) or kind not in PLUGIN_KINDS or status not in ("available", "disabled"):
        return None

    detects = raw.get("detects") if isinstance(raw.get("detects"), dict) else {}
    probe = _manifest_probe(detects)
    if probe is None:
        # No marker probe → the manifest is simply DECLARED for this repo. An explicit
        # ``"detected": false`` opts out; the default is True ("present because declared").
        declared = raw.get("detected") if isinstance(raw.get("detected"), bool) else True
        probe = _const_probe(declared)

    runtime = _runtime_descriptor(raw.get("runtime")) if source == "external" else None

    return PluginSpec(
        id=pid,
        kind=kind,
        displayName=_as_short_text(raw.get("displayName") or raw.get("name") or pid, pid),
        activatesOn=_as_short_text(raw.get("activatesOn") or _DEFAULT_ACTIVATES.get(source, "")),
        provides=_as_string_tuple(raw.get("provides")),
        status=status,
        source=source,
        probe=probe,
        version=_as_short_text(raw.get("version")),
        description=_as_short_text(raw.get("description")),
        capabilities=_as_string_tuple(raw.get("capabilities")),
        runtime=runtime,
    )


def _local_spec_from_manifest(path: Path):
    """Read one repo-local manifest (``.openfde/plugins/*.json``) into a PluginSpec, or None when
    invalid. Strict validation through the shared :func:`_spec_from_manifest_dict`; an invalid
    manifest is ignored with a warning so a bad declaration never breaks ``openfde watch``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("local plugin manifest ignored (%s): %s", path.name, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("local plugin manifest ignored (%s): expected object", path.name)
        return None
    spec = _spec_from_manifest_dict(raw, source="local", id_fallback=path.stem)
    if spec is None:
        logger.warning("local plugin manifest ignored (%s): invalid id/kind/status", path.name)
    return spec


def local_specs(root=None) -> list:
    """Repo-local plugin manifests from ``.openfde/plugins/*.json``.

    Read-only v1-C: manifests are metadata/probes only, not executable plugins.
    Invalid manifests are ignored with a warning so a bad declaration never breaks
    ``openfde watch``.
    """
    if root is None:
        return []
    d = Path(root) / _LOCAL_PLUGIN_DIR
    try:
        files = sorted(p for p in d.glob("*.json") if p.is_file())[:_LOCAL_PLUGIN_MAX]
    except OSError:
        return []
    specs = []
    for p in files:
        spec = _local_spec_from_manifest(p)
        if spec is not None:
            specs.append(spec)
    return specs


# ── External plugins via Python entry points (v1-G) ──────────────────────────────
#
# An installed package can contribute a pack OUTSIDE core by declaring an entry point in
# one of these groups, pointing at a lightweight MANIFEST PROVIDER (a zero-arg callable
# returning a manifest dict — or a list of dicts — or an object exposing ``manifest()`` /
# ``to_manifest()``, or a manifest dict directly). DISCOVERY ONLY: OpenFDE loads the
# provider and normalizes its manifest through the SAME ``_spec_from_manifest_dict`` contract
# as a local manifest (``source='external'``). It does NOT activate or runtime-load the pack —
# the heavy analyzer must live behind a separate import the provider does not pull in, so
# discovery stays cheap. Install/download is still a separate, deferred concern.
_ENTRY_POINT_GROUPS = ("openfde.plugins", "openfde.language_packs", "openfde.domain_packs")


def _iter_entry_points(group: str) -> list:
    """Entry points declared for ``group`` — robust across importlib.metadata versions and
    never raising (discovery must not break the listing)."""
    import importlib.metadata as importlib_metadata
    try:
        eps = importlib_metadata.entry_points(group=group)        # Python 3.10+
    except TypeError:
        try:
            eps = importlib_metadata.entry_points().get(group, [])  # Python 3.9 dict API
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry-point discovery unavailable: %s", exc)
            return []
    except Exception as exc:  # noqa: BLE001 — a metadata read must never crash the listing
        logger.warning("entry-point discovery failed for %s: %s", group, exc)
        return []
    return list(eps or [])


def _coerce_manifests(loaded) -> list:
    """Normalize a loaded entry-point target into a list of manifest dicts. Accepts a zero-arg
    callable (called once), an object exposing ``manifest()`` / ``to_manifest()``, a manifest
    dict, or a list/tuple of dicts. Anything else → ``[]``."""
    obj = loaded() if callable(loaded) else loaded
    fn = getattr(obj, "to_manifest", None) or getattr(obj, "manifest", None)
    if callable(fn):
        obj = fn()
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, (list, tuple)):
        return [m for m in obj if isinstance(m, dict)]
    return []


def _note_external_problem(diagnostics, group, name, msg) -> None:
    """A bad/malformed external plugin is LOGGED and skipped (never crashes discovery); when a
    ``diagnostics`` list is supplied, a structured record is appended for callers/tests."""
    logger.warning("external plugin ignored (%s:%s): %s", group, name, msg)
    if diagnostics is not None:
        diagnostics.append({"group": group, "name": name, "error": str(msg)})


def _external_spec_from_entry_point(ep, group, diagnostics) -> list:
    """Load one entry point's manifest provider and normalize it into PluginSpec(s). Every step is
    guarded — a load failure, a throwing provider, a non-manifest return, or an invalid manifest is
    reported and skipped, never raised."""
    name = getattr(ep, "name", "?")
    try:
        loaded = ep.load()
    except Exception as exc:  # noqa: BLE001 — a third-party import must not crash OpenFDE
        _note_external_problem(diagnostics, group, name, f"load failed: {exc}")
        return []
    try:
        manifests = _coerce_manifests(loaded)
    except Exception as exc:  # noqa: BLE001 — a throwing provider must not crash OpenFDE
        _note_external_problem(diagnostics, group, name, f"manifest provider failed: {exc}")
        return []
    if not manifests:
        _note_external_problem(diagnostics, group, name, "no manifest returned")
        return []
    specs = []
    for raw in manifests:
        spec = _spec_from_manifest_dict(raw, source="external", id_fallback=name)
        if spec is None:
            _note_external_problem(diagnostics, group, name, "invalid manifest (id/kind/status/shape)")
            continue
        specs.append(spec)
    return specs


def _discover_external(diagnostics=None) -> list:
    """Discover external plugins across the entry-point groups, de-duped by id (first wins). Pass a
    list as ``diagnostics`` to also collect per-plugin failure records."""
    specs, seen = [], set()
    for group in _ENTRY_POINT_GROUPS:
        for ep in _iter_entry_points(group):
            for spec in _external_spec_from_entry_point(ep, group, diagnostics):
                if spec.id in seen:
                    continue                       # same id across groups/eps → first wins
                seen.add(spec.id)
                specs.append(spec)
    return specs


def external_specs() -> list:
    """External plugins discovered via Python entry points (v1-G). DISCOVERY ONLY — lightweight
    manifest/probe metadata with ``source='external'``; nothing is activated or runtime-loaded, and
    a bad/malformed external plugin is logged + skipped, never crashing the listing."""
    return _discover_external()


def all_specs() -> list:
    """Every provider the registry can describe (repo-independent): built-ins, deterministic
    suggestions, and external entry-point plugins (v1-G). All four sources share the one
    ``PluginSpec`` contract, so built-ins, suggestions, and externals flow through with no
    special case."""
    return builtin_specs() + _suggested_specs() + external_specs()


def _merged_specs(root=None) -> list:
    """The de-duped provider set for ``root`` as ``PluginSpec`` objects (NO manifest serialization,
    NO runtime import). The single dedup/precedence point shared by ``list_plugins`` (metadata) and
    the activation API (runtime).

    Precedence — MOST SPECIFIC wins, so every id appears exactly once (no duplicates):
      • built-ins keep their own id space and are never superseded;
      • an **external** package never shadows a built-in id, and a repo-**local** manifest of the
        same id takes precedence over an external package;
      • a **suggestion** is superseded by a same-id local manifest OR external package — so e.g.
        WebXR shows once, whether it's the suggestion, an enabled local manifest, or a packaged
        external pack."""
    builtins_ = builtin_specs()
    suggested_ = _suggested_specs()
    externals = external_specs()
    locals_ = local_specs(root)

    builtin_ids = {s.id for s in builtins_}
    local_ids = {s.id for s in locals_}
    # External never shadows a core built-in id; a same-id local manifest wins over an external.
    externals = [s for s in externals if s.id not in builtin_ids and s.id not in local_ids]
    external_ids = {s.id for s in externals}
    # A suggestion drops out once a same-id local manifest OR external package exists.
    superseded = local_ids | external_ids
    suggested_ = [s for s in suggested_ if s.id not in superseded]

    return builtins_ + suggested_ + externals + locals_


def list_plugins(root=None) -> list:
    """Plugin METADATA + per-repo activation for ``root`` (the watched repo): built-in providers,
    matched suggestions, external entry-point plugins, and validated repo-local manifests. ``root``
    None → metadata only (``active`` False everywhere; suggestions resolve to 'missing'; no local
    manifests; externals still listed since they are repo-independent).

    Metadata/probe ONLY — this NEVER imports a plugin's runtime module (that is the activation API's
    job, on request). One row per id (see :func:`_merged_specs` for the precedence)."""
    return [spec.manifest(root) for spec in _merged_specs(root)]


# ── Plugin activation / runtime loading (v1-H) ────────────────────────────────────
#
# Discovery (above) answers "what exists?" — cheap metadata/probe, no code imported. Activation
# answers "what code can I use for THIS repo right now?" It is LAZY: a plugin's runtime module is
# imported only when (a) the plugin is in the merged set, (b) its probe matches ``root``, and (c) a
# caller asks — via ``runtime_for_capability`` / ``load_plugin_runtime``. Listing never triggers it.
#
# Honest boundary: this is the lazy runtime CONTRACT only. Built-in language packs are NOT migrated
# onto it (their analysis still lives in core); a builtin/suggested spec carries no ``runtime``, so
# the activation API simply skips it. Install/download marketplace remains deferred.
_RUNTIME_FAILED = object()          # cache sentinel: this (plugin, root) tried to load and failed
_RUNTIME_CACHE: dict = {}           # (plugin_id, resolved_root) -> runtime object | _RUNTIME_FAILED


def _runtime_cache_key(plugin_id, root):
    try:
        return (plugin_id, str(Path(root).resolve()))
    except Exception:  # noqa: BLE001 — a weird root must not break activation
        return (plugin_id, str(root))


def runtime_hook(runtime, name):
    """Resolve a named hook (one of :data:`RUNTIME_HOOKS`) from a runtime dict or object, or None.
    Uniform access so callers don't care whether a factory returned a dict or an object."""
    if runtime is None:
        return None
    if isinstance(runtime, dict):
        return runtime.get(name)
    return getattr(runtime, name, None)


def _instantiate_runtime(spec, root):
    """Import ``spec.runtime['module']``, call its ``factory`` (passing ``root`` if it accepts an
    argument), and return the runtime object/dict — or None on any failure (logged, never raised).
    This is the ONLY place plugin runtime code is imported."""
    rt = spec.runtime or {}
    module_path, factory_name = rt.get("module"), rt.get("factory")
    if not module_path or not factory_name:
        return None
    try:
        import importlib
        module = importlib.import_module(module_path)
        factory = getattr(module, factory_name)
    except Exception as exc:  # noqa: BLE001 — a third-party import must never crash OpenFDE
        logger.warning("plugin runtime import failed for %s (%s:%s): %s",
                       spec.id, module_path, factory_name, exc)
        return None
    try:
        import inspect
        try:
            wants_arg = len(inspect.signature(factory).parameters) >= 1
        except (TypeError, ValueError):
            wants_arg = False
        runtime = factory(root) if wants_arg else factory()
    except Exception as exc:  # noqa: BLE001 — a throwing factory must never crash OpenFDE
        logger.warning("plugin runtime factory failed for %s: %s", spec.id, exc)
        return None
    if runtime is None:
        logger.warning("plugin runtime factory for %s returned None", spec.id)
        return None
    return runtime


def load_plugin_runtime(plugin_id, root):
    """Lazily load ONE plugin's runtime for ``root``, or ``None``.

    Loads only when the plugin is in the merged set for ``root``, declares a ``runtime``, AND its
    probe matches the repo — so a non-matching or runtime-less plugin never imports anything. The
    result is cached per ``(plugin_id, resolved root)`` (successes and failures) to avoid repeat
    imports. A bad import/factory is logged and yields ``None``; it never raises."""
    if root is None or not plugin_id:
        return None
    key = _runtime_cache_key(plugin_id, root)
    if key in _RUNTIME_CACHE:
        cached = _RUNTIME_CACHE[key]
        return None if cached is _RUNTIME_FAILED else cached
    spec = next((s for s in _merged_specs(root) if s.id == plugin_id), None)
    if spec is None or not spec.runtime or not spec.detect(root):
        return None                                   # unknown / no runtime / repo doesn't match
    runtime = _instantiate_runtime(spec, root)
    _RUNTIME_CACHE[key] = runtime if runtime is not None else _RUNTIME_FAILED
    return runtime


def active_plugins(root, capability=None):
    """Provider MANIFESTS whose probe matches ``root`` (active for this repo), optionally filtered to
    those declaring ``capability``. Metadata only — does NOT import any runtime. ``root`` None → []."""
    if root is None:
        return []
    out = []
    for spec in _merged_specs(root):
        if not spec.detect(root):
            continue
        if capability and capability not in (spec.capabilities or ()):
            continue
        out.append(spec.manifest(root))
    return out


def runtime_for_capability(root, capability):
    """Loaded runtimes of every ACTIVE plugin that declares ``capability``, as
    ``[{"id", "capability", "runtime"}]``. Loads each matching plugin's runtime lazily (cached);
    plugins that don't declare the capability, don't match the repo, or fail to load are skipped.
    The one entry point a caller uses to ask "what code provides X for this repo right now?\""""
    if root is None or not capability:
        return []
    out = []
    for spec in _merged_specs(root):
        if capability not in (spec.capabilities or ()):
            continue
        if not spec.runtime or not spec.detect(root):
            continue
        runtime = load_plugin_runtime(spec.id, root)
        if runtime is not None:
            out.append({"id": spec.id, "capability": capability, "runtime": runtime})
    return out


# Sentinel: no plugin hook ran → the product call site uses its in-core fallback.
NO_HOOK = object()


def run_capability_hook(root, capability, invoke, *, provider_id=None):
    """v1-K consume seam: run the FIRST active plugin runtime hook for ``capability`` (optionally only
    the provider whose id is ``provider_id``) via ``invoke(hook)`` and return its result; return
    :data:`NO_HOOK` when no provider/hook is available, OR it raises (logged). The product call site
    then falls back to its in-core implementation.

    NEVER raises — a bad/throwing runtime hook must not crash the product path; it logs and falls back.
    Listing stays metadata-only (this never touches it); runtime loads lazily inside
    :func:`runtime_for_capability`, only for a repo that matches the provider's probe."""
    if root is None or not capability:
        return NO_HOOK
    try:
        for prov in runtime_for_capability(root, capability):
            if provider_id is not None and prov.get("id") != provider_id:
                continue
            hook = runtime_hook(prov.get("runtime"), capability)
            if callable(hook):
                return invoke(hook)
    except Exception as exc:  # noqa: BLE001 — a bad hook falls back, never crashes the product path
        logger.warning("plugin '%s' runtime hook failed; using core fallback: %s", capability, exc)
    return NO_HOOK


def resolve_webxr_summary(root) -> dict:
    """WebXR architecture summary via the plugin RUNTIME system first (v1-H), with a guaranteed
    fallback to the core :func:`webxr_summary`. This is the first real capability migrated onto the
    runtime hook.

    Prefers an active ``domain_summary`` runtime provider with id ``webxr`` (loaded lazily, only when
    its probe matches the repo). If no such runtime is active, or its hook is missing / raises /
    returns an empty result, it falls back to ``webxr_summary(root)`` — so the endpoint NEVER
    regresses and the response shape is identical (the built-in runtime delegates to the same core)."""
    try:
        for provider in runtime_for_capability(root, "domain_summary"):
            if provider.get("id") != "webxr":
                continue
            hook = runtime_hook(provider.get("runtime"), "domain_summary")
            if not callable(hook):
                continue
            result = hook(root)
            if isinstance(result, dict) and result:
                return result
    except Exception as exc:  # noqa: BLE001 — the runtime path must never break the endpoint
        logger.warning("WebXR runtime summary failed; using core fallback: %s", exc)
    return webxr_summary(root)
