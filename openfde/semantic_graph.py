"""
openfde/semantic_graph.py — Semantic Graph Adapter Layer (Step 37a, Slice 1).

OpenFDE does not build a semantic analyzer. It defines a durable graph **contract**
and lets verified OSS / stdlib **providers** fill it, normalized into one
``.openfde/semantic_graph.json``. The product owns the canvas, scope decisions,
verifier, replay, and glow; the parsing/graphing/scanning is pluggable evidence.

**Product rule: provider output is never truth — it is evidence.** Every node, edge,
tether, and risk carries ``provenance`` (tool, version, command, source, confidence)
so a wrong analyzer is traceable and swappable, never load-bearing.

Providers in this slice (all stdlib / verified, degrade gracefully):
  - ``ast_provider``     — Python files → file/function/class nodes, import + call
    candidate edges (stdlib ``ast``; the *Python* structure provider — other
    languages use tree-sitter later, same contract).
  - ``tether_provider``  — repeated identifier-like / env-var / route string literals
    across files (.py + web), cross-language (the "this id is in N places" map).
  - ``risk_provider``    — deterministic secret-name / hardcoded-secret heuristics.
  - ``code2flow_provider``    — optional candidate call graph (if installed).
  - ``detect_secrets_provider`` — optional secret scan (if installed).

Verifier foundation: ``tethers_partially_touched(graph, changed_files)`` warns when
a change touches only some files of a tethered concept.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_SKIP_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".openfde",
              "venv", ".venv", "site-packages", "coverage", ".pytest_cache", ".mypy_cache"}
_PY_EXT = {".py"}
_WEB_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# Identifier-like string literals worth tethering (provider ids, event names, etc.).
_RE_KEBAB = re.compile(r"^[a-z][a-z0-9]*([-_][a-z0-9]+)+$")        # codex-local, senior_dev
_RE_ENV = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")            # ANTHROPIC_API_KEY
_RE_ROUTE = re.compile(r"^/[a-z0-9][a-z0-9/_-]*$")                # /api/semantic-graph
# Quoted string literal in web sources (single/double/backtick, no interpolation).
_RE_WEBSTR = re.compile(r"""(['"`])([^'"`\n]{2,80})\1""")
# Secret-ish names (evidence, low confidence unless a value is hardcoded).
_RE_SECRET = re.compile(r"(api[_-]?key|secret|token|password|passwd|credential|private[_-]?key|auth[_-]?token)", re.I)

# Common id-like literals that are noise, not concepts.
_TETHER_STOP = {"utf-8", "utf8", "application/json", "text/plain", "text/html",
                "content-type", "new-password", "/", "//", "use-strict"}


# ─── provenance ──────────────────────────────────────────────────────────── #

def _prov(tool, version, command, source, confidence, errors=None):
    p = {"tool": tool, "version": version, "command": command,
         "source": source, "confidence": confidence}
    if errors:
        p["errors"] = errors
    return p


def _py_version():
    return "python-" + ".".join(map(str, sys.version_info[:3]))


def _tool_version(exe, args=("--version",)):
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=20)
        return ((out.stdout or out.stderr).strip().splitlines() or ["unknown"])[0][:40]
    except Exception:  # noqa: BLE001
        return "unknown"


# ─── file discovery (prunes node_modules etc. — fast on big repos) ─────────── #

def _iter_files(repo_root: Path, exts):
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1] in exts:
                yield Path(dirpath) / fn


def _rel(repo_root: Path, p: Path):
    try:
        return str(p.relative_to(repo_root))
    except ValueError:
        return str(p)


# ─── provider: Python ast (structure + imports + call candidates) ──────────── #

def ast_provider(repo_root: Path) -> dict:
    t0 = time.time()
    pv = _py_version()
    nodes, edges, warnings = [], [], []
    for f in _iter_files(repo_root, _PY_EXT):
        rel = _rel(repo_root, f)
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError) as exc:
            warnings.append(f"{rel}: {exc.__class__.__name__}")
            continue
        nodes.append({"id": f"file::{rel}", "kind": "file", "path": rel,
                      "provenance": _prov("ast", pv, "os.walk", rel, 1.0)})
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(n, ast.ClassDef) else "function"
                node = {"id": f"{kind}::{rel}::{n.name}#{n.lineno}", "kind": kind,
                        "path": rel, "name": n.name, "line": n.lineno,
                        "endLine": getattr(n, "end_lineno", n.lineno),
                        "provenance": _prov("ast", pv, f"ast.{type(n).__name__}", f"{rel}:{n.lineno}", 0.99)}
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node["args"] = [a.arg for a in n.args.args]
                    node["returns"] = _unparse(n.returns)
                    # call candidates inside this function
                    for c in ast.walk(n):
                        if isinstance(c, ast.Call):
                            called = _call_name(c.func)
                            if called:
                                edges.append({
                                    "from": node["id"], "to": f"symbol::{called}",
                                    "kind": "calls-candidate",
                                    "provenance": _prov("ast", pv, "ast.Call", f"{rel}:{getattr(c,'lineno',n.lineno)}", 0.6)})
                nodes.append(node)
            elif isinstance(n, ast.ImportFrom) and n.module:
                edges.append({
                    "from": f"file::{rel}", "to": f"module::{n.module}", "kind": "import",
                    "names": [a.name for a in n.names],
                    "provenance": _prov("ast", pv, "ast.ImportFrom", f"{rel}:{n.lineno}", 0.99)})
            elif isinstance(n, ast.Import):
                for a in n.names:
                    edges.append({
                        "from": f"file::{rel}", "to": f"module::{a.name}", "kind": "import",
                        "provenance": _prov("ast", pv, "ast.Import", f"{rel}:{n.lineno}", 0.99)})
    return _result("ast", pv, True, nodes=nodes, edges=edges, warnings=warnings, t0=t0)


def _unparse(node):
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return None


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ─── provider: repeated-identifier tethers (cross-language) ────────────────── #

def _classify_literal(s: str):
    if s in _TETHER_STOP or len(s) < 3 or len(s) > 80:
        return None
    if _RE_KEBAB.match(s):
        return "identifier"
    if _RE_ENV.match(s):
        return "env-var"
    if _RE_ROUTE.match(s):
        return "route"
    return None


def tether_provider(repo_root: Path) -> dict:
    t0 = time.time()
    pv = _py_version()
    occ = {}      # literal -> list[(rel, line)]
    kinds = {}    # literal -> classification
    warnings = []

    def add(lit, rel, line):
        k = _classify_literal(lit)
        if not k:
            return
        occ.setdefault(lit, []).append((rel, line))
        kinds[lit] = k

    # Python: string constants via ast (precise line numbers)
    for f in _iter_files(repo_root, _PY_EXT):
        rel = _rel(repo_root, f)
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                add(n.value, rel, getattr(n, "lineno", 0))
    # Web: quoted string literals via regex
    for f in _iter_files(repo_root, _WEB_EXT):
        rel = _rel(repo_root, f)
        try:
            text = f.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for m in _RE_WEBSTR.finditer(text):
            add(m.group(2), rel, text.count("\n", 0, m.start()) + 1)

    tethers = []
    for lit, places in occ.items():
        files = sorted({p for p, _ in places})
        if len(files) < 2:           # a concept lives in many files; <2 isn't a tether
            continue
        tethers.append({
            "identifier": lit, "kind": kinds[lit],
            "files": files, "fileCount": len(files), "count": len(places),
            "occurrences": [{"path": p, "line": ln} for p, ln in places[:50]],
            "reason": f"{kinds[lit]}-like string literal in {len(files)} files",
            "provenance": _prov("tether-scan", pv, "ast.Constant + regex", "repo", 0.9)})
    tethers.sort(key=lambda t: (-t["fileCount"], -t["count"], t["identifier"]))
    return _result("tether-scan", pv, True, tethers=tethers, warnings=warnings, t0=t0)


# ─── provider: deterministic risk / secret-name scan ───────────────────────── #

def risk_provider(repo_root: Path) -> dict:
    t0 = time.time()
    pv = _py_version()
    risks, warnings = [], []
    for f in _iter_files(repo_root, _PY_EXT):
        rel = _rel(repo_root, f)
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            # hardcoded secret: NAME matching secret pattern assigned a string literal
            if isinstance(n, ast.Assign) and isinstance(getattr(n, "value", None), ast.Constant) \
                    and isinstance(n.value.value, str) and len(n.value.value) >= 8:
                for tgt in n.targets:
                    name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                    if name and _RE_SECRET.search(name):
                        risks.append({
                            "check": "hardcoded-secret", "severity": "high", "path": rel,
                            "line": n.lineno, "message": f"`{name}` assigned a string literal",
                            "provenance": _prov("risk-scan", pv, "ast.Assign", f"{rel}:{n.lineno}", 0.8)})
            # secret-named string reference (low-confidence evidence)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and _RE_ENV.match(n.value) and _RE_SECRET.search(n.value):
                risks.append({
                    "check": "secret-name-reference", "severity": "info", "path": rel,
                    "line": getattr(n, "lineno", 0), "message": f"references secret-named value '{n.value}'",
                    "provenance": _prov("risk-scan", pv, "ast.Constant", f"{rel}:{getattr(n,'lineno',0)}", 0.4)})
    # de-dup identical (path,line,message)
    seen, uniq = set(), []
    for r in risks:
        key = (r["path"], r["line"], r["message"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return _result("risk-scan", pv, True, risks=uniq, warnings=warnings, t0=t0)


# ─── optional providers (graceful when not installed) ──────────────────────── #

def code2flow_provider(repo_root: Path) -> dict:
    t0 = time.time()
    exe = shutil.which("code2flow")
    if not exe:
        return _result("code2flow", "n/a", False, warnings=["code2flow not installed (optional)"], t0=t0)
    files = [str(f) for f in _iter_files(repo_root, _PY_EXT)]
    out = "/tmp/_openfde_c2f.json"
    try:
        subprocess.run([exe, "-q", "-o", out, *files], capture_output=True, text=True, timeout=180)
        data = json.loads(Path(out).read_text())
    except Exception as exc:  # noqa: BLE001
        return _result("code2flow", _tool_version(exe), False, warnings=[str(exc)[:120]], t0=t0)
    ver = _tool_version(exe)
    g = data.get("graph", {})
    gnodes = g.get("nodes", {})
    edges = []
    for e in g.get("edges", []):
        s = gnodes.get(e.get("source"), {}).get("name", e.get("source"))
        tt = gnodes.get(e.get("target"), {}).get("name", e.get("target"))
        edges.append({"from": f"c2f::{s}", "to": f"c2f::{tt}", "kind": "calls-candidate",
                      "provenance": _prov("code2flow", ver, "code2flow", "candidate", 0.7)})
    return _result("code2flow", ver, True, edges=edges, t0=t0)


def detect_secrets_provider(repo_root: Path) -> dict:
    t0 = time.time()
    exe = shutil.which("detect-secrets")
    if not exe:
        return _result("detect-secrets", "n/a", False, warnings=["detect-secrets not installed (optional)"], t0=t0)
    try:
        r = subprocess.run([exe, "scan", str(repo_root)], capture_output=True, text=True, timeout=180)
        res = json.loads(r.stdout).get("results", {})
    except Exception as exc:  # noqa: BLE001
        return _result("detect-secrets", _tool_version(exe), False, warnings=[str(exc)[:120]], t0=t0)
    ver = _tool_version(exe)
    risks = []
    for fpath, hits in res.items():
        for h in hits:
            risks.append({"check": "secret", "severity": "high", "path": fpath,
                          "line": h.get("line_number"), "message": h.get("type", "secret"),
                          "provenance": _prov("detect-secrets", ver, "detect-secrets scan",
                                              f"{fpath}:{h.get('line_number')}", 0.9)})
    return _result("detect-secrets", ver, True, risks=risks, t0=t0)


# ─── orchestration ─────────────────────────────────────────────────────────── #

PROVIDERS = [ast_provider, tether_provider, risk_provider,
             code2flow_provider, detect_secrets_provider]


def _result(provider, version, ok, *, nodes=None, edges=None, tethers=None,
            risks=None, warnings=None, t0=None):
    nodes, edges = nodes or [], edges or []
    tethers, risks, warnings = tethers or [], risks or [], warnings or []
    return {
        "nodes": nodes, "edges": edges, "tethers": tethers, "risks": risks,
        "run": {"provider": provider, "version": version, "ok": ok,
                "counts": {"nodes": len(nodes), "edges": len(edges),
                           "tethers": len(tethers), "risks": len(risks)},
                "warnings": warnings,
                "durationMs": int((time.time() - (t0 or time.time())) * 1000)},
    }


def _commit_sha(repo_root: Path):
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root),
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def build_graph(repo_root) -> dict:
    """Run all providers over ``repo_root`` and assemble the normalized graph."""
    repo_root = Path(repo_root)
    graph = {"schemaVersion": SCHEMA_VERSION, "repoRoot": str(repo_root),
             "generatedAt": datetime.now(timezone.utc).isoformat(),
             "commitSha": _commit_sha(repo_root),
             "nodes": [], "edges": [], "tethers": [], "risks": [], "providerRuns": []}
    for provider in PROVIDERS:
        try:
            res = provider(repo_root)
        except Exception as exc:  # noqa: BLE001 — a bad provider must not kill the graph
            graph["providerRuns"].append({"provider": getattr(provider, "__name__", "?"),
                                          "version": "n/a", "ok": False,
                                          "counts": {}, "warnings": [f"crashed: {exc}"], "durationMs": 0})
            continue
        graph["nodes"].extend(res["nodes"])
        graph["edges"].extend(res["edges"])
        graph["tethers"].extend(res["tethers"])
        graph["risks"].extend(res["risks"])
        graph["providerRuns"].append(res["run"])
    return graph


def graph_path(repo_root) -> Path:
    return Path(repo_root) / ".openfde" / "semantic_graph.json"


def write_graph(repo_root, graph=None) -> Path:
    graph = graph if graph is not None else build_graph(repo_root)
    out = graph_path(repo_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, indent=2))
    return out


def load_graph(repo_root):
    p = graph_path(repo_root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def graph_summary(graph) -> dict:
    """Compact summary for the UI: counts, top tethers, provider warnings."""
    if not graph:
        return {"exists": False}
    warns = []
    for run in graph.get("providerRuns", []):
        for w in run.get("warnings", []):
            warns.append({"provider": run.get("provider"), "warning": w})
    top = [{"identifier": t["identifier"], "kind": t.get("kind"),
            "fileCount": t.get("fileCount"), "count": t.get("count"),
            "files": t.get("files", [])[:8]} for t in graph.get("tethers", [])[:12]]
    return {
        "exists": True,
        "generatedAt": graph.get("generatedAt"),
        "commitSha": graph.get("commitSha"),
        "counts": {"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", [])),
                   "tethers": len(graph.get("tethers", [])), "risks": len(graph.get("risks", []))},
        "topTethers": top,
        "providerRuns": [{"provider": r.get("provider"), "ok": r.get("ok"),
                          "counts": r.get("counts"), "durationMs": r.get("durationMs")}
                         for r in graph.get("providerRuns", [])],
        "providerWarnings": warns,
    }


# ─── verifier foundation ────────────────────────────────────────────────────── #

def _norm(p):
    s = (p or "").strip().strip('"')
    return s[2:] if s.startswith("./") else s


def tethers_partially_touched(graph, changed_files) -> list:
    """Warn when a change touches only SOME files of a tethered concept.

    Returns a list of {identifier, touched, total, touchedFiles, untouchedFiles,
    message} — the seed of the Verifier's architecture-drift check.
    """
    changed = {_norm(c) for c in (changed_files or [])}
    if not changed:
        return []
    out = []
    for t in (graph or {}).get("tethers", []):
        files = set(t.get("files", []))
        touched = files & changed
        if touched and touched != files:
            untouched = sorted(files - touched)
            out.append({
                "identifier": t["identifier"], "kind": t.get("kind"),
                "touched": len(touched), "total": len(files),
                "touchedFiles": sorted(touched), "untouchedFiles": untouched,
                "message": (f"'{t['identifier']}' appears in {len(files)} places; this change "
                            f"touched only {len(touched)} of them.")})
    out.sort(key=lambda w: (-w["total"], w["identifier"]))
    return out


# ─── CLI (handy for smoke-testing on any repo) ─────────────────────────────── #

if __name__ == "__main__":  # pragma: no cover
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    g = build_graph(root)
    p = write_graph(root, g)
    s = graph_summary(g)
    print(f"nodes={s['counts']['nodes']} edges={s['counts']['edges']} "
          f"tethers={s['counts']['tethers']} risks={s['counts']['risks']}")
    for t in s["topTethers"][:10]:
        print(f"  tether '{t['identifier']}' ({t['kind']}) in {t['fileCount']} files")
    for w in s["providerWarnings"][:5]:
        print(f"  warn [{w['provider']}]: {w['warning']}")
    print(f"wrote {p}")
