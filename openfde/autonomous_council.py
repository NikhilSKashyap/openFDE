"""
openfde/autonomous_council.py — the OpenFDE-managed autonomous council relay.

ONE user prompt → the whole Codex(architect + verifier) + Claude-Code(senior dev) relay, with NO
human copy-paste:

    user prompt
      → architect (Codex) proposes a plan
      → senior dev (Claude Code) reviews it honestly (consultation)
      → architect (Codex) makes the final implementation decision
      → senior dev (Claude Code) implements (real file edits) — the RELAY commits
      → verifier (Codex) verifies the commit
      → pass: READY_TO_PUSH (push gated by config) · fail: CHANGES_REQUESTED → loop
      → loop until VERIFIED or BLOCKED

Provenance (general, not hardcoded): a PRODUCT run owns exactly ONE parent episode; every council
turn (consultation, decision, implementation, verification, push) is a CHILD turn/receipt of that
episode — never a new rail beat. OpenPM gets five phase cards under the parent's tag, moved as the
relay advances; a real implementation commit is attached to the episode. A SMOKE run (``product=
False``) writes only debug records (run.json + session logs) — no episode, no OpenPM cards, no Orient
inbox pollution. Existing OpenFDE ids are reused; the ``runId`` is the run's own.

Sessions are driven through :mod:`openfde.agent_sessions`: real ``codex``/``claude-code`` CLIs when
available (honest, precise block reason otherwise), or the deterministic ``echo`` adapter that backs
the tests. No human in the middle unless a protected boundary, credentials, the retry budget, or
genuine ambiguity demands it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone

from openfde import agent_sessions, council_bus, external_council, run_control

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
STATUS_BLOCKED_PROVIDER_TIMEOUT = "blocked_provider_timeout"
STATUS_BLOCKED_PROVIDER_ERROR = "blocked_provider_error"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = (STATUS_VERIFIED, STATUS_READY_TO_PUSH, STATUS_BLOCKED_NEEDS_HUMAN,
                     STATUS_BLOCKED_ADAPTER_UNAVAILABLE, STATUS_BLOCKED_PROVIDER_TIMEOUT,
                     STATUS_BLOCKED_PROVIDER_ERROR, STATUS_CANCELLED)

# ── Story loop edges (the data model preserves loop structure, not a flat line) ─
EDGE_PROPOSED = "proposed"
EDGE_CONSULTED = "consulted"
EDGE_DECIDED = "decided"
EDGE_IMPLEMENTED = "implemented"
EDGE_VERIFIED = "verified"
EDGE_CHANGES_REQUESTED = "changes_requested"
EDGE_FIXED = "fixed"
EDGE_BLOCKED = "blocked"

# ── OpenPM phase cards (under the parent episode's tag) ────────────────────────
_PHASE_TASKS = [
    ("plan", "Architect plan"),
    ("consult", "Senior dev consultation"),
    ("implement", "Implementation"),
    ("verify", "Verification"),
    ("push", "Push / blocked"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")


# ── Role prompts (framing for the REAL agents; echo keys on phase and ignores them) ──
def _plan_prompt(prompt: str) -> str:
    return ("You are the ARCHITECT on an autonomous engineering council. Propose a concise "
            "implementation plan for the task below: a short list of modules and concrete tasks "
            "(one per line as `- task: <title>`), then a one-line acceptance criterion. Plan only — "
            f"do not write code.\n\nTASK:\n{prompt}")


def _consult_prompt(plan: str) -> str:
    return ("You are the SENIOR DEV consultant reviewing the architect's plan BEFORE implementation. "
            "Give honest, brief feedback: what to keep small, risks, what is missing, what to add. "
            f"Do not implement.\n\nPLAN:\n{plan}")


def _decide_prompt(consult: str, prompt: str) -> str:
    return ("You are the ARCHITECT. Given the senior dev's feedback, make the FINAL implementation "
            "decision: a concise, ordered description of the minimal change to make now. Scope stays "
            f"within the task.\n\nTASK:\n{prompt}\n\nSENIOR DEV FEEDBACK:\n{consult}")


def _implement_prompt(prompt: str, plan: str, consult: str, decision: str) -> str:
    # Full context so implementation can proceed WITHOUT a second architect round-trip: the original
    # task, the architect's plan, the senior dev's consultation, and the decision/constraints.
    return ("You are the SENIOR DEV. Implement the decision now by editing files in this repository. "
            "Make the minimal real change. Do NOT run git and do NOT push — the runtime commits for "
            "you. When done, summarize what you changed in one line starting with `IMPLEMENTED:`.\n\n"
            f"TASK:\n{prompt}\n\n"
            f"ARCHITECT PLAN:\n{plan}\n\n"
            f"SENIOR DEV CONSULTATION:\n{consult}\n\n"
            f"DECISION / CONSTRAINTS:\n{decision}")


def _verify_prompt(sha: str, prompt: str) -> str:
    return ("You are the INDEPENDENT VERIFIER. A commit was just made on this repository: "
            f"{sha or '(no file changes)'}. Inspect it (you may read files and run `git show {sha}`) "
            "and decide whether it satisfies the task. Respond starting with EXACTLY one token — "
            "`VERIFIED` or `CHANGES_REQUESTED` — then a one-line reason.\n\n"
            f"TASK:\n{prompt}")


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
    # Terminal is STICKY: never let an in-flight relay turn (status=running, saved from the worker
    # thread) overwrite a run that was already terminated externally — e.g. cancel_run marked it
    # cancelled while the relay was mid-phase. The UI reads this file, so it must not flicker back to
    # "running" after a cancel; the worker converges on the terminal status at its next cancel guard.
    if rec.get("status") == STATUS_RUNNING:
        prior = load_run(repo_root, rec["runId"])
        if prior and prior.get("status") in TERMINAL_STATUSES:
            rec = {**rec, "status": prior["status"], "phase": prior.get("phase", rec.get("phase")),
                   "activeRole": prior.get("activeRole"),
                   "blockedReason": prior.get("blockedReason"), "timeoutInfo": prior.get("timeoutInfo")}
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


def latest_run(repo_root, *, product_only=True) -> dict | None:
    runs = [r for r in list_runs(repo_root) if (r.get("product", True) or not product_only)]
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
        "product": rec.get("product", True), "providers": rec.get("providers") or {},
        "providerIds": rec.get("providerIds") or {}, "timeoutInfo": rec.get("timeoutInfo"),
        "errorInfo": rec.get("errorInfo"),
        "decisionMode": rec.get("decisionMode"), "running": rec.get("status") == STATUS_RUNNING,
    }


def latest_run_summary(repo_root) -> dict | None:
    return run_summary(latest_run(repo_root, product_only=True))


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
    if not episode_id:
        return
    ep = persistence.get_episode(episode_id)
    if ep:
        ep["status"] = status
        ep["updatedAt"] = _now()
        persistence.upsert_episode(ep)


def _ensure_phase_tasks(persistence, rec):
    """Create any missing council phase cards under the parent episode, each tagged with a
    ``phaseKey`` so the relay can move just that card as the phase advances. Dedups by phaseKey."""
    repo_root = persistence.openfde_dir.parent
    tasks = persistence.load_tasks()
    ep = persistence.get_episode(rec["episodeId"]) or {}
    have = {t.get("phaseKey") for t in tasks if isinstance(t, dict) and t.get("episodeId") == rec["episodeId"]}
    added = []
    for key, title in _PHASE_TASKS:
        if key in have:
            continue
        tid = "task_" + secrets.token_hex(5)
        tasks.append({
            "id": tid, "title": title, "description": "", "column": "todo",
            "verificationStatus": "pending", "source": external_council.EXTERNAL_COUNCIL_SOURCE,
            "episodeId": rec["episodeId"], "phaseKey": key, "linkedBoxIds": list(rec["boxIds"]),
            "files": [], "commitSha": None, "episodeTag": ep.get("tag", ""),
            "promptTitle": ep.get("title", ""), "promptLabel": ep.get("title", ""),
        })
        added.append(tid)
    if added:
        persistence.save_tasks(tasks)
        rec["taskIds"] = list(rec["taskIds"]) + added
        _patch_work_item_header(repo_root, rec["episodeId"], {"taskIds": rec["taskIds"]})


def _phase_states_from_council(council: dict):
    """Map a parent episode's council summary (edges + status) to per-phase (column, verification)."""
    edges = set(council.get("edges") or [])
    status = council.get("status")
    blocked = str(status or "").startswith("blocked")
    states = {
        "plan": ("done", "passed") if "proposed" in edges else ("todo", "pending"),
        "consult": ("done", "passed") if "consulted" in edges else ("todo", "pending"),
        "implement": ("done", "passed") if ({"implemented", "fixed"} & edges) else ("todo", "pending"),
        "verify": ("done", "passed") if "verified" in edges
                  else (("doing", "failed") if "changes_requested" in edges else ("todo", "pending")),
        "push": ("done", "passed") if status in (STATUS_VERIFIED, STATUS_READY_TO_PUSH)
                else (("doing", "failed") if blocked else ("todo", "pending")),
    }
    return states, council.get("latestCommit")


def _council_from_run(persistence, episode):
    """Reconstruct a council summary from the episode's run record (so a parent whose ``council`` field
    predates the summary still hydrates) — None when no usable run exists."""
    repo_root = persistence.openfde_dir.parent
    for rid in reversed(episode.get("runIds") or []):
        rec = load_run(repo_root, rid)
        if rec and rec.get("episodeId") == episode.get("episodeId") and rec.get("storyEvents"):
            return {"runId": rid, "phase": rec.get("phase"), "status": rec.get("status"),
                    "loop": rec.get("loop", 0), "maxLoops": rec.get("maxLoops", 0),
                    "latestCommit": rec.get("latestCommit"), "blockedReason": rec.get("blockedReason"),
                    "providers": rec.get("providers") or {},
                    "edges": [ev["edge"] for ev in rec.get("storyEvents", [])],
                    "turns": (rec.get("turns") or [])[-14:], "updatedAt": rec.get("updatedAt")}
    return None


def hydrate_phase_cards(persistence) -> int:
    """Backfill the five OpenPM phase cards for any PRODUCT parent episode that has autonomous-run
    council data (on the episode, or reconstructable from its run record) but is missing them — so
    existing parents pick up phase cards on load. Card state reflects the council's edges/status; the
    implementation card carries the commit. Skips internal/demoted episodes. Idempotent."""
    eps = persistence.load_episodes()
    tasks = persistence.load_tasks()
    have = {(t.get("episodeId"), t.get("phaseKey")) for t in tasks
            if isinstance(t, dict) and t.get("phaseKey")}
    added, backfilled = 0, []
    for e in eps:
        if e.get("internal"):
            continue
        council = e.get("council")
        if not council and e.get("runIds"):
            council = _council_from_run(persistence, e)
            if council:
                e["council"] = council                    # backfill onto the episode (drawer + reuse)
                backfilled.append(e)
        if not council:
            continue
        eid = e["episodeId"]
        if all((eid, key) in have for key, _t in _PHASE_TASKS):
            continue
        states, commit = _phase_states_from_council(council)
        for key, title in _PHASE_TASKS:
            if (eid, key) in have:
                continue
            col, ver = states[key]
            tasks.append({
                "id": "task_" + secrets.token_hex(5), "title": title, "description": "", "column": col,
                "verificationStatus": ver, "source": external_council.EXTERNAL_COUNCIL_SOURCE,
                "episodeId": eid, "phaseKey": key, "linkedBoxIds": list(e.get("boxIds") or []),
                "files": [], "commitSha": (commit if key == "implement" else None),
                "episodeTag": e.get("tag", ""), "promptTitle": e.get("title", ""), "promptLabel": e.get("title", ""),
            })
            added += 1
    for e in backfilled:
        persistence.upsert_episode(e)
    if added:
        persistence.save_tasks(tasks)
    return added


def _set_phase_task(persistence, rec, phase_key, column, verification=None, commit=None):
    if not (rec.get("product") and rec.get("episodeId")):
        return
    tasks = persistence.load_tasks()
    changed = False
    for t in tasks:
        if isinstance(t, dict) and t.get("episodeId") == rec["episodeId"] and t.get("phaseKey") == phase_key:
            t["column"] = column
            if verification:
                t["verificationStatus"] = verification
            if commit:
                t["commitSha"] = commit
            changed = True
    if changed:
        persistence.save_tasks(tasks)


def _attach_commit_to_episode(persistence, episode_id, sha):
    """Record the implementation commit on the EPISODE — the source of truth OpenPM repairs cards
    from. Echo emits a synthetic sha; the real path emits the actual commit."""
    if not (sha and episode_id):
        return
    ep = persistence.get_episode(episode_id)
    if ep:
        shas = list(ep.get("commitShas") or [])
        if sha not in shas:
            shas.append(sha)
            ep["commitShas"] = shas
            ep["updatedAt"] = _now()
            persistence.upsert_episode(ep)


def _update_episode_council(persistence, rec):
    """Mirror a compact council summary onto the parent episode so the drawer can show the loop:
    transcript summary, commits, verification result, blocked reason. Product runs only."""
    if not (rec.get("product") and rec.get("episodeId")):
        return
    ep = persistence.get_episode(rec["episodeId"])
    if not ep:
        return
    ep["council"] = {
        "runId": rec["runId"], "phase": rec["phase"], "status": rec["status"],
        "activeRole": rec.get("activeRole"), "loop": rec.get("loop", 0), "maxLoops": rec.get("maxLoops", 0),
        "latestCommit": rec.get("latestCommit"), "blockedReason": rec.get("blockedReason"),
        "providers": rec.get("providers") or {}, "edges": [e["edge"] for e in rec.get("storyEvents", [])],
        "turns": (rec.get("turns") or [])[-14:], "updatedAt": _now(),
    }
    persistence.upsert_episode(ep)


def _porcelain(repo_root) -> dict:
    """{path: status} from `git status --porcelain` — used to scope the relay's commit to exactly the
    files the senior dev touched (never sweeping in unrelated pre-existing changes)."""
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=30)
        out = {}
        for ln in (r.stdout or "").splitlines():
            if len(ln) > 3:
                out[ln[3:].strip().strip('"')] = ln[:2]
        return out
    except (OSError, subprocess.SubprocessError):
        return {}


def _commit_implementation(repo_root, rec, before_status: dict) -> str:
    """Commit ONLY the files the senior dev changed this turn (diffed against ``before_status``),
    stamped with OpenFDE trailers. Returns the real sha, or '' when nothing was produced. The relay
    commits (the law: Claude Code's edits, but a deterministic, trailer-stamped commit)."""
    after = _porcelain(repo_root)
    touched = [p for p, st in after.items() if before_status.get(p) != st]
    if not touched:
        return ""
    try:
        add = subprocess.run(["git", "-C", str(repo_root), "add", "--"] + touched,
                            capture_output=True, text=True, timeout=60)
        if add.returncode != 0:
            return ""
        staged = subprocess.run(["git", "-C", str(repo_root), "diff", "--cached", "--quiet"])
        if staged.returncode == 0:        # nothing actually staged
            return ""
        title = (rec.get("title") or _first_line(rec["prompt"]) or "autonomous council change")[:72]
        msg = (f"{title}\n\n"
               f"OpenFDE-Episode: {rec['episodeId']}\n"
               f"OpenFDE-Run: {rec['runId']}\n"
               f"OpenFDE-Role: senior_dev\n"
               f"OpenFDE-Handoff: ready_for_codex_verification\n\n"
               "Co-Authored-By: Claude Code (autonomous council) <noreply@anthropic.com>")
        commit = subprocess.run(["git", "-C", str(repo_root), "commit", "-m", msg],
                              capture_output=True, text=True, timeout=60)
        if commit.returncode != 0:
            return ""
        sha = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return sha.stdout.strip() if sha.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _parse_impl(text: str):
    """Extract (commit_sha, checks) from an echo implementer reply — the echo contract."""
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


# ── Consultation classifier — is a second (architect) decision call actually needed? ──
CLEAR_TO_IMPLEMENT = "clear_to_implement"
NEEDS_ARCHITECT_DECISION = "needs_architect_decision"

# Phrases in a senior-dev consultation that mean "don't just proceed — the architect/user must weigh
# in": a hard stop, a security/permission concern, a scope or architecture conflict, an explicit
# "architect/user must decide". Soft notes ("add a test", "watch X", "keep scope small") are NOT here.
_ESCALATE_PAT = re.compile(
    r"do not (?:proceed|implement)|don't (?:proceed|implement)|must not proceed|"
    r"needs? (?:an? )?(?:architect|user|human)\b|(?:architect|user|human) (?:must|should|needs? to|has to) decide|"
    r"requires? (?:an? )?(?:architect|user|human) (?:review|decision|sign|approval|input)|"
    r"(?:security|permission) (?:concern|boundary|risk|issue|problem|violation|vulnerabilit\w*)|"
    r"scope is wrong|wrong scope|out of scope|scope (?:disagreement|conflict|creep)|"
    r"conflicting approach|architectural (?:conflict|disagreement|concern|mismatch)|"
    r"major (?:risk|blocker|concern|problem|rewrite|redesign)|"
    r"\bblocked?\b|\bblocker\b|\bhalt\b|\bescalat\w+|needs? (?:a )?decision|must decide|"
    r"needs? clarification|cannot proceed|can't proceed",
    re.I)
# A negation right before an escalate phrase neutralises it ("no security concern", "not a blocker").
_NEGATION_PAT = re.compile(r"(?:\bno\b|\bnot\b|n't|\bwithout\b|\bnon-?\b|\bzero\b)\W+\w*\W*$", re.I)


def classify_consultation(text) -> str:
    """Deterministic: does a senior-dev consultation clear implementation, or need the architect/user
    to decide? DEFAULTS to ``clear_to_implement`` (the consult already happened) and escalates only on a
    real blocker. Conservative but not paranoid — soft notes never escalate; an obvious negation in
    front of a blocker word ("no security concern") is not treated as a blocker. No LLM."""
    t = " ".join(str(text or "").split())
    if not t:
        return CLEAR_TO_IMPLEMENT
    for m in _ESCALATE_PAT.finditer(t):
        if _NEGATION_PAT.search(t[max(0, m.start() - 18):m.start()]):
            continue                       # negated ("no …", "not a …") → not a real blocker
        return NEEDS_ARCHITECT_DECISION
    return CLEAR_TO_IMPLEMENT


# The deterministic decision recorded when the senior dev raises no blocking pushback. Honest: it names
# itself as automatic and states the architect provider was NOT called (no fake provider attribution).
_AUTO_DECISION = (
    "Decision: proceed with the architect plan, incorporating the Senior Dev's notes. Keep the change "
    "minimal and in scope; address the Senior Dev's notes inline; the runtime makes the commit.\n"
    "(Automatic decision recorded by OpenFDE — the Senior Dev raised no blocking pushback, so the "
    "architect provider was not called for a second decision.)")


# ── Generic role/provider labels — NO hardcoded Codex=Architect / Claude=Senior Dev ──
# The transcript/banner label for a role is derived from the ASSIGNED provider, so "architect (Codex)"
# only renders when the architect provider is codex; swap providers and the labels follow.
_PROVIDER_DISPLAY = {"codex": "Codex", "claude-code": "Claude Code", "echo": "echo"}
_ROLE_DISPLAY = {"architect": "architect", "sr_dev": "sr dev", "verifier": "verifier"}


def _role_label(role: str, provider: str) -> str:
    rd = _ROLE_DISPLAY.get(role, role)
    pd = _PROVIDER_DISPLAY.get((provider or "").lower(), provider or "")
    return f"{rd} ({pd})" if pd else rd


def _labels_for(providers: dict) -> dict:
    """The display label per council role for a providers map (architect/srDev/verifier)."""
    p = providers or {}
    return {"architect": _role_label("architect", p.get("architect")),
            "sr_dev": _role_label("sr_dev", p.get("srDev") or p.get("sr_dev")),
            "verifier": _role_label("verifier", p.get("verifier"))}


# Human label for an EXACT provider id (claude-code-local stays distinct from the claude-code adapter).
_PROVIDER_ID_LABEL = {
    "claude-code-local": "Claude Code local", "claude-code": "Claude Code",
    "codex-local": "Codex local", "codex": "Codex", "echo": "Echo",
}


def _provider_id_label(provider_id: str) -> str:
    pid = (provider_id or "").strip()
    if pid in _PROVIDER_ID_LABEL:
        return _PROVIDER_ID_LABEL[pid]
    if not pid:
        return "the selected provider"
    words = [w if w in ("local", "cli") else w.capitalize() for w in pid.replace("_", "-").split("-")]
    return " ".join(words)


def _role_title(role: str) -> str:
    return {"architect": "Architect", "sr_dev": "Senior Dev", "verifier": "Verifier"}.get(role, role.title())


def _add_turn(repo_root, rec, role, label, kind, **fields) -> dict:
    """Record a council turn. Always updates the run record (debug); for PRODUCT runs it also appends
    to the durable Orient transcript (a smoke run never pollutes the inbox). Stamps program/slice ids
    so OpenPM + Story can group a program's slices."""
    summary = fields.get("summary", "")
    at = _now()
    compact = {"role": role, "label": label, "kind": kind, "summary": summary, "at": at,
               "latestCommit": fields.get("latestCommit")}
    rec.setdefault("turns", []).append(compact)
    rec["latestTurn"] = {"role": role, "label": label, "kind": kind, "summary": summary, "at": at}
    rec["turnCount"] = rec.get("turnCount", 0) + 1
    if rec.get("product") and rec.get("episodeId"):
        external_council.append_transcript_turn(repo_root, {
            "role": role, "label": label, "kind": kind, "episodeId": rec["episodeId"],
            "taskIds": rec["taskIds"], "runId": rec["runId"], "boxIds": rec["boxIds"],
            "programId": rec.get("programId"), "sliceId": rec.get("sliceId"), "at": at, **fields})
    return compact


def _add_edge(rec, edge, role):
    rec["storyEvents"].append({"edge": edge, "role": role, "loop": rec.get("loop", 0), "at": _now()})


# ── Public API: init → advance → run ──────────────────────────────────────────
def init_run(persistence, *, prompt, box_ids=None, providers=None, provider_ids=None, max_loops=3,
             auto_push=False, allow_edits=False, product=True, parent_episode_id=None, run_id=None,
             program_id=None, slice_id=None, slice_title=None, program_title=None, acceptance=None) -> dict:
    """Create the run record (+ for a PRODUCT run: the parent episode, five OpenPM phase cards, and the
    bus work item) and record the opening user turn. FAST + synchronous — returns the ids the API
    responds with before the relay advances. Reuses OpenFDE ids; the runId is the run's own.

    ``product=False`` is a SMOKE run: no episode, no OpenPM cards, no Orient-inbox turns — only debug
    records. ``parent_episode_id`` attaches to an existing episode instead of minting a new one."""
    repo_root = persistence.openfde_dir.parent
    run_id = run_id or ("run_" + secrets.token_hex(6))
    providers = _norm_providers(providers)
    # The EXACT selected provider ids (e.g. claude-code-local) kept beside the council adapters
    # (claude-code) so debug/status/timeout reasons name the real provider, not just the adapter.
    provider_ids = _norm_providers(provider_ids) if provider_ids else dict(providers)
    box_ids = [b for b in (box_ids or []) if b]
    prompt = (prompt or "").strip()
    ts = _now()

    episode_id, task_ids, title = "", [], ""
    rec = {
        "runId": run_id, "episodeId": "", "taskIds": [], "boxIds": box_ids, "prompt": prompt,
        "title": "", "providers": providers, "providerIds": provider_ids,
        "maxLoops": int(max_loops), "autoPush": bool(auto_push),
        "allowEdits": bool(allow_edits), "product": bool(product), "parentEpisodeId": parent_episode_id,
        "programId": program_id, "sliceId": slice_id, "sliceTitle": slice_title,
        "status": STATUS_RUNNING, "phase": PHASE_ARCHITECT_PLANNING, "activeRole": "architect",
        "loop": 0, "latestCommit": None, "blockedReason": None,
        "storyEvents": [], "turns": [], "latestTurn": None, "turnCount": 0,
        "createdAt": ts, "updatedAt": ts,
    }

    if product:
        if parent_episode_id and persistence.get_episode(parent_episode_id):
            episode_id = parent_episode_id        # attach to the originating episode (no new beat)
            title = (persistence.get_episode(parent_episode_id) or {}).get("title", "")
            rec["episodeId"] = episode_id
            _attach_run_to_episode(persistence, episode_id, run_id)
            _ensure_phase_tasks(persistence, rec)
        else:
            work = external_council.create_external_council_work(
                persistence, objective=prompt, acceptance=[_seed_acceptance(prompt)],
                box_ids=box_ids, task_titles=[t for _t, t in _PHASE_TASKS])
            episode_id, task_ids = work["episodeId"], work["taskIds"]
            title = work.get("title", "")
            rec["episodeId"], rec["taskIds"] = episode_id, list(task_ids)
            for i, (key, _t) in enumerate(_PHASE_TASKS):
                persistence.update_task(task_ids[i], {"phaseKey": key})
            _patch_work_item_header(repo_root, episode_id, {"runId": run_id})
            _attach_run_to_episode(persistence, episode_id, run_id)
        rec["title"] = title
        # Stamp program/slice provenance on the episode + phase cards so OpenPM + Story group a
        # program's slices under their parent (general — null for a plain single run).
        if program_id or slice_id:
            ep = persistence.get_episode(episode_id)
            if ep:
                ep["programId"], ep["sliceId"] = program_id, slice_id
                ep["sliceTitle"], ep["programTitle"] = slice_title, program_title
                persistence.upsert_episode(ep)
            for t in persistence.load_tasks():
                if t.get("episodeId") == episode_id:
                    persistence.update_task(t["id"], {"programId": program_id, "sliceId": slice_id,
                                                      "programTitle": program_title, "sliceTitle": slice_title})

    _add_turn(repo_root, rec, "user", "user", "prompt", summary=prompt[:140], body=prompt)
    _update_episode_council(persistence, rec)
    save_run(repo_root, rec)
    return rec


def _emit(persistence, rec, on_event):
    repo_root = persistence.openfde_dir.parent
    rec["updatedAt"] = _now()
    save_run(repo_root, rec)
    _update_episode_council(persistence, rec)
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
        _set_phase_task(persistence, rec, "push", "doing", "pending")
        _add_turn(repo_root, rec, "system", "system", "push",
                  summary="Auto-push enabled — handed to Claude Code to push.",
                  body="autoPush=true: the senior dev (Claude Code) owns the push; the verifier never pushes.")
    else:
        rec["phase"], rec["status"] = PHASE_READY_TO_PUSH, STATUS_READY_TO_PUSH
        _set_phase_task(persistence, rec, "push", "done", "passed")
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
    _set_phase_task(persistence, rec, "push", "doing", "failed")
    if rec.get("episodeId"):
        external_council.set_work_item_status(repo_root, rec["episodeId"], council_bus.STATUS_BLOCKED_NEEDS_HUMAN)
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
    _set_phase_task(persistence, rec, "push", "doing", "failed")
    if rec.get("episodeId"):
        external_council.set_work_item_status(repo_root, rec["episodeId"], council_bus.STATUS_BLOCKED_NEEDS_HUMAN)
    _emit(persistence, rec, on_event)
    return rec


_ROLE_KEY = {"architect": "architect", "sr_dev": "srDev", "verifier": "verifier"}
_PHASE_TASK = {PHASE_ARCHITECT_PLANNING: "plan", PHASE_SR_DEV_CONSULTING: "consult",
               PHASE_ARCHITECT_DECIDING: "consult", PHASE_SR_DEV_IMPLEMENTING: "implement",
               PHASE_CODEX_VERIFYING: "verify"}
# Plain-language phase name keyed by the metadata phase token (used in blocked reasons / turns).
_PHASE_HUMAN = {"plan": "architect plan", "consult": "senior dev consultation",
                "decide": "architect decision", "implement": "senior dev implementation",
                "verify": "verification"}
# The phase-task column key for each metadata phase token (so a provider error fails the right card).
_PHASE_TASK_BY_TOKEN = {"plan": "plan", "consult": "consult", "decide": "consult",
                        "implement": "implement", "verify": "verify"}


def _provider_id_for_role(rec, role):
    return (rec.get("providerIds") or {}).get(_ROLE_KEY.get(role, role)) or \
        (rec.get("providers") or {}).get(_ROLE_KEY.get(role, role)) or ""


def _guard_cancel(rec):
    """Raise ProviderCancelled if a cancel landed between provider turns (so a run stops promptly even
    when no subprocess is mid-flight)."""
    if run_control.is_cancelled(rec["runId"]):
        role = rec.get("activeRole") or "architect"
        raise run_control.ProviderCancelled(_provider_id_for_role(rec, role), role, rec.get("phase") or "")


def _block_provider_timeout(persistence, rec, exc, on_event):
    """Terminal: a managed provider call exceeded its budget. Records role/provider/phase + a turn that
    reads, in Orient, e.g. 'Architect timed out while using Claude Code local.'"""
    repo_root = persistence.openfde_dir.parent
    role = exc.role or rec.get("activeRole") or "architect"
    phase, seconds = exc.phase or rec.get("phase") or "", exc.seconds
    provider_id = _provider_id_for_role(rec, role)
    label = _provider_id_label(provider_id)
    rec["phase"], rec["status"], rec["activeRole"] = PHASE_BLOCKED, STATUS_BLOCKED_PROVIDER_TIMEOUT, None
    rec["blockedReason"] = f"{_role_title(role)} timed out while using {label} after {seconds}s ({phase})."
    rec["timeoutInfo"] = {"role": role, "providerId": provider_id, "displayLabel": label,
                          "phase": phase, "seconds": seconds}
    _add_turn(repo_root, rec, "system", "system", "blocked",
              summary=f"{_role_title(role)} timed out while using {label}.",
              body=f"The {role} provider ({provider_id or label}) did not return within {seconds}s during "
                   f"{phase}. Blocked (BLOCKED_PROVIDER_TIMEOUT); the managed subprocess was stopped.")
    _add_edge(rec, EDGE_BLOCKED, "system")
    _set_phase_task(persistence, rec, _PHASE_TASK.get(phase, "plan"), "doing", "failed")
    if rec.get("episodeId"):
        external_council.set_work_item_status(repo_root, rec["episodeId"], council_bus.STATUS_BLOCKED_NEEDS_HUMAN)
        _mark_episode(persistence, rec["episodeId"], "blocked")
    _emit(persistence, rec, on_event)
    return rec


def _cancelled_inflight(persistence, rec, exc, on_event):
    """Terminal: the run was cancelled mid-flight. The managed subprocess is already being killed by
    run_control.request_cancel; here we just record the cancelled status + a visible system turn."""
    repo_root = persistence.openfde_dir.parent
    role = getattr(exc, "role", None) or rec.get("activeRole") or "architect"
    phase = getattr(exc, "phase", None) or rec.get("phase") or ""
    provider_id = _provider_id_for_role(rec, role)
    label = _provider_id_label(provider_id)
    rec["phase"], rec["status"], rec["activeRole"] = PHASE_BLOCKED, STATUS_CANCELLED, None
    rec["blockedReason"] = "cancelled by user"
    _add_turn(repo_root, rec, "system", "system", "cancelled",
              summary=f"Run cancelled — {_role_title(role)} stopped while using {label}.",
              body=f"Cancelled by user during {phase}. The managed {role} subprocess ({provider_id or label}) "
                   "was terminated; no slice was left running.")
    _add_edge(rec, EDGE_BLOCKED, "system")
    _set_phase_task(persistence, rec, _PHASE_TASK.get(phase, "plan"), "doing", "failed")
    if rec.get("episodeId"):
        external_council.set_work_item_status(repo_root, rec["episodeId"], council_bus.STATUS_BLOCKED_NEEDS_HUMAN)
        _mark_episode(persistence, rec["episodeId"], "blocked")
    _emit(persistence, rec, on_event)
    return rec


def _require_real_response(rec, role, phase, text):
    """Reject an obvious provider/runtime error returned where a real ``role`` response was expected — a
    transport failure (``API Error: Overloaded``, an empty reply, an auth/rate-limit error) must NEVER
    become plan/consult/decision/implementation/verification content. Returns the text when it's real;
    raises :class:`run_control.ProviderError` otherwise so the run blocks before the next role runs."""
    summary = run_control.classify_provider_error(text)
    if summary:
        raise run_control.ProviderError(_provider_id_for_role(rec, role), role, phase, summary)
    return text


def _block_provider_error(persistence, rec, exc, on_event):
    """Terminal: a provider returned a transport/runtime error instead of a real role response. Reads, in
    Orient, e.g. 'Claude Code local returned a provider error during architect plan: API Error: Overloaded.'
    The error text is recorded as a system BLOCKED turn — never as a proposal/consultation/decision."""
    repo_root = persistence.openfde_dir.parent
    role = exc.role or rec.get("activeRole") or "architect"
    phase = exc.phase or rec.get("phase") or ""
    summary = (exc.summary or "provider error").strip()
    provider_id = _provider_id_for_role(rec, role)
    label = _provider_id_label(provider_id)
    phase_h = _PHASE_HUMAN.get(phase, phase or "the run")
    rec["phase"], rec["status"], rec["activeRole"] = PHASE_BLOCKED, STATUS_BLOCKED_PROVIDER_ERROR, None
    rec["blockedReason"] = f"{label} returned a provider error during {phase_h}: {summary}."
    rec["errorInfo"] = {"role": role, "providerId": provider_id, "displayLabel": label,
                        "phase": phase, "summary": summary}
    _add_turn(repo_root, rec, "system", "system", "blocked",
              summary=f"{label} returned a provider error during {phase_h}: {summary}.",
              body=f"The {role} provider ({provider_id or label}) returned a transport/runtime error "
                   f"instead of a real {phase_h} response, so OpenFDE blocked (BLOCKED_PROVIDER_ERROR) "
                   f"rather than treating it as content. Any managed subprocess was stopped.\n\n"
                   f"Provider returned: {summary}")
    _add_edge(rec, EDGE_BLOCKED, "system")
    _set_phase_task(persistence, rec, _PHASE_TASK_BY_TOKEN.get(phase, "plan"), "doing", "failed")
    if rec.get("episodeId"):
        external_council.set_work_item_status(repo_root, rec["episodeId"], council_bus.STATUS_BLOCKED_NEEDS_HUMAN)
        _mark_episode(persistence, rec["episodeId"], "blocked")
    _emit(persistence, rec, on_event)
    return rec


def advance_run(persistence, rec, *, session_factory=None, on_event=None) -> dict:
    """Drive the relay from ARCHITECT_PLANNING to a terminal status. Synchronous (the caller may run
    it off the event loop). ``session_factory(role, provider, run_dir=...)`` builds managed sessions —
    defaults to the real (codex/claude-code) factory bound to the repo. ``on_event(rec)`` fires after
    each turn for live WS."""
    repo_root = persistence.openfde_dir.parent
    factory = session_factory or agent_sessions.session_factory_for(
        repo_root, allow_edits=rec.get("allowEdits", False), run_id=rec["runId"])
    rdir = run_dir(repo_root, rec["runId"])
    labels = _labels_for(rec["providers"])   # dynamic per assigned provider — never hardcoded

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
        _set_phase_task(persistence, rec, "plan", "doing", "pending")
        _emit(persistence, rec, on_event)
        _guard_cancel(rec)
        plan = _require_real_response(rec, "architect", "plan",
                                      arch.send(_plan_prompt(rec["prompt"]), {"phase": "plan", "runId": rec["runId"]}))
        _add_turn(repo_root, rec, "architect", labels["architect"], "proposal",
                  summary=_first_line(plan), body=plan)
        _add_edge(rec, EDGE_PROPOSED, "architect")
        _set_phase_task(persistence, rec, "plan", "done", "passed")
        _emit(persistence, rec, on_event)

        # 2. Senior dev consults — honest review BEFORE building.
        rec["phase"], rec["activeRole"] = PHASE_SR_DEV_CONSULTING, "sr_dev"
        _set_phase_task(persistence, rec, "consult", "doing", "pending")
        _emit(persistence, rec, on_event)
        _guard_cancel(rec)
        consult = _require_real_response(rec, "sr_dev", "consult",
                                         srdev.send(_consult_prompt(plan), {"phase": "consult", "runId": rec["runId"]}))
        _add_turn(repo_root, rec, "sr_dev", labels["sr_dev"], "consultation",
                  summary=_first_line(consult), body=consult)
        _add_edge(rec, EDGE_CONSULTED, "sr_dev")
        _set_phase_task(persistence, rec, "consult", "done", "passed")
        _emit(persistence, rec, on_event)

        # 3. Decision. The second architect call is CONDITIONAL: if the senior dev's consultation has no
        #    blocking pushback, OpenFDE records a deterministic decision and goes straight to
        #    implementation (no provider call). A real blocker / scope or architecture conflict /
        #    security or permission concern / explicit "architect must decide" routes back to the
        #    architect provider for the final decision.
        if classify_consultation(consult) == CLEAR_TO_IMPLEMENT:
            decision = _AUTO_DECISION
            rec["decisionMode"] = "automatic"          # honest: no architect provider responded
            _add_turn(repo_root, rec, "system", "architect decision (automatic)", "decision",
                      summary="Senior Dev had no blocking pushback; OpenFDE proceeded with the plan.",
                      body=decision, decisionMode="automatic")
            _add_edge(rec, EDGE_DECIDED, "system")      # Story/OpenPM still show the decision beat
            _emit(persistence, rec, on_event)
        else:
            rec["phase"], rec["activeRole"] = PHASE_ARCHITECT_DECIDING, "architect"
            rec["decisionMode"] = "architect"
            _emit(persistence, rec, on_event)
            _guard_cancel(rec)
            decision = _require_real_response(rec, "architect", "decide",
                                              arch.send(_decide_prompt(consult, rec["prompt"]),
                                                        {"phase": "decide", "runId": rec["runId"]}))
            _add_turn(repo_root, rec, "architect", labels["architect"], "decision",
                      summary=_first_line(decision), body=decision)
            _add_edge(rec, EDGE_DECIDED, "architect")
            _emit(persistence, rec, on_event)

        work_input = decision
        while True:
            # 4. Senior dev implements; the RELAY commits the real edits (or echo reports a sha).
            rec["phase"], rec["activeRole"] = PHASE_SR_DEV_IMPLEMENTING, "sr_dev"
            _set_phase_task(persistence, rec, "implement", "doing", "pending")
            _emit(persistence, rec, on_event)
            _guard_cancel(rec)
            before = _porcelain(repo_root)
            # Validate BEFORE any commit logic — a provider error must produce no commit.
            impl = _require_real_response(rec, "sr_dev", "implement",
                                          srdev.send(_implement_prompt(rec["prompt"], plan, consult, work_input),
                                                     {"phase": "implement", "runId": rec["runId"], "loop": rec["loop"]}))
            if srdev.provider == "echo":
                sha, checks = _parse_impl(impl)
            elif getattr(srdev, "allow_edits", False):
                sha = _commit_implementation(repo_root, rec, before)
                checks = "edited + committed by the senior dev" if sha else "no file changes were produced"
            else:
                sha, checks = "", "planned only — real edits gated (enable allowEdits to let the senior dev write files)"
            if sha:
                rec["latestCommit"] = sha
                _attach_commit_to_episode(persistence, rec["episodeId"], sha)
            is_fix = rec["loop"] > 0
            _add_turn(repo_root, rec, "sr_dev", labels["sr_dev"], "implementation",
                      summary=_first_line(impl), body=impl, latestCommit=(sha or None), checks=checks)
            _add_edge(rec, EDGE_FIXED if is_fix else EDGE_IMPLEMENTED, "sr_dev")
            _set_phase_task(persistence, rec, "implement", "done" if sha else "doing",
                            "passed" if sha else "pending", commit=sha or None)
            if rec.get("episodeId"):
                external_council.set_work_item_status(repo_root, rec["episodeId"],
                                                      council_bus.STATUS_READY_FOR_CODEX_VERIFICATION,
                                                      latest_commit=sha or None)
            _emit(persistence, rec, on_event)

            # 5. Verifier verifies the commit.
            rec["phase"], rec["activeRole"] = PHASE_CODEX_VERIFYING, "verifier"
            _set_phase_task(persistence, rec, "verify", "doing", "pending")
            _emit(persistence, rec, on_event)
            _guard_cancel(rec)
            vtext = _require_real_response(rec, "verifier", "verify",
                                           verifier.send(_verify_prompt(sha, rec["prompt"]),
                                                         {"phase": "verify", "commit": sha, "loop": rec["loop"]}))
            rec["loop"] += 1
            if _verdict_from_text(vtext) == council_bus.STATUS_VERIFIED:
                _add_turn(repo_root, rec, "verifier", labels["verifier"], "verified",
                          summary=_first_line(vtext), body=vtext, latestCommit=(sha or None),
                          findings=[_first_line(vtext)] if vtext.strip() else [])
                _add_edge(rec, EDGE_VERIFIED, "verifier")
                _set_phase_task(persistence, rec, "verify", "done", "passed")
                if rec.get("episodeId"):
                    external_council.set_work_item_status(repo_root, rec["episodeId"],
                                                          council_bus.STATUS_VERIFIED, latest_commit=sha or None)
                    _mark_episode(persistence, rec["episodeId"], "landed")
                _finish_verified(persistence, rec, on_event)
                return rec

            # changes requested
            _add_turn(repo_root, rec, "verifier", labels["verifier"], "changes_requested",
                      summary=_first_line(vtext), body=vtext, latestCommit=(sha or None),
                      findings=[_first_line(vtext)] if vtext.strip() else ["changes requested"])
            _add_edge(rec, EDGE_CHANGES_REQUESTED, "verifier")
            _set_phase_task(persistence, rec, "verify", "doing", "failed")
            if rec.get("episodeId"):
                external_council.set_work_item_status(repo_root, rec["episodeId"],
                                                      council_bus.STATUS_CHANGES_REQUESTED, latest_commit=sha or None)
            if rec["loop"] >= rec["maxLoops"]:
                _block_max_loops(persistence, rec, on_event)
                return rec
            rec["phase"] = PHASE_CHANGES_REQUESTED
            _emit(persistence, rec, on_event)
            work_input = vtext       # the next fix attempt works from the change request
    except run_control.ProviderError as exc:               # transport/runtime error as a "response"
        return _block_provider_error(persistence, rec, exc, on_event)
    except run_control.ProviderTimeout as exc:
        return _block_provider_timeout(persistence, rec, exc, on_event)
    except run_control.ProviderCancelled as exc:
        return _cancelled_inflight(persistence, rec, exc, on_event)
    except agent_sessions.AdapterUnavailable as exc:        # a send-path failure (not just start())
        return _block_adapter(persistence, rec, exc, on_event)
    finally:
        for s in sessions.values():
            try:
                s.stop()
            except Exception:  # noqa: BLE001 - session teardown must not mask the run result
                pass


def run(persistence, *, prompt, box_ids=None, providers=None, provider_ids=None, max_loops=3,
        auto_push=False, allow_edits=False, product=True, parent_episode_id=None, run_id=None,
        program_id=None, slice_id=None, slice_title=None, program_title=None, acceptance=None,
        session_factory=None, on_event=None) -> dict:
    """Synchronous convenience: init + advance to terminal. The testable core of the relay."""
    rec = init_run(persistence, prompt=prompt, box_ids=box_ids, providers=providers,
                   provider_ids=provider_ids, max_loops=max_loops, auto_push=auto_push,
                   allow_edits=allow_edits, product=product, parent_episode_id=parent_episode_id,
                   run_id=run_id, program_id=program_id, slice_id=slice_id, slice_title=slice_title,
                   program_title=program_title, acceptance=acceptance)
    return advance_run(persistence, rec, session_factory=session_factory, on_event=on_event)


def cancel_run(repo_root, run_id: str) -> dict | None:
    """Cancel a still-running run: flag + KILL any managed provider subprocess for it (so a hung
    `claude -p` / `codex exec` dies now, not at its timeout), then mark the record cancelled. The
    in-flight relay turn raises ProviderCancelled and converges on the same terminal status."""
    run_control.request_cancel(run_id)        # stop the live subprocess + flag for the poll loop
    rec = load_run(repo_root, run_id)
    if not rec or rec.get("status") in TERMINAL_STATUSES:
        return rec
    rec["status"], rec["phase"], rec["activeRole"] = STATUS_CANCELLED, PHASE_BLOCKED, None
    rec["blockedReason"] = "cancelled by user"
    rec["updatedAt"] = _now()
    return save_run(repo_root, rec)
