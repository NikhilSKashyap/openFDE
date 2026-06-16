"""Manifest provider — the entry-point target (``openfde_webxr_plugin.plugin:manifest``).

LIGHTWEIGHT: metadata + the WebXR marker probe + a STRING runtime pointer. Imports no runtime, so
plugin LISTING never loads the summary code."""


def manifest():
    """This pack's manifest. ``id="webxr"`` so it de-dupes with the built-in suggestion / local
    manifest to one WebXR row; the probe mirrors the built-in WebXR markers."""
    return {
        "id": "webxr",
        "kind": "domain_pack",
        "displayName": "WebXR / Immersive Web",
        "version": "0.1.0",
        "description": "WebXR architecture hints (entrypoints, assets, frameworks) as an external "
                       "pack — the v1-M skeleton for a future openfde-webxr package.",
        "capabilities": ["domain_summary"],
        # Mirror the built-in WebXR markers (Three / R3F / Babylon / A-Frame deps, .glb/.gltf assets,
        # or navigator.xr / requestSession / XRFrame in source) — marker-only, bounded by the registry.
        "detects": {
            "dependencies": ["three", "@react-three/fiber", "@react-three/drei", "babylonjs", "aframe"],
            "files": ["**/*.glb", "**/*.gltf"],
            "content": ["navigator.xr", "requestSession", "XRFrame"],
        },
        # STRING pointer — the runtime is imported lazily, only when domain_summary is requested.
        "runtime": {"module": "openfde_webxr_plugin.runtime", "factory": "make_runtime"},
    }
