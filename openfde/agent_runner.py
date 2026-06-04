"""
openfde/agent_runner.py — native agent execution loop (Step 22a walking skeleton).

This is the ONE place in OpenFDE that executes: it drives a bounded tool-use loop
against a pluggable transport so a real model can read and write files in the
watched repo. The transport is injectable, so the loop logic is fully testable
with a fake (no network, no key).

Trust boundary (the Step 22 inversion is deliberate and contained here):
  - writes are allowed ONLY to editable, in-scope files;
  - protected files are never written — an attempt forces a `needs_approval`
    outcome that routes to the Step-20 approval gate;
  - out-of-scope / path-traversal writes are rejected and reported back to the
    model so it can correct;
  - the loop is bounded by `max_turns`;
  - the loop never commits or calls git — it returns a Step-20 result contract
    that the server reconciles through the existing gated path.

Transport contract (keeps a fake trivial):
  request  = {"model", "system", "messages": [...], "tools": [...], "max_tokens"}
  response = {"stop_reason": str,
              "content": [ {"type":"text","text":...}
                         | {"type":"tool_use","id","name","input"} ]}
This matches the Anthropic Messages API 1:1 (see anthropic_transport.py).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("openfde.agent_runner")

_MAX_TURNS = 6
_MAX_READ_CHARS = 16_000
_MAX_WRITE_CHARS = 200_000

# ─── Tool schemas (Anthropic tool-use format) ────────────────────────────── #

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the repository (in-scope files only).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative path."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": ("Overwrite an editable, in-scope file with new full contents. "
                        "Protected or out-of-scope paths are rejected."),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative path (must be editable + in scope)."},
                "content": {"type": "string", "description": "Full new file contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "submit_result",
        "description": "Finish the task and report the outcome. Call this exactly once at the end.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["passed", "failed", "needs_approval"]},
                "reportSummary": {"type": "string", "description": "One or two sentences on what changed."},
                "functionsChanged": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "name": {"type": "string"}, "path": {"type": "string"}}},
                },
                "testsRun": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "command": {"type": "string"}, "result": {"type": "string"}}},
                },
                "verificationResult": {"type": "string"},
                "suggestedCanvasUpdates": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "boxId": {"type": "string"}, "change": {"type": "string"}}},
                },
            },
            "required": ["status", "reportSummary"],
        },
    },
]


def build_system_prompt(scope_summary: str, editable: list, protected: list) -> str:
    """Compose the Senior Dev system prompt for one bounded run.

    Args:
        scope_summary: str — human summary of the selected scope.
        editable: list[str] — editable in-scope file paths.
        protected: list[str] — protected file paths (never writable).

    Returns:
        str — the system prompt.
    """
    ed = "\n".join(f"  - {p}" for p in editable) or "  (none)"
    pr = "\n".join(f"  - {p}" for p in protected) or "  (none)"
    return (
        "You are the Senior Dev inside OpenFDE. Implement the requested change by editing "
        "ONLY the editable files listed below, using the provided tools. Keep changes minimal "
        "and correct. When done, call submit_result exactly once.\n\n"
        f"Scope: {scope_summary}\n\n"
        f"Editable files (you may write these):\n{ed}\n\n"
        f"Protected files (NEVER write these — request approval via your report instead):\n{pr}\n\n"
        "Rules:\n"
        "- write_file replaces the entire file; include the complete new contents.\n"
        "- Do not invent files outside the editable list.\n"
        "- If the task truly requires a protected file, set status to needs_approval and explain.\n"
        "- Be concise; do not narrate."
    )


# ─── Path helpers (enforcement lives here) ───────────────────────────────── #

def _norm(p: str) -> str:
    s = (p or "").strip().strip('"')
    return s[2:] if s.startswith("./") else s


def _safe_repo_path(root: Path, rel: str):
    """Resolve a repo-relative path under root, or None if it escapes root."""
    rel = _norm(rel)
    if not rel:
        return None
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


# ─── The loop ────────────────────────────────────────────────────────────── #

def run_agent(transport, *, model, system, user_prompt, root,
              editable_files, protected_files, max_turns=_MAX_TURNS, on_write=None,
              on_read=None, should_cancel=None):
    """Drive a bounded tool-use loop; return a Step-20 result contract.

    Args:
        transport: callable — request dict -> response dict (see module docstring).
        model: str — provider model id.
        system: str — system prompt.
        user_prompt: str — the task.
        root: Path — repository root (write enforcement boundary).
        editable_files: list[str] — writable in-scope paths.
        protected_files: list[str] — protected paths (force needs_approval).
        max_turns: int — hard cap on transport round-trips.
        on_write: callable | None — invoked with each repo-relative path the moment
            it is successfully written (live progress for the canvas). Never raises.

    Returns:
        dict — {result, transcript, writes, rejected, protectedAttempts, turns, error}
               where `result` is a contract accepted by workflow_result.validate_result.
    """
    root = Path(root)
    editable = {_norm(p) for p in (editable_files or [])}
    protected = {_norm(p) for p in (protected_files or [])}

    messages = [{"role": "user", "content": user_prompt}]
    transcript = []
    writes, rejected, protected_attempts = [], [], []
    submitted = None
    error = None
    turns = 0

    def do_read(rel):
        target = _safe_repo_path(root, rel)
        if target is None:
            return False, f"Path '{rel}' is outside the repository."
        if not target.exists() or not target.is_file():
            return False, f"File '{rel}' does not exist."
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"Could not read '{rel}': {exc}"
        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + "\n… [truncated]"
        if on_read:
            try:
                on_read(_norm(rel))
            except Exception:  # noqa: BLE001 — progress must never break the run
                pass
        return True, text

    def do_write(rel, content):
        rel_n = _norm(rel)
        target = _safe_repo_path(root, rel)
        if target is None:
            rejected.append({"path": rel_n or str(rel), "reason": "outside-repo / traversal"})
            return False, f"Path '{rel}' is outside the repository — rejected."
        if rel_n in protected:
            protected_attempts.append(rel_n)
            rejected.append({"path": rel_n, "reason": "protected"})
            return False, f"'{rel}' is protected. Do not write it; request approval in submit_result."
        if rel_n not in editable:
            rejected.append({"path": rel_n, "reason": "out-of-scope"})
            return False, f"'{rel}' is not in the editable scope. Editable: {sorted(editable)}"
        if not isinstance(content, str):
            rejected.append({"path": rel_n, "reason": "non-string-content"})
            return False, "content must be a string."
        if len(content) > _MAX_WRITE_CHARS:
            rejected.append({"path": rel_n, "reason": "content-too-large"})
            return False, "content too large."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            rejected.append({"path": rel_n, "reason": f"write-error: {exc}"})
            return False, f"Could not write '{rel}': {exc}"
        if rel_n not in writes:
            writes.append(rel_n)
        if on_write:
            try:
                on_write(rel_n)
            except Exception:  # noqa: BLE001 — progress must never break the run
                pass
        return True, f"Wrote {rel} ({len(content)} bytes)."

    for turns in range(1, max_turns + 1):
        if should_cancel and should_cancel():
            error = "Cancelled by user."
            break
        try:
            resp = transport({
                "model": model, "system": system,
                "messages": messages, "tools": TOOLS, "max_tokens": 4096,
            })
        except Exception as exc:  # transport/network failure — fail closed
            error = f"transport error: {exc}"
            logger.error("Agent transport failed: %s", exc)
            break

        content = resp.get("content", []) if isinstance(resp, dict) else []
        # Record assistant text for the transcript / ledger.
        for block in content:
            if block.get("type") == "text" and block.get("text", "").strip():
                transcript.append({"role": "assistant", "text": block["text"].strip()})
        messages.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            break  # plain answer with no tools — end the loop

        tool_results = []
        end = False
        for tu in tool_uses:
            name, tid, args = tu.get("name"), tu.get("id"), (tu.get("input") or {})
            if name == "read_file":
                ok, out = do_read(args.get("path", ""))
            elif name == "write_file":
                ok, out = do_write(args.get("path", ""), args.get("content"))
            elif name == "submit_result":
                submitted = args
                ok, out, end = True, "Result recorded.", True
            else:
                ok, out = False, f"Unknown tool '{name}'."
            tool_results.append({
                "type": "tool_result", "tool_use_id": tid,
                "content": out, "is_error": not ok,
            })
        messages.append({"role": "user", "content": tool_results})
        if end:
            break

    result = _build_contract(submitted, writes, rejected, protected_attempts, error)
    return {
        "result": result, "transcript": transcript, "writes": writes,
        "rejected": rejected, "protectedAttempts": protected_attempts,
        "turns": turns, "error": error,
    }


def _build_contract(submitted, writes, rejected, protected_attempts, error) -> dict:
    """Turn loop outcomes into a Step-20 result contract (honest + fail-clear).

    filesChanged always reflects *actual* successful writes, not the model's
    claims. Precedence for the final status:
      1. protected attempt        -> needs_approval (route to the approval gate)
      2. transport/network error  -> failed
      3. denied writes, no writes -> failed (the model cannot claim success when
                                     every action was rejected)
      4. claimed passed, no writes-> failed (no-op is not a passing change)
      5. otherwise                -> passed if any write, else failed
    Rejected reasons are always surfaced in `errors`.
    """
    sub = submitted if isinstance(submitted, dict) else {}
    report = sub.get("reportSummary") or ""
    status = sub.get("status")

    reject_reasons = [f"{r.get('path')}: {r.get('reason')}" for r in (rejected or [])]
    errors = []

    if protected_attempts:
        status = "needs_approval"
        if not report:
            report = f"Requested changes to protected file(s): {', '.join(protected_attempts)}."
    elif error:
        status = "failed"
        report = ("Cancelled by user." if "cancel" in error.lower()
                  else (report or f"Run did not complete: {error}"))
        errors.append(error)
    elif rejected and not writes:
        status = "failed"
        report = report or f"All write attempts were denied: {', '.join(reject_reasons)}."
    elif status == "passed" and not writes:
        status = "failed"
        report = report or "Reported passed but no files were changed."
    elif status not in ("passed", "failed", "needs_approval"):
        status = "passed" if writes else "failed"
        report = report or ("Applied edits." if writes else "No changes were made.")

    # Always make denied attempts visible in the contract.
    errors.extend(reject_reasons)

    return {
        "status": status,
        "reportSummary": report,
        "filesChanged": [{"path": p, "status": "M"} for p in writes],
        "functionsChanged": sub.get("functionsChanged", []),
        "testsRun": sub.get("testsRun", []),
        "verificationResult": sub.get("verificationResult", ""),
        "suggestedCanvasUpdates": sub.get("suggestedCanvasUpdates", []),
        "errors": errors,
    }
