"""Built-in WebXR ``domain_summary`` runtime (Plugin Registry v1-H — the first capability migrated
onto the plugin runtime hook).

Imported LAZILY by the activation API, only when a WebXR-active repo's ``domain_summary`` capability is
requested — listing ``/api/plugins`` never loads it. The hook DELEGATES to the canonical
``openfde.plugins.webxr_summary`` (one implementation, identical response shape; no duplicated logic).

Cheap to import: this module pulls in no heavy language-pack / assimilation code, and the delegate
import is deferred to call time so there is no import cycle with ``openfde.plugins``.
"""
from __future__ import annotations


def make_runtime(root=None):
    """Factory → the WebXR runtime hooks. ``domain_summary(root)`` returns the bounded architecture
    summary, delegating to the core implementation so the API/UI shape never changes."""
    def domain_summary(repo_root=None):
        from openfde.plugins import webxr_summary    # deferred: no import cycle, no heavy imports
        return webxr_summary(repo_root if repo_root is not None else root)

    return {"domain_summary": domain_summary}
