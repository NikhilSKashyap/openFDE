"""
openfde/language_packs/registry.py — the front door.

``get_language_packs(root)`` returns every pack that applies to a repo;
``get_pack_for_file(path)`` returns the pack that owns a single file. Python is the
only pack today — JS/TS and others are added here as they land. Boring on purpose.
"""
from __future__ import annotations

from .python_pack import PythonPack

# Ordered: the first pack that claims a file wins in get_pack_for_file.
_ALL_PACKS = (PythonPack(),)


def get_language_packs(root) -> list:
    """Every LanguagePack whose ``detects(root)`` is true for this repo."""
    return [p for p in _ALL_PACKS if p.detects(root)]


def get_pack_for_file(path):
    """The pack that owns ``path`` by extension, or ``None`` (no pack for that
    language yet)."""
    name = str(path)
    for pack in _ALL_PACKS:
        for glob in pack.file_globs:
            if name.endswith(glob.lstrip("*")):
                return pack
    return None
