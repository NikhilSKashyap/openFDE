"""
openfde/memory_kit.py — the repo-local `.openfde/` markdown memory room.

OpenFDE should feel useful even if you start watching late. On `openfde watch`, it
bootstraps a calm set of markdown files under `.openfde/` — **never the repo root, never
the tracked tree** (`.openfde/` is git-excluded), so watching a foreign repo never
dirties it.

  USER-EDITABLE (created once from a template, NEVER overwritten):
    FLOW.md       — this repo's operating contract (what it is, how work flows, boundaries)
    TASTE.md      — taste / UX / coding preferences ("what good feels like")
    DECISIONS.md  — lifecycle ledger: Now / Next / Deferred / Watch / Abandoned

  GENERATED (safe to (re)write every bootstrap):
    COUNCIL.md    — human-readable role ledger rendered from project_log.jsonl
    BRIEF.md      — compact, paste-safe context for agents/council (FLOW + TASTE +
                    DECISIONS-Now + the generated CouncilContext brief)

Pure render functions + a bootstrap that writes only what's missing (templates) and
(re)writes the generated files. Atomic writes; failures are swallowed — memory is a
convenience, never a blocker for the watcher.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("openfde.memory_kit")

USER_FILES = ("FLOW.md", "TASTE.md", "DECISIONS.md")        # created once, never clobbered
GENERATED_FILES = ("COUNCIL.md", "BRIEF.md")                # rewritten each bootstrap
_MAX_LEDGER_PER_ROLE = 20
_MAX_BRIEF_EXCERPT = 1200

# project_log roles → the five COUNCIL buckets (unknown/missing → council, the system voice).
_COUNCIL_ORDER = ("user", "architect", "sr dev", "verifier", "council")
_ROLE_BUCKET = {
    "user": "user", "human": "user", "you": "user",
    "architect": "architect",
    "senior_dev": "sr dev", "sr dev": "sr dev", "sr_dev": "sr dev",
    "senior dev": "sr dev", "dev": "sr dev", "senior": "sr dev",
    "verifier": "verifier",
    "council": "council", "assistant": "council", "agent": "council", "system": "council",
}


# ── Templates (user-editable; written once) ──────────────────────────────────

def flow_template(repo_name: str) -> str:
    return f"""# FLOW — {repo_name}

*Editable by you and your team. OpenFDE reads this for context and will NOT overwrite it.*

## What this repo is
<one or two lines: the product / library and who it serves>

## How work flows
- Intent → scope → change → verify → land.
- <how changes get proposed, reviewed, and merged here>

## Boundaries
- <what agents may edit freely vs. what needs a human's approval>
- <files / dirs that are off-limits>

## Verification
- Checks that must pass before landing: <e.g. unit tests, lint, build>.
- Green verify lands automatically; red verify waits for a human.
"""


def taste_template() -> str:
    return """# TASTE — what "good" feels like here

*Editable by you and your team. Tunes style and taste, never permissions.*

## Code
- Naming: <conventions>
- Structure: <where logic lives, function size, comment density>
- Tests: <style, what to assert, fixtures>

## UI / UX (if applicable)
- <calm, minimal, "easy on the mind"; spacing, color, motion>

## Reviews
- <what you want called out; what you don't sweat>
"""


def decisions_template() -> str:
    return """# DECISIONS

*Editable by you and your team. OpenFDE may append below your entries; it will not
rewrite your sections.*

## Now
<decisions / constraints in force right now>

## Next
<what's queued>

## Deferred
<consciously postponed, with why>

## Watch
<things to keep an eye on>

## Abandoned
<tried and dropped, with why>
"""


# ── Generated renderers (pure) ───────────────────────────────────────────────

def _entry_text(entry: dict) -> str:
    for k in ("text", "summary", "detail", "message", "prompt", "note"):
        v = entry.get(k) if isinstance(entry, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def render_council_md(project_log: list) -> str:
    """The human-readable role ledger, grouped into the five council buckets.

    Generated from project_log.jsonl — do not hand-edit. Newest entries last per role,
    capped. Empty buckets still render their header so the shape is always legible.
    """
    buckets = {b: [] for b in _COUNCIL_ORDER}
    for e in (project_log or []):
        if not isinstance(e, dict):
            continue
        text = _entry_text(e)
        if not text:
            continue
        role = str(e.get("role") or "").strip().lower()
        bucket = _ROLE_BUCKET.get(role, "council")
        ts = (str(e.get("timestamp") or e.get("createdAt") or "")[:10]) or ""
        buckets[bucket].append((ts, text))
    lines = ["# COUNCIL — role ledger", "",
             "*Generated from `.openfde/project_log.jsonl` by OpenFDE. Do not hand-edit — "
             "your changes will be overwritten. Put durable notes in DECISIONS / FLOW / TASTE.*", "",
             "**External council session start:** before working, orient with your inbox —",
             "`openfde council status --role codex` (Architect+Verifier) or "
             "`openfde council status --role claude` (Senior Dev). It shows the current handoff "
             "addressed to you: status, episode/task ids, objective, next action, and (for Claude) "
             "the exact commit trailers to stamp.", ""]
    for b in _COUNCIL_ORDER:
        lines.append(f"## {b}")
        rows = buckets[b][-_MAX_LEDGER_PER_ROLE:]
        if not rows:
            lines.append("_(nothing yet)_")
        for ts, text in rows:
            head = text.splitlines()[0].strip()
            head = head[:200] + ("…" if len(head) > 200 else "")
            lines.append(f"- {('`' + ts + '` ') if ts else ''}{head}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _section(md: str, header: str) -> str:
    """Extract one `## header` section's body from a markdown doc (best-effort)."""
    if not md:
        return ""
    out, capturing = [], False
    for ln in md.splitlines():
        if ln.strip().lower().startswith("## "):
            capturing = ln.strip()[3:].strip().lower() == header.lower()
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out).strip()


def render_brief_md(*, brief_text: str = "", flow_md: str = "", taste_md: str = "",
                    decisions_md: str = "") -> str:
    """The compact, paste-safe agent/council brief: the generated CouncilContext plus
    the durable operating contract (FLOW), taste, and the current DECISIONS → Now."""
    def _clip(s):
        s = (s or "").strip()
        return s if len(s) <= _MAX_BRIEF_EXCERPT else s[:_MAX_BRIEF_EXCERPT].rstrip() + " …"

    flow = _clip(flow_md.split("##", 1)[0].strip() or flow_md) if flow_md else ""
    # FLOW's first real content: skip the title/blurb, prefer the body.
    flow_body = _clip("\n".join(l for l in (flow_md or "").splitlines()
                                if l.strip() and not l.startswith("#") and not l.startswith("*")))
    taste_body = _clip("\n".join(l for l in (taste_md or "").splitlines()
                                 if l.strip() and not l.startswith("#") and not l.startswith("*")))
    now = _clip(_section(decisions_md, "Now"))
    lines = ["# BRIEF — compact context for agents & council", "",
             "*Generated by OpenFDE. Safe to paste into a role prompt.*", ""]
    if brief_text.strip():
        lines += ["## Live state", brief_text.strip(), ""]
    lines += ["## Operating contract (FLOW)",
              flow_body or "_set this in `.openfde/FLOW.md`_", ""]
    lines += ["## Taste", taste_body or "_set this in `.openfde/TASTE.md`_", ""]
    lines += ["## Decisions — Now", now or "_set this in `.openfde/DECISIONS.md`_", ""]
    return "\n".join(lines).rstrip() + "\n"


# ── I/O ──────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, text: str) -> bool:
    try:
        tmp = path.parent / (".~" + path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("memory_kit: could not write %s: %s", path.name, exc)
        return False


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _council_brief_text(persistence, root) -> str:
    """The generated CouncilContext, rendered — reuses council_context (lazy import to
    avoid coupling memory_kit to git/the router). Empty string on any failure."""
    try:
        from openfde import council_context
        from openfde.git_timeline import git_status
        ctx = council_context.build_council_context(
            active_episode=persistence.latest_active_episode(),
            recent_episodes=persistence.load_episodes(),
            repo_status=git_status(Path(root)),
            verify_latest=persistence.load_verify_latest(),
            project=persistence.load_project(),
            project_log=persistence.load_project_log())
        return council_context.render_brief(ctx)
    except Exception:  # noqa: BLE001 — the brief is a convenience, never a blocker
        logger.debug("memory_kit: council brief unavailable", exc_info=True)
        return ""


def regenerate_generated(persistence, root) -> list:
    """(Re)write the GENERATED files (COUNCIL.md, BRIEF.md). Safe to call often."""
    d = Path(persistence.dir)
    written = []
    council = render_council_md(persistence.load_project_log())
    if _atomic_write(d / "COUNCIL.md", council):
        written.append("COUNCIL.md")
    brief = render_brief_md(brief_text=_council_brief_text(persistence, root),
                            flow_md=_read(d / "FLOW.md"), taste_md=_read(d / "TASTE.md"),
                            decisions_md=_read(d / "DECISIONS.md"))
    if _atomic_write(d / "BRIEF.md", brief):
        written.append("BRIEF.md")
    return written


def bootstrap_memory_kit(persistence, root) -> dict:
    """Initialize the `.openfde/` memory kit. Creates the user-editable files from a
    template only when MISSING (never overwrites your edits) and (re)writes the
    generated files. Returns ``{created: [...], regenerated: [...]}``.
    """
    d = Path(persistence.dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"created": [], "regenerated": []}
    repo_name = Path(root).name or "this repo"
    templates = {"FLOW.md": flow_template(repo_name), "TASTE.md": taste_template(),
                 "DECISIONS.md": decisions_template()}
    created = []
    for name in USER_FILES:
        path = d / name
        if not path.exists():                       # never clobber a user's edits
            if _atomic_write(path, templates[name]):
                created.append(name)
    regenerated = regenerate_generated(persistence, root)
    if created or regenerated:
        logger.info("memory_kit: created %s, regenerated %s under .openfde/",
                    created or "none", regenerated or "none")
    return {"created": created, "regenerated": regenerated}
