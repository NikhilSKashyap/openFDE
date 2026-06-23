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
import re
from collections import defaultdict

logger = logging.getLogger("openfde.intent_graph")

INTENT_KIND = "intent"

# Safe, repo-local workspace for intent-only sketches that have no real file
# target yet. A pure intent graph runs here instead of being rejected — the agent
# may create/edit files ONLY under this prefix (enforced by the runners). Visible
# and committable for the demo (chosen over .openfde/ deliberately).
GENERATED_WORKSPACE = "openfde_work/"

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


def resolve_run_scope(editable: list, protected: list, intent_graph: dict):
    """Decide the editable/protected scope for a council run (Sketch-First Intent).

    Keeps the permission boundary intact for normal architecture work and only
    opens a *generated* workspace when there is genuinely nothing else to edit:

    - editable files present (architecture or mixed intent+dotted) → unchanged;
      the intent brief still rides along but scope is NOT widened.
    - no editable files + an intent graph → a safe generated workspace
      (``GENERATED_WORKSPACE``); existing selected solid files stay protected.
    - no editable files + no intent graph → ``None`` (caller keeps the 400:
      a pure architecture selection with nothing to edit is still rejected).

    Args:
        editable: list — editable (dotted) linked file paths from the selection.
        protected: list — protected (solid) linked file paths from the selection.
        intent_graph: dict — output of :func:`compile_intent_graph`.

    Returns:
        (editable, protected, generated) tuple, or None when there's nothing to run.
    """
    if editable:
        return list(editable), list(protected), False
    if intent_graph and intent_graph.get("present"):
        return [GENERATED_WORKSPACE], list(protected), True
    return None


def merge_step_files(steps: list, intent_links: dict) -> list:
    """Attach each intent step's run files onto the step, keyed by ``boxId``.

    ``intent_links`` (from :func:`attribute_intent_files`) maps boxId → {files,…}; this
    folds those files onto the episode's ``intentSource.steps`` so Story can show, per
    step, which files it produced. Preserves every existing step field (boxId/title);
    a step with no link gets an empty ``files`` list. Pure — returns a new list.

    Args:
        steps: list — intentSource steps ({boxId, title}).
        intent_links: dict — boxId → {files, attribution, confidence}.

    Returns:
        list — steps with a ``files`` list added to each.
    """
    links = intent_links or {}
    out = []
    for s in (steps or []):
        files = (links.get(s.get("boxId")) or {}).get("files") or []
        out.append({**s, "files": list(files)})
    return out


# Generic, content-free words that must not force a per-step file match on their own.
_ATTR_STOP = {"the", "a", "an", "of", "to", "and", "or", "for", "with", "from", "into",
              "this", "that", "your", "our", "all", "new", "get", "set", "run"}


def _attr_words(text: str) -> set:
    """Distinctive lowercase words (≥3 chars, non-stop) in a title or filename stem."""
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) >= 3 and w not in _ATTR_STOP}


def _stem_words(path: str) -> set:
    """Distinctive words in a file's basename stem (``a/b/ingest.py`` → {ingest})."""
    base = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return _attr_words(stem)


def _files_for_step(title: str, files: list) -> list:
    """Files whose basename stem shares a distinctive word with the step title — prefix-tolerant
    so ``log resolution`` ↔ ``logging.py`` and ``ingest customer messages`` ↔ ``ingest.py``.
    Returns [] when nothing matches, so the caller falls back to the honest coarse set."""
    tw = _attr_words(title)
    if not tw:
        return []
    out = []
    for f in files:
        sw = _stem_words(f)
        if any(a == b or a.startswith(b) or b.startswith(a) for a in tw for b in sw):
            out.append(f)
    return out


def attribute_intent_files(intent_boxes: list, changed_files: list, named_text: str = "") -> dict:
    """Attribute a run's changed files back to the selected intent steps.

    Per-step first: a file whose name echoes a step's title (``ingest customer messages`` →
    ``ingest.py``) is attributed to THAT step alone — so role-named generated files (e.g. the
    support-inbox demo backend) map one-to-one and each box shows its OWN file. When a step has
    no name match, it falls back to the honest coarse v1 (the whole sketch shares the run's
    files). A step whose title also appears in the plan/report is flagged ``"named"`` (higher
    confidence). Generic — keyed on filename↔title word overlap, not any specific demo.

    Args:
        intent_boxes: list — the selected intent boxes ({id, title}).
        changed_files: list — file paths the run created/edited.
        named_text: str — architect plan + report text (for the name-match flag).

    Returns:
        dict — {boxId: {files: [str], attribution: "named"|"matched"|"graph", confidence: float}}.
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
        specific = _files_for_step(title, files)        # per-step name match (e.g. ingest.py)
        if specific:
            # A clean per-step file match is always "matched" — the unambiguous signal that this
            # step OWNS this file (so it can transform into an architecture module). Being named in
            # the report only raises confidence; it must NOT relabel a real match as coarse "named".
            links[b["id"]] = {
                "files": sorted(specific),
                "attribution": "matched",
                "confidence": 0.85 if named else 0.75,
            }
        else:
            links[b["id"]] = {
                "files": files,
                "attribution": "named" if named else "graph",
                "confidence": 0.6 if named else 0.4,
            }
    return links


def module_title_from_file(path: str) -> str:
    """A module-ish title from a generated file path: ``a/b/ingest.py`` → ``ingest/``."""
    base = str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = stem.strip("_") or stem                      # __init__ → init; never empty
    return f"{stem}/" if stem else (base or "module/")


def architecturize_intent_box(box: dict, link: dict, episode_id: str = "", run_id: str = "") -> dict:
    """Transform a built intent box into an architecture module box IN PLACE — *intent is the
    scaffolding, architecture is the artifact* — but only when the run produced a clear, single,
    per-step file (``attribution == "matched"`` with exactly one file). The intent's history is not
    lost: ``originIntent`` records the original box id / title / prompt / episode / run / files so
    Story, OpenPM, and a curious FDE can always trace the module back to the sketch that produced it.

    The box keeps its id and position (so every Story/OpenPM/episode link by boxId still resolves)
    and its ``implementationFiles`` (so it drills in and stays Story-highlightable), but drops its
    ``kind: "intent"`` and ``runState`` so it renders and behaves as architecture, editable like any
    module. Generic — the module title is derived from the file name, not any specific demo.

    Args:
        box: dict — the built intent box (mutated in place).
        link: dict — its attribution link ({files, attribution, confidence}).
        episode_id: str — the owning episode id (for provenance).
        run_id: str — the council run id (for provenance).

    Returns:
        dict — the mutated (now architecture) box, or None when it is not eligible (then the caller
        leaves it a built intent box).
    """
    files = [f for f in (link.get("files") or []) if f]
    if link.get("attribution") != "matched" or len(files) != 1:
        return None
    box["originIntent"] = {
        "boxId": box.get("id"),
        "title": box.get("title"),
        "prompt": box.get("prompt"),
        "episodeId": episode_id or "",
        "runId": run_id or "",
        "files": list(files),
    }
    box["title"] = module_title_from_file(files[0])
    box["linkedFiles"] = list(files)
    box["implementationFiles"] = list(files)
    box["implementationMeta"] = {"runId": run_id, "attribution": link.get("attribution"),
                                 "confidence": link.get("confidence")}
    box.pop("kind", None)            # intent scaffolding → architecture artifact
    box.pop("runState", None)        # no longer an intent lifecycle card
    return box
