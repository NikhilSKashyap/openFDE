"""
openfde/box_spec.py — deterministic box spec / prompt provenance (Step 15).

Each architecture box carries an evolution story derived from Execute runs and
the project.md ledger: a current intent, the latest prompt fragment, a bounded
prompt history, and references to the ledger entries / timeline events that
explain how it got there.

This is *provenance*, not AI extraction. Attribution is deterministic:
- an explicit user prompt is attributed to every scoped box (confidence drops
  when one prompt is shared across many boxes — that ambiguity is editable
  later);
- otherwise the box's own prompt is used (high confidence — it is the box's
  stated intent);
- otherwise the execution summary is used as a low-confidence fallback.

The full compiled spec is never copied into a box spec — only references
(ledger entry id + event id) are stored. The full prompt lives in project.md.
"""

import logging
import secrets
from datetime import datetime, timezone

logger = logging.getLogger("openfde.box_spec")

# ─── Tunables ─────────────────────────────────────────────────────────────── #

_MAX_HISTORY: int = 30            # promptHistory items kept per box
_MAX_FRAGMENT_LEN: int = 280      # prompt fragment characters
_MAX_PRIOR_FRAGMENTS: int = 3     # fragments per box in the "Prior Box Story"
_MAX_PRIOR_BOXES: int = 8         # boxes rendered in the "Prior Box Story"
_MAX_REF_IDS: int = 50            # linkedEntryIds / linkedEventIds cap
_FILE_REF_CAP: int = 25           # filePaths per history item
_DEFAULT_BOX_PROMPT: str = "Describe what this module does..."


# ─── Helpers ──────────────────────────────────────────────────────────────── #

def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, limit: int) -> str:
    """Truncate a string to ``limit`` chars with an ellipsis when needed."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _meaningful_prompt(box: dict) -> str:
    """Return a box's own prompt if it is set and non-default, else ''."""
    bp = (box.get("prompt") or "").strip()
    return "" if (not bp or bp == _DEFAULT_BOX_PROMPT) else bp


def _confidence(has_user_prompt: bool, num_boxes: int,
                used_box_prompt: bool, used_fallback: bool) -> float:
    """Deterministic confidence that a fragment belongs to a box.

    Args:
        has_user_prompt: bool — an explicit user prompt drove this execute.
        num_boxes: int — number of boxes the user prompt was attributed to.
        used_box_prompt: bool — fragment came from the box's own prompt.
        used_fallback: bool — fragment came from the execution summary.

    Returns:
        float — confidence in [0.3, 1.0].
    """
    if used_fallback:
        return 0.3
    if used_box_prompt:
        return 0.9
    # Explicit user prompt: fully attributable to a single box, ambiguous when
    # one prompt is spread across many boxes.
    if num_boxes <= 1:
        return 1.0
    return max(0.3, round(1.0 / num_boxes, 2))


def _append_unique(seq: list, value, cap: int) -> list:
    """Append ``value`` to ``seq`` if absent (and truthy), keeping last ``cap``."""
    if not value:
        return seq
    if value in seq:
        return seq
    seq = seq + [value]
    return seq[-cap:]


# ─── Public API ───────────────────────────────────────────────────────────── #

def update_box_specs_from_execute(
    existing: dict,
    boxes_by_id: dict,
    *,
    box_ids: list,
    user_prompt: str,
    ledger_entry_id: str,
    event_id: str,
    file_paths: list,
    summary: str,
    outcome: str,
) -> dict:
    """Update box specs for every scoped box after an Execute run.

    Pure-ish: returns a new specs map (the input is not mutated). Attribution is
    deterministic (see module docstring).

    Args:
        existing: dict — current box-specs map keyed by boxId.
        boxes_by_id: dict — canvas boxes keyed by id (for title / prompt / files).
        box_ids: list — scoped box ids to update.
        user_prompt: str — optional freeform user instruction from Execute.
        ledger_entry_id: str — architect ledger entry id (full prompt reference).
        event_id: str — spec_generated event id.
        file_paths: list — in-scope file paths for this Execute.
        summary: str — short execution summary (fallback fragment).
        outcome: str — outcome label recorded against each prompt.

    Returns:
        dict — updated box-specs map keyed by boxId.
    """
    specs = dict(existing or {})
    ts = _now()
    up = (user_prompt or "").strip()
    valid_ids = [bid for bid in (box_ids or []) if bid]
    num_boxes = len(valid_ids) or 1
    scoped_files = [str(p) for p in (file_paths or [])]

    for bid in valid_ids:
        box = boxes_by_id.get(bid, {"id": bid, "title": bid})

        # ── Decide the fragment + provenance flags (deterministic) ──────────
        if up:
            fragment, used_box_prompt, used_fallback = up, False, False
        else:
            own = _meaningful_prompt(box)
            if own:
                fragment, used_box_prompt, used_fallback = own, True, False
            else:
                fragment, used_box_prompt, used_fallback = (summary or "").strip(), False, True

        fragment = _truncate(fragment, _MAX_FRAGMENT_LEN)
        confidence = _confidence(bool(up), num_boxes, used_box_prompt, used_fallback)

        # ── Per-box file attribution: intersect scope with the box's files ──
        linked = set(box.get("linkedFiles") or [])
        box_files = [f for f in scoped_files if f in linked][:_FILE_REF_CAP]

        item = {
            "id":             f"ph_{secrets.token_hex(5)}",
            "timestamp":      ts,
            "promptFragment": fragment,
            "fullPromptRef":  ledger_entry_id or None,
            "eventId":        event_id or None,
            "outcome":        _truncate(outcome or "", _MAX_FRAGMENT_LEN),
            "boxIds":         list(valid_ids),
            "filePaths":      box_files,
            "confidence":     confidence,
        }

        spec = specs.get(bid) or {
            "boxId":          bid,
            "title":          box.get("title", bid),
            "currentIntent":  "",
            "latestPromptFragment": "",
            "promptHistory":  [],
            "linkedEntryIds": [],
            "linkedEventIds": [],
            "updatedAt":      ts,
        }

        # promptHistory is newest-first; latest fragment mirrors history[0].
        spec["title"]                = box.get("title", spec.get("title", bid))
        spec["promptHistory"]        = ([item] + list(spec.get("promptHistory", [])))[:_MAX_HISTORY]
        spec["latestPromptFragment"] = fragment
        # Current intent: prefer the box's own stated prompt, else the fragment.
        spec["currentIntent"]        = _meaningful_prompt(box) or fragment or spec.get("currentIntent", "")
        spec["linkedEntryIds"]       = _append_unique(spec.get("linkedEntryIds", []), ledger_entry_id, _MAX_REF_IDS)
        spec["linkedEventIds"]       = _append_unique(spec.get("linkedEventIds", []), event_id, _MAX_REF_IDS)
        spec["updatedAt"]            = ts

        specs[bid] = spec

    logger.info("Box specs updated for %d box(es) (entry=%s event=%s)",
                len(valid_ids), ledger_entry_id, event_id)
    return specs


def render_prior_story(box_specs: dict, box_ids: list) -> str:
    """Render a bounded 'Prior Box Story' markdown section for selected boxes.

    Only boxes that already have a spec are included. Each box shows its current
    intent and up to the last 3 prompt fragments, plus reference counts — never
    the whole project.md.

    Args:
        box_specs: dict — box-specs map keyed by boxId.
        box_ids: list — scoped box ids (selection or all scoped boxes).

    Returns:
        str — markdown section (empty string when there is no prior story).
    """
    if not box_specs or not box_ids:
        return ""

    rendered: list = []
    for bid in box_ids:
        spec = box_specs.get(bid)
        if not spec or not spec.get("promptHistory"):
            continue
        rendered.append(spec)
        if len(rendered) >= _MAX_PRIOR_BOXES:
            break

    if not rendered:
        return ""

    lines: list = ["## Prior Box Story", ""]
    lines.append("_Bounded provenance from earlier Execute runs — see project.md for full prompts._")
    lines.append("")
    for spec in rendered:
        title = spec.get("title") or spec.get("boxId")
        lines.append(f"### {title}")
        intent = (spec.get("currentIntent") or "").strip()
        if intent:
            lines.append(f"- **Current intent:** {intent}")
        history = spec.get("promptHistory", [])[:_MAX_PRIOR_FRAGMENTS]
        if history:
            lines.append("- **Recent prompts:**")
            for h in history:
                frag = (h.get("promptFragment") or "").strip() or "(no fragment)"
                conf = h.get("confidence")
                out  = (h.get("outcome") or "").strip()
                meta = []
                if isinstance(conf, (int, float)):
                    meta.append(f"conf {conf:.2f}")
                if out:
                    meta.append(out)
                suffix = f" _({' · '.join(meta)})_" if meta else ""
                lines.append(f"  - {frag}{suffix}")
        entries = spec.get("linkedEntryIds", [])
        if entries:
            lines.append(f"- **Ledger refs:** {len(entries)} entry(ies); latest `{entries[-1]}`")
        lines.append("")

    return "\n".join(lines)


# ─── Workflow result intake (Step 20) ──────────────────────────────────────── #

_MAX_RESULTS = 10
_RESULT_FILE_CAP = 25
_RESULT_FN_CAP = 25


def apply_workflow_result(
    existing: dict,
    boxes_by_id: dict,
    box_ids: list,
    *,
    workflow_id: str,
    run_id: str,
    ledger_ids: list,
    event_ids: list,
    report_summary: str,
    tests_run: list,
    verification_result: str,
    files_changed: list,
    functions_changed: list,
    suggested_canvas_updates: list = None,
) -> dict:
    """Attach a bounded workflow-result record to each *attributable* box's spec.

    Attribution is deterministic and per-box — a box's story only records files
    and functions that actually belong to it. A scoped box gets a record when:

      - one of its ``linkedFiles`` is in ``files_changed`` (attribution "linked"),
      - a ``functions_changed`` entry's path is one of its ``linkedFiles``
        (attribution "linked"),
      - a ``suggested_canvas_updates`` entry names its boxId (attribution
        "suggestion"), or
      - *no* scoped box matched at all — then every scoped box gets a single
        low-confidence "global" note (empty file/function lists) so the result is
        still recorded somewhere without falsely attributing changes.

    A scoped box that does not match while *another* box does is left untouched —
    we never copy one box's changes into an unrelated box. The full workflow
    script is never copied — only references and a short summary.

    Args:
        existing: dict — current box-specs map keyed by boxId.
        boxes_by_id: dict — canvas boxes keyed by id (for title / linkedFiles).
        box_ids: list — scoped box ids to consider.
        workflow_id: str — workflow id.
        run_id: str — run id.
        ledger_ids: list — project.md ledger entry ids for this result.
        event_ids: list — timeline event ids for this result.
        report_summary: str — workflow reportSummary.
        tests_run: list — normalized testsRun entries.
        verification_result: str — pass/fail/skipped.
        files_changed: list — normalized filesChanged entries ({path, status}).
        functions_changed: list — normalized functionsChanged entries ({name, path}).
        suggested_canvas_updates: list — normalized suggestions ({boxId, change}).

    Returns:
        dict — updated box-specs map.
    """
    specs = dict(existing or {})
    ts = _now()
    files_changed = files_changed or []
    functions_changed = functions_changed or []
    summary = _truncate(report_summary, 280)
    suggested_box_ids = {
        s.get("boxId") for s in (suggested_canvas_updates or []) if s.get("boxId")
    }

    # ── Pass 1: compute each scoped box's own attributable changes ───────────
    valid_ids = [bid for bid in (box_ids or []) if bid]
    matches: dict = {}
    any_matched = False
    for bid in valid_ids:
        box = boxes_by_id.get(bid, {"id": bid, "title": bid})
        linked = set(box.get("linkedFiles") or [])
        box_files = [f.get("path") for f in files_changed
                     if f.get("path") and f.get("path") in linked][:_RESULT_FILE_CAP]
        box_fns = [fn.get("name") for fn in functions_changed
                   if fn.get("name") and fn.get("path") and fn.get("path") in linked][:_RESULT_FN_CAP]
        via_suggestion = bid in suggested_box_ids
        matched = bool(box_files or box_fns or via_suggestion)
        any_matched = any_matched or matched
        matches[bid] = {
            "box": box, "files": box_files, "fns": box_fns,
            "matched": matched, "viaSuggestion": via_suggestion,
        }

    # ── Pass 2: attach records only where attribution is real (or global) ────
    for bid in valid_ids:
        m = matches[bid]
        box = m["box"]
        if m["matched"]:
            box_files, box_fns = m["files"], m["fns"]
            if box_files or box_fns:
                attribution, confidence = "linked", 0.9
            else:
                attribution, confidence = "suggestion", 0.7
            is_global = False
        elif not any_matched:
            # Nothing could be attributed to any box: intentional global note.
            box_files, box_fns = [], []
            attribution, confidence, is_global = "global", 0.3, True
        else:
            # Another box owns this change — do not pollute this box's story.
            continue

        record = {
            "id":                 f"wr_{secrets.token_hex(5)}",
            "timestamp":          ts,
            "workflowId":         workflow_id,
            "runId":              run_id,
            "ledgerIds":          list(ledger_ids or []),
            "eventIds":           list(event_ids or []),
            "reportSummary":      summary,
            "testsRun":           list(tests_run or [])[:_RESULT_FN_CAP],
            "verificationResult": verification_result or "",
            "filesChanged":       box_files,
            "functionsChanged":   box_fns,
            "attribution":        attribution,
            "confidence":         confidence,
            "global":             is_global,
        }

        spec = specs.get(bid) or {
            "boxId": bid, "title": box.get("title", bid),
            "currentIntent": "", "latestPromptFragment": "",
            "promptHistory": [], "linkedEntryIds": [], "linkedEventIds": [],
            "workflowResults": [], "updatedAt": ts,
        }
        spec["title"]           = box.get("title", spec.get("title", bid))
        spec["workflowResults"] = ([record] + list(spec.get("workflowResults", [])))[:_MAX_RESULTS]
        for lid in (ledger_ids or []):
            spec["linkedEntryIds"] = _append_unique(spec.get("linkedEntryIds", []), lid, _MAX_REF_IDS)
        for eid in (event_ids or []):
            spec["linkedEventIds"] = _append_unique(spec.get("linkedEventIds", []), eid, _MAX_REF_IDS)
        spec["updatedAt"] = ts
        specs[bid] = spec

    attached = sum(1 for bid in valid_ids
                   if matches[bid]["matched"] or not any_matched)
    logger.info("Box specs updated from workflow result: %d/%d box(es) attributed (wf=%s)",
                attached, len(valid_ids), workflow_id)
    return specs
