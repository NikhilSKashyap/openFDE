"""
openfde/council.py — bounded Agent Council orchestration (Step 29 Slice 2).

The copy-paste killer: OpenFDE *is* the message bus between roles.

    Architect → Senior Dev → Verifier → reprompt-or-advance

Pure orchestration: roles are injected callables, so this is fully testable with
fakes and no network. The server wires the real roles (Architect/Verifier via the
OpenAI-compatible transport, Senior Dev via the existing scoped `agent_runner`)
and lands the final result through the existing gated `reconcile_result`.

Role callable contracts:
    architect(context: dict) -> brief: str
    senior_dev(brief: str)   -> outcome: dict  (agent_runner shape:
                                {result, writes, rejected, protectedAttempts, error})
    verifier(brief, result)  -> verdict: dict {status, summary, fixPrompt,
                                testsSuggested, risks}  (status in passed|failed|needs_human)

This module never writes files, never commits, never enforces scope — Senior Dev
(the agent runner) owns all write/scope enforcement server-side. The council only
sequences roles and decides advance/reprompt/stop. Secrets never pass through here.
"""

import logging

logger = logging.getLogger("openfde.council")

_MAX_SUMMARY = 300
_MAX_DETAIL = 2000


def _stage(role, status, summary, attempt=0, detail=""):
    return {
        "role": role,
        "status": status,
        "summary": (summary or "")[:_MAX_SUMMARY],
        "attempt": attempt,
        "detail": (detail or "")[:_MAX_DETAIL],
    }


def run_council(*, architect, senior_dev, verifier, context, max_reprompts=1):
    """Run one bounded Architect → Senior Dev → Verifier loop.

    Args:
        architect/senior_dev/verifier: injected role callables (see module doc).
        context: dict — scope/intent passed to the Architect.
        max_reprompts: int — verifier-triggered Senior Dev retries (this slice: 1).

    Returns:
        dict — {stages, finalResult, status, attempts, verifier} where `status`
        is passed|failed|needs_approval|needs_human and `finalResult` is a
        Step-20 contract whose status is normalized for reconcile
        (passed | failed | needs_approval) — needs_human lands as failed (no commit)
        while the API surfaces needs_human.
    """
    stages = []

    # ── Architect ───────────────────────────────────────────────────────────
    brief = ""
    try:
        brief = (architect(context) or "").strip()
    except Exception as exc:  # noqa: BLE001 — never let a role crash the loop
        stages.append(_stage("architect", "failed", f"Architect error: {exc}"))
    if not brief:
        brief = (context.get("prompt") or "Implement the selected scope.").strip()
    if not stages or stages[-1]["role"] != "architect":
        stages.append(_stage("architect", "completed", brief))

    # ── Senior Dev (the only writer; scope enforced inside it) ───────────────
    def run_sr(b, attempt):
        outcome = senior_dev(b) or {}
        result = outcome.get("result") or {}
        writes = outcome.get("writes") or []
        prot = outcome.get("protectedAttempts") or []
        rej = [r.get("reason") for r in (outcome.get("rejected") or [])]
        st = result.get("status", "failed")
        summary = result.get("reportSummary") or f"{len(writes)} file(s) written"
        detail = f"writes={writes} protectedAttempts={prot} rejected={rej}"
        stages.append(_stage("sr_dev", st, summary, attempt, detail))
        return result

    def run_verifier(b, res, attempt):
        try:
            v = verifier(b, res) or {}
        except Exception as exc:  # noqa: BLE001
            v = {"status": "needs_human", "summary": f"Verifier error: {exc}"}
        if v.get("status") not in ("passed", "failed", "needs_human"):
            v["status"] = "needs_human"
        stages.append(_stage("verifier", v["status"], v.get("summary", ""), attempt))
        return v

    def landed(result, overall, verdict, attempts):
        final = dict(result)
        # Normalize for reconcile: only passed | failed | needs_approval commit-gate.
        if overall == "passed":
            final["status"] = "passed"
        elif overall == "needs_approval":
            final["status"] = "needs_approval"
        else:                                   # failed | needs_human
            final["status"] = "failed"
        return {"stages": stages, "finalResult": final, "status": overall,
                "attempts": attempts, "verifier": verdict}

    result = run_sr(brief, 1)
    if result.get("status") == "needs_approval":      # protected scope — skip verifier
        return landed(result, "needs_approval", None, 1)

    verdict = run_verifier(brief, result, 1)
    if verdict["status"] == "passed":
        return landed(result, "passed", verdict, 1)
    if verdict["status"] == "needs_human":
        return landed(result, "needs_human", verdict, 1)

    # ── Verifier failed → reprompt Senior Dev once ──────────────────────────
    if max_reprompts < 1:
        return landed(result, "failed", verdict, 1)

    fix = verdict.get("fixPrompt") or "Address the verifier's concerns and try again."
    brief2 = (f"{brief}\n\nThe previous attempt was REJECTED by the Verifier:\n"
              f"{verdict.get('summary', '')}\nFix instructions:\n{fix}")
    result2 = run_sr(brief2, 2)
    if result2.get("status") == "needs_approval":
        return landed(result2, "needs_approval", verdict, 2)

    verdict2 = run_verifier(brief2, result2, 2)
    if verdict2["status"] == "passed":
        return landed(result2, "passed", verdict2, 2)
    overall = "needs_human" if verdict2["status"] == "needs_human" else "failed"
    return landed(result2, overall, verdict2, 2)
