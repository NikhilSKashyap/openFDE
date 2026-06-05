#!/usr/bin/env python3
"""
semantic_graph_probe.py — OpenFDE Semantic Graph Adapter Layer (Step 37a) probe.

Point it at a repo; it runs verified OSS providers and emits ONE normalized
``.openfde/semantic_graph.json`` matching the OpenFDE graph contract. OpenFDE does
not build a semantic analyzer — it defines the contract and lets providers fill it.

Providers used here (all verified, permissive licenses):
  - stdlib ``ast``         — structure / imports / cross-file tethers (Python fallback)
  - simple boundary check  — declared "A must not import B" over the import edges
  - ``code2flow`` (opt)    — candidate call arrows (not ground truth)
  - ``detect-secrets`` (opt) — secret/risk evidence
Missing optional tools degrade gracefully (the node is simply absent).

Product rule: **provider output is never truth — it is evidence.** Every node/edge
carries provenance{tool, version, command, confidence, source} so a wrong analyzer
is traceable and swappable, never load-bearing. Deterministic providers emit high
confidence; inferred providers (Step 37b) emit lower and stay provisional until a
verified run or a human promotes them.

Multi-language note: ``ast`` is the *Python* structure provider. JS/TS/Rust/etc.
use ``tree-sitter`` as the primary structure provider (same contract, same
provenance) — not wired here yet.

Usage:
  python scripts/semantic_graph_probe.py [REPO] [--out PATH] [--forbid A:B ...]
"""
import argparse
import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _prov(tool, version, command, confidence, source):
    return {"tool": tool, "version": version, "command": command,
            "confidence": confidence, "source": source}


def _pyver():
    return "python-" + ".".join(map(str, sys.version_info[:3]))


def _tool_version(exe, args=("--version",)):
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().split("\n")[0][:40] or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def ast_provider(repo: Path, files):
    """Structure / imports / cross-file tethers from stdlib ast (Python)."""
    functions, imports, defs, refs = [], [], {}, {}
    pv = _pyver()
    for f in files:
        rel = str(f.relative_to(repo))
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append({
                    "path": rel, "name": n.name, "line": n.lineno,
                    "args": [a.arg for a in n.args.args],
                    "provenance": _prov("ast", pv, "ast.FunctionDef", 0.99, f"{rel}:{n.lineno}")})
                defs[n.name] = rel
            elif isinstance(n, ast.ImportFrom) and n.module:
                imports.append({
                    "from": rel, "module": n.module, "names": [a.name for a in n.names],
                    "provenance": _prov("ast", pv, "ast.ImportFrom", 0.99, f"{rel}:{n.lineno}")})
            if isinstance(n, ast.Name):
                refs.setdefault(n.id, set()).add(rel)
            elif isinstance(n, ast.Attribute):
                refs.setdefault(n.attr, set()).add(rel)
    tethers = []
    for sym, dfile in sorted(defs.items()):
        others = sorted(refs.get(sym, set()) - {dfile})
        if others:
            tethers.append({
                "identifier": sym, "defined_in": dfile, "referenced_in": others,
                "span": [dfile] + others, "kind": "function-symbol",
                "provenance": _prov("ast", pv, "name-xref", 0.95, dfile)})
    return functions, imports, tethers


def module_edges(imports):
    edges = []
    for i in imports:
        a = i["from"].split("/")[0].replace(".py", "")
        b = (i["module"] or "").split(".")[0]
        edges.append((a, b))
    return edges


def boundary_checks(edges, forbid):
    """Declared 'A must not import B' rules enforced over the import edges."""
    out = []
    edgeset = set(edges)
    for rule in forbid:
        if ":" not in rule:
            continue
        x, y = (s.strip() for s in rule.split(":", 1))
        hit = (x, y) in edgeset
        out.append({
            "rule": f"{x} must NOT import {y}", "violated": hit,
            "evidence": f"{x} -> {y}" if hit else None,
            "provenance": _prov("boundary-checker", "1", "import-edge check", 0.99, "ast-imports")})
    return out


def code2flow_provider(files):
    exe = shutil.which("code2flow")
    if not exe:
        return []
    out = "/tmp/_c2f_probe.json"
    try:
        subprocess.run([exe, "-q", "-o", out, *map(str, files)],
                       capture_output=True, text=True, timeout=120)
        data = json.loads(Path(out).read_text())
    except Exception:  # noqa: BLE001
        return []
    ver = _tool_version(exe)
    g = data.get("graph", {})
    nodes = g.get("nodes", {})
    calls = []
    for e in g.get("edges", []):
        s = nodes.get(e.get("source"), {}).get("name", e.get("source"))
        t = nodes.get(e.get("target"), {}).get("name", e.get("target"))
        calls.append({"from": s, "to": t,
                      "provenance": _prov("code2flow", ver, "code2flow", 0.7, "candidate")})
    return calls


def detect_secrets_provider(repo: Path):
    exe = shutil.which("detect-secrets")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "scan", str(repo)], capture_output=True, text=True, timeout=120)
        res = json.loads(r.stdout).get("results", {})
    except Exception:  # noqa: BLE001
        return None
    n = sum(len(v) for v in res.values())
    return {"check": "hardcoded-secrets", "findings": n,
            "status": "clean" if n == 0 else "flagged",
            "provenance": _prov("detect-secrets", _tool_version(exe), "detect-secrets scan", 0.9, "repo")}


def main():
    ap = argparse.ArgumentParser(description="OpenFDE semantic graph adapter probe.")
    ap.add_argument("repo", nargs="?", default=".", help="repo to scan (default: cwd)")
    ap.add_argument("--out", default=None, help="output path (default: <repo>/.openfde/semantic_graph.json)")
    ap.add_argument("--forbid", action="append", default=[], metavar="A:B",
                    help="declared boundary: module A must not import module B (repeatable)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    files = sorted(p for p in repo.rglob("*.py")
                   if ".openfde" not in p.parts and "venv" not in str(p) and "site-packages" not in str(p))

    g = {"contract_version": 1, "repo": str(repo),
         "files": [{"path": str(f.relative_to(repo)),
                    "provenance": _prov("ast", _pyver(), "rglob *.py", 1.0, str(f.relative_to(repo)))}
                   for f in files],
         "functions": [], "imports": [], "calls": [], "tethers": [],
         "boundary_checks": [], "risks": []}

    g["functions"], g["imports"], g["tethers"] = ast_provider(repo, files)
    g["boundary_checks"] = boundary_checks(module_edges(g["imports"]), args.forbid)
    g["calls"] = code2flow_provider(files)
    ds = detect_secrets_provider(repo)
    if ds:
        g["risks"].append(ds)

    out = Path(args.out) if args.out else repo / ".openfde" / "semantic_graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(g, indent=2))

    print(f"files={len(g['files'])} functions={len(g['functions'])} imports={len(g['imports'])} "
          f"calls={len(g['calls'])} tethers={len(g['tethers'])} "
          f"boundary={len(g['boundary_checks'])} risks={len(g['risks'])}")
    for t in g["tethers"]:
        print(f"  tether '{t['identifier']}': {t['defined_in']} -> {t['referenced_in']}")
    for b in g["boundary_checks"]:
        print(f"  boundary '{b['rule']}': {'VIOLATED ' + b['evidence'] if b['violated'] else 'clean'}")
    for r in g["risks"]:
        print(f"  risk {r['check']}: {r['status']} ({r['findings']})")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
