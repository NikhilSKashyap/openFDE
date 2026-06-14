"""
openfde/failure_flow.py — failure fingerprints + the failure FLOW.

A failure receipt says WHERE a check failed; the failure flow says HOW the
failure got there: the failing test, the functions it calls, and the assertion
that broke, as a small labeled graph the canvas can draw as a lens.

Two laws govern this module:
  • Deterministic evidence is the source of truth — AST calls, traceback
    frames, the failing line's own text. The optional LLM pass may ONLY
    rewrite edge labels and the summary into human language (strict JSON);
    any failure falls back to the deterministic labels.
  • The fingerprint pins an artifact to a failure MEANING — same failure,
    same artifact (the LLM runs once); when the location, message, or the
    involved code changes, the fingerprint changes and artifacts regenerate.
"""

import ast
import hashlib
import json
import logging
import re

from openfde.source_edit import SourceEditError, resolve_repo_path

logger = logging.getLogger("openfde.failure_flow")

_MAX_NODES = 8
_MAX_EDGES = 10
_MAX_IMPORT_PARSES = 4


def _h(s: str, n: int = 12) -> str:
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()[:n]


_TIMING_RE = re.compile(r"\b(?:in\s+)?\d+(?:\.\d+)?s\b")
_COUNTS_RE = re.compile(r"\b\d+ (?:passed|failed|skipped|deselected|warnings?|errors?)\b")


def normalize_failure_msg(msg: str) -> str:
    """Stable failure identity from volatile check output.

    Check output carries timings ("in 15.53s") and counts that change every
    run — hashing them raw made the SAME failure regenerate its artifacts on
    every Run checks. Keep only the error-bearing lines, stripped of the
    volatile bits, so two runs of one failure hash equal.
    """
    keep = []
    for ln in (msg or "").splitlines():
        t = ln.strip()
        if not t:
            continue
        if (t.startswith(("E ", "E\t", "FAILED", 'File "'))
                or ": in " in t
                or re.search(r"\b\w+(?:Error|Exception)\b", t)):
            keep.append(_COUNTS_RE.sub("", _TIMING_RE.sub("", t)).strip())
    if keep:
        return "\n".join(keep[:20])
    return _COUNTS_RE.sub("", _TIMING_RE.sub("", (msg or "")))[:400]


def failure_fingerprint(*, episode_id="", check_id="", file="", line="",
                        func="", test="", failure_msg="", code="") -> str:
    """Stable identity of one failure meaning.

    Args:
        episode_id/check_id/file/line/func/test: location identity.
        failure_msg: str — check summary / output tail (hashed).
        code: str — the involved function source (hashed).

    Returns:
        str — 16-hex fingerprint; changes when the failure meaning changes.
    """
    raw = "|".join(str(x) for x in (
        episode_id, check_id, file, line, func, test,
        _h(normalize_failure_msg(failure_msg)), _h(code or "")))
    return _h(raw, 16)


def _call_name(node) -> str:
    """Dotted name of a call target ('acquire_watch_lock', 'Path.exists', …)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):                 # chained call: f(...).g(...)
        parts.append(_call_name(node.func) or "…")
    return ".".join(reversed(parts))


def _arg_preview(call: ast.Call, cap: int = 3) -> str:
    out = []
    for a in call.args[:cap]:
        if isinstance(a, ast.Name):
            out.append(a.id)
        elif isinstance(a, ast.Constant):
            out.append(repr(a.value))
        else:
            out.append("…")
    for kw in call.keywords:
        if len(out) >= cap:
            break
        out.append(f"{kw.arg}=…" if kw.arg else "**…")
    return ", ".join(out)


def _enclosing_function(tree, line):
    """Innermost FunctionDef containing `line` (None when not inside one)."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= line <= end:
                if best is None or node.lineno > best.lineno:   # innermost wins
                    best = node
    return best


def _first_doc_line(node) -> str:
    doc = ast.get_docstring(node) or ""
    return doc.strip().splitlines()[0].strip() if doc.strip() else ""


def _import_map(tree) -> dict:
    """name → module path ('openfde/instance_lock.py') for from-imports."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            rel = node.module.replace(".", "/") + ".py"
            for alias in node.names:
                out[alias.asname or alias.name] = rel
    return out


def _resolve_callee(name, tree, imports, root, parsed_cache):
    """Find an in-repo def for a (possibly dotted) call name.

    Returns:
        dict | None — {file, line, doc} of the definition.
    """
    base = name.split(".")[-1]
    for node in ast.walk(tree):                       # same file first
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == base:
            return {"file": None, "line": node.lineno, "doc": _first_doc_line(node)}
    head = name.split(".")[0]
    rel = imports.get(head) or imports.get(base)
    if not rel:
        return None
    if rel not in parsed_cache:
        if len(parsed_cache) >= _MAX_IMPORT_PARSES:
            return None
        try:
            p = resolve_repo_path(root, rel)
            parsed_cache[rel] = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SourceEditError, OSError, SyntaxError, ValueError):
            parsed_cache[rel] = None
    sub = parsed_cache.get(rel)
    if sub is None:
        return None
    for node in ast.walk(sub):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == base:
            return {"file": rel, "line": node.lineno, "doc": _first_doc_line(node)}
    return None


_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_PYTEST_FRAME_RE = re.compile(r"^([^\s:][^:\n]*\.py):(\d+): in (\S+)", re.M)


def _frame_rel(root, fpath: str):
    """Repo-relative path for a traceback frame, or None (outside / missing)."""
    root_s = str(root)
    p = fpath
    if p.startswith("/"):
        if not p.startswith(root_s + "/"):
            return None
        p = p[len(root_s) + 1:]
    if "/lib/python" in fpath or "site-packages" in fpath:
        return None
    try:
        resolve_repo_path(root, p)
    except (SourceEditError, ValueError, OSError):
        return None
    return p


def chain_files(root, output_tail: str, fallback_file: str = "", cap: int = 4) -> list:
    """The in-repo files on the failure's traceback chain, outermost first.

    A failing TEST is usually a two-legged contract — the test (expectation)
    and the product code (behavior) — and the right fix may live on either
    leg. The repair run's editable scope is exactly these files, never just
    the one the hatch opened on, and never the whole repo.

    Returns:
        list[str] — unique repo-relative files (≤ cap); fallback_file is
        always included (last) when given.
    """
    raw = [(m.start(), m.group(1)) for m in _FRAME_RE.finditer(output_tail or "")]
    raw += [(m.start(), m.group(1)) for m in _PYTEST_FRAME_RE.finditer(output_tail or "")]
    out = []
    for _, fpath in sorted(raw):
        rel = _frame_rel(root, fpath)
        if rel and rel not in out:
            out.append(rel)
    if fallback_file and fallback_file not in out:
        out.append(fallback_file)
    return out[:cap]


def build_failure_flow(root, *, file, line, func="", test="", output_tail="") -> dict:
    """Derive the deterministic failure flow for one failing check.

    Drawn the way you trace an error in a terminal, left to right: the CALLER
    CHAIN from the traceback first — defA() → defB() → ✕ the failing function —
    then what the failing line itself touches (its calls, resolved in-repo).
    The failing node carries ``fail: True`` + ``detail`` (the failing line's
    text) so the lens can mark the ✕ where the error actually lives. With no
    traceback in the output, falls back to AST-only: the enclosing function and
    its calls (the failing line's calls flagged as the failure edges).

    Args:
        root: Path — repo root (in-repo resolution boundary).
        file/line: failing location; func/test: names from the receipt.
        output_tail: str — check output (traceback frames build the chain).

    Returns:
        dict — {summary, nodes:[{id,label,file,line,fail?,detail?}],
                edges:[{from,to,label,confidence}]}; never raises.
    """
    line = int(line or 0)
    fail_text = ""
    nodes, edges, seen = [], [], set()

    def add_node(nid, label, nfile=None, nline=None, fail=False, detail=""):
        if nid in seen:
            return
        seen.add(nid)
        if len(nodes) < _MAX_NODES:
            n = {"id": nid, "label": label}
            if nfile:
                n["file"] = nfile
            if nline:
                n["line"] = nline
            if fail:
                n["fail"] = True
            if detail:
                n["detail"] = detail
            nodes.append(n)

    def add_edge(frm, to, label, confidence):
        if len(edges) < _MAX_EDGES and frm in seen and to in seen:
            edges.append({"from": frm, "to": to, "label": label, "confidence": confidence})

    tree, enc, tname = None, None, (test or func or "failure")
    try:
        path = resolve_repo_path(root, file)
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
        lines = src.split("\n")
        if 1 <= line <= len(lines):
            fail_text = lines[line - 1].strip()
        enc = _enclosing_function(tree, line)
        tname = (enc.name if enc else None) or tname
    except (SourceEditError, OSError, SyntaxError, ValueError) as exc:
        logger.warning("failure flow: cannot parse %s: %s", file, exc)

    # ── 1) The caller chain — in-repo traceback frames, outermost first. ──
    raw = [(m.start(), m.group(1), int(m.group(2)), m.group(3))
           for m in _FRAME_RE.finditer(output_tail or "")]
    raw += [(m.start(), m.group(1), int(m.group(2)), m.group(3))   # pytest --tb=short
            for m in _PYTEST_FRAME_RE.finditer(output_tail or "")]
    chain = []
    for _, ffile, fline, ffunc in sorted(raw):
        rel = _frame_rel(root, ffile)
        if rel is None:
            continue
        if chain and chain[-1]["func"] == ffunc and chain[-1]["file"] == rel:
            continue
        chain.append({"file": rel, "line": fline, "func": ffunc})
    chain = chain[-5:]
    if not chain or chain[-1]["func"] != tname:
        chain.append({"file": file, "line": line, "func": tname})

    def _nid(fr, i):
        base = fr["func"]
        return base if all(c["func"] != base for c in chain[:i]) else f"{base}@{fr['line']}"

    ids = [_nid(fr, i) for i, fr in enumerate(chain)]
    for i, fr in enumerate(chain):
        last = i == len(chain) - 1
        add_node(ids[i], fr["func"], fr["file"], fr["line"],
                 fail=last, detail=(fail_text if last else ""))
    for i in range(len(chain) - 1):
        add_edge(ids[i], ids[i + 1],
                 f"calls {chain[i + 1]['func']}() — line {chain[i]['line']}", "high")

    fail_id = ids[-1]

    # ── 2) What the failing line touches (AST on the failing function). With a
    #       real chain, only the fail-line calls; AST-only mode (chain of one)
    #       keeps the surrounding calls for context. ──
    if tree is not None and enc is not None:
        imports = _import_map(tree)
        parsed_cache: dict = {}
        called = []
        for node in ast.walk(enc):
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if not name or name in ("self", tname):
                    continue
                called.append((node.lineno, name, _arg_preview(node)))
        called.sort(key=lambda t: t[0])
        skipped = {"assertTrue", "assertFalse", "assertEqual", "assertIn", "assertRaises",
                   "assertIsNone", "assertIsNotNone", "print", "len", "str", "int", "repr"}
        context_too = len(chain) == 1
        dedup = set()
        for lno, name, args in called:
            base = name.split(".")[-1]
            on_fail = lno == line
            if not on_fail and (not context_too or base in skipped):
                continue
            if name in dedup or name == fail_id:
                continue
            dedup.add(name)
            res = _resolve_callee(name, tree, imports, root, parsed_cache) or {}
            add_node(name, name, res.get("file") or (file if res.get("line") else None),
                     res.get("line"))
            if on_fail:
                label = f"fails here — {fail_text[:80]}" if fail_text else f"fails at line {line}"
                conf = "high"
            else:
                label = f"calls {base}({args}) — line {lno}" if args \
                    else f"calls {base}() — line {lno}"
                conf = "high" if res.get("line") and not res.get("file") else \
                       ("medium" if res.get("line") else "low")
            add_edge(fail_id, name, label, conf)

    chain_names = " → ".join(fr["func"] for fr in chain)
    summary = (f"{chain_names} — fails at line {line}"
               + (f": {fail_text[:90]}" if fail_text else "."))
    primary_path, primary_edges = _distill_primary_path(chain, ids, edges)
    return {"summary": summary, "nodes": nodes, "edges": edges,
            "primaryPath": primary_path, "primaryEdges": primary_edges}


def _short_edge_label(raw: str, to_func: str) -> str:
    """A ≤3-word causal phrase for a primary edge ('calls create')."""
    if raw:
        head = raw.split(" — ")[0].split("(")[0].strip()
        if head:
            return " ".join(head.split()[:3])
    return f"calls {to_func}"


def _distill_primary_path(chain, ids, edges):
    """The causal SENTENCE behind the failure — function to function.

    Failure flow is a path, not a graph: collapse the traceback chain to the
    DISTINCT in-repo functions in first-occurrence order (this drops a test's
    assertion re-entry, so an assertion failure reads 'test → product_fn' with
    the product function as the terminus). Roles: first = source, last =
    failure, middle = step. One short edge per causal hop.

    Returns:
        (list[dict], list[dict]) — primaryPath nodes {id,file,function,line,role}
        and primaryEdges {from,to,label}.
    """
    seen, order = {}, []
    for i, fr in enumerate(chain):
        key = (fr.get("file"), fr["func"])
        if key in seen:
            continue
        seen[key] = ids[i]
        order.append({"id": ids[i], "file": fr.get("file"), "function": fr["func"],
                      "line": fr["line"]})
    n = len(order)
    for j, node in enumerate(order):
        node["role"] = ("failure" if j == n - 1 else ("source" if j == 0 else "step"))
    label_by_pair = {(e["from"], e["to"]): e.get("label", "") for e in edges}
    pedges = [{"from": a["id"], "to": b["id"],
               "label": _short_edge_label(label_by_pair.get((a["id"], b["id"]), ""),
                                          b["function"])}
              for a, b in zip(order, order[1:])]
    return order, pedges


def humanize_flow(flow: dict, caller) -> tuple:
    """Optionally rewrite edge labels + summary via a text role — labels ONLY.

    The graph structure (nodes, edge endpoints, confidence) is deterministic
    truth and never changes here. Strict JSON in/out; ANY failure returns the
    deterministic flow untouched.

    Args:
        flow: dict — deterministic flow.
        caller: callable(system, user) | None — text role (Verifier/Architect).

    Returns:
        (dict, bool) — (possibly-humanized flow, whether the LLM pass landed).
    """
    if not caller or not flow.get("edges"):
        return flow, False
    sys_prompt = (
        "You rewrite failure-flow labels for developers. Given JSON "
        '{"summary", "edges":[{"from","to","label","confidence"}]}, return ONLY '
        "JSON of the same shape: the SAME edges in the SAME order with the same "
        '"from"/"to"/"confidence", each "label" rewritten as a short human phrase '
        "(≤ 8 words, e.g. 'creates the lock file'), and \"summary\" rewritten as "
        "one plain sentence about how the failure happens. No other keys, no prose.")
    payload = {"summary": flow.get("summary", ""),
               "edges": [{k: e.get(k) for k in ("from", "to", "label", "confidence")}
                         for e in flow["edges"]]}
    try:
        out = caller(sys_prompt, json.dumps(payload, ensure_ascii=False))
        m = re.search(r"\{.*\}", out or "", re.S)
        data = json.loads(m.group(0)) if m else {}
        new_edges = data.get("edges")
        if (not isinstance(new_edges, list) or len(new_edges) != len(flow["edges"]) or
                any(not isinstance(e, dict) or e.get("from") != o["from"] or
                    e.get("to") != o["to"] for e, o in zip(new_edges, flow["edges"]))):
            return flow, False
        merged = dict(flow)
        merged["edges"] = [{**o, "label": (str(e.get("label") or o["label"]))[:90]}
                           for e, o in zip(new_edges, flow["edges"])]
        if isinstance(data.get("summary"), str) and data["summary"].strip():
            merged["summary"] = data["summary"].strip()[:240]
        return merged, True
    except Exception as exc:  # noqa: BLE001 — any failure → deterministic truth
        logger.warning("flow humanize failed: %s", exc)
        return flow, False
