"""
openfde/story.py — deterministic "Story mode" semantic summaries (Step 26 Batch 5).

Turns a selected scope (module / file / function / multi-module) into a short,
plain-English narrative: what it is, what happens first → next → last, and the
raw grounding (nodeIds / flowIds) so the canvas can highlight the story path.

Deterministic heuristics only — no LLM. Functions are classified into semantic
phases by name keywords, grouped, ordered into 3–7 steps, and ranked by graph
position (fan-in / fan-out). An LLM can later polish the prose; the structure is
the durable part.
"""

# Phase classification — checked in priority order (first keyword match wins).
_CLASSIFY = (
    ("verification", ("verify", "validate", "assert", "_test", "sanity")),
    ("persistence",  ("save", "persist", "write", "dump", "store", "upsert", "append_", "commit_")),
    ("discovery",    ("discover", "collect", "walk", "scan", "gather", "detect_module",
                      "list_", "_files", "load_state", "iter_")),
    ("relationship", ("detect_edge", "_edges", "edge", "dependency", "resolve", "import",
                      "_call", "flow", "rollup", "relate", "merge_")),
    ("parsing",      ("parse", "extract", "read", "tokenize", "_json", "load")),
    ("build",        ("build", "generate", "render", "compile", "make_", "create",
                      "layout", "analyze", "_arrows", "_to_node", "canvas")),
    ("execution",    ("run", "execute", "agent", "workflow", "dispatch", "handle",
                      "serve", "start", "_loop", "reconcile")),
)

# Canonical first → last ordering for the steps.
_PHASE_ORDER = ("discovery", "parsing", "relationship", "build",
                "execution", "persistence", "verification")

_PHASE_META = {
    "discovery":    ("Discover files", "Finds the source files and groups them by module."),
    "parsing":      ("Extract structure", "Reads the files and pulls out functions, classes, and imports."),
    "relationship": ("Detect relationships", "Resolves how functions call each other and how modules depend."),
    "build":        ("Build the result", "Assembles everything into the output structure."),
    "execution":    ("Execute", "Runs the work and drives the main loop."),
    "persistence":  ("Persist", "Saves the result so it can be reloaded later."),
    "verification": ("Verify", "Checks and validates the outcome."),
}

_MAX_STEPS = 7
_MAX_STEP_NODES = 6
_MAX_STEP_FLOWS = 12
_MAX_IO = 6


def _short(name: str) -> str:
    return (name or "").split(".")[-1]


def _classify(name: str):
    n = (name or "").lower()
    for phase, kws in _CLASSIFY:
        if any(k in n for k in kws):
            return phase
    return None


def _humanize_arg(name: str) -> str:
    n = (name or "").lower()
    if n in ("root", "path", "repo", "repo_path", "repo_root"):
        return "repo path"
    return (name or "").replace("_", " ").strip()


def _first_sentence(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    for sep in (". ", "\n"):
        if sep in t:
            t = t.split(sep)[0]
    return t.rstrip(".").strip()


def _resolve_scope(boxes_by_id, box_ids, selected_entity, functions):
    """Determine which functions are in scope + a human title base.

    Returns:
        dict — {kind, fns, title, summaryHint, scopeFnIds, files}.
    """
    ent = selected_entity or {}
    kind = ent.get("kind")
    by_path = {}
    for fn in functions:
        by_path.setdefault(fn["path"], []).append(fn)

    # Function selection.
    if kind == "function" and ent.get("path") and ent.get("name"):
        fid = f"function:{ent['path']}:{ent['name']}"
        target = next((f for f in functions if f["id"] == fid), None)
        fns = by_path.get(ent["path"], [])  # whole file as light context
        short = _short(ent["name"])
        return {"kind": "function", "fns": fns, "target": target,
                "title": f"What {short}() does",
                "summaryHint": (target or {}).get("purpose", ""),
                "files": [ent["path"]]}

    # File selection.
    if kind == "file" and ent.get("path"):
        fns = by_path.get(ent["path"], [])
        return {"kind": "file", "fns": fns, "target": None,
                "title": f"How {ent['path']} works", "summaryHint": "",
                "files": [ent["path"]]}

    # Module / box selection (incl. multi-select).
    module_ids, titles = [], []
    if kind == "module" and ent.get("id"):
        mid = ent["id"].replace("box:", "")
        module_ids.append(mid)
        titles.append(mid.replace("module:", ""))
    for bid in (box_ids or []):
        box = boxes_by_id.get(bid)
        if box and box.get("moduleId"):
            module_ids.append(box["moduleId"])
            titles.append(box.get("title") or box["moduleId"].replace("module:", ""))
    module_ids = list(dict.fromkeys(module_ids))
    fns = [f for f in functions if f.get("moduleId") in module_ids]
    files = sorted({f["path"] for f in fns})
    names = list(dict.fromkeys(titles))
    if len(names) > 1:
        title = f"How {', '.join(names[:3])} work together"
    elif names:
        title = f"How the {names[0]} module works"
    else:
        title = "Architecture story"
    return {"kind": "module", "fns": fns, "target": None,
            "title": title, "summaryHint": "", "files": files}


def build_story(boxes_by_id: dict, box_ids: list, selected_entity: dict, graph: dict) -> dict:
    """Build a deterministic story for the selected scope.

    Args:
        boxes_by_id: dict — canvas boxes keyed by id.
        box_ids: list — selected box ids.
        selected_entity: dict — {kind, id, path, name} or None.
        graph: dict — ArchGraph (functions, flows, …).

    Returns:
        dict — {ok, title, summary, steps[], inputs[], outputs[], confidence}.
    """
    functions = graph.get("functions", []) or []
    flows = graph.get("flows", []) or []

    scope = _resolve_scope(boxes_by_id, box_ids, selected_entity, functions)
    fns = scope["fns"]
    if not fns:
        return {"ok": True, "title": scope["title"],
                "summary": "No parsed functions in this scope to summarize.",
                "steps": [], "inputs": [], "outputs": [], "confidence": "heuristic"}

    scope_fn_ids = {f["id"] for f in fns}
    fn_by_id = {f["id"]: f for f in fns}

    # Fan-in / fan-out within the whole graph (degree informs ranking).
    fan_out, fan_in = {}, {}
    for fw in flows:
        a, b = fw.get("fromFunctionId"), fw.get("toFunctionId")
        if a in scope_fn_ids:
            fan_out[a] = fan_out.get(a, 0) + 1
        if b in scope_fn_ids:
            fan_in[b] = fan_in.get(b, 0) + 1

    def degree(fid):
        return fan_out.get(fid, 0) + fan_in.get(fid, 0)

    # ── Group functions by phase ────────────────────────────────────────────
    phases = {}
    for f in fns:
        ph = _classify(f["name"])
        if ph:
            phases.setdefault(ph, []).append(f)

    # ── Build steps in canonical order ──────────────────────────────────────
    steps = []
    order = 0
    for ph in _PHASE_ORDER:
        members = phases.get(ph)
        if not members:
            continue
        order += 1
        if order > _MAX_STEPS:
            break
        members = sorted(members, key=lambda f: -degree(f["id"]))
        label, blurb = _PHASE_META[ph]
        rep_purpose = next((m.get("purpose") for m in members if m.get("purpose")), "")
        description = _first_sentence(rep_purpose) or blurb
        node_ids = [f"box:{m['id']}" for m in members[:_MAX_STEP_NODES]]
        member_ids = {m["id"] for m in members}
        flow_ids = [fw["id"] for fw in flows
                    if (fw.get("fromFunctionId") in member_ids or fw.get("toFunctionId") in member_ids)
                    and fw.get("fromFunctionId") in scope_fn_ids
                    and fw.get("toFunctionId") in scope_fn_ids][:_MAX_STEP_FLOWS]
        steps.append({
            "id": f"story:{ph}",
            "order": order,
            "label": label,
            "description": description,
            "nodeIds": node_ids,
            "filePaths": sorted({m["path"] for m in members}),
            "flowIds": flow_ids,
        })

    # ── Inputs / outputs from the entry function ────────────────────────────
    if scope["kind"] == "function" and scope.get("target"):
        entry = scope["target"]
    else:
        # Highest net fan-out, prefer public names.
        entry = max(fns, key=lambda f: (fan_out.get(f["id"], 0) - fan_in.get(f["id"], 0),
                                        not f["name"].startswith("_"), degree(f["id"])))
    inputs = []
    for a in (entry.get("args") or [])[:_MAX_IO]:
        h = _humanize_arg(a.get("name", ""))
        if h and h not in inputs:
            inputs.append(h)
    outputs = []
    # Entry return first, then the highest-degree functions; skip None and
    # private type names (leading underscore) to keep the list readable.
    for f in [entry] + sorted(fns, key=lambda f: -degree(f["id"])):
        r = (f.get("returns") or "").strip()
        if r and r not in ("None", "") and not r.startswith("_") and r not in outputs:
            outputs.append(r)
        if len(outputs) >= 4:
            break

    # ── Title / summary ─────────────────────────────────────────────────────
    title = scope["title"]
    summary = _first_sentence(scope.get("summaryHint", ""))
    if not summary:
        if steps:
            summary = f"Goes from “{steps[0]['label']}” to “{steps[-1]['label']}”."
        else:
            summary = "A small set of related functions."

    return {
        "ok": True,
        "title": title,
        "summary": summary,
        "steps": steps,
        "inputs": inputs,
        "outputs": outputs,
        "confidence": "heuristic",
    }
