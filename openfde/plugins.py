"""
openfde/plugins.py — internal capability-provider registry (Plugin Registry v1-A/B/C).

OpenFDE's capabilities — language packs, domain packs, verify adapters, agent
providers, layout engines, integrations — should be DESCRIBABLE without importing
their heavy code. This module is the manifest/probe layer: it lists every provider as
lightweight METADATA and computes per-repo activation by probing cheap markers.

Three sources, one ``PluginSpec`` contract (the ``source`` field):
  • **builtin**   (v1-A) — the built-in language packs (Python, JS/TS), wired from the
    existing registry; their probe is each pack's own ``detects(root)``, so activation
    stays the single source of truth.
  • **suggested** (v1-B Lite) — deterministic domain-pack SUGGESTIONS (WebXR) surfaced
    when cheap repo markers match; never active, never loaded.
  • **local**     (v1-C) — read-only manifests from ``.openfde/plugins/*.json`` in the
    watched repo, so a provider can exist outside the hardcoded built-ins.

Honest boundary: **metadata + probe ONLY.** NO install/download, NO network, NO
subprocess, NO external code loaded — a local manifest can only declare metadata + cheap
marker probes (file / dependency / content), and a bad manifest is ignored with a
warning, never crashing ``openfde watch``. Installing suggested packs (entry points /
packages) is later (v1-D). The single contract means a local manifest slots in with no
special case.
"""
from __future__ import annotations

import json
import logging
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
    source: str = "builtin"            # builtin | suggested | local

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
            source="builtin",
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
        return {"detected": False, "entrypoints": [], "assets": [], "frameworks": [],
                "markers": [], "warnings": ["WebXR scan unavailable."]}

    root = Path(root)
    frameworks, assets, entrypoints = [], [], []
    seen_markers: set = set()
    truncated = False

    pkg = _read_package_json(root)
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    for dep, label in _XR_FRAMEWORK_LABELS.items():
        if dep in deps and label not in frameworks:
            frameworks.append(label)

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
                    if len(assets) < _WEBXR_SUMMARY_CAP:
                        assets.append(rel)
                    else:
                        truncated = True
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
                                if rel not in entrypoints and len(entrypoints) < _WEBXR_SUMMARY_CAP:
                                    entrypoints.append(rel)
                                elif len(entrypoints) >= _WEBXR_SUMMARY_CAP:
                                    truncated = True
                    except OSError:
                        pass
    except OSError:
        pass

    warnings = ["Architecture hints only — no WebXR runtime or test lens is installed."]
    if truncated:
        warnings.append("Scan bounded — results may be partial on a large repo.")
    return {
        "detected": bool(frameworks or assets or seen_markers),
        "entrypoints": entrypoints,
        "assets": assets,
        "frameworks": frameworks,
        "markers": sorted(seen_markers),
        "warnings": warnings,
    }


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
        source="suggested",
        probe=_detect_webxr,
    )]


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


def _local_spec_from_manifest(path: Path):
    """Read one repo-local manifest into a PluginSpec, or None when invalid.

    v1-C is deliberately read-only: no import path, entry point, package name, command,
    or download field is honored. A manifest can only describe metadata + cheap markers.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("local plugin manifest ignored (%s): %s", path.name, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("local plugin manifest ignored (%s): expected object", path.name)
        return None

    pid = _as_short_text(raw.get("id") or path.stem)
    kind = _as_short_text(raw.get("kind"))
    status = _as_short_text(raw.get("status") or "available")
    if not _LOCAL_ID_RE.match(pid) or kind not in PLUGIN_KINDS or status not in ("available", "disabled"):
        logger.warning("local plugin manifest ignored (%s): invalid id/kind/status", path.name)
        return None

    detects = raw.get("detects") if isinstance(raw.get("detects"), dict) else {}
    probe = _manifest_probe(detects)
    if probe is None:
        # No marker probe → the manifest is simply DECLARED for this repo. An explicit
        # ``"detected": false`` opts out; the default is True ("present because declared").
        declared = raw.get("detected") if isinstance(raw.get("detected"), bool) else True
        probe = _const_probe(declared)

    return PluginSpec(
        id=pid,
        kind=kind,
        displayName=_as_short_text(raw.get("displayName") or raw.get("name") or pid, pid),
        activatesOn=_as_short_text(raw.get("activatesOn") or "declared in .openfde/plugins"),
        provides=_as_string_tuple(raw.get("provides")),
        status=status,
        source="local",
        probe=probe,
    )


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


def all_specs() -> list:
    """Every provider the registry can describe: built-ins + deterministic
    suggestions. Built-ins and suggestions share the one ``PluginSpec`` contract."""
    return builtin_specs() + _suggested_specs()


def list_plugins(root=None) -> list:
    """Plugin metadata + per-repo activation for ``root`` (the watched repo):
    built-in providers plus any matched suggestions. ``root`` None → metadata only
    (``active`` False everywhere; suggestions resolve to 'missing')."""
    specs = all_specs() + local_specs(root)
    return [spec.manifest(root) for spec in specs]
