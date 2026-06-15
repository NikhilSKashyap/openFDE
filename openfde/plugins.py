"""
openfde/plugins.py — internal capability-provider registry (Plugin Registry v1-A).

OpenFDE's capabilities — language packs, domain packs, verify adapters, agent
providers, layout engines, integrations — should be DESCRIBABLE without importing
their heavy code. This module is the manifest/probe layer: it lists every built-in
provider as lightweight METADATA, and computes per-repo activation by probing cheap
markers (a language pack's ``detects(root)``).

Honest boundary (v1-A): **metadata + probe ONLY.** Built-ins only — nothing here
changes pack behavior, the existing ``get_language_packs(root)`` is untouched and
still owns activation. There is NO manifest loading from disk, NO install/download,
and NO external code loaded from arbitrary paths. Those are later:
  • **v1-B** — load local plugin manifests (still no install).
  • **v1-C** — install suggested plugins (entry points / packages).
The shape here is manifest/probe so an external plugin can slot into the same
contract without a special case.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
            "activatesOn": self.activatesOn,
            "provides": list(self.provides),
            "active": active,
            "detected": detected,
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
            probe=pack.detects,
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


def _suggested_specs() -> list:
    """Deterministic domain-pack SUGGESTIONS (v1-B Lite): metadata only, surfaced
    when the repo shows cheap markers. NEVER active, NEVER loaded — each manifest's
    ``status`` resolves to 'suggested' when its probe matches, else 'missing'."""
    return [PluginSpec(
        id="webxr",
        kind="domain_pack",
        displayName="WebXR / Immersive Web",
        activatesOn="HTML entry + an XR signal — navigator.xr / XRFrame / "
                    "requestSession, .glb/.gltf assets, or Three / Babylon / A-Frame",
        provides=("xr-entrypoints", "scene-graph-hints", "device-frame-lens"),
        status="suggested",
        probe=_detect_webxr,
    )]


def all_specs() -> list:
    """Every provider the registry can describe: built-ins + deterministic
    suggestions. Built-ins and suggestions share the one ``PluginSpec`` contract."""
    return builtin_specs() + _suggested_specs()


def list_plugins(root=None) -> list:
    """Plugin metadata + per-repo activation for ``root`` (the watched repo):
    built-in providers plus any matched suggestions. ``root`` None → metadata only
    (``active`` False everywhere; suggestions resolve to 'missing')."""
    return [spec.manifest(root) for spec in all_specs()]
