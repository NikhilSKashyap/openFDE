"""
openfde/feedback.py — general product-feedback issues for OpenFDE's OWN tracker.

The repair hatch already files "Run with Senior Dev failed inside OpenFDE" issues
(see issue_repro + server's /api/feedback/*). This module is the GENERAL path: a
top-bar "Raise issue" where the user describes a bug, feature idea, rough UX, or
perf problem in plain words, the ARCHITECT drafts an editable issue from that plus
LIGHT app context, and nothing posts until the user clicks Raise.

Two guarantees keep OpenFDE's tracker free of the WATCHED repo's private data —
exactly like the hatch path:
  1. a hard output contract (Architect is told to describe in product terms only),
  2. a deterministic scrub of every known repo string PLUS a generic safety net
     (absolute paths, repo-relative paths, source filenames, pytest test names,
     fenced code) over whatever was actually written.

The deterministic template is the fallback when no Architect provider answers, so the
flow works offline. Everything here is pure (no GitHub, no network) — issue creation
is a separate, explicit, click-only step in the server.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("openfde.feedback")

# The body marker the tracker (and any future auto-triage) keys on. Mirrors the
# repair path's `kind=repair-run` marker.
GENERAL_MARKER = "<!-- openfde:report v=1 kind=general-feedback -->"

# Idempotent label taxonomy seeded on OpenFDE's repo. Includes the product-surface
# labels plus the repair-path labels (kept so the hatch flow's tags still exist).
SEED_LABELS = [
    ("bug", "Something in OpenFDE misbehaves"),
    ("feature", "New capability request"),
    ("ux", "Confusing or rough user experience"),
    ("performance", "Slowness, lag, or resource use"),
    ("auto-report", "Raised from inside OpenFDE by the report card"),
    ("canvas", "Architecture canvas and lenses"),
    ("openpm", "Board, intents, card lifecycle"),
    ("story", "Story / Tell narrative"),
    ("council", "Agent council / chat / runners"),
    ("verify-gate", "Checks, receipts, fingerprints"),
    ("language-pack", "Language packs (Python, JS/TS, …)"),
    ("webxr", "WebXR / 3D / immersive surfaces"),
    ("repair-hatch", "The failure → repair loop surfaces"),
    ("runner", "Senior Dev / council runners"),
]

# The compose chip → its canonical label (None for "other": no kind label).
KIND_LABEL = {"bug": "bug", "feature": "feature", "ux": "ux",
              "performance": "performance", "other": None}
_KIND_TITLE = {"bug": "Bug", "feature": "Feature request", "ux": "UX",
               "performance": "Performance", "other": "Feedback"}

# ── deterministic scrub (defense in depth over the Architect's output) ───────── #

_COST_RE = re.compile(r"\s*\(cost \$[\d.]+\)")
_SCRUB_FENCE = re.compile(r"```.*?```", re.S)
# Absolute paths under common roots only — never touches URLs.
_SCRUB_ABS = re.compile(
    r"(?:[A-Za-z]:\\|/(?:Users|home|var|tmp|opt|srv|mnt|private|data|root)/)[\w.\-/\\ ]+")
# A repo-relative path: at least one `/` and a file extension.
_SCRUB_RELPATH = re.compile(r"\b[\w.-]+(?:/[\w.-]+)+\.\w{1,5}\b")
# A bare source filename (reveals repo structure / a test file's name).
_SCRUB_FILE = re.compile(
    r"\b[\w-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|mts|cts|rb|go|rs|java|cpp|cc|cxx|c|hpp|"
    r"h|cs|php|swift|kt|scala|sh|sql)\b")
# A pytest-style test function name.
_SCRUB_TEST = re.compile(r"\btest_[A-Za-z0-9_]+\b")


def _apply_replacements(text: str, repls: dict) -> str:
    """Replace each known repo string with its placeholder, longest-first so a full
    path wins over its basename (same rule as the hatch scrubber)."""
    out = text or ""
    for needle, repl in sorted((repls or {}).items(), key=lambda kv: -len(kv[0])):
        if needle and len(needle) >= 3:
            out = out.replace(needle, repl)
    return out


def scrub_general(text: str, repls: dict | None = None) -> str:
    """Make a draft safe for OpenFDE's public tracker: drop known repo strings
    (``repls``), then run the generic net — fenced code, absolute/relative paths,
    source filenames, and pytest test names become placeholders, and any ``(cost
    $X)`` vintage is stripped. The user still reviews and edits before raising."""
    out = _apply_replacements(text or "", repls or {})
    out = _SCRUB_FENCE.sub("`<code omitted>`", out)
    out = _SCRUB_ABS.sub("<path>", out)
    out = _SCRUB_RELPATH.sub("<path>", out)
    out = _SCRUB_FILE.sub("<file>", out)
    out = _SCRUB_TEST.sub("<test>", out)
    return _COST_RE.sub("", out)


def general_replacements(context: dict | None, repo_name: str = "") -> dict:
    """Known repo strings to scrub: the watched repo's name, plus any path-like
    tokens that slipped into the curated context. The context SHOULD already be
    product-level — this is belt-and-suspenders."""
    repl: dict = {}
    if repo_name:
        repl[repo_name] = "<repo>"

    def _harvest(v):
        if isinstance(v, str):
            for tok in re.findall(r"[\w.-]*/[\w./-]+|\b[\w-]+\.\w{1,5}\b", v):
                if "/" in tok or "." in tok:
                    repl.setdefault(tok, "<path>")
        elif isinstance(v, dict):
            for x in v.values():
                _harvest(x)
        elif isinstance(v, list):
            for x in v:
                _harvest(x)

    _harvest(context or {})
    return repl


# ── drafting ─────────────────────────────────────────────────────────────────── #

_ARCHITECT_SYS = (
    "You are the Architect in OpenFDE, drafting a PRODUCT FEEDBACK issue for "
    "OpenFDE'S OWN public tracker from a user's description plus light app context "
    "(current view, app version, maybe an episode title). This is about OpenFDE the "
    "tool — a bug, feature idea, confusing UX, or performance problem — NOT about the "
    "user's repository. You MUST NOT include any watched-repo detail: no file paths, "
    "no test names, no source code, no raw logs — describe in product terms. Write a "
    "concise, specific title and a markdown body with sections: '## What the user "
    "was doing', '## What happened', '## Why it matters', and '## OpenFDE context' "
    "(view / version / episode). Return ONLY JSON {\"title\": str, \"body\": str}.")


def deterministic_general_report(description: str, kind: str, context: dict | None) -> tuple:
    """The template fallback (no provider / bad JSON). Interpolates only the user's
    words and the curated context; the caller scrubs the result. Returns (title,
    body) WITHOUT the marker (``draft_general`` adds it)."""
    k = (kind or "other").lower()
    label = _KIND_TITLE.get(k, "Feedback")
    desc = (description or "").strip()
    first = desc.splitlines()[0] if desc else ""
    title = f"{label}: {(first[:80] or 'user feedback').rstrip('. ')}"
    ctx = context or {}
    lines = [
        f"## OpenFDE {label.lower()} report",
        "",
        "## What the user described",
        "",
        desc or "_(no description provided)_",
        "",
        "## OpenFDE context",
        f"- View: {ctx.get('view') or 'unknown'}",
        f"- OpenFDE commit: {ctx.get('openfdeVersion') or 'unknown'}",
    ]
    ep = ctx.get("episode")
    if isinstance(ep, dict) and (ep.get("title") or ep.get("status")):
        lines.append(f"- Active episode: {ep.get('title') or '?'} ({ep.get('status') or '?'})")
    ev = ctx.get("recentEvents")
    if isinstance(ev, list) and ev:
        lines.append(f"- Recent events: {', '.join(str(e) for e in ev[:6])}")
    lines += ["",
              "_Drafted from the user's description and light OpenFDE context. No "
              "watched-repo source, paths, or logs are included._"]
    return title, "\n".join(lines)


def draft_general(description: str, kind: str, context: dict | None,
                  repo_name: str = "", caller=None) -> dict:
    """Draft one general-feedback issue. With ``caller`` (the Architect text role),
    the model writes it and the output is scrubbed; without one (or on any failure /
    bad JSON), the deterministic template is used. The body is always marker-prefixed
    and scrubbed. Pure — never posts to GitHub.

    Returns:
        dict — {title, body, source}.
    """
    repls = general_replacements(context, repo_name)
    if caller:
        try:
            user = json.dumps({"description": description, "kind": kind,
                               "app_context": context or {}}, ensure_ascii=False)
            raw = caller(_ARCHITECT_SYS, user)
            m = re.search(r"\{.*\}", raw or "", re.S)
            data = json.loads(m.group(0)) if m else {}
            title = (data.get("title") or "").strip()
            text = (data.get("body") or "").strip()
            if title and text:
                return {"title": scrub_general(title, repls)[:200],
                        "body": GENERAL_MARKER + "\n" + scrub_general(text, repls),
                        "source": "Architect · drafted from OpenFDE context"}
        except Exception as exc:  # noqa: BLE001 — fall through to the template
            logger.warning("general feedback draft failed: %s", exc)
    title, text = deterministic_general_report(description, kind, context)
    return {"title": scrub_general(title, repls)[:200],
            "body": GENERAL_MARKER + "\n" + scrub_general(text, repls),
            "source": "OpenFDE · template"}


# ── labels ───────────────────────────────────────────────────────────────────── #

def select_labels(kind: str, hint, available, picks=None) -> list:
    """Deterministic label set for a general-feedback issue: always ``auto-report``,
    then the kind's own label, caller-supplied hints, and any classifier ``picks`` —
    but only the ones that ALREADY EXIST (seeded/existing labels win; nothing is
    invented here — minting one new label is a separate, explicit step). Falls back
    to ``bug`` when nothing else matched. Capped at 5."""
    chosen = ["auto-report"]
    avail = set(available or [])
    for label in [KIND_LABEL.get((kind or "").lower())] + list(hint or []) + list(picks or []):
        if label and label in avail and label not in chosen:
            chosen.append(label)
    if len(chosen) == 1:
        chosen.append("bug" if "bug" in avail else next(iter(sorted(avail)), "bug"))
    return chosen[:5]
