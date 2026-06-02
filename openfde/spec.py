"""
openfde/spec.py — OpenArchitect Write: canvas selection → implementation spec.

Compiles the current canvas selection (boxes + arrows), their linked files,
ArchGraph function contracts, task context, and permission boundaries into a
structured markdown document ready to paste into a coding agent.

This is a compiler/preview layer only.  It does not execute code, edit files,
or call an agent.
"""

import logging
from typing import Optional

from openfde.box_spec import render_prior_story

logger = logging.getLogger("openfde.spec")

# ─── Public API ───────────────────────────────────────────────────────────── #

def compile_spec(
    canvas_state: dict,
    tasks: list,
    project: dict,
    graph: dict,
    selected_box_ids: list,
    selected_arrow_ids: list,
    user_prompt: str,
    box_specs: Optional[dict] = None,
) -> dict:
    """Compile a canvas selection into a structured implementation spec.

    Reads the persisted canvas state, task list, project metadata, and live
    ArchGraph to produce a markdown document and a structured context object.

    Selection rules:
    - selectedBoxIds non-empty  → those boxes, their linked files/functions,
      and arrows that touch those boxes.
    - selectedArrowIds non-empty (boxes empty) → those arrows, plus source
      and target boxes with their linked files/functions.
    - Nothing selected → repo-level spec: all boxes, all tasks, all warnings.

    Args:
        canvas_state:      dict  — persisted canvas state {boxes, arrows}.
        tasks:             list  — persisted OpenPM task list.
        project:           dict  — persisted project metadata.
        graph:             dict  — live ArchGraph from analyze_repo().
        selected_box_ids:  list  — box IDs selected in the canvas.
        selected_arrow_ids: list — arrow IDs selected in the canvas.
        user_prompt:       str   — optional freeform instruction from the user.

    Returns:
        dict — {markdown: str, context: dict} where context contains:
            boxes, arrows, files, functions, tasks, warnings.

    Side effects:
        Logs a spec-generation summary at INFO level.
    """
    # ── Lookup tables ──────────────────────────────────────────────────────── #
    all_boxes  = canvas_state.get("boxes",  [])
    all_arrows = canvas_state.get("arrows", [])

    box_by_id   = {b["id"]: b for b in all_boxes}
    arrow_by_id = {a["id"]: a for a in all_arrows}

    # ArchGraph lookups
    files_by_path: dict = {f["path"]: f for f in graph.get("files", [])}
    fns_by_file:   dict = {}
    for fn in graph.get("functions", []):
        fns_by_file.setdefault(fn["path"], []).append(fn)

    # ── Resolve selection ──────────────────────────────────────────────────── #
    sel_boxes:  list = []
    sel_arrows: list = []

    if selected_box_ids:
        sel_boxes  = [box_by_id[bid] for bid in selected_box_ids if bid in box_by_id]
        box_id_set = {b["id"] for b in sel_boxes}
        # All arrows that touch any selected box
        sel_arrows = [
            a for a in all_arrows
            if a.get("fromBox") in box_id_set or a.get("toBox") in box_id_set
        ]
        # Also add any explicitly selected arrows not yet included
        sel_arrow_ids_set = {a["id"] for a in sel_arrows}
        for aid in selected_arrow_ids:
            if aid in arrow_by_id and aid not in sel_arrow_ids_set:
                sel_arrows.append(arrow_by_id[aid])

    elif selected_arrow_ids:
        sel_arrows   = [arrow_by_id[aid] for aid in selected_arrow_ids if aid in arrow_by_id]
        # Source and target boxes for those arrows
        implied_ids: set = set()
        for a in sel_arrows:
            if a.get("fromBox"): implied_ids.add(a["fromBox"])
            if a.get("toBox"):   implied_ids.add(a["toBox"])
        sel_boxes = [box_by_id[bid] for bid in implied_ids if bid in box_by_id]

    else:
        # Repo-level: include everything
        sel_boxes  = list(all_boxes)
        sel_arrows = list(all_arrows)

    # ── Collect linked files and functions ─────────────────────────────────── #
    linked_paths: set = set()
    for box in sel_boxes:
        for fp in box.get("linkedFiles", []):
            linked_paths.add(fp)

    sel_files: list = sorted(
        [files_by_path[fp] for fp in linked_paths if fp in files_by_path],
        key=lambda f: f["path"],
    )

    sel_functions: list = []
    fn_warnings:   list = []
    for fp in sorted(linked_paths):
        for fn in fns_by_file.get(fp, []):
            sel_functions.append(fn)
            fn_warnings.extend(fn.get("warnings", []))

    # ── Collect tasks ──────────────────────────────────────────────────────── #
    if selected_box_ids or selected_arrow_ids:
        sel_box_id_set = {b["id"] for b in sel_boxes}
        sel_tasks = [
            t for t in tasks
            if any(bid in sel_box_id_set for bid in t.get("linkedBoxIds", []))
        ]
    else:
        sel_tasks = list(tasks)

    # ── Collect warnings ───────────────────────────────────────────────────── #
    arch_warnings = graph.get("warnings", [])
    all_warnings  = fn_warnings + arch_warnings

    # ── Log summary ───────────────────────────────────────────────────────────
    logger.info(
        "Spec compiled: %d box(es), %d arrow(s), %d file(s), %d function(s), %d warning(s)",
        len(sel_boxes), len(sel_arrows), len(sel_files), len(sel_functions), len(all_warnings),
    )

    # ── Build outputs ──────────────────────────────────────────────────────── #
    context = {
        "boxes":     sel_boxes,
        "arrows":    sel_arrows,
        "files":     sel_files,
        "functions": sel_functions,
        "tasks":     sel_tasks,
        "warnings":  all_warnings,
    }

    # Bounded prior-box-story section, derived from earlier Execute runs.
    prior_story = render_prior_story(box_specs or {}, [b["id"] for b in sel_boxes])

    markdown = _build_markdown(
        sel_boxes, sel_arrows, sel_files, sel_functions,
        sel_tasks, project, user_prompt, all_warnings,
        box_by_id,
        is_global=(not selected_box_ids and not selected_arrow_ids),
        prior_story=prior_story,
    )

    return {"markdown": markdown, "context": context}


# ─── Markdown builder ─────────────────────────────────────────────────────── #

def _build_markdown(
    boxes: list,
    arrows: list,
    files: list,
    functions: list,
    tasks: list,
    project: dict,
    user_prompt: str,
    warnings: list,
    box_by_id: dict,
    is_global: bool,
    prior_story: str = "",
) -> str:
    """Assemble the full implementation spec markdown document.

    Args:
        boxes:      list — selected canvas box dicts.
        arrows:     list — selected/connected canvas arrow dicts.
        files:      list — ArchGraph file dicts for linked file paths.
        functions:  list — ArchGraph function dicts for linked files.
        tasks:      list — OpenPM task dicts relevant to the selection.
        project:    dict — project metadata {name, description, entries}.
        user_prompt: str — optional freeform instruction from the user.
        warnings:   list — ArchGraph and function-level warnings.
        box_by_id:  dict — all canvas boxes keyed by ID (for arrow lookup).
        is_global:  bool — True when no explicit selection (repo-level spec).
        prior_story: str — pre-rendered bounded "Prior Box Story" section.

    Returns:
        str — complete markdown document.
    """
    lines: list = []
    a = lines.append   # shorthand

    # ── Header ────────────────────────────────────────────────────────────── #
    a("# Implementation Spec")
    a("")
    scope = "repository-level" if is_global else f"{len(boxes)} box(es) selected"
    a(f"> Auto-generated · {scope} · read-only preview")
    a("")

    # ── Problem Statement / Why ────────────────────────────────────────────── #
    a("## Problem Statement / Why")
    a("")
    proj_name = project.get("name", "")
    proj_desc = project.get("description", "")
    if proj_name or proj_desc:
        if proj_name:
            a(f"**Project:** {proj_name}")
        if proj_desc:
            a("")
            a(proj_desc)
        a("")

    box_prompts = [
        (b["title"], b.get("prompt", "").strip())
        for b in boxes
        if b.get("prompt", "").strip()
        and b.get("prompt") != "Describe what this module does..."
    ]
    if box_prompts:
        for title, prompt in box_prompts:
            a(f"**{title}:** {prompt}")
            a("")
    elif not proj_desc:
        a("_No module prompts defined. Add a prompt to each box to describe its purpose._")
        a("")

    # ── Selected Architecture ──────────────────────────────────────────────── #
    a("## Selected Architecture")
    a("")
    if not boxes:
        a("_No boxes in scope._")
        a("")
    else:
        for box in sorted(boxes, key=lambda b: b.get("title", "").lower()):
            perm  = "dotted · agent-editable" if box.get("type") == "dotted" else "solid · requires permission"
            color = "🔵" if box.get("type") == "dotted" else "🟢"
            a(f"- {color} **{box.get('title', box['id'])}** ({perm})")
            desc = box.get("prompt", "").strip()
            if desc and desc != "Describe what this module does...":
                a(f"  - _{desc}_")
            linked = box.get("linkedFiles", [])
            if linked:
                a(f"  - {len(linked)} linked file(s)")
        a("")

    # ── Prior Box Story (bounded provenance from earlier Execute runs) ─────── #
    if prior_story:
        a(prior_story)
        if lines and lines[-1] != "":
            a("")

    # ── Relevant Files ────────────────────────────────────────────────────── #
    a("## Relevant Files")
    a("")
    if not files:
        a("_No files linked to the selected modules. Add `linkedFiles` via repo scan or Inspector._")
        a("")
    else:
        # Group by language
        by_lang: dict = {}
        for f in files:
            by_lang.setdefault(f["language"], []).append(f)
        for lang in sorted(by_lang):
            a(f"**{lang}**")
            for f in sorted(by_lang[lang], key=lambda x: x["path"]):
                size_str = _fmt_size(f["size"])
                a(f"- `{f['path']}` ({size_str})")
            a("")

    # ── Relevant Functions ────────────────────────────────────────────────── #
    a("## Relevant Functions")
    a("")
    if not functions:
        a("_No function metadata available. Run 'Scan repo → canvas' to populate ArchGraph._")
        a("")
    else:
        # Group by file path
        by_file: dict = {}
        for fn in functions:
            by_file.setdefault(fn["path"], []).append(fn)
        for fpath in sorted(by_file):
            a(f"### `{fpath}`")
            a("")
            for fn in by_file[fpath]:
                sig = _format_sig(fn)
                a(f"**`{sig}`**")
                if fn.get("purpose"):
                    a(f"> {fn['purpose']}")
                args = fn.get("args", [])
                if args:
                    for arg in args:
                        type_note = f": `{arg['type']}`" if arg.get("type") else ""
                        a(f"- `{arg['name']}`{type_note}")
                if fn.get("returns"):
                    a(f"- → `{fn['returns']}`")
                if fn.get("warnings"):
                    for w in fn["warnings"]:
                        a(f"- ⚠ {w}")
                a("")

    # ── Dataflow / Edges ──────────────────────────────────────────────────── #
    a("## Dataflow / Edges")
    a("")
    if not arrows:
        a("_No arrows in scope._")
        a("")
    else:
        for arrow in arrows:
            from_box = box_by_id.get(arrow.get("fromBox", ""))
            to_box   = box_by_id.get(arrow.get("toBox",   ""))
            from_title = from_box.get("title", arrow.get("fromBox", "?")) if from_box else arrow.get("fromBox", "?")
            to_title   = to_box.get("title",   arrow.get("toBox",   "?")) if to_box   else arrow.get("toBox",   "?")
            eff_type   = _arrow_type(arrow, from_box)
            perm_note  = "agent-editable" if eff_type == "dotted" else "requires permission"
            label_part = f" — `{arrow['label']}`" if arrow.get("label") else ""
            a(f"- **{from_title}** → **{to_title}** ({perm_note}){label_part}")
        a("")

    # ── Permission Boundaries ─────────────────────────────────────────────── #
    a("## Permission Boundaries")
    a("")

    dotted_boxes = [b for b in boxes if b.get("type") == "dotted"]
    solid_boxes  = [b for b in boxes if b.get("type") != "dotted"]

    dotted_files = []
    for b in dotted_boxes:
        dotted_files.extend(b.get("linkedFiles", []))
    dotted_files = sorted(set(dotted_files))

    solid_files = []
    for b in solid_boxes:
        solid_files.extend(b.get("linkedFiles", []))
    solid_files = sorted(set(solid_files))

    a("### Allowed Direct Edits")
    a("")
    if dotted_boxes:
        a("The agent may modify the following files without approval:")
        a("")
        for b in sorted(dotted_boxes, key=lambda x: x.get("title", "")):
            lf = b.get("linkedFiles", [])
            if lf:
                a(f"**{b['title']}/**")
                for fp in sorted(lf)[:15]:
                    a(f"- `{fp}`")
            else:
                a(f"- **{b['title']}** _(no files linked)_")
        a("")
    else:
        a("_No dotted (agent-editable) modules in scope._")
        a("")

    a("### Requires Approval")
    a("")
    if solid_boxes:
        a("The agent must request permission before modifying the following files:")
        a("")
        for b in sorted(solid_boxes, key=lambda x: x.get("title", "")):
            lf = b.get("linkedFiles", [])
            if lf:
                a(f"**{b['title']}/**")
                for fp in sorted(lf)[:15]:
                    a(f"- `{fp}`")
            else:
                a(f"- **{b['title']}** _(no files linked)_")
        a("")
    else:
        a("_No solid (protected) modules in scope._")
        a("")

    # ── OpenPM Tasks ──────────────────────────────────────────────────────── #
    a("## OpenPM Tasks")
    a("")
    if not tasks:
        a("_No tasks linked to the selected modules._")
        a("")
    else:
        col_order = {"doing": 0, "testing": 1, "todo": 2, "done": 3}
        for task in sorted(tasks, key=lambda t: col_order.get(t.get("column", "todo"), 99)):
            col    = task.get("column", "todo").upper()
            veri   = task.get("verificationStatus", "pending")
            vmark  = {"passed": "✅", "failed": "❌", "pending": "⏳"}.get(veri, "⏳")
            a(f"- {vmark} **{task.get('title', '(untitled)')}** [{col}]")
            desc = task.get("description", "").strip()
            if desc:
                a(f"  - {desc}")
        a("")

    # ── Requested Change ──────────────────────────────────────────────────── #
    a("## Requested Change")
    a("")
    if user_prompt and user_prompt.strip():
        a(user_prompt.strip())
    else:
        a("_No specific instruction provided. Describe what you want the agent to do._")
    a("")

    # ── Acceptance Criteria ───────────────────────────────────────────────── #
    a("## Acceptance Criteria")
    a("")
    dotted_n = len(dotted_boxes)
    solid_n  = len(solid_boxes)
    a(f"- {len(boxes)} module(s) in scope"
      + (f" ({dotted_n} agent-editable, {solid_n} protected)" if boxes else ""))
    if dotted_files:
        a(f"- {len(dotted_files)} file(s) available for direct edit")
    if solid_files:
        a(f"- {len(solid_files)} file(s) requiring approval")
    if tasks:
        doing_tasks    = [t for t in tasks if t.get("column") == "doing"]
        todo_tasks     = [t for t in tasks if t.get("column") == "todo"]
        testing_tasks  = [t for t in tasks if t.get("column") == "testing"]
        if doing_tasks:
            a(f"- In progress: {', '.join(t['title'] for t in doing_tasks)}")
        if todo_tasks:
            a(f"- Pending: {', '.join(t['title'] for t in todo_tasks)}")
        if testing_tasks:
            a(f"- Under verification: {', '.join(t['title'] for t in testing_tasks)}")
    if functions:
        a(f"- {len(functions)} function contract(s) provided for reference")
    if not boxes and not tasks:
        a("- _Define modules on the canvas and link tasks to generate specific criteria._")
    a("")

    # ── Warnings / Missing Metadata ───────────────────────────────────────── #
    a("## Warnings / Missing Metadata")
    a("")
    if not warnings:
        a("✅ No warnings detected.")
    else:
        for w in warnings[:50]:   # cap display at 50
            a(f"- ⚠ {w}")
        if len(warnings) > 50:
            a(f"- … and {len(warnings) - 50} more warning(s)")
    a("")

    return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────── #

def _format_sig(fn: dict) -> str:
    """Format a function node into a readable signature string.

    Args:
        fn: dict — ArchGraph function dict with name, args, returns.

    Returns:
        str — e.g. "start(repo_path: str, port: int) → None"
    """
    args = fn.get("args", [])
    parts = []
    for arg in args:
        if arg.get("type"):
            parts.append(f"{arg['name']}: {arg['type']}")
        else:
            parts.append(arg["name"])
    sig = f"{fn['name']}({', '.join(parts)})"
    if fn.get("returns"):
        sig += f" → {fn['returns']}"
    return sig


def _arrow_type(arrow: dict, from_box: Optional[dict]) -> str:
    """Resolve the effective permission type of an arrow.

    Uses the arrow's own `type` field if set, otherwise inherits from the
    source box type.  Defaults to 'dotted' if neither is available.

    Args:
        arrow:    dict        — canvas arrow dict.
        from_box: dict | None — source box dict, or None if unresolvable.

    Returns:
        str — 'dotted' or 'solid'.
    """
    if arrow.get("type") in ("dotted", "solid"):
        return arrow["type"]
    if from_box and from_box.get("type") in ("dotted", "solid"):
        return from_box["type"]
    return "dotted"


def _fmt_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable size string.

    Args:
        size_bytes: int — file size in bytes.

    Returns:
        str — e.g. "14.2 KB" or "512 B".
    """
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"
