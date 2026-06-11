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
        _h((failure_msg or "").strip()), _h(code or "")))
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


def build_failure_flow(root, *, file, line, func="", test="", output_tail="") -> dict:
    """Derive the deterministic failure flow for one failing check.

    Args:
        root: Path — repo root (in-repo resolution boundary).
        file/line: failing location; func/test: names from the receipt.
        output_tail: str — check output (traceback frames enrich the chain).

    Returns:
        dict — {summary, nodes:[{id,label,file,line}], edges:[{from,to,label,
                confidence}]}; never raises, degrades to a minimal flow.
    """
    line = int(line or 0)
    fail_text = ""
    nodes, edges, seen = [], [], set()

    def add_node(nid, label, nfile=None, nline=None):
        if nid in seen:
            return
        seen.add(nid)
        if len(nodes) < _MAX_NODES:
            n = {"id": nid, "label": label}
            if nfile:
                n["file"] = nfile
            if nline:
                n["line"] = nline
            nodes.append(n)

    def add_edge(frm, to, label, confidence):
        if len(edges) < _MAX_EDGES and frm in seen and to in seen:
            edges.append({"from": frm, "to": to, "label": label, "confidence": confidence})

    try:
        path = resolve_repo_path(root, file)
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (SourceEditError, OSError, SyntaxError, ValueError) as exc:
        logger.warning("failure flow: cannot parse %s: %s", file, exc)
        tid = test or func or "failure"
        add_node(tid, tid, file, line or None)
        return {"summary": f"{tid} fails at {file}:{line}.", "nodes": nodes, "edges": edges}

    lines = src.split("\n")
    if 1 <= line <= len(lines):
        fail_text = lines[line - 1].strip()

    enc = _enclosing_function(tree, line)
    tname = (enc.name if enc else None) or test or func or "failure"
    add_node(tname, tname, file, enc.lineno if enc else (line or None))

    imports = _import_map(tree)
    parsed_cache: dict = {}
    called = []
    if enc is not None:
        for node in ast.walk(enc):
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if not name or name in ("self", tname):
                    continue
                called.append((node.lineno, name, _arg_preview(node)))
    called.sort(key=lambda t: t[0])

    skipped = {"assertTrue", "assertFalse", "assertEqual", "assertIn", "assertRaises",
               "assertIsNone", "assertIsNotNone", "print", "len", "str", "int", "repr"}
    dedup = set()
    for lno, name, args in called:
        base = name.split(".")[-1]
        if base in skipped and lno != line:
            continue
        if name in dedup:
            continue
        dedup.add(name)
        res = _resolve_callee(name, tree, imports, root, parsed_cache) or {}
        add_node(name, name, res.get("file") or (file if res.get("line") else None),
                 res.get("line"))
        on_fail = lno == line
        if on_fail:
            label = f"fails here — {fail_text[:80]}" if fail_text else f"fails at line {line}"
            conf = "high"
        else:
            label = f"calls {name.split('.')[-1]}({args}) — line {lno}" if args \
                else f"calls {name.split('.')[-1]}() — line {lno}"
            conf = "high" if res.get("line") and not res.get("file") else \
                   ("medium" if res.get("line") else "low")
        add_edge(tname, name, label, conf)

    # Traceback frames from the check output — deepest frames chain the path the
    # raise actually took (in-repo frames only, the receipt parser's law).
    frames = [(m.group(1), int(m.group(2)), m.group(3))
              for m in _FRAME_RE.finditer(output_tail or "")]
    prev = None
    for ffile, fline, ffunc in frames[-4:]:
        if "/lib/python" in ffile or "site-packages" in ffile:
            continue
        fid = f"{ffunc}@{fline}"
        if ffunc == tname:
            prev = tname
            continue
        add_node(fid, ffunc, None, fline)
        if prev:
            add_edge(prev, fid, f"raises through {ffunc} (line {fline})", "high")
        prev = fid

    call_names = [n["id"].split(".")[-1] for n in nodes[1:]] or ["nothing in-scope"]
    summary = (f"{tname} calls {', '.join(call_names[:4])} and fails at "
               f"line {line}" + (f": {fail_text[:90]}" if fail_text else "."))
    return {"summary": summary, "nodes": nodes, "edges": edges}


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
