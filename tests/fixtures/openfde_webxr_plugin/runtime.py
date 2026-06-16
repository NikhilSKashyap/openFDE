"""Runtime — imported LAZILY by the activation API, only when ``domain_summary`` is requested for a
WebXR-matching repo. ``make_runtime`` returns the capability hook.

For this v1-M skeleton the hook REUSES core's bounded ``webxr_summary`` (same {detected, entrypoints,
assets, frameworks, markers, fileBadges, warnings} shape), so ``/api/plugins/webxr/summary`` is
byte-identical whether served from core or this package. A real ``openfde-webxr`` package would OWN
this bounded summary; the delegate import is deferred to call time (no import cycle, listing stays
cheap)."""


def make_runtime(root=None):
    def domain_summary(repo_root=None):
        from openfde.plugins import webxr_summary   # deferred: reuse core's bounded WebXR summary
        return webxr_summary(repo_root if repo_root is not None else root)

    return {"domain_summary": domain_summary}
