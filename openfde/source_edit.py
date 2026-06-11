"""
openfde/source_edit.py — the repair hatch's hands: read a slice of a source
file, splice a replacement back in.

The hatch is function-scoped by design (the UI only ever asks for one
function's range, resolved from the ArchGraph), and it only opens FROM a
failure receipt — this module just does the safe mechanics:

  * paths are repo-relative, resolved under the watched root, and may not
    escape it (no ``..``, no absolute paths, no symlink hops outside);
  * reads return numbered lines so the UI can mark the failing one;
  * writes splice an inclusive 1-based line range and preserve the file's
    trailing-newline convention.

Edits made through the hatch hit the worktree like any other edit — the
watcher attributes them to the live episode, so a fix made here is recorded
exactly like a fix made in your editor. Zero ceremony, even for repairs.
"""

from pathlib import Path


class SourceEditError(ValueError):
    """Raised for unsafe paths or out-of-range splices (maps to HTTP 400)."""


def resolve_repo_path(root, rel: str) -> Path:
    """Resolve ``rel`` under ``root``, refusing anything that escapes it.

    Args:
        root: repository root (str | Path).
        rel: repo-relative file path from the client.

    Returns:
        Path — the resolved, existing file.

    Raises:
        SourceEditError: absolute path, traversal outside root, or not a file.
    """
    root = Path(root).resolve()
    rel = str(rel or "").strip()
    if not rel or rel.startswith(("/", "\\")) or rel.split(":", 1)[0].isalpha() and ":" in rel[:3]:
        raise SourceEditError("path must be repo-relative")
    candidate = (root / rel).resolve()
    if not str(candidate).startswith(str(root) + "/") and candidate != root:
        raise SourceEditError("path escapes the repository")
    if not candidate.is_file():
        raise SourceEditError("not a file in the repository")
    return candidate


def read_slice(root, rel: str, start: int, end: int) -> dict:
    """Read lines ``start``..``end`` (1-based, inclusive, clamped to the file).

    Returns:
        dict — {path, start, end, total, code} where ``code`` is the joined
        slice and ``end`` reflects clamping.
    """
    p = resolve_repo_path(root, rel)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, int(start))
    end = min(total, int(end)) if end else total
    if start > total:
        raise SourceEditError(f"start {start} beyond end of file ({total} lines)")
    if end < start:
        raise SourceEditError("end before start")
    return {"path": rel, "start": start, "end": end, "total": total,
            "code": "\n".join(lines[start - 1:end])}


def splice_lines(root, rel: str, start: int, end: int, code: str) -> dict:
    """Replace lines ``start``..``end`` (1-based, inclusive) with ``code``.

    Preserves the file's trailing-newline convention. Returns the new range so
    the UI can stay anchored on the function after a save.

    Returns:
        dict — {path, start, end, total} for the spliced file.
    """
    p = resolve_repo_path(root, rel)
    original = p.read_text(encoding="utf-8", errors="replace")
    had_trailing = original.endswith("\n")
    lines = original.splitlines()
    total = len(lines)
    start = max(1, int(start))
    end = min(total, int(end))
    if start > total or end < start:
        raise SourceEditError("splice range outside the file")
    # split("\n"), not splitlines(): a trailing newline in the hatch's draft means
    # "keep the blank line at the end of the range" — splitlines() would eat it
    # (found by the first live dogfood: the blank between two test methods vanished).
    new_lines = (code or "").split("\n")
    lines[start - 1:end] = new_lines
    out = "\n".join(lines) + ("\n" if had_trailing else "")
    p.write_text(out, encoding="utf-8")
    return {"path": rel, "start": start, "end": start + len(new_lines) - 1,
            "total": len(lines)}
