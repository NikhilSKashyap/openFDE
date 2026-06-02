"""Recursive file tree builder for the watched repository path.

Produces a nested dict tree suitable for JSON serialisation and consumed by
the frontend FileTree component.
"""

from pathlib import Path

# Directory names that are always excluded from the tree
_EXCLUDE_DIRS: frozenset = frozenset({
    ".git",
    "node_modules",
    ".openfde",
    "dist",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".eggs",
    ".cache",
    "build",
    "htmlcov",
    ".next",
    ".nuxt",
    "coverage",
    ".turbo",
    ".parcel-cache",
    ".ruff_cache",
})

# File suffixes (including the dot) that are excluded (build artifacts, binaries)
_EXCLUDE_SUFFIXES: frozenset = frozenset({
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".egg-info",
    ".dist-info",
    ".map",
})


def build_file_tree(root: Path, max_depth: int = 8) -> dict:
    """Build a recursive file tree dict for the given root directory.

    Excludes common non-source directories (node_modules, .git, dist, …)
    and binary build artifacts.

    Args:
        root: Path — repository root to scan
        max_depth: int — maximum recursion depth to prevent runaway scans (default: 8)

    Returns:
        dict with keys:
            name: str — file or directory name
            path: str — POSIX-style path relative to root (or '.' for root itself)
            type: Literal['directory', 'file']
            children: list[dict] — child nodes (directories only)
            size: int — file size in bytes (files only)
    """
    return _scan(root, root, max_depth, 0)


def _scan(root: Path, path: Path, max_depth: int, depth: int) -> dict:
    """Recursively scan a single path and return its tree node.

    Args:
        root: Path — repository root used for computing relative paths
        path: Path — the path being scanned on this call
        max_depth: int — recursion depth limit
        depth: int — current recursion depth (0 = root)

    Returns:
        dict — file tree node (see build_file_tree for shape)
    """
    name = path.name or str(path)
    rel = path.relative_to(root).as_posix() if path != root else "."

    if path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {"name": name, "path": rel, "type": "file", "size": size}

    # Directory node
    children: list = []
    if depth < max_depth:
        try:
            raw_entries = list(path.iterdir())
        except PermissionError:
            raw_entries = []

        # Dirs first (alphabetical), then files (alphabetical)
        entries = sorted(raw_entries, key=lambda p: (p.is_file(), p.name.lower()))

        for entry in entries:
            # Skip excluded directory names
            if entry.is_dir() and entry.name in _EXCLUDE_DIRS:
                continue
            # Skip hidden entries except .openfde (which we already excluded above)
            if entry.name.startswith("."):
                continue
            # Skip excluded file suffixes
            if entry.is_file() and entry.suffix in _EXCLUDE_SUFFIXES:
                continue
            children.append(_scan(root, entry, max_depth, depth + 1))

    return {
        "name": name if path != root else str(root),
        "path": rel,
        "type": "directory",
        "children": children,
    }
