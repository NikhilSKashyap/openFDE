"""
openfde/verify.py — Verify Gate Evidence v1 (local checks, visible receipts).

*Trust needs receipts.* Before OpenFDE lands an episode it can run the repo's local
checks and keep the evidence — command, status, short summary, capped output tail,
timing — on the episode (and as the latest worktree result). Auto-Land treats a
failed required check as a blocker; an explicit user Land stays the escape hatch
(evidence still recorded, failure still visible). When nothing is configured the
gate records **skipped** evidence instead of pretending success.

This is deliberately *not* a CI product: deterministic discovery (an optional
``.openfde/verify.json``, else two obvious heuristics — Python unittest when
``tests/`` has test files, ``npm run lint`` when ``frontend/package.json`` defines
it), local subprocesses with a timeout, and a hard cap on stored output. No GitHub
Actions, no policy engine, no remote anything in v1.

Pure-ish helpers; the subprocess runner is injectable for tests.
"""

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

VERIFY_CONFIG = "verify.json"          # optional override: .openfde/verify.json
_TAIL_CAP = 2000                       # chars of combined output kept as evidence
_SUMMARY_CAP = 120
_CHECK_TIMEOUT = 300                   # seconds per check

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_checks(root) -> list:
    """Deterministically discover this repo's verify checks.

    Order of truth: an explicit ``.openfde/verify.json`` (a list of
    ``{id, label, command[], cwd?, required?}``) wins; otherwise two heuristics:

      - ``tests/`` containing ``test_*.py`` → ``python3 -m unittest discover -s tests``
      - ``frontend/package.json`` with a ``lint`` script → ``npm run lint`` in frontend/

    The frontend *build* is intentionally not auto-discovered in v1 (expensive on
    every land); add it via the config file when wanted.

    Returns:
        list[dict] — checks ({id, label, command, cwd, required}); [] when nothing
            is configured (the gate then records skipped evidence).
    """
    root = Path(root)
    cfg = root / ".openfde" / VERIFY_CONFIG
    if cfg.exists():
        try:
            raw = json.loads(cfg.read_text())
        except (json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, list):
            checks = []
            for i, c in enumerate(raw):
                if not isinstance(c, dict) or not isinstance(c.get("command"), list) \
                        or not c["command"]:
                    continue
                checks.append({
                    "id": str(c.get("id") or f"check-{i + 1}"),
                    "label": str(c.get("label") or c.get("id") or f"Check {i + 1}"),
                    "command": [str(x) for x in c["command"]],
                    "cwd": str(c["cwd"]) if c.get("cwd") else "",
                    "required": bool(c.get("required", True)),
                })
            return checks

    checks = []
    tests_dir = root / "tests"
    if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
        checks.append({"id": "unit-tests", "label": "Unit tests",
                       "command": ["python3", "-m", "unittest", "discover", "-s", "tests"],
                       "cwd": "", "required": True})
    pkg = root / "frontend" / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text()) or {}).get("scripts") or {}
        except (json.JSONDecodeError, OSError):
            scripts = {}
        if "lint" in scripts:
            checks.append({"id": "frontend-lint", "label": "Frontend lint",
                           "command": ["npm", "run", "lint"],
                           "cwd": "frontend", "required": True})
    return checks


def _tail(text: str, cap: int = _TAIL_CAP) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else "…" + text[-(cap - 1):]


# "File "/abs/or/rel/path.py", line 27, in test_acquire_then_conflict"
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_FAILHEAD_RE = re.compile(r'^(?:FAIL|ERROR): (\S+)', re.M)
_MAX_FAILURES = 5


def parse_failure_locations(output: str, root) -> list:
    """Repo-relative failure sites from a unittest-style traceback.

    For each FAIL/ERROR block: every in-repo frame is considered and the
    DEEPEST one wins (the assertion or raise site — usually the test line, or
    the app frame when the error happened inside product code). Frames outside
    the repo (stdlib, site-packages) are ignored. Capped, deduped by file:line.

    Returns:
        list[dict] — [{test, file, line, func}] suitable for the Show → hatch.
    """
    root_s = str(Path(root).resolve())
    tests = _FAILHEAD_RE.findall(output or "")
    blocks = re.split(r'^(?:FAIL|ERROR): \S+.*$', output or "", flags=re.M)[1:]
    out, seen = [], set()
    for i, block in enumerate(blocks):
        frames = []
        for m in _FRAME_RE.finditer(block):
            fpath, line, func = m.group(1), int(m.group(2)), m.group(3)
            p = Path(fpath)
            if not p.is_absolute():
                p = Path(root_s) / fpath
            try:
                rel = str(p.resolve()).removeprefix(root_s + "/")
            except (OSError, ValueError):
                continue
            if rel.startswith("/") or rel == str(p.resolve()):
                continue                       # outside the repo (stdlib etc.)
            frames.append({"file": rel, "line": line, "func": func})
        if not frames:
            continue
        site = frames[-1]                      # deepest in-repo frame
        key = f"{site['file']}:{site['line']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"test": tests[i] if i < len(tests) else "",
                    "file": site["file"], "line": site["line"],
                    "func": site["func"]})
        if len(out) >= _MAX_FAILURES:
            break
    return out


def _summary(output: str, exit_code: int) -> str:
    """A one-line receipt: the last meaningful output line, with a terse final line
    folded into its predecessor — unittest's "Ran 155 tests in 11.5s" + "OK" becomes
    "Ran 155 tests in 11.5s — OK". Falls back to the exit code."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "exit 0" if exit_code == 0 else f"exit {exit_code}"
    last = lines[-1]
    if len(last) < 8 and len(lines) >= 2:
        last = f"{lines[-2]} — {last}"
    return last[:_SUMMARY_CAP]


def run_check(root, check: dict, *, runner=None, timeout: int = _CHECK_TIMEOUT) -> dict:
    """Run one check and return its evidence (never raises).

    Returns:
        dict — {id, label, command, cwd, required, status, exitCode, summary,
                outputTail, startedAt, finishedAt, durationMs}.
    """
    run = runner or subprocess.run
    cwd = Path(root) / check["cwd"] if check.get("cwd") else Path(root)
    started, t0 = _now(), time.monotonic()
    try:
        proc = run(check["command"], cwd=str(cwd), capture_output=True, text=True,
                   timeout=timeout)
        exit_code = getattr(proc, "returncode", 1)
        output = (getattr(proc, "stdout", "") or "") + "\n" + (getattr(proc, "stderr", "") or "")
    except subprocess.TimeoutExpired:
        exit_code, output = -1, f"timed out after {timeout}s"
    except FileNotFoundError:
        exit_code, output = -1, f"command not found: {check['command'][0]}"
    except Exception as exc:  # noqa: BLE001 — a broken check must record, not raise
        exit_code, output = -1, f"check error: {exc}"
    dur_ms = int((time.monotonic() - t0) * 1000)
    status = PASSED if exit_code == 0 else FAILED
    evidence = {
        "id": check["id"], "label": check["label"],
        "command": " ".join(check["command"]), "cwd": check.get("cwd") or "",
        "required": bool(check.get("required", True)),
        "status": status,
        "exitCode": exit_code,
        "summary": _summary(output, exit_code),
        "outputTail": _tail(output),
        "startedAt": started, "finishedAt": _now(), "durationMs": dur_ms,
    }
    if status == FAILED:
        # Failure LOCATIONS, parsed from the FULL output before tailing — these
        # power "Show →": receipt → exact function on the canvas → repair hatch.
        locs = parse_failure_locations(output, root)
        if locs:
            evidence["failures"] = locs
    return evidence


def run_verification(root, *, checks=None, runner=None, timeout: int = _CHECK_TIMEOUT) -> dict:
    """Run all checks (discovered unless given) and fold them into one verdict.

    Overall status: **failed** if any *required* check failed, **passed** when every
    required check passed, **skipped** when no checks exist (recorded explicitly —
    "verification not configured" — never silent success).

    Returns:
        dict — {status, checks[], ranAt, durationMs, note?}.
    """
    if checks is None:
        checks = discover_checks(root)
    started, t0 = _now(), time.monotonic()
    if not checks:
        return {"status": SKIPPED, "checks": [], "ranAt": started, "durationMs": 0,
                "note": "verification not configured — no checks discovered"}
    evidence = [run_check(root, c, runner=runner, timeout=timeout) for c in checks]
    failed_required = [e for e in evidence if e["required"] and e["status"] == FAILED]
    return {
        "status": FAILED if failed_required else PASSED,
        "checks": evidence,
        "ranAt": started,
        "durationMs": int((time.monotonic() - t0) * 1000),
    }
