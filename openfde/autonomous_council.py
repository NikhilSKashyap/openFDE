"""
openfde/autonomous_council.py — the OpenFDE-managed autonomous council relay.

ONE user prompt → the whole Codex(architect + verifier) + Claude-Code(senior dev) relay, with NO
human copy-paste:

    user prompt
      → architect (Codex) proposes a plan
      → senior dev (Claude Code) reviews it honestly (consultation)
      → architect (Codex) makes the final implementation decision
      → senior dev (Claude Code) implements, verifies, commits, hands off
      → verifier (Codex) verifies the commit
      → pass: READY_TO_PUSH (push gated by config) · fail: CHANGES_REQUESTED → loop
      → loop until VERIFIED or BLOCKED

OpenFDE owns the managed sessions (:mod:`openfde.agent_sessions`), records every turn into the durable
council transcript (shown in Orient), drives the SAME episode / OpenPM / bus the manual council uses,
and records Story loop edges. No new council id is invented — the run is keyed on a real ``episodeId``
+ ``taskIds`` (+ ``boxIds``); the ``runId`` is the run's own id.

No human in the middle unless: a protected boundary requires approval, credentials are needed, the
retry budget is exceeded, or the task is too ambiguous to proceed. v1 is proven end-to-end by the
deterministic ``echo`` adapter; real claude-code/codex adapters report ``adapter_unavailable`` and
block honestly until the next slice wires them.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone

from openfde import agent_sessions, council_bus, external_council

# ── Run phases (the relay's internal state machine) ───────────────────────────
PHASE_USER_PROMPT = "USER_PROMPT"
PHASE_ARCHITECT_PLANNING = "ARCHITECT_PLANNING"
PHASE_SR_DEV_CONSULTING = "SR_DEV_CONSULTING"
PHASE_ARCHITECT_DECIDING = "ARCHITECT_DECIDING"
PHASE_SR_DEV_IMPLEMENTING = "SR_DEV_IMPLEMENTING"
PHASE_CODEX_VERIFYING = "CODEX_VERIFYING"
PHASE_CHANGES_REQUESTED = "CHANGES_REQUESTED"
PHASE_VERIFIED = "VERIFIED"
PHASE_READY_TO_PUSH = "READY_TO_PUSH"
PHASE_BLOCKED = "BLOCKED"

# ── Terminal run statuses ─────────────────────────────────────────────────────
STATUS_RUNNING = "running"
STATUS_VERIFIED = "verified"
STATUS_READY_TO_PUSH = "ready_to_push"
STATUS_BLOCKED_NEEDS_HUMAN = "blocked_needs_human"
STATUS_BLOCKED_ADAPTER_UNAVAILABLE = "blocked_adapter_unavailable"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = (STATUS_VERIFIED, STATUS_READY_TO_PUSH, STATUS_BLOCKED_NEEDS_HUMAN,
                     STATUS_BLOCKED_ADAPTER_UNAVAILABLE, STATUS_CANCELLED)

# ── Story loop edges (the data model preserves loop structure, not a flat line) ─
EDGE_PROPOSED = "proposed"
EDGE_CONSULTED = "consulted"
EDGE_DECIDED = "decided"
EDGE_IMPLEMENTED = "implemented"
EDGE_VERIFIED = "verified"
EDGE_CHANGES_REQUESTED = "changes_requested"
EDGE_FIXED = "fixed"
EDGE_BLOCKED = "blocked"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")


# ── Run record persistence — .openfde/council/runs/<runId>/run.json ───────────
def runs_dir(repo_root) -> str:
    return os.path.join(council_bus.ensure_council_dir(repo_root), "runs")


def run_dir(repo_root, run_id: str) -> str:
    d = os.path.join(runs_dir(repo_root), run_id)
    os.makedirs(d, exist_ok=True)
    return d


def _run_path(repo_root, run_id: str) -> str:
    return os.path.join(run_dir(repo_root, run_id), "run.json")


def save_run(repo_root, rec: dict) -> dict:
    with open(_run_path(repo_root, rec["runId"]), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2)
    return rec


def load_run(repo_root, run_id: str) -> dict | None:
    try:
        with open(_run_path(repo_root, run_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_runs(repo_root) -> list:
    base = runs_dir(repo_root)
    out = []
    try:
        for name in os.listdir(base):
            rec = load_run(repo_root, name)
            if rec:
                out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("createdAt", ""))
    return out


def latest_run(repo_root) -> dict | None:
    runs = list_runs(repo_root)
    return runs[-1] if runs else None


def run_summary(rec: dict | None) -> dict | None:
    """A compact view for the live banner / API — who has the baton + what happened last."""
    if not rec:
        return None
    return {
        "runId": rec.get("runId"), "episodeId": rec.get("episodeId"), "taskIds": rec.get("taskIds") or [],
        "status": rec.get("status"), "phase": rec.get("phase"), "activeRole": rec.get("activeRole"),
        "loop": rec.get("loop", 0), "maxLoops": rec.get("maxLoops", 0),
        "latestCommit": rec.get("latestCommit"), "latestTurn": rec.get("latestTurn"),
        "blockedReason": rec.get("blockedReason"), "autoPush": rec.get("autoPush", False),
        "running": rec.get("status") == STATUS_RUNNING,
    }


def latest_run_summary(repo_root) -> dict | None:
    return run_summary(latest_run(repo_root))


# ── Internal helpers ──────────────────────────────────────────────────────────
def _norm_providers(providers) -> dict:
    p = providers or {}
    return {"architect": p.get("architect") or "echo",
            "srDev": p.get("srDev") or p.get("sr_dev") or "echo",
            "verifier": p.get("verifier") or "echo"}


def _seed_acceptance(prompt: str) -> str:
    line = _first_line(prompt)
    return ("Deliver: " + line)[:200] if line else "Deliver the requested change."


def _patch_work_item_header(repo_root, episode_id, patch: dict) -> bool:
    """Merge ``patch`` into a TASKS.md work item header, preserving its body. Bus-only."""
    items = council_bus.parse_work_items(council_bus.read_bus_file(repo_root, "tasks"))
    rebuilt, found = [], False
    for it in items:
        h = it["header"]
        if h.get("episodeId") == episode_id:
            h = {**h, **patch}
            found = True
        rebuilt.append(council_bus.render_front_matter(h, it["body"]).rstrip("\n"))
    if found:
        council_bus.write_bus_file(repo_root, "tasks", "\n\n".join(rebuilt) + "\n")
    return found


def _attach_run_to_episode(persistence, episode_id, run_id):
    ep = persistence.get_episode(episode_id)
    if ep:
        runs = list(ep.get("runIds") or [])
        if run_id not in runs:
            runs.append(run_id)
            ep["runIds"] = runs
            ep["updatedAt"] = _now()
            persistence.upsert_episode(ep)


def _mark_episode(persistence, episode_id, status):
    ep = persistence.get_episode(episode_id)
    if ep:
        ep["status"] = status
        ep["updatedAt"] = _now()
        persistence.upsert_episode(ep)


def _parse_plan_tasks(plan: str) -> list:
    out = []
    for ln in (plan or "").splitlines():
        m = re.match(r"\s*[-*]\s*task:\s*(.+)", ln, re.I)
        if m:
            out.append(m.group(1).strip())
    return out


def _ensure_plan_tasks(persistence, rec, plan):
    """Create OpenPM tasks from the architect's plan (in addition to the seed task), bound to the
    episode + boxes, and keep the bus work item's taskIds in sync. Existing ids preserved."""
    titles = _parse_plan_tasks(plan)
    if not titles:
        return
    repo_root = persistence.openfde_dir.parent
    tasks = persistence.load_tasks()
    ep = persistence.get_episode(rec["episodeId"]) or {}
    have = {t.get("title") for t in tasks if isinstance(t, dict) and t.get("episodeId") == rec["episodeId"]}
    added = []
    for title in titles:
        if title in have:
            continue
        tid = "task_" + secrets.token_hex(5)
        tasks.append({
            "id": tid, "title": title, "description": "", "column": "todo",
            "verificationStatus": "pending", "source": external_council.EXTERNAL_COUNCIL_SOURCE,
            "episodeId": rec["episodeId"], "linkedBoxIds": list(rec["boxIds"]), "files": [], "commitSha": None,
            "episodeTag": ep.get("tag", ""), "promptTitle": ep.get("title", ""), "promptLabel": ep.get("title", ""),
        })
        added.append(tid)
    if added:
        persistence.save_tasks(tasks)
        rec["taskIds"] = list(rec["taskIds"]) + added
        _patch_work_item_header(repo_root, rec["episodeId"], {"taskIds": rec["taskIds"]})


def _attach_commit_to_tasks(persistence, episode_id, sha):
    tasks = persistence.load_tasks()
    changed = False
    for t in tasks:
        if isinstance(t, dict) and t.get("episodeId") == episode_id and t.get("commitSha") != sha:
            t["commitSha"] = sha
            changed = True
    if changed:
        persistence.save_tasks(tasks)


def _attach_commit_to_episode(persistence, episode_id, sha):
    """Record the implementation commit on the EPISODE — the source of truth OpenPM repairs cards
    from (else /api/tasks would null the card's commit back to episode truth). Echo emits a
    synthetic sha; real adapters emit the real one."""
    if not sha:
        return
    ep = persistence.get_episode(episode_id)
    if ep:
        shas = list(ep.get("commitShas") or [])
        if sha not in shas:
            shas.append(sha)
            ep["commitShas"] = shas
            ep["updatedAt"] = _now()
            persistence.upsert_episode(ep)


def _parse_impl(text: str):
    """Extract (commit_sha, checks) from an implementer reply — the contract real adapters follow too."""
    sha, checks = "", ""
    m = re.search(r"commit=(\S+)", text or "")
    if m:
        sha = m.group(1)
    m = re.search(r"checks=(.+)$", text or "", re.M)
    if m:
        checks = m.group(1).strip()
    return sha, checks


def _verdict_from_text(text: str) -> str:
    up = (text or "").upper()
    if "CHANGES_REQUESTED" in up or "CHANGES REQUESTED" in up:
        return council_bus.STATUS_CHANGES_REQUESTED
    return council_bus.STATUS_VERIFIED


def _add_turn(repo_root, rec, role, label, kind, **fields) -> dict:
    turn = {"role": role, "label": label, "kind": kind, "episodeId": rec["episodeId"],
            "taskIds": rec["taskIds"], "runId": rec["runId"], "boxIds": rec["boxIds"], **fields}
    t = external_council.append_transcript_turn(repo_root, turn)
    rec["latestTurn"] = {"role": role, "label": label, "kind": kind,
                         "summary": fields.get("summary", ""), "at": t.get("at", "")}
    rec["turnCount"] = rec.get("turnCount", 0) + 1
    return t


def _add_edge(rec, edge, role):
    rec["storyEvents"].append({"edge": edge, "role": role, "loop": rec.get("loop", 0), "at": _now()})


# ── Public API: init → advance → run ──────────────────────────────────────────
def init_run(persistence, *, prompt, box_ids=None, providers=None, max_loops=3, auto_push=False,
             run_id=None) -> dict:
    """Create the episode + OpenPM seed task + bus work item (READY_FOR_CC) and the run record, and
    record the opening user turn. FAST + synchronous — returns the ids the API responds with before
    the (slow) relay advances. Reuses OpenFDE's own id scheme; never mints a parallel council id."""
    repo_root = persistence.openfde_dir.parent
    run_id = run_id or ("run_" + secrets.token_hex(6))
    providers = _norm_providers(providers)
    box_ids = [b for b in (box_ids or []) if b]
    prompt = (prompt or "").strip()
    ts = _now()

    work = external_council.create_external_council_work(
        persistence, objective=prompt, acceptance=[_seed_acceptance(prompt)], box_ids=box_ids)
    episode_id, task_ids = work["episodeId"], work["taskIds"]

    _patch_work_item_header(repo_root, episode_id, {"runId": run_id})
    _attach_run_to_episode(persistence, episode_id, run_id)

    rec = {
        "runId": run_id, "episodeId": episode_id, "taskIds": list(task_ids), "boxIds": box_ids,
        "prompt": prompt, "providers": providers, "maxLoops": int(max_loops), "autoPush": bool(auto_push),
        "status": STATUS_RUNNING, "phase": PHASE_ARCHITECT_PLANNING, "activeRole": "architect",
        "loop": 0, "latestCommit": None, "blockedReason": None,
        "storyEvents": [], "latestTurn": None, "turnCount": 0,
        "createdAt": ts, "updatedAt": ts,
    }
    _add_turn(repo_root, rec, "user", "user", "prompt", summary=prompt[:140], body=prompt)
    save_run(repo_root, rec)
    return rec


def _emit(persistence, rec, on_event):
    repo_root = persistence.openfde_dir.parent
    rec["updatedAt"] = _now()
    save_run(repo_root, rec)
    if on_event:
        try:
            on_event(dict(rec))
        except Exception:  # noqa: BLE001 - a misbehaving broadcaster must never break the relay
            pass


def _finish_verified(persistence, rec, on_event):
    repo_root = persistence.openfde_dir.parent
    rec["activeRole"] = None
    if rec["autoPush"]:
        rec["phase"], rec["status"] = PHASE_READY_TO_PUSH, STATUS_VERIFIED
        _add_turn(repo_root, rec, "system", "system", "push",
                  summary="Auto-push enabled — handed to Claude Code to push.",
                  body="autoPush=true: the senior dev (Claude Code) owns the push; the verifier never pushes.")
    else:
        rec["phase"], rec["status"] = PHASE_READY_TO_PUSH, STATUS_READY_TO_PUSH
        _add_turn(repo_root, rec, "system", "system", "ready_to_push",
                  summary="Verified — ready to push (autoPush off).",
                  body="Verification passed. Push is gated by config (autoPush=false); nothing was pushed.")
    _emit(persistence, rec, on_event)


def _block_max_loops(persistence, rec, on_event):
    repo_root = persistence.openfde_dir.parent
    rec["phase"], rec["status"] = PHASE_BLOCKED, STATUS_BLOCKED_NEEDS_HUMAN
    rec["blockedReason"] = f"exceeded {rec['maxLoops']} verification loops"
    rec["activeRole"] = None
    _add_turn(repo_root, rec, "system", "system", "blocked",
              summary=f"Blocked — exceeded {rec['maxLoops']} verification loops; needs human.",
              body="Retry budget exhausted without a passing verification — escalated to a human.")
    _add_edge(rec, EDGE_BLOCKED, "system")
    external_council.set_work_item_status(repo_root, rec["episodeId"], council_bus.STATUS_BLOCKED_NEEDS_HUMAN)
    persistence.move_tasks_for_episode(rec["episodeId"], "doing", "failed", from_columns=("doing", "testing"))
    _mark_episode(persistence, rec["episodeId"], "blocked")
    _emit(persistence, rec, on_event)


def _block_adapter(persistence, rec, exc, on_event):
    repo_root = persistence.openfde_dir.parent
    rec["phase"], rec["status"] = PHASE_BLOCKED, STATUS_BLOCKED_ADAPTER_UNAVAILABLE
    rec["blockedReason"] = f"{exc.provider} adapter unavailable for {exc.role}: {exc.reason}"
    rec["activeRole"] = None
    _add_turn(repo_root, rec, "system", "system", "blocked",
              summary=f"Blocked — {exc.provider} adapter unavailable for {exc.role}.", body=exc.reason)
    _add_edge(rec, EDGE_BLOCKED, "system")
    external_council.set_work_item_status(repo_root, rec["episodeId"], council_bus.STATUS_BLOCKED_NEEDS_HUMAN)
    _emit(persistence, rec, on_event)
    return rec


def advance_run(persistence, rec, *, session_factory=None, on_event=None) -> dict:
    """Drive the relay from ARCHITECT_PLANNING to a terminal status. Synchronous (the caller may run
    it off the event loop). ``session_factory(role, provider, run_dir=...)`` builds managed sessions —
    defaults to the honest real/echo factory. ``on_event(rec)`` fires after each turn for live WS."""
    repo_root = persistence.openfde_dir.parent
    factory = session_factory or agent_sessions.default_session_factory
    rdir = run_dir(repo_root, rec["runId"])

    sessions = {}
    try:
        for role_key, role in (("architect", "architect"), ("srDev", "sr_dev"), ("verifier", "verifier")):
            sessions[role] = factory(role, rec["providers"].get(role_key, "echo"), run_dir=rdir)
        for role in ("architect", "sr_dev", "verifier"):
            try:
                sessions[role].start()
            except agent_sessions.AdapterUnavailable as exc:
                return _block_adapter(persistence, rec, exc, on_event)
        arch, srdev, verifier = sessions["architect"], sessions["sr_dev"], sessions["verifier"]

        # 1. Architect proposes a plan.
        rec["phase"], rec["activeRole"] = PHASE_ARCHITECT_PLANNING, "architect"
        _emit(persistence, rec, on_event)
        plan = arch.send(rec["prompt"], {"phase": "plan", "runId": rec["runId"]})
        _add_turn(repo_root, rec, "architect", "architect (Codex)", "proposal",
                  summary=_first_line(plan), body=plan)
        _add_edge(rec, EDGE_PROPOSED, "architect")
        _ensure_plan_tasks(persistence, rec, plan)
        _emit(persistence, rec, on_event)

        # 2. Senior dev consults — honest review BEFORE building.
        rec["phase"], rec["activeRole"] = PHASE_SR_DEV_CONSULTING, "sr_dev"
        _emit(persistence, rec, on_event)
        consult = srdev.send(plan, {"phase": "consult", "runId": rec["runId"]})
        _add_turn(repo_root, rec, "sr_dev", "sr dev (Claude Code)", "consultation",
                  summary=_first_line(consult), body=consult)
        _add_edge(rec, EDGE_CONSULTED, "sr_dev")
        _emit(persistence, rec, on_event)

        # 3. Architect makes the final implementation decision.
        rec["phase"], rec["activeRole"] = PHASE_ARCHITECT_DECIDING, "architect"
        _emit(persistence, rec, on_event)
        decision = arch.send(consult, {"phase": "decide", "runId": rec["runId"]})
        _add_turn(repo_root, rec, "architect", "architect (Codex)", "decision",
                  summary=_first_line(decision), body=decision)
        _add_edge(rec, EDGE_DECIDED, "architect")
        _emit(persistence, rec, on_event)

        work_input = decision
        while True:
            # 4. Senior dev implements + commits + hands off.
            rec["phase"], rec["activeRole"] = PHASE_SR_DEV_IMPLEMENTING, "sr_dev"
            _emit(persistence, rec, on_event)
            persistence.move_tasks_for_episode(rec["episodeId"], "doing", "pending", from_columns=("todo",))
            impl = srdev.send(work_input, {"phase": "implement", "runId": rec["runId"], "loop": rec["loop"]})
            sha, checks = _parse_impl(impl)
            if sha:
                rec["latestCommit"] = sha
                _attach_commit_to_episode(persistence, rec["episodeId"], sha)
                _attach_commit_to_tasks(persistence, rec["episodeId"], sha)
            is_fix = rec["loop"] > 0
            _add_turn(repo_root, rec, "sr_dev", "sr dev (Claude Code)", "implementation",
                      summary=_first_line(impl), body=impl, latestCommit=(sha or None), checks=checks)
            _add_edge(rec, EDGE_FIXED if is_fix else EDGE_IMPLEMENTED, "sr_dev")
            external_council.set_work_item_status(repo_root, rec["episodeId"],
                                                  council_bus.STATUS_READY_FOR_CODEX_VERIFICATION,
                                                  latest_commit=sha or None)
            _emit(persistence, rec, on_event)

            # 5. Verifier verifies the commit.
            rec["phase"], rec["activeRole"] = PHASE_CODEX_VERIFYING, "verifier"
            _emit(persistence, rec, on_event)
            vtext = verifier.send(f"verify commit {sha}",
                                  {"phase": "verify", "commit": sha, "loop": rec["loop"]})
            rec["loop"] += 1
            if _verdict_from_text(vtext) == council_bus.STATUS_VERIFIED:
                _add_turn(repo_root, rec, "verifier", "verifier (Codex)", "verified",
                          summary=_first_line(vtext), body=vtext, latestCommit=(sha or None),
                          findings=[_first_line(vtext)] if vtext.strip() else [])
                _add_edge(rec, EDGE_VERIFIED, "verifier")
                external_council.set_work_item_status(repo_root, rec["episodeId"],
                                                      council_bus.STATUS_VERIFIED, latest_commit=sha or None)
                persistence.move_tasks_for_episode(rec["episodeId"], "done", "passed",
                                                   from_columns=("todo", "doing", "testing"))
                _mark_episode(persistence, rec["episodeId"], "landed")
                _finish_verified(persistence, rec, on_event)
                return rec

            # changes requested
            _add_turn(repo_root, rec, "verifier", "verifier (Codex)", "changes_requested",
                      summary=_first_line(vtext), body=vtext, latestCommit=(sha or None),
                      findings=[_first_line(vtext)] if vtext.strip() else ["changes requested"])
            _add_edge(rec, EDGE_CHANGES_REQUESTED, "verifier")
            external_council.set_work_item_status(repo_root, rec["episodeId"],
                                                  council_bus.STATUS_CHANGES_REQUESTED, latest_commit=sha or None)
            if rec["loop"] >= rec["maxLoops"]:
                _block_max_loops(persistence, rec, on_event)
                return rec
            rec["phase"] = PHASE_CHANGES_REQUESTED
            _emit(persistence, rec, on_event)
            work_input = vtext       # the next fix attempt works from the change request
    finally:
        for s in sessions.values():
            try:
                s.stop()
            except Exception:  # noqa: BLE001 - session teardown must not mask the run result
                pass


def run(persistence, *, prompt, box_ids=None, providers=None, max_loops=3, auto_push=False,
        run_id=None, session_factory=None, on_event=None) -> dict:
    """Synchronous convenience: init + advance to terminal. The testable core of the relay."""
    rec = init_run(persistence, prompt=prompt, box_ids=box_ids, providers=providers,
                   max_loops=max_loops, auto_push=auto_push, run_id=run_id)
    return advance_run(persistence, rec, session_factory=session_factory, on_event=on_event)


def cancel_run(repo_root, run_id: str) -> dict | None:
    """Mark a still-running run cancelled (best-effort; the relay checks terminal status on save)."""
    rec = load_run(repo_root, run_id)
    if not rec or rec.get("status") in TERMINAL_STATUSES:
        return rec
    rec["status"], rec["phase"], rec["activeRole"] = STATUS_CANCELLED, PHASE_BLOCKED, None
    rec["blockedReason"] = "cancelled by user"
    rec["updatedAt"] = _now()
    return save_run(repo_root, rec)
