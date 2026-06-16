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
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("openfde.verify")

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
    if _is_pytest_repo(root):
        # The Python pack owns "how do we run pytest here": it resolves the working
        # runner (pytest CLI vs `python3 -m pytest`) and appends OpenFDE's flags
        # (-q --tb=short -p no:cacheprovider — compact "path:line: in func" frames the
        # failure parser reads best; no:cacheprovider keeps .pytest_cache out of the
        # worktree). Lazy import keeps verify ⇄ language_packs acyclic, so a machine
        # where `python3 -m pytest` is broken still gets a green gate.
        from openfde.language_packs.python_pack import resolve_pytest_cmd
        checks.append({"id": "unit-tests", "label": "Unit tests",
                       "command": resolve_pytest_cmd(),
                       "cwd": "", "required": True})
    elif tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
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


def _is_pytest_repo(root) -> bool:
    """Pytest config or a conftest marks the repo as pytest-run.

    Checked BEFORE the unittest heuristic — most of the open-source Python
    ecosystem is pytest (flask, requests, click…). OpenFDE itself has neither
    marker, so its own suite stays on unittest.

    Returns:
        bool — True when the repo should be verified with pytest.
    """
    root = Path(root)
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists() \
            or (root / "tests" / "conftest.py").exists():
        return True
    for name, marker in (("pyproject.toml", "[tool.pytest"),
                         ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]")):
        f = root / name
        try:
            if f.exists() and marker in f.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
    return False


def _tail(text: str, cap: int = _TAIL_CAP) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else "…" + text[-(cap - 1):]


# "File "/abs/or/rel/path.py", line 27, in test_acquire_then_conflict"
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_FAILHEAD_RE = re.compile(r'^(?:FAIL|ERROR): (\S+)', re.M)
_MAX_FAILURES = 5

# Pytest 8.x grammar (all --tb styles share the FAILURES/ERRORS sections; grounded
# in captured output, see tests):
#   =========================== FAILURES ===========================
#   ____________________ TestGroup.test_in_class ____________________   ← header
#   tests/test_calc.py:10: in test_in_class                ← short-tb frame
#   tests/test_calc.py:13:                                 ← long-tb chained frame
#       def deep_helper(x):                                  (func from the last
#   src/calc.py:5: ValueError                                 "def NAME(" above)
#   File "/abs/path.py", line 5, in deep_helper            ← native tb (shared RE)
#   FAILED tests/test_calc.py::test_param[2] - assert 2 == 1   ← short summary
# The "_ _ _ _" chain dividers never match the header RE (no 4 consecutive "_").
_PYTEST_SECTION_RE = re.compile(r"^=+ (?:FAILURES|ERRORS) =+$", re.M)
_PYTEST_HEAD_RE = re.compile(r"^_{4,}\s(.+?)\s_{4,}\s*$", re.M)
_PYTEST_SUMMARY_RE = re.compile(r"^(?:FAILED|ERROR) \S+\.py(?:::\S+)?", re.M)
_PYTEST_LOC_RE = re.compile(r"^([^\s:][^:\n]*\.py):(\d+):(.*)$", re.M)
_PYTEST_DEF_RE = re.compile(r"^\s+def\s+(\w+)\s*\(", re.M)


def _in_repo(root_s: str, fpath: str):
    """Repo-relative path for an in-repo frame, or None (stdlib, site-packages)."""
    p = Path(fpath)
    if not p.is_absolute():
        p = Path(root_s) / fpath
    try:
        r = str(p.resolve())
    except (OSError, ValueError):
        return None
    rel = r.removeprefix(root_s + "/")
    if rel.startswith("/") or rel == r:
        return None
    return rel


def _pytest_test_name(header: str) -> str:
    """Bare test display name from a FAILURES/ERRORS block header.

    "TestGroup.test_in_class" → "test_in_class"; "test_param[2]" stays whole;
    "ERROR collecting tests/test_broken.py" → "tests/test_broken.py".
    """
    h = header.strip()
    if h.startswith("ERROR collecting "):
        return h[len("ERROR collecting "):].strip()
    base = h.split("[")[0]
    return base.split(".")[-1] + h[len(base):]


def _parse_pytest_failures(output: str, root) -> list:
    """Failure sites from pytest FAILURES/ERRORS sections (any --tb style).

    Same law as the unittest path: per failure block the DEEPEST in-repo frame
    wins; short-tb frames carry "in func", long-tb frames recover the function
    from the preceding "def NAME(" context line, native-tb reuses _FRAME_RE.

    Returns:
        list[dict] — [{test, file, line, func}], capped and deduped.
    """
    root_s = str(Path(root).resolve())
    m = _PYTEST_SECTION_RE.search(output)
    region = output[m.start():] if m else output
    parts = _PYTEST_HEAD_RE.split(region)            # [pre, h1, b1, h2, b2, …]
    out, seen = [], set()
    for i in range(1, len(parts) - 1, 2):
        header, block = parts[i], parts[i + 1]
        block = re.split(r"^=+", block, maxsplit=1, flags=re.M)[0]   # stop at summary
        test = _pytest_test_name(header)
        frames, last_end = [], 0
        for fm in _PYTEST_LOC_RE.finditer(block):
            rel = _in_repo(root_s, fm.group(1))
            if rel is None:
                last_end = fm.end()
                continue
            rest = (fm.group(3) or "").strip()
            if rest.startswith("in "):
                func = rest[3:].strip()
            else:                                    # long tb: no "in func" — the
                defs = _PYTEST_DEF_RE.findall(block[last_end:fm.start()])
                func = defs[-1] if defs else test.split("[")[0]
            frames.append({"file": rel, "line": int(fm.group(2)), "func": func})
            last_end = fm.end()
        if not frames:                               # native tb inside the block
            for fm in _FRAME_RE.finditer(block):
                rel = _in_repo(root_s, fm.group(1))
                if rel is not None:
                    frames.append({"file": rel, "line": int(fm.group(2)),
                                   "func": fm.group(3)})
        if not frames:
            continue
        site = frames[-1]                            # deepest in-repo frame
        key = f"{site['file']}:{site['line']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"test": test, "file": site["file"], "line": site["line"],
                    "func": site["func"]})
        if len(out) >= _MAX_FAILURES:
            break
    if out:
        return out
    # ── --tb=line fallback: no per-test headers — the FAILURES section is one
    # "path:line: message" per failure, in test order, so pair locations with
    # the short-summary FAILED lines by index for the test names. The location
    # is the raise site (deepest frame), consistent with the law above. With no
    # function name in this format, the bare test name stands in — the canvas
    # re-resolves the true enclosing function from the line anyway. ──
    cut = re.search(r"^=+ short test summary", region, re.M)
    body = region[:cut.start()] if cut else region
    names = [m.group(1).split("::")[-1] for m in
             re.finditer(r"^(?:FAILED|ERROR) \S+\.py::(\S+)", output, re.M)]
    for i, fm in enumerate(_PYTEST_LOC_RE.finditer(body)):
        rel = _in_repo(root_s, fm.group(1))
        if rel is None:
            continue
        key = f"{rel}:{fm.group(2)}"
        if key in seen:
            continue
        seen.add(key)
        test = names[i] if i < len(names) else ""
        out.append({"test": test, "file": rel, "line": int(fm.group(2)),
                    "func": test.split("[")[0]})
        if len(out) >= _MAX_FAILURES:
            break
    return out


def parse_failure_locations(output: str, root) -> list:
    """Repo-relative failure sites from a unittest-style traceback.

    For each FAIL/ERROR block: every in-repo frame is considered and the
    DEEPEST one wins (the assertion or raise site — usually the test line, or
    the app frame when the error happened inside product code). Frames outside
    the repo (stdlib, site-packages) are ignored. Capped, deduped by file:line.

    Returns:
        list[dict] — [{test, file, line, func}] suitable for the Show → hatch.
    """
    out_text = output or ""
    # Pytest first: its FAILURES/ERRORS sections (or FAILED file::node summary
    # lines) never co-occur with unittest's "FAIL: test (…)" headers.
    if _PYTEST_SECTION_RE.search(out_text) or _PYTEST_SUMMARY_RE.search(out_text):
        return _parse_pytest_failures(out_text, root)
    root_s = str(Path(root).resolve())
    tests = _FAILHEAD_RE.findall(out_text)
    blocks = re.split(r'^(?:FAIL|ERROR): \S+.*$', out_text, flags=re.M)[1:]
    out, seen = [], set()
    for i, block in enumerate(blocks):
        frames = []
        for m in _FRAME_RE.finditer(block):
            rel = _in_repo(root_s, m.group(1))
            if rel is None:
                continue                       # outside the repo (stdlib etc.)
            frames.append({"file": rel, "line": int(m.group(2)), "func": m.group(3)})
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


def recheck_single_test(root, base_cmd: list, test_name: str,
                        timeout: int = 120) -> dict:
    """Re-run ONE test by name through the repo's own check command (pytest -k).

    The repair loop's fast verdict: after a fix lands, does the exact failing
    test now pass? Parametrize ids are stripped for -k (substring match); the
    full gate still owns the real receipt. rc==5 (nothing collected) is an
    ERROR, never a pass.

    Returns:
        dict — {status: passed|failed|error, tail}.
    """
    if not base_cmd or "pytest" not in " ".join(map(str, base_cmd)):
        return {"status": "error", "tail": "recheck needs a pytest check command"}
    base = str(test_name or "").split("[")[0].strip()
    if not base:
        return {"status": "error", "tail": "no test name on the receipt"}
    cmd = [*base_cmd, "-k", base]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "tail": f"recheck timed out after {timeout}s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "tail": str(exc)[:300]}
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-800:]
    if proc.returncode == 5:
        return {"status": "error", "tail": f"no test matched -k {base!r}"}
    return {"status": "passed" if proc.returncode == 0 else "failed", "tail": tail}


def run_fault_domain(art: dict) -> str:
    """Whose failure is a repair-run outcome — OURS or the repo's?

    The rule is honest and simple: a run that FAILED to execute/produce is an
    OPENFDE failure (our runner, our scope, our machinery — the user should be
    able to report it to us); a run that executed cleanly but whose recheck
    still fails is the REPO's reality (the trail continues: explain → prompt →
    run again). A clean run with a passing recheck is nobody's failure.

    Returns:
        str — "openfde" | "repo" | "".
    """
    if art.get("status") in (None, "failed"):
        return "openfde"
    if art.get("recheck") == "failed":
        return "repo"
    return ""


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
        # Parsing is a language seam → route through the pack (raw fallback).
        locs = _parse_via_packs(output, root)
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
        checks = _discover_via_packs(root)
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


# ── Language seam ──────────────────────────────────────────────────────────────
# Discovery + failure parsing are language-specific, so they route through the
# LanguagePack registry (Python is Pack #1, JS/TS is Pack #2). A repo can match more
# than one pack: discovery collects every pack's checks (dedup by id), and failure
# parsing tries each pack in detection order and takes the FIRST non-empty result —
# so a polyglot repo still reads a Vitest/Jest failure even though Python is tried
# first. PythonPack wraps parse_failure_locations below, so single-language Python
# behavior is byte-for-byte unchanged. The raw fallbacks cover repos no pack claims
# (config-only / frontend-only). Imports are lazy to keep verify ↔ language_packs
# free of an import cycle.

def _valid_check_list(value) -> bool:
    """v1-K hardening: a plugin ``test_detector`` result is acceptable only as a LIST of dicts, each
    with at least a non-empty string ``id`` and a list/tuple ``command``. Lenient otherwise — extra
    fields are preserved, an empty list is valid (a repo with no checks). A trusted hook returning the
    wrong type must not crash the discovery path; it falls back."""
    if not isinstance(value, list):
        return False
    for c in value:
        if not isinstance(c, dict):
            return False
        if not (isinstance(c.get("id"), str) and c.get("id")):
            return False
        if not isinstance(c.get("command"), (list, tuple)):
            return False
    return True


def _pack_checks(pack, root) -> list:
    """One pack's checks as ``[as_dict]`` — PREFERRING its plugin ``test_detector`` runtime hook (v1-K),
    else the pack's in-core discovery. A missing/failing/**malformed** hook falls back. Scoped by
    ``provider_id=pack.name`` so only THIS pack's hook is preferred, preserving pack order."""
    from openfde import plugins
    r = plugins.run_capability_hook(root, "test_detector", lambda h: h(root), provider_id=pack.name)
    if r is not plugins.NO_HOOK:
        if _valid_check_list(r):
            return r                               # valid hook output (standard check dicts) wins
        logger.warning("plugin 'test_detector' hook for %s returned a malformed result (%s); "
                       "using in-core discovery", pack.name, type(r).__name__)
    return [spec.as_dict() for spec in pack.discover_checks(root)]


def _discover_via_packs(root) -> list:
    """discover_checks() through the language packs (dedup by check id), preferring each pack's plugin
    ``test_detector`` runtime hook (v1-K) with fallback to its in-core discovery."""
    from openfde.language_packs import get_language_packs
    packs = get_language_packs(root)
    if not packs:
        return discover_checks(root)
    out, seen = [], set()
    for pack in packs:
        for d in _pack_checks(pack, root):
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            out.append(d)
    return out


def _valid_failure_list(value) -> bool:
    """v1-K hardening: a plugin ``failure_parser`` result is acceptable only as a LIST of dicts, each
    with at least a non-empty string ``file`` and an integer ``line`` (a bool is not a line). Lenient
    otherwise — extra fields preserved, an empty list valid. A trusted hook returning a wrong shape must
    not pass invalid data through as if it were failure locations; it falls back."""
    if not isinstance(value, list):
        return False
    for loc in value:
        if not isinstance(loc, dict):
            return False
        if not (isinstance(loc.get("file"), str) and loc.get("file")):
            return False
        if not isinstance(loc.get("line"), int) or isinstance(loc.get("line"), bool):
            return False
    return True


def _pack_failures(pack, output: str, root) -> list:
    """One pack's failure locations as ``[as_dict]`` — PREFERRING its plugin ``failure_parser`` runtime
    hook (v1-K), else the pack's in-core parser. A missing/failing/**malformed** hook falls back. Scoped
    by ``provider_id=pack.name`` so only THIS pack's hook is preferred, preserving the cross-pack order
    (first non-empty wins)."""
    from openfde import plugins
    r = plugins.run_capability_hook(root, "failure_parser", lambda h: h(output, root),
                                    provider_id=pack.name)
    if r is not plugins.NO_HOOK:
        if _valid_failure_list(r):
            return r                               # valid hook output (standard failure dicts) wins
        logger.warning("plugin 'failure_parser' hook for %s returned a malformed result (%s); "
                       "using in-core parser", pack.name, type(r).__name__)
    return [loc.as_dict() for loc in pack.parse_failures(output, root)]


def _parse_via_packs(output: str, root) -> list:
    """Failure locations through the detected packs — the FIRST non-empty wins.

    A repo can match several packs (e.g. Python + JS/TS), but one check's output is
    in exactly one language, so we try each detected pack in detection order and
    return the first that actually parses something: PythonPack sees Vitest/Jest
    output as unknown and returns [], then JsTsPack parses it. Python behavior is
    unchanged — its parser is first, so when it succeeds it wins. A repo no pack
    claims (or output nobody parses) falls back to the raw unittest/pytest parser.
    """
    from openfde.language_packs import get_language_packs
    packs = get_language_packs(root)
    if not packs:
        return parse_failure_locations(output, root)
    for pack in packs:
        locs = _pack_failures(pack, output, root)
        if locs:
            return locs
    # Nobody matched. PythonPack's parser IS parse_failure_locations, so if Python
    # was among the packs this is already []; otherwise give the raw parser a last
    # chance for Python-style output in a non-Python pack's repo.
    return parse_failure_locations(output, root)
