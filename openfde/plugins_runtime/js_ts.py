"""openfde/plugins_runtime/js_ts.py — TRUSTED built-in JS/TS runtime (Plugin v1-J).

The JS/TS language pack's EXISTING capabilities, exposed as plugin-runtime HOOKS so the plugin
contract is real end-to-end — not new intelligence, just the shipped implementation behind the
capability seam. The activation API imports this LAZILY, only when a JS/TS repo asks for a
capability; importing the module pulls in no heavy code (the architect + pack are imported inside
the factory, at activation).

Hooks (each delegates to the existing impl, shape unchanged):
  • architecture(root)        -> architect._analyze_repo_core(root)  (the IN-CORE analyzer — NOT the
    v1-K dispatcher, so the dispatcher→hook→analyzer path never recurses; tree-sitter when installed,
    else regex)
  • test_detector(root)       -> JsTsPack().discover_checks(root) as [check.as_dict()]
  • failure_parser(out, root) -> JsTsPack().parse_failures(out, root) as [loc.as_dict()]
  • repro_drafter(root)       -> JsTsPack().repro_context(root) + an honest "drafting deferred" marker

This is a TRUSTED built-in pointer, wired in openfde.plugins for the js_ts built-in spec. A repo-local
manifest can never point here — that path is blocked in the manifest normalizer (security).
"""
from __future__ import annotations


def make_runtime(root=None):
    """Build the JS/TS runtime hooks. Imports the architect + pack lazily (here, at activation) so
    importing this module — or listing plugins — stays cheap."""
    from openfde import architect
    from openfde.language_packs import JsTsPack

    def architecture(repo_root=None):
        """The repo's ArchGraph (modules/files/functions/edges/flows/fileEdges/warnings) — same shape +
        source of truth as the canvas. Delegates to the IN-CORE analyzer (``_analyze_repo_core``), NOT
        the v1-K dispatcher, so dispatcher→hook→analyzer never recurses. tree-sitter when available,
        else the regex fallback."""
        return architect._analyze_repo_core(repo_root if repo_root is not None else root)

    def test_detector(repo_root=None):
        """Discovered JS/TS checks (Vitest/Jest/Playwright/test scripts) as the standard check dicts
        (``{id, label, command, cwd, required}``), so the consume seam reconstructs them losslessly."""
        checks = JsTsPack().discover_checks(repo_root if repo_root is not None else root)
        return [c.as_dict() for c in (checks or [])]

    def failure_parser(output, repo_root=None):
        """Structured JS/TS failure locations ({file, line, func, test}) parsed from test output."""
        locs = JsTsPack().parse_failures(output or "", repo_root if repo_root is not None else root)
        return [loc.as_dict() for loc in (locs or [])]

    def repro_drafter(repo_root=None):
        """Repro CONTEXT (language/framework/test command/conventions). Honest boundary: actual
        JS/TS repro DRAFTING is not implemented yet (pytest-only today) — the seam is explicit."""
        return {
            "context": JsTsPack().repro_context(repo_root if repo_root is not None else root),
            "drafting": "deferred — JS/TS repro drafting is not implemented (pytest-only today)",
        }

    return {
        "architecture": architecture,
        "test_detector": test_detector,
        "failure_parser": failure_parser,
        "repro_drafter": repro_drafter,
    }
