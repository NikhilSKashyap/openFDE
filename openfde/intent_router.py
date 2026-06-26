"""Deterministic Orient intent router — one prompt → one execution mode, no LLM.

The Orient UI exposes a single Run button; this decides what it does. The rules are explainable and
conservative: a prompt only becomes a Program when it CLEARLY enumerates multiple slices/phases/parts
(or an explicit first→then multi-deliverable flow), so a casual mention of the word "slice" never
forces Program Mode. Everything else is a single bounded task (council), a question (ask, no edits), an
issue report (issue), or — only for an under-specified / high-blast-radius prompt — a clarify block.

Default permissions: implementation routes (program/council) default ``allowEdits=True``; ask/issue/
clarify never edit. Push is always off here (a separate, explicit opt-in). A later step may let the
Architect refine the decomposition AFTER the route is chosen — this module stays deterministic.
"""

import re

from openfde.program import BLOCKED_BLAST_RADIUS, _BLAST, _VAGUE, plan_program

MODE_PROGRAM = "program"
MODE_COUNCIL = "council"
MODE_ASK = "ask"
MODE_ISSUE = "issue"
MODE_CLARIFY = "clarify"

# Implementation verbs — a prompt that commands a change (so an advisory phrasing isn't mistaken for it).
_IMPL_VERB = re.compile(
    r"\b(add|fix|update|implement|build|create|make|change|refactor|rename|remove|delete|write|wire|"
    r"hook up|set up|integrate|migrate|replace|polish|tighten|adjust|patch|extend|enable|disable)\b", re.I)

# Numbered slice/phase/part/step markers at a clause boundary, e.g. "Slice 1:", ". Phase 2.", "Part 3)".
# Requires the digit + trailing punctuation so "part of the file" / "slice it up" never match.
_NUM_MARKER = re.compile(r"(?:^|\n|[.;])\s*(slice|phase|part|step)\s*(\d+)\s*[:.\-)]", re.I)
# An explicit multi-deliverable flow: "first …, then …".
_MULTI_FLOW = re.compile(r"\bfirst\b.{3,}\b(then|after that|afterwards|next|second(ly)?|finally)\b", re.I | re.S)

# Issue / bug-report framing.
_ISSUE_CREATE = re.compile(r"\b(raise|file|open|create|log|submit)\b[\w\s'-]{0,18}\b(issue|bug|ticket)\b|"
                           r"\b(github|gh)\s+issue\b|\breport (a |this )?(bug|issue)\b", re.I)
_BUG_REPORT = re.compile(r"\bthis broke\b|\bbroke when\b|\bstopped working\b|\bis broken\b|\bdoes ?n'?t work\b|"
                         r"\bnot working\b|\bregression\b|\bcrash(es|ed|ing)?\b|\bthrows? an? (error|exception)\b", re.I)

# Question / planning-only framing (advisory patterns + a generic question that gives no command).
_ADVISORY = re.compile(
    r"\bwhat do you think\b|\bshould (we|i)\b|\bdo you think\b|\bhow (does|do|would|should)\b.{0,60}\bwork\b|"
    r"\bwhat'?s the best way\b|\bis it (worth|possible|a good idea|better)\b|\bwhy (does|is|are|do|did)\b|"
    r"\bexplain\b|\bthoughts\b|\bwhich (is|approach|one|option)\b|\bwhat are the (trade|options|pros|cons)\b|"
    r"\bwhat'?s the difference\b|\bhow does this work\b", re.I)
_QUESTION_START = re.compile(r"^\s*(what|why|how|should|could|would|is|are|do|does|can|which|when|where|who)\b", re.I)


def _result(mode, confidence, reason, allow_edits, *, detected_slices=None, signals=None) -> dict:
    return {"mode": mode, "confidence": round(float(confidence), 2), "reason": reason,
            "allowEdits": bool(allow_edits), "detectedSlices": detected_slices or [], "signals": signals or []}


def _numbered_markers(prompt: str):
    """Distinct (kind, number) slice/phase/part/step markers, e.g. {('slice','1'), ('slice','2')}."""
    return {(m.group(1).lower(), m.group(2)) for m in _NUM_MARKER.finditer(prompt)}


def _is_question(prompt: str, has_impl: bool) -> bool:
    if _ADVISORY.search(prompt):
        return True
    return prompt.rstrip().endswith("?") and not has_impl and bool(_QUESTION_START.match(prompt))


def route_intent(prompt, context=None) -> dict:
    """Route a prompt to one of ``program | council | ask | issue | clarify`` with a confidence, a
    plain-language reason, the default ``allowEdits``, and (for explainability) the matched ``signals``
    + any ``detectedSlices``. Deterministic; conservative about forcing Program Mode."""
    p = (prompt or "").strip()
    words = re.findall(r"\w+", p)

    # 1. A bare fragment (empty or a single word like "fix") → clarify. A short but concrete command
    #    ("update README") is NOT a fragment — it falls through to council; only genuinely vague short
    #    prompts ("do stuff") are caught by the _VAGUE check below.
    if not p or len(words) < 2:
        return _result(MODE_CLARIFY, 0.9, "Too short to route — describe the change or question.",
                       False, signals=["too_short"])

    has_impl = bool(_IMPL_VERB.search(p))

    # 2. Issue / bug report — explicit "raise an issue", or bug-report language with no fix command.
    if _ISSUE_CREATE.search(p) or (_BUG_REPORT.search(p) and not has_impl):
        return _result(MODE_ISSUE, 0.85, "Reads as an issue / bug report — routing to the issue flow.",
                       False, signals=["issue"])

    # 3. Question / planning — answered in chat, never edits files.
    if _is_question(p, has_impl):
        return _result(MODE_ASK, 0.8, "A question / planning prompt — answered with no file edits.",
                       False, signals=["question"])

    # 4. Program — CLEARLY multiple slices/phases/parts (≥2 numbered markers) or a first→then flow.
    markers = _numbered_markers(p)
    if len(markers) >= 2 or _MULTI_FLOW.search(p):
        slices, block = plan_program(p)
        if block == BLOCKED_BLAST_RADIUS:
            return _result(MODE_CLARIFY, 0.75, "Spans the whole codebase — narrow the scope before running.",
                           False, signals=["blast_radius"])
        if slices and len(slices) >= 2:
            sig = sorted(f"{k}{n}" for k, n in markers) or ["multi_flow"]
            return _result(MODE_PROGRAM, 0.9 if markers else 0.75,
                           f"{len(slices)} slices detected — running as a Program.",
                           True, detected_slices=[s["title"] for s in slices], signals=sig)
        # markers present but only one real slice → fall through to a single council task.

    # 5. Single-but-dangerous → clarify.
    if _BLAST.search(p):
        return _result(MODE_CLARIFY, 0.75, "High blast radius — confirm the scope before running.",
                       False, signals=["blast_radius"])
    if _VAGUE.match(p):
        return _result(MODE_CLARIFY, 0.7, "Too vague — say what to change.", False, signals=["vague"])

    # 6. Default — one bounded implementation / review task → the council loop.
    return _result(MODE_COUNCIL, 0.8, "One bounded implementation / review task — running the council loop.",
                   True, signals=["single_task"])
