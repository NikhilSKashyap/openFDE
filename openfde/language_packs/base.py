"""
openfde/language_packs/base.py — the LanguagePack contract.

OpenFDE's core loop (Intent → Architecture → Execute → Review → Remember) must not
know about any one language. Only four seams are language/framework-specific:

    parser           — repo → architecture graph
    testDetector     — repo → the check command(s) to run
    failureParser    — a check's raw output → normalized failure sites
    reproDrafter     — language/framework context for drafting a repro test

This module is the typed door for those seams. The dataclasses normalize to the
EXACT shapes OpenFDE already uses on the wire, so wrapping the current Python code
behind a pack changes no behavior. ``reporter`` is groundwork for structured test
output (a later slice) and is intentionally NOT serialized yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class FailureLocation:
    """A normalized failure site — the shape the Show → hatch already consumes:
    ``{file, line, func, test, message?}`` (message omitted when empty)."""
    file: str
    line: int | None = None
    func: str = ""
    test: str = ""
    message: str = ""

    def as_dict(self) -> dict:
        d = {"test": self.test, "file": self.file, "line": self.line, "func": self.func}
        if self.message:
            d["message"] = self.message
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FailureLocation":
        return cls(file=d.get("file") or "", line=d.get("line"),
                   func=d.get("func") or "", test=d.get("test") or "",
                   message=d.get("message") or "")


@dataclass
class VerifyCheckSpec:
    """A verify check — OpenFDE's existing ``{id, label, command, cwd, required}``
    shape, plus a ``reporter`` for later structured-output parsing. ``as_dict()``
    deliberately omits ``reporter`` so discovery/run stay byte-for-byte unchanged."""
    id: str
    label: str
    command: list
    cwd: str = ""
    required: bool = True
    reporter: str = "text"          # groundwork: "text" | (future) "json" | "junit"

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "command": list(self.command),
                "cwd": self.cwd, "required": self.required}

    @classmethod
    def from_dict(cls, d: dict) -> "VerifyCheckSpec":
        return cls(id=str(d.get("id") or ""),
                   label=str(d.get("label") or d.get("id") or ""),
                   command=[str(x) for x in (d.get("command") or [])],
                   cwd=str(d.get("cwd") or ""),
                   required=bool(d.get("required", True)),
                   reporter=str(d.get("reporter") or "text"))


@runtime_checkable
class LanguagePack(Protocol):
    """One language's implementation of the four seams. Packs WRAP the existing
    OpenFDE functions — the loop calls the pack, never the language directly."""
    name: str
    file_globs: tuple

    def detects(self, root) -> bool:
        """True when this pack applies to the repo (e.g. Python files exist)."""
        ...

    def build_arch_graph(self, root) -> dict:
        """Repo → architecture graph (the canvas's source of truth)."""
        ...

    def discover_checks(self, root) -> list:
        """The repo's verify checks as ``list[VerifyCheckSpec]``."""
        ...

    def parse_failures(self, output: str, root) -> list:
        """A check's raw output → ``list[FailureLocation]``."""
        ...

    def repro_context(self) -> dict:
        """Language/framework context for the repro drafter:
        ``{framework, language, test_command}``."""
        ...

    def ensure_check_config(self, root) -> None:
        """Pin a check config (e.g. ``.openfde/verify.json``) so a bare repo's
        'Run checks' will run the test the repro is about to write. Idempotent."""
        ...
