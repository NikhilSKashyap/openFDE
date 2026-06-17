"""
openfde/focus.py — L2-A: a focused, O(issue) path for large repos (additive + conservative).

Whole-repo assimilation (``architect.analyze_repo``) and the existing verify gate are UNCHANGED. These
helpers are opt-in: a caller working from an issue / failure / touched file asks for a focused
NEIGHBORHOOD (seed files + 1–2 hops of import + function-flow neighbors, from the EXISTING ArchGraph)
and the smallest honest VERIFY set. Nothing here re-parses or replaces the whole tree; it extracts a
small subgraph from graph data that already exists. Honest boundary: the neighborhood is still derived
from the whole-repo ArchGraph for now (true O(issue) *parsing* is future), and scoped verify does NOT
claim test-impact analysis — when it can't prove coverage it falls back and says so.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("openfde.focus")

_DEFAULT_MAX_FILES = 40


def _neighborhood_from_graph(graph: dict, seeds: list, *, hops: int = 1,
                             max_files: int = _DEFAULT_MAX_FILES, primary_path=None) -> dict:
    """Pure neighborhood selection from an ArchGraph dict (no I/O) — unit-testable with a synthetic
    graph. Order: seeds first, then nearest evidence (import neighbors, then flow neighbors, then any
    caller-provided failure-flow files). Deterministically capped at ``max_files``."""
    graph = graph or {}
    file_edges = graph.get("fileEdges") or []
    flows = graph.get("flows") or []
    repo_files = {f.get("path") for f in (graph.get("files") or [])}
    func_by_id = {fn.get("id"): fn for fn in (graph.get("functions") or [])}
    seed_set = set(seeds)

    files, seen = [], set()
    warnings, out_edges, out_funcs = [], [], []

    def add_file(p):
        if p and p not in seen and len(files) < max_files:
            seen.add(p)
            files.append(p)

    # 1. seeds first (honest: warn on unknown seeds, but still include them)
    for s in seeds:
        add_file(s)
        if repo_files and s not in repo_files:
            warnings.append(f"seed not in repo graph: {s}")

    # 2. import (file-edge) neighbors — BFS up to `hops`
    frontier = set(seeds)
    for _ in range(max(0, hops)):
        nxt = set()
        for fe in file_edges:
            a, b = fe.get("fromFile"), fe.get("toFile")
            for src, dst in ((a, b), (b, a)):
                if src in frontier and dst and dst not in seen and len(files) < max_files:
                    add_file(dst)
                    nxt.add(dst)
        frontier = nxt
        if not frontier:
            break

    # 3. direct function-flow neighbors of the SEED files (caller/callee across the flow graph)
    seen_func_ids = set()
    for fl in flows:
        ff, tf = func_by_id.get(fl.get("fromId")), func_by_id.get(fl.get("toId"))
        ffile = ff.get("path") if ff else None
        tfile = tf.get("path") if tf else None
        if ffile in seed_set or tfile in seed_set:
            add_file(ffile)
            add_file(tfile)
            for fn in (ff, tf):
                if fn and fn.get("id") and fn["id"] not in seen_func_ids:
                    seen_func_ids.add(fn["id"])
                    out_funcs.append({"id": fn["id"], "name": fn.get("name"), "path": fn.get("path")})
            out_edges.append({"from": fl.get("fromId"), "to": fl.get("toId"), "type": "flow"})

    # 4. failure-flow primary-path files (caller-provided evidence), if any
    for p in (primary_path or []):
        add_file(p)

    # 5. import edges fully inside the neighborhood (for focused rendering)
    for fe in file_edges:
        if fe.get("fromFile") in seen and fe.get("toFile") in seen:
            out_edges.append({"from": fe["fromFile"], "to": fe["toFile"],
                              "type": fe.get("type", "import")})

    if not file_edges and not flows:
        warnings.append("No ArchGraph edges available — focused neighborhood is the seeds only.")
    return {"ok": True, "mode": "focused", "seeds": list(seeds), "files": files,
            "functions": out_funcs, "edges": out_edges, "warnings": warnings}


def neighborhood(root, seeds, *, hops: int = 1, max_files: int = _DEFAULT_MAX_FILES,
                 primary_path=None, graph: dict = None) -> dict:
    """Focused issue/failure neighborhood (L2-A) — ADDITIVE, opt-in. Returns
    ``{ok, mode:'focused', seeds, files, functions, edges, warnings}``. Unknown seeds or a missing graph
    yield a focused response + an honest warning, never an error.

    ``graph`` may be passed in (e.g. the server's cached ArchGraph) to stay O(issue); when omitted it is
    derived from ``architect.analyze_repo(root)`` for now (the honest "still whole-repo" boundary)."""
    seeds = [s for s in (seeds or []) if isinstance(s, str) and s]
    if graph is None:
        try:
            from openfde import architect
            graph = architect.analyze_repo(Path(root)) if root is not None else {}
        except Exception as exc:  # noqa: BLE001 — the focused path must never crash
            logger.warning("focus: could not build the ArchGraph; returning seeds only: %s", exc)
            graph = {}
    return _neighborhood_from_graph(graph or {}, seeds, hops=hops, max_files=max_files,
                                    primary_path=primary_path)


def _obvious_tests_for(root, touched_files) -> list:
    """Conservative name-match of touched Python files to obvious test files (``tests/test_<stem>.py``
    etc.) that actually exist. Best-effort — NOT test-impact analysis."""
    root = Path(root)
    found = []
    for tf in (touched_files or []):
        p = Path(tf)
        if p.suffix != ".py" or p.name.startswith("test_"):
            continue
        for cand in (f"tests/test_{p.stem}.py", f"test_{p.stem}.py",
                     (p.parent / f"test_{p.stem}.py").as_posix()):
            if (root / cand).is_file() and cand not in found:
                found.append(cand)
    return found


def scoped_verify(root, *, touched_files=None, repro_check=None) -> dict:
    """Choose the smallest HONEST verification set (L2-A). Priority: an explicit repro check → the repo's
    pinned ``.openfde/verify.json`` → obvious tests for the touched files → the existing verify discovery
    (fallback). NEVER overclaims test-impact: when it can't prove coverage it falls back and says so.
    Returns ``{mode:'scoped'|'fallback', checks, reason, warnings}``."""
    from openfde import verify

    if repro_check:
        return {"mode": "scoped", "checks": [repro_check],
                "reason": "ran the repro check for this issue", "warnings": []}

    if (Path(root) / ".openfde" / "verify.json").exists():
        checks = verify.discover_checks(root)
        if checks:
            return {"mode": "scoped", "checks": checks,
                    "reason": "ran the repo's pinned .openfde/verify.json check(s)", "warnings": []}

    if touched_files:
        tests = _obvious_tests_for(root, touched_files)
        if tests:
            return {"mode": "scoped",
                    "checks": [{"id": "scoped-tests", "label": "Scoped tests",
                                "command": ["python", "-m", "pytest", *tests],
                                "cwd": "", "required": True}],
                    "reason": f"ran obvious tests for {len(touched_files)} touched file(s)",
                    "warnings": ["Best-effort test selection; coverage is not proven "
                                 "(no test-impact analysis)."]}

    note = "Scoped verify could not prove coverage; falling back to full/default checks."
    return {"mode": "fallback", "checks": verify.discover_checks(root), "reason": note,
            "warnings": [note]}
