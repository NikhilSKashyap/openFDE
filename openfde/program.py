"""
openfde/program.py — Autonomous Program Mode v1.

A PROGRAM turns one high-level product direction into a managed sequence of scoped SLICES, each run
through the existing autonomous council loop (:mod:`openfde.autonomous_council`). Intentionally narrow:
ONE active program at a time, at most 3 slices, GENERIC role/provider routing (no hardcoded
Codex=Architect / Claude=Senior Dev), no auto-push.

    Program (parent) ─┬─ Slice 1 → council run → episode/tasks/transcript/commit receipt → verified
                      ├─ Slice 2 → … (auto-starts when the previous slice verifies)
                      └─ Slice 3
    Stops only on: retry budget exhausted, adapter unavailable, blast radius too broad, product
    ambiguity, or a protected-scope approval — never silently.

The planner is deterministic (tests use it / echo, never a real LLM call). The runner reuses the
council loop verbatim, so each slice gets the same episode/OpenPM/Story/commit receipts a single run
gets — grouped under the program via programId/sliceId stamped on the episode + cards + transcript.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone

from openfde import autonomous_council as ac

MAX_SLICES = 3

# ── Program statuses ──────────────────────────────────────────────────────────
STATUS_PLANNING = "planning"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETE = "complete"
STATUS_CANCELLED = "cancelled"
TERMINAL_PROGRAM = (STATUS_BLOCKED, STATUS_COMPLETE, STATUS_CANCELLED)

# ── Slice statuses ────────────────────────────────────────────────────────────
SLICE_QUEUED = "queued"
SLICE_RUNNING = "running"
SLICE_VERIFIED = "verified"
SLICE_FAILED = "failed"
SLICE_BLOCKED = "blocked"

# ── Block reasons (honest, named) ─────────────────────────────────────────────
BLOCKED_NEEDS_PRODUCT_CLARITY = "BLOCKED_NEEDS_PRODUCT_CLARITY"
BLOCKED_BLAST_RADIUS = "BLOCKED_BLAST_RADIUS"
BLOCKED_NO_PROVIDER_FOR_ROLE = "BLOCKED_NO_PROVIDER_FOR_ROLE"
BLOCKED_MAX_RETRIES = "BLOCKED_MAX_RETRIES"
BLOCKED_ADAPTER_UNAVAILABLE = "BLOCKED_ADAPTER_UNAVAILABLE"

# role name (CLI/UI) → providers-map key
_ROLE_KEY = {"architect": "architect", "senior-dev": "srDev", "senior_dev": "srDev",
             "sr_dev": "srDev", "srdev": "srDev", "verifier": "verifier"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")


# ── Persistence — .openfde/programs.json ──────────────────────────────────────
def programs_path(repo_root) -> str:
    return os.path.join(str(repo_root), ".openfde", "programs.json")


def load_programs(repo_root) -> list:
    try:
        with open(programs_path(repo_root), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_programs(repo_root, programs: list) -> None:
    os.makedirs(os.path.join(str(repo_root), ".openfde"), exist_ok=True)
    with open(programs_path(repo_root), "w", encoding="utf-8") as fh:
        json.dump(programs, fh, indent=2)


def get_program(repo_root, program_id) -> dict | None:
    return next((p for p in load_programs(repo_root) if p.get("programId") == program_id), None)


def upsert_program(repo_root, program: dict) -> dict:
    programs = load_programs(repo_root)
    for i, p in enumerate(programs):
        if p.get("programId") == program["programId"]:
            programs[i] = program
            break
    else:
        programs.append(program)
    save_programs(repo_root, programs)
    return program


def active_program(repo_root) -> dict | None:
    """The single non-terminal program (newest first) — v1 allows one active at a time."""
    progs = sorted(load_programs(repo_root), key=lambda p: p.get("createdAt", ""), reverse=True)
    return next((p for p in progs if p.get("status") not in TERMINAL_PROGRAM), None)


def latest_program(repo_root) -> dict | None:
    progs = sorted(load_programs(repo_root), key=lambda p: p.get("createdAt", ""))
    return progs[-1] if progs else None


# ── Planner — decompose a product direction into ≤3 scoped slices ─────────────
_VAGUE = re.compile(r"^\s*(make it better|improve everything|do (some )?stuff|fix it|"
                    r"enhance it|polish it|clean it up|make it nice)\s*\.?\s*$", re.I)
_BLAST = re.compile(r"\b(rewrite (everything|the (whole|entire) (app|codebase|repo))|"
                    r"(the )?(entire|whole) (app|codebase|repo|project)|all (the )?files|"
                    r"everything from scratch|migrate the (whole|entire))\b", re.I)
_SPLIT = re.compile(r"(?:^|\n)\s*(?:\d+[.)]\s+|[-*•]\s+)|;\s+|\.\s+(?=[A-Z])|\bthen\b|\band then\b", re.I)


def _slug_title(text: str) -> str:
    line = _first_line(text) or text.strip()
    line = re.sub(r"^\s*(please\s+|add\s+|implement\s+|build\s+|create\s+|make\s+)", "", line, flags=re.I)
    words = line.split()
    title = " ".join(words[:7]).rstrip(".,:;").strip()
    return (title[:1].upper() + title[1:]) if title else "Slice"


def _risk_note(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("delete", "remove", "migrat", "rename", "schema", "auth", "secret")):
        return "elevated blast radius — touches data/auth/migration; keep the change scoped"
    return "scoped change — limited blast radius"


def _clean_chunk(p: str) -> str:
    p = re.sub(r"^\s*\d+[.)]\s*", "", p)          # drop a leading list marker "1."
    p = re.sub(r"[\s,.;]+\d+[.)]?\s*$", "", p)     # drop an orphan number left by the split boundary
    return p.strip(" -*•\t")


def _split_into_slices(prompt: str, max_slices: int) -> list:
    parts = [_clean_chunk(p) for p in _SPLIT.split(prompt) if p and p.strip(" -*•\t")]
    parts = [p for p in parts if len(p.split()) >= 2]
    if not parts:
        parts = [prompt.strip()]
    if len(parts) <= max_slices:
        return parts
    head, tail = parts[: max_slices - 1], parts[max_slices - 1:]
    return head + ["; ".join(tail)]          # fold the overflow into the last slice (lose nothing)


def _make_slice(text: str) -> dict:
    title = _slug_title(text)
    return {
        "sliceId": "slice_" + secrets.token_hex(4), "title": title, "prompt": text.strip(),
        "acceptance": [f"{title} is implemented and the verifier passes"],
        "blastRadius": _risk_note(text), "status": SLICE_QUEUED, "episodeId": "", "taskIds": [],
        "commits": [], "latestCommit": None, "retryCount": 0, "failureReason": None,
    }


def plan_program(prompt: str, *, max_slices: int = MAX_SLICES):
    """Decompose a high-level prompt into ``(slices, block_reason)`` — at most ``max_slices`` slices,
    each with a concrete title, a scoped implementation prompt, acceptance, and a blast-radius note.
    Returns ``(None, BLOCKED_NEEDS_PRODUCT_CLARITY)`` when too vague, or
    ``(None, BLOCKED_BLAST_RADIUS)`` when the direction is too broad. Deterministic (no LLM)."""
    prompt = (prompt or "").strip()
    words = re.findall(r"\w+", prompt)
    if not prompt or len(words) < 3 or _VAGUE.match(prompt):
        return None, BLOCKED_NEEDS_PRODUCT_CLARITY
    if _BLAST.search(prompt):
        return None, BLOCKED_BLAST_RADIUS
    return [_make_slice(c) for c in _split_into_slices(prompt, max_slices)], None


# ── Provider/role routing check ───────────────────────────────────────────────
def _provider_for_role_ok(providers: dict) -> bool:
    p = providers or {}
    return bool((p.get("architect")) and (p.get("srDev") or p.get("sr_dev")) and p.get("verifier"))


# ── Program lifecycle ─────────────────────────────────────────────────────────
def start_program(persistence, *, prompt, providers, allow_edits=False, max_loops=2,
                  title=None, program_id=None) -> dict:
    """Plan + persist a program (NO slices run yet — the caller advances it off the event loop). One
    active program at a time; a second start while one is active returns that active program untouched."""
    repo_root = persistence.openfde_dir.parent
    existing = active_program(repo_root)
    if existing:
        return existing
    program_id = program_id or ("program_" + secrets.token_hex(6))
    raw_providers = providers or {}          # the caller's explicit assignment — checked before defaults
    ts = _now()
    program = {
        "programId": program_id, "title": title or _slug_title(prompt),
        "prompt": (prompt or "").strip(), "originalPrompt": (prompt or "").strip(),
        "status": STATUS_PLANNING, "currentSliceIndex": 0, "currentSliceId": None, "slices": [],
        "roleAssignments": ac._norm_providers(providers), "allowEdits": bool(allow_edits),
        "maxLoops": int(max_loops),
        "createdAt": ts, "updatedAt": ts, "summary": "", "finalReport": "", "blockerReason": None,
    }
    if not _provider_for_role_ok(raw_providers):     # a role with no assigned provider → honest block
        program["status"], program["blockerReason"] = STATUS_BLOCKED, BLOCKED_NO_PROVIDER_FOR_ROLE
        return upsert_program(repo_root, program)
    slices, block = plan_program(prompt, max_slices=MAX_SLICES)
    if block:
        program["status"], program["blockerReason"] = STATUS_BLOCKED, block
    else:
        program["slices"] = slices
        program["status"] = STATUS_RUNNING
        program["currentSliceId"] = slices[0]["sliceId"]
    return upsert_program(repo_root, program)


def _map_block_reason(run_status: str, run_reason: str) -> str:
    if run_status == ac.STATUS_BLOCKED_ADAPTER_UNAVAILABLE:
        return BLOCKED_ADAPTER_UNAVAILABLE
    if run_status == ac.STATUS_BLOCKED_NEEDS_HUMAN:
        return BLOCKED_MAX_RETRIES
    return run_reason or run_status


def _save(persistence, program, on_event):
    repo_root = persistence.openfde_dir.parent
    program["updatedAt"] = _now()
    upsert_program(repo_root, program)
    if on_event:
        try:
            on_event(program_summary(program))
        except Exception:  # noqa: BLE001 - a misbehaving broadcaster must never break the program
            pass


def advance_program(persistence, program, *, session_factory=None, on_event=None) -> dict:
    """Run the program's queued slices in order through the council loop, auto-advancing on a verified
    slice and stopping the whole program (honest reason) on the first blocked slice. Synchronous."""
    if program.get("status") in TERMINAL_PROGRAM:
        return program
    for sl in program["slices"]:
        if sl["status"] == SLICE_VERIFIED:
            continue
        program["currentSliceIndex"] = program["slices"].index(sl)
        program["currentSliceId"] = sl["sliceId"]
        program["status"] = STATUS_RUNNING
        sl["status"], sl["failureReason"] = SLICE_RUNNING, None
        _save(persistence, program, on_event)

        def _turn(_rec, _sl=sl):                      # mirror the live council turn up to the program
            _sl["episodeId"] = _rec.get("episodeId") or _sl["episodeId"]
            _sl["taskIds"] = _rec.get("taskIds") or _sl["taskIds"]
            _sl["latestCommit"] = _rec.get("latestCommit") or _sl["latestCommit"]
            _save(persistence, program, on_event)

        rec = ac.run(persistence, prompt=sl["prompt"], providers=program["roleAssignments"],
                     allow_edits=program["allowEdits"], max_loops=program["maxLoops"],
                     program_id=program["programId"], slice_id=sl["sliceId"], slice_title=sl["title"],
                     acceptance=sl["acceptance"], session_factory=session_factory, on_event=_turn)

        sl["episodeId"], sl["taskIds"] = rec["episodeId"], rec["taskIds"]
        sl["latestCommit"], sl["retryCount"] = rec["latestCommit"], rec.get("loop", 0)
        ep = persistence.get_episode(rec["episodeId"]) if rec.get("episodeId") else None
        sl["commits"] = (ep.get("commitShas") if ep else None) or ([rec["latestCommit"]] if rec["latestCommit"] else [])
        if ep and ep.get("programTitle") != program["title"]:    # link the slice beat to its program parent
            ep["programTitle"] = program["title"]
            persistence.upsert_episode(ep)
        if rec["status"] in (ac.STATUS_READY_TO_PUSH, ac.STATUS_VERIFIED):
            sl["status"] = SLICE_VERIFIED
            _save(persistence, program, on_event)
            continue                                  # auto-start the next queued slice
        sl["status"], sl["failureReason"] = SLICE_BLOCKED, _map_block_reason(rec["status"], rec.get("blockedReason"))
        program["status"], program["blockerReason"] = STATUS_BLOCKED, sl["failureReason"]
        _save(persistence, program, on_event)
        return program

    program["status"], program["currentSliceId"] = STATUS_COMPLETE, None
    program["finalReport"] = _final_report(program)
    _save(persistence, program, on_event)
    return program


def continue_program(persistence, program_id, *, session_factory=None, on_event=None) -> dict | None:
    """Resume a blocked program: re-queue its blocked slice and advance again."""
    repo_root = persistence.openfde_dir.parent
    program = get_program(repo_root, program_id)
    if not program or program["status"] == STATUS_CANCELLED:
        return program
    for sl in program["slices"]:
        if sl["status"] in (SLICE_BLOCKED, SLICE_FAILED):
            sl["status"], sl["failureReason"] = SLICE_QUEUED, None
    program["status"], program["blockerReason"] = STATUS_RUNNING, None
    upsert_program(repo_root, program)
    return advance_program(persistence, program, session_factory=session_factory, on_event=on_event)


def cancel_program(repo_root, program_id) -> dict | None:
    program = get_program(repo_root, program_id)
    if not program or program["status"] in (STATUS_COMPLETE, STATUS_CANCELLED):
        return program
    program["status"], program["currentSliceId"] = STATUS_CANCELLED, None
    program["updatedAt"] = _now()
    return upsert_program(repo_root, program)


def run(persistence, *, prompt, providers, allow_edits=False, max_loops=2,
        session_factory=None, on_event=None, title=None) -> dict:
    """Synchronous convenience for tests: start + advance to a terminal program state."""
    program = start_program(persistence, prompt=prompt, providers=providers,
                            allow_edits=allow_edits, max_loops=max_loops, title=title)
    if program["status"] != STATUS_RUNNING:
        return program
    return advance_program(persistence, program, session_factory=session_factory, on_event=on_event)


# ── Summaries + status bridge ─────────────────────────────────────────────────
def _final_report(program: dict) -> str:
    done = [s for s in program["slices"] if s["status"] == SLICE_VERIFIED]
    commits = [s["latestCommit"][:7] for s in done if s.get("latestCommit")]
    return (f"{len(done)}/{len(program['slices'])} slices verified"
            + (f"; commits: {', '.join(commits)}" if commits else "; no file changes"))


def slice_summary(sl: dict) -> dict:
    return {k: sl.get(k) for k in ("sliceId", "title", "status", "episodeId", "taskIds", "commits",
                                   "latestCommit", "retryCount", "failureReason", "acceptance", "blastRadius")}


def program_summary(program: dict | None) -> dict | None:
    if not program:
        return None
    slices = program.get("slices", [])
    cur = next((s for s in slices if s["sliceId"] == program.get("currentSliceId")), None)
    done = len([s for s in slices if s["status"] == SLICE_VERIFIED])
    idx = (slices.index(cur) + 1) if cur else done
    return {
        "programId": program.get("programId"), "title": program.get("title"),
        "status": program.get("status"), "blockerReason": program.get("blockerReason"),
        "sliceIndex": idx, "sliceCount": len(slices), "currentSliceIndex": idx,
        "currentSliceId": program.get("currentSliceId"), "currentSliceTitle": cur.get("title") if cur else None,
        "roleAssignments": program.get("roleAssignments", {}),
        "slices": [slice_summary(s) for s in slices],
        "finalReport": program.get("finalReport"), "updatedAt": program.get("updatedAt"),
    }


def latest_program_summary(repo_root) -> dict | None:
    return program_summary(active_program(repo_root) or latest_program(repo_root))


def _next_action(program: dict, cur: dict | None, role_key: str) -> str:
    st = program.get("status")
    if st == STATUS_COMPLETE:
        return "program complete — nothing to do"
    if st == STATUS_CANCELLED:
        return "program cancelled"
    if st == STATUS_BLOCKED:
        return f"blocked ({program.get('blockerReason')}) — resolve, then `openfde program continue`"
    if not cur:
        return "wait — no active slice"
    return (f"this slice ({cur['title']}) is running under your role; read your own session and "
            "act on the current council turn — OpenFDE does not type into your chat")


def program_status(program: dict | None, role: str) -> str:
    """Human-readable status for a role's session — the SESSION BRIDGE (NOT native chat injection).
    Concise + suitable to display directly in a Codex/Claude chat."""
    if not program:
        return "No active program."
    role_key = _ROLE_KEY.get((role or "").strip().lower(), "architect")
    summ = program_summary(program)
    provider = (program.get("roleAssignments") or {}).get(role_key) or "(unassigned)"
    cur = next((s for s in program.get("slices", []) if s["sliceId"] == program.get("currentSliceId")), None)
    lines = [
        f"Program {summ['programId']} — {summ['title']}",
        f"  status: {summ['status']}" + (f" · {summ['blockerReason']}" if summ.get("blockerReason") else ""),
        f"  slice: {summ['sliceIndex']}/{summ['sliceCount']}" + (f" — {summ['currentSliceTitle']}" if summ.get("currentSliceTitle") else ""),
        f"  your role: {role} → {provider}",
        f"  latest commit: {(cur.get('latestCommit') or '—') if cur else '—'}",
        f"  latest handoff: {cur['status'] if cur else (summ['status'])}",
        f"  next action: {_next_action(program, cur, role_key)}",
        "  (session bridge — read-only; OpenFDE never injects into your chat)",
    ]
    return "\n".join(lines)
