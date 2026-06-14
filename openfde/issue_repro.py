"""
openfde/issue_repro.py — the Reproduce button: issue text → honest verdict.

Not every issue is a bug, and many bug reports carry no reproduction signal —
so this module's first job is to REFUSE WELL:

  • triage first — deterministic signals plus an optional strict-JSON LLM pass
    that may only make the verdict MORE conservative unless the text carries
    real anchors. Feature requests/questions → "not_a_bug"; bug-shaped issues
    without enough signal → "insufficient", with the missing pieces named.
  • locate the claim in the repo before believing it — files/symbols/error
    strings from the issue are checked against HEAD; stale issues surface here.
  • only then draft ONE failing test (the senior_dev text role, matched to the
    repo's own test conventions) asserting the issue's DESIRED behavior.
  • run exactly that test through the repo's own check command:
    FAIL → "reproduced" (a real receipt; the test stays as the work product);
    PASS → "not_reproduced" (likely stale/already fixed) and the write is
    REVERTED — a repro that doesn't reproduce never pollutes the worktree.

Every verdict says what was done and why. Nothing is ever fabricated.
"""

import ast
import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("openfde.issue_repro")

_MAX_BODY = 4000
_MAX_TARGETS = 3
_MAX_TEST_LINES = 80
_RUN_TIMEOUT = 180

_FEATURE_TITLE_RE = re.compile(
    r"^\s*(add|support|feature|request|suggestion|suggest|proposal|propose|docs?|"
    r"how |can |could |consider|need support|any interest|is there)", re.I)
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)|^\s*File \"", re.M)
_PYTEST_TB_RE = re.compile(r"^[^\s:]+\.py:\d+: in \w+", re.M)
_ERROR_NAME_RE = re.compile(r"\b([A-Z]\w+(?:Error|Exception))\b")
_FILE_REF_RE = re.compile(r"\b([\w\-]+(?:/[\w\-]+)*\.py)\b")
_SYMBOL_RE = re.compile(r"`([\w.]+)`|(\b\w+(?:\.\w+)+)\(\)")
_QUOTED_ERR_RE = re.compile(r"[\"“']([^\"“”']{12,80})[\"”']")


def issue_body_hash(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8", "replace")).hexdigest()[:12]


def _signal_scan(title: str, body: str, labels: list) -> dict:
    text = f"{title}\n{body or ''}"
    lset = {str(x).lower() for x in (labels or [])}
    return {
        "label_bug": bool(lset & {"bug", "defect", "regression"}),
        "label_feature": bool(lset & {"enhancement", "feature", "feature request",
                                      "question", "provider support", "documentation"}),
        "title_feature": bool(_FEATURE_TITLE_RE.search(title or "")),
        "has_traceback": bool(_TRACEBACK_RE.search(body or "")
                              or _PYTEST_TB_RE.search(body or "")),
        "error_names": sorted(set(_ERROR_NAME_RE.findall(body or ""))),
        "has_code_block": "```" in (body or ""),
        "file_refs": sorted({m for m in _FILE_REF_RE.findall(text)}),
        "symbol_refs": sorted({a or b for a, b in _SYMBOL_RE.findall(text) if (a or b)}),
        "expected_actual": bool(re.search(r"\bexpected\b", text, re.I)
                                and re.search(r"\bactual|\binstead\b|\bbut got\b|\bwould be\b",
                                              text, re.I)),
    }


def _json_block(text: str):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


def triage_issue(title: str, body: str, labels: list, caller=None) -> dict:
    """Classify an issue for reproducibility — conservative by construction.

    Deterministic signals decide first; the optional LLM pass may freely
    DOWNGRADE (to not_a_bug/insufficient) but may only confirm "candidate"
    when the text itself carries anchors (a file, symbol, or error name).

    Returns:
        dict — {verdict: candidate|not_a_bug|insufficient, kind, missing,
                targets, failureMode, desired, signals}.
    """
    sig = _signal_scan(title, body, labels)
    bug_signal = (sig["has_traceback"] or bool(sig["error_names"])
                  or sig["expected_actual"] or sig["label_bug"])
    anchors = bool(sig["file_refs"] or sig["symbol_refs"] or sig["error_names"])

    out = {"kind": "bug", "missing": [], "targets": [], "failureMode": "",
           "desired": "", "signals": sig}
    if (sig["title_feature"] or sig["label_feature"]) and not bug_signal:
        return {**out, "verdict": "not_a_bug", "kind": "feature",
                "missing": ["this reads as a feature request / question — "
                            "there is no wrong behavior to reproduce"]}
    if not bug_signal:
        return {**out, "verdict": "insufficient",
                "missing": ["an error message or traceback",
                            "steps or code that trigger the problem"]}
    if not anchors:
        return {**out, "verdict": "insufficient",
                "missing": ["a code location — a file, function, or the exact "
                            "error text to search for"]}

    verdict = {**out, "verdict": "candidate",
               "targets": [{"file": f} for f in sig["file_refs"][:_MAX_TARGETS]]}

    if caller:
        sys_prompt = (
            "You triage GitHub issues for automatic reproduction. Given an issue, "
            'return ONLY JSON: {"kind": "bug"|"feature"|"question"|"unclear", '
            '"reproducible": bool, "missing": [str], '
            '"targets": [{"file": str, "symbol": str}], '
            '"failure_mode": str, "desired_behavior": str}. '
            "Be conservative: reproducible=true ONLY if the text alone gives enough "
            "to write a failing unit test (what to call, with what, what should "
            "happen, what happens instead).")
        user = (f"title: {title}\nlabels: {', '.join(map(str, labels or []))}\n\n"
                f"{(body or '')[:_MAX_BODY]}")
        try:
            data = _json_block(caller(sys_prompt, user)) or {}
        except Exception as exc:  # noqa: BLE001 — triage must never crash
            logger.warning("triage LLM failed: %s", exc)
            data = {}
        kind = data.get("kind")
        if kind in ("feature", "question"):
            return {**out, "verdict": "not_a_bug", "kind": kind,
                    "missing": [f"the agent reads this as a {kind} — nothing to reproduce"]}
        if data.get("reproducible") is False:
            missing = [str(x) for x in (data.get("missing") or [])][:4]
            return {**out, "verdict": "insufficient",
                    "missing": missing or ["the agent could not derive a reproduction "
                                           "from the text"]}
        for t in (data.get("targets") or [])[:_MAX_TARGETS]:
            if isinstance(t, dict) and (t.get("file") or t.get("symbol")):
                verdict["targets"].append({"file": t.get("file", ""),
                                           "symbol": t.get("symbol", "")})
        verdict["failureMode"] = str(data.get("failure_mode") or "")[:200]
        verdict["desired"] = str(data.get("desired_behavior") or "")[:200]
    return verdict


def locate_targets(root, triage: dict, body: str) -> list:
    """Ground the issue's claims in the repo at HEAD — or report they aren't there.

    Returns:
        list[dict] — [{file, line?}] existing, source-preferred, capped.
    """
    root = Path(root)
    seen, out = set(), []

    def _add(rel, line=None):
        rel = str(rel).lstrip("./")
        if rel in seen or len(out) >= _MAX_TARGETS:
            return
        p = root / rel
        try:
            if not (p.resolve().is_file()
                    and str(p.resolve()).startswith(str(root.resolve()) + "/")):
                return
        except OSError:
            return
        seen.add(rel)
        out.append({"file": rel, **({"line": line} if line else {})})

    for t in triage.get("targets") or []:
        if t.get("file"):
            _add(t["file"])
    # source files before tests — the bug lives in the product
    out.sort(key=lambda x: x["file"].startswith("tests"))

    if not out:
        # Search the exact error strings the reporter quoted.
        needles = _QUOTED_ERR_RE.findall(body or "")[:3]
        needles += triage.get("signals", {}).get("error_names", [])[:2]
        for needle in needles:
            try:
                proc = subprocess.run(
                    ["grep", "-rln", "--include=*.py", needle, "."],
                    cwd=str(root), capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                continue
            for ln in (proc.stdout or "").splitlines():
                if "test" not in ln:
                    _add(ln.strip())
    return out


def map_issue_to_files(root, title: str, body: str, caller=None, cap: int = 3) -> list:
    """Best-effort code ties for an issue WITHOUT a reproduction.

    Named files / quoted error strings first (deterministic); else, with an
    agent, a strict-JSON pick from the repo's own source list — only entries
    FROM the list are accepted. The tie is navigational (canvas/episode
    anchoring), never treated as proof.

    Returns:
        list[str] — up to ``cap`` existing repo-relative source files.
    """
    sig = _signal_scan(title, body, [])
    found = locate_targets(root, {"targets": [{"file": f} for f in sig["file_refs"]],
                                  "signals": sig}, body)
    if found or caller is None:
        return [t["file"] for t in found][:cap]
    try:
        proc = subprocess.run(["git", "ls-files", "*.py"], cwd=str(root),
                              capture_output=True, text=True, timeout=15)
        files = [ln.strip() for ln in (proc.stdout or "").splitlines()
                 if ln.strip() and not ln.startswith("tests/")][:400]
    except (OSError, subprocess.SubprocessError):
        return []
    if not files:
        return []
    sys_prompt = ("Pick the existing source files this GitHub issue most concerns. "
                  'Return ONLY JSON {"files": ["path", ...]} with at most '
                  f"{cap} entries, each EXACTLY as it appears in the list.")
    user = (f"title: {title}\n\n{(body or '')[:2000]}\n\n--- files ---\n"
            + "\n".join(files))
    try:
        data = _json_block(caller(sys_prompt, user)) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("issue→files mapping failed: %s", exc)
        return []
    allowed = set(files)
    return [f for f in (data.get("files") or []) if f in allowed][:cap]


def scrub_report(text: str, replacements: dict) -> str:
    """Deterministic post-pass over an LLM-drafted report.

    The model receives the ACCURATE receipt (paths, test names) for precision —
    but the public tracker must never see them. Every known repo-identifying
    string is replaced with its placeholder, longest-first so full paths win
    over their basenames. The model writes precisely; this guarantees the leak
    surface stays zero regardless of what it wrote.
    """
    out = text or ""
    for needle, repl in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
        if needle and len(needle) >= 3:
            out = out.replace(needle, repl)
    # Cost is data, never display — strip it here too, so no receipt vintage or
    # model phrasing can carry "(cost $X)" onto the tracker.
    return re.sub(r"\s*\(cost \$[\d.]+\)", "", out)


def report_replacements(scope: list, file: str = "", test: str = "",
                        repo_name: str = "") -> dict:
    """The scrub map for one repair-run report: paths AND their basenames."""
    repl = {}
    for f in (scope or []):
        tag = "<test-file>" if "test" in f.lower() else "<source-file>"
        repl[f] = tag
        repl[f.rsplit("/", 1)[-1]] = tag
    if file:
        tag = "<test-file>" if "test" in file.lower() else "<source-file>"
        repl.setdefault(file, tag)
        repl.setdefault(file.rsplit("/", 1)[-1], tag)
    if test:
        repl[test] = "<failing-test>"
        repl[test.split("[")[0]] = "<failing-test>"
    if repo_name:
        repl[repo_name] = "<repo>"
    return repl


def deterministic_report(run: dict, ctx: dict) -> tuple:
    """The template fallback (no provider / bad JSON): repo-clean by
    construction — it interpolates only shape, status, and OpenFDE's own
    contract strings.

    Returns:
        (title, body) — strings ready for the report card.
    """
    reason = re.sub(r"\s*\(cost \$[\d.]+\)", "", run.get("error") or run.get("summary") or "")
    scope = run.get("scope") or []
    tests = sum(1 for f in scope if "test" in f.lower())
    shape = f"{len(scope)} file{'' if len(scope) == 1 else 's'} ({tests} test, {len(scope) - tests} source)"
    if "no in-scope changes" in reason:
        suspect = ("Run scope / prompt staleness — the runner produced no diff inside "
                   "its editable scope. Look at post_hatch_run scope derivation "
                   "(failure_flow.chain_files) and repair-prompt reuse: a cached prompt "
                   "for an already-applied fix tells the agent to do nothing.")
    elif "timed out" in reason.lower():
        suspect = "Runner timeout handling (claude_code_runner)."
    elif "CLI not found" in reason:
        suspect = "Provider availability checks (agent settings → runner dispatch)."
    elif "uncommitted changes" in reason:
        suspect = "allow_dirty scoping in run_claude_code."
    else:
        suspect = "Runner contract (claude_code_runner._build_contract) / post_hatch_run pipeline."
    title = f"Repair hatch: Run with Senior Dev failed — {(reason or 'no reason surfaced')[:90]}"
    body = "\n".join([
        "## OpenFDE bug report",
        "",
        "- Feature: Repair hatch → Run with Senior Dev (scoped repair runner)",
        f"- OpenFDE commit: {run.get('openfdeVersion') or 'unknown'} (the OpenFDE install — not the watched repo)",
        f"- Provider path: {run.get('source') or 'senior_dev'}",
        "",
        "## How it was produced (OpenFDE actions only)",
        "1. A failing check sat on an episode (Run checks → red receipt).",
        "2. Show → opened the repair hatch on the failing function.",
        "3. Generate prompt composed the repair prompt (fingerprint-cached).",
        f"4. Run with Senior Dev executed the runner — editable scope: {shape}, allow_dirty, no-commit directive.",
        "",
        "## Expected",
        "The runner edits within its scope and the failing check passes the single-test recheck.",
        "",
        "## Actual",
        f"- status: {run.get('status') or 'failed'}",
        f"- reason: {reason or '(none surfaced — that absence is itself a bug)'}",
        f"- recheck: {run.get('recheck') or 'not reached'}",
        "",
        "## Suspected area / possible fix",
        suspect,
    ])
    return title, body


def find_test_home(root, target_file: str) -> dict:
    """The repo-conventional place for the repro test (+ a style excerpt).

    Prefers an existing test file that imports the target module; falls back
    to a new tests/ file named for the target.

    Returns:
        dict — {path, excerpt, exists}.
    """
    root = Path(root)
    stem = Path(target_file).stem
    pkg_dots = str(Path(target_file).with_suffix("")).replace("/", ".")
    best, best_score = None, 0
    pkg_re = re.compile(rf"\b{re.escape(pkg_dots)}\b")
    for tf in sorted(root.glob("tests/**/test_*.py")):
        try:
            head = tf.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        score = 0
        if tf.stem == f"test_{stem}":
            score += 6                      # name-exact mirror wins decisively
        if stem in tf.parts[:-1]:
            score += 3                      # tests/<stem>/… directory mirror
        if pkg_re.search(head):
            score += 4                      # word-bounded module import — NOT a
        if stem in tf.stem:                 # substring ('.client' once matched
            score += 2                      # mcp.client's transport tests)
        score += len(set(Path(target_file).parts[:-1]) & set(tf.parts))
        if score > best_score:
            best, best_score = tf, score
    if best is not None and best_score >= 3:
        rel = str(best.relative_to(root))
        return {"path": rel, "excerpt": best.read_text(encoding="utf-8",
                                                       errors="replace")[:3500],
                "exists": True}
    return {"path": f"tests/test_{stem}_repro.py", "excerpt": "", "exists": False}


def draft_repro_test(caller, issue_ctx: str, target: dict, home: dict):
    """One failing test in the repo's own dialect — or None, honestly.

    The test must assert the issue's DESIRED behavior (so it fails while the
    bug exists). Appended to the conventional test file; any imports beyond
    that file's existing ones must live inside the test function.

    Returns:
        dict | None — {name, code} (validated: parses, single test_ def, capped).
    """
    if caller is None:
        return None
    sys_prompt = (
        "You are the Senior Dev. Write exactly ONE pytest test function that "
        "REPRODUCES the GitHub issue below: assert the DESIRED behavior, so the "
        "test FAILS while the bug exists and passes once fixed. Match the style "
        "of the existing test file excerpt. Imports beyond the excerpt's go "
        "INSIDE the test function. Return ONLY JSON: "
        '{"name": "test_…", "code": "<the complete function source>"}.')
    user = (f"{issue_ctx}\n\n--- target code ({target.get('file')}) ---\n"
            f"{target.get('snippet', '')[:2500]}\n\n"
            f"--- existing test file excerpt ({home['path']}) ---\n"
            f"{home['excerpt'][:2500] or '(new file)'}")
    try:
        data = _json_block(caller(sys_prompt, user)) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("repro draft failed: %s", exc)
        return None
    name, code = str(data.get("name") or ""), str(data.get("code") or "")
    if not re.fullmatch(r"test_\w+", name) or f"def {name}" not in code:
        return None
    if len(code.split("\n")) > _MAX_TEST_LINES:
        return None
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(defs) != 1 or defs[0].name != name:
        return None
    return {"name": name, "code": code}


def run_single_test(root, base_cmd: list, test_path: str, test_name: str,
                    timeout: int = _RUN_TIMEOUT) -> dict:
    """Run exactly one test through the repo's own check command (pytest v1).

    Returns:
        dict — {status: failed|passed|error, tail, failures}.
    """
    from openfde.language_packs import get_pack_for_file
    from openfde.verify import parse_failure_locations
    cmd = [*base_cmd, f"{test_path}::{test_name}"]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "tail": f"timed out after {timeout}s", "failures": []}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "tail": str(exc)[:300], "failures": []}
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tail = output.strip()[-1500:]
    if proc.returncode == 0:
        return {"status": "passed", "tail": tail, "failures": []}
    # Failure parsing is a language seam → the pack that owns the test file parses
    # it (raw fallback when no pack claims the file).
    pack = get_pack_for_file(test_path)
    failures = ([loc.as_dict() for loc in pack.parse_failures(output, root)]
                if pack is not None else parse_failure_locations(output, root))
    status = "failed" if failures or "failed" in output else "error"
    return {"status": status, "tail": tail, "failures": failures}


def reproduce_issue(root, *, title: str, body: str, labels: list,
                    caller=None, check_cmd=None, before_write=None) -> dict:
    """The whole Reproduce flow, end to end, with honest verdicts throughout.

    Returns:
        dict — {verdict, summary, missing?, testFile?, testName?, failures?,
                tail?, targets?}. verdicts: not_a_bug | insufficient | no_agent |
                unsupported_runner | draft_failed | reproduced | not_reproduced |
                run_error. ``before_write`` (optional, () -> episodeId) fires
                exactly once, right before the test write — the server uses it
                to bootstrap the WORK EPISODE the repro belongs to, so the
                watcher attributes the edit there and the whole existing loop
                (verification → Show → hatch) anchors on it. Every verdict
                carries ``links``: the issue's code ties for canvas anchoring.
    """
    root = Path(root)
    triage = triage_issue(title, body, labels, caller)
    if triage["verdict"] == "not_a_bug":
        return {"verdict": "not_a_bug", "summary": triage["missing"][0],
                "missing": triage["missing"],
                "links": map_issue_to_files(root, title, body, caller)}
    if triage["verdict"] == "insufficient":
        return {"verdict": "insufficient",
                "summary": "can't reproduce from the issue text",
                "missing": triage["missing"],
                "links": map_issue_to_files(root, title, body, caller)}

    targets = locate_targets(root, triage, body)
    if not targets:
        return {"verdict": "insufficient",
                "summary": "the code the issue names was not found in this repo "
                           "(stale issue, or it never named one)",
                "missing": ["a file/function that exists at HEAD, or the exact "
                            "error text to search for"],
                "links": map_issue_to_files(root, title, body, caller)}
    target = targets[0]
    try:
        tp = root / target["file"]
        target["snippet"] = tp.read_text(encoding="utf-8", errors="replace")[:2500]
    except OSError:
        target["snippet"] = ""

    if caller is None:
        return {"verdict": "no_agent", "targets": targets,
                "links": [t["file"] for t in targets],
                "summary": "triage says reproducible, but no text-capable agent "
                           "is configured to draft the test (Agents → Senior Dev)"}
    if not check_cmd or "pytest" not in " ".join(map(str, check_cmd)):
        # The runner the repo discovered isn't this language's (or there's none).
        # Ask the language pack that owns the target file for its framework: it
        # pins a check config (so "Run checks" runs the test we're about to write)
        # and hands back the command to run it with now — instead of refusing.
        # This is the piece that makes "point OpenFDE at any repo → it works" true.
        from openfde.language_packs import get_pack_for_file
        pack = get_pack_for_file(target["file"])
        if pack is None:
            return {"verdict": "unsupported_runner", "targets": targets,
                    "links": [t["file"] for t in targets],
                    "summary": "no language pack for this file type yet (v1: Python)"}
        pack.ensure_check_config(root)
        check_cmd = list(pack.repro_context()["test_command"])

    home = find_test_home(root, target["file"])
    issue_ctx = (f"--- github issue ---\ntitle: {title}\n"
                 f"failure mode: {triage.get('failureMode') or '?'}\n"
                 f"desired: {triage.get('desired') or '?'}\n\n{(body or '')[:_MAX_BODY]}")
    draft = draft_repro_test(caller, issue_ctx, target, home)
    if draft is None:
        return {"verdict": "draft_failed", "targets": targets,
                "links": [t["file"] for t in targets],
                "summary": "the agent could not produce a single valid test from "
                           "the issue text — reproduce by hand or improve the issue"}

    # Path guard: the ONLY write this module ever makes is appending a test
    # under tests/ — anything else is refused outright.
    home_path = (root / home["path"]).resolve()
    if not str(home_path).startswith(str((root / "tests").resolve()) + "/"):
        return {"verdict": "draft_failed", "targets": targets,
                "summary": f"refused to write outside tests/: {home['path']}"}
    episode_id = ""
    if before_write is not None:
        try:
            episode_id = before_write() or ""
        except Exception as exc:  # noqa: BLE001 — the repro must not die on hooks
            logger.warning("before_write hook failed: %s", exc)
    original = home_path.read_text(encoding="utf-8", errors="replace") \
        if home_path.exists() else None
    new_text = ((original.rstrip("\n") + "\n\n\n") if original
                else "import pytest  # noqa: F401\n\n\n") + draft["code"].rstrip("\n") + "\n"
    home_path.parent.mkdir(parents=True, exist_ok=True)
    home_path.write_text(new_text, encoding="utf-8")

    def _revert():
        if original is None:
            try:
                home_path.unlink()
            except OSError:
                pass
        else:
            home_path.write_text(original, encoding="utf-8")

    run = run_single_test(root, check_cmd, home["path"], draft["name"])
    base = {"targets": targets, "testFile": home["path"], "testName": draft["name"],
            "tail": run["tail"], "episodeId": episode_id,
            "links": [target["file"], home["path"]]}
    if run["status"] == "failed":
        return {**base, "verdict": "reproduced", "failures": run["failures"],
                "summary": f"reproduced — {draft['name']} fails as the issue "
                           f"describes (test kept in {home['path']})"}
    _revert()
    if run["status"] == "passed":
        return {**base, "verdict": "not_reproduced", "testFile": None,
                "links": [target["file"]],
                "summary": "the drafted test PASSES on current code — the issue "
                           "may be stale or already fixed (test reverted)"}
    return {**base, "verdict": "run_error", "testFile": None,
            "summary": "the repro test errored for unrelated reasons (reverted) — "
                       "see the output tail"}
