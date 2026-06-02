"""
openfde/explain.py — deterministic "Explain this" for a canvas selection (Step 26).

Given selected module boxes, the ArchGraph (modules / files / functions / flows),
and box-spec stories, produce a grounded markdown explanation: what the selected
modules are, how they relate via real function-level dataflow (Step 23), which
functions are the hubs, and what intent the boxes carry.

This is deterministic provenance, not an LLM guess — it always works, needs no
key, and reads straight from the read model. (An LLM can later turn this grounded
context into prose; the structure here is the grounding.)
"""

_MAX_MODULES = 12
_MAX_REL = 14
_MAX_SAMPLES = 3
_MAX_HUBS = 8
_MAX_FILES_PER_MOD = 8


def _mod_name(module_id: str) -> str:
    return (module_id or "").replace("module:", "") or "(unknown)"


def _short(name: str) -> str:
    return (name or "").split(".")[-1]


def explain_selection(boxes_by_id: dict, box_ids: list, graph: dict, box_specs: dict) -> dict:
    """Build a deterministic explanation of the selected boxes.

    Args:
        boxes_by_id: dict — canvas boxes keyed by id.
        box_ids: list — selected box ids.
        graph: dict — ArchGraph (modules, files, functions, flows, edges).
        box_specs: dict — box-specs map keyed by boxId.

    Returns:
        dict — {markdown, summary, moduleCount, flowCount}.
    """
    sel_boxes = [boxes_by_id[b] for b in (box_ids or []) if b in boxes_by_id]
    flows = graph.get("flows", []) or []
    functions = graph.get("functions", []) or []
    fn_by_id = {fn["id"]: fn for fn in functions}

    if not sel_boxes:
        return {"markdown": "_Select one or more boxes on the canvas, then ask to explain them._",
                "summary": "Nothing selected.", "moduleCount": 0, "flowCount": 0}

    sel_mod_ids = {b.get("moduleId") for b in sel_boxes if b.get("moduleId")}
    sel_files = set()
    for b in sel_boxes:
        for f in (b.get("linkedFiles") or []):
            sel_files.add(f)

    # ── Relationships from real function flows ──────────────────────────────
    cross, internal, inbound, outbound = {}, {}, {}, {}
    for fw in flows:
        fm, tm = fw.get("fromModuleId"), fw.get("toModuleId")
        fin, tin = fm in sel_mod_ids, tm in sel_mod_ids
        if fin and tin:
            if fm == tm:
                internal[fm] = internal.get(fm, 0) + 1
            else:
                e = cross.setdefault((fm, tm), {"count": 0, "samples": []})
                e["count"] += 1
                if len(e["samples"]) < _MAX_SAMPLES and fw.get("label"):
                    e["samples"].append(fw["label"])
        elif fin and not tin:
            outbound[tm] = outbound.get(tm, 0) + 1
        elif tin and not fin:
            inbound[fm] = inbound.get(fm, 0) + 1

    # ── Hub functions (most-connected within the selection) ─────────────────
    deg = {}
    for fw in flows:
        if fw.get("fromFile") in sel_files:
            deg[fw["fromFunctionId"]] = deg.get(fw["fromFunctionId"], 0) + 1
        if fw.get("toFile") in sel_files:
            deg[fw["toFunctionId"]] = deg.get(fw["toFunctionId"], 0) + 1
    hubs = sorted(deg.items(), key=lambda kv: -kv[1])[:_MAX_HUBS]

    total_flows = sum(internal.values()) + sum(e["count"] for e in cross.values())

    # ── Compose markdown ────────────────────────────────────────────────────
    lines = ["## Explanation", ""]
    n = len(sel_boxes)
    lines.append(f"_{n} module{'s' if n != 1 else ''} in scope · grounded in the "
                 f"function-level dataflow read model._")
    lines.append("")

    lines.append("### Modules in scope")
    for b in sel_boxes[:_MAX_MODULES]:
        title = b.get("title") or _mod_name(b.get("moduleId", ""))
        files = sorted(b.get("linkedFiles") or [])
        spec = (box_specs or {}).get(b.get("id")) or {}
        intent = (spec.get("currentIntent") or b.get("prompt") or "").strip()
        intent = "" if intent.startswith("Describe what this module") else intent
        lines.append(f"- **{title}** — {len(files)} file(s)"
                     + (f"; intent: {intent}" if intent else ""))
        for f in files[:_MAX_FILES_PER_MOD]:
            lines.append(f"    - `{f}`")
        if len(files) > _MAX_FILES_PER_MOD:
            lines.append(f"    - …and {len(files) - _MAX_FILES_PER_MOD} more")
    lines.append("")

    if cross or internal:
        lines.append("### How they relate (dataflow)")
        for (fm, tm), e in sorted(cross.items(), key=lambda kv: -kv[1]["count"])[:_MAX_REL]:
            ex = f" — e.g. {', '.join(e['samples'])}" if e["samples"] else ""
            lines.append(f"- **{_mod_name(fm)} → {_mod_name(tm)}**: "
                         f"{e['count']} function flow(s){ex}")
        for fm, c in sorted(internal.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{_mod_name(fm)}** (internal): {c} function flow(s) between its own files")
        lines.append("")
    else:
        lines.append("### How they relate (dataflow)")
        lines.append("- No resolved function flows among the selected modules "
                     "(they may connect only via imports, or the calls are dynamic).")
        lines.append("")

    if inbound or outbound:
        lines.append("### Connections outside the selection")
        for m, c in sorted(inbound.items(), key=lambda kv: -kv[1])[:_MAX_REL]:
            lines.append(f"- ← **{_mod_name(m)}** calls into the selection ({c} flow(s))")
        for m, c in sorted(outbound.items(), key=lambda kv: -kv[1])[:_MAX_REL]:
            lines.append(f"- → selection calls into **{_mod_name(m)}** ({c} flow(s))")
        lines.append("")

    if hubs:
        lines.append("### Key functions (most connected)")
        for fid, d in hubs:
            fn = fn_by_id.get(fid)
            if not fn:
                continue
            purpose = (fn.get("purpose") or "").strip()
            loc = f"`{_short(fn['name'])}()` in `{fn['path']}`"
            lines.append(f"- {loc} — {d} connection(s)" + (f"; {purpose}" if purpose else ""))
        lines.append("")

    summary = (f"{n} module(s), {total_flows} internal/cross function flow(s), "
               f"{len(hubs)} hub function(s).")
    return {"markdown": "\n".join(lines).rstrip() + "\n",
            "summary": summary, "moduleCount": n, "flowCount": total_flows}
