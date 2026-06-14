"""
openfde/council_context.py — the generated CouncilContext (read-only brief).

The Council Chat Router answers from ONE shared, generated brief instead of a
human-maintained scratchpad. This module is PURE: it takes already-loaded data
(active/recent episodes, runs, verify evidence, project + log, repo status, and the
derived agent states) and assembles a capped, deterministic context dict plus a
compact text rendering for prompts. Nothing here reads files, calls the network, or
knows about request handlers — the server does the I/O and injects the data.

Two consumers: GET /api/council/context returns build_council_context(...); the ask
orchestrator (council_router.run_ask) renders it into role prompts via render_brief().
"""
from __future__ import annotations

import re

_MAX_RECENT_EP = 5
_MAX_DIRTY = 20
_MAX_DECISIONS = 5
_MAX_JOBS = 5
_STR = 200
_BRIEF_CAP = 3200

# The authoritative current architecture — the single strongest grounding. Without it the role
# models drifted to a deprecated plan (the old Step-13 `/api/execute` + Anthropic-SDK path).
ARCHITECTURE_ANCHOR = (
    "OpenFDE is the cockpit / orange box — the system of record and memory for agent-built "
    "software. Codex and Claude Code (and the council backends) are the EXTERNAL ENGINES that "
    "actually edit and run code; OpenFDE routes work to them and OBSERVES, preserves canvas "
    "boundaries (dotted = agent-editable, solid = protected/approval), captures prompt episodes, "
    "verifies, lands commits, and tells the story. Execute = route/observe an engine (Codex / "
    "Claude Code or a council backend) — NOT a direct Anthropic-SDK call. The old Step-13 "
    "`/api/execute` Anthropic-SDK execution path is HISTORICAL and deprecated; do not recommend it "
    "unless the current ROADMAP explicitly revives it."
)

_PLACEHOLDER = re.compile(r"<[^>]+>")                    # un-filled memory-kit template placeholders / HTML
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")


def _section_lines(md: str, names, cap: int = 6) -> list:
    """Non-placeholder body lines under headings whose text matches any of ``names`` (prefix match),
    accumulated across every matching section — so a concatenated [filled-template, real-doc] yields
    the real lines and silently drops the template's ``<placeholder>`` rows."""
    want = [n.lower() for n in names]
    out, grab = [], False
    for ln in (md or "").splitlines():
        h = _HEADING.match(ln.strip())
        if h:
            head = h.group(1).strip().lower()
            grab = any(head == n or head.startswith(n) for n in want)
            continue
        if not grab:
            continue
        s = ln.strip().lstrip("-*•0123456789. ").strip()
        if s and not _PLACEHOLDER.search(s):
            out.append(s[:160])
            if len(out) >= cap:
                break
    return out


def _recent_headings(md: str, cap: int = 5) -> list:
    """The LAST ``cap`` section headings (+ first detail line) in ``md`` — the newest direction,
    since ROADMAP-style docs are append-only. The caller passes only the TAIL of a long roadmap so
    old/shipped sections near the top never appear."""
    lines = (md or "").splitlines()
    heads = []
    for i, ln in enumerate(lines):
        h = _HEADING.match(ln.strip())
        if not h:
            continue
        title = h.group(1).strip()
        detail = ""
        for nxt in lines[i + 1:i + 6]:
            t = nxt.strip().lstrip("-*•0123456789. ").strip()
            if t and not _HEADING.match(nxt.strip()) and not _PLACEHOLDER.search(t):
                detail = t[:140]
                break
        heads.append(f"{title} — {detail}" if detail else title)
    return heads[-cap:]


def assemble_direction(*, decisions_md: str = "", flow_md: str = "", roadmap_md: str = "") -> list:
    """Ordered CURRENT-DIRECTION lines: the fixed architecture anchor, then Now/Next from DECISIONS,
    the FLOW contract, and the latest roadmap headings. Empty/placeholder parts are skipped."""
    out = [ARCHITECTURE_ANCHOR]
    nn = _section_lines(decisions_md, ["now"]) + _section_lines(decisions_md, ["next"])
    if nn:
        out.append("Now / Next (DECISIONS.md): " + "; ".join(nn[:6]))
    fl = _section_lines(flow_md, ["how work flows", "boundaries", "what this repo is",
                                  "verification"])
    if fl:
        out.append("Flow contract (FLOW.md): " + "; ".join(fl[:5]))
    rm = _recent_headings(roadmap_md)
    if rm:
        out.append("Latest roadmap (most recent): " + " | ".join(rm))
    return out

# Every role has TWO modes: read-only `.chat` (a _text_role call — never tracked as
# a run) and `.work` (the role doing real OpenFDE work: planning, editing, verifying,
# running checks). A run record means some role is mid-`.work`; this maps a run's kind
# to the role doing it. `.work` busy NEVER blocks `.chat` — it only shows in the brief.
_WORK_ROLE = {
    "council_run": "senior_dev", "agent_run": "senior_dev", "hatch": "senior_dev",
    "repair": "senior_dev", "repair_run": "senior_dev",
    "verify_run": "verifier", "verification": "verifier",
    "plan_run": "architect", "assimilate": "architect", "assimilation": "architect",
}
_DEFAULT_WORK_ROLE = "senior_dev"          # unknown editing kinds → the writer


def _s(v, cap: int = _STR) -> str:
    """Coerce to a stripped, length-capped string."""
    if v is None:
        return ""
    return (v if isinstance(v, str) else str(v)).strip()[:cap]


def derive_agent_states(*, available=None, runs=None, active_run_ids=None) -> dict:
    """Per-role liveness for the router + the brief.

    Each role carries ``{available, chatBusy, workBusy}``. ``chatBusy`` is always
    False — read-only chat is never long-busy. ``workBusy`` is derived from run
    records (``status == 'running'`` and no ``endedAt``) plus the in-flight run ids the
    server passes (``set(_RUN_CONTROLS)``), attributed to the role doing that work via
    ``_WORK_ROLE``. The router must NOT treat workBusy as blocking chat — it is
    reported, never gating (a provider that cannot run concurrent text calls is a
    future, provider-level concern — text calls today are independent requests).

    Args:
        available: {role: bool} — whether each role has a working text provider
            (server computes it as ``_text_role(cfg) is not None``). Missing → True.
        runs: list[dict] — persistence.load_runs().
        active_run_ids: iterable[str] — in-flight run ids (_RUN_CONTROLS keys).

    Returns:
        dict — {architect, senior_dev, verifier, runningWorkJobs}.
    """
    avail = available or {}
    active = set(active_run_ids or [])
    jobs, seen = [], set()
    busy = {"architect": False, "senior_dev": False, "verifier": False}

    def add_job(rid, kind, started):
        role = _WORK_ROLE.get(kind, _DEFAULT_WORK_ROLE)
        busy[role] = True
        jobs.append({"runId": _s(rid), "role": role, "kind": _s(kind or "run"),
                     "startedAt": _s(started)})

    for r in (runs or []):
        if not isinstance(r, dict):
            continue
        rid = r.get("runId")
        running = (rid in active) or (r.get("status") == "running" and not r.get("endedAt"))
        if not running or rid in seen:
            continue
        seen.add(rid)
        add_job(rid, r.get("kind"), r.get("startedAt"))
    for rid in active:                      # in-flight, not yet written to runs.json
        if rid not in seen:
            seen.add(rid)
            add_job(rid, "council_run", "")

    def role(name):
        return {"available": bool(avail.get(name, True)), "chatBusy": False,
                "workBusy": busy[name]}

    return {
        "architect": role("architect"),
        "senior_dev": role("senior_dev"),
        "verifier": role("verifier"),
        "runningWorkJobs": jobs[:_MAX_JOBS],
    }


def _episode_view(ep) -> dict | None:
    """Compact, secret-free view of one episode for the brief."""
    if not isinstance(ep, dict):
        return None
    intent = ep.get("intentSource") if isinstance(ep.get("intentSource"), dict) else {}
    verification = ep.get("verification") if isinstance(ep.get("verification"), dict) else {}
    pr = ep.get("pr") if isinstance(ep.get("pr"), dict) else {}
    out = {
        "id": _s(ep.get("episodeId") or ep.get("id")),
        "tag": _s(ep.get("tag")),
        "title": _s(ep.get("title") or ep.get("summary")),
        "status": _s(ep.get("status")),
    }
    if intent.get("kind"):
        out["intent"] = _s(intent.get("kind"))
        if intent.get("ref"):
            out["intentRef"] = _s(intent.get("ref"))
    if verification.get("status"):
        out["verify"] = _s(verification.get("status"))
    if pr.get("url") or pr.get("number"):
        out["pr"] = _s(pr.get("url") or f"#{pr.get('number')}")
    return out


def _decisions(project, project_log, episodes) -> list:
    """Recent decisions/constraints — an APPROXIMATION (no decisions store yet):
    the project description, then the newest ledger entries, then episode summaries."""
    out, seen = [], set()

    def add(text):
        t = _s(text)
        if t and t not in seen and len(out) < _MAX_DECISIONS:
            seen.add(t)
            out.append(t)

    if isinstance(project, dict):
        add(project.get("description"))
    for entry in reversed(project_log or []):          # newest-first
        if len(out) >= _MAX_DECISIONS:
            break
        if isinstance(entry, dict):
            add(entry.get("summary") or entry.get("text") or entry.get("detail")
                or entry.get("prompt"))
    for ep in (episodes or []):
        if len(out) >= _MAX_DECISIONS:
            break
        if isinstance(ep, dict):
            add(ep.get("summary"))
    return out[:_MAX_DECISIONS]


def build_council_context(*, active_episode=None, recent_episodes=None,
                          repo_status=None, verify_latest=None, project=None,
                          project_log=None, agent_states=None,
                          decisions_md="", flow_md="", roadmap_md="", recent_commits=None) -> dict:
    """Assemble the generated CouncilContext from injected store data. Capped + pure.

    ``currentDirection`` is the authoritative grounding (architecture anchor + Now/Next + FLOW +
    latest roadmap) and is rendered FIRST; ``recentCommits`` are the newest landed subjects. The
    server reads the docs/commits (I/O) and injects them here."""
    repo_status = repo_status if isinstance(repo_status, dict) else {}
    verify_latest = verify_latest if isinstance(verify_latest, dict) else {}
    dirty = [p for p in (repo_status.get("dirty") or []) if isinstance(p, str)]
    recent = [v for v in (_episode_view(e) for e in (recent_episodes or [])[:_MAX_RECENT_EP]) if v]
    return {
        "generatedFrom": "openfde-stores",
        "currentDirection": assemble_direction(decisions_md=decisions_md, flow_md=flow_md,
                                               roadmap_md=roadmap_md),
        "recentCommits": [_s(c) for c in (recent_commits or [])[:5] if c],
        "activeEpisode": _episode_view(active_episode),
        "recentEpisodes": recent,
        "repo": {
            "branch": _s(repo_status.get("branch")),
            "head": _s(repo_status.get("shortHead") or repo_status.get("head")),
            "dirtyCount": len(dirty),
            "dirtyFiles": [_s(p) for p in dirty[:_MAX_DIRTY]],
        },
        "verify": ({"status": _s(verify_latest.get("status")),
                    "ranAt": _s(verify_latest.get("ranAt")),
                    "note": _s(verify_latest.get("note"))}
                   if verify_latest.get("status") else None),
        "agents": agent_states or {},
        "recentDecisions": _decisions(project, project_log, recent_episodes),
    }


def render_brief(context) -> str:
    """A compact, deterministic text rendering of the context for a role prompt."""
    if not isinstance(context, dict):
        return ""
    lines = []
    # CURRENT DIRECTION first — authoritative grounding the role must prefer over older activity.
    direction = context.get("currentDirection") or []
    if direction:
        lines.append("CURRENT DIRECTION — authoritative; prefer this over the older activity below:")
        lines += [f"- {d}" for d in direction]
        rc = context.get("recentCommits") or []
        if rc:
            lines.append("Recently landed: " + "; ".join(rc[:5]))
        lines.append("")
        lines.append("Recent activity (supporting only — may include HISTORICAL items that are "
                     "NOT current direction):")
    ae = context.get("activeEpisode")
    if ae:
        bits = [f'Active episode: {ae.get("tag") or ae.get("id") or "?"}']
        if ae.get("title"):
            bits.append(f'"{ae["title"]}"')
        if ae.get("status"):
            bits.append(f'({ae["status"]})')
        if ae.get("verify"):
            bits.append(f'— verify {ae["verify"]}')
        if ae.get("pr"):
            bits.append(f'— PR {ae["pr"]}')
        lines.append(" ".join(bits))
    else:
        lines.append("Active episode: none")
    repo = context.get("repo") or {}
    df = repo.get("dirtyFiles") or []
    lines.append(f'Repo: branch {repo.get("branch") or "?"}, '
                 f'{repo.get("dirtyCount", 0)} dirty file(s)'
                 + (f' ({", ".join(df[:6])})' if df else ''))
    v = context.get("verify")
    if v and v.get("status"):
        lines.append(f'Verify: {v["status"]}'
                     + (f' (ran {v["ranAt"]})' if v.get("ranAt") else ''))
    agents = context.get("agents") or {}
    busy_roles = [n for n in ("architect", "senior_dev", "verifier")
                  if (agents.get(n) or {}).get("workBusy")]
    if busy_roles:
        human = {"architect": "Architect", "senior_dev": "Senior Dev", "verifier": "Verifier"}
        n = len(agents.get("runningWorkJobs") or [])
        lines.append(f'Agents: {", ".join(human[r] for r in busy_roles)} mid-WORK '
                     f'({n} running job(s)) — read-only chat is still available')
    else:
        lines.append("Agents: all idle")
    dec = context.get("recentDecisions") or []
    if dec:
        lines.append("Recent: " + "; ".join(dec[:3]))
    eps = context.get("recentEpisodes") or []
    if eps:
        lines.append("Recent episodes: " + ", ".join(
            f'{e.get("tag") or e.get("id")} "{(e.get("title") or "")[:40]}"' for e in eps[:4]))
    return "\n".join(lines)[:_BRIEF_CAP]
