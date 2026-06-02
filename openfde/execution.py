"""
openfde/execution.py — execution backend abstraction (Step 19).

OpenFDE owns architecture, permissions, memory, timeline, tracing, and
reporting. Execution backends are pluggable:

  - openfde-native      : the local placeholder flow (Steps 17–18) — canvas
                          pulse, trace, auto-commit. No real code changes.
  - claude-code-workflow: compile the selected scope into a Claude Code dynamic
                          workflow artifact (Architect → Senior Dev → Verifier →
                          Report). Prepared only — OpenFDE does not auto-run it.

This module is the clean *contract*, not a competing agent framework. It does no
shell execution, no network calls, and no git mutations.
"""

import logging
import secrets

from openfde.claude_workflow import build_workflow_payload, detect_verification, render_workflow_script
from openfde.spec import compile_spec

logger = logging.getLogger("openfde.execution")

ACTIVE_DEFAULT = "openfde-native"

_BACKENDS = [
    {
        "id": "openfde-native",
        "label": "OpenFDE native",
        "description": "Local placeholder execution — canvas pulse, trace, and auto-commit. No real code changes.",
        "mode": "native",
    },
    {
        "id": "claude-code-workflow",
        "label": "Claude Code workflow",
        "description": "Compile the selected scope into a Claude Code dynamic workflow. Prepared only — OpenFDE does not auto-run it.",
        "mode": "prepared",
    },
    {
        "id": "openfde-agent",
        "label": "OpenFDE native agent (beta)",
        "description": "Run the Senior Dev role (api mode) to edit in-scope files via a real model call, then reconcile through the gated commit path.",
        "mode": "agent",
    },
    {
        "id": "openfde-council",
        "label": "Agent Council (Architect → Sr Dev → Verifier)",
        "description": "Run the bounded council loop: Architect briefs, Senior Dev implements (scoped), Verifier reviews and reprompts once. Lands through the gated commit/approval path.",
        "mode": "council",
    },
]
_BACKEND_IDS = {b["id"] for b in _BACKENDS}


def list_backends(active: str) -> dict:
    """Return available backends with the active one flagged.

    Args:
        active: str — currently active backend id.

    Returns:
        dict — {"backends": [...with "active" bool...], "active": str}.
    """
    return {
        "backends": [{**b, "active": b["id"] == active} for b in _BACKENDS],
        "active": active,
    }


def is_valid_backend(backend_id: str) -> bool:
    """Return whether a backend id is known.

    Args:
        backend_id: str — candidate backend id.

    Returns:
        bool — True if known.
    """
    return backend_id in _BACKEND_IDS


def compile_workflow(
    canvas_state: dict,
    tasks: list,
    project: dict,
    graph: dict,
    box_specs: dict,
    ledger: list,
    root,
    selected_box_ids: list,
    selected_arrow_ids: list,
    user_prompt: str,
) -> dict:
    """Compile a selected scope into a Claude Code workflow payload + script.

    Reuses the Step-12 spec compiler for the structured context, then renders
    the workflow payload (scope, permissions, box story, ledger, verification)
    and the markdown workflow definition.

    Args:
        canvas_state: dict — persisted canvas {boxes, arrows}.
        tasks: list — OpenPM tasks.
        project: dict — project metadata.
        graph: dict — live ArchGraph.
        box_specs: dict — box-spec provenance map.
        ledger: list — project.md ledger entries.
        root: Path — repo root (for verification detection).
        selected_box_ids: list — selected box ids.
        selected_arrow_ids: list — selected arrow ids.
        user_prompt: str — optional user request.

    Returns:
        dict — {workflowId, payload, script, context, specMarkdown}.
    """
    spec = compile_spec(
        canvas_state, tasks, project, graph,
        selected_box_ids, selected_arrow_ids, user_prompt,
        box_specs=box_specs,
    )
    context = spec["context"]
    verification = detect_verification(root)
    payload = build_workflow_payload(context, project, box_specs, ledger, user_prompt, verification)
    workflow_id = "wf_" + secrets.token_hex(5)
    script = render_workflow_script(payload, workflow_id)

    logger.info(
        "Workflow compiled %s: %d module(s), %d file(s), %d function(s), %d verification cmd(s)",
        workflow_id, len(context.get("boxes", [])), len(context.get("files", [])),
        len(context.get("functions", [])), len(verification),
    )
    return {
        "workflowId": workflow_id,
        "payload": payload,
        "script": script,
        "context": context,
        "specMarkdown": spec["markdown"],
    }
