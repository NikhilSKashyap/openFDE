"""
openfde/boot_cache.py — the warm-start boot cache (.openfde/cache/).

OpenFDE is the cockpit / orange box: it must feel alive the instant you open it, never a blank
canvas while heavy repo intelligence runs. The slow part is ``architect.analyze_repo`` (~1s on a
mid-size repo, recomputed per request). This module persists a warm snapshot — the file tree and
the last ArchGraph — plus a meta header, so a fresh boot serves the last known-good canvas
immediately ("Restored from P14 · refreshing…") and recomputes in the background.

Trust model: the snapshot is keyed by git HEAD + a dirty-worktree signature + a parser version.
``is_stale`` says whether the current repo state still matches; a stale snapshot is STILL served
(better a slightly-old canvas than a blank one) with a "refreshing" flag. Writes are atomic and
partial (file-tree-only or arch-only are both fine), so a torn write never yields a blank boot.

Pure + dependency-light (json + hashlib + pathlib): unit-testable without a server or git.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Bump when the ArchGraph/file-tree SHAPE changes so every old snapshot invalidates on upgrade.
PARSER_VERSION = "1"

_FILE_TREE = "file_tree.json"
_ARCH = "arch_snapshot.json"
_META = "cache_meta.json"


def cache_dir(openfde_dir) -> Path:
    return Path(openfde_dir) / "cache"


def dirty_signature(repo_status: dict) -> str:
    """A stable short hash of HEAD + the sorted dirty-file set. Changes whenever HEAD moves or the
    worktree's dirty set changes — the trigger to treat a snapshot as stale."""
    rs = repo_status if isinstance(repo_status, dict) else {}
    head = str(rs.get("head") or rs.get("shortHead") or "")
    dirty = sorted(p for p in (rs.get("dirty") or []) if isinstance(p, str))
    raw = head + "::" + "|".join(dirty)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_meta(openfde_dir) -> dict:
    """The cache meta header ({} when never written / unreadable)."""
    m = _read_json(cache_dir(openfde_dir) / _META)
    return m if isinstance(m, dict) else {}


def read_warm(openfde_dir) -> "dict | None":
    """The warm-start payload, or None when there is no cache at all.

    Returns:
        dict | None — {"meta": {...}, "fileTree": <tree|None>, "arch": <graph|None>}. Either
        artifact may be None (partial cache); meta is always present in the returned dict.
    """
    d = cache_dir(openfde_dir)
    meta = _read_json(d / _META)
    tree = _read_json(d / _FILE_TREE)
    arch = _read_json(d / _ARCH)
    if meta is None and tree is None and arch is None:
        return None
    return {"meta": meta if isinstance(meta, dict) else {}, "fileTree": tree, "arch": arch}


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)                                   # atomic on POSIX — never a torn read


def write_warm(openfde_dir, *, file_tree=None, arch=None, head="", dirty_sig="",
               episode_tag="", generated_at="", repo_root="") -> dict:
    """Write/refresh the warm cache. ``file_tree`` and ``arch`` are written only when provided, so a
    caller can refresh just one artifact; the meta header is always updated with the non-empty
    fields passed in (so an episode-completion write can stamp ``episode_tag`` without touching the
    snapshots). Returns the merged meta.
    """
    d = cache_dir(openfde_dir)
    if file_tree is not None:
        _atomic_write(d / _FILE_TREE, file_tree)
    if arch is not None:
        _atomic_write(d / _ARCH, arch)
    meta = read_meta(openfde_dir)
    meta["parserVersion"] = PARSER_VERSION
    for k, v in (("repoRoot", repo_root), ("head", head), ("dirtySignature", dirty_sig),
                 ("episodeTag", episode_tag), ("generatedAt", generated_at)):
        if v:
            meta[k] = v
    _atomic_write(d / _META, meta)
    return meta


def is_stale(meta: dict, *, head: str, dirty_sig: str) -> bool:
    """True when the cached snapshot no longer matches the current repo state (HEAD moved, the dirty
    set changed, or the parser version was bumped). A stale snapshot is still usable for first
    paint — the caller just shows a 'refreshing' state and recomputes."""
    if not isinstance(meta, dict) or not meta:
        return True
    if meta.get("parserVersion") != PARSER_VERSION:
        return True
    return meta.get("head") != head or meta.get("dirtySignature") != dirty_sig
