"""
openfde/sketch_demo.py — a deterministic, local-only Sketch-First demo fixture.

Loads a tiny saved sketch — ``[read data] -> [clean data] -> [train model]`` — with each intent box
already linked to a real implementation file, so a browser can verify the v3 "✓ built / BECAME /
highlight" surfaces without depending on stale manual canvas state. It writes ONE clearly-named file
under the generated workspace (so the archGraph yields real function children) plus the canvas
boxes/arrows. Demo-only and non-destructive: the caller refuses to run on a non-empty canvas.

Pure helpers + one tiny file write — no agent, no commit, no network. The fixture file is
deliberately Python so the symbol children read richly, but nothing here makes the *workspace*
Python-only; it is just a demo example.
"""

from pathlib import Path

# A clearly-named, throwaway file under the generated workspace — never a real module.
DEMO_FILE = "openfde_work/sketch_demo_pipeline.py"

DEMO_CODE = '''\
"""OpenFDE Sketch-First demo — a tiny data pipeline. Safe to delete."""


def read_data(path):
    """Read the CSV into rows."""
    return open(path).read().splitlines()


def drop_nans(rows):
    """Drop rows that contain a missing value."""
    return [r for r in rows if r and "nan" not in r.lower()]


def train_model(rows):
    """Fit a tiny classifier and report its size."""
    return {"model": "fitted", "rows": len(rows)}
'''


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


def write_demo(root) -> dict:
    """Write the demo implementation file under the generated workspace and return the demo canvas
    state. Idempotent (overwrites the demo file's known contents). The path is clearly named and
    lives under ``openfde_work/``, so it reads as demo scaffolding, never a real module."""
    target = Path(root) / DEMO_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEMO_CODE, encoding="utf-8")
    return sketch_first_demo_state()
