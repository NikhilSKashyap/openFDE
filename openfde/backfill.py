"""
openfde/backfill.py — reconstruct historical prompt episodes when OpenFDE starts late.

Passive capture is *forward-only* (it baselines transcripts to EOF on startup). But the
local agent transcripts persist on disk, so the work done before `openfde watch` is not
lost — just un-ingested. This module reads those transcripts **from the beginning**,
imports each human prompt as a historical episode, and links a nearby commit when the
evidence is strong.

Reuses the prompt_capture primitives (transcript discovery, prompt parsing, edit-file
attribution, the captureKey). Laws:

  • Match to THIS repo — Claude Code: a SAME-cwd turn (the prompt's session cwd IS the
    repo) imports even when discussion-only; a CROSS-cwd turn (a session rooted elsewhere)
    imports ONLY when it edited files under the repo. Discussion-only prompts from a
    different cwd are NOT this repo's history and are skipped. Codex stays cwd-exact (its
    tool events carry no clean file path yet).
  • Skip OpenFDE-internal prompts — ``is_human_prompt`` already drops the summarizer /
    capture INTERNAL_MARKER and the runner directive, and machine/tool/meta turns.
  • Preserve source (claude-code / codex) and the original timestamp.
  • Link a commit only at HIGH confidence: it lands AFTER the prompt within a window,
    touched files the prompt's turn edited, and is not already linked → status ``landed``.
    Edited-but-no-commit → ``needs_manual_land`` (work happened, can't confirm the land).
    No file/commit evidence → a discussion episode (``open``). Never guess.
  • Idempotent — keyed by the stable captureKey, so a restart never duplicates.
  • Never auto-commit. Backfill reconstructs history; it does not rewrite git history.
"""
from __future__ import annotations

import logging
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from openfde.prompt_capture import (
    _same_dir, capture_key_exists,
    codex_prompt_text, codex_session_id_from_path, edit_files_under,
    is_codex_human_prompt, is_human_prompt, read_new_lines, _claude_transcripts,
    _codex_init_ctx, codex_transcripts, _prompt_record,
)

logger = logging.getLogger("openfde.backfill")

_LINK_WINDOW_S = 3 * 3600       # a commit may land up to 3h after its prompt
_MAX_IMPORT = 500               # cap a single backfill so a huge home never floods


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch(ts) -> float:
    """ISO-8601 (or epoch-ish) → epoch seconds; 0.0 when unparseable."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ── git history (for high-confidence commit linking) ─────────────────────────

def _git_commits(root) -> list:
    """[{sha, ts(epoch), files(set)}] for HEAD's history (newest-first). Empty on error."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "--no-merges", "--name-only",
             "--format=__C__%H %ct"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    commits, cur = [], None
    for ln in (proc.stdout or "").splitlines():
        if ln.startswith("__C__"):
            sha, _, ct = ln[5:].partition(" ")
            cur = {"sha": sha, "ts": float(ct or 0), "files": set()}
            commits.append(cur)
        elif ln.strip() and cur is not None:
            cur["files"].add(ln.strip())
    return commits


def _link_commit(prompt_ts: float, edited: set, commits: list, linked: set):
    """The highest-confidence commit for a prompt, or None.

    Strong signal only: lands within the window AFTER the prompt, touched a file the
    prompt's turn edited, and is not already linked to an episode. Earliest such commit
    wins (closest to the prompt). No time-only guessing.
    """
    if not prompt_ts or not edited:
        return None
    best = None
    for c in commits:
        if c["sha"] in linked:
            continue
        if not (prompt_ts <= c["ts"] <= prompt_ts + _LINK_WINDOW_S):
            continue
        if not (edited & c["files"]):
            continue
        if best is None or c["ts"] < best["ts"]:
            best = c
    return best


# ── transcript → turns (prompt + the files its turn edited) ──────────────────

def _claude_turns(path: Path, root) -> list:
    """[(prompt_record, edited_files)] for a Claude transcript — each human prompt with
    the repo-relative files its turn (up to the next prompt) edited."""
    entries, _ = read_new_lines(Path(path), 0)
    turns, cur, edits = [], None, []
    for e in entries:
        if is_human_prompt(e):
            if cur is not None:
                turns.append((cur, sorted(set(edits))))
            cur, edits = _prompt_record(e), []
        elif cur is not None:
            edits.extend(edit_files_under(e, root))
    if cur is not None:
        turns.append((cur, sorted(set(edits))))
    return turns


def _codex_turns(path: Path, root) -> list:
    """[(prompt_record, [])] for a Codex transcript whose session cwd is this repo.

    Codex tool events carry no clean file path (v1), so Codex turns import as discussion
    episodes with no edit-file evidence — honest, not guessed."""
    entries, _ = read_new_lines(Path(path), 0)
    fctx = _codex_init_ctx(Path(path))
    cwd = fctx.get("cwd")
    if not (cwd and _same_dir(cwd, str(root))):
        return []
    sid = fctx.get("sessionId") or codex_session_id_from_path(path)
    out = []
    for e in entries:
        if not is_codex_human_prompt(e):
            continue
        txt = (codex_prompt_text(e) or "").strip()
        if not txt:
            continue
        uuid = e.get("id") or e.get("uuid") or secrets.token_hex(8)
        out.append(({"key": f"{sid}:{uuid}", "text": txt, "sessionId": sid,
                     "uuid": uuid, "timestamp": e.get("timestamp") or fctx.get("timestamp"),
                     "cwd": cwd, "kind": "codex"}, []))
    return out


# ── orchestration ────────────────────────────────────────────────────────────

def _historical_episode(root, prompt: dict, edited: list, commit, kind: str) -> dict:
    """Build a historical (backfilled) episode. Status reflects confidence:
    linked commit → landed; edited-no-commit → needs_manual_land; else → open (discussion)."""
    if commit is not None:
        status, files, confidence = "landed", sorted(commit["files"]), "high"
    elif edited:
        status, files, confidence = "needs_manual_land", sorted(edited), "needs_review"
    else:
        status, files, confidence = "open", [], "discussion"
    return {
        "episodeId": "episode_" + secrets.token_hex(6),
        "createdAt": prompt.get("timestamp") or _now(), "updatedAt": _now(),
        "prompt": prompt.get("text", ""), "kind": kind, "status": status,
        "runIds": [], "eventIds": [], "projectEntryIds": [],
        "commitShas": ([commit["sha"]] if commit else []),
        "files": files, "summary": "", "source": "openfde-backfill",
        "captureKey": prompt.get("key"), "sessionId": prompt.get("sessionId"),
        "sessionCwd": prompt.get("cwd"), "historical": True,
        "historicalSource": kind, "backfillConfidence": confidence,
    }


def backfill_historical(root, persistence, *, home=None, max_import: int = _MAX_IMPORT) -> dict:
    """Import historical prompts for ``root`` from local Claude Code + Codex transcripts.

    Idempotent (keyed by captureKey). Never commits. Appends a quiet ``backfill_imported``
    event with the count and writes the episodes; returns a summary dict.

    Returns:
        dict — {imported, landed, needsReview, discussion, scanned, event?}.
    """
    root = Path(root)
    commits = _git_commits(root)
    linked = {s for e in persistence.load_episodes() for s in (e.get("commitShas") or [])}
    counts = {"imported": 0, "landed": 0, "needsReview": 0, "discussion": 0, "scanned": 0}

    def _ingest(turns, kind):
        for prompt, edited in turns:
            counts["scanned"] += 1
            # cwd attribution (backfill accuracy): a SAME-cwd turn (this prompt's session
            # cwd IS the repo) imports even when discussion-only; a CROSS-cwd turn imports
            # ONLY when it edited files under THIS repo. A discussion-only prompt from a
            # different cwd is another repo's history — skip it. (Codex turns reach here
            # only after _codex_turns filtered them to the repo's session cwd.)
            if not edited and not _same_dir(prompt.get("cwd"), str(root)):
                continue
            key = prompt.get("key")
            if not key or capture_key_exists(persistence, key):
                continue                                  # idempotent: already imported
            if counts["imported"] >= max_import:
                return
            commit = _link_commit(_epoch(prompt.get("timestamp")), set(edited), commits, linked)
            ep = _historical_episode(root, prompt, edited, commit, kind)
            if commit:
                linked.add(commit["sha"])
            persistence.upsert_episode(ep)
            counts["imported"] += 1
            counts[{"landed": "landed", "needs_manual_land": "needsReview",
                    "open": "discussion"}[ep["status"]]] += 1

    try:
        # Scan EVERY Claude transcript (the repo's own cwd dir is among them); _ingest
        # applies the same-cwd / cross-cwd-with-edits rule per turn.
        for path in sorted(_claude_transcripts(home)):
            _ingest(_claude_turns(path, root), "claude-code")
        for path in codex_transcripts(home):
            _ingest(_codex_turns(path, root), "codex")
    except Exception:  # noqa: BLE001 — backfill is best-effort, never blocks the watcher
        logger.debug("backfill scan failed", exc_info=True)

    event = None
    if counts["imported"]:
        try:
            event = persistence.append_event({
                "type": "backfill_imported",
                "payload": {"detail": f"Imported {counts['imported']} historical prompt"
                            f"{'' if counts['imported'] == 1 else 's'} for this repo "
                            f"({counts['landed']} landed, {counts['needsReview']} need review, "
                            f"{counts['discussion']} discussion).", **counts}})
        except Exception:  # noqa: BLE001
            logger.debug("backfill event append failed", exc_info=True)
    logger.info("backfill: imported %d historical prompt(s) for %s (scanned %d)",
                counts["imported"], root, counts["scanned"])
    return {**counts, **({"event": event} if event else {})}
