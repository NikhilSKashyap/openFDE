"""
openfde/workflow_result.py — validate + normalize a Claude Code workflow result
and derive commit metadata (Step 20).

OpenFDE ingests the Step-19 output contract a workflow reports back. This module
validates it defensively (no trust in shape), normalizes every field to a safe
form, and derives a sanitized commit message. Pure functions: no side effects,
no shell, no network.
"""

import logging

logger = logging.getLogger("openfde.workflow_result")

VALID_STATUS = ("passed", "failed", "needs_approval")

_MAX_FILES = 200
_MAX_FUNCS = 200
_MAX_TESTS = 50
_MAX_ERRORS = 50
_MAX_SUGGESTIONS = 50
_MAX_SUMMARY = 4000
_MAX_COMMIT_SUBJECT = 72


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def _as_str(v) -> str:
    return v if isinstance(v, str) else ""


def validate_result(payload) -> tuple:
    """Validate + normalize a workflow result against the output contract.

    Args:
        payload: any — the JSON body posted by the caller.

    Returns:
        tuple — (ok: bool, error: str | None, normalized: dict | None).
                On success `normalized` always has every contract field present
                with a safe type.
    """
    if not isinstance(payload, dict):
        return False, "result must be a JSON object", None

    status = payload.get("status")
    if status not in VALID_STATUS:
        return False, f"status must be one of {list(VALID_STATUS)}", None

    files = []
    for f in _as_list(payload.get("filesChanged")):
        if isinstance(f, dict) and f.get("path"):
            files.append({"path": str(f["path"]), "status": (str(f.get("status", "M"))[:1] or "M")})
        elif isinstance(f, str) and f.strip():
            files.append({"path": f.strip(), "status": "M"})

    functions = []
    for fn in _as_list(payload.get("functionsChanged")):
        if isinstance(fn, dict) and fn.get("name"):
            functions.append({"name": str(fn["name"]), "path": str(fn.get("path", ""))})
        elif isinstance(fn, str) and fn.strip():
            functions.append({"name": fn.strip(), "path": ""})

    tests = []
    for t in _as_list(payload.get("testsRun")):
        if isinstance(t, dict) and t.get("command"):
            tests.append({"command": str(t["command"]), "result": str(t.get("result", ""))})
        elif isinstance(t, str) and t.strip():
            tests.append({"command": t.strip(), "result": ""})

    suggestions = []
    for s in _as_list(payload.get("suggestedCanvasUpdates")):
        if isinstance(s, dict):
            suggestions.append({"boxId": str(s.get("boxId", "")), "change": str(s.get("change", ""))})
        elif isinstance(s, str) and s.strip():
            suggestions.append({"boxId": "", "change": s.strip()})

    errors = [str(e) for e in _as_list(payload.get("errors")) if str(e).strip()]

    normalized = {
        "status": status,
        "filesChanged": files[:_MAX_FILES],
        "functionsChanged": functions[:_MAX_FUNCS],
        "testsRun": tests[:_MAX_TESTS],
        "verificationResult": _as_str(payload.get("verificationResult"))[:200],
        "errors": errors[:_MAX_ERRORS],
        "suggestedCanvasUpdates": suggestions[:_MAX_SUGGESTIONS],
        "reportSummary": _as_str(payload.get("reportSummary"))[:_MAX_SUMMARY],
    }
    return True, None, normalized


# OpenFDE-generated files — never proof of an implementation change.
_BOOKKEEPING_FILES = {"project.md", "plan.md", "report.md", "project_meta.md"}
_BOOKKEEPING_PREFIXES = (".openfde/",)


def is_bookkeeping_path(p) -> bool:
    """Return True if a path is OpenFDE-generated bookkeeping (not source).

    PLAN.md / project.md / REPORT.md / PROJECT_META.md and anything under
    `.openfde/` are written by OpenFDE itself, so a diff in them is never proof
    that a workflow implemented anything.

    Args:
        p: any — a candidate path.

    Returns:
        bool — True when the path is OpenFDE bookkeeping.
    """
    n = str(p or "").strip().strip('"')
    if n.startswith("./"):
        n = n[2:]
    if not n:
        return True
    low = n.lower()
    if low in _BOOKKEEPING_FILES:
        return True
    return any(low.startswith(pre) for pre in _BOOKKEEPING_PREFIXES)


def source_files(files_changed) -> list:
    """Return reported file paths that represent real source changes.

    Filters out OpenFDE bookkeeping files so commit eligibility is judged only on
    actual implementation files.

    Args:
        files_changed: list — normalized filesChanged entries ({path, status}) or
                       raw path strings.

    Returns:
        list[str] — source file paths (bookkeeping removed, order preserved).
    """
    out = []
    for f in files_changed or []:
        p = f.get("path") if isinstance(f, dict) else f
        if p and not is_bookkeeping_path(p):
            out.append(str(p))
    return out


def commit_message(report_summary: str) -> str:
    """Derive a sanitized, capped commit subject from a report summary.

    Args:
        report_summary: str — the workflow's reportSummary.

    Returns:
        str — e.g. "openfde(claude): add /health endpoint".
    """
    lines = (report_summary or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    first = first.replace("`", "").replace("\r", "").strip()
    if not first:
        first = "apply workflow changes"
    return f"openfde(claude): {first[:_MAX_COMMIT_SUBJECT]}"


def tests_summary(tests: list) -> str:
    """Summarize a testsRun list as 'N run, P passed, F failed'.

    Args:
        tests: list[dict] — normalized testsRun entries.

    Returns:
        str — human-readable summary.
    """
    n = len(tests)
    passed = sum(1 for t in tests if str(t.get("result", "")).lower() == "pass")
    failed = sum(1 for t in tests if str(t.get("result", "")).lower() == "fail")
    return f"{n} test command(s) run, {passed} passed, {failed} failed"
