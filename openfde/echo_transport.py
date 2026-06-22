"""
openfde/echo_transport.py — offline "echo" agent transport (Step 22a demo).

Lets you watch the full native-agent loop run in the UI with NO API key and NO
network: draw a dotted box → Execute (openfde-agent) → the echo agent makes one
small, safe, reversible edit to the first editable file → it flows through the
real diff / commit / canvas / timeline / ledger / box-spec path.

It implements the same transport contract as anthropic_transport: it returns
tool_use blocks the runner executes. The edit is a single comment marker chosen
to match the file's language so it stays syntactically valid; JSON (which has no
comments) gets a harmless trailing newline instead.
"""

from datetime import datetime, timezone
from pathlib import Path

# Extension → line-comment style.
_SLASH = {"js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "java", "c", "cc",
          "cpp", "h", "hpp", "kt", "swift", "scala", "php"}
_BLOCK = {"css", "scss", "less"}
_HTMLISH = {"html", "htm", "xml", "md", "markdown", "svg", "vue"}


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def _marked_content(path: str, current: str, iso: str) -> str:
    """Return `current` with a language-appropriate demo marker appended."""
    ext = _ext(path)
    text = f"openfde echo agent — demo edit {iso}"
    base = current.rstrip("\n")
    if ext == "json":
        # JSON has no comments — a trailing newline is a harmless, valid diff.
        return current if current.endswith("\n\n") else (current.rstrip("\n") + "\n\n")
    if ext in _SLASH:
        line = f"// {text}"
    elif ext in _BLOCK:
        line = f"/* {text} */"
    elif ext in _HTMLISH:
        line = f"<!-- {text} -->"
    else:                       # py, sh, yaml, toml, unknown → '#'
        line = f"# {text}"
    return f"{base}\n{line}\n" if base else f"{line}\n"


def make_echo_transport(root, editable_files):
    """Build an offline transport that edits the first editable file then submits.

    Args:
        root: Path | str — repository root (to read current file contents).
        editable_files: list[str] — editable, in-scope paths; the first is edited.

    Returns:
        callable — request(dict) -> response(dict), matching the runner contract.
    """
    root = Path(root)
    target_rel = editable_files[0] if editable_files else ""
    # A directory scope (the generated intent workspace) has no file to edit yet —
    # create a small starter file under it so the keyless demo still produces a
    # real, committable change instead of trying to write the directory itself.
    if target_rel.endswith("/"):
        target_rel = target_rel + "intent_demo.py"
    state = {"i": 0}

    def transport(req: dict) -> dict:
        state["i"] += 1
        if state["i"] == 1 and target_rel:
            iso = datetime.now(timezone.utc).isoformat()
            try:
                current = (root / target_rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                current = ""
            new_content = _marked_content(target_rel, current, iso)
            return {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": f"Echo agent: appending a demo marker to {target_rel}."},
                    {"type": "tool_use", "id": "echo_w", "name": "write_file",
                     "input": {"path": target_rel, "content": new_content}},
                ],
            }
        return {
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "echo_s", "name": "submit_result",
                 "input": {
                     "status": "passed",
                     "reportSummary": (f"Echo agent appended a demo marker to {target_rel} "
                                       "(offline — no model call)."),
                     "functionsChanged": [],
                     "testsRun": [],
                     "verificationResult": "skipped (echo)",
                 }},
            ],
        }

    return transport
