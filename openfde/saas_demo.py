"""
openfde/saas_demo.py — a deterministic, local-only SaaS example seed: an "AI support inbox".

Loads a realistic, product-shaped intent flow onto an EMPTY canvas — five connected **planned**
intent steps a SaaS team would sketch for a lightweight AI support inbox:

    [ingest customer messages] -> [classify issue] -> [draft response]
        -> [review approval] -> [log resolution]

Unlike the Sketch-First demo (a static "already built" showcase whose boxes carry implementationFiles
up front), THIS seed is meant to be RUN. The boxes start PLANNED — no implementationFiles — so a user
selects them, presses Run, and watches the Agent Council ground them into real files IN PLACE,
proving the full OpenFDE loop on a realistic example:

    intent -> Run -> architecture/files -> OpenPM tasks -> episode/commit -> Story.

**Side-effect-free + honest:** seeding writes NO repo files and triggers NO scan (only
``.openfde/state.json``). The implementation a Run produces is whatever the configured provider
writes; with the keyless **echo** provider it is a small, clearly-marked demo fixture under
``openfde_work/`` — this module only seeds the sketch, it never fakes a build.
"""

# Five product-shaped steps for an AI support inbox. PLANNED on purpose (no implementationFiles) — a
# Run grounds them into files in place, so the example exercises the real intent-run machinery.
_STEPS = [
    ("inbox_ingest",   "ingest customer messages", "pull new customer messages from the support inbox"),
    ("inbox_classify", "classify issue",           "tag each message by urgency and topic"),
    ("inbox_draft",    "draft response",           "draft a suggested reply for the issue"),
    ("inbox_review",   "review approval",          "a human reviews and approves or edits the draft"),
    ("inbox_log",      "log resolution",           "send the approved reply and log how it was resolved"),
]

_W, _H, _Y, _X0, _GAP = 200, 140, 170, 60, 230


def _intent_box(bid, i, title, prompt):
    return {"id": bid, "x": _X0 + i * _GAP, "y": _Y, "w": _W, "h": _H, "type": "dotted",
            "kind": "intent", "title": title, "prompt": prompt,
            "linkedFiles": [], "status": "draft"}


def support_inbox_demo_state() -> dict:
    """The deterministic SaaS example canvas: five connected PLANNED intent steps for an AI support
    inbox. Pure — returns ``{boxes, arrows}``; a real Run grounds the steps into files in place
    (never pre-set here). The caller only persists this canvas state; no file I/O, no scan."""
    boxes = [_intent_box(bid, i, title, prompt)
             for i, (bid, title, prompt) in enumerate(_STEPS)]
    arrows = [{"id": f"inbox_ar{i}", "fromBox": _STEPS[i][0], "fromPort": "e",
               "toBox": _STEPS[i + 1][0], "toPort": "w", "type": "dotted", "label": ""}
              for i in range(len(_STEPS) - 1)]
    return {"boxes": boxes, "arrows": arrows}
