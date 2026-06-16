"""ofde_sample_plugin — a REFERENCE external OpenFDE plugin (Plugin v1-L).

A tiny, dependency-free package used ONLY in tests to prove the plug-and-play contract end to end.
It is NOT installed and NOT a product: the tests put ``tests/fixtures`` on ``sys.path`` and monkeypatch
``importlib.metadata.entry_points`` to point at it, simulating a pip-installed package that contributes
an entry point. It also doubles as the **plugin-author contract** (copy this shape for a real plugin).

──────────────────────────────────────────────────────────────────────────────
THE PLUGIN AUTHOR CONTRACT
──────────────────────────────────────────────────────────────────────────────

1. PACKAGING — ship a normal Python package and declare an entry point in ONE of these groups
   (pyproject ``[project.entry-points."openfde.domain_packs"]`` etc.):
       openfde.plugins | openfde.language_packs | openfde.domain_packs
   The entry-point value is ``your_pkg.module:provider`` — a MANIFEST PROVIDER (see #2).

2. MANIFEST PROVIDER (``plugin.py`` here) — a lightweight callable / object / dict that returns the
   manifest. It is loaded during LISTING, so keep it cheap: metadata + a cheap probe + a STRING
   runtime pointer. It must NOT import the runtime (so listing never loads heavy code). Manifest shape:
       {
         "id": "<short id>",            # ^[A-Za-z0-9_.-]{1,80}$ ; must not equal a built-in id
         "kind": "<one of PLUGIN_KINDS>",   # language_pack | domain_pack | verify_adapter | …
         "displayName": "…", "version": "…", "description": "…",
         "capabilities": ["domain_summary", …],   # the runtime hooks this pack provides (#4)
         "detects": { … },             # cheap activation probe (#3); omit → "declared, always on"
         "runtime": {"module": "your_pkg.runtime", "factory": "make_runtime"},  # STRING pointer (#4)
       }

3. ACTIVATION / PROBE — ``detects`` is marker-only and bounded (NO assimilation, NO code import):
       {"dependencies": ["pkg", …]}   # names in package.json
       {"files": ["**/*.ext", …]}     # glob(s) present in the repo
       {"content": ["marker", …]}     # substrings in a bounded source scan
   A provider is ACTIVE for a repo only when its probe matches; its runtime loads only then (#4).

4. RUNTIME (``runtime.py`` here) — imported LAZILY by the activation API, never during listing. The
   ``factory`` (``make_runtime(root=None)``) returns a dict/object of capability HOOKS, e.g.
       {"domain_summary": fn, "architecture": fn, "test_detector": fn, "failure_parser": fn,
        "repro_drafter": fn}
   A hook is called by the consuming product path; return plain JSON-able data. The product path
   defensively validates hook output and falls back on a bad/throwing hook — never trust blindly.

5. SAFETY (non-negotiable):
   • A repo-LOCAL manifest (``.openfde/plugins/*.json``) may declare metadata + a probe but can NEVER
     declare a ``runtime`` — opening a repo must never import code. Only a TRUSTED source (a built-in
     pointer, or an entry point from an installed Python package) may carry a runtime.
   • External plugins are trusted only ONCE the user has pip-installed the package. OpenFDE does not
     download or install anything: there is no marketplace, no network, no ``pip install`` from the app.
     Install/distribution is DEFERRED — v1-L proves the contract, not distribution.
"""
