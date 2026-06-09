"""
openfde/prompt_wrapper.py — OpenFDE Prompt Capture Bridge v1.

    Git tells us what landed. OpenFDE should tell us why.

`openfde cc "…"` / `openfde codex "…"` run an external coding agent (Claude Code /
Codex) over the repo, but capture the *prompt* as a durable OpenFDE **episode**
first. The agent only EDITS; it never commits (no-git directive + tool limits).
After it exits we inspect the work tree, record the touched files, and set the
episode status so the change surfaces on the Prompt Story Rail as a prompt chip the
user can Review and **Land** (OpenFDE makes the commit, stamped OpenFDE-Episode).

This captures *future* prompts only — no historical log scraping (that's a future,
best-effort, confidence-tagged effort; see FLOW.md / ROADMAP.md).
"""

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from openfde.persistence import Persistence

logger = logging.getLogger("openfde.prompt_wrapper")

_KIND_LABEL = {"claude-code": "Claude Code", "codex": "Codex"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _head(root: Path):
    """Current git HEAD sha for the repo, or None (best-effort, never raises)."""
    try:
        from openfde.git_timeline import git_status
        return git_status(root).get("head")
    except Exception:  # noqa: BLE001
        return None


def _invoke_agent(kind: str, root: Path, prompt: str, model):
    """Dispatch to the right repo-wide editing runner. Returns its result dict
    ({ok, touched, summary, error}). Imported lazily so `--help` stays light."""
    if kind == "codex":
        from openfde.codex_local_runner import run_codex_local_edit
        return run_codex_local_edit(repo_root=root, prompt=prompt, model=model)
    from openfde.claude_code_runner import run_claude_code_cli
    return run_claude_code_cli(repo_root=root, prompt=prompt, model=model)


def run_prompt_wrapper(kind: str, prompt: str, repo_path: str, model=None,
                       invoke=_invoke_agent) -> dict:
    """Capture a prompt as an episode, run the agent, finalize the episode.

    Args:
        kind: str — "claude-code" or "codex".
        prompt: str — the user's prompt text.
        repo_path: str — repository path (the wrapper resolves + creates `.openfde/`).
        model: str | None — optional model override.
        invoke: callable(kind, root, prompt, model) -> dict — agent dispatcher
            (injectable for tests; defaults to the real runners).

    Returns:
        dict — {ok, episode, result, message}.
    """
    root = Path(repo_path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    openfde_dir = root / ".openfde"
    openfde_dir.mkdir(parents=True, exist_ok=True)
    p = Persistence(openfde_dir)

    wid = "wrap_" + secrets.token_hex(5)
    now = _now()
    episode = {
        "episodeId": "episode_" + secrets.token_hex(6),
        "createdAt": now, "updatedAt": now,
        "prompt": (prompt or "").strip(),
        "kind": kind, "status": "open",
        "runIds": [wid], "eventIds": [], "projectEntryIds": [],
        "commitShas": [], "files": [], "summary": "",
        "source": "openfde-wrapper", "initialHead": _head(root),
    }
    # Persist BEFORE invoking the agent, so the prompt is captured even if the agent
    # crashes or is interrupted — the "why" is never lost.
    p.upsert_episode(episode)
    logger.info("Captured prompt episode %s (%s)", episode["episodeId"], kind)

    res = invoke(kind, root, episode["prompt"], model) or {}
    touched = sorted(res.get("touched") or [])
    summary = (res.get("summary") or res.get("error") or "")[:200]

    if touched and res.get("ok"):
        # Agent finished cleanly with edits → AUTO-LAND its files (scoped commit under
        # this episode). Manual Land remains the fallback (autoland returns
        # needs_manual_land if attribution is ambiguous or the commit can't isolate).
        episode.update({"updatedAt": _now(), "status": "reviewing", "files": touched, "summary": summary})
        p.upsert_episode(episode)
        from openfde import autoland
        land = autoland.land_episode(root, p, episode, auto=True)   # separate process: broadcasts ignored
        episode = land.get("episode", episode)
    elif touched:
        # Edits exist but the agent reported failure → leave for manual review.
        episode.update({"updatedAt": _now(), "status": "reviewing", "files": touched, "summary": summary})
        p.upsert_episode(episode)
    elif res.get("ok"):
        episode.update({"updatedAt": _now(), "status": "complete_no_changes", "files": [], "summary": summary})
        p.upsert_episode(episode)
    else:
        episode.update({"updatedAt": _now(), "status": "failed", "files": [], "summary": summary})
        p.upsert_episode(episode)

    return {"ok": bool(res.get("ok")), "episode": episode, "result": res,
            "message": _next_steps(kind, episode, res, root)}


def _next_steps(kind: str, episode: dict, res: dict, root: Path) -> str:
    """A concise, honest next-step message for the terminal."""
    label = _KIND_LABEL.get(kind, kind)
    eid = episode["episodeId"]
    status = episode["status"]
    touched = episode["files"]
    lines = []
    shas = episode.get("commitShas") or []
    if status == "landed":
        short = (shas[-1][:7] if shas else "")
        lines.append(f"✓ {label} edited {len(touched)} file(s) — auto-landed as {short} under prompt episode {eid}.")
        for f in touched[:12]:
            lines.append(f"    • {f}")
        if len(touched) > 12:
            lines.append(f"    … +{len(touched) - 12} more")
        lines.append("  OpenFDE committed only this prompt's files (unrelated changes left untouched).")
        lines.append(f"  See it on the rail / OpenPM / Story:  openfde watch {root} --port 7441")
    elif status == "needs_manual_land":
        lines.append(f"• {label} edited {len(touched)} file(s) — captured as episode {eid}, but auto-land was held back.")
        lines.append("  Reason: attribution was ambiguous (unrelated dirty files overlap).")
        lines.append("  Review changes → Land on the rail to commit this prompt's set manually.")
    elif status == "reviewing":
        lines.append(f"✓ {label} edited {len(touched)} file(s) — captured as prompt episode {eid} (awaiting review).")
        for f in touched[:12]:
            lines.append(f"    • {f}")
        lines.append("  Review changes → Land on the OpenArchitect rail to commit under this prompt.")
    elif status == "complete_no_changes":
        lines.append(f"• {label} ran but made no file changes — prompt episode {eid} recorded (no review needed).")
    else:
        err = (res.get("error") or "agent failed").strip()
        lines.append(f"✗ {label} did not complete: {err}")
        lines.append(f"  Prompt episode {eid} recorded as failed (no commit, no fake edits).")
    return "\n".join(lines)
