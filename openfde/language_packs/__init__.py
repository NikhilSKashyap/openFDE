"""
openfde.language_packs — the LanguagePack registry.

The language/framework-specific seams (arch graph, verify discovery, failure
parsing, repro context) live behind a pack so the core loop stays language-agnostic.
Python is Pack #1; the door is open for JS/TS, Go, Rust to follow.
"""
from .base import FailureLocation, LanguagePack, VerifyCheckSpec
from .js_ts_pack import JsTsPack
from .python_pack import PythonPack
from .registry import all_language_packs, get_language_packs, get_pack_for_file

__all__ = [
    "FailureLocation",
    "VerifyCheckSpec",
    "LanguagePack",
    "PythonPack",
    "JsTsPack",
    "get_language_packs",
    "get_pack_for_file",
    "all_language_packs",
]
