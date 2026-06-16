"""Runtime — imported LAZILY by the activation API, never during listing. A real plugin's heavy
analyzer lives here, behind this lazy boundary; ``make_runtime`` returns the capability hooks."""

LOADED = True   # a sentinel for tests; `runtime` in sys.modules is the real proof of lazy loading


def make_runtime(root=None):
    """Return this plugin's capability hooks. Called only when a matching repo requests a capability."""

    def domain_summary(repo_root=None):
        return {
            "ok": True,
            "provider": "sample-pack",
            "summary": "reference external domain_summary",
        }

    return {"domain_summary": domain_summary}
