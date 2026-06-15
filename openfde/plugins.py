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
#   available — an external plugin is installed/loadable (v1-B+)
#   missing   — known/suggested but not installed (v1-C can offer install)
#   disabled  — present but turned off by the user
PLUGIN_STATUSES = ("builtin", "available", "missing", "disabled")


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

    def is_active(self, root) -> bool:
        if root is None or not callable(self.probe):
            return False
        try:
            return bool(self.probe(root))
        except Exception as exc:  # noqa: BLE001 — a probe must never break the listing
            logger.warning("plugin probe failed for %s: %s", self.id, exc)
            return False

    def manifest(self, root=None) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "displayName": self.displayName,
            "status": self.status,
            "activatesOn": self.activatesOn,
            "provides": list(self.provides),
            "active": self.is_active(root),
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


def list_plugins(root=None) -> list:
    """Built-in plugin metadata + per-repo activation for ``root`` (the watched
    repo). ``root`` None → metadata only (``active`` is False everywhere)."""
    return [spec.manifest(root) for spec in builtin_specs()]
