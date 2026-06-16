"""Manifest provider — the entry-point target (``ofde_sample_plugin.plugin:manifest``).

LIGHTWEIGHT by contract: it is loaded during plugin LISTING, so it declares metadata + a cheap probe +
a STRING runtime pointer only. It must NOT import ``runtime`` (so listing never loads the runtime)."""


def manifest():
    """Return this plugin's manifest (see the package docstring for the full contract)."""
    return {
        "id": "sample-pack",
        "kind": "domain_pack",
        "displayName": "Sample Domain Pack",
        "version": "0.1.0",
        "description": "Reference external plugin proving the OpenFDE plug-and-play contract (v1-L).",
        "capabilities": ["domain_summary"],
        # Cheap, marker-only probe: active only for a repo containing a *.samplemarker file.
        "detects": {"files": ["**/*.samplemarker"]},
        # STRING pointer — the runtime is imported lazily, only when a capability is requested.
        "runtime": {"module": "ofde_sample_plugin.runtime", "factory": "make_runtime"},
    }
