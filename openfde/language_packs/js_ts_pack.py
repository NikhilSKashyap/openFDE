"""
openfde/language_packs/js_ts_pack.py — Pack #2: JavaScript / TypeScript
(L1-A/B/C shipped, all regex / dependency-free).

The assimilation + verify + repro SEAMS for Node repos. This pack can:

  • detect a Node/JS/TS repo (a ``package.json``, or ``*.js/*.jsx/*.ts/*.tsx`` and
    the ``.mjs/.cjs/.mts/.cts`` variants outside vendor/build dirs),
  • build a real ArchGraph (``build_arch_graph`` routes to ``architect.analyze_repo``
    — the canvas's source of truth): modules, files, functions (declarations,
    exported/arrow forms with TS annotations, classes, methods, **object methods**,
    React components), import edges, and function-level flows (same-file **high**,
    resolved relative-import **medium**), with config / ``.d.ts`` / story files
    filtered out of symbol mining (L1-C noise control),
  • discover a conservative, deterministic test command from ``package.json``'s
    scripts + the lockfile's package manager (Vitest / Jest / **Playwright**),
  • hand back a repro context (language, framework, test command, file
    conventions) for the drafter seam,
  • parse common **Vitest** / **Jest** / **Playwright** failure output into the SAME
    ``{file, line, func, test}`` shape the Python pack produces — honestly: when
    the output gives no in-repo file+line, it returns NO locations rather than
    guessing — and feed the **failure-flow lens** (L1-C): a failing JS/TS test maps
    to its connected implementation (``architect.js_call_context``).

Parser (L1-D): tree-sitter is the PREFERRED precise path for ArchGraph symbol + import
extraction. When the optional ``tree-sitter`` + JS/TS grammars are installed
(``pip install "openfde[treesitter]"``), ``analyze_repo`` parses from a real AST; without
them it falls back to the built-in REGEX path (still dependency-free, covering the forms
common real repos use), and the ArchGraph warnings name which path ran. OpenFDE surfaces a
recommendation to install the grammars when a JS/TS repo lacks them (approval-gated; see
``plugins.treesitter_recommendation``) — but tree-sitter is OPTIONAL, never a hard dependency.
UNCHANGED by L1-D: the function-level flows, the failure-flow resolver
(``architect.js_call_context``), and repro drafting all stay regex/heuristic, and issue_repro's
drafter stays pytest-only (a non-Python target stops cleanly instead of getting a fabricated
Python test). Still NOT here: deep TS type graph, Node test-impact analysis, big-repo scoped
assimilation. No ``npm install``.

Imports of the wrapped modules are LAZY (inside methods) so the language_packs
package has no import cycle with verify/issue_repro/server/architect.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .base import FailureLocation, VerifyCheckSpec

logger = logging.getLogger("openfde.language_packs.js_ts")

# Extensions that make a repo "JS/TS". `.d.ts` (pure type declarations) is not code
# under test and is excluded from detection.
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_TS_EXTS = (".ts", ".tsx", ".mts", ".cts")

# Vendor/build/output dirs a JS/TS repo must never be assimilated or located into.
_SKIP_DIRS = {".git", ".openfde", "node_modules", "dist", "build", ".next", "out",
              "coverage", ".nuxt", ".svelte-kit", ".turbo", ".cache", ".parcel-cache",
              ".vercel", ".output", "__pycache__"}

# Scripts we prefer to run, most-specific first. "test:unit" is the surest unit
# signal; "test" is the conventional entry; bare "vitest"/"jest" scripts are common.
# The e2e/playwright scripts come LAST — unit tests are the default gate; a
# playwright-only repo still discovers a check (but browsers may need install).
_SCRIPT_PRIORITY = ("test:unit", "test", "vitest", "jest",
                    "test:e2e", "e2e", "test:playwright", "playwright")

_MAX_FAILURES = 5

_FILE_RE = r"(\S+?\.(?:[cm]?[jt]sx?))"          # a path ending .js/.jsx/.ts/.tsx/.mjs…

# Vitest per-test failure header: " FAIL  src/a.test.ts > suite > it name"
_VITEST_FAIL_RE = re.compile(rf"^\s*FAIL\s+{_FILE_RE}\s*>\s*(.+?)\s*$", re.M)
# Jest file-level header: " FAIL  src/a.test.js"  (no chain on the line)
_JEST_FAILFILE_RE = re.compile(rf"^\s*FAIL\s+{_FILE_RE}\s*$", re.M)
# Jest per-test bullet: "  ● suite › it name"
_JEST_BULLET_RE = re.compile(r"^\s*●\s+(.+?)\s*$", re.M)
# Vitest stack pointer line, glyph-agnostic: a single leading ornament (❯, ×, …),
# an optional function, then path:line(:col).  " ❯ src/a.test.ts:8:19"
_VITEST_LOC_RE = re.compile(
    rf"^\s*[^\w\s/.]\s+(?:(\S[^\n]*?)\s+)?{_FILE_RE}:(\d+)(?::\d+)?", re.M)
# Node/Jest stack frame: "at fn (src/a.test.js:8:15)" or "at src/a.test.js:8:15"
_NODE_FRAME_RE = re.compile(
    rf"\bat\s+(?:(.+?)\s+\()?{_FILE_RE}:(\d+)(?::\d+)?\)?", re.M)
# Playwright per-failure header: "  1) [chromium] › e2e/a.spec.ts:6:3 › test name"
# (the project tag and the leading number are both optional in trimmed output).
_PW_FAIL_RE = re.compile(
    rf"^\s*\d+\)\s+(?:\[[^\]]+\]\s+[›>]\s+)?{_FILE_RE}:(\d+):\d+\s+[›>]\s+"
    r"(.+?)(?:\s+[=─-]{3,})?\s*$", re.M)


# ── repo facts ───────────────────────────────────────────────────────────────

def _iter_files(root):
    """Filenames under root, pruning vendor/build and hidden dirs."""
    for _dirpath, dirnames, filenames in os.walk(Path(root)):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        yield from filenames


def _has_ext(root, exts) -> bool:
    try:
        for f in _iter_files(root):
            if f.endswith(exts) and not f.endswith(".d.ts"):
                return True
    except OSError:
        return False
    return False


def _read_package_json(root) -> dict:
    try:
        data = json.loads((Path(root) / "package.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _detect_pm(root) -> str:
    """Package manager from the lockfile (deterministic; defaults to npm)."""
    root = Path(root)
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    return "npm"


def _pick_script(scripts) -> str:
    for name in _SCRIPT_PRIORITY:
        if name in scripts:
            return name
    return ""


def _has_dep(name, pkg) -> bool:
    deps = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
    return name in deps


def _needs_vitest_run_flag(name, scripts) -> bool:
    """Vitest defaults to WATCH mode — a check must force a single run. True when
    the chosen script runs vitest and doesn't already pin run-mode."""
    cmd = str(scripts.get(name, ""))
    runs_vitest = name == "vitest" or bool(re.search(r"\bvitest\b", cmd))
    already_run = bool(re.search(r"\bvitest\s+run\b", cmd)) or "--run" in cmd
    return runs_vitest and not already_run


def _infer_framework(scripts, pkg, script_name) -> str:
    cmd = str(scripts.get(script_name, "")) if script_name else ""
    if script_name == "vitest" or re.search(r"\bvitest\b", cmd) or _has_dep("vitest", pkg):
        return "vitest"
    if script_name == "jest" or re.search(r"\bjest\b", cmd) or _has_dep("jest", pkg):
        return "jest"
    if (script_name in ("playwright", "test:playwright", "test:e2e", "e2e")
            or re.search(r"\bplaywright\b", cmd)
            or _has_dep("@playwright/test", pkg) or _has_dep("playwright", pkg)):
        return "playwright"
    if re.search(r"\bnode\s+--test\b", cmd) or "node:test" in cmd:
        return "node-test"
    return "node-test"          # honest neutral default (Node's built-in runner)


def _discover_spec(root):
    """The single JS/TS check for this repo, or None when there's no test script.

    Conservative + deterministic: ``<pm> run <script>`` for the highest-priority
    script that exists, with the ONE sanctioned exception — forcing vitest out of
    watch mode (``-- --run`` for npm/pnpm, ``--run`` for yarn/bun). No other flags
    are invented.
    """
    pkg = _read_package_json(root)
    scripts = pkg.get("scripts") or {}
    name = _pick_script(scripts)
    if not name:
        return None
    pm = _detect_pm(root)
    cmd = [pm, "run", name]
    if _needs_vitest_run_flag(name, scripts):
        cmd += (["--", "--run"] if pm in ("npm", "pnpm") else ["--run"])
    return VerifyCheckSpec(id="js-tests", label="JS/TS tests", command=cmd,
                           cwd="", required=True, reporter="text")


# ── failure parsing (Vitest / Jest v1) ───────────────────────────────────────

def _loc_in_repo(root_s, fpath):
    """Repo-relative path for an in-repo frame, or None for vendor/out-of-repo."""
    from openfde.verify import _in_repo
    rel = _in_repo(root_s, fpath)
    if rel is None or rel.split("/", 1)[0] in _SKIP_DIRS:
        return None
    return rel


def _clean_func(func) -> str:
    func = (func or "").strip()
    return "" if (not func or "<anonymous>" in func) else func


def _js_test_name(chain) -> str:
    """Bare test name from a "suite > it" (vitest) / "suite › it" (jest) chain."""
    parts = re.split(r"\s+[>›]\s+", (chain or "").strip())
    return parts[-1].strip() if parts else (chain or "").strip()


def _first_in_repo_js_loc(block, root_s):
    """The FIRST in-repo {file,line,func} in a failure block (the assertion site),
    skipping node_modules / vendor frames. None when the block names no in-repo
    file+line — honesty over a guessed location."""
    cands = []
    for m in _VITEST_LOC_RE.finditer(block):
        cands.append((m.start(), m.group(1), m.group(2), int(m.group(3))))
    for m in _NODE_FRAME_RE.finditer(block):
        cands.append((m.start(), m.group(1), m.group(2), int(m.group(3))))
    cands.sort(key=lambda c: c[0])
    for _pos, func, fpath, line in cands:
        rel = _loc_in_repo(root_s, fpath)
        if rel is not None:
            return {"func": _clean_func(func), "file": rel, "line": line}
    return None


def _parse_js_failures(output, root) -> list:
    """Vitest/Jest output → ``[{test, file, line, func}]`` (capped, deduped).

    Anchors are per-test failure markers — vitest's ``FAIL file > chain`` lines,
    else jest's ``● chain`` bullets. Each anchor's block contributes one location
    (its first in-repo frame). Unknown formats yield ``[]``.
    """
    text = output or ""
    root_s = str(Path(root).resolve())
    # (start, test-chain, anchor_file|None, anchor_line|None). Only one runner's
    # markers appear in a given output, tried most-structured first.
    anchors = [(m.start(), m.group(2), None, None) for m in _VITEST_FAIL_RE.finditer(text)]
    if not anchors:                              # jest "● chain" bullets
        anchors = [(m.start(), m.group(1), None, None) for m in _JEST_BULLET_RE.finditer(text)]
    if not anchors:                              # playwright "N) … › file:line › name"
        anchors = [(m.start(), m.group(3), m.group(1), int(m.group(2)))
                   for m in _PW_FAIL_RE.finditer(text)]
    anchors.sort(key=lambda a: a[0])
    if not anchors:
        return []
    bounds = [a[0] for a in anchors] + [len(text)]
    out, seen = [], set()
    for i, (start, chain, afile, aline) in enumerate(anchors):
        loc = _first_in_repo_js_loc(text[start:bounds[i + 1]], root_s)
        if loc is None and afile is not None:    # playwright anchor carries file:line
            rel = _loc_in_repo(root_s, afile)
            if rel is not None:
                loc = {"func": "", "file": rel, "line": aline}
        if loc is None:
            continue
        key = f"{loc['file']}:{loc['line']}"
        if key in seen:
            continue
        seen.add(key)
        test = _js_test_name(chain)
        out.append({"test": test, "file": loc["file"], "line": loc["line"],
                    "func": loc["func"] or test})
        if len(out) >= _MAX_FAILURES:
            break
    return out


# ── the pack ─────────────────────────────────────────────────────────────────

class JsTsPack:
    """LanguagePack for JavaScript/TypeScript (see base.LanguagePack). L1-A/B/C
    shipped (regex, dependency-free): architecture assimilation (modules/files/
    functions/object-methods/flows) + verify discovery + Vitest/Jest/Playwright
    failure parsing + the failure-flow lens + repro context. Tree-sitter and
    automatic JS/TS repro drafting remain L1-D / Next (see the module docstring)."""
    name = "js_ts"
    file_globs = ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs", "*.mts", "*.cts")

    def detects(self, root) -> bool:
        return (Path(root) / "package.json").is_file() or _has_ext(root, _JS_EXTS)

    def build_arch_graph(self, root) -> dict:
        # Route to OpenFDE's repo assimilation (``architect.analyze_repo``) — the
        # SAME ArchGraph the canvas renders — so a JS/TS repo gets real modules,
        # files, functions (incl. classes, methods, object methods), import edges,
        # function-level flows (same-file high, resolved relative-import medium), and
        # HTML/web-app entrypoint edges (L1-D-A). This returns the architect ArchGraph
        # shape (modules/files/functions/edges/flows/fileEdges/warnings), the canvas's
        # source of truth — distinct from the semantic_graph provider shape.
        # Extraction is regex-based; tree-sitter is L1-D (the warnings name that
        # boundary honestly). Lazy import avoids cycles.
        from openfde import architect
        return architect.analyze_repo(Path(root))

    def discover_checks(self, root) -> list:
        root = Path(root)
        if (root / ".openfde" / "verify.json").exists():
            # An explicit config wins — reuse verify's validated parser, exactly
            # like the Python pack, so configured behavior is byte-for-byte unchanged.
            from openfde import verify
            return [VerifyCheckSpec.from_dict(c) for c in verify.discover_checks(root)]
        spec = _discover_spec(root)
        return [spec] if spec is not None else []

    def parse_failures(self, output: str, root) -> list:
        return [FailureLocation.from_dict(d) for d in _parse_js_failures(output, root)]

    def repro_context(self, root=None) -> dict:
        conventions = ["*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx",
                       "*.spec.ts", "*.spec.js", "__tests__/"]
        if root is None:
            return {"framework": "node-test", "language": "javascript",
                    "test_command": ["npm", "run", "test"],
                    "test_conventions": conventions}
        root = Path(root)
        pkg = _read_package_json(root)
        scripts = pkg.get("scripts") or {}
        name = _pick_script(scripts)
        spec = _discover_spec(root)
        return {"framework": _infer_framework(scripts, pkg, name),
                "language": "typescript" if _has_ts(root) else "javascript",
                "test_command": list(spec.command) if spec is not None
                else [_detect_pm(root), "run", "test"],
                "test_conventions": conventions}

    def ensure_check_config(self, root) -> None:
        """Pin the discovered JS/TS check as ``.openfde/verify.json`` so the repo's
        own 'Run checks' runs it. Idempotent; pins nothing when there's no test
        script (we don't invent a runner)."""
        try:
            cfg = Path(root) / ".openfde" / "verify.json"
            if cfg.exists():
                return
            spec = _discover_spec(root)
            if spec is None:
                return
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps([spec.as_dict()], indent=2), encoding="utf-8")
            logger.info("js/ts pack: pinned a JS/TS test check (.openfde/verify.json)")
        except OSError:
            pass


def _has_ts(root) -> bool:
    return (Path(root) / "tsconfig.json").is_file() or _has_ext(root, _TS_EXTS)
