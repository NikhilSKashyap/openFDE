"""
openfde/intent_graph.py — Sketch-First Intent: plain-English boxes → ordered brief.

A user draws *intent boxes* (``box.kind == "intent"``) on the canvas — plain-English
steps like "read the data" → "drop nan rows" → "train a classifier" — and connects
them with arrows to express order / dataflow. This module compiles that selected
sketch into a structured, ordered **Intent Graph Brief** that the Agent Council
implements from, and (after a run) attributes the changed files back to the steps.

Pure + deterministic: no I/O, no canvas mutation, no agent calls. The server feeds
the brief into the Architect stage and writes the attribution back onto the boxes.

Honesty contract:
  - Ordering is derived from arrows ONLY when it is unambiguous (a single linear
    chain). When the sketch branches, cycles, or has no connecting arrows, we do
    NOT invent an order — we fall back to canvas order and say so via ``ambiguous``
    + ``ambiguityReason``.
  - File attribution back to a step is heuristic in v1 (the whole graph shares its
    run's files; a step named in the plan is flagged "named"). We label the
    attribution and its confidence rather than fake per-step precision.
"""

import logging
from collections import defaultdict

logger = logging.getLogger("openfde.intent_graph")

INTENT_KIND = "intent"

_DEFAULT_BOX_PROMPT = "Describe what this module does..."
_MAX_STEPS = 40           # bound a runaway sketch
_MAX_SUMMARY_STEPS = 6    # titles shown in the one-line summary
_MAX_TITLE_LEN = 60


# ─── Predicates ───────────────────────────────────────────────────────────── #

def is_intent_box(box: dict) -> bool:
    """Return whether a canvas box is a plain-English intent step (vs a module)."""
    return isinstance(box, dict) and box.get("kind") == INTENT_KIND


def _step_text(box: dict) -> str:
    """A step's description: its own prompt if meaningful, else its title."""
    p = (box.get("prompt") or "").strip()
    if p and p != _DEFAULT_BOX_PROMPT:
        return p
    return (box.get("title") or "").strip()


def _short(text: str, limit: int = _MAX_TITLE_LEN) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ─── Ordering (deterministic topological sort with honest ambiguity) ──────── #

def _order_intent(ids: list, edges: list, order_index: dict) -> dict:
    """Order intent boxes from their connecting arrows.

    Args:
        ids: list — intent box ids in canvas order.
        edges: list — (fromBox, toBox) pairs among intent boxes.
        order_index: dict — boxId → canvas-order index (tie-break + fallback).

    Returns:
        dict — {order: [ids…], ambiguous: bool, reason: str}.
    """
    adj: dict = defaultdict(list)
    indeg = {i: 0 for i in ids}
    outdeg = {i: 0 for i in ids}
    edge_set: set = set()
    for f, t in edges:
        if f in indeg and t in indeg and f != t and (f, t) not in edge_set:
            edge_set.add((f, t))
            adj[f].append(t)
            indeg[t] += 1
            outdeg[f] += 1

    canvas_order = sorted(ids, key=lambda i: order_index.get(i, 0))

    # No connecting arrows → unordered. One step is trivially ordered.
    if not edge_set:
        if len(ids) <= 1:
            return {"order": canvas_order, "ambiguous": False, "reason": ""}
        return {"order": canvas_order, "ambiguous": True,
                "reason": "no arrows connect the steps; listed in canvas order — sequence not specified"}

    # Kahn's algorithm with a canvas-order tie-break (fully deterministic).
    indeg_left = dict(indeg)
    ready = sorted([i for i in ids if indeg_left[i] == 0], key=lambda i: order_index.get(i, 0))
    out: list = []
    while ready:
        n = ready.pop(0)
        out.append(n)
        for m in adj[n]:
            indeg_left[m] -= 1
            if indeg_left[m] == 0:
                ready.append(m)
        ready.sort(key=lambda i: order_index.get(i, 0))

    if len(out) != len(ids):   # a cycle blocked the sort
        return {"order": canvas_order, "ambiguous": True,
                "reason": "the connected steps form a cycle; using canvas order"}

    roots = [i for i in ids if indeg[i] == 0]
    sinks = [i for i in ids if outdeg[i] == 0]
    linear = (len(edge_set) == len(ids) - 1 and len(roots) == 1 and len(sinks) == 1
              and all(indeg[i] <= 1 for i in ids) and all(outdeg[i] <= 1 for i in ids))
    if linear:
        return {"order": out, "ambiguous": False, "reason": ""}
    return {"order": out, "ambiguous": True,
            "reason": "the sketch branches (parallel or converging steps); order is best-effort from the arrows"}


# ─── Public API ───────────────────────────────────────────────────────────── #

def compile_intent_graph(sel_boxes: list, sel_arrows: list) -> dict:
    """Compile selected intent boxes + arrows into an ordered Intent Graph Brief.

    Args:
        sel_boxes: list — selected canvas boxes (mix of modules + intent steps).
        sel_arrows: list — arrows in scope (connecting the selection).

    Returns:
        dict — {
            present: bool,                # any intent steps in the selection
            steps: [{order, boxId, title, prompt}],   # ordered (1-based)
            edges: [{fromBox, toBox, fromTitle, toTitle, label}],
            ambiguous: bool,
            ambiguityReason: str,         # "" when unambiguous
            summary: str,                 # "read data → clean → train"
            acceptance: [str],            # acceptance-criteria scaffold
            instruction: str,             # implement-the-sketch + link-back contract
        }
    """
    intent_boxes = [b for b in (sel_boxes or []) if is_intent_box(b)][:_MAX_STEPS]
    if not intent_boxes:
        return {"present": False, "steps": [], "edges": [], "ambiguous": False,
                "ambiguityReason": "", "summary": "", "acceptance": [], "instruction": ""}

    ids = [b["id"] for b in intent_boxes if b.get("id")]
    box_by_id = {b["id"]: b for b in intent_boxes if b.get("id")}
    order_index = {bid: i for i, bid in enumerate(ids)}
    intent_id_set = set(ids)

    raw_edges = [
        (a.get("fromBox"), a.get("toBox"))
        for a in (sel_arrows or [])
        if a.get("fromBox") in intent_id_set and a.get("toBox") in intent_id_set
    ]
    ordering = _order_intent(ids, raw_edges, order_index)

    steps = []
    for i, bid in enumerate(ordering["order"], start=1):
        box = box_by_id[bid]
        steps.append({
            "order": i,
            "boxId": bid,
            "title": _short(box.get("title") or "(untitled step)"),
            "prompt": _step_text(box),
        })

    # Edges rendered with titles, de-duplicated, in canvas order for stability.
    seen: set = set()
    edges = []
    for a in (sel_arrows or []):
        f, t = a.get("fromBox"), a.get("toBox")
        if f in intent_id_set and t in intent_id_set and f != t and (f, t) not in seen:
            seen.add((f, t))
            edges.append({
                "fromBox": f, "toBox": t,
                "fromTitle": _short(box_by_id[f].get("title") or f),
                "toTitle": _short(box_by_id[t].get("title") or t),
                "label": (a.get("label") or "").strip(),
            })

    titles = [s["title"] for s in steps]
    summary = " → ".join(titles[:_MAX_SUMMARY_STEPS])
    if len(titles) > _MAX_SUMMARY_STEPS:
        summary += f" → … (+{len(titles) - _MAX_SUMMARY_STEPS})"

    acceptance = [f"Step {s['order']} ({s['title']}): implemented and linked to a file" for s in steps]

    instruction = (
        "Implement this sketch as the smallest correct working version, in order. "
        "Translate each intent step into concrete modules / files / functions / tests, "
        "stay strictly within the editable (dotted) scope, never modify protected (solid) "
        "modules, and after implementing, note which step each created/edited file fulfils "
        "so the work can be linked back to the intent boxes.")

    logger.info("Intent graph compiled: %d step(s), %d edge(s), ambiguous=%s",
                len(steps), len(edges), ordering["ambiguous"])

    return {
        "present": True,
        "steps": steps,
        "edges": edges,
        "ambiguous": ordering["ambiguous"],
        "ambiguityReason": ordering["reason"],
        "summary": summary,
        "acceptance": acceptance,
        "instruction": instruction,
    }


def render_intent_brief(graph: dict) -> str:
    """Render an Intent Graph Brief as a markdown section (empty when absent).

    Args:
        graph: dict — output of :func:`compile_intent_graph`.

    Returns:
        str — markdown, or "" when there are no intent steps.
    """
    if not graph or not graph.get("present") or not graph.get("steps"):
        return ""
    lines = ["## Intent Graph (sketch)", ""]
    lines.append("_Plain-English steps the user drew. Implement them as a pipeline, in order, "
                 "and link the work back to each step._")
    lines.append("")

    if graph.get("ambiguous"):
        lines.append(f"> ⚠ Order is best-effort — {graph.get('ambiguityReason', 'ambiguous sketch')}.")
        lines.append("")

    lines.append("**Ordered steps**")
    for s in graph["steps"]:
        prompt = (s.get("prompt") or "").strip()
        suffix = f" — {prompt}" if prompt and prompt != s.get("title") else ""
        lines.append(f"{s['order']}. **{s['title']}**{suffix}")
    lines.append("")

    edges = graph.get("edges") or []
    if edges:
        lines.append("**Dataflow / order**")
        for e in edges:
            label = f" — `{e['label']}`" if e.get("label") else ""
            lines.append(f"- {e['fromTitle']} → {e['toTitle']}{label}")
        lines.append("")

    acceptance = graph.get("acceptance") or []
    if acceptance:
        lines.append("**Acceptance criteria (scaffold)**")
        for a in acceptance:
            lines.append(f"- [ ] {a}")
        lines.append("")

    instruction = (graph.get("instruction") or "").strip()
    if instruction:
        lines.append(instruction)
        lines.append("")

    return "\n".join(lines)


def attribute_intent_files(intent_boxes: list, changed_files: list, named_text: str = "") -> dict:
    """Attribute a run's changed files back to the selected intent steps (v1 heuristic).

    v1 is deliberately coarse and honest: the whole sketch shares the run's files.
    A step whose title is named in the plan/report is flagged ``"named"`` (higher
    confidence); otherwise the attribution is ``"graph"``. We never fake per-step
    file precision — the label and confidence carry that uncertainty.

    Args:
        intent_boxes: list — the selected intent boxes ({id, title}).
        changed_files: list — file paths the run created/edited.
        named_text: str — architect plan + report text (for the name-match flag).

    Returns:
        dict — {boxId: {files: [str], attribution: "named"|"graph", confidence: float}}.
        Empty when there are no intent boxes or no changed files.
    """
    files = sorted({str(f) for f in (changed_files or []) if f})
    boxes = [b for b in (intent_boxes or []) if is_intent_box(b) and b.get("id")]
    if not files or not boxes:
        return {}
    hay = (named_text or "").lower()
    links: dict = {}
    for b in boxes:
        title = (b.get("title") or "").strip()
        named = bool(title) and len(title) > 2 and title.lower() in hay
        links[b["id"]] = {
            "files": files,
            "attribution": "named" if named else "graph",
            "confidence": 0.6 if named else 0.4,
        }
    return links
