"""
openfde/architect.py — OpenArchitect read-only repo analysis.

Analyzes a repository and returns an ArchGraph containing:
  - Module structure  (top-level directories and standalone scripts)
  - File metadata     (language, size)
  - Function / class definitions  (Python via ast; JS/TS via regex)
  - Import-level dependency edges between modules

This is read-only.  No code generation, no agent execution.
"""

import ast
import bisect
import logging
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

logger = logging.getLogger("openfde.architect")

# ─── Exclusion lists ─────────────────────────────────────────────────────── #

_EXCLUDED_DIRS: frozenset = frozenset({
    ".git", ".openfde", "node_modules", "dist", "__pycache__",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox",
    ".eggs", ".cache", "build", "htmlcov", ".next", ".nuxt",
    "coverage", ".turbo", ".parcel-cache", ".ruff_cache",
})

# Well-known root-level config / lock files that are not standalone modules
_ROOT_SKIP_NAMES: frozenset = frozenset({
    ".gitignore", ".gitattributes", ".editorconfig",
    "pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in",
    "Makefile", "Dockerfile", "docker-compose.yml",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintignore",
    ".prettierrc", ".prettierignore",
    "vite.config.js", "vite.config.ts", "jest.config.js", "jest.config.ts",
    "tsconfig.json", "tsconfig.base.json",
    ".env", ".env.example",
    "LICENSE", "LICENCE", "NOTICE",
    "openfde.sh",
})

_LARGE_FILE_BYTES: int = 512 * 1_024   # skip files larger than 512 KB

# ─── Language map ─────────────────────────────────────────────────────────── #

_LANG_MAP: dict = {
    ".py":   "Python",
    ".js":   "JavaScript",
    ".jsx":  "JavaScript",
    ".mjs":  "JavaScript",
    ".cjs":  "JavaScript",
    ".ts":   "TypeScript",
    ".tsx":  "TypeScript",
    ".mts":  "TypeScript",
    ".cts":  "TypeScript",
    ".css":  "CSS",
    ".scss": "CSS",
    ".html": "HTML",
    ".htm":  "HTML",
    ".md":   "Markdown",
    ".json": "JSON",
    ".toml": "TOML",
    ".sh":   "Shell",
    ".bash": "Shell",
    ".yaml": "YAML",
    ".yml":  "YAML",
    ".rs":   "Rust",
    ".go":   "Go",
    ".java": "Java",
    ".rb":   "Ruby",
    ".cpp":  "C++",
    ".c":    "C",
    ".h":    "C",
}

# ─── Internal data structures ─────────────────────────────────────────────── #

@dataclass
class _Module:
    """Internal representation of a detected top-level module."""

    id: str            # "module:frontend"
    name: str          # "frontend"
    path: str          # "frontend" (relative to root)
    type: str          # "directory" | "file"
    file_count: int = 0
    languages: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict.

        Returns:
            dict — ArchGraph module object.
        """
        return {
            "id":        self.id,
            "name":      self.name,
            "path":      self.path,
            "type":      self.type,
            "fileCount": self.file_count,
            "languages": self.languages,
        }


@dataclass
class _FileNode:
    """Internal representation of a source file."""

    id: str         # "file:openfde/server.py"
    path: str       # "openfde/server.py"
    module_id: str  # "module:openfde"
    language: str   # "Python"
    size: int       # bytes

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict.

        Returns:
            dict — ArchGraph file object.
        """
        return {
            "id":       self.id,
            "path":     self.path,
            "moduleId": self.module_id,
            "language": self.language,
            "size":     self.size,
        }


@dataclass
class _FunctionNode:
    """Internal representation of a function, method, or class definition."""

    id: str               # "function:openfde/server.py:start"
    name: str             # "start"  or  "ConnectionManager.connect"
    path: str             # "openfde/server.py"
    module_id: str        # "module:openfde"
    line: int
    args: list            # [{"name": "repo_path", "type": "str"}, ...]
    returns: Optional[str]
    purpose: str          # first sentence of docstring, or ""
    warnings: list        # per-definition issues

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict.

        Returns:
            dict — ArchGraph function object.
        """
        return {
            "id":       self.id,
            "name":     self.name,
            "path":     self.path,
            "moduleId": self.module_id,
            "line":     self.line,
            "args":     self.args,
            "returns":  self.returns,
            "purpose":  self.purpose,
            "warnings": self.warnings,
        }


@dataclass
class _Edge:
    """Internal representation of a module-level import dependency."""

    id: str         # "edge:frontend->openfde"
    from_id: str    # "module:frontend"
    to_id: str      # "module:openfde"
    type: str       # "import"
    label: str      # "imports"
    confidence: str # "high" | "medium" | "low"

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict.

        Returns:
            dict — ArchGraph edge object.
        """
        return {
            "id":         self.id,
            "from":       self.from_id,
            "to":         self.to_id,
            "type":       self.type,
            "label":      self.label,
            "confidence": self.confidence,
        }


# ─── Public API ───────────────────────────────────────────────────────────── #

def analyze_repo(root: Path) -> dict:
    """Analyze a repository and return an ArchGraph.

    Detects top-level modules, collects file metadata, extracts
    function / class definitions, and resolves import-level dependencies
    between modules.

    Args:
        root: Path — absolute path to the repository root.

    Returns:
        dict — ArchGraph with keys: modules, files, functions, edges, warnings.

    Side effects:
        Logs a summary line at INFO level: module/file/function/edge/warning counts.
    """
    modules             = _detect_modules(root)
    module_by_id        = {m.id:   m for m in modules}
    module_by_path      = {m.path: m for m in modules}

    files, file_warns   = _collect_files(root, modules)
    _tally_module_stats(modules, files)

    functions, fn_warns       = _extract_functions(root, files)
    import_edges, edge_warns  = _detect_edges(root, files, module_by_id, module_by_path)

    file_dicts = [f.to_dict()  for f in files]
    func_dicts = [fn.to_dict() for fn in functions]

    # Function-level dataflow (Step 23): the new source of truth. Module/file
    # import edges become fallback only where no function flow exists.
    flows, flow_warns         = _extract_flows(root, file_dicts, func_dicts)
    module_rollups, file_rollups = _rollup_flows(flows)
    merged_edges              = _merge_module_edges(import_edges, module_rollups)
    file_edges                = _build_file_edges(file_rollups, file_dicts)

    # HTML/web-app entrypoints → referenced JS/TS modules (L1-D-A). Prepended so the
    # "page → module" hop reads first; module edges merge (existing pairs win).
    html_file_edges, html_mod_edges = _html_entry_edges(root, file_dicts)
    if html_file_edges:
        file_edges = html_file_edges + file_edges
        have = {(e["from"], e["to"]) for e in merged_edges}
        merged_edges = merged_edges + [e for e in html_mod_edges
                                       if (e["from"], e["to"]) not in have]

    all_warnings = file_warns + fn_warns + edge_warns + flow_warns

    logger.info(
        "ArchGraph: %d module(s), %d file(s), %d function(s), %d edge(s), "
        "%d flow(s), %d file-edge(s), %d warning(s)",
        len(modules), len(files), len(functions), len(merged_edges),
        len(flows), len(file_edges), len(all_warnings),
    )

    return {
        "modules":   [m.to_dict() for m in modules],
        "files":     file_dicts,
        "functions": func_dicts,
        "edges":     merged_edges,   # module-level, dataflow-preferred (backward compatible)
        "flows":     flows,          # function-level dataflow edges
        "fileEdges": file_edges,     # file-level rollups of function flows
        "warnings":  all_warnings,
    }


def generate_canvas_state(root: Path) -> tuple:
    """Generate canvas boxes and arrows from the repo's ArchGraph.

    Module boxes are placed in a deterministic grid (up to 3 columns,
    sorted alphabetically). Box IDs are derived from module paths so
    that regenerating the canvas yields the same IDs.

    Args:
        root: Path — absolute path to the repository root.

    Returns:
        tuple — (canvas_state: dict, graph: dict)
            canvas_state has keys 'boxes' and 'arrows'.
            graph is the raw ArchGraph dict from analyze_repo().
    """
    graph           = analyze_repo(root)
    boxes           = _layout_boxes(graph["modules"], graph["files"], graph["edges"])
    # Map by each box's moduleId (boxes are alphabetically sorted, modules are
    # not — never zip the two lists positionally).
    mod_id_to_box   = {b["moduleId"]: b for b in boxes}
    arrows          = _make_arrows(graph["edges"], mod_id_to_box)
    return {"boxes": boxes, "arrows": arrows}, graph


# ─── Module detection ─────────────────────────────────────────────────────── #

def _detect_modules(root: Path) -> list:
    """Detect top-level meaningful modules in the repository.

    A module is:
    - A top-level non-excluded, non-hidden directory, or
    - A top-level Python / JS / TS script file that is not a known
      configuration or metadata file.

    Args:
        root: Path — repository root.

    Returns:
        list[_Module] — detected modules, sorted: directories first, then
        files, each group sorted alphabetically.
    """
    dirs  = []
    files = []

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        logger.error("Cannot read repo root %s: %s", root, exc)
        return []

    for entry in entries:
        name = entry.name

        # Skip all hidden entries (starts with '.')
        if name.startswith("."):
            continue

        if entry.is_dir():
            # Skip excluded directories and egg-info artifacts
            if name in _EXCLUDED_DIRS or name.endswith(".egg-info"):
                continue
            dirs.append(_Module(
                id=f"module:{name}",
                name=name,
                path=name,
                type="directory",
            ))

        elif entry.is_file():
            # Only surface top-level Python / JS / TS / HTML source files, skipping
            # well-known config / lock files. HTML entrypoints (index.html and other
            # top-level pages) become boxes so the web-app story reads HTML → JS.
            if name in _ROOT_SKIP_NAMES:
                continue
            suffix = entry.suffix.lower()
            if suffix not in (".py", ".js", ".ts", ".html", ".htm"):
                continue
            files.append(_Module(
                id=f"module:{name}",
                name=name,
                path=name,
                type="file",
            ))

    return dirs + files


# ─── File collection ──────────────────────────────────────────────────────── #

def _collect_files(root: Path, modules: list) -> tuple:
    """Collect all source files that belong to each detected module.

    Skips excluded directories, hidden entries, files larger than
    _LARGE_FILE_BYTES, and files whose extension is not in _LANG_MAP.

    Args:
        root:    Path — repository root.
        modules: list[_Module] — detected modules.

    Returns:
        tuple — (files: list[_FileNode], warnings: list[str])
    """
    files:    list = []
    warnings: list = []

    for mod in modules:
        mod_abs = root / mod.path

        if mod.type == "file":
            size = _safe_size(mod_abs)
            lang = _LANG_MAP.get(mod_abs.suffix.lower(), "Unknown")
            files.append(_FileNode(
                id=f"file:{mod.path}",
                path=mod.path,
                module_id=mod.id,
                language=lang,
                size=size,
            ))
            continue

        # Directory module — walk recursively
        try:
            for abs_path in _walk_source_files(mod_abs):
                rel  = abs_path.relative_to(root).as_posix()
                size = _safe_size(abs_path)
                if size > _LARGE_FILE_BYTES:
                    warnings.append(f"skipped large file: {rel} ({size // 1024} KB)")
                    continue
                lang = _LANG_MAP.get(abs_path.suffix.lower(), "Unknown")
                files.append(_FileNode(
                    id=f"file:{rel}",
                    path=rel,
                    module_id=mod.id,
                    language=lang,
                    size=size,
                ))
        except OSError as exc:
            warnings.append(f"error reading module {mod.path}: {exc}")

    return files, warnings


def _walk_source_files(directory: Path):
    """Yield source files recursively, skipping excluded and hidden directories.

    Args:
        directory: Path — root directory to start from.

    Yields:
        Path — absolute path to each recognized source file.
    """
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return

    for entry in entries:
        if entry.is_dir():
            name = entry.name
            if name.startswith("."):
                continue
            if name in _EXCLUDED_DIRS or name.endswith(".egg-info"):
                continue
            yield from _walk_source_files(entry)
        elif entry.is_file():
            if entry.suffix.lower() in _LANG_MAP:
                yield entry


def _tally_module_stats(modules: list, files: list) -> None:
    """Update file_count and languages on each module in-place.

    Args:
        modules: list[_Module] — modules to update.
        files:   list[_FileNode] — all collected files.

    Returns:
        None
    """
    for mod in modules:
        mod_files   = [f for f in files if f.module_id == mod.id]
        lang_counts: dict = {}
        for f in mod_files:
            lang_counts[f.language] = lang_counts.get(f.language, 0) + 1
        mod.file_count = len(mod_files)
        mod.languages  = lang_counts


def _safe_size(path: Path) -> int:
    """Return file size in bytes, or 0 on error.

    Args:
        path: Path — file to stat.

    Returns:
        int — size in bytes.
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ─── Function / class extraction ──────────────────────────────────────────── #

def _extract_functions(root: Path, files: list) -> tuple:
    """Extract function and class definitions from Python and JS/TS files.

    Python: uses ast for full type-annotation and docstring extraction.
    JS/TS:  uses regex to detect obvious function declarations (v1).

    Args:
        root:  Path — repository root.
        files: list[_FileNode] — collected source files.

    Returns:
        tuple — (functions: list[_FunctionNode], warnings: list[str])
    """
    functions: list = []
    warnings:  list = []
    js_parsers: set = set()

    for f in files:
        abs_path = root / f.path
        if f.language == "Python":
            fns, warns = _extract_python_functions(abs_path, f.path, f.module_id)
            functions.extend(fns)
            warnings.extend(warns)
        elif f.language in ("JavaScript", "TypeScript"):
            if is_js_noise_file(f.path):       # config / *.d.ts / stories → file only
                continue
            fns = _extract_js_functions_ts(abs_path, f.path, f.module_id)   # precise AST (L1-D) …
            js_parsers.add("tree-sitter" if fns is not None else "regex")   # … else the regex path
            if fns is None:
                fns = _extract_js_functions(abs_path, f.path, f.module_id)
            functions.extend(fns)

    warnings.extend(_js_parser_warnings(js_parsers))
    return functions, warnings


# ── Python extraction ─────────────────────────────────────────────────────── #

def _extract_python_functions(abs_path: Path, rel_path: str, module_id: str) -> tuple:
    """Extract top-level functions, classes, and methods from a Python file.

    Uses ast.iter_child_nodes to stay at the top level; for classes also
    iterates direct child nodes to capture methods (not nested helpers).

    Args:
        abs_path:  Path — absolute path to the .py file.
        rel_path:  str  — repo-relative path (used in IDs and messages).
        module_id: str  — parent module ID.

    Returns:
        tuple — (functions: list[_FunctionNode], warnings: list[str])

    Side effects:
        None.
    """
    functions: list = []
    warnings:  list = []

    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
        tree   = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        warnings.append(f"syntax error in {rel_path}: {exc}")
        return functions, warnings
    except OSError:
        return functions, warnings

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = _py_func_to_node(node, rel_path, module_id)
            functions.append(fn)
            if not fn.purpose and not fn.name.startswith("_"):
                warnings.append(f"missing docstring: {rel_path}:{fn.name}()")

        elif isinstance(node, ast.ClassDef):
            cls_fn = _py_class_to_node(node, rel_path, module_id)
            functions.append(cls_fn)
            if not cls_fn.purpose and not cls_fn.name.startswith("_"):
                warnings.append(f"missing docstring: {rel_path}:{cls_fn.name}")

            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method = _py_func_to_node(item, rel_path, module_id, class_name=node.name)
                    functions.append(method)

    return functions, warnings


def _py_func_to_node(
    node,
    rel_path: str,
    module_id: str,
    class_name: Optional[str] = None,
) -> _FunctionNode:
    """Convert an ast FunctionDef / AsyncFunctionDef to a _FunctionNode.

    Args:
        node      — ast.FunctionDef or ast.AsyncFunctionDef.
        rel_path:  str — repo-relative file path.
        module_id: str — parent module ID.
        class_name: str | None — enclosing class name for methods.

    Returns:
        _FunctionNode
    """
    qualified = f"{class_name}.{node.name}" if class_name else node.name
    fn_id     = f"function:{rel_path}:{qualified}"

    # Positional + keyword args (skip 'self' / 'cls')
    args = []
    for arg in node.args.args:
        if arg.arg in ("self", "cls"):
            continue
        type_str: Optional[str] = None
        if arg.annotation:
            try:
                type_str = ast.unparse(arg.annotation)
            except Exception:  # noqa: BLE001
                pass
        args.append({"name": arg.arg, "type": type_str})

    # Return annotation
    returns: Optional[str] = None
    if node.returns:
        try:
            returns = ast.unparse(node.returns)
        except Exception:  # noqa: BLE001
            pass

    # Docstring — first non-empty sentence only
    purpose = ""
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        doc   = node.body[0].value.value.strip()
        first = doc.split("\n")[0].split(". ")[0]
        purpose = first.rstrip(".").strip()

    return _FunctionNode(
        id=fn_id,
        name=qualified,
        path=rel_path,
        module_id=module_id,
        line=node.lineno,
        args=args,
        returns=returns,
        purpose=purpose,
        warnings=[],
    )


def _py_class_to_node(node, rel_path: str, module_id: str) -> _FunctionNode:
    """Convert an ast ClassDef to a _FunctionNode representing the class.

    Args:
        node      — ast.ClassDef.
        rel_path:  str — repo-relative file path.
        module_id: str — parent module ID.

    Returns:
        _FunctionNode — with empty args and no return annotation.
    """
    fn_id   = f"function:{rel_path}:{node.name}"
    purpose = ""
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        doc   = node.body[0].value.value.strip()
        first = doc.split("\n")[0].split(". ")[0]
        purpose = first.rstrip(".").strip()

    return _FunctionNode(
        id=fn_id,
        name=node.name,
        path=rel_path,
        module_id=module_id,
        line=node.lineno,
        args=[],
        returns=None,
        purpose=purpose,
        warnings=[],
    )


# ── JS / TS extraction (regex, dependency-free — L1-B/C shipped) ────────────── #
#
# Dependency-free structural extraction. Comments and string/template literals are
# SCRUBBED first (blanked to same-length spaces, newlines kept) so no pattern ever
# matches inside them; then anchored regexes catch the common declaration forms and
# class bodies are brace-matched for their methods. This is the shipped regex
# assimilation (L1-B) + the L1-C additions (object methods, failure-flow call
# resolution, noise filtering); it covers the forms real JS/TS/React repos use, and
# the flows it feeds carry confidence reflecting the heuristic (high same-file,
# medium resolved import). The boundary (regex literals, computed/dynamic calls,
# deep TS) is what tree-sitter (L1-D / Next) fixes.


def _scrub_js(source: str) -> str:
    """Blank JS/TS comments and string/template literals (same length, newlines
    preserved) so structural scans never match inside them. Offsets and line numbers
    are unchanged — callers compute lines from match offsets. Regex literals are not
    special-cased (rare in the structural positions we scan); that is the L1-D edge."""
    out = list(source)
    i, n, state = 0, len(source), None      # state: 'line' 'block' or a quote char
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "/" and nxt == "/":
                out[i] = out[i + 1] = " "
                state, i = "line", i + 2
            elif c == "/" and nxt == "*":
                out[i] = out[i + 1] = " "
                state, i = "block", i + 2
            elif c in ("'", '"', "`"):
                out[i] = " "
                state, i = c, i + 1
            else:
                i += 1
        elif state == "line":
            if c == "\n":
                state = None
            else:
                out[i] = " "
            i += 1
        elif state == "block":
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                state, i = None, i + 2
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
        else:                               # inside a string / template literal
            if c == "\\":                   # blank the escape and its escaped char
                out[i] = " "
                if i + 1 < n and source[i + 1] != "\n":
                    out[i + 1] = " "
                i += 2
            elif c == state:                # closing quote
                out[i] = " "
                state, i = None, i + 1
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
    return "".join(out)


def _line_starts(text: str) -> list:
    """Offsets at which each line begins (index 0 → line 1)."""
    starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            starts.append(i + 1)
    return starts


def _line_of(line_starts: list, offset: int) -> int:
    """1-based line number for a character offset."""
    return bisect.bisect_right(line_starts, offset)


def _brace_block(scrubbed: str, start: int):
    """``(open_index, close_index)`` of the next balanced ``{ … }`` at/after
    ``start`` on scrubbed source (braces in strings/comments already blanked), or
    None. Used to bound a class body for method extraction."""
    i = scrubbed.find("{", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(scrubbed)):
        ch = scrubbed[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (i, j)
    return None


# Top-level declaration forms (column 0). Ordered specific→general; a name claimed
# by an earlier pattern is not re-emitted. The parenthesized arrow patterns allow a
# TS return-type annotation (`): T =>`) and a leading generic (`<T>(…) =>`); the last
# two cover the unparenthesized SINGLE-param arrow (`const f = x => …`, optional
# `async`). Requiring `=>` (never `>`/`>=`) keeps comparisons out; a single param
# can't carry a TS type without parens, so `x => …` is the only no-paren form.
_JS_DECL_PATTERNS: list = [
    re.compile(r"^export\s+default\s+(?:async\s+)?function\*?\s+(\w+)\s*[(<]", re.M),
    re.compile(r"^export\s+(?:async\s+)?function\*?\s+(\w+)\s*[(<]", re.M),
    re.compile(r"^(?:async\s+)?function\*?\s+(\w+)\s*[(<]", re.M),
    re.compile(r"^export\s+(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function\*?\s*\(", re.M),
    re.compile(r"^(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function\*?\s*\(", re.M),
    re.compile(r"^export\s+(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=;{]+?)?=>", re.M),
    re.compile(r"^(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=;{]+?)?=>", re.M),
    re.compile(r"^export\s+(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\w+\s*=>", re.M),
    re.compile(r"^(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\w+\s*=>", re.M),
]

# Class declaration (optionally exported / default / abstract).
_JS_CLASS_RE = re.compile(r"^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(\w+)", re.M)

# Inside a class body: a method (indented, optional modifiers, name, (params),
# optional return type, then `{`), or a class-field arrow (`handler = () => …`).
# Control-flow keywords are excluded so `if (…) {` is never read as a method.
_JS_METHOD_RE = re.compile(
    r"^[ \t]+(?:(?:public|private|protected|static|readonly|abstract|override|async|get|set)\s+)*"
    r"\*?\s*(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^={;]+?)?\{", re.M)
_JS_FIELD_ARROW_RE = re.compile(
    r"^[ \t]+(?:(?:public|private|protected|static|readonly)\s+)*"
    r"(\w+)\s*=\s*(?:async\s+)?(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=;{]+?)?=>", re.M)
_JS_METHOD_SKIP: frozenset = frozenset({
    "if", "for", "while", "switch", "catch", "return", "function", "do", "else",
    "with", "typeof", "await", "yield", "new", "void", "delete", "in", "of", "case",
    "constructor",
})

# A module-level object literal bound to a name: `const api = { … }`. Its method
# properties (a service / handler / store object) are real app symbols — but only
# the IMMEDIATE level, capped, so config/options objects and nested literals don't
# flood the canvas. (L1-C — conservative; non-named/inline objects stay L1-D.)
_JS_OBJ_DECL_RE = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*\{", re.M)
# Inside an object: a shorthand method `foo() {` or a function/arrow property
# `foo: () => …` / `foo: function`. Indentation is captured to keep only the
# immediate (shallowest) level.
_JS_OBJ_METHOD_RE = re.compile(
    r"^([ \t]+)(?:async\s+|get\s+|set\s+|\*\s*)*(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^={;]+?)?\{", re.M)
_JS_OBJ_PROP_FN_RE = re.compile(
    r"^([ \t]+)(\w+)\s*:\s*(?:async\s+)?(?:function\*?\s*\(|(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=;{]+?)?=>)", re.M)
_JS_OBJ_METHOD_CAP = 16

# Files whose SYMBOLS are config / build / type-declaration / story noise, not app
# code under test. Kept on the canvas as files, but not mined for functions — so the
# flow lens never targets a config object or a `.d.ts` shim.
_JS_NOISE_RE = re.compile(
    r"(?:^|/)(?:[\w.-]+\.config\.(?:[cm]?[jt]s)|[\w.-]+\.d\.[cm]?ts|"
    r"[\w.-]+\.stories\.(?:[cm]?[jt]sx?)|[\w.-]+\.stories\.mdx|"
    r"\.eslintrc\.(?:[cm]?js)|babel\.config\.(?:[cm]?js)|"
    r"(?:next|nuxt|svelte|astro|remix|tailwind|postcss|rollup|webpack|jest|vitest|"
    r"playwright|cypress)\.config\.(?:[cm]?[jt]s))$", re.I)
# A JS/TS test file (Vitest / Jest / Playwright conventions).
_JS_TEST_RE = re.compile(
    r"(?:^|/)(?:__tests__/.+|[\w.-]+\.(?:test|spec)\.(?:[cm]?[jt]sx?))$", re.I)


def is_js_noise_file(rel_path: str) -> bool:
    """True for JS/TS config / type-declaration / story files: real files, but their
    symbols are not app code under test, so they are not mined for functions."""
    return bool(_JS_NOISE_RE.search((rel_path or "").replace("\\", "/")))


def is_js_test_file(rel_path: str) -> bool:
    """True for JS/TS test files (``*.test.*``, ``*.spec.*``, ``__tests__/``)."""
    return bool(_JS_TEST_RE.search((rel_path or "").replace("\\", "/")))


def _extract_js_functions(abs_path: Path, rel_path: str, module_id: str) -> list:
    """Extract function, arrow, class, and method definitions from a JS/TS/JSX/TSX
    file (regex; comments/strings scrubbed first).

    Covers: function declarations (incl. export / default / async / generator),
    ``const|let|var`` arrow and function expressions (with TS return types and
    generics), class declarations, class methods, and class-field arrows. React
    components are these same forms, so they are covered. Nested arrows inside a
    function body are not captured (regex boundary; tree-sitter is L1-D / Next).

    Args:
        abs_path:  Path — absolute path to the source file.
        rel_path:  str  — repo-relative path (used in IDs).
        module_id: str  — parent module ID.

    Returns:
        list[_FunctionNode] — methods are named ``Class.method`` (like Python).
    """
    functions: list = []
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return functions

    scrubbed = _scrub_js(source)
    line_starts = _line_starts(scrubbed)
    seen: set = set()                       # top-level names already emitted

    def emit(qualified, line):
        functions.append(_FunctionNode(
            id=f"function:{rel_path}:{qualified}", name=qualified, path=rel_path,
            module_id=module_id, line=line, args=[], returns=None,
            purpose="", warnings=[]))

    # Top-level functions / arrows / function expressions.
    for pattern in _JS_DECL_PATTERNS:
        for m in pattern.finditer(scrubbed):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            emit(name, _line_of(line_starts, m.start()))

    # Classes and their methods. The class is a node (like a Python class); methods
    # are `Class.method` so they nest under the class on the canvas and land in the
    # flow resolver's method index.
    for cm in _JS_CLASS_RE.finditer(scrubbed):
        cls = cm.group(1)
        if cls not in seen:
            seen.add(cls)
            emit(cls, _line_of(line_starts, cm.start()))
        span = _brace_block(scrubbed, cm.end())
        if span is None:
            continue
        body_open, body_close = span
        body = scrubbed[body_open + 1:body_close]
        m_seen: set = set()
        for mre in (_JS_METHOD_RE, _JS_FIELD_ARROW_RE):
            for mm in mre.finditer(body):
                meth = mm.group(1)
                if meth in _JS_METHOD_SKIP or meth in m_seen:
                    continue
                m_seen.add(meth)
                emit(f"{cls}.{meth}", _line_of(line_starts, body_open + 1 + mm.start()))

    # Module-level named object literals → their immediate method properties as
    # `obj.method` (service / handler / store objects). Conservative: only the
    # shallowest indent level, capped, control-flow keywords excluded.
    for om in _JS_OBJ_DECL_RE.finditer(scrubbed):
        obj = om.group(1)
        span = _brace_block(scrubbed, om.start())
        if span is None:
            continue
        body_open, body_close = span
        body = scrubbed[body_open + 1:body_close]
        cand = []
        for mre in (_JS_OBJ_METHOD_RE, _JS_OBJ_PROP_FN_RE):
            for mm in mre.finditer(body):
                cand.append((len(mm.group(1)), mm.group(2), body_open + 1 + mm.start(2)))
        cand = [c for c in cand if c[1] not in _JS_METHOD_SKIP]
        if not cand:
            continue
        min_indent = min(c[0] for c in cand)
        o_seen: set = set()
        for indent, meth, off in cand:
            if indent != min_indent or meth in o_seen:
                continue
            o_seen.add(meth)
            if len(o_seen) > _JS_OBJ_METHOD_CAP:
                break
            emit(f"{obj}.{meth}", _line_of(line_starts, off))

    return functions


def _extract_js_functions_ts(abs_path: Path, rel_path: str, module_id: str):
    """OPTIONAL tree-sitter symbol extraction for one JS/TS file → list[_FunctionNode], or ``None``
    to signal the regex fallback (:func:`_extract_js_functions`). Lazy: imports tree-sitter only when
    the optional grammars are installed; a missing package or any parse error returns ``None``."""
    from openfde.language_packs import js_ts_treesitter as ts_adapter
    if not ts_adapter.available():
        return None
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    facts = ts_adapter.extract(source, rel_path)
    if facts is None:
        return None
    return [_FunctionNode(id=f"function:{rel_path}:{q}", name=q, path=rel_path,
                          module_id=module_id, line=ln, args=[], returns=None,
                          purpose="", warnings=[]) for q, ln in facts["functions"]]


def _js_parser_warnings(parsers: set) -> list:
    """One honest line naming the JS/TS symbol parser path used — tree-sitter when the optional
    grammars are installed, the regex fallback otherwise (or on a per-file parse error)."""
    if not parsers:
        return []
    if parsers == {"tree-sitter"}:
        return ["JS/TS symbols parsed with tree-sitter (precise AST)."]
    if parsers == {"regex"}:
        return ["JS/TS symbols parsed with the regex fallback (tree-sitter unavailable or a parse error)."]
    return ["JS/TS symbols parsed with tree-sitter where parseable, else the regex fallback."]


# ─── Import / dependency edges ────────────────────────────────────────────── #

def _detect_edges(
    root: Path,
    files: list,
    module_by_id: dict,
    module_by_path: dict,
) -> tuple:
    """Detect module-level import dependencies and return deduplicated edges.

    Python: resolves 'import x' and 'from x import y' via ast; maps the
    top-level package name to a known module.  Confidence: high.

    JS/TS: resolves 'import … from' and require() via regex; relative paths
    are traced back to their containing module.  Confidence: medium.

    Args:
        root:            Path — repository root.
        files:           list[_FileNode] — collected source files.
        module_by_id:   dict — module_id → _Module.
        module_by_path: dict — module.path → _Module.

    Returns:
        tuple — (edges: list[_Edge], warnings: list[str])
    """
    name_to_mod_id: dict = {m.name: m.id for m in module_by_id.values()}

    # Accumulate (from_id, to_id) → highest confidence seen
    pair_conf: dict = {}
    warnings:  list = []

    for f in files:
        abs_path   = root / f.path
        from_mod   = f.module_id

        if f.language == "Python":
            for imp in _parse_python_imports(abs_path):
                top = imp.split(".")[0]
                to_mod = name_to_mod_id.get(top)
                if to_mod and to_mod != from_mod:
                    key = (from_mod, to_mod)
                    if pair_conf.get(key) != "high":
                        pair_conf[key] = "high"

        elif f.language in ("JavaScript", "TypeScript"):
            specs = _parse_js_imports_ts(abs_path)          # tree-sitter (L1-D) …
            if specs is None:
                specs = _parse_js_imports(abs_path)         # … else the regex path
            for spec in specs:
                to_mod = _resolve_js_import(spec, f.path, module_by_path, name_to_mod_id)
                if to_mod and to_mod != from_mod:
                    key = (from_mod, to_mod)
                    if key not in pair_conf:
                        pair_conf[key] = "medium"

    # Build deduplicated Edge objects
    edges: list = []
    seen_ids: set = set()
    for (from_id, to_id), confidence in pair_conf.items():
        from_name = from_id.replace("module:", "")
        to_name   = to_id.replace("module:", "")
        edge_id   = f"edge:{from_name}->{to_name}"
        if edge_id in seen_ids:
            continue
        seen_ids.add(edge_id)
        edges.append(_Edge(
            id=edge_id,
            from_id=from_id,
            to_id=to_id,
            type="import",
            label="imports",
            confidence=confidence,
        ))

    return edges, warnings


def _parse_python_imports(abs_path: Path) -> list:
    """Parse import statements from a Python file using ast.

    Args:
        abs_path: Path — absolute path to the .py file.

    Returns:
        list[str] — imported module names (e.g. "os", "openfde.server").
    """
    imports: list = []
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
        tree   = ast.parse(source, filename=str(abs_path))
    except (SyntaxError, OSError):
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    return imports


# Static import: 'from … from "specifier"' covers both named and default forms
_RE_JS_FROM    = re.compile(r"""from\s+['"]([^'"]+)['"]""")
# CommonJS require
_RE_JS_REQUIRE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
# Dynamic import()
_RE_JS_DYN     = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""")


def _parse_js_imports(abs_path: Path) -> list:
    """Parse import / require specifiers from a JS / TS file via regex.

    Args:
        abs_path: Path — absolute path to the source file.

    Returns:
        list[str] — specifiers (relative paths or bare package names).
    """
    imports: list = []
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return imports

    for pattern in (_RE_JS_FROM, _RE_JS_REQUIRE, _RE_JS_DYN):
        for m in pattern.finditer(source):
            imports.append(m.group(1))

    return imports


def _parse_js_imports_ts(abs_path: Path):
    """OPTIONAL tree-sitter import specifiers for one JS/TS file → list[str], or ``None`` for the
    regex fallback (:func:`_parse_js_imports`). Lazy + safe, like the symbol path."""
    from openfde.language_packs import js_ts_treesitter as ts_adapter
    if not ts_adapter.available():
        return None
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    facts = ts_adapter.extract(source, abs_path.name)
    return facts["imports"] if facts is not None else None


def _resolve_js_import(
    specifier: str,
    source_rel: str,
    module_by_path: dict,
    name_to_mod_id: dict,
) -> Optional[str]:
    """Resolve a JS import specifier to a known module ID.

    Relative specifiers (e.g. '../api/backend') are resolved from the
    importing file's directory.  Bare specifiers (e.g. 'react') are
    matched against the top-level module names.

    Args:
        specifier:     str  — the imported path string.
        source_rel:    str  — repo-relative path of the importing file.
        module_by_path: dict — module.path → _Module.
        name_to_mod_id: dict — module name → module ID.

    Returns:
        str | None — module ID if resolved, else None.
    """
    if specifier.startswith("."):
        # Relative import — resolve against the importing file's directory
        source_dir = Path(source_rel).parent
        resolved   = (source_dir / specifier).as_posix()
        for mod_path, mod in module_by_path.items():
            if resolved == mod_path or resolved.startswith(mod_path + "/"):
                return mod.id
        return None
    else:
        # Bare specifier — match top-level name only
        top = specifier.split("/")[0]
        return name_to_mod_id.get(top)


# ─── Function-level dataflow (Step 23) ────────────────────────────────────── #
#
# Data flows at function level (one function calls another); those flows roll up
# to file-level and module-level edges. Import edges become fallback-only. The
# resolver is deliberately conservative — high-signal edges over noisy guesses.
# Python: ast-based (same-file, self-method, and `from x import fn` resolution).
# JS/TS: same-file heuristic at low confidence, with a warning.

_CONF_RANK: dict = {"high": 3, "medium": 2, "low": 1}
_ROLLUP_FLOW_CAP: int = 12   # underlying flow summaries kept per rollup edge


def _conf_max(a: str, b: str) -> str:
    """Return the higher-confidence label of two."""
    return a if _CONF_RANK.get(a, 0) >= _CONF_RANK.get(b, 0) else b


def _short_name(name: str) -> str:
    """Return the short callable name (drop an enclosing Class. prefix)."""
    return name.split(".")[-1]


def _extract_flows(root: Path, file_dicts: list, func_dicts: list) -> tuple:
    """Resolve function-level call flows across the collected files.

    Args:
        root:       Path — repository root.
        file_dicts: list[dict] — serialised file nodes (path, moduleId, language).
        func_dicts: list[dict] — serialised function nodes (id, name, path, …).

    Returns:
        tuple — (flows: list[dict], warnings: list[str]). Each flow has the
        Step-23 shape: id, fromFunctionId, toFunctionId, fromFile, toFile,
        fromModuleId, toModuleId, type, label, confidence, evidence.
    """
    callable_by_file_name: dict = {}   # (path, simple_name) -> fn_id  (funcs + classes)
    methods_by_file:       dict = {}   # (path, class, method) -> fn_id
    fn_by_id:              dict = {}
    module_by_file:        dict = {}

    for fn in func_dicts:
        fn_by_id[fn["id"]] = fn
        nm = fn["name"]
        if "." in nm:
            cls, meth = nm.split(".", 1)
            methods_by_file[(fn["path"], cls, meth)] = fn["id"]
        else:
            callable_by_file_name[(fn["path"], nm)] = fn["id"]

    for fl in file_dicts:
        module_by_file[fl["path"]] = fl["moduleId"]

    # Dotted python module name -> repo file path (for `from x import fn`).
    dotted_to_path: dict = {}
    for fl in file_dicts:
        if fl["language"] != "Python":
            continue
        p = fl["path"]
        no_ext = p[:-3] if p.endswith(".py") else p
        parts = no_ext.split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            dotted_to_path[".".join(parts)] = p

    flows:    dict = {}   # (owner_id, callee_id) -> flow dict (deduped)
    warnings: list = []
    js_seen = False

    # JS/TS cross-file resolution inputs: the set of known JS/TS files (for import
    # path resolution) and each file's named default export (for `import X from`).
    js_files = {fl["path"] for fl in file_dicts
                if fl["language"] in ("JavaScript", "TypeScript")}
    default_export_by_file = _js_default_exports(root, file_dicts) if js_files else {}

    for fl in file_dicts:
        abs_path = root / fl["path"]
        if fl["language"] == "Python":
            _py_flows(abs_path, fl["path"], fl["moduleId"], flows,
                      callable_by_file_name, methods_by_file, fn_by_id,
                      module_by_file, dotted_to_path)
        elif fl["language"] in ("JavaScript", "TypeScript"):
            js_seen = True
            _js_flows(abs_path, fl["path"], fl["moduleId"], flows, func_dicts,
                      fn_by_id, callable_by_file_name, module_by_file,
                      default_export_by_file, js_files)

    if js_seen:
        warnings.append(
            "JS/TS flows are regex-derived (no tree-sitter): same-file calls are "
            "high-confidence, resolved relative-import calls medium; dynamic, "
            "computed, and non-relative-import calls are not traced.")

    return list(flows.values()), warnings


def _resolve_importfrom(node, rel: str, dotted_to_path: dict):
    """Resolve a `from X import …` statement to a repo file path, or None.

    Handles absolute (`from auth.tokens import x`) and simple relative
    (`from .tokens import x`) forms.
    """
    if node.level and node.level > 0:
        base_parts = rel.split("/")[:-1]          # package dir of the importing file
        up = node.level - 1
        if up:
            base_parts = base_parts[:-up] if up <= len(base_parts) else []
        target = list(base_parts) + (node.module.split(".") if node.module else [])
        return dotted_to_path.get(".".join(target)) if target else None
    if not node.module:
        return None
    return dotted_to_path.get(node.module)


def _py_flows(abs_path, rel, module_id, flows,
              callable_by_file_name, methods_by_file, fn_by_id,
              module_by_file, dotted_to_path) -> None:
    """Extract Python function-call flows from one file into the `flows` dict."""
    try:
        tree = ast.parse(abs_path.read_text(encoding="utf-8", errors="replace"), filename=rel)
    except (SyntaxError, OSError):
        return

    # Local name -> (target_file, original_attr) for resolvable `from x import fn`.
    import_map: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            t_rel = _resolve_importfrom(node, rel, dotted_to_path)
            if not t_rel:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                import_map[alias.asname or alias.name] = (t_rel, alias.name)

    def add(owner_id, callee_id, conf, lineno):
        if owner_id == callee_id:
            return                                  # ignore self-recursion
        caller, callee = fn_by_id.get(owner_id), fn_by_id.get(callee_id)
        if not caller or not callee:
            return
        key = (owner_id, callee_id)
        existing = flows.get(key)
        if existing and _CONF_RANK.get(conf, 0) <= _CONF_RANK.get(existing["confidence"], 0):
            return
        flows[key] = {
            "id":             f"flow:{owner_id}->{callee_id}",
            "fromFunctionId": owner_id,
            "toFunctionId":   callee_id,
            "fromFile":       caller["path"],
            "toFile":         callee["path"],
            "fromModuleId":   module_by_file.get(caller["path"], module_id),
            "toModuleId":     module_by_file.get(callee["path"], callee["moduleId"]),
            "type":           "call",
            "label":          f"{_short_name(caller['name'])}() → {_short_name(callee['name'])}()",
            "confidence":     conf,
            "evidence":       f"line {lineno}: {_short_name(callee['name'])}()",
        }

    def resolve(call, class_name):
        func = call.func
        if isinstance(func, ast.Name):
            nm = func.id
            tid = callable_by_file_name.get((rel, nm))
            if tid:
                return tid, "high"               # same-file bare call
            if nm in import_map:
                t_rel, t_attr = import_map[nm]
                tid = callable_by_file_name.get((t_rel, t_attr))
                if tid:
                    return tid, "medium"         # imported function
            return None
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id == "self" and class_name:
                tid = methods_by_file.get((rel, class_name, func.attr))
                if tid:
                    return tid, "high"           # self.method()
        return None

    def scan(owner_id, body_node, class_name):
        for sub in ast.walk(body_node):
            if isinstance(sub, ast.Call):
                r = resolve(sub, class_name)
                if r:
                    add(owner_id, r[0], r[1], getattr(sub, "lineno", 0))

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scan(f"function:{rel}:{node.name}", node, None)
        elif isinstance(node, ast.ClassDef):
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scan(f"function:{rel}:{node.name}.{item.name}", item, node.name)


def _record_flow(flows, fn_by_id, module_by_file, default_module_id,
                 owner_id, callee_id, conf, lineno) -> None:
    """Insert (or upgrade) a function-flow edge; higher confidence wins on a dup
    pair. Same shape as the Python resolver so rollups/canvas are language-blind."""
    if owner_id == callee_id:
        return                                      # ignore self-recursion
    caller, callee = fn_by_id.get(owner_id), fn_by_id.get(callee_id)
    if not caller or not callee:
        return
    key = (owner_id, callee_id)
    existing = flows.get(key)
    if existing and _CONF_RANK.get(conf, 0) <= _CONF_RANK.get(existing["confidence"], 0):
        return
    flows[key] = {
        "id":             f"flow:{owner_id}->{callee_id}",
        "fromFunctionId": owner_id,
        "toFunctionId":   callee_id,
        "fromFile":       caller["path"],
        "toFile":         callee["path"],
        "fromModuleId":   module_by_file.get(caller["path"], default_module_id),
        "toModuleId":     module_by_file.get(callee["path"], callee["moduleId"]),
        "type":           "call",
        "label":          f"{_short_name(caller['name'])}() → {_short_name(callee['name'])}()",
        "confidence":     conf,
        "evidence":       f"line {lineno}: {_short_name(callee['name'])}()",
    }


def _js_body_end_line(scrubbed, line_starts, start_line, next_line) -> int:
    """End line of the def starting at ``start_line``: its body block's closing
    brace — the first ``{`` at paren-depth 0 within ``[start_line, next_line)``,
    brace-matched. Paren-depth tolerates object-typed params (``(a = {}) =>``).
    Returns ``start_line`` for an expression-bodied arrow (no block)."""
    start = line_starts[start_line - 1]
    bound = (line_starts[next_line - 1]
             if next_line and next_line - 1 < len(line_starts) else len(scrubbed))
    paren, body_open, i = 0, -1, start
    while i < bound:
        c = scrubbed[i]
        if c == "(":
            paren += 1
        elif c == ")":
            paren = paren - 1 if paren > 0 else 0
        elif paren == 0:
            if c == "{":
                body_open = i
                break
            if c == ";":                            # statement ends before any block
                break
        i += 1
    if body_open < 0:
        return start_line
    depth = 0
    for j in range(body_open, len(scrubbed)):
        c = scrubbed[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return _line_of(line_starts, j)
    return start_line


# `export default function Foo` / `export default class Foo` / `export default Foo`.
_RE_JS_DEFAULT_EXPORT = re.compile(
    r"^export\s+default\s+(?:async\s+)?(?:function\*?|class)\s+(\w+)"
    r"|^export\s+default\s+(\w+)\s*;?\s*$", re.M)
# ESM import with a RELATIVE specifier; CommonJS require with a relative specifier.
_RE_JS_IMPORT_STMT = re.compile(
    r"""import\s+(?!type[\s{])(?P<clause>[\w*\s,{}$]+?)\s+from\s+['"](?P<spec>\.[^'"]+)['"]""")
_RE_JS_REQUIRE_STMT = re.compile(
    r"""(?:const|let|var)\s+(?P<bind>\{[^}]*\}|[\w$]+)\s*=\s*require\(\s*['"](?P<spec>\.[^'"]+)['"]\s*\)""")
_JS_RESOLVE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _js_default_exports(root: Path, file_dicts: list) -> dict:
    """rel_path → the file's default-export NAME when it is a named function / class
    / const (`export default function Foo`, `export default class Foo`, `export
    default Foo`). Anonymous default exports are omitted (never guessed)."""
    out: dict = {}
    for fl in file_dicts:
        if fl["language"] not in ("JavaScript", "TypeScript"):
            continue
        try:
            src = _scrub_js((root / fl["path"]).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        m = _RE_JS_DEFAULT_EXPORT.search(src)
        if m:
            out[fl["path"]] = m.group(1) or m.group(2)
    return out


def _resolve_js_file(specifier: str, source_rel: str, js_files: set):
    """Resolve a RELATIVE import specifier (`./x`, `../a/b`) to a repo file path,
    trying extensionless, then JS/TS extensions, then `/index.*`. Bare/package
    specifiers never resolve here (we don't invent cross-package edges)."""
    if not specifier.startswith("."):
        return None
    base = os.path.normpath(os.path.join(os.path.dirname(source_rel), specifier))
    base = base.replace(os.sep, "/")
    if base in js_files:
        return base
    for ext in _JS_RESOLVE_EXTS:
        if base + ext in js_files:
            return base + ext
    for ext in _JS_RESOLVE_EXTS:
        cand = base + "/index" + ext
        if cand in js_files:
            return cand
    return None


def _parse_js_import_map(source, source_rel, js_files, default_export_by_file) -> dict:
    """``local_name → (target_rel, original_name|None, kind)`` for RESOLVABLE
    relative imports. ``kind`` ∈ {named, default, namespace}. Default imports resolve
    to the target's named default export (else dropped). require() destructure /
    namespace forms are included. Parsed on raw source (specifiers are strings)."""
    out: dict = {}
    for m in _RE_JS_IMPORT_STMT.finditer(source):
        target = _resolve_js_file(m.group("spec"), source_rel, js_files)
        if not target:
            continue
        clause = m.group("clause")
        ns = re.search(r"\*\s+as\s+([\w$]+)", clause)              # * as ns
        if ns:
            out[ns.group(1)] = (target, None, "namespace")
        named = re.search(r"\{([^}]*)\}", clause)                  # { a, b as c }
        if named:
            for part in named.group(1).split(","):
                part = part.strip()
                if not part or part.startswith("type "):
                    continue
                mm = re.match(r"([\w$]+)(?:\s+as\s+([\w$]+))?$", part)
                if mm:
                    out[mm.group(2) or mm.group(1)] = (target, mm.group(1), "named")
        head = clause.split("{")[0].split("*")[0].strip().rstrip(",").strip()
        dm = re.match(r"^([\w$]+)$", head)                         # default import
        if dm:
            orig = default_export_by_file.get(target)
            if orig:
                out[dm.group(1)] = (target, orig, "default")
    for m in _RE_JS_REQUIRE_STMT.finditer(source):
        target = _resolve_js_file(m.group("spec"), source_rel, js_files)
        if not target:
            continue
        bind = m.group("bind").strip()
        if bind.startswith("{"):
            for part in bind.strip("{}").split(","):
                part = part.strip()
                mm = re.match(r"([\w$]+)(?:\s*:\s*([\w$]+))?$", part) if part else None
                if mm:
                    out[mm.group(2) or mm.group(1)] = (target, mm.group(1), "named")
        else:
            out[bind] = (target, None, "namespace")
    return out


def _resolve_js_file_fs(specifier: str, source_rel: str, root: Path):
    """Resolve a relative import to a repo file by probing the FILESYSTEM (no
    precomputed file set) — for single-file callers like the failure lens."""
    if not specifier.startswith("."):
        return None
    base = os.path.normpath(os.path.join(os.path.dirname(source_rel), specifier))
    base = base.replace(os.sep, "/")
    for cand in ([base] + [base + e for e in _JS_RESOLVE_EXTS]
                 + [base + "/index" + e for e in _JS_RESOLVE_EXTS]):
        if (root / cand).is_file():
            return cand
    return None


def js_call_context(root, rel_file: str, line: int, cap: int = 4) -> dict:
    """For a failing JS/TS site (``rel_file:line``), the enclosing function (if the
    site sits inside an extracted one) and the resolved IMPLEMENTATION calls the
    failure flows into — same-file callees (high) and callees reached through a
    RESOLVABLE relative import (medium), NEAREST the failing line first. Reuses the
    L1-B extraction; deterministic and conservative. Never raises.

    This is the JS/TS analogue of the Python failure flow's AST callee resolution:
    given the test/function that failed, find the product function it exercises so
    the lens can light the connected implementation.

    Returns:
        dict — ``{"function": name|"", "line": int, "calls": [{name, file, line,
        confidence, callLine}]}``.
    """
    root = Path(root)
    line = int(line or 0)
    try:
        source = (root / rel_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"function": "", "line": line, "calls": []}
    scrubbed = _scrub_js(source)
    line_starts = _line_starts(scrubbed)

    defs = sorted(_extract_js_functions(root / rel_file, rel_file, ""), key=lambda f: f.line)
    # The enclosing def (innermost brace-matched span containing `line`). When the
    # failing line sits in a bare test callback (not an extracted def), there is no
    # enclosing function — we scan the whole file, nearest-line-first.
    enc_name, scan_lo, scan_hi = "", 1, len(line_starts)
    best = None
    for i, d in enumerate(defs):
        nxt = defs[i + 1].line if i + 1 < len(defs) else None
        e = _js_body_end_line(scrubbed, line_starts, d.line, nxt)
        if d.line <= line <= e and (best is None or d.line > best[0]):
            best = (d.line, e, d)
    if best is not None:
        enc_name, scan_lo, scan_hi = best[2].name, best[0], best[1]

    scan_start = line_starts[scan_lo - 1]
    scan_end = line_starts[scan_hi] if scan_hi < len(line_starts) else len(scrubbed)
    region = scrubbed[scan_start:scan_end]

    local = {d.name: d for d in defs if "." not in d.name}
    # Resolve this file's relative imports against the filesystem, then extract each
    # target's functions so a resolved callee carries its real definition line.
    js_files: set = set()
    for rgx in (_RE_JS_IMPORT_STMT, _RE_JS_REQUIRE_STMT):
        for m in rgx.finditer(source):
            t = _resolve_js_file_fs(m.group("spec"), rel_file, root)
            if t:
                js_files.add(t)
    default_exports, target_defs = {}, {}
    for t in js_files:
        try:
            tsrc = (root / t).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        dm = _RE_JS_DEFAULT_EXPORT.search(_scrub_js(tsrc))
        if dm:
            default_exports[t] = dm.group(1) or dm.group(2)
        target_defs[t] = {f.name: f.line for f in _extract_js_functions(root / t, t, "")}
    import_map = _parse_js_import_map(source, rel_file, js_files, default_exports)

    found = []   # (proximity_to_fail, name, file, line, confidence, callLine)

    def consider(name, tfile, tline, conf, off):
        cl = _line_of(line_starts, scan_start + off)
        found.append((abs(cl - line), name, tfile, tline, conf, cl))

    for nm, d in local.items():                          # same-file calls (high)
        if nm == enc_name:
            continue
        for mm in re.finditer(r"(?<![\w.$])" + re.escape(nm) + r"\s*\(", region):
            consider(nm, rel_file, d.line, "high", mm.start())
    for loc, (trel, original, kind) in import_map.items():   # resolved imports (medium)
        if kind == "namespace":
            for mm in re.finditer(r"(?<![\w.$])" + re.escape(loc) + r"\.(\w+)\s*\(", region):
                consider(mm.group(1), trel, target_defs.get(trel, {}).get(mm.group(1)),
                         "medium", mm.start())
        elif original:
            for mm in re.finditer(r"(?<![\w.$])" + re.escape(loc) + r"\s*\(", region):
                consider(original, trel, target_defs.get(trel, {}).get(original),
                         "medium", mm.start())

    found.sort(key=lambda c: c[0])
    calls, seen = [], set()
    for _prox, name, tfile, tline, conf, cl in found:
        key = (tfile, name)
        if key in seen:
            continue
        seen.add(key)
        calls.append({"name": name, "file": tfile, "line": tline,
                      "confidence": conf, "callLine": cl})
        if len(calls) >= cap:
            break
    return {"function": enc_name, "line": line, "calls": calls}


def _js_flows(abs_path, rel, module_id, flows, func_dicts, fn_by_id,
              callable_by_file_name, module_by_file, default_export_by_file,
              js_files) -> None:
    """JS/TS call flows for one file into ``flows``:

      • same-file bare ``name()`` to a callable defined here, and ``this.method()``
        to a sibling method of the caller's class → **high**;
      • calls through resolvable RELATIVE imports — named, default, and ``* as ns``
        member calls, plus require() destructure / namespace → **medium**.

    Comments/strings are scrubbed first; each call is attributed to the innermost
    enclosing def by brace-matched span, so module-scope calls are not mis-blamed
    on the nearest function (the old low-confidence heuristic's main noise source).
    """
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    defs = sorted([f for f in func_dicts if f["path"] == rel], key=lambda f: f["line"])
    if not defs:
        return

    scrubbed = _scrub_js(source)
    line_starts = _line_starts(scrubbed)

    # Def spans [startLine, endLine] (brace-matched body, bounded by the next
    # sibling). owner_of(line) = the innermost def whose span contains the line.
    spans = []
    for idx, d in enumerate(defs):
        nxt = defs[idx + 1]["line"] if idx + 1 < len(defs) else None
        spans.append((d["line"], _js_body_end_line(scrubbed, line_starts, d["line"], nxt), d))

    def owner_of(line):
        best = None
        for s, e, d in spans:
            if s <= line <= e and (best is None or s > best[0]):
                best = (s, e, d)
        return best[2] if best else None

    def add(owner, callee_id, conf, line):
        if owner and owner["id"] != callee_id:
            _record_flow(flows, fn_by_id, module_by_file, module_id,
                         owner["id"], callee_id, conf, line)

    local_by_name, methods_here = {}, {}
    for d in defs:
        if "." in d["name"]:
            methods_here[tuple(d["name"].split(".", 1))] = d
        else:
            local_by_name[d["name"]] = d
    # OFFSETS of definition NAME tokens the bare-call scan could mistake for a call:
    # `function name(` declarations and class `method(` signatures (the only forms
    # where the name is immediately followed by `(`). We skip the TOKEN, not the
    # whole line — so a one-line method body's real calls, later on the same line
    # (`add(n) { return add(n, 1) }`), are still detected.
    sig_offsets = set()
    for pat in _JS_DECL_PATTERNS[:3]:               # function-declaration name tokens
        for m in pat.finditer(scrubbed):
            sig_offsets.add(m.start(1))
    for cm in _JS_CLASS_RE.finditer(scrubbed):      # class method-signature name tokens
        blk = _brace_block(scrubbed, cm.end())
        if blk is None:
            continue
        for mm in _JS_METHOD_RE.finditer(scrubbed[blk[0] + 1:blk[1]]):
            sig_offsets.add(blk[0] + 1 + mm.start(1))

    # (1) same-file: bare `name(` → a top-level callable defined here (high).
    for nm, d in local_by_name.items():
        for m in re.finditer(r"(?<![\w.$])" + re.escape(nm) + r"\s*\(", scrubbed):
            if m.start() in sig_offsets:            # the definition token, not a call
                continue
            line = _line_of(line_starts, m.start())
            add(owner_of(line), d["id"], "high", line)

    # (2) same-file: `this.method(` → a sibling method of the caller's class (high).
    if methods_here:
        for m in re.finditer(r"(?<![\w.$])this\.(\w+)\s*\(", scrubbed):
            line = _line_of(line_starts, m.start())
            owner = owner_of(line)
            if not owner or "." not in owner["name"]:
                continue
            target = methods_here.get((owner["name"].split(".", 1)[0], m.group(1)))
            if target:
                add(owner, target["id"], "high", line)

    # (3) cross-file: resolvable relative imports (medium).
    for local, (target_rel, original, kind) in _parse_js_import_map(
            source, rel, js_files, default_export_by_file).items():
        if kind == "namespace":
            for m in re.finditer(r"(?<![\w.$])" + re.escape(local) + r"\.(\w+)\s*\(", scrubbed):
                callee_id = callable_by_file_name.get((target_rel, m.group(1)))
                if callee_id:
                    add(owner_of(_line_of(line_starts, m.start())), callee_id,
                        "medium", _line_of(line_starts, m.start()))
        else:                                       # named / default
            callee_id = callable_by_file_name.get((target_rel, original)) if original else None
            if not callee_id:
                continue
            for m in re.finditer(r"(?<![\w.$])" + re.escape(local) + r"\s*\(", scrubbed):
                if m.start() in sig_offsets:        # the definition token, not a call
                    continue
                line = _line_of(line_starts, m.start())
                add(owner_of(line), callee_id, "medium", line)


def _rollup_flows(flows: list) -> tuple:
    """Roll function flows up to module-level and file-level groupings.

    Args:
        flows: list[dict] — function-level flow edges.

    Returns:
        tuple — (module_rollups, file_rollups), each a dict keyed by an
        ordered pair → {"confidence", "flows": [capped summaries], "count"}.
    """
    module_rollups: dict = {}
    file_rollups:   dict = {}

    def accumulate(store, key, fw):
        entry = store.setdefault(key, {"confidence": "low", "flows": [], "count": 0})
        entry["count"] += 1
        entry["confidence"] = _conf_max(entry["confidence"], fw["confidence"])
        if len(entry["flows"]) < _ROLLUP_FLOW_CAP:
            entry["flows"].append({
                "id":             fw["id"],
                "label":          fw["label"],
                "fromFile":       fw["fromFile"],
                "toFile":         fw["toFile"],
                "fromFunctionId": fw["fromFunctionId"],
                "toFunctionId":   fw["toFunctionId"],
                "type":           fw["type"],
                "confidence":     fw["confidence"],
            })

    for fw in flows:
        if fw["fromModuleId"] != fw["toModuleId"]:
            accumulate(module_rollups, (fw["fromModuleId"], fw["toModuleId"]), fw)
        if fw["fromFile"] != fw["toFile"]:
            accumulate(file_rollups, (fw["fromFile"], fw["toFile"]), fw)

    return module_rollups, file_rollups


def _rollup_label(entry: dict) -> str:
    """Compact label for a rollup edge.

    Single underlying flow → the function pair (`validate() → save()`);
    multiple → `N function flows`.
    """
    if entry["count"] == 1 and entry["flows"]:
        return entry["flows"][0]["label"]
    return f"{entry['count']} function flows"


def _merge_module_edges(import_edges: list, module_rollups: dict) -> list:
    """Merge dataflow rollups with import edges; dataflow wins per module pair.

    Args:
        import_edges:   list[_Edge] — raw import edges.
        module_rollups: dict — (from_mod, to_mod) → rollup entry.

    Returns:
        list[dict] — module edges (backward-compatible shape + flow metadata).
    """
    by_pair: dict = {}

    for (fm, tm), entry in module_rollups.items():
        from_name, to_name = fm.replace("module:", ""), tm.replace("module:", "")
        by_pair[(fm, tm)] = {
            "id":         f"edge:{from_name}->{to_name}",
            "from":       fm,
            "to":         tm,
            "type":       "dataflow",
            "label":      _rollup_label(entry),
            "confidence": entry["confidence"],
            "flows":      entry["flows"],
            "flowCount":  entry["count"],
        }

    for e in import_edges:
        key = (e.from_id, e.to_id)
        if key in by_pair:
            by_pair[key]["hasImport"] = True          # dataflow already present
            continue
        d = e.to_dict()
        d["flows"] = []
        d["flowCount"] = 0
        by_pair[key] = d

    return list(by_pair.values())


def _build_file_edges(file_rollups: dict, file_dicts: list) -> list:
    """Build file-level dataflow rollup edges from grouped flows.

    Args:
        file_rollups: dict — (from_file, to_file) → rollup entry.
        file_dicts:   list[dict] — serialised file nodes.

    Returns:
        list[dict] — file-level edges (id, from/to file ids, label, flows, …).
    """
    fid_by_path = {f["path"]: f["id"] for f in file_dicts}
    mod_by_path = {f["path"]: f["moduleId"] for f in file_dicts}
    out: list = []
    for (ff, tf), entry in file_rollups.items():
        out.append({
            "id":           f"fileedge:{ff}->{tf}",
            "from":         fid_by_path.get(ff, f"file:{ff}"),
            "to":           fid_by_path.get(tf, f"file:{tf}"),
            "fromFile":     ff,
            "toFile":       tf,
            "fromModuleId": mod_by_path.get(ff, ""),
            "toModuleId":   mod_by_path.get(tf, ""),
            "type":         "dataflow",
            "label":        _rollup_label(entry),
            "confidence":   entry["confidence"],
            "flows":        entry["flows"],
            "flowCount":    entry["count"],
        })
    return out


# ─── HTML / web-app entrypoint edges (L1-D-A) ─────────────────────────────────
#
# A web/WebXR app's story starts in HTML: a page loads a JS module, which calls the
# app's functions. We make that first hop explicit — HTML entrypoint → referenced
# JS/TS module — deterministically and CONSERVATIVELY (an edge is drawn only when the
# reference resolves to a file that actually exists in the repo). External URLs, CDN
# scripts, and bare npm specifiers never produce an edge. No tests/runtime claims:
# this is architecture only. Regex + stdlib HTMLParser; no dependency.

# import './x'  |  import … from './x'  |  import('./x')  — captures the specifier.
_RE_HTML_INLINE_IMPORT = re.compile(
    r"""\bimport\b(?:[^'";]*\bfrom\b\s*|\s*\(\s*|\s+)['"]([^'"]+)['"]""")


class _ScriptRefCollector(HTMLParser):
    """Collect a page's script references: external ``src`` (module or classic) and
    the bodies of INLINE ``<script type="module">`` blocks (for their imports)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.srcs: list = []            # external script src strings
        self.inline_modules: list = []  # inline module-script bodies
        self._in_module = False
        self._buf: list = []

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if a.get("src"):
            self.srcs.append(a["src"])
        elif a.get("type", "").lower() == "module":
            self._in_module = True
            self._buf = []

    def handle_data(self, data):
        if self._in_module:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_module:
            self.inline_modules.append("".join(self._buf))
            self._in_module = False
            self._buf = []


def _html_script_refs(source: str) -> list:
    """``[(specifier, kind)]`` for one HTML page — external ``src`` (kind ``"src"``)
    and inline ``<script type="module">`` import specifiers (kind ``"import"``)."""
    p = _ScriptRefCollector()
    try:
        p.feed(source or "")
        p.close()
    except Exception:  # noqa: BLE001 — malformed HTML must never break assimilation
        pass
    refs = [(s, "src") for s in p.srcs]
    for body in p.inline_modules:
        refs += [(m.group(1), "import") for m in _RE_HTML_INLINE_IMPORT.finditer(body)]
    return refs


def _resolve_html_ref(ref: str, html_rel: str, known: set):
    """Resolve an HTML script reference to a repo JS/TS file in ``known``, or None.
    Relative (``./x``, ``js/x.js``) → from the page's dir; root-relative (``/js/x``)
    → from the repo root. External URLs / data: / blob: are never resolved, and only
    files that EXIST resolve — so a wrong edge is impossible (missing is acceptable)."""
    ref = (ref or "").strip()
    if not ref or ref.startswith(("http://", "https://", "//", "data:", "blob:")):
        return None
    ref = ref.split("?")[0].split("#")[0]
    if not ref:
        return None
    if ref.startswith("/"):
        base = ref.lstrip("/")
    else:
        base = os.path.normpath(os.path.join(os.path.dirname(html_rel), ref))
    base = base.replace(os.sep, "/")
    if base in known:
        return base
    for ext in _JS_RESOLVE_EXTS:
        if base + ext in known:
            return base + ext
    for ext in _JS_RESOLVE_EXTS:
        if base + "/index" + ext in known:
            return base + "/index" + ext
    return None


def _html_entry_edges(root: Path, file_dicts: list) -> tuple:
    """HTML entrypoint → JS/TS module edges (file-level and module-level).

    For each HTML page, resolve its external ``src`` scripts and inline-module
    imports to real repo JS/TS files; emit one file edge per (page, module) pair and
    a deduped module edge per (page-module, js-module) pair. Bare ESM specifiers
    (npm packages) and unresolved refs are dropped.

    Returns:
        tuple — (file_edges: list[dict], module_edges: list[dict]).
    """
    known = {f["path"] for f in file_dicts if f["language"] in ("JavaScript", "TypeScript")}
    if not known:
        return [], []
    fid_by_path = {f["path"]: f["id"] for f in file_dicts}
    mod_by_path = {f["path"]: f["moduleId"] for f in file_dicts}
    file_edges, mod_pairs, seen = [], {}, set()
    for f in file_dicts:
        if f["language"] != "HTML":
            continue
        html_rel = f["path"]
        try:
            source = (root / html_rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec, kind in _html_script_refs(source):
            # inline ESM bare specifiers (`import 'three'`) are npm packages, not
            # repo modules — only relative / root-relative imports can resolve.
            if kind == "import" and not spec.startswith((".", "/")):
                continue
            target = _resolve_html_ref(spec, html_rel, known)
            if target is None or (html_rel, target) in seen:
                continue
            seen.add((html_rel, target))
            label = "loads" if kind == "src" else "imports"
            file_edges.append({
                "id":           f"htmledge:{html_rel}->{target}",
                "from":         fid_by_path.get(html_rel, f"file:{html_rel}"),
                "to":           fid_by_path.get(target, f"file:{target}"),
                "fromFile":     html_rel,
                "toFile":       target,
                "fromModuleId": mod_by_path.get(html_rel, ""),
                "toModuleId":   mod_by_path.get(target, ""),
                "type":         "entry",
                "label":        label,
                "confidence":   "high",
                "flows":        [],
                "flowCount":    0,
            })
            fm, tm = mod_by_path.get(html_rel, ""), mod_by_path.get(target, "")
            if fm and tm and fm != tm:
                mod_pairs.setdefault((fm, tm), label)
    mod_edges = [{"id": f"edge:{fm}->{tm}:entry", "from": fm, "to": tm,
                  "type": "entry", "label": lbl, "confidence": "high"}
                 for (fm, tm), lbl in mod_pairs.items()]
    return file_edges, mod_edges


# ─── Canvas layout ────────────────────────────────────────────────────────── #

_BOX_W:    int = 220
_BOX_H:    int = 130
_COL_GAP:  int = 210
_ROW_GAP:  int = 80
_MAX_COLS: int = 3
_ORIGIN_X: int = 80
_ORIGIN_Y: int = 80
_ARROW_FLOW_CAP: int = 8   # flow summaries embedded per canvas arrow


def _module_id_to_box_id(module_id: str) -> str:
    """Derive a stable canvas box ID from a module ID.

    Args:
        module_id: str — e.g. "module:frontend".

    Returns:
        str — e.g. "box:module:frontend".
    """
    return f"box:{module_id}"


_LINKED_FILES_CAP: int = 25   # max file paths stored per box


def _module_layers(mod_ids: list, edges: list):
    """Assign each module a flow layer via longest-path from a source.

    Used to lay modules out left-to-right in dependency order (a clean pipeline)
    instead of an alphabetical grid. Returns None when there are no module edges
    or when the graph has a cycle — callers then fall back to the grid.

    Args:
        mod_ids: list[str] — module ids.
        edges:   list[dict] — module edges ({from, to, …}).

    Returns:
        dict | None — {module_id: layer_index}, or None to use the grid.
    """
    ids = set(mod_ids)
    adj = {m: [] for m in mod_ids}
    indeg = {m: 0 for m in mod_ids}
    has_edge = False
    for e in (edges or []):
        f, t = e.get("from"), e.get("to")
        if f in ids and t in ids and f != t:
            adj[f].append(t)
            indeg[t] += 1
            has_edge = True
    if not has_edge:
        return None
    layer = {m: 0 for m in mod_ids}
    indeg2 = dict(indeg)
    queue = sorted(m for m in mod_ids if indeg2[m] == 0)
    processed = 0
    while queue:
        m = queue.pop(0)
        processed += 1
        for t in sorted(adj[m]):
            if layer[t] < layer[m] + 1:
                layer[t] = layer[m] + 1
            indeg2[t] -= 1
            if indeg2[t] == 0:
                queue.append(t)
    if processed < len(mod_ids):
        return None   # cycle — fall back to the grid for safety
    return layer


def _layout_boxes(modules: list, files: list, edges: list = None) -> list:
    """Arrange module dicts into canvas boxes.

    When module dataflow edges exist, modules are laid out left-to-right in
    **flow order** (dependency layers) so the architecture reads as a clean
    pipeline; otherwise they fall back to a deterministic alphabetical grid.
    IDs are derived from module IDs so regenerating yields the same box IDs.

    Args:
        modules: list[dict] — serialised module dicts from analyze_repo().
        files:   list[dict] — serialised file dicts (for linkedFiles).
        edges:   list[dict] — module edges (for flow-order layout); optional.

    Returns:
        list[dict] — canvas box dicts.
    """
    files_by_module: dict = {}
    for f in files:
        files_by_module.setdefault(f["moduleId"], []).append(f["path"])

    def make_box(mod, x, y):
        lang_parts = [f"{v} {k}" for k, v in sorted(mod["languages"].items(), key=lambda kv: -kv[1])]
        lang_str = ", ".join(lang_parts[:3]) if lang_parts else "no source files"
        return {
            "id":          _module_id_to_box_id(mod["id"]),
            "x":           x,
            "y":           y,
            "w":           _BOX_W,
            "h":           _BOX_H,
            "type":        "dotted",
            "title":       mod["name"],
            "prompt":      f"{mod['type'].capitalize()}: {mod['fileCount']} file(s) — {lang_str}",
            "files":       [],
            "linkedPath":  mod["path"],
            "linkedFiles": sorted(files_by_module.get(mod["id"], []))[:_LINKED_FILES_CAP],
            "moduleId":    mod["id"],
        }

    sorted_mods = sorted(modules, key=lambda m: m["name"].lower())
    layers = _module_layers([m["id"] for m in modules], edges)

    boxes: list = []
    if layers is None:
        # Alphabetical grid (no module dataflow / cyclic — unchanged behavior).
        for i, mod in enumerate(sorted_mods):
            x = _ORIGIN_X + (i % _MAX_COLS) * (_BOX_W + _COL_GAP)
            y = _ORIGIN_Y + (i // _MAX_COLS) * (_BOX_H + _ROW_GAP)
            boxes.append(make_box(mod, x, y))
    else:
        # Flow-order: one column per dependency layer, stacked within a layer.
        by_layer: dict = {}
        for mod in sorted_mods:
            by_layer.setdefault(layers[mod["id"]], []).append(mod)
        for lyr in sorted(by_layer):
            for row, mod in enumerate(by_layer[lyr]):
                x = _ORIGIN_X + lyr * (_BOX_W + _COL_GAP)
                y = _ORIGIN_Y + row * (_BOX_H + _ROW_GAP)
                boxes.append(make_box(mod, x, y))

    return boxes


def _make_arrows(edges: list, mod_id_to_box: dict) -> list:
    """Convert ArchGraph edge dicts to canvas arrow dicts.

    Port direction is derived from the relative positions of the source
    and target boxes:
    - Horizontal separation dominates → E→W or W→E
    - Vertical separation dominates   → S→N or N→S

    Args:
        edges:        list[dict] — serialised edge dicts from analyze_repo().
        mod_id_to_box: dict — module_id → canvas box dict.

    Returns:
        list[dict] — canvas arrow dicts (id, fromBox, fromPort, toBox, toPort, label).
    """
    arrows: list = []

    for edge in edges:
        from_box = mod_id_to_box.get(edge["from"])
        to_box   = mod_id_to_box.get(edge["to"])
        if not from_box or not to_box:
            continue

        from_cx = from_box["x"] + from_box["w"] / 2
        to_cx   = to_box["x"]   + to_box["w"] / 2
        from_cy = from_box["y"] + from_box["h"] / 2
        to_cy   = to_box["y"]   + to_box["h"] / 2

        dx = to_cx - from_cx
        dy = to_cy - from_cy

        if abs(dx) >= abs(dy):
            from_port = "E" if dx > 0 else "W"
            to_port   = "W" if dx > 0 else "E"
        else:
            from_port = "S" if dy > 0 else "N"
            to_port   = "N" if dy > 0 else "S"

        arrows.append({
            "id":         f"arrow:{edge['id']}",
            "fromBox":    from_box["id"],
            "fromPort":   from_port,
            "toBox":      to_box["id"],
            "toPort":     to_port,
            "label":      edge.get("label", ""),
            # Step 23: carry rollup metadata so the inspector/hover can show the
            # underlying function flows without refetching the graph.
            "edgeType":   edge.get("type", "import"),
            "confidence": edge.get("confidence", ""),
            "flowCount":  edge.get("flowCount", 0),
            "flows":      (edge.get("flows") or [])[:_ARROW_FLOW_CAP],
            "hasImport":  edge.get("hasImport", edge.get("type") == "import"),
        })

    return arrows
