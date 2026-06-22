"""
openfde/sketch_demo.py — a deterministic, local-only Sketch-First demo fixture.

Loads a tiny saved sketch — ``[read data] -> [clean data] -> [train model]`` — onto an EMPTY canvas
so a fresh user instantly sees the v3 "✓ built / BECAME / highlight" surfaces without depending on
stale manual canvas state. Each intent box carries an ``implementationFiles`` link, which drives the
``✓ BUILT`` state and **file-level** BECAME children via v3's existing graceful path.

**Side-effect-free by design:** this writes NO files to the repo and triggers NO archGraph rescan or
review reassimilation — so the demo loads in well under a second on any repo. It therefore shows
file-level BECAME only (no per-function children); the function-rich path is reserved for REAL runs,
where the council writes real files that assimilation parses for symbols. Pure data; no agent, no
commit, no network, no file I/O.
"""

# A clearly-named, illustrative path the sketch "became" — never written to disk (file-level demo).
DEMO_FILE = "openfde_work/sketch_demo_pipeline.py"


def _intent_box(bid, x, title, prompt):
    return {"id": bid, "x": x, "y": 150, "w": 210, "h": 150, "type": "dotted",
            "kind": "intent", "title": title, "prompt": prompt,
            "linkedFiles": [], "status": "draft",
            "implementationFiles": [DEMO_FILE],
            "implementationMeta": {"attribution": "graph", "confidence": 0.4, "runId": "sketch-demo"}}


def sketch_first_demo_state() -> dict:
    """The deterministic demo canvas: three connected intent steps, each already implemented by the
    demo file, plus a module box that links the same file (so clicking an intent box ambers a real
    canvas node and dims the rest). Pure — returns ``{boxes, arrows}``; the file write is the
    caller's job (see :func:`write_demo`)."""
    boxes = [
        _intent_box("sketch_read",  70, "read the data",      "load the CSV into rows"),
        _intent_box("sketch_clean", 320, "drop nan values",   "find any NaN values and drop those rows"),
        _intent_box("sketch_train", 570, "train a classifier", "fit a classification model"),
        {"id": "sketch_mod", "x": 320, "y": 420, "w": 220, "h": 120, "type": "dotted",
         "title": "Pipeline module", "prompt": "the generated implementation",
         "linkedFiles": [DEMO_FILE], "status": "draft"},
    ]
    arrows = [
        {"id": "sketch_ar1", "fromBox": "sketch_read", "fromPort": "e",
         "toBox": "sketch_clean", "toPort": "w", "type": "dotted", "label": ""},
        {"id": "sketch_ar2", "fromBox": "sketch_clean", "fromPort": "e",
         "toBox": "sketch_train", "toPort": "w", "type": "dotted", "label": ""},
    ]
    return {"boxes": boxes, "arrows": arrows}
