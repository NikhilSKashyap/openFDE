"""
openfde/language_packs/python_pack.py — Pack #1: Python.

Wraps OpenFDE's existing Python/pytest seams behind the LanguagePack contract:
the architect AST graph, pytest/unittest verify discovery + failure parsing, and
the pytest repro context. Nothing about the behavior changes — this only moves the
Python knowledge behind one door so the next slice (structured reporters + a JS/TS
pack) is a drop-in. Imports of the wrapped modules are LAZY (inside methods) so the
language_packs package has no import cycle with verify/issue_repro/server.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .base import FailureLocation, VerifyCheckSpec

logger = logging.getLogger("openfde.language_packs.python")

# OpenFDE's standard pytest flags: -q compact output; --tb=short for the
# "path:line: in func" frames the failure parser reads best; no:cacheprovider so
# the verifier never writes .pytest_cache into the worktree (the watcher must not
# see the verifier's run as an edit).
_PYTEST_FLAGS = ("-q", "--tb=short", "-p", "no:cacheprovider")

# The runner prefix that actually executes pytest HERE, probed once per process.
# `python3 -m pytest` and the `pytest` CLI are NOT interchangeable: a machine can
# have one without the other (pytest on PATH but "No module named pytest" for the
# interpreter, or the reverse). Resolve to whichever truly runs so Pack #1 is
# environment-robust instead of hardcoding a shape that fails on half of them.
_resolved_pytest_base = None


def _pytest_runs(argv) -> bool:
    """True if ``argv --version`` exits 0 — pytest is genuinely runnable that way."""
    try:
        return subprocess.run([*argv, "--version"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve_pytest_base() -> list:
    """The argv prefix (no flags) that runs pytest in this environment. Prefers a
    working ``pytest`` CLI; else a working ``python3 -m pytest``; else falls back to
    ``python3 -m pytest`` so a genuinely missing pytest still fails loudly rather
    than masking the problem. Probed once and cached — the binary doesn't move."""
    global _resolved_pytest_base
    if _resolved_pytest_base is not None:
        return list(_resolved_pytest_base)
    cli = shutil.which("pytest")
    if cli and _pytest_runs([cli]):
        base = [cli]
    elif _pytest_runs(["python3", "-m", "pytest"]):
        base = ["python3", "-m", "pytest"]
    else:
        base = ["python3", "-m", "pytest"]  # honest fallback: let it fail loudly
    _resolved_pytest_base = base
    return list(base)


def resolve_pytest_cmd() -> list:
    """OpenFDE's full pytest command for this environment: the resolved runner
    (``pytest`` CLI or ``python3 -m pytest``) followed by OpenFDE's standard flags.
    Single source of truth for 'how do we run pytest here' — verify discovery, the
    repro context, and the pinned check config all route through this so the drafted
    repro is ALWAYS a pytest command that actually runs on the host."""
    return [*_resolve_pytest_base(), *_PYTEST_FLAGS]

_SKIP_DIRS = {".git", ".openfde", "node_modules", "__pycache__", ".venv", "venv",
              "env", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}


def _has_python(root) -> bool:
    """True if the repo contains at least one ``.py`` file (pruning vendor dirs)."""
    root = Path(root)
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            if any(f.endswith(".py") for f in filenames):
                return True
    except OSError:
        return False
    return False


def _ensure_pytest_check(root) -> None:
    """Persist a pytest check as ``.openfde/verify.json`` so the repo's own 'Run
    checks' runs the repro test. Idempotent: never overwrites an existing config."""
    try:
        cfg = Path(root) / ".openfde" / "verify.json"
        if cfg.exists():
            return
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps([{
            "id": "unit-tests", "label": "Unit tests",
            "command": resolve_pytest_cmd(), "required": True,
        }], indent=2), encoding="utf-8")
        logger.info("python pack: pinned a pytest check (.openfde/verify.json)")
    except OSError:
        pass


class PythonPack:
    """LanguagePack for Python (see openfde.language_packs.base.LanguagePack)."""
    name = "python"
    file_globs = ("*.py",)

    def detects(self, root) -> bool:
        return _has_python(root)

    def build_arch_graph(self, root) -> dict:
        from openfde import semantic_graph
        return semantic_graph.build_graph(root)

    def discover_checks(self, root) -> list:
        from openfde import verify
        return [VerifyCheckSpec.from_dict(c) for c in verify.discover_checks(root)]

    def parse_failures(self, output: str, root) -> list:
        from openfde import verify
        return [FailureLocation.from_dict(f)
                for f in verify.parse_failure_locations(output, root)]

    def repro_context(self, root=None) -> dict:
        # root is accepted for signature parity with JsTsPack (Python's context is
        # environment-, not repo-, derived) so callers can pass it uniformly.
        return {"framework": "pytest", "language": "python",
                "test_command": resolve_pytest_cmd()}

    def ensure_check_config(self, root) -> None:
        _ensure_pytest_check(root)
