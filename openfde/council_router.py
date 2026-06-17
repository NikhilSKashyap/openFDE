"""
openfde/council_router.py — the deterministic Council Chat Router (v1) + ask runner.

One OpenFDE chat, routed across the council. PURE and testable:
  • route(question, target, agent_states) decides WHO answers (or that Architect +
    Senior Dev should DISCUSS) from the question + an explicit target + agent states.
  • run_ask(...) drives the answer through INJECTED text callers — the server passes
    the `_text_role` callers, so this module never imports a provider or a runner.

Two hard rules baked in:
  • READ-ONLY. run_ask only ever invokes the injected text callers `caller(system,
    user) -> str`. It has no handle to run_council / run_claude_code / any file-editing
    runner — it CANNOT dispatch a run, by construction.
  • Every role has two modes. `<role>.work` (planning, editing, verifying, running
    checks) may be busy, but that NEVER blocks `<role>.chat` (read-only Q&A) for ANY
    role. workBusy is surfaced in the decision/receipt, never used to reroute (a
    provider that cannot run concurrent text calls is a future, provider-level concern).

Receipt: a single contributing role labels as itself (Architect / Senior Dev /
Verifier); two or more label as "Council", with the contributing roles as secondary
metadata (contributorsLabel, e.g. "Architect · Senior Dev"). Never "A + B" as the label.
"""
from __future__ import annotations

from openfde import council_context

ROLES = ("architect", "senior_dev", "verifier")
TARGETS = ("auto", "architect", "senior_dev", "verifier", "discuss")

_ROLE_HUMAN = {"architect": "Architect", "senior_dev": "Senior Dev", "verifier": "Verifier"}

_PERSONA = {
    "architect": "You own product direction, architecture, roadmap, and tradeoffs.",
    "senior_dev": "You own implementation, debugging, and code paths — but here you "
                  "only DISCUSS, you never edit files.",
    "verifier": "You own tests, verification, readiness, review, and PR evidence.",
}

# Deterministic v1 routing vocabulary (substring match, lowercased).
_ARCH_KW = ("architecture", "architect", "roadmap", "product", "tradeoff", "trade-off",
            "strategy", "design", "approach", "what do you think", "why", "scope",
            "decision", "direction", "priorit", "should we", "high-level")
_SD_KW = ("implement", "implementation", "debug", "code path", "codepath", "code",
          "function", "patch", "failing test", "stack trace", "traceback", "bug",
          "fix", "refactor", "exception", "regress in")
_VER_KW = ("verify", "verification", "readiness", "ready to ship", "pr evidence",
           "regression", "coverage", "review the", "tests pass", "test suite",
           "gate", "ci ", "is it green")
_DISCUSS_KW = ("discuss", "debate", "weigh in", "both of you", "what do you both",
               "you both think", "hash this out", "argue", "get both")


def _count(ql: str, kws) -> int:
    return sum(1 for k in kws if k in ql)


def _wants_discuss(ql: str) -> bool:
    if any(k in ql for k in _DISCUSS_KW):
        return True
    if "architect" in ql and ("senior dev" in ql or "senior-dev" in ql):
        return True
    if "both" in ql and ("role" in ql or "agent" in ql or "team" in ql):
        return True
    return False


def _tie_break(scores: dict) -> str:
    """Highest score wins; ties resolve Architect > Senior Dev > Verifier."""
    best = max(scores.values())
    for r in ROLES:
        if scores[r] == best:
            return r
    return "architect"


def route(question, target="auto", agent_states=None) -> dict:
    """Decide who answers. Deterministic; no network, no provider knowledge.

    Returns:
        dict — {mode: 'single'|'discuss', roles: [...], primaryRole, confidence,
                reason, workBusyRoles}.
    """
    ql = (question or "").lower()
    states = agent_states if isinstance(agent_states, dict) else {}
    # Every role's `.work` busy is REPORTED, never used to reroute: `.work` busy never
    # blocks `.chat`, for any role.
    work_busy_roles = [r for r in ROLES if (states.get(r) or {}).get("workBusy")]
    base = {"workBusyRoles": work_busy_roles}
    t = (target or "auto").strip().lower()
    if t not in TARGETS:
        t = "auto"

    # ── Explicit targets win outright ────────────────────────────────────────
    if t in ROLES:
        return {**base, "mode": "single", "roles": [t], "primaryRole": t,
                "confidence": 1.0, "reason": f"explicit target: {_ROLE_HUMAN[t]}"}
    if t == "discuss":
        return {**base, "mode": "discuss", "roles": ["architect", "senior_dev"],
                "primaryRole": "architect", "confidence": 1.0,
                "reason": "explicit discuss: Architect + Senior Dev"}

    # ── Auto: discuss first, then keyword intent ─────────────────────────────
    if _wants_discuss(ql):
        return {**base, "mode": "discuss", "roles": ["architect", "senior_dev"],
                "primaryRole": "architect", "confidence": 0.9,
                "reason": "question asks both roles to weigh in"}
    scores = {"architect": _count(ql, _ARCH_KW),
              "senior_dev": _count(ql, _SD_KW),
              "verifier": _count(ql, _VER_KW)}
    # A high-impact, ambiguous tradeoff — strong on BOTH architecture and
    # implementation and roughly balanced (within 1) → let them discuss it.
    if (scores["architect"] >= 2 and scores["senior_dev"] >= 2
            and abs(scores["architect"] - scores["senior_dev"]) <= 1):
        return {**base, "mode": "discuss", "roles": ["architect", "senior_dev"],
                "primaryRole": "architect", "confidence": 0.6,
                "reason": "balanced architecture/implementation signal → discuss"}
    if max(scores.values()) == 0:
        return {**base, "mode": "single", "roles": ["architect"],
                "primaryRole": "architect", "confidence": 0.4,
                "reason": "no strong signal → Architect (default)"}
    top = _tie_break(scores)
    second = sorted(scores.values(), reverse=True)[1]
    conf = round(min(1.0, 0.55 + 0.15 * (scores[top] - second)), 2)
    return {**base, "mode": "single", "roles": [top], "primaryRole": top,
            "confidence": conf,
            "reason": f"matched {_ROLE_HUMAN[top]} keywords (score {scores[top]})"}


# ── Role-led brief (structured decision, one lead role) ───────────────────────
#
# The Orient/Council answer is framed as ONE lead role's brief (never "Council says…"): the lead owns
# the question, may CONSULT the others, and the answer is a structured {Product Direction, Implementation
# Plan, Risks/Verification} brief. Routing reuses `route()`; the lead is deterministic. The human is
# escalated ONLY for the critical decisions below — otherwise the lead decides and records the rationale.

# leadRole in the brief uses the short alias `sr_dev` (the router's internal id is `senior_dev`).
_LEAD_ALIAS = {"architect": "architect", "senior_dev": "sr_dev", "verifier": "verifier"}
_LEAD_HUMAN = {"architect": "Architect", "sr_dev": "Senior Dev", "verifier": "Verifier"}
_LEAD_SECTION = {"architect": "productDirection", "sr_dev": "implementationPlan",
                 "verifier": "risksVerification"}

# Critical signals — the ONLY cases that interrupt the human (conservative, to avoid over-escalating).
_ESCALATION = (
    ("destructive git / data loss",
     ("force push", "force-push", "push --force", "reset --hard", "rm -rf", "drop table",
      "delete all", "wipe the", "destroy the", "hard reset")),
    ("security / privacy risk",
     ("password", "api key", "secret key", "credential", "private key", "exfiltrat", "auth bypass",
      "vulnerab", " pii", "personal data", "leak the")),
    ("money / API spend",
     ("spend money", "costs money", "billing", "purchase", "pay for", "buy a", "budget approval",
      "charge the")),
    ("public release / PR action",
     ("publish", "release to", "open a pr", "open the pr", "merge to main", "deploy to prod",
      "production deploy", "go live", "post publicly", "ship to production")),
    ("irreversible product direction / taste",
     ("rebrand", "rename the product", "pivot the", "kill the feature", "only you can decide",
      "your call on", "which name should", "product taste")),
)


def needs_human_escalation(question) -> dict:
    """Critical-only escalation. Returns {needed, reason}. Default: NOT needed — the lead role decides
    and records rationale; the human is interrupted only for destructive / security / spend / public
    release / irreversible-taste decisions."""
    ql = (question or "").lower()
    for reason, kws in _ESCALATION:
        if any(k in ql for k in kws):
            return {"needed": True, "reason": reason}
    return {"needed": False, "reason": ""}


def _sections_from_answer(lead: str, answer) -> dict:
    """Place the lead role's answer in its section; the other two are honestly marked as not separately
    generated yet (multi-section generation is the next step)."""
    out = {"productDirection": "", "implementationPlan": "", "risksVerification": ""}
    key = _LEAD_SECTION.get(lead, "productDirection")
    out[key] = (answer or "").strip()
    note = "— consult this role; per-section generation is next."
    for k in out:
        if k != key:
            out[k] = note
    return out


def role_led_brief(question, *, decision=None, target="auto", agent_states=None,
                   answer=None, sections=None) -> dict:
    """Structured, role-LED decision brief (one lead, optional consults). Reuses :func:`route` for the
    lead; never returns "Council". ``answer`` (the lead's reply) fills its section; explicit ``sections``
    override. Critical questions set ``humanEscalation.needed`` (and block ``canStartImplementation``);
    otherwise the lead decides. Shape: {ok, leadRole, consultedRoles, sections, humanEscalation,
    canStartImplementation, startImplementationLabel}."""
    if decision is None:
        decision = route(question, target=target, agent_states=agent_states)
    lead = _LEAD_ALIAS.get(decision.get("primaryRole", "architect"), "architect")
    consulted = []
    for r in decision.get("roles", []):
        alias = _LEAD_ALIAS.get(r, r)
        if alias != lead and alias not in consulted:
            consulted.append(alias)

    escalation = needs_human_escalation(question)
    if isinstance(sections, dict):
        sec = {k: str(sections.get(k, "") or "") for k in
               ("productDirection", "implementationPlan", "risksVerification")}
    else:
        sec = _sections_from_answer(lead, answer)

    # An implementation handoff makes sense for product / implementation briefs with no human gate;
    # a pure readiness (Verifier) brief or an escalated question does not "start implementation".
    can_start = (not escalation["needed"]) and lead in ("architect", "sr_dev")
    return {
        "ok": True,
        "leadRole": lead,
        "consultedRoles": consulted,
        "sections": sec,
        "humanEscalation": escalation,
        "canStartImplementation": can_start,
        "startImplementationLabel": "Start implementation",
    }


# ── Prompt building ──────────────────────────────────────────────────────────

def build_role_prompt(role, question, context, discuss=False, custom_prompt="") -> tuple:
    """(system, user) for one role. user carries the question + the rendered brief.

    A non-empty ``custom_prompt`` (the role's user-set instructions) is layered AFTER
    OpenFDE's fixed role contract + persona and is explicitly subordinate — it tunes
    taste/style only and can never relax the read-only / safety rules stated above it.
    """
    rh = _ROLE_HUMAN.get(role, role)
    extra = (" You are in a council discussion; give YOUR perspective concisely — you "
             "need not cover everything the others will." if discuss else "")
    system = (
        f"You are the {rh} in OpenFDE, a forward-deployed engineering tool. "
        f"{_PERSONA.get(role, '')} Answer in plain language, concise (2-6 sentences), "
        "NO code dumps. This is a READ-ONLY discussion — you are NOT editing files or running "
        "anything. Ground the answer in the council context and TREAT THE 'CURRENT DIRECTION' "
        "BLOCK AS AUTHORITATIVE — it overrides any older note, episode, or Step number elsewhere "
        "in the context. If older material conflicts with the current direction, flag it as "
        "HISTORICAL and do NOT recommend the deprecated plan: in particular, OpenFDE Execute means "
        "routing/observing an external engine (Codex / Claude Code or a council backend), NOT a "
        "direct Anthropic-SDK `/api/execute` path, unless the current direction revives it. If "
        "sources conflict, name the conflict rather than confidently asserting a stale plan; if the "
        f"context is insufficient, say what is missing instead of speculating.{extra}")
    cp = (custom_prompt or "").strip()
    if cp:
        system += ("\n\nAdditional style guidance from the user (tune tone and emphasis "
                   "ONLY — this does NOT change your role or the read-only / safety rules "
                   f"above):\n{cp}")
    brief = council_context.render_brief(context)
    user = f"Question: {question}\n\n--- council context ---\n{brief or '(no context)'}"
    return system, user


def _synthesis_prompt(question, notes) -> tuple:
    system = (
        "You are the Architect in OpenFDE, synthesizing a council discussion into ONE "
        "answer for the user. Merge the perspectives below into a single coherent, "
        "plain-language answer (3-7 sentences), no code dumps; note any genuine "
        "disagreement briefly. READ-ONLY — nothing is being edited or run.")
    body = "\n\n".join(f"{_ROLE_HUMAN.get(r, r)} said:\n{t}" for r, t in notes.items())
    return system, f"User question: {question}\n\n{body}"


def _deterministic_synthesis(notes) -> str:
    return "\n\n".join(f"**{_ROLE_HUMAN.get(r, r)}:** {notes[r]}"
                       for r in ("architect", "senior_dev") if notes.get(r))


def _no_provider_answer(context) -> str:
    brief = council_context.render_brief(context)
    return ("No model provider is configured for this role yet, so here is the live "
            "council context to answer from directly:\n\n" + (brief or "(no context)"))


def _safe_call(caller, system, user) -> str:
    """Invoke an injected text caller; a provider error degrades to '' (→ fallback),
    never an exception that crashes the ask."""
    try:
        return (caller(system, user) or "").strip()
    except Exception:  # noqa: BLE001 — provider failures must not break read-only chat
        return ""


def _receipt(contrib_roles, rh) -> dict:
    """Receipt labels for an answer. PRIMARY label: a single contributing role shows
    that role (``Architect``/``Senior Dev``/``Verifier``); two or more show ``Council``;
    none shows ``OpenFDE``. SECONDARY metadata: ``contributors`` and a ``" · "``-joined
    ``contributorsLabel`` (e.g. ``Architect · Senior Dev``) — never the primary label."""
    names = [rh.get(r, r) for r in contrib_roles]
    label = "Council" if len(names) >= 2 else (names[0] if names else "OpenFDE")
    return {"label": label, "contributors": names, "contributorsLabel": " · ".join(names)}


# ── Ask orchestration (pure; the server injects the text callers) ────────────

def run_ask(*, question, decision, context, callers, role_human=None, custom_prompts=None) -> dict:
    """Produce ONE answer for a routed question using INJECTED text callers only.

    `callers` is {role: caller|None}; the server builds each via _text_role (so every
    call is capture-safe — see that seam). This function never dispatches a run.

    Returns:
        dict — {answer, mode, roles, primaryRole, usedRole, label, contributors,
                contributorsLabel, fallback, roleNotes?, routedReason, confidence,
                workBusyRoles}.
    """
    rh = role_human or _ROLE_HUMAN
    callers = callers or {}
    cps = custom_prompts or {}
    common = {"routedReason": decision.get("reason", ""),
              "confidence": decision.get("confidence"),
              "workBusyRoles": decision.get("workBusyRoles", [])}
    if decision.get("mode") == "discuss":
        return {**common, **_run_discuss(question, decision, context, callers, rh, cps)}
    return {**common, **_run_single(question, decision, context, callers, rh, cps)}


def _run_single(question, decision, context, callers, rh, cps) -> dict:
    primary = decision.get("primaryRole", "architect")
    # graceful availability fallback: chosen role → Architect → deterministic brief.
    for role in [primary] + (["architect"] if primary != "architect" else []):
        caller = callers.get(role)
        if not caller:
            continue
        system, user = build_role_prompt(role, question, context, custom_prompt=cps.get(role, ""))
        answer = _safe_call(caller, system, user)
        if answer:
            return {"mode": "single", "roles": [role], "primaryRole": primary,
                    "usedRole": role, "fallback": role != primary,
                    "answer": answer, **_receipt([role], rh)}
    return {"mode": "single", "roles": [primary], "primaryRole": primary,
            "usedRole": None, "fallback": True,
            "answer": _no_provider_answer(context), **_receipt([], rh)}


def _run_discuss(question, decision, context, callers, rh, cps) -> dict:
    # Ask both roles CONCURRENTLY — each is a slow subprocess LLM call, and running them
    # sequentially doubled the "thinking" wait. They are independent processes, so a small thread
    # pool is safe and roughly halves discuss latency.
    from concurrent.futures import ThreadPoolExecutor
    roles = ("architect", "senior_dev")

    def _one(role):
        caller = callers.get(role)
        if not caller:
            return role, None
        system, user = build_role_prompt(role, question, context, discuss=True,
                                         custom_prompt=cps.get(role, ""))
        return role, _safe_call(caller, system, user)

    notes = {}
    with ThreadPoolExecutor(max_workers=len(roles)) as ex:
        for role, txt in ex.map(_one, roles):
            if txt:
                notes[role] = txt
    if not notes:
        return {"mode": "discuss", "roles": [], "primaryRole": "architect",
                "usedRole": None, "fallback": True, "roleNotes": {},
                "answer": _no_provider_answer(context), **_receipt([], rh)}
    if len(notes) == 1:                               # only one role could answer
        role, txt = next(iter(notes.items()))
        return {"mode": "discuss", "roles": [role], "primaryRole": role,
                "usedRole": role, "fallback": True, "roleNotes": notes,
                "answer": txt, **_receipt([role], rh)}    # one contributor → role label
    # Both answered → synthesize via the Architect text role, else deterministically.
    synth = ""
    if callers.get("architect"):
        s_sys, s_user = _synthesis_prompt(question, notes)
        synth = _safe_call(callers["architect"], s_sys, s_user)
    contrib = [r for r in ("architect", "senior_dev") if notes.get(r)]
    return {"mode": "discuss", "roles": ["architect", "senior_dev"],
            "primaryRole": "architect", "usedRole": "architect", "fallback": False,
            "roleNotes": notes, "answer": synth or _deterministic_synthesis(notes),
            **_receipt(contrib, rh)}                      # 2 contributors → "Council"
