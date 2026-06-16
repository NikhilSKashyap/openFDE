"""openfde_webxr_plugin — a REFERENCE skeleton for WebXR as an EXTERNAL OpenFDE plugin (Plugin v1-M).

Shows the shape a future ``openfde-webxr`` pip package would take, so WebXR can live OUTSIDE core
without bloating it: an entry-point MANIFEST PROVIDER (``plugin.py``) + a LAZY runtime (``runtime.py``)
exposing the ``domain_summary`` capability. It is a TEST FIXTURE — not installed, not a real package;
tests put ``tests/fixtures`` on ``sys.path`` and monkeypatch ``entry_points`` to reach it, simulating a
pip-installed ``openfde-webxr``.

De-dup: it uses id ``"webxr"``, so OpenFDE's existing precedence (built-in > local > external >
suggestion) collapses it with the built-in suggestion / an enabled local manifest to exactly ONE WebXR
row — no special case. WebXR therefore stays available WITHOUT this package (the built-in suggestion +
core runtime); when the package is present, the same WebXR row is served from here instead.

Honest boundary: a package SKELETON / reference shape, NOT a marketplace or install flow. For this
skeleton the runtime REUSES core's bounded ``webxr_summary`` (a real ``openfde-webxr`` would own that
logic). External plugins are trusted only once pip-installed; OpenFDE downloads/installs nothing.
"""
