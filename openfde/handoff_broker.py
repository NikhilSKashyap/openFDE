"""
openfde/handoff_broker.py — event-driven SESSION WAKEUP / agent handoff resume for the external
council.

**This is NOT native UI injection.** OpenFDE never types into Codex or Claude Code. When a council
bus status transitions, the broker creates a durable **delivery record** — "this receiving role
should resume and read its inbox" — and attempts to WAKE the receiving session through
capability-gated adapters. The receiving session then runs ``openfde council status --role <…>`` and
does the work; it is responsible for displaying the handoff.

Product rule: a handoff is complete only when the receiving role has a DURABLE delivery record
(``.openfde/council/deliveries.json``, gitignored like the bus). The ``session-inbox`` adapter
guarantees that. Native session wakeup (``codex-session`` / ``claude-session``) is a capability-gated
bonus — when no runtime API exists it is reported honestly as ``native_unavailable``, never faked and
never typed into a UI.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

from openfde import council_bus

DELIVERIES_FILE = "deliveries.json"

# Council status → the role that must resume and read its inbox (a WAKEUP-creating transition).
# VERIFIED closes the delivery (handled explicitly); CLAUDE_WORKING and unknowns are no-ops.
_STATUS_TO_ROLE = {
    council_bus.STATUS_READY_FOR_CC:                 "claude",
    council_bus.STATUS_READY_FOR_CODEX_VERIFICATION: "codex",
    council_bus.STATUS_CHANGES_REQUESTED:            "claude",
    council_bus.STATUS_BLOCKED_NEEDS_ARCHITECT:      "codex",
    council_bus.STATUS_BLOCKED_NEEDS_HUMAN:          "human",   # human / UI only — no agent wakeup
}
_SENDER_FOR_RECEIVER = {"claude": "codex", "codex": "claude", "human": "council"}


def receiving_role(status: str):
    """The role a status hands off TO (must resume), or None for a non-wakeup status (VERIFIED,
    CLAUDE_WORKING, unknown)."""
    return _STATUS_TO_ROLE.get(status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Durable delivery log (gitignored, like the bus) ──────────────────────────
def deliveries_path(repo_root) -> str:
    return os.path.join(str(repo_root), council_bus.COUNCIL_DIRNAME, DELIVERIES_FILE)


def load_deliveries(repo_root) -> list:
    try:
        with open(deliveries_path(repo_root), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_deliveries(repo_root, deliveries) -> None:
    council_bus.ensure_council_dir(repo_root)
    with open(deliveries_path(repo_root), "w", encoding="utf-8") as f:
        json.dump(deliveries, f, indent=2)


def _is_open(d: dict) -> bool:
    return isinstance(d, dict) and not d.get("closedAt") and not d.get("supersededAt")


def active_delivery(deliveries, role: str = None):
    """The newest OPEN (not closed / superseded) delivery, optionally for a role. An acknowledged
    delivery stays active until the next transition supersedes or closes it."""
    for d in reversed(deliveries or []):
        if _is_open(d) and (not role or d.get("toRole") == role):
            return d
    return None


def pending_delivery(repo_root, role: str):
    """The active, NOT-yet-acknowledged delivery awaiting ``role`` (what ``council status`` shows
    first), or None."""
    d = active_delivery(load_deliveries(repo_root), role=role)
    return d if (d and not d.get("acknowledgedAt")) else None


# ── Wake adapters: wake(role, delivery) -> WakeResult ────────────────────────
def _native_wake_available(role: str) -> bool:
    """Capability gate for a NATIVE runtime wakeup API (codex-session / claude-session). v1 has no
    such API, so this is False — the broker never fakes a wake and never types into a UI."""
    return False


def run_wake_adapters(role: str, delivery: dict, repo_root) -> list:
    """Attempt to wake the receiving role through each adapter; return their WakeResults. Order: the
    durable ``session-inbox`` (the REQUIRED guarantee — ``pending`` until acknowledged), the
    ``openfde-ui`` bubble, then the capability-gated native adapter (honest ``native_unavailable``)."""
    results = [{"adapter": "session-inbox", "status": "pending"}]
    if role in ("codex", "claude"):
        results.append({"adapter": "openfde-ui", "status": "delivered"})
        native = "codex-session" if role == "codex" else "claude-session"
        results.append({"adapter": native,
                        "status": "delivered" if _native_wake_available(role) else "native_unavailable"})
    elif role == "human":
        results.append({"adapter": "openfde-ui", "status": "delivered"})
    return results


# ── The broker ───────────────────────────────────────────────────────────────
def build_delivery(view: dict, *, delivery_id: str, now: str) -> dict:
    """A concise delivery record from a work-item view — EXISTING ids only (``deliveryId`` is the
    delivery's own id, never a substitute for episode/task ids)."""
    status = view.get("status") or ""
    to_role = receiving_role(status)
    return {
        "deliveryId": delivery_id,
        "episodeId": view.get("episodeId") or "",
        "taskIds": list(view.get("taskIds") or []),
        "runId": view.get("runId") or "",
        "boxIds": list(view.get("boxIds") or []),
        "fromRole": _SENDER_FOR_RECEIVER.get(to_role, "council"),
        "toRole": to_role or "",
        "status": status,
        "latestCommit": view.get("latestCommit") or "",
        "objective": view.get("objective") or "",
        "createdAt": now, "deliveredAt": now, "acknowledgedAt": None,
        "closedAt": None, "supersededAt": None, "wake": [],
    }


def process_transition(repo_root, view: dict, *, deliveries=None, now: str = None):
    """Observe one council bus transition (a current work-item view) and update the durable delivery
    log. Creates ONE delivery for the receiving role — **deduped** (a repeated file event at the same
    episode+status+role does NOT duplicate); **supersedes** the episode's prior open deliveries; on
    VERIFIED **closes** the episode's open deliveries and creates none. Returns the active delivery
    for this transition (or None). Persists the log unless caller passed ``deliveries`` to mutate."""
    now = now or _now()
    persist = deliveries is None
    deliveries = load_deliveries(repo_root) if deliveries is None else deliveries
    episode = view.get("episodeId") or ""
    status = view.get("status") or ""
    role = receiving_role(status)

    if status == council_bus.STATUS_VERIFIED:         # terminal → close the episode's deliveries
        changed = False
        for d in deliveries:
            if d.get("episodeId") == episode and _is_open(d):
                d["closedAt"] = now
                changed = True
        if changed and persist:
            save_deliveries(repo_root, deliveries)
        return None
    if role is None:                                  # CLAUDE_WORKING / unknown → no delivery action
        return None

    # Dedup: an open delivery for this exact (episode, status, role) already exists → no duplicate.
    for d in deliveries:
        if (d.get("episodeId") == episode and d.get("status") == status
                and d.get("toRole") == role and _is_open(d)):
            return d

    # Supersede the episode's prior open deliveries — a new transition replaces the old wakeup.
    for d in deliveries:
        if d.get("episodeId") == episode and _is_open(d):
            d["supersededAt"] = now

    delivery = build_delivery(view, delivery_id="delivery_" + secrets.token_hex(5), now=now)
    delivery["wake"] = run_wake_adapters(role, delivery, repo_root)
    deliveries.append(delivery)
    if persist:
        save_deliveries(repo_root, deliveries)
    return delivery


def acknowledge_delivery(repo_root, role: str, *, now: str = None):
    """Mark the active delivery for ``role`` acknowledged (the receiving session has started). The
    delivery stays in the log; only its ``acknowledgedAt`` is set. Returns it, or None when none."""
    now = now or _now()
    deliveries = load_deliveries(repo_root)
    d = active_delivery(deliveries, role=role)
    if not d or d.get("acknowledgedAt"):
        return d
    d["acknowledgedAt"] = now
    save_deliveries(repo_root, deliveries)
    return d


def delivery_summary(delivery) -> dict:
    """A concise delivery payload for the WS event / UI (or None). Never overclaims — it carries the
    raw ``wake`` adapter statuses so the UI can show 'pending' vs 'delivered' honestly."""
    if not delivery:
        return None
    return {k: delivery.get(k) for k in
            ("deliveryId", "episodeId", "taskIds", "runId", "boxIds", "fromRole", "toRole",
             "status", "latestCommit", "wake", "acknowledgedAt")}


def delivery_banner(repo_root, role: str) -> str:
    """One-line resume banner for the CLI when a delivery is pending for ``role`` (else '')."""
    d = pending_delivery(repo_root, role)
    if not d:
        return ""
    native = next((w["status"] for w in (d.get("wake") or [])
                   if str(w.get("adapter", "")).endswith("-session")), "native_unavailable")
    return (f"▶ Resume council handoff — delivery {d['deliveryId']} from {d.get('fromRole') or 'council'} "
            f"(native wakeup: {native}). Read your inbox below; `openfde council ack --role {role}` "
            "when you have started:")
