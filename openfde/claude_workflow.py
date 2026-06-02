"""
openfde/claude_workflow.py — compile an OpenFDE scope into a Claude Code
dynamic-workflow payload + script (Step 19).

OpenFDE owns the architecture, permissions, memory, and verification policy.
This module renders that scope into the contract a Claude Code dynamic workflow
runs against — Architect Review → Senior Dev Implementation → Verifier → Report
Back to OpenFDE — and the structured JSON OpenFDE expects back.

Pure functions only: no execution, no shell, no network. The output is a
*prepared* artifact a human (or a future bridge) hands to Claude Code.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("openfde.claude_workflow")

_MAX_FILES = 60
_MAX_FUNCS = 80
_MAX_STORY = 12
_MAX_LEDGER = 10
_DEFAULT_PROMPT = "Describe what this module does..."

# The exact JSON shape OpenFDE expects a workflow run to report back.
OUTPUT_CONTRACT = {
    "status": "passed | failed | needs_approval",
    "filesChanged": [{"path": "str", "status": "A|M|D"}],
    "functionsChanged": [{"name": "str", "path": "str"}],
    "testsRun": [{"command": "str", "result": "pass|fail"}],
    "verificationResult": "pass | fail | skipped",
    "errors": ["str"],
    "suggestedCanvasUpdates": [{"boxId": "str", "change": "str"}],
    "reportSummary": "str — touched files, tests, risks, follow-up actions",
}

WORKFLOW_RULES = [
    "Do NOT modify solid/protected files without explicit approval from OpenFDE.",
    "Stay inside the declared scope; do not edit files outside the listed modules.",
    "Commit only through OpenFDE's git commit endpoint, or report changes back for OpenFDE to commit — never push, never force, never rewrite history.",
    "Run the listed verification commands; do not invent destructive ones.",
    "Return exactly the JSON output contract below so OpenFDE can record the outcome.",
]


# ─── Verification detection ───────────────────────────────────────────────── #

def detect_verification(root: Path) -> list:
    """Detect likely verification commands for the repo (never run them).

    Args:
        root: Path — repository root.

    Returns:
        list[str] — human-readable verification commands (possibly empty).
    """
    cmds: list = []
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts", {})
        except (OSError, ValueError):
            scripts = {}
        for name in ("lint", "build", "test", "typecheck"):
            if name in scripts:
                cmds.append(f"npm run {name}")
    # Also handle a frontend/ subdir (OpenFDE-style layout)
    fe_pkg = root / "frontend" / "package.json"
    if fe_pkg.exists():
        try:
            scripts = (json.loads(fe_pkg.read_text(encoding="utf-8")) or {}).get("scripts", {})
        except (OSError, ValueError):
            scripts = {}
        for name in ("lint", "build"):
            if name in scripts:
                cmds.append(f"cd frontend && npm run {name}")
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        if (root / "tests").exists() or any(root.glob("**/test_*.py")):
            cmds.append("python -m pytest")
        cmds.append("python -m compileall .")
    # Dedup, keep order
    seen: set = set()
    out: list = []
    for c in cmds:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ─── Payload ──────────────────────────────────────────────────────────────── #

def _fn_signature(fn: dict) -> str:
    args = ", ".join(a["name"] + (f": {a['type']}" if a.get("type") else "") for a in fn.get("args", []))
    sig = f"{fn.get('name', '?')}({args})"
    if fn.get("returns"):
        sig += f" → {fn['returns']}"
    return sig


def build_workflow_payload(
    context: dict,
    project: dict,
    box_specs: dict,
    ledger: list,
    user_prompt: str,
    verification: list,
) -> dict:
    """Assemble the structured workflow payload from a compiled scope.

    Args:
        context: dict — compile_spec() context {boxes, arrows, files, functions, tasks, warnings}.
        project: dict — project metadata.
        box_specs: dict — box-spec provenance map (boxId → spec).
        ledger: list — project.md ledger entries (oldest-first).
        user_prompt: str — optional freeform user request.
        verification: list — detected verification commands.

    Returns:
        dict — the workflow payload (JSON-safe).
    """
    boxes = context.get("boxes", [])
    arrows = context.get("arrows", [])
    files = context.get("files", [])
    functions = context.get("functions", [])

    box_by_id = {b["id"]: b for b in boxes}
    dotted = [b for b in boxes if b.get("type") == "dotted"]
    solid = [b for b in boxes if b.get("type") != "dotted"]

    editable_files, protected_files = [], []
    for b in dotted:
        editable_files.extend(b.get("linkedFiles", []))
    for b in solid:
        protected_files.extend(b.get("linkedFiles", []))

    # Per-box story (current intent + last few prompt fragments)
    box_story = []
    for b in boxes[:_MAX_STORY]:
        spec = (box_specs or {}).get(b["id"])
        if not spec:
            continue
        box_story.append({
            "boxId": b["id"],
            "title": b.get("title"),
            "currentIntent": spec.get("currentIntent", ""),
            "recentPrompts": [h.get("promptFragment", "") for h in spec.get("promptHistory", [])[:3]],
        })

    # Relevant ledger context (entries touching the selected boxes, else recent)
    sel_box_ids = set(box_by_id)
    relevant = [e for e in (ledger or []) if any(bid in sel_box_ids for bid in e.get("boxIds", []))]
    ledger_ctx = (relevant or (ledger or []))[-_MAX_LEDGER:]
    ledger_context = [
        {"role": e.get("role"), "title": e.get("title", ""), "summary": e.get("summary", "")}
        for e in ledger_ctx
    ]

    dataflow = []
    for a in arrows:
        fb = box_by_id.get(a.get("fromBox"))
        tb = box_by_id.get(a.get("toBox"))
        dataflow.append({
            "from": fb.get("title") if fb else a.get("fromBox"),
            "to": tb.get("title") if tb else a.get("toBox"),
            "type": a.get("type") or (fb.get("type") if fb else "dotted"),
            "label": a.get("label", ""),
        })

    return {
        "openfde": {"version": "0.1.0", "owns": ["architecture", "permissions", "memory", "verification", "reporting"]},
        "scope": {
            "modules": [
                {"boxId": b["id"], "title": b.get("title"), "type": b.get("type", "dotted"),
                 "path": b.get("linkedPath", ""), "linkedFiles": b.get("linkedFiles", [])[:_MAX_FILES]}
                for b in boxes
            ],
            "files": [
                {"path": f["path"], "language": f.get("language"), "size": f.get("size")}
                for f in files[:_MAX_FILES]
            ],
            "functions": [
                {"name": fn.get("name"), "path": fn.get("path"), "line": fn.get("line"),
                 "signature": _fn_signature(fn), "purpose": fn.get("purpose", "")}
                for fn in functions[:_MAX_FUNCS]
            ],
            "arrows": dataflow,
        },
        "permissions": {
            "editableModules": [b.get("title") for b in dotted],
            "protectedModules": [b.get("title") for b in solid],
            "editableFiles": sorted(set(editable_files))[:_MAX_FILES],
            "protectedFiles": sorted(set(protected_files))[:_MAX_FILES],
            "protectedRequiresApproval": True,
        },
        "boxStory": box_story,
        "ledgerContext": ledger_context,
        "verification": verification,
        "userRequest": (user_prompt or "").strip(),
        "rules": WORKFLOW_RULES,
        "outputContract": OUTPUT_CONTRACT,
        "summaryRequirements": ["touched files", "tests run", "risks", "follow-up actions"],
        "project": {"name": project.get("name", ""), "description": project.get("description", "")},
    }


# ─── Script ───────────────────────────────────────────────────────────────── #

def render_workflow_script(payload: dict, workflow_id: str) -> str:
    """Render the Claude Code dynamic-workflow definition as markdown.

    The document describes the four stages, the permission rules, the
    verification commands, and the exact JSON contract OpenFDE expects back. A
    human (or a future bridge) hands this to Claude Code; OpenFDE does not run it.

    Args:
        payload: dict — the workflow payload from build_workflow_payload().
        workflow_id: str — the prepared workflow id.

    Returns:
        str — markdown workflow definition.
    """
    p = payload
    perm = p["permissions"]
    lines: list = []
    a = lines.append

    a("# OpenFDE → Claude Code Dynamic Workflow")
    a("")
    a(f"> Backend: `claude-code-workflow` · Workflow: `{workflow_id}` · Status: **prepared** (not auto-run)")
    a("")
    a("OpenFDE owns the architecture, permissions, project memory, and verification policy. "
      "Run this as a Claude Code dynamic workflow and report the result back to OpenFDE.")
    a("")

    if p.get("userRequest"):
        a("## Requested change")
        a("")
        a(p["userRequest"])
        a("")

    a("## Scope")
    a("")
    for m in p["scope"]["modules"]:
        tag = "🔵 dotted" if m["type"] == "dotted" else "🟢 solid"
        a(f"- {tag} **{m['title']}** (`{m['path'] or m['boxId']}`) — {len(m['linkedFiles'])} file(s)")
    a("")
    if p["scope"]["functions"]:
        a(f"- {len(p['scope']['functions'])} function(s) in scope (see payload JSON).")
        a("")

    a("## Permissions")
    a("")
    a(f"- **Editable (dotted), no approval needed:** {', '.join(perm['editableModules']) or '—'}")
    a(f"- **Protected (solid), REQUIRES APPROVAL:** {', '.join(perm['protectedModules']) or '—'}")
    if perm["protectedFiles"]:
        a("- Protected files (do not modify without approval):")
        for f in perm["protectedFiles"][:20]:
            a(f"  - `{f}`")
    a("")

    if p["scope"]["arrows"]:
        a("## Dataflow")
        a("")
        for d in p["scope"]["arrows"]:
            label = f" — `{d['label']}`" if d.get("label") else ""
            a(f"- **{d['from']}** → **{d['to']}** ({d['type']}){label}")
        a("")

    if p.get("boxStory"):
        a("## Prior intent (box story)")
        a("")
        for s in p["boxStory"]:
            a(f"- **{s['title']}** — {s['currentIntent']}")
            for frag in s.get("recentPrompts", []):
                if frag:
                    a(f"  - _{frag}_")
        a("")

    if p.get("ledgerContext"):
        a("## Recent project ledger")
        a("")
        for e in p["ledgerContext"]:
            a(f"- **{e.get('role')}**: {e.get('title') or e.get('summary')}")
        a("")

    a("## Stages")
    a("")
    a("### 1. Architect Review")
    a("Read the scope, permissions, dataflow, and prior intent. Produce a short implementation plan. "
      "Flag anything that would require touching protected (solid) scope — that needs approval first.")
    a("")
    a("### 2. Senior Dev Implementation")
    a("Implement the requested change within the editable (dotted) scope only.")
    for r in p["rules"]:
        a(f"- {r}")
    a("")
    a("### 3. Verifier")
    a("Run the verification commands and record pass/fail:")
    if p["verification"]:
        for c in p["verification"]:
            a(f"- `{c}`")
    else:
        a("- _No verification commands detected — state that verification was skipped._")
    a("")
    a("### 4. Report Back to OpenFDE")
    a("Return the structured JSON below. Do not commit directly — either call OpenFDE's "
      "`POST /api/git/commit` endpoint or include the changed files so OpenFDE commits them.")
    a("")
    a("## Output contract — return exactly this JSON shape")
    a("")
    a("```json")
    a(json.dumps(p["outputContract"], indent=2))
    a("```")
    a("")

    return "\n".join(lines)
