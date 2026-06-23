"""aiohttp web server: serves the built frontend and exposes the REST API.

Routes
------
GET  /ws                       — WebSocket endpoint; broadcasts state/task/event changes
GET  /api/files                — recursive file tree for the watched path
GET  /api/state                — canvas state (boxes + arrows)
PUT  /api/state                — persist canvas state  → writes state.json + PLAN.md
GET  /api/tasks                — OpenPM task list
PUT  /api/tasks                — persist task list     → writes tasks.json + PLAN.md
GET  /api/events               — all design/code events (oldest-first)
POST /api/events               — append a normalized event
GET  /api/project              — project metadata
POST /api/project              — persist project       → writes project.json + PROJECT_META.md + PLAN.md
GET  /api/project-log          — conversation ledger entries (oldest-first)
POST /api/project-log          — append a ledger entry → regenerates project.md
GET  /api/project-md           — generated project.md ledger as markdown
GET  /api/box-specs            — box prompt-provenance map (boxId → spec)
GET  /api/box-specs/{boxId}    — single box spec (404 when none)
POST /api/box-specs/update-from-execute — deterministic provenance update
GET  /api/plan                 — PLAN.md as a markdown string
GET  /api/archgraph            — ArchGraph for the watched repo (read-only)
POST /api/state/from-archgraph — generate canvas state from ArchGraph and persist it
POST /api/spec                 — compile canvas selection → implementation spec markdown
POST /api/runs                 — start an execution run (Step 17)
GET  /api/runs                 — list execution runs (latest-first)
POST /api/runs/{runId}/event   — append a trace event (payloads summarized/redacted)
GET  /api/runs/{runId}         — a run record + its trace events
GET  /api/execution/backends   — list execution backends + active one
POST /api/execution/backend    — set the active execution backend
POST /api/execution/compile-workflow — compile scope → workflow payload + script
POST /api/execution/run        — prepare a workflow run (artifact + events; no auto-run)
GET  /api/execution/workflows  — list prepared workflow artifacts
GET  /api/execution/workflow/{id} — a single workflow artifact
POST /api/execution/workflow/{id}/result — ingest a workflow result + reconcile
GET  /api/approvals            — protected-scope approval requests
POST /api/approvals/{id}/approve — approve a protected-scope gate
POST /api/approvals/{id}/reject  — reject a protected-scope gate
GET  /api/git/status           — repo git status (branch, head, dirty, staged)
GET  /api/git/timeline         — commit history (newest-first)
POST /api/git/commit           — stage meaningful files + commit → commit_created event
GET  /api/git/commit/{sha}/diff — commit metadata, files, stat, capped patch
POST /api/report               — generate + write + commit REPORT.md
GET  /                         — frontend SPA (served from frontend/dist/)
GET  /{path}                   — static assets or SPA fallback
"""

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from openfde import agent_settings as agent_settings_mod
from openfde import fs_watch
from openfde import semantic_graph as semantic_graph_mod
from openfde import watch_function
from openfde.agent_runner import build_system_prompt, run_agent
from openfde.anthropic_transport import make_transport
from openfde.claude_code_runner import cli_available as claude_cli_available, run_claude_code, run_claude_code_text
from openfde.codex_local_runner import cli_available as codex_cli_available, run_codex_local_edit, run_codex_local_text
from openfde.echo_transport import make_echo_transport
from openfde.openai_transport import complete as llm_complete, make_transport as make_openai_transport
from openfde.council import run_council
from openfde import council_context as council_context_mod
from openfde import council_router as council_router_mod
from openfde import session as session_mod
from openfde import boot_cache as boot_cache_mod
from openfde import story_cache as story_cache_mod
from openfde import __version__ as _OPENFDE_VERSION
from openfde.architect import analyze_repo, generate_canvas_state
from openfde.explain import explain_selection
from openfde.story import build_story
from openfde.prompt_story import build_prompt_graph
from openfde import failure_flow as failure_flow_mod
from openfde import feedback as feedback_mod
from openfde import plugins as plugins_mod
from openfde import focus as focus_mod
from openfde import issue_repro as issue_repro_mod
from openfde import source_edit
from openfde.episode_summary import (commit_display, is_bad_title, reconcile_intent_tasks,
                                     reconcile_task_status, repair_episode_tasks,
                                     repair_task_commit_shas, sync_intent_tasks)
from openfde.issue_intents import gh_issue_list, gh_issue_view, normalize_issue, upsert_intent_task
from openfde import verify as verify_mod
from openfde import prs as prs_mod
from openfde.prs import create_episode_pr, pr_readiness
from openfde import episode_llm_summary
from openfde import episode_commits as episode_commits_mod
from openfde.box_spec import apply_workflow_result, update_box_specs_from_execute
from openfde.execution import ACTIVE_DEFAULT, compile_workflow, is_valid_backend, list_backends
from openfde.git_timeline import changed_paths, commit_files, ensure_baseline, git_commit, git_diff, git_status, git_timeline, worktree_diff, worktree_impact
from openfde.report import generate_report
from openfde.spec import compile_spec
from openfde.intent_graph import (
    GENERATED_WORKSPACE,
    architecturize_intent_box,
    attribute_intent_files,
    is_intent_box,
    merge_step_files,
    render_intent_brief,
    resolve_run_scope,
)
from openfde.workflow_result import commit_message, source_files, tests_summary, validate_result
from openfde.filetree import build_file_tree
from openfde.persistence import Persistence
from openfde.plan import generate_plan

logger = logging.getLogger("openfde.server")


# ------------------------------------------------------------------ #
#  Run cancellation (Step 33)                                          #
# ------------------------------------------------------------------ #

class CancelToken:
    """Per-run cancellation handle. `is_set()` is polled by the in-process agent
    runner between turns; `proc` (if set by the Claude Code runner) is the live
    subprocess so cancel can terminate it. Thread-safe (Event)."""

    def __init__(self):
        self._ev = threading.Event()
        self.proc = None

    def is_set(self):
        return self._ev.is_set()

    def set_proc(self, p):
        self.proc = p

    def cancel(self):
        self._ev.set()
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass


# runId → CancelToken, for in-flight council runs only.
_RUN_CONTROLS = {}


# ------------------------------------------------------------------ #
#  CORS middleware                                                      #
# ------------------------------------------------------------------ #

@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.Response:
    """Add CORS headers to every REST response; bypass for WebSocket upgrades.

    Args:
        request: web.Request — incoming aiohttp request
        handler — next handler in the middleware chain

    Returns:
        web.Response — response with Access-Control-Allow-Origin: * added
    """
    if request.method == "OPTIONS":
        return web.Response(
            headers={
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )
    # Skip CORS header mutation for WebSocket upgrade requests
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await handler(request)
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ------------------------------------------------------------------ #
#  WebSocket connection manager                                         #
# ------------------------------------------------------------------ #

class ConnectionManager:
    """Tracks active WebSocket connections and provides server-side broadcast.

    All methods must be called from the asyncio event loop.

    Args:
        None
    """

    def __init__(self) -> None:
        """Initialize with an empty connection set."""
        self._sockets: set = set()

    async def connect(self, ws: web.WebSocketResponse) -> None:
        """Register a new connection.

        Args:
            ws: web.WebSocketResponse — the accepted WebSocket connection

        Returns:
            None
        """
        self._sockets.add(ws)

    def disconnect(self, ws: web.WebSocketResponse) -> None:
        """Remove a connection.

        Args:
            ws: web.WebSocketResponse — connection to remove

        Returns:
            None
        """
        self._sockets.discard(ws)

    async def broadcast(self, message: dict) -> None:
        """Send a JSON message to all connected clients.

        Dead connections are removed silently.

        Args:
            message: dict — JSON-serializable message payload

        Returns:
            None
        """
        if not self._sockets:
            return
        text = json.dumps(message)
        dead: set = set()
        for ws in list(self._sockets):
            try:
                if not ws.closed:
                    await ws.send_str(text)
                else:
                    dead.add(ws)
            except Exception:
                dead.add(ws)
        self._sockets -= dead

    @property
    def count(self) -> int:
        """Return the current number of connected clients.

        Returns:
            int — active connection count
        """
        return len(self._sockets)


# ------------------------------------------------------------------ #
#  PLAN.md writer                                                       #
# ------------------------------------------------------------------ #

def _write_plan_md(persistence: Persistence, repo_root: Path) -> None:
    """Generate and atomically write PLAN.md into `.openfde/` (never the repo root).

    Reads current state, tasks, and project from disk, generates markdown
    via generate_plan(), and writes atomically (tmp → rename) to
    ``persistence.dir / PLAN.md`` so watching a foreign repo never dirties its tree.

    Args:
        persistence: Persistence — the active persistence instance (its `.dir` is `.openfde/`)
        repo_root: Path — repository root (used only for logging/context)

    Returns:
        None
    """
    md = generate_plan(
        persistence.load_state(),
        persistence.load_tasks(),
        persistence.load_project(),
    )
    # PLAN.md is OpenFDE's OWN artifact — write it inside .openfde/ (excluded from
    # git), never the repo root, so watching a foreign repo never dirties it.
    plan_path = persistence.dir / "PLAN.md"
    tmp_path  = persistence.dir / ".plan_md.tmp"
    try:
        tmp_path.write_text(md, encoding="utf-8")
        os.replace(tmp_path, plan_path)
        logger.info("PLAN.md written")
    except OSError as exc:
        logger.error("Failed to write PLAN.md: %s", exc)


# ------------------------------------------------------------------ #
#  Server entry point                                                  #
# ------------------------------------------------------------------ #

async def _summarizer_loop(persistence, manager) -> None:
    """Best-effort LLM story summarizer — upgrades ONE eligible episode per cycle.

    Runs entirely off the request path: every ~25s it picks the newest episode still on a
    deterministic summary and shells out (in a thread) to the local Codex/Claude CLI to
    rewrite its title/summary/storyFacts. Cached per fingerprint + attempted at most once,
    so it converges and never loops on a failing call. No-op when no local CLI is available.
    """
    providers = episode_llm_summary.available_providers()
    if not providers:
        logger.info("LLM story summarizer: no local CLI provider — deterministic summaries only.")
        return
    logger.info("LLM story summarizer active (providers: %s)", ", ".join(providers))
    loop = asyncio.get_event_loop()
    while True:
        try:
            await asyncio.sleep(25)
            target = next((e for e in persistence.load_episodes() if episode_llm_summary.wants_llm(e)), None)
            if not target:
                continue
            tid = target["episodeId"]
            before = (target.get("summarySource"), target.get("title"))
            updated = await loop.run_in_executor(
                None,
                lambda: episode_llm_summary.ensure_facts(persistence, allow_llm=True, providers=providers, limit=1),
            )
            t = next((e for e in updated if e.get("episodeId") == tid), None)
            if t and (t.get("summarySource"), t.get("title")) != before:
                await manager.broadcast({"type": "episode_updated", "episode": t})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad tick must never kill the server
            logger.debug("summarizer tick failed", exc_info=True)


# Episode states that are a stable restore point (a warm cache stamped here is what the next boot
# restores from). Module-level so build_boot_payload is importable + unit-testable.
_TERMINAL_STATES = frozenset({"landed", "verified", "needs_manual_land", "blocked"})


def latest_terminal_tag(persistence) -> str:
    """Tag of the most recent terminal-state episode (or PR-created), for the 'Restored from P14'
    label. Cheap: a single load_episodes() read, never a scan."""
    for e in persistence.load_episodes():                # newest-first
        if e.get("status") in _TERMINAL_STATES or (e.get("pr") or {}).get("url"):
            return e.get("tag") or e.get("episodeId") or ""
    return ""


def build_boot_payload(path, persistence, started_at: str, version: str, *,
                       want_canvas: bool = False, identity: dict = None) -> dict:
    """The tiny first-paint payload — repo identity + the last warm snapshot + restored counts.

    CONTRACT: CACHE-ONLY. Reads the warm cache on disk + episode/task counts (+ a ~3 ms file-tree
    scan only when there is no cache yet). It must NEVER spawn git, run analyze_repo / backfill /
    the semantic graph / the memory-kit bootstrap — those are background jobs. ``identity``
    (repoName/branch/gitRoot) is resolved ONCE at server start and passed in, so boot spawns no git
    subprocess and stays sub-second even on a loaded machine; the frontend re-fetches /api/archgraph
    on every boot, so the snapshot is an instant placeholder, not the source of truth. Top-level +
    dependency-light so a test can patch the heavy functions to blow up and assert boot still works.
    """
    warm = boot_cache_mod.read_warm(persistence.openfde_dir) or {}
    meta = warm.get("meta") or {}
    cached_tree = warm.get("fileTree")
    tree = cached_tree if cached_tree is not None else build_file_tree(path)   # ~3 ms scan if no cache
    ident = identity or session_mod.session_payload(path, started_at, version)
    episodes = persistence.load_episodes()
    return {
        "ok": True,
        "repoName": ident.get("repoName"), "branch": ident.get("branch"),
        "gitRoot": ident.get("gitRoot"), "openfdeVersion": version,
        "fileTree": tree,
        "canvasSnapshot": warm.get("arch") if want_canvas else None,
        "hasSnapshot": bool(warm.get("arch")),
        "restoredFrom": meta.get("episodeTag") or latest_terminal_tag(persistence),
        "stale": False,            # cache-only boot: the live /api/archgraph fetch refreshes the canvas
        "generatedAt": meta.get("generatedAt") or "",
        "episodeCount": len(episodes),
        "taskCount": len(persistence.load_tasks() or []),
        "candidateCount": persistence.backfill_candidate_count(),
        "restorePath": "warm-cache" if cached_tree is not None else "cheap-scan",   # for the timing log
    }


def _rail_chip(e: dict) -> dict:
    """One TINY rail chip from a persisted episode — fixed-size, no full prompt/files/concepts."""
    shas = e.get("commitShas") or []
    cm = e.get("commitMeta") or {}
    return {
        "episodeId": e.get("episodeId"), "tag": e.get("tag"), "sequence": e.get("sequence"),
        "title": e.get("title"), "status": e.get("status"), "kind": e.get("kind"),
        "signal": e.get("signal"), "createdAt": e.get("createdAt"), "updatedAt": e.get("updatedAt"),
        "fileCount": len(e.get("files") or []), "commitCount": len(shas), "commitShas": shas,
        "commits": [{"sha": s, "shortSha": (s or "")[:7],
                     "displayTitle": (cm.get(s) or {}).get("title") or (e.get("title") or "")}
                    for s in shas],
        # operational flag only (drives the OpenPM filter) — not the full concept list.
        "storyFacts": {"operational": bool((e.get("storyFacts") or {}).get("operational"))},
        "prReadiness": None,
    }


def _rail_order_key(e: dict):
    """Deterministic prompt-rail order — by ``sequence`` (assigned monotonically per repo), falling
    back to ``createdAt`` ONLY when sequence is missing. Use with ``reverse=True`` → newest first.
    NEVER ``updatedAt`` (the summarizer / hydration / reconciliation rewrite it) and NEVER the
    persisted file order (a reconciliation upsert reorders the store). The single source of rail
    order so the boot rail and the full rail always agree."""
    return (e.get("sequence") or 0, e.get("createdAt") or "")


def build_rail_payload(persistence, *, limit=None) -> dict:
    """CHEAP prompt-rail payload — the default /api/review/episodes, safe to poll often.

    CONTRACT: TINY CHIP data only, from persisted state. Each episode contributes a small,
    fixed-size chip (id/tag/title/status/counts + commit chips with cached titles) — NOT the whole
    episode object (no full prompt text, no files arrays, no storyFacts concepts, no verify/PR
    detail), so the 15s poll ships ~tens of KB instead of ~440 KB. It must NEVER run git, ensure_facts,
    reconciliation, or readiness; the full detail (files, readiness, Outside bucket, concepts) loads
    on demand via /api/review/episodes/full. Top-level + dependency-light so a test can patch the
    git/reconcile/readiness seams to explode and prove the rail never touches them.

    ``limit`` → **BOOT mode** (``?mode=boot&limit=10``): the latest ``limit`` chips only (episodes are
    newest-first), so first paint renders ~10 chips instead of all ~115. Boot is **never** the
    authoritative empty (``confirmed: False``); the full rail (``limit=None``) is, so the UI may show
    an empty rail only after that. ``totalCount`` lets the UI know more chips are hydrating.
    """
    persistence.backfill_episode_meta()                    # tag/title/seq — deterministic, no git
    # Deterministic order (sequence desc) for BOTH boot and full — never the store's file order,
    # which a reconciliation upsert reorders. Boot then takes the latest N off the top.
    eps = sorted(persistence.load_episodes(), key=_rail_order_key, reverse=True)
    total = len(eps)
    boot = limit is not None
    if boot:
        eps = eps[:max(0, int(limit))]
    return {
        "ok": True,
        "episodes": [_rail_chip(e) for e in eps],
        "confirmed": not boot,                             # full rail is authoritative; boot is not
        "cached": boot,
        "totalCount": total,
        # Boot drops the Outside bucket (a full-rail / git concern); full rail still omits it here
        # (it is hydrated by /api/review/episodes/full) but keeps the empty shape for back-compat.
        "outside": {"episodeId": "outside", "kind": "manual", "status": "landed",
                    "prompt": "Outside OpenFDE", "summary": "", "commits": [],
                    "commitCount": 0, "files": [], "fileCount": 0},
    }


def build_boot_canvas(persistence) -> dict:
    """Cache-only FIRST-PAINT hydration, in ONE small call: the persisted canvas (the boxes+arrows
    that ARE the architecture modules — the canvas is empty without them) + the cached file tree
    (Explorer). Pure disk reads — NEVER git, analyze_repo, or a file-tree scan. Deliberately does
    NOT ship the full ~1.5 MB ArchGraph: the modules render from boxes alone, so the heavy arch
    (box-internal file/function detail) loads separately via the gated /api/archgraph AFTER first
    paint. ``hasSnapshot`` still tells the UI a warm arch exists. ``boxes`` is empty only before the
    repo has ever been scanned. Top-level + dependency-light so a test can patch the heavy seams to
    explode and prove first paint never touches them.
    """
    warm = boot_cache_mod.read_warm(persistence.openfde_dir) or {}
    state = persistence.load_state() or {}
    return {"ok": True,
            "boxes": state.get("boxes") or [],
            "arrows": state.get("arrows") or [],
            "fileTree": warm.get("fileTree"),
            "hasCanvas": bool(state.get("boxes")),
            "hasSnapshot": bool(warm.get("arch"))}


def create_council_handoff(body, *, persistence, agent_states=None):
    """Core of ``POST /api/council/implementation`` — the 'Start implementation' affordance.

    Creates a SAFE, VISIBLE implementation handoff from a role-led council brief. It is READ-ONLY with
    respect to repo files: it re-validates the gate server-side (never trusting the client to bypass
    escalation / lead rules), persists a pending handoff record + a compact confirmation chat turn, and
    returns ``(status, payload)``. It does NOT dispatch a file-editing run — that path
    (``/api/council/run``) requires an explicit canvas scope with dotted/solid permissions a chat brief
    does not carry. A future slice carries the pending handoff into that scoped run.

    Module-level + persistence-injected (like :func:`build_boot_payload`) so the endpoint's behavior is
    directly testable without a live server.

    Args:
        body: dict | None — parsed request body {question, brief?}; None signals invalid JSON.
        persistence: the active Persistence (its handoff/chat stores are written here).
        agent_states: dict | None — council agent states for routing (read-only).

    Returns:
        (int, dict) — HTTP status + JSON body.
    """
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid JSON"}
    question = (body.get("question") or "").strip()
    if not question:
        return 400, {"ok": False, "error": "question required"}
    client_brief = body.get("brief") if isinstance(body.get("brief"), dict) else {}
    client_sections = client_brief.get("sections") if isinstance(client_brief.get("sections"), dict) else None

    # Re-derive the gate server-side from the QUESTION (route + escalation). Section display text may
    # come from the client (low risk — it only feeds the prompt), but whether a handoff is ALLOWED is
    # decided here, authoritatively. No section_filler → no LLM calls on this path.
    decision = council_router_mod.route(question, "auto", agent_states or {})
    brief = council_router_mod.role_led_brief(question, decision=decision, sections=client_sections)
    if not brief.get("canStartImplementation"):
        esc = brief.get("humanEscalation") or {}
        reason = esc.get("reason") or ("a readiness/Verifier brief does not start implementation — "
                                       "ask a product or implementation question to plan a change")
        return 409, {"ok": False, "error": "implementation handoff not allowed",
                     "reason": reason, "humanEscalation": esc}

    # Active-episode context for the handoff prompt (best-effort; the handoff is still valid without it).
    ep = persistence.latest_active_episode() or {}
    ep_ctx, ep_id = "", None
    if isinstance(ep, dict):
        ep_ctx = (ep.get("title") or ep.get("summary") or ep.get("intent") or ep.get("prompt") or "").strip()
        ep_id = ep.get("episodeId")
    prompt = council_router_mod.build_handoff_prompt(
        question, brief["leadRole"], brief["sections"], episode=ep_ctx)

    ts = datetime.now(timezone.utc).isoformat()
    handoff = {
        "id": "handoff_" + secrets.token_hex(5),
        "status": "pending",
        "question": question,
        "leadRole": brief["leadRole"],
        "sections": brief["sections"],
        "prompt": prompt,
        "activeEpisodeId": ep_id,
        "ts": ts,
    }
    # HONEST copy: a handoff was CREATED (a scoped record), not an implementation "started" — no run
    # has been dispatched. Only a real /api/council/run start would warrant "started".
    message = f"Implementation handoff created. (handoff {handoff['id']})"
    try:
        persistence.append_council_handoff(handoff)
        # Persist a compact confirmation turn so the result survives a browser refresh.
        persistence.append_council_chat([
            {"role": "assistant", "text": message, "label": "OpenFDE", "ts": ts},
        ])
    except Exception:  # noqa: BLE001
        logger.warning("could not persist council handoff")
    return 200, {"ok": True, "handoff": handoff, "message": message}


async def start(repo_path: str, port: int = 7373, auto_open: bool = True) -> None:
    """Start the OpenFDE server for the given repository path.

    Configures logging, creates the .openfde/ directory, registers all
    routes (WebSocket, REST, static SPA), and runs until interrupted.

    Args:
        repo_path: str — path to the repository to watch; created if absent
        port: int — TCP port to listen on (default: 7373)
        auto_open: bool — open the IDE URL in the default browser after 500 ms

    Returns:
        None
    """
    # ---- logging ---------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # ---- repo path -------------------------------------------------------
    path = Path(repo_path).expanduser().resolve()

    # ---- wrong-repo startup trust ---------------------------------------
    # Never silently serve (or fail-to-bind into) another repo. If an OpenFDE server
    # already holds this port, identify the repo it watches and act on the truth:
    # same canonical repo → say "already running"; a DIFFERENT repo → refuse loudly and
    # do NOT open the browser into it. (Falls back to /api/files for pre-/api/session servers.)
    existing = session_mod.probe_openfde_repo(port)
    verdict, detail = session_mod.port_collision_verdict(existing, path)
    if verdict == "already_running":
        url = f"http://localhost:{port}"
        print(f"\n  OpenFDE is already watching {detail} on {url}\n")
        if auto_open:
            webbrowser.open(url)
        return
    if verdict == "wrong_repo":
        print(f"\n  ✗  Port {port} is already watching {detail}.\n"
              f"     Stop it (Ctrl-C in that terminal) or choose another port:\n"
              f"     openfde watch {path} --port <other>\n")
        return

    server_started_at = datetime.now(timezone.utc).isoformat()
    path.mkdir(parents=True, exist_ok=True)

    openfde_dir = path / ".openfde"
    openfde_dir.mkdir(exist_ok=True)

    # One watcher per repo. Two live processes (the restart-overlap window) tore
    # episodes.json via a shared tmp and captured duplicate episode pairs — refuse
    # loudly instead. Stale locks (dead pid) are swept automatically.
    from openfde.instance_lock import WatchLockHeld, acquire_watch_lock, release_watch_lock
    try:
        watch_lock = acquire_watch_lock(openfde_dir)
    except WatchLockHeld as exc:
        logging.getLogger("openfde").error("%s", exc)
        return

    _boot_t0    = time.perf_counter()        # for the "server bound in Xms" startup-timing log
    persistence = Persistence(openfde_dir)
    manager     = ConnectionManager()

    # One-time migration BEFORE the server serves: move low-confidence backfill (discussion /
    # needs_review transcript fragments) out of episodes.json into backfill_candidates.json and
    # renumber the real episodes P1..PN. Idempotent + cheap, so the first paint shows clean prompt
    # numbers (P<n> = a real prompt) instead of inflated P1138-style tags from quarantined noise.
    try:
        _q = persistence.quarantine_backfill_pollution()
        if _q.get("quarantined"):
            logger.info("quarantined %d backfill candidate(s); %d real episode(s) renumbered P1..P%d",
                        _q["quarantined"], _q["real"], _q["real"])
    except Exception:  # noqa: BLE001 — never let migration block the watcher
        logger.warning("backfill quarantine migration failed", exc_info=True)

    logger.info("Watching: %s", path)

    # ---- locate frontend/dist -------------------------------------------
    pkg_root = Path(__file__).parent.parent   # repo root when running from source
    dist_dir = pkg_root / "frontend" / "dist"

    # ---- aiohttp application ---------------------------------------------
    app = web.Application(middlewares=[cors_middleware])

    # ================================================================== #
    #  WebSocket — GET /ws                                                #
    # ================================================================== #

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        """Accept a WebSocket connection, send hello, and relay broadcasts.

        Incoming client messages are ignored in v1 (the server is write-only
        for now).  The connection is kept alive until the client disconnects
        or an error occurs.

        Args:
            request: web.Request — upgrade request from the client

        Returns:
            web.WebSocketResponse — the completed WebSocket session
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await manager.connect(ws)
        logger.info("WebSocket connected (clients: %d)", manager.count)

        try:
            await ws.send_json({"type": "hello", "version": "0.1.0"})
            async for msg in ws:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
                # TEXT messages from client are accepted but ignored in v1
        except Exception:
            pass
        finally:
            manager.disconnect(ws)
            logger.info("WebSocket disconnected (clients: %d)", manager.count)

        return ws

    # ================================================================== #
    #  REST — /api/files                                                  #
    # ================================================================== #

    async def get_files(request: web.Request) -> web.Response:
        """Return the recursive file tree for the watched repo.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON root directory tree node
        """
        tree = build_file_tree(path)
        return web.json_response(tree)

    # ================================================================== #
    #  REST — /api/state                                                  #
    # ================================================================== #

    async def get_state(request: web.Request) -> web.Response:
        """Return the persisted canvas state.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON {boxes: [...], arrows: [...]}
        """
        return web.json_response(persistence.load_state())

    async def get_boot_canvas(request: web.Request) -> web.Response:
        """First-paint hydration: persisted canvas boxes/arrows + warm arch + cached file tree, in
        ONE cache-only call. Runs on boot's dedicated pool so it never queues behind Story/git, and
        never analyzes or scans — the cockpit shows its modules + Explorer instantly."""
        loop = asyncio.get_event_loop()
        return web.json_response(
            await loop.run_in_executor(_boot_pool, lambda: build_boot_canvas(persistence)))

    async def put_state(request: web.Request) -> web.Response:
        """Persist canvas state and regenerate PLAN.md.

        Side effects: writes state.json, writes PLAN.md, broadcasts state_updated.

        Args:
            request: web.Request — body: JSON {boxes, arrows}

        Returns:
            web.Response — JSON {ok: true}
        """
        data = await request.json()
        # Skip the write (and the PLAN.md regen) when nothing semantically
        # changed — a fresh hydrate-then-persist on reload must not dirty the
        # repo.
        current = persistence.load_state()
        incoming = {"boxes": data.get("boxes", []), "arrows": data.get("arrows", [])}
        if incoming == {"boxes": current.get("boxes", []), "arrows": current.get("arrows", [])}:
            return web.json_response({"ok": True, "unchanged": True})
        persistence.save_state(data)
        _write_plan_md(persistence, path)
        await manager.broadcast({"type": "state_updated"})
        return web.json_response({"ok": True})

    # ================================================================== #
    #  REST — /api/tasks                                                  #
    # ================================================================== #

    async def get_tasks(request: web.Request) -> web.Response:
        """Return the persisted OpenPM task list, healing stale episode-commit cards.

        Older OpenPM cards may have captured the raw commit subject ("Here's the CC prompt")
        as their title. We repair those **server-side** from the owning (cleaned) episode and
        persist the fix — so it's durable and immune to frontend hydration order (the reducer
        self-heal alone loses the race against `HYDRATE_TASKS` on reload).

        Returns:
            web.Response — JSON array of task objects (repaired in place when needed).
        """
        episodes = episode_llm_summary.ensure_facts(persistence)   # ensure clean episode titles first
        tasks = persistence.load_tasks()
        repaired, changed = repair_episode_tasks(tasks, episodes)
        # Heal stale commit mappings: a card showing a commit its owning episode no longer claims is
        # repaired from episode truth (episode commitShas win) — so a re-attributed/dropped commit
        # can't keep showing on the wrong card.
        repaired, c2 = repair_task_commit_shas(repaired, episodes)
        changed = changed or c2
        # Intent-graph runs: the step cards are the source of truth. Heal any step card whose
        # files/commitSha a UI hydration dropped (from episode truth), and drop a redundant
        # episode/commit card that duplicates an episode already covered by step cards — so opening
        # OpenPM can neither erase receipts nor leave a 6th card behind.
        repaired, c3 = reconcile_intent_tasks(repaired, episodes)
        changed = changed or c3
        # Make every episode card mirror its episode's CURRENT verify/landed state
        # — no stale FAILED next to a passed episode (one source of truth).
        if reconcile_task_status(repaired, episodes):
            changed = True
        if changed:
            persistence.save_tasks(repaired)
        return web.json_response(repaired)

    async def put_tasks(request: web.Request) -> web.Response:
        """Persist the task list and regenerate PLAN.md.

        Side effects: writes tasks.json, writes PLAN.md, broadcasts tasks_updated.

        Args:
            request: web.Request — body: JSON array of task objects

        Returns:
            web.Response — JSON {ok: true}
        """
        data = await request.json()
        if data == persistence.load_tasks():
            return web.json_response({"ok": True, "unchanged": True})
        persistence.save_tasks(data)
        _write_plan_md(persistence, path)
        await manager.broadcast({"type": "tasks_updated"})
        return web.json_response({"ok": True})

    async def post_sketch_demo(request: web.Request) -> web.Response:
        """Local, demo-only: load a deterministic Sketch-First fixture so a fresh user instantly sees
        the v3 canvas surfaces (``✓ BUILT`` / ``BECAME`` / intent→file highlight) without depending on
        stale manual canvas state — three connected intent boxes + a module, each carrying an
        ``implementationFiles`` link that drives file-level BECAME via v3's graceful path.

        **Side-effect-free + instant:** writes NO repo files and triggers NO archGraph rescan or review
        reassimilation (only ``.openfde/state.json``, which the analyzer skips) — so it returns in well
        under a second on any repo. Function-rich BECAME stays reserved for real runs, where the council
        writes real files that assimilation parses for symbols.

        **Non-destructive:** REFUSES (409) when the canvas already has boxes — it never overwrites real
        work, so hitting it on a live instance is safe. Reload the canvas after to see it.
        """
        if persistence.load_state().get("boxes"):
            return web.json_response({"ok": False, "error":
                "Canvas is not empty — the Sketch-First demo refuses to overwrite real work. "
                "Clear the canvas or use a fresh instance."}, status=409)
        from openfde import sketch_demo
        # Pure canvas state only — NO repo file write, NO git, NO archGraph rebuild. The boxes carry
        # implementationFiles, so the canvas shows ✓ BUILT + file-level BECAME via v3's graceful path
        # without an assimilation pass. save_state writes only .openfde/state.json (skipped by the
        # analyzer), so nothing here triggers a rescan or reassimilation.
        demo = sketch_demo.sketch_first_demo_state()
        persistence.save_state(demo)
        logger.info("Loaded Sketch-First demo: %d box(es), %d arrow(s) (file-level, no file write)",
                    len(demo["boxes"]), len(demo["arrows"]))
        await manager.broadcast({"type": "state_updated", "payload": {"reason": "sketch_demo"}})
        return web.json_response({"ok": True, **demo})

    async def post_saas_demo(request: web.Request) -> web.Response:
        """Local, demo-only: seed a realistic, product-shaped SaaS example — an "AI support inbox" —
        as five connected **planned** intent steps (ingest → classify → draft → review → log).

        Unlike the Sketch-First fixture, these boxes are PLANNED (no implementationFiles): the example
        is meant to be RUN, so the user selects the steps, presses Run, and the Agent Council grounds
        them into files in place — exercising the real loop (intent → Run → architecture/files →
        OpenPM tasks → episode/commit → Story). Seeding itself is pure canvas state — NO repo file
        write, NO scan (only ``.openfde/state.json``). REFUSES (409) when the canvas already has boxes
        so it never overwrites real work.
        """
        if persistence.load_state().get("boxes"):
            return web.json_response({"ok": False, "error":
                "Canvas is not empty — the support-inbox example refuses to overwrite real work. "
                "Clear the canvas or use a fresh instance."}, status=409)
        from openfde import saas_demo
        demo = saas_demo.support_inbox_demo_state()
        persistence.save_state(demo)
        logger.info("Loaded support-inbox SaaS example: %d planned step(s), %d arrow(s)",
                    len(demo["boxes"]), len(demo["arrows"]))
        await manager.broadcast({"type": "state_updated", "payload": {"reason": "saas_demo"}})
        return web.json_response({"ok": True, **demo})

    # ================================================================== #
    #  REST — /api/issues/github  (durable intent v1)                     #
    # ================================================================== #
    # A GitHub Issue is intent BEFORE the episode: importing one creates an
    # OpenPM To Do card carrying `intentSource` — it becomes Story memory only
    # when an episode/commit lands. v1 rides the local `gh` CLI (no OAuth).

    async def get_github_issues(request: web.Request) -> web.Response:
        """List open GitHub issues for this repo via `gh issue list`.

        Returns:
            web.Response — {ok, issues: [normalized intents]} — or {ok: false,
                error} when gh is missing/unauthenticated (soft failure: the UI
                shows the message; nothing breaks without gh).
        """
        loop = asyncio.get_event_loop()
        try:
            issues = await loop.run_in_executor(None, lambda: gh_issue_list(str(path)))
            return web.json_response({"ok": True, "issues": issues})
        except FileNotFoundError:
            return web.json_response({"ok": False, "error": "gh CLI not installed"})
        except Exception as exc:  # noqa: BLE001 — gh/auth/JSON problems are soft here
            return web.json_response({"ok": False, "error": str(exc)[:300]})

    async def post_github_issue_import(request: web.Request) -> web.Response:
        """Import one GitHub issue as a durable-intent OpenPM card (idempotent).

        Body: {issueNumber: 42} → fetched via `gh issue view`, or
              {issue: {...raw issue JSON...}} → normalized directly (no gh needed).

        Side effects: upserts the OpenPM card (To Do; board state preserved on
        re-import), writes tasks.json, broadcasts tasks_updated.

        Returns:
            web.Response — {ok, task, created} | {ok: false, error} (400 for a
                malformed payload, 502 when gh itself fails).
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = {}
        try:
            if isinstance(body.get("issue"), dict):
                intent = normalize_issue(body["issue"])
            elif body.get("issueNumber") is not None:
                num = int(body["issueNumber"])
                loop = asyncio.get_event_loop()
                intent = await loop.run_in_executor(
                    None, lambda: gh_issue_view(num, str(path)))
            else:
                return web.json_response(
                    {"ok": False, "error": "provide issueNumber or issue"}, status=400)
        except ValueError as exc:                       # malformed payload/number
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except FileNotFoundError:
            return web.json_response(
                {"ok": False, "error": "gh CLI not installed"}, status=502)
        except Exception as exc:  # noqa: BLE001 — gh exec/auth/JSON failure
            return web.json_response({"ok": False, "error": str(exc)[:300]}, status=502)
        tasks = persistence.load_tasks()
        tasks, task, created = upsert_intent_task(tasks, intent)
        persistence.save_tasks(tasks)
        await manager.broadcast({"type": "tasks_updated"})
        return web.json_response({"ok": True, "task": task, "created": created})

    async def post_issue_reproduce(request: web.Request) -> web.Response:
        """The Reproduce button: issue card → honest repro verdict.

        Re-fetches the LIVE issue (text may have changed since import), then
        triage → locate → draft → single-test run via openfde.issue_repro.
        Feature requests and signal-free reports come back refused, never
        fabricated. Run-once by issue-body hash; {regenerate: true} re-runs.
        The verdict is stored on the card (task.repro) and broadcast.

        Returns:
            web.Response — {ok, repro, reused} | 4xx/502.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = {}
        task_id = str(body.get("taskId") or "")
        task = next((t for t in persistence.load_tasks()
                     if isinstance(t, dict) and t.get("id") == task_id), None)
        if task is None:
            return web.json_response({"ok": False, "error": "unknown task"}, status=404)
        src = task.get("intentSource") or {}
        if src.get("provider") != "github" or src.get("issueNumber") is None:
            return web.json_response(
                {"ok": False, "error": "not a GitHub-issue card"}, status=400)

        loop = asyncio.get_event_loop()
        try:
            intent = await loop.run_in_executor(
                None, lambda: gh_issue_view(int(src["issueNumber"]), str(path)))
        except FileNotFoundError:
            return web.json_response(
                {"ok": False, "error": "gh CLI not installed"}, status=502)
        except Exception as exc:  # noqa: BLE001 — gh exec/auth failure
            return web.json_response({"ok": False, "error": str(exc)[:300]}, status=502)

        bhash = issue_repro_mod.issue_body_hash(intent.get("body") or "")
        prior = task.get("repro") or {}
        if prior and prior.get("bodyHash") == bhash and not body.get("regenerate"):
            return web.json_response({"ok": True, "repro": prior, "reused": True})

        caller, caption = _hatch_text_role("senior_dev")
        checks = verify_mod.discover_checks(path)
        check_cmd = next((c["command"] for c in checks if c.get("id") == "unit-tests"),
                         checks[0]["command"] if checks else None)

        # The repro belongs to a WORK EPISODE carrying the ISSUE as its intent —
        # created right before the test write (the watcher then attributes the
        # edit to it), so the whole existing loop (verification → Show → the
        # hatch with explain/prompt/flow) anchors on that episode unchanged.
        created = {}

        def _bootstrap_episode():
            now = datetime.now(timezone.utc).isoformat()
            ep = {"episodeId": "episode_" + secrets.token_hex(6),
                  "createdAt": now, "updatedAt": now,
                  "prompt": (f"GitHub issue #{src['issueNumber']} — "
                             f"{intent.get('title') or ''}\n\n"
                             f"{(intent.get('body') or '')[:2000]}"),
                  "title": (intent.get("title") or "")[:90],
                  "kind": "issue-repro", "status": "reviewing",
                  "runIds": [], "eventIds": [], "projectEntryIds": [],
                  "commitShas": [], "files": [],
                  "summary": f"Reproduction of GitHub issue #{src['issueNumber']}",
                  "intentSource": src}
            created.update(persistence.upsert_episode(ep))
            return created["episodeId"]

        verdict = await loop.run_in_executor(None, lambda: issue_repro_mod.reproduce_issue(
            path, title=intent.get("title") or "", body=intent.get("body") or "",
            labels=intent.get("labels") or [], caller=caller, check_cmd=check_cmd,
            before_write=_bootstrap_episode))
        verdict["tail"] = (verdict.get("tail") or "")[-400:]
        verdict["bodyHash"] = bhash
        verdict["source"] = caption or "OpenFDE · triage"
        verdict["ts"] = int(time.time())

        task_patch = {"repro": verdict}
        if created and verdict.get("verdict") == "reproduced":
            # Real receipts on the episode: the full check run (the repro test
            # fails inside it) attaches exactly like a manual Run checks.
            evidence = await loop.run_in_executor(
                None, lambda: verify_mod.run_verification(path))
            persistence.save_verify_latest(evidence)
            ep = persistence.get_episode(created["episodeId"])
            if ep is not None:
                ep["verify"] = evidence
                files = set(ep.get("files") or [])
                files.add(verdict.get("testFile") or "")
                ep["files"] = sorted(f for f in files if f)
                ep["updatedAt"] = datetime.now(timezone.utc).isoformat()
                persistence.upsert_episode(ep)
                await manager.broadcast({"type": "episode_updated", "episode": ep})
                task_patch.update({"episodeId": ep["episodeId"],
                                   "episodeTag": ep.get("tag"),
                                   "promptTitle": ep.get("title"),
                                   "column": "doing"})
        elif created:
            # The hook fired but the run didn't reproduce — keep the episode as
            # the honest record of the attempt (status stays reviewing; no files).
            task_patch.update({"episodeId": created.get("episodeId"),
                               "episodeTag": created.get("tag"),
                               "column": "doing"})

        persistence.update_task(task_id, task_patch)
        await manager.broadcast({"type": "tasks_updated"})
        return web.json_response({"ok": True, "repro": verdict, "reused": False})

    # ================================================================== #
    #  REST — Land as PR v1 (episode → branch → GitHub PR via local gh)   #
    # ================================================================== #

    async def get_episode_pr_readiness(request: web.Request) -> web.Response:
        """Fresh, read-only ready-for-PR verdict for one episode (the episode card
        re-checks on open so the embedded payload's worktree state can't go stale).

        Returns:
            web.Response — {ok, episodeId, readiness} | 404 unknown episode.
        """
        eid = request.match_info.get("episodeId", "")
        ep = persistence.get_episode(eid)
        if ep is None:
            return web.json_response({"ok": False, "error": "unknown episode"}, status=404)
        loop = asyncio.get_event_loop()
        readiness = await loop.run_in_executor(None, lambda: pr_readiness(path, ep))
        return web.json_response({"ok": True, "episodeId": eid, "readiness": readiness})

    async def post_episode_pr(request: web.Request) -> web.Response:
        """Open a GitHub PR for a landed episode — the PR body IS the episode's
        story + Verify receipts. Manual action only (nothing auto-PRs in v1).

        Guardrails live in ``prs.create_episode_pr``: clean worktree required,
        idempotent on an existing ``episode.pr``, structured errors when gh is
        missing/unauthenticated or push/create fails (episode left untouched).

        Returns:
            web.Response — {ok, pr, episodeId, existing} | {ok: false, error, …}
                (soft 200 so the Review UI can show the error inline; 404 only
                for an unknown episode).
        """
        eid = request.match_info.get("episodeId", "")
        ep = persistence.get_episode(eid)
        if ep is None:
            return web.json_response({"ok": False, "error": "unknown episode"}, status=404)
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: create_episode_pr(path, ep))
        if not res.get("ok"):
            return web.json_response({"ok": False, "error": res.get("error") or "PR failed",
                                      **{k: res[k] for k in ("branch", "pushed") if k in res}})
        if not res.get("existing"):
            ep["pr"] = res["pr"]
            ep["updatedAt"] = datetime.now(timezone.utc).isoformat()
            persistence.upsert_episode(ep)
            await manager.broadcast({"type": "episode_updated", "episode": ep})
        return web.json_response({"ok": True, "pr": res["pr"], "episodeId": eid,
                                  "existing": bool(res.get("existing"))})

    # ================================================================== #
    #  REST — /api/verify  (Verify Gate Evidence v1 — local receipts)     #
    # ================================================================== #

    async def get_verify_status(request: web.Request) -> web.Response:
        """The discovered checks + the latest worktree-level evidence (no run).

        Returns:
            web.Response — {ok, checks: [{id,label,command,required}], latest}.
        """
        checks = [{"id": c["id"], "label": c["label"], "command": " ".join(c["command"]),
                   "required": c["required"]} for c in verify_mod.discover_checks(path)]
        return web.json_response({"ok": True, "checks": checks,
                                  "latest": persistence.load_verify_latest()})

    async def get_source_slice(request: web.Request) -> web.Response:
        """A line slice of a repo file — the repair hatch's READ.

        Query: path (repo-relative), start, end (1-based inclusive; end optional).
        The hatch is function-scoped: the UI resolves the range from the
        ArchGraph and only ever asks for one function.

        Returns:
            web.Response — {ok, path, start, end, total, code} | 400 {error}.
        """
        q = request.rel_url.query
        try:
            return web.json_response({"ok": True, **source_edit.read_slice(
                path, q.get("path", ""), int(q.get("start", 1)), int(q.get("end", 0)))})
        except (source_edit.SourceEditError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def post_source_patch(request: web.Request) -> web.Response:
        """Splice a replacement into a repo file — the repair hatch's WRITE (⌘S).

        Body: {path, start, end, code}. The edit hits the worktree like any
        other edit, so the watcher attributes it to the live episode — repairs
        are recorded with the same zero ceremony as the work itself.

        Returns:
            web.Response — {ok, path, start, end, total} | 400 {error}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        try:
            result = source_edit.splice_lines(path, str(body.get("path", "")),
                                              int(body.get("start", 0)),
                                              int(body.get("end", 0)),
                                              str(body.get("code", "")))
        except (source_edit.SourceEditError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        ep_id = (body.get("episodeId") or "").strip() if isinstance(body, dict) else ""
        if ep_id:
            reopened = persistence.reopen_episode(ep_id)
            if reopened is not None:
                await manager.broadcast({"type": "episode_updated", "episode": reopened})
            if persistence.move_tasks_for_episode(ep_id, "doing", from_columns=("todo",)):
                await manager.broadcast({"type": "tasks_updated"})
            ep = persistence.get_episode(ep_id)
            rel = body.get("path")
            if ep is not None and rel and rel not in (ep.get("files") or []):
                ep["files"] = sorted({*(ep.get("files") or []), rel})
                persistence.upsert_episode(ep)
                await manager.broadcast({"type": "episode_updated", "episode": ep})
        await manager.broadcast({"type": "source_patched", "payload": result})
        return web.json_response({"ok": True, **result})

    async def post_verify_run(request: web.Request) -> web.Response:
        """Run the repo's local checks now and store the evidence.

        Body (optional): {episodeId} — also attach the evidence to that episode
        (it then rides /api/review/episodes into Review/OpenPM surfaces).

        Side effects: writes verify_latest.json; upserts the episode when given;
        broadcasts verify_updated (+ episode_updated when attached).

        Returns:
            web.Response — {ok, verify, episodeId?}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = {}
        loop = asyncio.get_event_loop()
        evidence = await loop.run_in_executor(None, lambda: verify_mod.run_verification(path))
        persistence.save_verify_latest(evidence)
        ep_id = (body.get("episodeId") or "").strip()
        if ep_id:
            ep = next((e for e in persistence.load_episodes()
                       if e.get("episodeId") == ep_id), None)
            if ep is not None:
                ep["verify"] = evidence
                ep["updatedAt"] = datetime.now(timezone.utc).isoformat()
                persistence.upsert_episode(ep)
                await manager.broadcast({"type": "episode_updated", "episode": ep})
                # Green verify lands automatically (Slice B): a passed gate on an active
                # episode that owns scoped dirty changes auto-lands it — scoped ownership,
                # multi-episode ambiguity, and .openfde/ignored exclusions enforced inside
                # land_on_verify → land_episode (which syncs the card to Done/passed).
                # Red / skipped / ambiguous: no commit; the card moves to Testing and the
                # manual "Land changes" path stays.
                landed = False
                if (evidence.get("status") == "passed"
                        and ep.get("status") in ("open", "reviewing") and (ep.get("files") or [])):
                    from openfde import autoland
                    res = await loop.run_in_executor(
                        None, lambda: autoland.land_on_verify(
                            path, persistence, ep, run_verify=lambda _r: evidence))
                    for msg in res.get("broadcasts", []):
                        await manager.broadcast(msg)
                    landed = bool(res.get("committed"))
                if not landed:
                    # The user ran the checks — the issue card moves to Testing,
                    # wearing the evidence's color.
                    failed = evidence.get("status") != "passed"
                    moved = persistence.move_tasks_for_episode(
                        ep_id, "testing", "failed" if failed else "passed",
                        from_columns=None if failed else ("todo", "doing", "testing"))
                    if moved:
                        await manager.broadcast({"type": "tasks_updated"})
        await manager.broadcast({"type": "verify_updated", "verify": evidence})
        return web.json_response({"ok": True, "verify": evidence,
                                  **({"episodeId": ep_id} if ep_id else {})})

    # ================================================================== #
    #  REST — /api/events                                                 #
    # ================================================================== #

    async def get_events(request: web.Request) -> web.Response:
        """Return all persisted events, oldest-first.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON array of normalized event objects
        """
        return web.json_response(persistence.load_events())

    async def post_event(request: web.Request) -> web.Response:
        """Normalize, persist, and broadcast a single event.

        Side effects: appends to events.jsonl, broadcasts event_appended.

        Args:
            request: web.Request — body: JSON event object

        Returns:
            web.Response — JSON {ok: true, event: <normalized>}
        """
        raw = await request.json()
        normalized = persistence.append_event(raw)
        await manager.broadcast({"type": "event_appended", "event": normalized})
        return web.json_response({"ok": True, "event": normalized})

    # ================================================================== #
    #  REST — /api/archgraph                                              #
    # ================================================================== #

    def _latest_terminal_tag() -> str:
        return latest_terminal_tag(persistence)              # module-level helper (also used by boot)

    # In-process ArchGraph cache. analyze_repo() is ~1s on a mid-size repo and the canvas awaits it,
    # so recomputing per request blocked first paint. Key by the worktree signature (HEAD + dirty
    # set): a hit returns instantly, a miss recomputes once and persists the warm snapshot to disk
    # so even a fresh process boot serves the last canvas immediately.
    _arch_mem = {"sig": None, "graph": None}
    _arch_inflight = {"sig": None, "fut": None}   # coalesce concurrent analyze_repo scans

    # /api/boot must stay sub-second even while the capture poll, arch warm, and the heavy
    # (now off-loop) Story/Review/Timeline handlers are all using the default thread pool. Give
    # boot its OWN tiny pool so its cache read never queues behind them.
    _boot_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ofde-boot")
    # Repo identity (repoName/branch/gitRoot) is stable for the server's lifetime — resolve it ONCE
    # here (the only git boot ever needs) so /api/boot itself spawns NO subprocess and is cache-only.
    _boot_identity = session_mod.session_payload(path, server_started_at, _OPENFDE_VERSION)

    def _write_warm_after_episode(episode=None) -> None:
        """Snapshot a known-good restore point when an episode reaches a stable terminal state — the
        trusted warm-start the next boot restores from. Reuses the last computed arch (no recompute);
        best-effort so it never blocks the terminal transition. Never waits for shutdown."""
        try:
            st = git_status(path)
            boot_cache_mod.write_warm(
                persistence.openfde_dir, file_tree=build_file_tree(path),
                arch=_arch_mem.get("graph"), head=st.get("head", ""),
                dirty_sig=boot_cache_mod.dirty_signature(st), repo_root=str(path),
                episode_tag=(episode or {}).get("tag", "") or _latest_terminal_tag(),
                generated_at=datetime.now(timezone.utc).isoformat())
        except (OSError, KeyError, AttributeError):
            logger.warning("could not write warm cache after episode terminal state")

    async def _archgraph_async(force: bool = False) -> dict:
        """ArchGraph for the watched repo, cached by worktree signature. A hit (memory or matching
        disk snapshot) returns instantly. On a miss, the ~1s analyze_repo() runs off the event loop
        in a thread, and — crucially — /api/boot never waits on it (it serves the warm snapshot), so
        the scan happens in the background and the cockpit is interactive immediately."""
        loop = asyncio.get_event_loop()
        st = await loop.run_in_executor(None, lambda: git_status(path))     # git = I/O, thread is fine
        sig = boot_cache_mod.dirty_signature(st)
        if not force and _arch_mem["sig"] == sig and _arch_mem["graph"] is not None:
            return _arch_mem["graph"]                       # exact cache hit
        if not force:
            # Prefer ANY cached arch — even a STALE one — over re-analyzing. analyze_repo can take
            # tens of seconds on a large repo (observed: 57s), and it must NEVER run under the user
            # during first paint / browsing. A slightly stale architecture is fine: reassimilation on
            # file edits and an explicit rescan (?refresh=1 / "Scan repo") keep it fresh.
            if _arch_mem["graph"] is not None:
                return _arch_mem["graph"]                   # in-memory snapshot (possibly stale)
            warm = boot_cache_mod.read_warm(persistence.openfde_dir)
            if warm and warm.get("arch"):
                _arch_mem.update(sig=sig, graph=warm["arch"])
                logger.info("archgraph: served from disk snapshot (no scan)")
                return warm["arch"]                         # disk snapshot (possibly stale)
        # Coalesce concurrent misses for the same signature into ONE scan — at startup the gated
        # /api/archgraph and the deferred background warm would otherwise each run analyze_repo and
        # double the GIL pressure while the cockpit is trying to paint.
        inflight = _arch_inflight.get("fut")
        if not force and inflight is not None and _arch_inflight.get("sig") == sig:
            return await inflight
        t0 = time.perf_counter()
        fut = loop.run_in_executor(None, analyze_repo, path)               # CPU scan, off the loop
        _arch_inflight.update(sig=sig, fut=fut)
        try:
            graph = await fut
        finally:
            if _arch_inflight.get("fut") is fut:
                _arch_inflight.update(sig=None, fut=None)
        logger.info("archgraph: scanned in %dms", int((time.perf_counter() - t0) * 1000))
        _arch_mem.update(sig=sig, graph=graph)
        try:                                            # keep the warm cache fresh — best-effort
            boot_cache_mod.write_warm(
                persistence.openfde_dir, file_tree=build_file_tree(path), arch=graph,
                head=st.get("head", ""), dirty_sig=sig, repo_root=str(path),
                generated_at=datetime.now(timezone.utc).isoformat())
        except OSError:
            logger.warning("could not write warm arch cache")
        return graph

    async def get_archgraph(request: web.Request) -> web.Response:
        """Return the ArchGraph for the watched repository (cached by worktree signature).

        Cached; on a miss the read-only analyzer runs off the event loop in a thread.
        ``?refresh=1`` forces a recompute.
        """
        force = request.query.get("refresh") in ("1", "true")
        return web.json_response(await _archgraph_async(force=force))

    async def get_boot(request: web.Request) -> web.Response:
        """The tiny first-paint payload — repo identity + the last warm snapshot (file tree + canvas),
        served from disk WITHOUT running analyze_repo, so the cockpit is never blank on boot.

        ``stale`` says the snapshot no longer matches HEAD/worktree (the UI shows "refreshing…" and
        re-fetches /api/archgraph in the background); ``restoredFrom`` is the episode the snapshot was
        stamped at (or the latest terminal episode), for the "Restored from P14 · refreshing…" label.
        """
        want_canvas = request.query.get("canvas") in ("1", "true")
        loop = asyncio.get_event_loop()
        t0 = time.perf_counter()
        # build_boot_payload is CACHE-ONLY (no git — identity is precomputed) and runs on boot's own
        # tiny pool, so the handler returns sub-second even while everything else loads.
        payload = await loop.run_in_executor(
            _boot_pool,
            lambda: build_boot_payload(path, persistence, server_started_at, _OPENFDE_VERSION,
                                       want_canvas=want_canvas, identity=_boot_identity))
        logger.info("/api/boot served in %dms (restore: %s)",
                    int((time.perf_counter() - t0) * 1000), payload.get("restorePath"))
        return web.json_response(payload)

    async def post_explain(request: web.Request) -> web.Response:
        """Explain a canvas selection deterministically (Step 26).

        Reads the selected boxes, the ArchGraph (incl. function-level flows), and
        box-spec stories, and returns a grounded markdown explanation of what the
        modules are and how they relate. No LLM, no key — pure read model.

        Args:
            request: web.Request — body: {selectedBoxIds}.

        Returns:
            web.Response — JSON {ok, markdown, summary, moduleCount, flowCount}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        box_ids = body.get("selectedBoxIds") or []
        boxes_by_id = {b["id"]: b for b in persistence.load_state().get("boxes", [])}
        graph = analyze_repo(path)
        result = explain_selection(boxes_by_id, box_ids, graph, persistence.load_box_specs())
        return web.json_response({"ok": True, **result})

    async def post_story(request: web.Request) -> web.Response:
        """Build a deterministic Story-mode summary for a selection (Batch 5).

        Turns the selected module / file / function into semantic phases (a short
        plain-English narrative) plus the raw nodeIds / flowIds so the canvas can
        highlight the story path. No LLM.

        Args:
            request: web.Request — body: {selectedBoxIds, selectedEntity}.

        Returns:
            web.Response — JSON story {ok, title, summary, steps, inputs, outputs,
                                       confidence}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        box_ids = body.get("selectedBoxIds") or []
        entity = body.get("selectedEntity") or None
        boxes_by_id = {b["id"]: b for b in persistence.load_state().get("boxes", [])}
        graph = analyze_repo(path)
        return web.json_response(build_story(boxes_by_id, box_ids, entity, graph))

    async def post_state_from_archgraph(request: web.Request) -> web.Response:
        """Generate canvas state from the repo's ArchGraph and persist it.

        Runs the analyzer, converts modules and edges to canvas boxes and
        arrows, persists the result, regenerates PLAN.md, appends an event
        to events.jsonl, and broadcasts state_updated to connected clients.

        The existing canvas state is always replaced by this call; the user
        invokes it explicitly.

        Args:
            request: web.Request — body is ignored.

        Returns:
            web.Response — JSON {ok: true, state: {boxes, arrows}, summary: {...}}
        """
        canvas_state, graph = generate_canvas_state(path)
        persistence.save_state(canvas_state)
        _write_plan_md(persistence, path)

        summary = {
            "modules":   len(graph["modules"]),
            "files":     len(graph["files"]),
            "functions": len(graph["functions"]),
            "edges":     len(graph["edges"]),
            "flows":     len(graph.get("flows", [])),
            "warnings":  len(graph["warnings"]),
        }

        event = {
            "type": "archgraph_generated",
            "payload": {
                "moduleCount":   summary["modules"],
                "fileCount":     summary["files"],
                "functionCount": summary["functions"],
                "edgeCount":     summary["edges"],
                "detail": (
                    f"Repo scanned: {summary['modules']} module(s), "
                    f"{summary['edges']} edge(s), "
                    f"{summary['warnings']} warning(s)"
                ),
            },
        }
        normalized_event = persistence.append_event(event)

        await manager.broadcast({"type": "state_updated"})

        return web.json_response({
            "ok":      True,
            "state":   canvas_state,
            "summary": summary,
            "event":   normalized_event,
        })

    # ================================================================== #
    #  REST — /api/spec                                                   #
    # ================================================================== #

    async def post_spec(request: web.Request) -> web.Response:
        """Compile a canvas selection into a structured implementation spec.

        Reads the request body to get the selected box and arrow IDs plus an
        optional freeform user prompt, then calls compile_spec() with the
        current canvas state, tasks, project metadata, and a live ArchGraph.

        Appends a spec_generated event to events.jsonl and broadcasts
        event_appended to connected WebSocket clients.

        Args:
            request: web.Request — body: JSON {selectedBoxIds, selectedArrowIds, prompt}

        Returns:
            web.Response — JSON {ok: true, markdown: str, context: dict, event: dict}
        """
        body = await request.json()
        selected_box_ids   = body.get("selectedBoxIds",   [])
        selected_arrow_ids = body.get("selectedArrowIds", [])
        user_prompt        = body.get("prompt", "")

        canvas_state = persistence.load_state()
        tasks        = persistence.load_tasks()
        project      = persistence.load_project()
        box_specs    = persistence.load_box_specs()
        graph        = analyze_repo(path)

        result = compile_spec(
            canvas_state,
            tasks,
            project,
            graph,
            selected_box_ids,
            selected_arrow_ids,
            user_prompt,
            box_specs=box_specs,
        )

        ctx = result["context"]
        event = {
            "type": "spec_generated",
            "payload": {
                "boxCount":      len(ctx["boxes"]),
                "arrowCount":    len(ctx["arrows"]),
                "fileCount":     len(ctx["files"]),
                "functionCount": len(ctx["functions"]),
                "warningCount":  len(ctx["warnings"]),
                "detail": (
                    f"Spec compiled: {len(ctx['boxes'])} module(s), "
                    f"{len(ctx['files'])} file(s), "
                    f"{len(ctx['functions'])} function(s)"
                ),
            },
        }
        normalized_event = persistence.append_event(event)
        await manager.broadcast({"type": "event_appended", "event": normalized_event})

        return web.json_response({
            "ok":      True,
            "markdown": result["markdown"],
            "context":  ctx,
            "event":    normalized_event,
        })

    # ================================================================== #
    #  REST — /api/project                                                #
    # ================================================================== #

    async def get_project(request: web.Request) -> web.Response:
        """Return the persisted project metadata.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON {name, description, entries}
        """
        return web.json_response(persistence.load_project())

    async def get_plugins(request: web.Request) -> web.Response:
        """Capability providers + their activation for the watched repo (Plugin Registry
        v1-A/B/C). Metadata/probe only — no install, no network, no external code loaded.
        Three sources, one shape: built-ins (Python, JS/TS), deterministic suggestions
        (WebXR), and read-only repo-local manifests from ``.openfde/plugins/*.json``.

        Returns:
            web.Response — {ok, kinds, plugins:[{id, kind, displayName, status, source,
            activatesOn, provides, active, detected}]}.
        """
        # list_plugins runs bounded marker scans (suggestions + local manifests) — keep that
        # file-walk OFF the event loop. On-demand (opened from the palette), never polled.
        loop = asyncio.get_event_loop()
        listed = await loop.run_in_executor(None, lambda: plugins_mod.list_plugins(path))
        return web.json_response({"ok": True, "kinds": list(plugins_mod.PLUGIN_KINDS),
                                  "plugins": listed})

    async def get_webxr_summary(request: web.Request) -> web.Response:
        """WebXR domain-pack architecture hints for the watched repo (v1-E): frameworks (Three /
        R3F / Babylon / A-Frame), ``.glb``/``.gltf`` assets, XR entrypoints, and the markers found.
        Bounded scan, OFF the event loop. **Metadata + architecture hints only — no WebXR device
        runtime / test lens, no install, no network;** the honest boundary rides in ``warnings``.

        Resolved through the plugin RUNTIME system (v1-H): the WebXR pack's ``domain_summary`` hook is
        loaded lazily when the repo is WebXR-active, falling back to the core scan otherwise — the
        response shape is identical either way.

        Returns:
            web.Response — {ok, detected, entrypoints[], assets[], frameworks[], markers[],
            fileBadges[], warnings[]} — each list bounded.
        """
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, lambda: plugins_mod.resolve_webxr_summary(path))
        return web.json_response({"ok": True, **summary})

    async def post_plugin_install(request: web.Request) -> web.Response:
        """v1-F: ENABLE a known optional pack by writing its LOCAL MANIFEST into
        ``.openfde/plugins/{id}.json`` — a JSON file only. **Nothing is downloaded, imported, or
        executed; no network, no subprocess.** Allowlist-gated (an unknown id is refused) and
        idempotent. Off the event loop (a tiny file write). The next GET /api/plugins shows the pack
        as an ``available`` local manifest (superseding its suggestion — no duplicate row)."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: plugins_mod.install_local(path, request.match_info.get("id", "")))
        return web.json_response(result)

    async def get_plugin_install_plan(request: web.Request) -> web.Response:
        """v1-I: the CURATED install PLAN for a pack — a PROPOSAL, never an execution. Returns
        {ok, id, installable, requiresApproval, method, actions[], reason, …} with STRUCTURED actions
        (argv lists / endpoints, never a shell string), allowlisted to the curated registry (an unknown
        id is refused). Downloads/installs NOTHING — actual package install stays approval-gated and
        deferred. A cheap in-memory lookup, so it runs inline (no executor, no slowdown to listing)."""
        return web.json_response(plugins_mod.plugin_install_plan(request.match_info.get("id", "")))

    async def get_treesitter_recommendation(request: web.Request) -> web.Response:
        """L1-D: should OpenFDE recommend the tree-sitter JS/TS parser for THIS repo? Returns
        {recommended, id, reason, plan?} — a JS/TS repo without tree-sitter yields a recommendation
        carrying the approval-gated curated install plan (a PROPOSAL; nothing is installed, regex stays
        the fallback). Off the event loop (a bounded repo-language probe)."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: plugins_mod.treesitter_recommendation(path))
        return web.json_response(result)

    async def post_focus_neighborhood(request: web.Request) -> web.Response:
        """L2-A: a focused, O(issue) neighborhood for large repos — seed files + 1–2 hops of import /
        function-flow neighbors from the ArchGraph, capped. ADDITIVE: whole-repo assimilation is
        unchanged; this is opt-in. Body: {seeds:[paths], hops?, maxFiles?, primaryPath?:[paths]}.
        Returns {ok, mode:'focused', seeds, files, functions, edges, warnings}. Off the event loop."""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 — a bad body must not crash the focused path
            data = {}
        # Coerce + clamp every field (hops 0..3, maxFiles 1..200, seeds/primaryPath = list[str]) so a
        # malformed body yields a focused response with warnings, never a 500.
        args = focus_mod.coerce_request(data)
        # PERFORMANCE GUARD (L2-B): focus must be O(issue) and bounded — reuse the ALREADY-CACHED
        # ArchGraph (in-memory snapshot, else the warm disk snapshot) instead of re-running analyze_repo
        # (which can take tens of seconds on a large repo). No cache yet → pass {} so the neighborhood
        # is the seeds + an honest warning (graceful, never a fresh scan and never a blank canvas).
        cached = _arch_mem.get("graph")
        if cached is None:
            warm = boot_cache_mod.read_warm(persistence.openfde_dir)
            cached = (warm or {}).get("arch")
        graph_for_focus = cached if cached is not None else {}
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: focus_mod.neighborhood(
            path, args["seeds"], hops=args["hops"], max_files=args["max_files"],
            primary_path=args["primary_path"], graph=graph_for_focus))
        return web.json_response(result)

    async def post_focus_verify_plan(request: web.Request) -> web.Response:
        """L2-B: surface the SCOPED VERIFY PLAN — the smallest honest check set — for a focused/repro
        context. Body: {touchedFiles?:[paths], reproCheck?:{...}}. Returns
        {ok, mode:'scoped'|'fallback', checks, reason, warnings}. ADVISORY + READ-ONLY: it neither runs
        nor changes the verify gate; it only shows whether verify WOULD be scoped (and why) or fall back,
        so the scoped plan is visible before it ever becomes the enforced default."""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 — a bad body must not crash the advisory path
            data = {}
        args = focus_mod.coerce_verify_request(data)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: focus_mod.scoped_verify(
            path, touched_files=args["touched_files"], repro_check=args["repro_check"]))
        return web.json_response({"ok": True, **result})

    async def post_project(request: web.Request) -> web.Response:
        """Persist project metadata, regenerate PROJECT_META.md and PLAN.md.

        Side effects: writes project.json, writes PROJECT_META.md, writes PLAN.md,
        broadcasts project_updated.

        Args:
            request: web.Request — body: JSON {name, description, entries}

        Returns:
            web.Response — JSON {ok: true}
        """
        data = await request.json()
        persistence.save_project(data, path)   # also writes PROJECT.md
        _write_plan_md(persistence, path)
        await manager.broadcast({"type": "project_updated"})
        return web.json_response({"ok": True})

    # ================================================================== #
    #  REST — /api/project-log  (conversation ledger)                    #
    # ================================================================== #

    async def get_project_log(request: web.Request) -> web.Response:
        """Return all conversation-ledger entries, oldest-first.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON array of normalized ledger entries.
        """
        return web.json_response(persistence.load_project_log())

    async def post_project_log(request: web.Request) -> web.Response:
        """Append one ledger entry and regenerate the repo-root project.md.

        Side effects: appends to project_log.jsonl, rewrites project.md,
        broadcasts project_log_appended.

        Args:
            request: web.Request — body: JSON ledger entry (role, title,
                     summary, body, eventId, boxIds, arrowIds, filePaths, …)

        Returns:
            web.Response — JSON {ok: true, entry: <normalized>}
        """
        raw = await request.json()
        entry = persistence.append_project_log_entry(raw, path)
        await manager.broadcast({"type": "project_log_appended", "entry": entry})
        return web.json_response({"ok": True, "entry": entry})

    async def get_project_md(request: web.Request) -> web.Response:
        """Return the generated project.md ledger as a markdown string.

        Always rendered fresh from the structured ledger on disk.

        Args:
            request: web.Request

        Returns:
            web.Response — text/markdown document.
        """
        return web.Response(text=persistence.render_project_md(), content_type="text/markdown")

    # ================================================================== #
    #  REST — /api/box-specs  (prompt provenance)                        #
    # ================================================================== #

    async def get_box_specs(request: web.Request) -> web.Response:
        """Return the full box-specs map (boxId → spec).

        Args:
            request: web.Request

        Returns:
            web.Response — JSON object keyed by boxId.
        """
        return web.json_response(persistence.load_box_specs())

    async def get_box_spec(request: web.Request) -> web.Response:
        """Return the spec for a single box, or 404 when none exists.

        Args:
            request: web.Request — match_info 'boxId'

        Returns:
            web.Response — JSON spec, or 404.
        """
        box_id = request.match_info.get("boxId", "")
        spec = persistence.load_box_specs().get(box_id)
        if spec is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response(spec)

    async def post_box_specs_update(request: web.Request) -> web.Response:
        """Deterministically update box specs for the scoped boxes of an Execute.

        Side effects: writes box_specs.json.

        Args:
            request: web.Request — body: JSON {boxIds, userPrompt, ledgerEntryId,
                     eventId, filePaths, summary, outcome}

        Returns:
            web.Response — JSON {ok: true, updated: [...], specs: {...}}
        """
        body = await request.json()
        box_ids = body.get("boxIds", []) or []

        canvas_state = persistence.load_state()
        boxes_by_id  = {b["id"]: b for b in canvas_state.get("boxes", [])}
        existing     = persistence.load_box_specs()

        updated = update_box_specs_from_execute(
            existing,
            boxes_by_id,
            box_ids=box_ids,
            user_prompt=body.get("userPrompt", ""),
            ledger_entry_id=body.get("ledgerEntryId", ""),
            event_id=body.get("eventId", ""),
            file_paths=body.get("filePaths", []),
            summary=body.get("summary", ""),
            outcome=body.get("outcome", ""),
        )
        persistence.save_box_specs(updated)

        return web.json_response({
            "ok":      True,
            "updated": [bid for bid in box_ids if bid in updated],
            "specs":   updated,
        })

    # ================================================================== #
    #  REST — /api/runs  (execution runs + trace, Step 17)              #
    # ================================================================== #

    async def post_run(request: web.Request) -> web.Response:
        """Start an execution run and record its scope.

        This is execution-state *visualization* for the current placeholder
        flow — it does not modify repo files. Persists a run record, appends a
        run_started trace event, and a run_started timeline event.

        Args:
            request: web.Request — body: {scopedBoxIds, scopedFileIds,
                     scopedFunctionIds, scopedArrowIds}

        Returns:
            web.Response — JSON {ok, run, event}
        """
        body = await request.json()
        run_id = "run_" + secrets.token_hex(5)
        now = datetime.now(timezone.utc).isoformat()
        run = {
            "runId":             run_id,
            "status":            "planning",
            "startedAt":         now,
            "endedAt":           None,
            "scopedBoxIds":      body.get("scopedBoxIds", []),
            "scopedFileIds":     body.get("scopedFileIds", []),
            "scopedFunctionIds": body.get("scopedFunctionIds", []),
            "scopedArrowIds":    body.get("scopedArrowIds", []),
            "simulated":         True,
        }
        persistence.upsert_run(run)
        persistence.append_run_event({"runId": run_id, "type": "run_started", "status": "planning"})
        timeline_event = persistence.append_event({
            "type": "run_started",
            "payload": {
                "runId":      run_id,
                "boxCount":   len(run["scopedBoxIds"]),
                "arrowCount": len(run["scopedArrowIds"]),
                "detail":     f"Execution run started ({len(run['scopedBoxIds'])} module(s) in scope)",
            },
        })
        await manager.broadcast({"type": "event_appended", "event": timeline_event})
        return web.json_response({"ok": True, "run": run, "event": timeline_event})

    async def post_run_event(request: web.Request) -> web.Response:
        """Append a trace event to a run; payloads are summarized + redacted.

        Run-level lifecycle events (run_running / run_passed / run_failed) also
        update the run record and (for pass/fail) append a timeline event.

        Args:
            request: web.Request — match_info 'runId'; body: trace event
                     {type, nodeId?, edgeId?, status?, input?, output?, error?}

        Returns:
            web.Response — JSON {ok, event, run?, timelineEvent?}
        """
        run_id = request.match_info.get("runId", "")
        body = await request.json()
        body["runId"] = run_id
        stored = persistence.append_run_event(body)
        result = {"ok": True, "event": stored}

        etype = body.get("type")
        if etype in ("run_running", "run_passed", "run_failed"):
            run = persistence.get_run(run_id) or {"runId": run_id, "scopedBoxIds": [], "scopedArrowIds": []}
            run["status"] = {"run_running": "running", "run_passed": "passed", "run_failed": "failed"}[etype]
            if etype in ("run_passed", "run_failed"):
                run["endedAt"] = datetime.now(timezone.utc).isoformat()
            persistence.upsert_run(run)
            result["run"] = run
            if etype in ("run_passed", "run_failed"):
                detail = ("Execution run passed" if etype == "run_passed"
                          else f"Execution run failed: {str(body.get('errorSummary') or 'see trace')}")
                tl = persistence.append_event({"type": etype, "payload": {"runId": run_id, "detail": detail}})
                await manager.broadcast({"type": "event_appended", "event": tl})
                result["timelineEvent"] = tl
        return web.json_response(result)

    async def get_runs(request: web.Request) -> web.Response:
        """Return all execution run records, latest-first.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON array of run records.
        """
        return web.json_response(persistence.load_runs())

    async def get_run_one(request: web.Request) -> web.Response:
        """Return a single run record plus its trace events.

        Args:
            request: web.Request — match_info 'runId'

        Returns:
            web.Response — JSON {run, events} or 404.
        """
        run_id = request.match_info.get("runId", "")
        run = persistence.get_run(run_id)
        if run is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response({"run": run, "events": persistence.load_run_events(run_id)})

    # ================================================================== #
    #  REST — /api/execution  (backend abstraction + workflow bridge)   #
    # ================================================================== #

    def _compile_workflow_for(body: dict) -> dict:
        """Compile a workflow from a request body (shared by compile + run)."""
        return compile_workflow(
            persistence.load_state(),
            persistence.load_tasks(),
            persistence.load_project(),
            analyze_repo(path),
            persistence.load_box_specs(),
            persistence.load_project_log(),
            path,
            body.get("selectedBoxIds", []),
            body.get("selectedArrowIds", []),
            body.get("prompt", ""),
        )

    async def get_execution_backends(request: web.Request) -> web.Response:
        """Return available execution backends and the active one.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON {backends: [...], active: str}.
        """
        active = persistence.load_execution_config().get("activeBackend", ACTIVE_DEFAULT)
        return web.json_response(list_backends(active))

    async def post_execution_backend(request: web.Request) -> web.Response:
        """Set the active execution backend.

        Args:
            request: web.Request — body: {backend: str}

        Returns:
            web.Response — JSON {ok, active} or 400 for an unknown backend.
        """
        body = await request.json()
        bid = body.get("backend", "")
        if not is_valid_backend(bid):
            return web.json_response({"ok": False, "error": "unknown backend"}, status=400)
        persistence.save_execution_config({"activeBackend": bid})
        return web.json_response({"ok": True, "active": bid})

    # ================================================================== #
    #  REST — /api/agent-settings  (role → provider config, Step 21)     #
    # ================================================================== #

    async def get_agent_settings(request: web.Request) -> web.Response:
        """Return sanitized agent role settings + UI options.

        Never exposes a raw apiKey — only hasApiKey + maskedApiKey per role.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON {ok, settings, options}.
        """
        stored = persistence.load_agent_settings()
        return web.json_response({
            "ok": True,
            "settings": agent_settings_mod.to_public(stored),
            "options": agent_settings_mod.options(),
        })

    async def put_agent_settings(request: web.Request) -> web.Response:
        """Apply a full/partial settings update and return sanitized settings.

        Stored secrets are preserved when the caller omits apiKey; a non-empty
        apiKey replaces it; clearApiKey wipes it. The response never contains a
        raw key.

        Args:
            request: web.Request — body: {settings:{...}} or a bare role map.

        Returns:
            web.Response — JSON {ok, settings, options} or 400.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        incoming = body.get("settings") if isinstance(body, dict) and isinstance(body.get("settings"), dict) else body
        merged = agent_settings_mod.merge(persistence.load_agent_settings(), incoming)
        stored = persistence.save_agent_settings(merged)
        return web.json_response({
            "ok": True,
            "settings": agent_settings_mod.to_public(stored),
            "options": agent_settings_mod.options(),
        })

    async def post_agent_settings_check(request: web.Request) -> web.Response:
        """Validate config shape only (no network). Returns per-role results.

        Body forms:
          - {role, config}     → check a single in-progress role config, with a
                                  stored key as fallback;
          - {settings}         → check a full proposed settings map;
          - {}                 → check the stored settings.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON {ok, roles} or 400 for malformed JSON.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        stored = persistence.load_agent_settings()
        body = body if isinstance(body, dict) else {}
        role = body.get("role")
        if role in agent_settings_mod.ROLES and isinstance(body.get("config"), dict):
            merged = agent_settings_mod.merge(stored, {role: body["config"]})
            result = agent_settings_mod.check(merged, role=role)
        elif isinstance(body.get("settings"), dict):
            merged = agent_settings_mod.merge(stored, body["settings"])
            result = agent_settings_mod.check(merged)
        else:
            result = agent_settings_mod.check(stored)
        return web.json_response({"ok": result["ok"], "roles": result["roles"]})

    # ================================================================== #
    #  REST — /api/semantic-graph  (Step 37a: Semantic Graph Adapter)    #
    # ================================================================== #

    async def get_semantic_graph(request: web.Request) -> web.Response:
        """Return the stored semantic-graph summary (+ full graph on ?full=1).

        Provider output is evidence, not truth — every artifact carries provenance.

        Args:
            request: web.Request — optional query ?full=1 for the whole graph.

        Returns:
            web.Response — {ok, exists, summary, graph?}.
        """
        graph = semantic_graph_mod.load_graph(path)
        out = {"ok": True, "exists": graph is not None,
               "summary": semantic_graph_mod.graph_summary(graph)}
        if graph is not None and request.query.get("full"):
            out["graph"] = graph
        return web.json_response(out)

    async def post_semantic_graph_refresh(request: web.Request) -> web.Response:
        """Regenerate .openfde/semantic_graph.json for the watched repo.

        Runs the deterministic providers (ast / tethers / risk) plus any installed
        optional providers (code2flow / detect-secrets) off the event loop.

        Returns:
            web.Response — {ok, summary} or 500 with the error.
        """
        loop = asyncio.get_event_loop()
        try:
            graph = await loop.run_in_executor(None, lambda: semantic_graph_mod.build_graph(path))
            await loop.run_in_executor(None, lambda: semantic_graph_mod.write_graph(path, graph))
        except Exception as exc:  # noqa: BLE001
            logger.error("semantic graph refresh failed: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)[:200]}, status=500)
        return web.json_response({"ok": True, "summary": semantic_graph_mod.graph_summary(graph)})

    async def post_review_reassimilate(request: web.Request) -> web.Response:
        """Incremental Re-assimilation v1 (Land · Watch · Review).

        After external edits settle (Watch Any Agent), refresh OpenFDE's *understanding*
        so Review operates on a fresh-enough architecture: re-run the ArchGraph analyzer
        and rebuild + persist the semantic graph (concepts/tethers), so newly created
        files/functions/modules become part of the read model and the Review Delta.

        **v1 is honest about being a full recompute** (`mode: "full-recompute"`): it is
        *triggered* by the changed files but internally re-analyzes the whole repo with
        the existing analyzers — true partial parsing is a later optimization. It never
        mutates saved canvas state, never stages git, and never regenerates the canvas
        from the ArchGraph (no `/api/state/from-archgraph`). On failure it returns a
        structured warning and the caller keeps using its current graph.

        Args:
            request: web.Request — body {files?: [repo-rel paths], reason?: str}.

        Returns:
            web.Response — {ok, files, reason, mode, archGraph, semanticSummary, warnings}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = {}
        files = [f for f in (body.get("files") or []) if isinstance(f, str)]
        reason = body.get("reason") or "manual"
        loop = asyncio.get_event_loop()
        warnings: list = []
        try:
            graph = await loop.run_in_executor(None, lambda: analyze_repo(path))
        except Exception as exc:  # noqa: BLE001 — keep the caller's current graph usable
            logger.error("reassimilate: ArchGraph analysis failed: %s", exc)
            return web.json_response(
                {"ok": False, "files": files, "reason": reason, "mode": "full-recompute",
                 "archGraph": None, "semanticSummary": None,
                 "warnings": [f"archgraph analysis failed: {str(exc)[:160]}"]},
            )
        warnings.extend(graph.get("warnings") or [])
        # Rebuild + persist the semantic graph so the worktree impact's concept delta
        # picks up new tethers. A failure here is non-fatal — the ArchGraph refresh
        # alone still improves Review; we just flag it.
        semantic_summary = None
        try:
            sem = await loop.run_in_executor(None, lambda: semantic_graph_mod.build_graph(path))
            await loop.run_in_executor(None, lambda: semantic_graph_mod.write_graph(path, sem))
            semantic_summary = semantic_graph_mod.graph_summary(sem)
        except Exception as exc:  # noqa: BLE001
            logger.error("reassimilate: semantic graph rebuild failed: %s", exc)
            warnings.append(f"semantic graph rebuild failed: {str(exc)[:160]}")
        return web.json_response({
            "ok": True, "files": files, "reason": reason, "mode": "full-recompute",
            "archGraph": graph, "semanticSummary": semantic_summary, "warnings": warnings,
        })

    # ================================================================== #
    #  REST — /api/review/episodes  (Prompt Story Rail — OpenFDE commits)#
    # ================================================================== #
    #  Coding agents edit; OpenFDE watches, reviews, groups, commits, and
    #  narrates. A prompt "episode" is the durable unit: user intent +
    #  the runs/events it spawned + the commit(s) OpenFDE lands for it.

    def _scope_hint(files: list) -> str:
        """A concise scope label from changed paths (e.g. 'openfde', 'frontend').

        Deterministic — used to give a commit subject a where-hint when the prompt
        text is empty, and to head the encapsulating commit body. Prefers the
        common module directory; falls back to the top-level dirs touched.
        """
        paths = [p for p in (files or []) if p]
        if not paths:
            return ""
        # If everything shares a 2-segment prefix (a module), name it precisely.
        segs = [p.split("/") for p in paths]
        if len(paths) > 1 and all(len(s) >= 2 for s in segs):
            two = {"/".join(s[:2]) for s in segs}
            if len(two) == 1:
                return next(iter(two))
        tops = []
        for s in segs:
            t = s[0] if len(s) > 1 else s[0]
            if t not in tops:
                tops.append(t)
        return ", ".join(tops[:2]) + ("…" if len(tops) > 2 else "")

    def _episode_subject(ep: dict, imp: dict = None) -> str:
        """A commit subject that encapsulates a prompt episode's work.

        Deterministic (no LLM): ``openfde: <prompt first line>``. When the episode
        carries no prompt text, fall back to a scope hint derived from the changed
        files (``openfde: update <scope>``). Kept under a normal git subject length.
        """
        p = (ep.get("prompt") or "").strip().splitlines()[0] if (ep.get("prompt") or "").strip() else ""
        if not p:
            scope = _scope_hint((imp or {}).get("files") and [f.get("path") for f in imp["files"]] or [])
            if scope:
                p = f"update {scope}"
            else:
                p = "Manual changes" if ep.get("kind") == "manual" else "OpenFDE change"
        return f"openfde: {p}"[:78]

    def _land_body(ep: dict, imp: dict) -> str:
        """An encapsulating commit body: the prompt's own summary + a file manifest.

        One prompt may touch a file many times before Land; the single commit's body
        records the whole reviewed set so the message stands on its own later.
        """
        lines: list = []
        summary = (ep.get("summary") or "").strip()
        if summary:
            lines.append(summary)
        files = [f.get("path") for f in (imp.get("files") or []) if f.get("path")]
        if files:
            scope = _scope_hint(files)
            head = f"Scope: {scope}" if scope else "Scope: (repo)"
            lines.append(f"{head} · {len(files)} file{'s' if len(files) != 1 else ''} reviewed and landed")
            lines += [f"- {p}" for p in files[:16]]
            if len(files) > 16:
                lines.append(f"- … +{len(files) - 16} more")
        return "\n".join(lines).strip()

    # Ready-for-PR: (sha, base) pairs once seen ON the base — permanent, so cached
    # across requests; "not on base" is never cached (a push can change it).
    _onbase_cache: dict = {}

    def _review_episodes_payload() -> dict:
        """Build the prompt-episodes list (newest-first, with landed commits) + the
        "Outside OpenFDE" bucket. SYNC and git-subprocess heavy (git_timeline + up to
        80 `git show` + reconciliation + per-episode merge-base readiness), so the
        async handler runs it OFF the event loop. This is the on-demand FULL endpoint;
        the frequent rail poll uses the cheap ``_review_episodes_rail`` above.

        Returns:
            dict — {ok, episodes:[{…episode, commits, commitCount, fileCount}],
                    outside:{…synthetic bucket}}.
        """
        persistence.backfill_episode_meta()                    # title/tag/seq/signal
        episodes = episode_llm_summary.ensure_facts(persistence)  # storyFacts (deterministic; no subprocess)
        # Settle any episode stuck in the transient ``auto_landing`` (a land interrupted between
        # its commit and the final ``landed`` write) so the rail never shows it landing forever.
        from openfde import autoland as _autoland
        for _e in episodes:
            if _autoland.heal_landing_status(_e):
                persistence.upsert_episode(_e)
        commits = git_timeline(path, limit=200)

        # Shared changed-files cache + a bounded fetch (one `git show --name-only` per sha) so the
        # reconciliation pass and the commit views never double-read git, and a poll stays cheap.
        file_cache: dict = {}
        fetch_budget = [80]                                    # list = mutable closure cell

        def _files(sha: str) -> list:
            if not sha:
                return []
            if sha not in file_cache and fetch_budget[0] > 0:
                fetch_budget[0] -= 1
                file_cache[sha] = commit_files(path, sha)
            return file_cache.get(sha, [])

        # Reconcile recent commits onto episodes by trailer (explicit, wins) or file overlap + timing
        # — the product model is *many prompts → one commit*, so one batched commit can land on several
        # prompt cards. A commit is a candidate until its sha is recorded on SOME episode's commitShas
        # (steady state: attached shas drop out → no work). Crucially we do NOT skip trailer-carrying
        # commits: a trailer'd commit made OUTSIDE autoland (a manual/external land) still needs its
        # episode attached — its episodeIds being parsed is not the same as the episode recording it,
        # and the explicit-trailer path below attaches it. Confident links only (explicit /
        # high_file_overlap / time_file_inferred); ambiguous is surfaced elsewhere, not auto-attached.
        already_attached = {s for e in episodes for s in (e.get("commitShas") or [])}
        candidates = [{**c, "files": _files(c.get("sha"))}
                      for c in commits[:40]
                      if c.get("sha") and c.get("sha") not in already_attached]
        if candidates:
            # Conservative: trailer wins; the heuristic attaches only OpenFDE-authored commits on a
            # single unambiguous, provenance-gated match (strong overlap / baseline / capture window)
            # and marks the episode landed. A baseline match also rescues cwd-agnostic capture.
            for eid in episode_commits_mod.reconcile_authored_episodes(candidates, episodes, watched_root=path):
                ep = next((e for e in episodes if e.get("episodeId") == eid), None)
                if ep is not None:
                    persistence.upsert_episode(ep)
        attached_shas: set = set()                            # shas shown under some episode → not Outside

        def _commit_view(c: dict, ep: dict) -> dict:
            sha = c.get("sha")
            cf = _files(sha)
            # Clean display text for OpenPM / evidence cards. A clustered Auto-Land stores a
            # per-commit title/summary on the episode (commitMeta[sha]) — use it so each commit
            # reads like its own logical change; otherwise fall back to the cleaned owning episode,
            # never the noisy raw commit subject ("openfde: Here's the CC…").
            cm = (ep.get("commitMeta") or {}).get(sha) if sha else None
            # A stored per-commit title can itself be noisy ("text", from a prompt that
            # opened with a ```text fence, captured before the LLM title upgrade) —
            # validate it like any other title and heal from the episode when bad.
            if cm and cm.get("title") and not is_bad_title(cm["title"]):
                dtitle, dsummary = cm["title"], cm.get("summary") or ""
            else:
                dtitle, dsummary = commit_display(ep.get("title"), ep.get("summary"), c.get("summary"))
            # Attribution confidence (explicit trailer vs. inferred from files/time) so the UI can
            # quietly mark inferred links. Absent for plain trailer commits that predate commitMeta.
            return {**c, "files": cf, "fileCount": len(cf),
                    "displayTitle": dtitle, "displaySummary": dsummary,
                    "confidence": (cm or {}).get("confidence"),
                    "matchedFiles": (cm or {}).get("matchedFiles") or []}

        # Ready-for-PR readiness (v1.1), embedded so cards/badges render without extra
        # clicks. One `git status` + one base resolution per request; merge-base only
        # for landed-no-PR episodes, with a positives-only memo (`_onbase_cache`) —
        # "on base" is permanent for a given (sha, base), "not on base" can change
        # after a push so it is re-checked. Steady state ≈ a couple of git reads.
        st = git_status(path)
        base_ref = prs_mod._base_ref(path)

        def _on_base(sha: str) -> bool:
            key = (sha, base_ref)
            if key in _onbase_cache:
                return True
            anc = subprocess.run(["git", "merge-base", "--is-ancestor", sha, base_ref],
                                 cwd=str(path), capture_output=True, text=True)
            if anc.returncode == 0:
                _onbase_cache[key] = True
                return True
            return False

        readiness_ctx = {"clean": not (st.get("dirty") or []), "base": base_ref,
                         "dirtyFiles": list(st.get("dirty") or []),
                         "on_base": _on_base, "gh_ok": shutil.which("gh") is not None}

        def _episode_commits(e: dict) -> list:
            # An episode's commits = those it explicitly declares (a trailer naming this episode,
            # singular or plural) ∪ those attached to it (landed or reconciled, via commitShas).
            # Iterate `commits` (newest-first) so order is preserved and each shows once.
            eid = e.get("episodeId")
            shas = set(e.get("commitShas") or [])
            picked = []
            for c in commits:
                if c.get("sha") in shas or (eid and eid in (c.get("episodeIds") or [])):
                    picked.append(c)
                    attached_shas.add(c.get("sha"))
            return picked

        # Deterministic rail order (sequence desc) AFTER reconciliation's upserts — never the
        # store's file order, which an upsert reorders (the P122 → P83 → P121 jumble). The
        # readiness cap below then applies to the genuinely-newest episodes, and the boot rail
        # (build_rail_payload) uses the SAME key so the two agree.
        episodes = sorted(episodes, key=_rail_order_key, reverse=True)
        # PR readiness runs git merge-base per landed episode; on a large history that is the
        # dominant per-poll cost. Compute it only for the most recent episodes (the ones a user
        # acts on); older episodes get null here and fetch readiness on demand when spotlighted
        # (ConceptPanel already does this). Episodes arrive newest-first.
        _READINESS_CAP = 40
        enriched = []
        for i, e in enumerate(episodes):
            ecs = [_commit_view(c, e) for c in _episode_commits(e)]
            enriched.append({**e, "commits": ecs, "commitCount": len(ecs),
                             "fileCount": len(e.get("files") or []),
                             "prReadiness": (pr_readiness(path, e, _ctx=readiness_ctx)
                                             if i < _READINESS_CAP else None)})
        # Outside OpenFDE = every commit not shown under some episode: manual commits, foreign
        # trailers (an episode id we don't know), and anything reconciliation wasn't confident
        # enough to attach. Never silently dropped.
        outside_commits = [c for c in commits if c.get("sha") not in attached_shas]
        outside_bucket = {
            "episodeId": "outside", "kind": "manual", "status": "landed",
            "prompt": "Outside OpenFDE",
            "summary": "Commits not linked to an OpenFDE prompt (manual / foreign).",
            "commits": outside_commits, "commitCount": len(outside_commits),
            "files": [], "fileCount": 0,
        }
        return {"ok": True, "episodes": enriched, "outside": outside_bucket}

    async def get_review_episodes(request: web.Request) -> web.Response:
        """Default rail endpoint: CHEAP, cache-only (persisted episodes + their declared
        commits). Safe to poll often — no git, no reconciliation, no readiness. The full
        enriched view is /api/review/episodes/full, loaded after first paint / on demand.

        ``?mode=boot`` → BOOT: the latest ~10 chips for instant first paint, served as a TINY READ
        of the persisted rail cache (~5 KB) on the dedicated boot pool — it NEVER parses the whole
        store on the request path. Cold (no cache yet) → a non-authoritative empty + a background
        build (default pool), so the read stays tiny and the cache is ready for the next tick.
        ``?limit=N`` builds a fresh slice."""
        loop = asyncio.get_event_loop()
        if request.query.get("mode") == "boot" and request.query.get("limit") is None:
            cached = await loop.run_in_executor(
                _boot_pool, lambda: story_cache_mod.read_rail_cache(persistence.openfde_dir))
            if cached is None:
                asyncio.create_task(_refresh_rail_cache())   # build it off the boot pool for next paint
                return web.json_response(story_cache_mod.empty_rail_boot())
            return web.json_response(cached)
        limit = None
        if request.query.get("limit") is not None:
            try:
                limit = max(1, min(int(request.query["limit"]), 200))
            except (TypeError, ValueError):
                pass
        pool = _boot_pool if limit is not None else None
        return web.json_response(
            await loop.run_in_executor(pool, lambda: build_rail_payload(persistence, limit=limit)))

    async def get_review_episodes_full(request: web.Request) -> web.Response:
        """Full enriched view: git_timeline + reconciliation + per-commit files + PR readiness
        + the Outside bucket. Git-subprocess heavy, so it runs OFF the event loop and is fetched
        sparingly (once after first paint, and when a shipping/detail view opens), never on the
        frequent rail poll."""
        loop = asyncio.get_event_loop()
        return web.json_response(await loop.run_in_executor(None, _review_episodes_payload))

    _story_lock = asyncio.Lock()              # serialize Story rebuilds (endpoint + warm + land hook)

    def _story_graph_full() -> dict:
        """The FULL Story-graph pipeline — backfill → deterministic facts → meta/junk reclassify →
        demo-plan clean → build_prompt_graph. Synchronous and touches git once (a check-ignore in
        the reclassify); callers run it OFF the event loop. Reused by /api/story/prompt-graph, the
        boot warm, and the land hook so the laws stay in one place."""
        persistence.backfill_episode_meta()
        episodes = episode_llm_summary.ensure_facts(persistence)  # storyFacts (deterministic; no subprocess)
        # Meta-by-effect: an episode that only edited gitignored docs (demo scripts, ROADMAP/FLOW)
        # and committed nothing — or only OS junk like .DS_Store — is reclassified operational, off
        # the spine (Events layer still has it).
        episodes = persistence.flag_nonimplementation_episodes(path, episodes)
        # Demo-PLANNING concepts (NanoGPT/Tailwind, live demo, …) leave product Story concepts; a
        # demo-prompt episode that made a real change is titled by the change.
        episodes = persistence.clean_story_facts(episodes)
        return build_prompt_graph(episodes, events=persistence.load_events()[-200:])

    def _cache_story(graph: dict) -> None:
        """Persist the Story boot cache from a freshly built graph (best-effort)."""
        try:
            story_cache_mod.write_story_cache(
                persistence.openfde_dir, graph,
                generated_at=datetime.now(timezone.utc).isoformat())
        except OSError:
            logger.warning("could not write story cache")

    _story_refresh = {"running": False, "again": False}   # coalesce a burst of lands → ≤1 extra rebuild
    _full_graph_mem = {"graph": None}     # last built FULL graph (in-memory) — served without a rebuild

    async def _refresh_story_cache_and_broadcast() -> None:
        """Rebuild the Story graph off-loop, refresh the boot cache, and nudge open clients to
        re-hydrate (``story_updated``). Best-effort, serialized, and COALESCED — a burst of lands
        collapses to at most one trailing rebuild (the rebuild reads the latest persisted state), so
        the ~seconds-long GIL-heavy build never piles up. A failure never blocks the triggering
        action (a land, server start)."""
        if _story_refresh["running"]:
            _story_refresh["again"] = True        # a rebuild is in flight; ask it to run once more
            return
        _story_refresh["running"] = True
        try:
            while True:
                _story_refresh["again"] = False
                async with _story_lock:
                    loop = asyncio.get_event_loop()
                    graph = await loop.run_in_executor(None, _story_graph_full)
                    _cache_story(graph)
                    _full_graph_mem["graph"] = graph     # serve future full requests from memory
                await manager.broadcast({"type": "story_updated"})
                if not _story_refresh["again"]:
                    break
        except Exception:  # noqa: BLE001 — cache refresh must never break a land / startup
            logger.debug("story cache refresh failed", exc_info=True)
        finally:
            _story_refresh["running"] = False

    def _cache_rail_sync() -> None:
        """Rebuild + persist the tiny rail boot cache (latest ~10 chips). Parses the store once,
        off the request path — best-effort."""
        try:
            story_cache_mod.write_rail_cache(
                persistence.openfde_dir, build_rail_payload(persistence, limit=10))
        except OSError:
            logger.warning("could not write rail cache")

    def _head_sha() -> "str | None":
        """The watched repo's current HEAD sha — a cheap gate so reconciliation runs only when a
        commit actually lands, not on every idle poll tick."""
        try:
            r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                               capture_output=True, text=True, timeout=5)
            return (r.stdout.strip() or None) if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _reconcile_landed_sync() -> bool:
        """Attribute recent OpenFDE-authored, trailer-less commits to their episodes (conservative:
        trailer wins; heuristic only on a single unambiguous, provenance-gated match — strong
        overlap / baseline / capture window) and mark those episodes landed. Persists only the
        changed episodes; off the event loop. Returns True iff anything changed. This is the RELIABLE
        rail-attribution path — independent of the heavy, sometimes-starved full-rail read — so a
        trailer-less land (the P119 gap) shows its commit + lands the episode without it."""
        episodes = persistence.load_episodes()
        # Heal polluted sessionCwd FIRST — an episode whose transcript cwd points outside the
        # watched repo but whose files are repo-relative under it provably belongs to this repo
        # (a cross-cwd agent session). Correcting the attribution is what lets the same-repo gate
        # below accept its manual commit; healing itself never attaches a commit.
        from openfde import prompt_capture
        healed = prompt_capture.heal_session_cwd(episodes, path)
        for ep in healed:
            persistence.upsert_episode(ep)
        if healed:
            # ``heal_session_cwd`` may return corrected copies rather than mutating the
            # loaded list. Re-read before attribution so a same-tick manual-land repair
            # sees the watched repo cwd instead of requiring a second HEAD move.
            episodes = persistence.load_episodes()
        attached = {s for e in episodes for s in (e.get("commitShas") or [])}
        # Candidate = any commit whose sha is not yet on an episode's commitShas — trailer'd commits
        # INCLUDED. A trailer'd commit made outside autoland (manual/external land) still needs its
        # episode attached + landed; the explicit-trailer path in reconcile_authored_episodes does that.
        # Steady state: once attached, the sha drops out of the candidate set, so there is no churn.
        cands = [{**c, "files": commit_files(path, c["sha"])}
                 for c in git_timeline(path, limit=40)[:20]
                 if c.get("sha") and c["sha"] not in attached]
        if not cands:
            return bool(healed)
        changed = episode_commits_mod.reconcile_authored_episodes(cands, episodes, watched_root=path)
        # A needs_manual_land episode whose landing commit was made OUTSIDE the OpenFDE land path
        # (manual `git commit`, no trailer, human-authored) is refused by the path above (author +
        # capture-window gates). Attach it when the link is unambiguous — the commit covers ALL the
        # episode's files, lands after capture, and is the single needs_manual_land candidate.
        attached_now = {s for e in episodes for s in (e.get("commitShas") or [])}
        manual_cands = [c for c in cands if c.get("sha") not in attached_now]
        manual = episode_commits_mod.reconcile_manual_land(manual_cands, episodes, watched_root=path)
        for eid, verdicts in manual.items():
            changed.setdefault(eid, []).extend(verdicts)
        for eid in changed:
            ep = next((e for e in episodes if e.get("episodeId") == eid), None)
            if ep is not None:
                persistence.upsert_episode(ep)
        # Settle any episode stuck at the transient ``auto_landing`` now that its trailer'd commit
        # has been (re)attached above — promote it to landed so the lifecycle is coherent.
        from openfde import autoland as _autoland
        settled = False
        for ep in persistence.load_episodes():
            if _autoland.heal_landing_status(ep):
                persistence.upsert_episode(ep)
                settled = True
        return bool(changed) or bool(healed) or settled

    _rail_refresh = {"running": False, "again": False, "mtime": 0.0, "head": None}

    async def _refresh_rail_cache() -> None:
        """Rebuild the tiny rail boot cache off-loop — serialized + coalesced so a burst of captures
        collapses to one trailing rebuild. The BUILD (parses the full store) runs on the DEFAULT
        pool, NEVER on ``_boot_pool`` — that pool is reserved for tiny cache READS (story + rail
        boot), so a rebuild can never queue behind / starve a first-paint read. Best-effort."""
        if _rail_refresh["running"]:
            _rail_refresh["again"] = True
            return
        _rail_refresh["running"] = True
        try:
            loop = asyncio.get_event_loop()
            while True:
                _rail_refresh["again"] = False
                await loop.run_in_executor(None, _cache_rail_sync)   # heavy build off the boot pool
                if not _rail_refresh["again"]:
                    break
        except Exception:  # noqa: BLE001 — cache refresh must never break capture / a land
            logger.debug("rail cache refresh failed", exc_info=True)
        finally:
            _rail_refresh["running"] = False

    async def _rail_cache_poller() -> None:
        """Keep the rail current through ALL mutation paths, off the request path. Each tick:
          (1) HEAD-gated — when a commit lands, attribute OpenFDE's trailer-less commits to their
              episodes + mark them landed (the reliable rail-attribution path; rebuilds the Story
              cache + nudges clients when it changes anything), then
          (2) mtime-gated — rebuild the tiny rail boot cache so first paint shows the latest ~10.
        Cheap when idle: a HEAD rev-parse + a stat, real work only on a change."""
        while True:
            try:
                await asyncio.sleep(5)
                loop = asyncio.get_event_loop()
                # (1) Attribute newly-landed OpenFDE commits → episodes (only when HEAD moves).
                reconciled = False
                head = await loop.run_in_executor(None, _head_sha)
                if head and head != _rail_refresh["head"]:
                    _rail_refresh["head"] = head
                    reconciled = await loop.run_in_executor(None, _reconcile_landed_sync)
                # (2) Rebuild the tiny rail boot cache FAST when the store changed (reconcile above,
                #     or any capture/land/create) — this is the user-visible first-paint surface.
                try:
                    mt = persistence.episodes_path.stat().st_mtime
                except OSError:
                    mt = None
                if mt is not None and mt != _rail_refresh["mtime"]:
                    _rail_refresh["mtime"] = mt
                    await _refresh_rail_cache()
                # (3) Story cache is the heavy build — refresh it in the BACKGROUND so it never
                #     delays the rail boot rebuild above (the bug: an ~8s Story build blocked it).
                if reconciled:
                    asyncio.create_task(_refresh_story_cache_and_broadcast())
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — a poller tick must never crash the loop
                logger.debug("rail cache poller tick failed", exc_info=True)

    async def get_story_boot(request: web.Request) -> web.Response:
        """CACHE-ONLY Story boot — the last-known-good recent Story (latest ~10 product episodes +
        counts + the capped graph), served instantly so first paint never waits on the full rebuild.
        No cache yet → a NON-authoritative empty (``building:true``) so the UI shows "Restoring
        Story…", never "No concepts yet". The full graph + the authoritative empty come from
        /api/story/prompt-graph."""
        loop = asyncio.get_event_loop()
        cached = await loop.run_in_executor(
            _boot_pool, lambda: story_cache_mod.read_story_cache(persistence.openfde_dir))
        return web.json_response(cached or story_cache_mod.empty_boot())

    async def get_prompt_story_graph(request: web.Request) -> web.Response:
        """Prompt Story Graph — the FULL conceptual narrative, the **authoritative** Story source
        (/api/story/boot serves the cached recent slice for first paint).

        Served from the in-memory last-built graph (``confirmed:true``) so it NEVER rebuilds on a
        request — the ~8s GIL-heavy build would otherwise run once per open tab and starve every
        boot read. When no graph is built yet, this returns a NON-authoritative ``building`` result
        (``confirmed:false`` → the UI keeps the boot cache, never "No concepts yet") and kicks the
        coalesced background rebuild, which populates the memory + cache and broadcasts
        ``story_updated`` so clients re-fetch the authoritative graph. So the heavy build happens
        once per *change*, in the background, off the request/event-loop contention path.

        Returns:
            web.Response — {ok, confirmed, concepts[], episodes[], edges[], counts, storyMap,
            storyTimeline, storyNarrative}.
        """
        if _full_graph_mem["graph"] is not None:
            return web.json_response({**_full_graph_mem["graph"], "confirmed": True})
        asyncio.create_task(_refresh_story_cache_and_broadcast())     # build off-request; broadcasts when ready
        return web.json_response({"ok": True, "confirmed": False, "building": True,
                                  "concepts": [], "episodes": [], "edges": [], "counts": {},
                                  "lifecycleCounts": {}, "storyMap": {}, "storyTimeline": {},
                                  "storyNarrative": {}})


    async def post_summarize_episodes(request: web.Request) -> web.Response:
        """On-demand LLM story summary: upgrade up to ``limit`` eligible episodes (default 1)
        using the local CLI, off the event loop. Broadcasts ``episode_updated`` for any
        upgraded. Returns immediately with deterministic data when no provider is available.

        Returns:
            web.Response — {ok, providers, upgraded, attempted}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = {}
        limit = max(1, min(int(body.get("limit") or 1), 8))
        providers = episode_llm_summary.available_providers()
        if not providers:
            return web.json_response({"ok": True, "providers": [], "upgraded": 0,
                                      "attempted": 0, "reason": "no local CLI provider"})
        before = {e["episodeId"]: (e.get("summarySource"), e.get("title"))
                  for e in persistence.load_episodes()}
        loop = asyncio.get_event_loop()
        eps = await loop.run_in_executor(
            None,
            lambda: episode_llm_summary.ensure_facts(persistence, allow_llm=True,
                                                     providers=providers, limit=limit))
        upgraded = 0
        for e in eps:
            b = before.get(e["episodeId"])
            if b and (b[0], b[1]) != (e.get("summarySource"), e.get("title")):
                upgraded += 1
                await manager.broadcast({"type": "episode_updated", "episode": e})
        return web.json_response({"ok": True, "providers": providers, "upgraded": upgraded,
                                  "attempted": limit})

    async def post_review_episode_create(request: web.Request) -> web.Response:
        """Create a prompt episode (e.g. when a prompt/run starts, or a 'Manual
        changes' bucket for hand-edited worktree changes about to be landed).

        Returns:
            web.Response — {ok, episode}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = {}
        now = datetime.now(timezone.utc).isoformat()
        ep = {
            "episodeId": "episode_" + secrets.token_hex(6),
            "createdAt": now, "updatedAt": now,
            "prompt": (body.get("prompt") or "").strip(),
            "kind": body.get("kind") or "manual",
            "status": body.get("status") or "open",
            "runIds": [r for r in (body.get("runIds") or []) if r],
            "eventIds": [], "projectEntryIds": [], "commitShas": [],
            "files": [], "summary": (body.get("summary") or "").strip(),
        }
        # Durable intent this work serves (e.g. a GitHub issue card): carried on the
        # episode so commits/Story can trace back to the issue. Optional, shape-checked
        # only loosely — the issue import path produces it, manual callers may omit it.
        if isinstance(body.get("intentSource"), dict):
            ep["intentSource"] = body["intentSource"]
        persistence.upsert_episode(ep)
        await manager.broadcast({"type": "episode_updated", "episode": ep})
        return web.json_response({"ok": True, "episode": ep})

    async def post_review_episode_land(request: web.Request) -> web.Response:
        """Land the current meaningful worktree changes through OpenFDE — the only
        user-facing path that creates a commit.

        Commits with OpenFDE trailers (Episode / Run / Project-Entry), links the
        commit to the episode, marks it landed, and broadcasts commit_created.
        No-ops cleanly when there are no meaningful changes. Unknown / 'manual'
        episodeIds create a fresh "Manual changes" episode.

        Returns:
            web.Response — {ok, committed, sha?, shortSha?, reason?, episode}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = {}
        eid = request.match_info.get("episodeId", "")
        now = datetime.now(timezone.utc).isoformat()
        ep = persistence.get_episode(eid)
        if ep is None:
            # Manual / unknown id → mint a "Manual changes" episode to own the commit.
            ep = {
                "episodeId": "episode_" + secrets.token_hex(6),
                "createdAt": now, "updatedAt": now,
                "prompt": (body.get("prompt") or "Manual changes").strip(),
                "kind": "manual", "status": "open", "runIds": [], "eventIds": [],
                "projectEntryIds": [], "commitShas": [], "files": [], "summary": "",
            }

        # A real episode with attributed files → SCOPED Land (commit only its files,
        # never unrelated dirty ones). The "Manual changes" bucket (no files) falls back
        # to a whole-tree commit of the meaningful changes the user is explicitly landing.
        from openfde import autoland
        land = await asyncio.get_event_loop().run_in_executor(
            None, lambda: autoland.land_episode(path, persistence, ep, auto=False, allow_llm=True))
        if not land.get("needsWholeTree"):
            for m in land.get("broadcasts", []):
                await manager.broadcast(m)
            if land.get("committed"):
                return web.json_response({"ok": True, "committed": True, "sha": land["sha"],
                                          "shortSha": land["shortSha"], "episode": land["episode"],
                                          "files": land.get("files", [])})
            return web.json_response({"ok": True, "committed": False, "status": land.get("status"),
                                      "reason": land.get("reason") or "no changes to land",
                                      "episode": land["episode"]})

        # ── Whole-tree fallback (Manual changes bucket) ──────────────────────
        # Only land when there are real, meaningful (non-ignored) changes.
        imp = worktree_impact(path)
        if not imp.get("dirty"):
            return web.json_response({"ok": True, "committed": False,
                                      "reason": "no meaningful changes to land", "episode": ep})

        trailers = {"OpenFDE-Episode": ep["episodeId"]}
        run_id = next((r for r in reversed(ep.get("runIds") or []) if r), None)
        if run_id:
            trailers["OpenFDE-Run"] = run_id
        entry_id = next((e for e in reversed(ep.get("projectEntryIds") or []) if e), None)
        if entry_id:
            trailers["OpenFDE-Project-Entry"] = entry_id

        subject = (body.get("message") or "").strip() or _episode_subject(ep, imp)
        commit = git_commit(path, subject, detail=_land_body(ep, imp), trailers=trailers)
        if not commit.get("committed"):
            return web.json_response({"ok": True, "committed": False,
                                      "reason": commit.get("reason") or "commit produced no changes",
                                      "episode": ep})

        ep["commitShas"] = list(dict.fromkeys((ep.get("commitShas") or []) + [commit["sha"]]))
        ep["files"] = sorted(set((ep.get("files") or []) + (commit.get("files") or [])))
        ep["status"] = "landed"
        ep["updatedAt"] = now
        persistence.upsert_episode(ep)

        ce = persistence.append_event({
            "type": "commit_created",
            "payload": {"sha": commit["sha"], "shortSha": commit["shortSha"],
                        "summary": commit["summary"], "episodeId": ep["episodeId"],
                        "fileCount": len(commit.get("files", [])),
                        "detail": f"Landed {commit['shortSha']} for episode {ep['episodeId']}"},
        })
        await manager.broadcast({"type": "event_appended", "event": ce})
        await manager.broadcast({"type": "episode_updated", "episode": ep})
        _write_warm_after_episode(ep)        # land = a trusted restore point → warm-cache it now
        # A land changes the Story (new commits, lifecycle) — refresh the Story + rail boot caches
        # off-loop and broadcast story_updated so open clients re-hydrate without a manual refetch.
        asyncio.create_task(_refresh_story_cache_and_broadcast())
        asyncio.create_task(_refresh_rail_cache())
        # Explicit commit_created so any client can mirror the prompt→commit story
        # into OpenPM (a Done task under this prompt) without waiting on a poll.
        _dt, _ds = commit_display(ep.get("title"), ep.get("summary"), commit["summary"])
        await manager.broadcast({
            "type": "commit_created", "sha": commit["sha"], "shortSha": commit["shortSha"],
            "summary": commit["summary"], "episodeId": ep["episodeId"],
            "episodeTag": ep.get("tag"), "promptTitle": ep.get("title"), "sequence": ep.get("sequence"),
            "displayTitle": _dt, "displaySummary": _ds,
            "promptLabel": ep.get("title") or (ep.get("prompt") or ep.get("summary") or "").split("\n")[0][:48],
            "files": commit.get("files", []),
        })
        return web.json_response({
            "ok": True, "committed": True, "sha": commit["sha"], "shortSha": commit["shortSha"],
            "episode": ep, "files": commit.get("files", []),
        })

    # ================================================================== #
    #  REST — /api/concept*  (Ask Concept + Concept Cards, Step 37a)     #
    # ================================================================== #

    _ARCH_KW = ("why", "architecture", "scope", "concept", "missed", "affected",
                "depend", "design", "where", "purpose", "should", "risk", "boundary", "mean")
    _SD_KW = ("code", "function", "implementation", "diff", "edited", "edit", "bug",
              "line", "refactor", "logic", "variable", "method", "how does", "how do")
    _ROLE_HUMAN = {"architect": "Architect", "senior_dev": "Senior Dev", "verifier": "Verifier"}

    def _classify_concept_question(q: str) -> str:
        """v1 deterministic router: architecture/why/concept → Architect; code/impl
        → Senior Dev; ties + uncertainty → Architect (it can defer to Sr Dev later)."""
        ql = (q or "").lower()
        sd = sum(1 for k in _SD_KW if k in ql)
        arch = sum(1 for k in _ARCH_KW if k in ql)
        return "senior_dev" if sd > arch else "architect"

    def _concept_prompt(question: str, ctx: dict) -> str:
        lines = []
        if ctx.get("kind") == "commit":
            lines.append(f"Commit: {ctx.get('summary') or ctx.get('label')}")
            files = ctx.get("files", [])
            lines.append(f"Files changed ({len(files)}): {', '.join(files[:20])}")
            cs = ctx.get("concepts", [])
            if cs:
                lines.append("Affected concepts: " + "; ".join(
                    f"{c['identifier']} ({c.get('touched')}/{c.get('total')}"
                    f"{' PARTIAL' if c.get('partial') else ''})" for c in cs[:12]))
        elif ctx.get("kind") == "episode":
            # A prompt episode is a RECORD of work that happened — ground the answer
            # in its prompt/summary/commits/files, never in concept-grep framing
            # (an episode title naturally "appears in 0 files"; saying so made the
            # model conclude the work was never implemented — observed live).
            lines.append(f"Prompt episode {ctx.get('tag') or ''}: {ctx.get('label')}".strip())
            if ctx.get("status"):
                lines.append(f"Status: {ctx['status']}")
            if ctx.get("summary"):
                lines.append(f"Summary: {ctx['summary']}")
            commits = ctx.get("commits") or []
            if commits:
                lines.append("Landed commits: " + "; ".join(
                    f"{(c.get('sha') or '')[:7]} {c.get('title') or ''}".strip()
                    for c in commits[:8]))
            files = ctx.get("files", [])
            if files:
                lines.append(f"Files attributed ({len(files)}): {', '.join(files[:20])}")
            if ctx.get("prompt"):
                lines.append("Original prompt (excerpt):")
                lines.append(str(ctx["prompt"])[:1200])
        else:
            files = ctx.get("files", [])
            lines.append(f"Concept: {ctx.get('label')} ({ctx.get('kind') or 'identifier'})")
            lines.append(f"Appears in {len(files)} files: {', '.join(files[:20])}")
        lines.append("")
        lines.append(f"Question: {question}")
        return "\n".join(lines)

    def _concept_fallback(ctx: dict, question: str) -> str:
        note = (" (No model provider configured for this role — deterministic summary "
                "from the semantic graph. Set a provider in Agent Settings for a richer answer.)")
        if ctx.get("kind") == "commit":
            files, cs = ctx.get("files", []), ctx.get("concepts", [])
            partial = [c for c in cs if c.get("partial")]
            out = [f'This commit "{ctx.get("summary") or ctx.get("label")}" changed {len(files)} file(s).']
            if cs:
                out.append("It touched these concepts: " + ", ".join(c["identifier"] for c in cs[:8]) + ".")
            if partial:
                p = partial[0]
                out.append(f'Heads up: "{p["identifier"]}" lives in {p.get("total")} places but this change '
                           f'touched only {p.get("touched")} — untouched: {", ".join(p.get("untouchedFiles", [])[:4])}.')
            return " ".join(out) + note
        files = ctx.get("files", [])
        return (f'"{ctx.get("label")}" is a {ctx.get("kind") or "concept"} OpenFDE tracks as a tether — '
                f'it appears in {len(files)} file(s): {", ".join(files[:8])}.' + note)

    async def post_concept_ask(request: web.Request) -> web.Response:
        """Ask a question about the active concept/commit; route to Architect or
        Senior Dev (deterministic v1) using their configured local/API provider,
        else return a deterministic semantic-graph answer. Never a raw code dump."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        question = (body.get("question") or "").strip()
        ctx = body.get("context") if isinstance(body.get("context"), dict) else {}
        if not question:
            return web.json_response({"ok": False, "error": "question required"}, status=400)

        role = _classify_concept_question(question)
        settings = persistence.load_agent_settings()
        caller = _text_role(settings.get(role, {}))
        used_role = role
        if not caller and role != "architect":  # chosen role has no text provider → Architect
            caller, used_role = _text_role(settings.get("architect", {})), "architect"

        answer, source = "", ""
        if caller:
            sys_prompt = (f"You are the {_ROLE_HUMAN.get(used_role, used_role)} in OpenFDE. Answer the "
                          "question about this concept/commit/prompt-episode at the architecture level — "
                          "concise, plain language, 2-5 sentences, NO code dumps. Ground the answer ONLY "
                          "in the provided context; when it is insufficient, say what is missing instead "
                          "of speculating about unbuilt features.")
            user = _concept_prompt(question, ctx)
            try:
                loop = asyncio.get_event_loop()
                answer = await loop.run_in_executor(None, lambda: caller(sys_prompt, user))
            except Exception as exc:  # noqa: BLE001
                logger.error("concept ask failed: %s", exc)
                answer = ""
            prov = settings.get(used_role, {}).get("provider")
            source = f"{_ROLE_HUMAN.get(used_role, used_role)} · {prov}"
        if not (answer or "").strip():
            answer, source = _concept_fallback(ctx, question), "OpenFDE · semantic graph"
        return web.json_response({"ok": True, "answer": answer.strip(),
                                  "role": used_role, "source": source})

    # ── The repair hatch (v3) — fingerprinted failure artifacts ──
    # A failing check is an implementation issue: explanation + repair prompt
    # speak through the Agent Council SENIOR_DEV role config (whatever provider
    # it holds). The failure FLOW derives deterministically (AST + receipts);
    # the Verifier (else Architect) text role may humanize labels ONLY.
    # Artifacts persist on the OWNING episode keyed by (kind, fingerprint): the
    # LLM runs once per failure meaning, reuse is the default, regenerate is
    # explicit and replaces. Never a new episode; never the user's terminal.

    _HATCH_PROVIDER_LABELS = {
        "claude-code-local": "Claude Code local", "codex-local": "Codex local",
        "anthropic": "Anthropic", "openai-compatible": "OpenAI-compatible",
        "openrouter": "OpenRouter", "ollama": "Ollama", "echo": "Echo"}

    def _openfde_version() -> str:
        """The install's identity in a form the TRACKER can use: the nearest
        commit that exists on the remote, plus the local distance — a bare
        local HEAD hash means nothing to anyone reading the issue."""
        own = str(Path(__file__).resolve().parents[1])

        def _git(*args):
            try:
                p = subprocess.run(["git", "-C", own, *args],
                                   capture_output=True, text=True, timeout=10)
                return p.stdout.strip() if p.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                return ""
        head = _git("rev-parse", "--short", "HEAD") or "unknown"
        for ref in ("origin/main", "origin/master"):
            base = _git("merge-base", "HEAD", ref)
            if base:
                base_short = _git("rev-parse", "--short", base) or base[:7]
                ahead = _git("rev-list", "--count", f"{base}..HEAD") or "0"
                return base_short if ahead == "0" else f"{base_short}+{ahead} local commits"
        return f"{head} (local build, not on the remote)"

    _OPENFDE_COMMIT = _openfde_version()

    def _hatch_ctx(body: dict) -> dict:
        return {"file": body.get("file", ""), "line": body.get("line", ""),
                "test": body.get("test", ""), "funcName": body.get("funcName", ""),
                "start": body.get("start", ""), "end": body.get("end", ""),
                "code": (body.get("code") or "")[:4000],
                "episodeId": body.get("episodeId") or "",
                "checkId": body.get("checkId") or "",
                "failureMsg": (body.get("failureMsg") or "")[:2000]}

    def _hatch_fp(c: dict, override: str = "") -> str:
        return override or failure_flow_mod.failure_fingerprint(
            episode_id=c["episodeId"], check_id=c["checkId"], file=c["file"],
            line=c["line"], func=c["funcName"], test=c["test"],
            failure_msg=c["failureMsg"], code=c["code"])

    def _hatch_text_role(primary: str):
        """(caller, caption) for a text role; falls back to the Architect's."""
        settings = persistence.load_agent_settings()
        order = [primary] + ([] if primary == "architect" else ["architect"])
        for role in order:
            caller = _text_role(settings.get(role, {}))
            if caller:
                prov = (settings.get(role, {}) or {}).get("provider")
                label = _HATCH_PROVIDER_LABELS.get(prov, prov or "?")
                return caller, f"{_ROLE_HUMAN.get(role, role)} · {label}"
        return None, ""

    def _artifact_base(c: dict, kind: str, fp: str) -> dict:
        return {"kind": kind, "fingerprint": fp, "checkId": c["checkId"],
                "file": c["file"], "line": c["line"], "function": c["funcName"],
                "test": c["test"]}

    async def _hatch_store(c: dict, art: dict) -> dict:
        """Persist on the owning episode (when known) + broadcast; never a new one."""
        if not c["episodeId"]:
            return art
        stored = persistence.upsert_repair_artifact(c["episodeId"], art)
        if stored:
            ep = persistence.get_episode(c["episodeId"])
            if ep:
                await manager.broadcast({"type": "episode_updated", "episode": ep})
            return stored
        return art

    def _hatch_reuse(c: dict, kind: str, fp: str):
        """The run-LLM-once law: a saved artifact for this failure meaning wins."""
        if not c["episodeId"]:
            return None
        for a in persistence.get_repair_artifacts(c["episodeId"], fp):
            if a.get("kind") == kind:
                return a
        return None

    async def _hatch_compose(c: dict, sys_prompt: str, fallback: str):
        """Senior-Dev text composition grounded in the function code; honest caption."""
        caller, caption = _hatch_text_role("senior_dev")
        text = ""
        if caller:
            user = (f"file: {c['file']}\nfunction: {c['funcName']} "
                    f"(lines {c['start']}-{c['end']})\n"
                    f"failing: {c['test'] or 'check'} at line {c['line']}\n"
                    + (f"failure output: {c['failureMsg']}\n" if c['failureMsg'] else "")
                    + f"\nfunction code:\n{c['code']}")
            try:
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(None, lambda: caller(sys_prompt, user))
            except Exception as exc:  # noqa: BLE001
                logger.error("hatch compose failed: %s", exc)
        if (text or "").strip():
            return text.strip(), caption
        return fallback, "OpenFDE · template"

    def _hatch_fallback_prompt(c: dict) -> str:
        return (f"In {c['file']}, function {c['funcName']}() "
                f"(lines {c['start']}–{c['end']}): "
                f"{('test ' + c['test']) if c['test'] else 'a check'} fails at "
                f"line {c['line']}. Fix the function so the check passes, without "
                "changing the test.")

    async def _hatch_generate(request: web.Request, kind: str,
                              sys_prompt: str, fallback_fn) -> web.Response:
        """Shared explain/prompt path: reuse by fingerprint unless regenerate."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        c = _hatch_ctx(body)
        fp = _hatch_fp(c, body.get("fingerprint") or "")
        if not body.get("regenerate"):
            saved = _hatch_reuse(c, kind, fp)
            if saved:
                return web.json_response({"ok": True, "artifact": saved,
                                          "fingerprint": fp, "reused": True})
        text, source = await _hatch_compose(c, sys_prompt, fallback_fn(c))
        art = {**_artifact_base(c, kind, fp), "source": source, "text": text,
               "summary": text.split("\n")[0][:160]}
        art = await _hatch_store(c, art)
        return web.json_response({"ok": True, "artifact": art,
                                  "fingerprint": fp, "reused": False})

    async def post_hatch_explain(request: web.Request) -> web.Response:
        """Explain WHY the check fails (senior_dev role); once per fingerprint.

        Returns:
            web.Response — {ok, artifact, fingerprint, reused}.
        """
        sys_prompt = ("You are the Senior Dev in OpenFDE. Explain WHY this check fails, "
                      "grounded ONLY in the provided function code and failure location: "
                      "what the failing line asserts, what actually happens, and the most "
                      "likely smallest fix. Plain language, 2-4 sentences; markdown for "
                      "emphasis and inline code is fine; no large code dumps.")
        return await _hatch_generate(
            request, "failure_explanation", sys_prompt,
            lambda c: (f"{c['test'] or 'A check'} fails at `{c['file']}:{c['line']}` inside "
                       f"`{c['funcName']}()` — the assertion on that line doesn't hold. "
                       "Compare what the line asserts with what the code above it produces."))

    async def post_hatch_prompt(request: web.Request) -> web.Response:
        """Compose the paste-ready repair prompt (senior_dev role); once per fingerprint.

        Returns:
            web.Response — {ok, artifact, fingerprint, reused}.
        """
        sys_prompt = ("You are the Senior Dev in OpenFDE. Compose a precise, paste-ready "
                      "prompt for a coding agent to FIX this failing check. Implementation "
                      "voice: name the file, the function, the exact failing line, what the "
                      "assertion expects, and the smallest correct change. 2-4 sentences. "
                      "Output ONLY the prompt text — no preamble, no quotes, no headers.")
        return await _hatch_generate(request, "repair_prompt", sys_prompt,
                                     _hatch_fallback_prompt)

    async def post_hatch_flow(request: web.Request) -> web.Response:
        """Derive the failure FLOW — how the failure got there; once per fingerprint.

        Deterministic AST/receipt evidence is the graph; the Verifier (else
        Architect) text role may only rewrite edge labels + summary (strict
        JSON, fallback to deterministic).

        Returns:
            web.Response — {ok, artifact, fingerprint, reused}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        c = _hatch_ctx(body)
        if not c["file"]:
            return web.json_response({"ok": False, "error": "file required"}, status=400)
        fp = _hatch_fp(c, body.get("fingerprint") or "")
        if not body.get("regenerate"):
            saved = _hatch_reuse(c, "failure_flow", fp)
            if saved:
                return web.json_response({"ok": True, "artifact": saved,
                                          "fingerprint": fp, "reused": True})
        loop = asyncio.get_event_loop()
        flow = await loop.run_in_executor(None, lambda: failure_flow_mod.build_failure_flow(
            path, file=c["file"], line=int(c["line"] or 0),
            func=c["funcName"], test=c["test"], output_tail=c["failureMsg"]))
        caller, caption = _hatch_text_role("verifier")
        used = False
        if caller:
            flow, used = await loop.run_in_executor(
                None, lambda: failure_flow_mod.humanize_flow(flow, caller))
        art = {**_artifact_base(c, "failure_flow", fp),
               "primaryPath": flow.get("primaryPath") or [],
               "primaryEdges": flow.get("primaryEdges") or [],
               "source": caption if used else "OpenFDE · static analysis",
               "text": "", "summary": flow.get("summary", ""),
               "nodes": flow.get("nodes") or [], "edges": flow.get("edges") or []}
        art = await _hatch_store(c, art)
        return web.json_response({"ok": True, "artifact": art,
                                  "fingerprint": fp, "reused": False})

    async def post_feedback_draft(request: web.Request) -> web.Response:
        """Draft the report-to-OpenFDE issue — the USER's senior_dev writes it
        (their provider, their cost), fed the ACCURATE receipt for precision.
        Two guarantees keep the tracker repo-clean anyway: a hard output
        contract, then a deterministic scrub of every known repo string
        (paths, basenames, test names, repo name) from the draft. The template
        is the fallback when no provider answers.

        Returns:
            web.Response — {ok, title, body, source}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        run = body.get("run") if isinstance(body.get("run"), dict) else {}
        hctx = body.get("hatch") if isinstance(body.get("hatch"), dict) else {}
        run = {k: run.get(k) for k in ("status", "error", "summary", "recheck",
                                       "scope", "source", "openfdeVersion", "writes")}
        # Cost never reaches any display or report — strip the legacy "(cost $X)"
        # suffix from receipts written before the runner stopped embedding it.
        for k in ("error", "summary"):
            if isinstance(run.get(k), str):
                run[k] = re.sub(r"\s*\(cost \$[\d.]+\)", "", run[k])
        run["openfdeVersion"] = run.get("openfdeVersion") or _OPENFDE_COMMIT
        repls = issue_repro_mod.report_replacements(
            run.get("scope") or [], hctx.get("file") or "", hctx.get("test") or "",
            Path(str(path)).name)
        caller, caption = _hatch_text_role("senior_dev")
        if caller:
            sys_prompt = (
                "You are the Senior Dev in OpenFDE, drafting a bug report for "
                "OPENFDE'S OWN public tracker about a failure of OpenFDE's repair-run "
                "machinery — NOT about the user's repository. You receive the full "
                "receipt, including private repo details, for accuracy. You MUST NOT "
                "reproduce any repo-identifying detail: no file paths, no test names, "
                "no code, no check output — refer to them generically ('the test leg', "
                "'the source file', 'the failing check'). Quote OpenFDE's own contract "
                "reason verbatim (it is OpenFDE's string). Structure: '## OpenFDE bug "
                "report'; bullet Feature / OpenFDE "
                "commit / Provider path; '## How it was produced (OpenFDE actions "
                "only)' numbered steps; '## Expected'; '## Actual' with status/reason/"
                "recheck; '## Suspected area / possible fix' naming the OpenFDE "
                "module(s) likely responsible and a concrete fix idea. Return ONLY "
                'JSON {"title": str, "body": str} — body is markdown.')
            user = json.dumps({
                "feature": "Repair hatch → Run with Senior Dev (scoped repair runner; "
                           "pipeline: failing receipt → Show → hatch → Generate prompt "
                           "(fingerprint-cached) → runner with editable scope + "
                           "allow_dirty + no-commit directive → single-test recheck)",
                "run_receipt": run,
                "failure_context": {"file": hctx.get("file"), "line": hctx.get("line"),
                                    "test": hctx.get("test"),
                                    "check_output_tail": (hctx.get("failureMsg") or "")[:900]},
            }, ensure_ascii=False)
            try:
                loop = asyncio.get_event_loop()
                raw = await loop.run_in_executor(None, lambda: caller(sys_prompt, user))
                m = re.search(r"\{.*\}", raw or "", re.S)
                data = json.loads(m.group(0)) if m else {}
                title = (data.get("title") or "").strip()
                text = (data.get("body") or "").strip()
                if title and text:
                    title = issue_repro_mod.scrub_report(title, repls)[:200]
                    text = ("<!-- openfde:report v=1 kind=repair-run -->\n"
                            + issue_repro_mod.scrub_report(text, repls))
                    return web.json_response({"ok": True, "title": title, "body": text,
                                              "source": caption})
            except Exception as exc:  # noqa: BLE001 — fall through to the template
                logger.warning("report draft failed: %s", exc)
        title, text = issue_repro_mod.deterministic_report(run, hctx)
        text = "<!-- openfde:report v=1 kind=repair-run -->\n" + text
        return web.json_response({"ok": True, "title": title, "body": text,
                                  "source": "OpenFDE · template"})

    async def post_feedback_draft_general(request: web.Request) -> web.Response:
        """Draft a GENERAL product-feedback issue (bug / feature / UX / performance)
        for OpenFDE's OWN tracker. The ARCHITECT writes it from the user's
        description plus LIGHT app context (view, OpenFDE version, maybe an episode
        title) — never the watched repo's source/paths/tests/logs. The draft is
        deterministically scrubbed and fully editable; NOTHING posts here (the user
        clicks Raise issue). Falls back to a template when no Architect provider
        answers, so the flow works offline.

        Returns:
            web.Response — {ok, title, body, source}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        description = (body.get("description") or "").strip()[:4000]
        if not description:
            return web.json_response({"ok": False, "error": "description required"}, status=400)
        kind = (body.get("kind") or "other").strip().lower()[:20]
        raw_ctx = body.get("context") if isinstance(body.get("context"), dict) else {}
        # Curated, product-level context ONLY — never repo files/paths. The version
        # is OpenFDE's own commit (the install, not the watched repo).
        ctx = {k: raw_ctx.get(k) for k in ("view", "episode", "recentEvents")}
        ctx["openfdeVersion"] = _OPENFDE_COMMIT
        repo_name = Path(str(path)).name           # a scrub needle, never displayed
        caller, _cap = _hatch_text_role("architect")
        loop = asyncio.get_event_loop()
        out = await loop.run_in_executor(
            None, lambda: feedback_mod.draft_general(description, kind, ctx,
                                                     repo_name, caller=caller))
        return web.json_response({"ok": True, **out})

    async def post_feedback_issue(request: web.Request) -> web.Response:
        """File an OpenFDE bug — on OUR tracker, never the watched repo's.

        The UI shows the prefilled title/body for review; THIS endpoint only
        fires on the user's explicit click. Repo slug comes from the OpenFDE
        install's own git remote.

        Returns:
            web.Response — {ok, url} | {ok: False, error}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        title = (body.get("title") or "").strip()[:200]
        text = (body.get("body") or "").strip()[:8000]
        if not title:
            return web.json_response({"ok": False, "error": "title required"}, status=400)
        # General product feedback carries a kind / labels hint (or the general
        # marker); the repair-hatch path carries neither and is handled exactly as
        # before. THIS endpoint only fires on the user's explicit click either way.
        kind = (body.get("kind") or "").strip().lower()[:20]
        hint = [str(lb) for lb in (body.get("labels") or []) if isinstance(lb, str)][:4]
        is_general = bool(kind or hint) or "kind=general-feedback" in text
        own_root = Path(__file__).resolve().parents[1]
        try:
            url = subprocess.run(["git", "-C", str(own_root), "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            url = ""
        m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
        if not m:
            return web.json_response({"ok": False,
                                      "error": "OpenFDE install has no GitHub remote"})
        slug = m.group(1)
        if not shutil.which("gh"):
            return web.json_response({"ok": False, "error": "gh CLI not installed"})

        loop = asyncio.get_event_loop()

        # ── Labels — the future auto-pull triages by these. The exhaustive seed
        # taxonomy (product surfaces + the repair-path labels) exists idempotently;
        # the role classifies among EXISTING labels and may mint ONE new kebab-case
        # label only when nothing fits (GitHub allows label creation, so the
        # taxonomy grows with the product). ──
        seeds = feedback_mod.SEED_LABELS

        def _gh(args, timeout=30):
            return subprocess.run(["gh", *args], capture_output=True, text=True,
                                  timeout=timeout)

        def _prepare_labels():
            try:
                proc = _gh(["label", "list", "-R", slug, "--json",
                            "name,description", "--limit", "200"])
                have = {l["name"]: (l.get("description") or "")
                        for l in (json.loads(proc.stdout or "[]")
                                  if proc.returncode == 0 else [])}
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                have = {}
            for n, d in seeds:
                if n not in have:
                    _gh(["label", "create", n, "-R", slug, "-d", d])
                    have[n] = d
            return have

        have = await loop.run_in_executor(None, _prepare_labels)
        _classify_sys = ("Classify a GitHub issue for the OpenFDE repo. Prefer "
                         "EXISTING labels (pick 1-3). Only when none fit, propose ONE "
                         "new label (kebab-case, short description). Return ONLY JSON "
                         '{"labels": ["..."], "new": {"name": "...", '
                         '"description": "..."} or null}.')
        if is_general:
            # General product feedback: the deterministic base prefers the seeded
            # labels (the kind chip + any hint); the ARCHITECT may add more existing
            # labels or mint one new when nothing fits. Robust without a provider.
            chosen = feedback_mod.select_labels(kind, hint, have)
            caller, _cap = _hatch_text_role("architect")
            if caller:
                listing = "\n".join(f"- {n}: {d}" for n, d in sorted(have.items()))
                user2 = f"title: {title}\n\nbody:\n{text[:3000]}\n\nexisting labels:\n{listing}"
                try:
                    raw = await loop.run_in_executor(None, lambda: caller(_classify_sys, user2))
                    m2 = re.search(r"\{.*\}", raw or "", re.S)
                    data = json.loads(m2.group(0)) if m2 else {}
                    picks = [lb for lb in (data.get("labels") or []) if isinstance(lb, str)]
                    new = data.get("new")
                    if isinstance(new, dict) and new.get("name") and not any(p in have for p in picks):
                        nm = re.sub(r"[^a-z0-9-]+", "-", str(new["name"]).lower()).strip("-")[:40]
                        if nm and nm not in have:
                            await loop.run_in_executor(None, lambda: _gh(
                                ["label", "create", nm, "-R", slug,
                                 "-d", str(new.get("description") or "")[:90]]))
                            have[nm] = ""
                        if nm:
                            picks.append(nm)
                    chosen = feedback_mod.select_labels(kind, hint, have, picks=picks)
                except Exception as exc:  # noqa: BLE001 — labels degrade, never block
                    logger.warning("label classification failed: %s", exc)
        else:
            # Repair-hatch path — unchanged: the user's sr_dev classifies the receipt.
            chosen = ["auto-report"]
            caller, _cap = _hatch_text_role("senior_dev")
            if caller:
                listing = "\n".join(f"- {n}: {d}" for n, d in sorted(have.items()))
                user2 = f"title: {title}\n\nbody:\n{text[:3000]}\n\nexisting labels:\n{listing}"
                try:
                    raw = await loop.run_in_executor(None, lambda: caller(_classify_sys, user2))
                    m2 = re.search(r"\{.*\}", raw or "", re.S)
                    data = json.loads(m2.group(0)) if m2 else {}
                    picked = [l for l in (data.get("labels") or [])
                              if isinstance(l, str) and l in have][:3]
                    new = data.get("new")
                    if not picked and isinstance(new, dict) and new.get("name"):
                        nm = re.sub(r"[^a-z0-9-]+", "-", str(new["name"]).lower()).strip("-")[:40]
                        if nm and nm not in have:
                            await loop.run_in_executor(None, lambda: _gh(
                                ["label", "create", nm, "-R", slug,
                                 "-d", str(new.get("description") or "")[:90]]))
                        if nm:
                            picked = [nm]
                    chosen += picked or ["bug"]
                except Exception as exc:  # noqa: BLE001 — labels degrade, never block
                    logger.warning("label classification failed: %s", exc)
                    chosen += ["bug"]
            else:
                chosen += ["bug", "repair-hatch"]
            chosen = list(dict.fromkeys(chosen))

        def _post():
            args = ["issue", "create", "-R", slug, "--title", title, "--body", text]
            for l in chosen:
                args += ["--label", l]
            return _gh(args, timeout=60)
        proc = await loop.run_in_executor(None, _post)
        if proc.returncode != 0:
            return web.json_response({"ok": False,
                                      "error": (proc.stderr or "gh failed").strip()[:300]})
        return web.json_response({"ok": True, "url": (proc.stdout or "").strip(),
                                  "labels": chosen})

    async def post_hatch_artifacts(request: web.Request) -> web.Response:
        """Hydrate: all saved artifacts for this failure meaning (hatch reopen).

        Returns:
            web.Response — {ok, fingerprint, artifacts: {kind: artifact}}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        c = _hatch_ctx(body)
        fp = _hatch_fp(c, body.get("fingerprint") or "")
        arts = persistence.get_repair_artifacts(c["episodeId"], fp) if c["episodeId"] else []
        return web.json_response({"ok": True, "fingerprint": fp,
                                  "artifacts": {a.get("kind"): a for a in arts}})

    async def post_hatch_run(request: web.Request) -> web.Response:
        """Run the repair WITH the configured senior_dev provider, scoped.

        Goes through OpenFDE's own runner (Claude Code: editable=[file] hard
        scope; Codex: workspace-write with the scope stated in the prompt) —
        NEVER by injecting into the user's terminal sessions. The receipt is
        stored as a repair_run artifact on the owning episode; never commits.

        Returns:
            web.Response — {ok, run: artifact}.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        c = _hatch_ctx(body)
        if not c["file"]:
            return web.json_response({"ok": False, "error": "file required"}, status=400)
        fp = _hatch_fp(c, body.get("fingerprint") or "")
        # The agent is starting work on this episode's failure: the card leaves
        # To Do now (not at completion), and a LANDED episode reopens — the fix
        # of a fix joins the SAME story, never a new episode.
        if c["episodeId"]:
            reopened = persistence.reopen_episode(c["episodeId"])
            if reopened is not None:
                await manager.broadcast({"type": "episode_updated", "episode": reopened})
            if persistence.move_tasks_for_episode(c["episodeId"], "doing",
                                                  from_columns=("todo",)):
                await manager.broadcast({"type": "tasks_updated"})
        prompt_text = (body.get("prompt") or "").strip() or _hatch_fallback_prompt(c)
        # The editable scope is the FAILURE, not one file: a failing test is a
        # two-legged contract (test ↔ product code) and the fix may live on
        # either leg — the chain's files are both, and nothing else.
        scope_files = failure_flow_mod.chain_files(path, c["failureMsg"], c["file"])
        task = ("Repair task — scoped to a single failing check. Edit ONLY within "
                f"these files: {', '.join(scope_files)} "
                f"(the failure is at {c['file']} line {c['line']}, "
                f"function {c['funcName']}); touch nothing else. " + prompt_text)
        settings = persistence.load_agent_settings()
        cfg = settings.get("senior_dev", {}) or {}
        prov, model = cfg.get("provider"), cfg.get("model")

        def _go():
            if prov == "claude-code-local":
                out = run_claude_code(repo_root=path, prompt=task, allow_dirty=True,
                                      editable=scope_files, protected=[], model=model)
                res = out.get("result") or {}   # run_agent-shaped: {status, reportSummary, …}
                status = res.get("status") or "failed"
                summary = (res.get("reportSummary") or "")[:400]
                # A failed run must NEVER be mute — the contract's reportSummary
                # carries the why whenever the error field is empty.
                err = out.get("error") or (summary if status == "failed" else None)
                return {"status": status, "summary": summary,
                        "writes": out.get("writes") or [],
                        "rejected": out.get("rejected") or [],
                        "error": err, "costUsd": out.get("costUsd")}
            if prov == "codex-local":
                out = run_codex_local_edit(repo_root=path, prompt=task, model=model)
                return {"status": "passed" if out.get("ok") else "failed",
                        "writes": out.get("touched") or [], "rejected": [],
                        "error": out.get("error"),
                        "summary": (out.get("summary") or "")[:400]}
            return {"status": "failed", "writes": [], "rejected": [],
                    "error": f"Senior Dev provider '{prov or 'none'}' can't run repairs "
                             "— configure a local CLI (Claude Code or Codex) in Agents."}

        loop = asyncio.get_event_loop()
        out = await loop.run_in_executor(None, _go)
        label = _HATCH_PROVIDER_LABELS.get(prov, prov or "?")
        for k in ("error", "summary"):
            if isinstance(out.get(k), str):
                out[k] = re.sub(r"\s*\(cost \$[\d.]+\)", "", out[k])
        art = {**_artifact_base(c, "repair_run", fp), **out,
               "scope": scope_files, "source": f"Senior Dev · {label}",
               "text": out.get("error") or out.get("summary") or ""}
        # Fast honest verdict for the repair ring: does the EXACT failing test
        # pass now? Green is earned, never assumed; the full gate still owns
        # the real receipt (Run checks).
        if art.get("status") not in (None, "failed") and c["test"]:
            checks = verify_mod.discover_checks(path)
            cmd = next((ch["command"] for ch in checks
                        if ch.get("id") == "unit-tests"), None)
            rc = await loop.run_in_executor(
                None, lambda: verify_mod.recheck_single_test(path, cmd, c["test"]))
            art["recheck"] = rc["status"]
            art["recheckTail"] = rc["tail"][-300:]
        # Whose failure is this? A failed RUN is OURS (reportable to OpenFDE's
        # tracker); a clean run whose recheck still fails is the repo's trail.
        # The receipt must answer "which file? what changed?" itself — attach the
        # uncommitted diff of everything the run wrote (vs HEAD; when the file was
        # clean before the run this IS the run's delta, labeled honestly in the UI).
        if art.get("writes"):
            try:
                proc = subprocess.run(["git", "diff", "--", *art["writes"]],
                                      cwd=str(path), capture_output=True,
                                      text=True, timeout=15)
                art["diff"] = (proc.stdout or "")[:4000]
            except (OSError, subprocess.SubprocessError):
                pass
        art["faultDomain"] = verify_mod.run_fault_domain(art)
        art["openfdeVersion"] = _OPENFDE_COMMIT
        art = await _hatch_store(c, art)
        if c["episodeId"] and art.get("status") not in (None, "failed"):
            ep = persistence.get_episode(c["episodeId"])
            if ep is not None and c["file"] not in (ep.get("files") or []):
                ep["files"] = sorted({*(ep.get("files") or []), c["file"]})
                persistence.upsert_episode(ep)
                await manager.broadcast({"type": "episode_updated", "episode": ep})
        return web.json_response({"ok": art.get("status") not in (None, "failed"),
                                  "run": art})

    async def get_concept_cards(request: web.Request) -> web.Response:
        """Return all saved concept cards (newest-first)."""
        return web.json_response({"ok": True, "cards": persistence.load_concept_cards()})

    async def post_concept_card(request: web.Request) -> web.Response:
        """Save a short concept card linked to a tether and/or commit + files."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        title = (body.get("title") or "").strip()
        if not title:
            return web.json_response({"ok": False, "error": "title required"}, status=400)
        card = {
            "id": "card_" + secrets.token_hex(5),
            "title": title[:200],
            "summary": (body.get("summary") or "").strip()[:2000],
            "tetherId": (body.get("tetherId") or None),
            "commitSha": (body.get("commitSha") or None),
            "meaning": (body.get("meaning") or "").strip()[:500],
            "files": body.get("files") if isinstance(body.get("files"), list) else [],
            "relatedFiles": body.get("relatedFiles") if isinstance(body.get("relatedFiles"), list) else [],
            "whyCheck": (body.get("whyCheck") or "").strip()[:1000],
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        persistence.add_concept_card(card)
        return web.json_response({"ok": True, "card": card})

    async def post_compile_workflow(request: web.Request) -> web.Response:
        """Compile the selected scope into a workflow payload + script (preview).

        No side effects — does not persist a run or write artifacts.

        Args:
            request: web.Request — body: {selectedBoxIds, selectedArrowIds, prompt}

        Returns:
            web.Response — JSON {ok, workflowId, payload, script}.
        """
        body = await request.json()
        compiled = _compile_workflow_for(body)
        return web.json_response({
            "ok": True,
            "workflowId": compiled["workflowId"],
            "payload": compiled["payload"],
            "script": compiled["script"],
        })

    async def post_execution_run(request: web.Request) -> web.Response:
        """Prepare a workflow run for the active backend (no auto-execution).

        Compiles the scope, writes a workflow artifact under .openfde/workflows/,
        records a 'prepared' run, appends architect + sr_dev ledger entries and a
        workflow_prepared timeline event. Does NOT run Claude Code or mutate the
        repo.

        Args:
            request: web.Request — body: {selectedBoxIds, selectedArrowIds, prompt}

        Returns:
            web.Response — JSON {ok, status, workflow, run, event,
                                 architectEntry, srDevEntry}.
        """
        body = await request.json()
        active = persistence.load_execution_config().get("activeBackend", ACTIVE_DEFAULT)
        compiled = _compile_workflow_for(body)
        wid = compiled["workflowId"]
        payload, script, ctx = compiled["payload"], compiled["script"], compiled["context"]
        now = datetime.now(timezone.utc).isoformat()

        # ── Git baseline: a workflow may run externally and edit source, so the
        #    watched repo must be a git repo with a real commit *before* prepare
        #    returns. Without this, result intake on a fresh (no-.git) repo can't
        #    diff reported source files and would never commit a real change.
        baseline = ensure_baseline(path)
        if baseline.get("baselineCreated"):
            be = persistence.append_event({
                "type": "commit_created",
                "payload": {"detail": "openfde: baseline before workflow",
                            "head": baseline.get("head"), "kind": "baseline"},
            })
            await manager.broadcast({"type": "event_appended", "event": be})

        box_ids   = [b["id"] for b in ctx.get("boxes", [])]
        file_paths = [f["path"] for f in ctx.get("files", [])]
        arrow_ids = [a["id"] for a in ctx.get("arrows", [])]
        protected = payload["permissions"]["protectedModules"]
        scope_summary = (f"{len(box_ids)} module(s), {len(file_paths)} file(s), "
                         f"{len(ctx.get('functions', []))} function(s)")

        # ── Ledger: architect workflow payload + sr_dev prepared status ─────
        arch_entry = persistence.append_project_log_entry({
            "role": "architect",
            "title": f"Workflow prepared ({active}) — {scope_summary}",
            "summary": f"Compiled the selected scope into a {active} workflow ({wid}).",
            "body": script,
            "boxIds": box_ids, "filePaths": file_paths,
            "metadata": {"backend": active, "workflowId": wid, "kind": "workflow"},
        }, path)
        sr_entry = persistence.append_project_log_entry({
            "role": "sr_dev",
            "title": "Workflow prepared — awaiting execution",
            "summary": ("Prepared only; not executed. Protected scope requires approval."
                        if protected else "Prepared only; not executed. All scoped modules are editable."),
            "body": (f"Backend `{active}` will run Architect → Senior Dev → Verifier → Report. "
                     + (f"Protected modules require approval: {', '.join(protected)}."
                        if protected else "No protected modules in scope.")),
            "metadata": {"backend": active, "workflowId": wid, "status": "prepared"},
        }, path)

        # ── Timeline ─────────────────────────────────────────────────────────
        event = persistence.append_event({
            "type": "workflow_prepared",
            "payload": {
                "workflowId": wid, "backend": active,
                "boxCount": len(box_ids), "fileCount": len(file_paths),
                "detail": f"Workflow prepared ({active}): {scope_summary}",
            },
        })
        await manager.broadcast({"type": "event_appended", "event": event})

        # ── Run record (prepared; no canvas visualization) ──────────────────
        run = {
            "runId": wid, "status": "prepared", "startedAt": now, "endedAt": None,
            "backend": active, "workflowId": wid, "kind": "workflow", "simulated": False,
            "scopedBoxIds": box_ids, "scopedArrowIds": arrow_ids,
            "scopedFileIds": [], "scopedFunctionIds": [],
        }
        persistence.upsert_run(run)

        # ── Workflow artifact ───────────────────────────────────────────────
        artifact = persistence.save_workflow_artifact({
            "workflowId": wid, "backend": active, "status": "prepared",
            "scope": {
                "boxIds": box_ids, "arrowIds": arrow_ids,
                "files": file_paths,
                "functions": [fn.get("name") for fn in ctx.get("functions", [])],
            },
            "userPrompt": body.get("prompt", ""),
            "payload": payload, "script": script,
            "eventIds": [event["id"]],
            "ledgerIds": [arch_entry["id"], sr_entry["id"]],
            "createdAt": now, "updatedAt": now,
        })

        return web.json_response({
            "ok": True, "status": "prepared",
            "workflow": {
                "workflowId": wid, "backend": active, "status": "prepared",
                "script": script, "payload": payload,
                "scopeSummary": scope_summary, "protectedModules": protected,
                "editableModules": payload["permissions"]["editableModules"],
                "moduleCount": len(box_ids), "fileCount": len(file_paths),
                "functionCount": len(ctx.get("functions", [])),
                "verification": payload["verification"],
            },
            "run": run, "event": event,
            "architectEntry": arch_entry, "srDevEntry": sr_entry,
            "artifactSaved": bool(artifact),
        })

    async def post_agent_run(request: web.Request) -> web.Response:
        """Run the native Senior Dev agent over the selected scope (Step 22a).

        This is the one path that actually executes: it makes a real provider
        call (Senior Dev role, api/anthropic) and lets the model edit editable,
        in-scope files via scoped tools, then reconciles the produced result
        contract through the SAME gated path as a workflow result. Protected /
        out-of-scope writes are blocked. Triggered explicitly (Execute); never
        automatic.

        Args:
            request: web.Request — body: {selectedBoxIds, selectedArrowIds, prompt}

        Returns:
            web.Response — reconciliation payload + transcript/writes, or 400.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        # ── Senior Dev role gate (native in-process agent: anthropic or echo) ─
        sd = persistence.load_agent_settings().get("senior_dev", {})
        provider = sd.get("provider")
        if provider not in ("anthropic", "echo"):
            return web.json_response({"ok": False, "error":
                f"Native agent supports the 'anthropic' or 'echo' provider (Senior Dev is '{provider}')."},
                status=400)
        if provider == "anthropic":
            if not sd.get("model"):
                return web.json_response({"ok": False, "error": "Senior Dev has no model set."}, status=400)
            if not sd.get("apiKey"):
                return web.json_response({"ok": False, "error":
                    "Senior Dev has no API key. Add one in Agent Settings."}, status=400)

        # ── Baseline + scope (editable vs protected files by box type) ───────
        ensure_baseline(path)
        compiled = _compile_workflow_for(body)
        ctx = compiled["context"]
        sel_boxes = ctx.get("boxes", [])
        box_ids = [b["id"] for b in sel_boxes]
        editable = sorted({f for b in sel_boxes if b.get("type") == "dotted"
                           for f in (b.get("linkedFiles") or [])})
        protected = sorted({f for b in sel_boxes if b.get("type") != "dotted"
                            for f in (b.get("linkedFiles") or [])})
        if not editable:
            return web.json_response({"ok": False, "error":
                "No editable (dotted) in-scope files. Select a dotted box with linked files."}, status=400)

        scope_summary = f"{len(box_ids)} module(s), {len(editable)} editable file(s)"
        user_prompt = (body.get("prompt") or "").strip() or "Implement the selected architecture scope."
        wid = "arun_" + secrets.token_hex(5)
        now = datetime.now(timezone.utc).isoformat()

        ev = persistence.append_event({
            "type": "agent_run_started",
            "payload": {"runId": wid, "backend": "openfde-agent",
                        "detail": f"Native agent run started: {scope_summary}"},
        })
        await manager.broadcast({"type": "event_appended", "event": ev})

        # ── Pick transport: real provider call, or offline echo demo ─────────
        if provider == "echo":
            transport = make_echo_transport(path, editable)
            model = sd.get("model") or "echo-1"
        else:
            transport = make_transport(sd["apiKey"], sd.get("baseUrl", ""))
            model = sd["model"]

        # ── Execute the bounded agent loop off the event loop ────────────────
        system = build_system_prompt(scope_summary, editable, protected)
        loop = asyncio.get_event_loop()
        outcome = await loop.run_in_executor(None, lambda: run_agent(
            transport, model=model, system=system, user_prompt=user_prompt,
            root=path, editable_files=editable, protected_files=protected,
        ))

        # Normalize the produced contract defensively before reconciling.
        ok, error, result = validate_result(outcome["result"])
        if not ok:
            result = {"status": "failed", "reportSummary": f"Invalid agent result: {error}",
                      "filesChanged": [], "functionsChanged": [], "testsRun": [],
                      "verificationResult": "", "suggestedCanvasUpdates": [], "errors": [error]}
            _ok2, _e2, result = validate_result(result)

        # Lightweight artifact so reconcile_result can attach specs + approvals.
        protected_titles = sorted({b.get("title", b["id"]) for b in sel_boxes if b.get("type") != "dotted"})
        artifact = {
            "workflowId": wid, "backend": "openfde-agent", "status": "prepared",
            "kind": "agent_run",
            "scope": {"boxIds": box_ids, "arrowIds": [], "files": editable + protected,
                      "functions": [fn.get("name") for fn in ctx.get("functions", [])]},
            "payload": {"permissions": {"protectedModules": protected_titles,
                                        "protectedFiles": protected, "editableFiles": editable}},
            "userPrompt": user_prompt, "createdAt": now, "updatedAt": now,
            "eventIds": [ev["id"]],
        }
        persistence.save_workflow_artifact(artifact)
        persistence.upsert_run({
            "runId": wid, "status": "prepared", "backend": "openfde-agent",
            "kind": "agent_run", "simulated": False, "startedAt": now, "endedAt": None,
            "scopedBoxIds": box_ids, "scopedArrowIds": [],
            "scopedFileIds": [], "scopedFunctionIds": [],
        })

        payload = await reconcile_result(artifact, wid, result)
        payload.update({
            "runId": wid, "transcript": outcome["transcript"], "writes": outcome["writes"],
            "rejected": outcome["rejected"], "protectedAttempts": outcome["protectedAttempts"],
            "turns": outcome["turns"], "agentError": outcome["error"],
        })
        return web.json_response(payload)

    # ── Agent Council orchestration (Step 29 Slice 2) ─────────────────────── #

    def _text_role(cfg: dict):
        """Build a text-completion caller (system, user) -> str for a role config,
        or None to fall back to the deterministic role. Architect / Verifier only —
        Senior Dev uses the scoped agent_runner / Claude Code runner.

        Providers: 'claude-code-local' drives the local `claude` CLI as a pure
        text role (keyless — uses the user's login); 'anthropic' / OpenAI-compatible
        use the API (key + model required)."""
        prov = cfg.get("provider")
        # Every text-role call is an OpenFDE-internal machine prompt (ask, hatch
        # compose/explain, flow humanize) — local CLIs write transcripts, and
        # passive capture must never read those as human work episodes. The
        # summarizer's marker is the established skip signal; carry it here too.
        mark = lambda user: f"{episode_llm_summary.INTERNAL_MARKER}\n\n{user}"  # noqa: E731
        # Claude Code (local CLI) text role — no key, runs on the user's login.
        if prov == "claude-code-local":
            if not claude_cli_available():
                return None
            model = cfg.get("model") or "sonnet"
            return lambda system, user: run_claude_code_text(
                system=system, user=mark(user), model=model, cwd=path)
        # Codex (local CLI) text role — Day 3B. Drives `codex exec -s read-only`,
        # keyless (uses the local Codex login); never mutates the repo.
        if prov == "codex-local":
            if not codex_cli_available():
                return None
            model = cfg.get("model") or None
            return lambda system, user: run_codex_local_text(
                system=system, user=mark(user), model=model, cwd=path)
        # API providers (Anthropic / OpenAI-compatible) — require a key + model.
        if not cfg.get("apiKey") or not cfg.get("model"):
            return None
        key, model, base = (cfg["apiKey"], cfg["model"], cfg.get("baseUrl", ""))
        if prov == "anthropic":
            tr = make_transport(key, base)
        elif prov in ("openai-compatible", "openrouter", "ollama"):
            tr = make_openai_transport(key, base)
        else:
            return None
        return lambda system, user: llm_complete(tr, model=model, system=system, user=user)

    def _resolve_sr_dev_backend(sd: dict):
        """Pick the Senior Dev implementation backend from its role config (Step 31).

        Returns (backend_id, None) on success or (None, error_message). backend_id
        is one of: 'claude_code' | 'echo' | 'anthropic'. Claude Code (local CLI) is
        selected by provider 'claude-code-local' (it drives the local `claude`
        CLI and uses the user's existing login — no key/model required here).
        """
        provider = sd.get("provider")
        if provider == "claude-code-local":
            if not claude_cli_available():
                return None, ("Claude Code CLI not found on PATH. Install Claude Code "
                              "or pick another Senior Dev provider (Anthropic or Echo).")
            return "claude_code", None
        if provider == "codex-local":
            return None, ("Codex Local is a text-only role (Architect/Verifier). Senior Dev "
                          "needs Claude Code, Anthropic, or Echo.")
        if provider == "echo":
            return "echo", None
        if provider == "anthropic":
            if not (sd.get("model") and sd.get("apiKey")):
                return None, "Senior Dev (Anthropic) needs a model + API key."
            return "anthropic", None
        return None, (f"Senior Dev must be 'anthropic', 'echo', or 'claude-code-local' "
                      f"(is '{provider}').")

    def _parse_verdict(text: str, result: dict) -> dict:
        """Best-effort parse of a Verifier JSON verdict; safe fallback otherwise."""
        v = None
        if text:
            try:
                start, end = text.find("{"), text.rfind("}")
                if start >= 0 and end > start:
                    v = json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                v = None
        if not isinstance(v, dict):
            low = (text or "").lower()
            status = "passed" if ("pass" in low and "fail" not in low) else "failed"
            v = {"status": status, "summary": (text or "")[:200]}
        if v.get("status") not in ("passed", "failed", "needs_human"):
            v["status"] = "failed"
        v.setdefault("summary", "")
        v.setdefault("fixPrompt", "")
        return v

    _ARCH_SYS = ("You are the Architect. Produce a short, concrete implementation brief for the "
                 "Senior Dev: what to change, the expected outcome, and how to verify it. Stay within "
                 "the editable files. The Senior Dev only EDITS files — it must not run git "
                 "add/commit/push; OpenFDE reviews the changes and lands the commit. No code fences, "
                 "no preamble — just the brief.")
    _VER_SYS = ("You are the Verifier. Review the Senior Dev's ACTUAL DIFF against the brief — judge the "
                "code that was written, not the self-report. Respond with "
                'JSON ONLY: {"status":"passed|failed|needs_human","summary":"...","fixPrompt":"...",'
                '"testsSuggested":[],"risks":[]}. Fail if the diff does not satisfy the intent, is empty, '
                "or only adds a placeholder/comment without real behavior.")

    async def post_council_run(request: web.Request) -> web.Response:
        """Run one bounded Agent Council loop over the selected scope (Step 29).

        Architect (OpenAI/Codex or Anthropic, else deterministic) writes a brief →
        Senior Dev (scoped agent_runner) implements → Verifier reviews → at most one
        reprompt → the final result lands through the SAME gated reconciliation.
        Scope/permission enforcement stays entirely inside the agent runner; the
        council never bypasses dotted/solid. No new UI — it writes ledger + timeline.

        Args:
            request: web.Request — body: {selectedBoxIds, selectedArrowIds, prompt}

        Returns:
            web.Response — {ok, runId, status, stages, commit?, approval?} or 400.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        settings = persistence.load_agent_settings()
        sd = settings.get("senior_dev", {})
        sd_backend, sd_err = _resolve_sr_dev_backend(sd)
        if sd_err:
            return web.json_response({"ok": False, "error": sd_err}, status=400)
        # Human label per role's backend — surfaced on every stage so Work Review
        # shows which agent did each step (e.g. all three "Claude Code").
        _PROV_LABEL = {"claude-code-local": "Claude Code (local CLI)",
                       "codex-local": "Codex (local CLI)",
                       "anthropic": "Anthropic (API)", "echo": "Echo",
                       "openai-compatible": "OpenAI (API)", "openrouter": "OpenRouter",
                       "ollama": "Ollama"}
        sd_label = {"claude_code": "Claude Code (local CLI)", "anthropic": "Anthropic API",
                    "echo": "Echo"}.get(sd_backend, sd_backend)

        def _role_label(role):
            if role == "sr_dev":
                return sd_label
            prov = settings.get(role, {}).get("provider")
            return _PROV_LABEL.get(prov, prov)

        def _stages_out(stages):
            rows = []
            for s in stages:
                row = {"role": s["role"], "status": s["status"], "summary": s["summary"],
                       "attempt": s.get("attempt", 0)}
                lbl = _role_label(s["role"])
                if lbl:
                    row["provider"] = lbl
                rows.append(row)
            return rows

        ensure_baseline(path)
        compiled = _compile_workflow_for(body)
        ctx = compiled["context"]
        sel_boxes = ctx.get("boxes", [])
        box_ids = [b["id"] for b in sel_boxes]
        editable = sorted({f for b in sel_boxes if b.get("type") == "dotted"
                           for f in (b.get("linkedFiles") or [])})
        protected = sorted({f for b in sel_boxes if b.get("type") != "dotted"
                            for f in (b.get("linkedFiles") or [])})

        # ── Sketch-First Intent scope resolution ─────────────────────────────
        # Intent-only sketches (no editable linked files) run in a SAFE generated
        # workspace (openfde_work/) instead of being rejected; existing selected
        # solid files stay protected and the permission boundary is NOT widened for
        # normal architecture work. A pure architecture selection with nothing to
        # edit (no intent graph) still returns the 400.
        intent_graph = ctx.get("intentGraph") or {}
        scope = resolve_run_scope(editable, protected, intent_graph)
        if scope is None:
            return web.json_response({"ok": False, "error":
                "No editable (dotted) in-scope files. Select a dotted box with linked files, "
                "or draw intent steps to build something new."}, status=400)
        editable, protected, generated_scope = scope

        scope_summary = (f"intent workspace ({GENERATED_WORKSPACE}) · {len(box_ids)} step(s)"
                         if generated_scope
                         else f"{len(box_ids)} module(s), {len(editable)} editable file(s)")
        user_request = (body.get("prompt") or "").strip()

        # The episode/ledger lead with the sketch summary (Story continuity) and
        # the Architect receives the ordered Intent Graph Brief (Part C).
        intent_brief = render_intent_brief(intent_graph)
        intent_source = None
        if intent_graph.get("present"):
            summary = intent_graph.get("summary") or "intent graph"
            user_prompt = f"Intent: {summary}" + (f" — {user_request}" if user_request else "")
            steps = intent_graph.get("steps") or []
            intent_source = {"kind": "intent-graph", "ref": summary,
                             "stepCount": len(steps),
                             "steps": [{"boxId": s.get("boxId"), "title": s.get("title")}
                                       for s in steps]}
        else:
            user_prompt = user_request or "Implement the selected architecture scope."

        wid = "council_" + secrets.token_hex(5)
        now = datetime.now(timezone.utc).isoformat()

        started = persistence.append_event({
            "type": "council_started",
            "payload": {"runId": wid, "detail": f"Agent Council started: {scope_summary}"},
        })
        await manager.broadcast({"type": "event_appended", "event": started})

        # ── Run starts an Episode (Sketch-First loop) ────────────────────────
        # For an intent-graph run, open the durable episode NOW — carrying its intentSource
        # and the selected box ids — so the loop's record (Story / OpenPM linkback) exists
        # whether the run lands, awaits review, OR fails. reconcile_result reuses it by runId
        # and fills in files/commit on success; on failure it simply stays open.
        intent_episode_id = None
        if intent_source:
            ep0 = _link_episode_for_run(wid, user_prompt, "council", [], [started["id"]], [],
                                        "", "open", intent_source=intent_source)
            ep0["boxIds"] = list(box_ids)
            persistence.upsert_episode(ep0)
            intent_episode_id = ep0["episodeId"]
            await manager.broadcast({"type": "episode_updated", "episode": ep0})
            # Server-durable OpenPM cards (source of truth): one per selected intent step, opened
            # as `doing`. Settled to done | testing by the run's outcome below.
            steps0 = [{"boxId": s.get("boxId"), "title": s.get("title")}
                      for s in (intent_source.get("steps") or []) if s.get("boxId")]
            tasks0, ch0 = sync_intent_tasks(persistence.load_tasks(), episode_id=intent_episode_id,
                                            run_id=wid, tag=intent_source.get("ref") or "", steps=steps0)
            if ch0:
                persistence.save_tasks(tasks0)
                await manager.broadcast({"type": "tasks_updated"})

        # ── Live activity stream (adaptive glow) ─────────────────────────────
        # Announce the planned files now (canvas pre-pulses them + drills in),
        # then stream each write the moment it lands so the glow follows the agent.
        loop = asyncio.get_event_loop()
        await manager.broadcast({"type": "agent_plan",
                                 "payload": {"runId": wid, "files": editable}})

        def emit_progress(rel, action="write"):
            # Called from the executor thread (run_agent) — hop back to the loop.
            # action: "read" (agent is looking at the file) | "write" (just edited).
            try:
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({"type": "agent_progress",
                                       "payload": {"runId": wid, "file": rel, "action": action}}), loop)
            except Exception:  # noqa: BLE001 — progress must never break the run
                pass

        # Cancellation handle for this run — the Stop button hits /cancel below.
        cancel_token = CancelToken()
        _RUN_CONTROLS[wid] = cancel_token

        # ── Build the three roles ────────────────────────────────────────────
        arch_caller = _text_role(settings.get("architect", {}))
        ver_caller = _text_role(settings.get("verifier", {}))

        def architect(c):
            if cancel_token.is_set():
                return ""
            sketch = (c.get("intentBrief") or "").strip()
            if arch_caller:
                user = (f"Intent: {c['prompt']}\nScope: {c['scopeSummary']}\n"
                        f"Editable files: {c['editable']}\nProtected (approval needed): {c['protected']}")
                if sketch:
                    user += ("\n\nThe user drew this sketch — translate each intent step into "
                             "concrete edits within the editable scope:\n" + sketch)
                return arch_caller(_ARCH_SYS, user)
            base = (f"Implement: {c['prompt']} within {c['scopeSummary']}. "
                    f"Edit only: {', '.join(c['editable'])}. Keep changes minimal and verify the intent.")
            return f"{base}\n\n{sketch}" if sketch else base

        def verifier(brief, result):
            if cancel_token.is_set():
                return {"status": "failed", "summary": "Run cancelled by user.",
                        "fixPrompt": "", "testsSuggested": [], "risks": []}
            # The Senior Dev's writes are still uncommitted in the work tree here
            # (reconcile commits AFTER the council returns), so this is the REAL diff.
            changed = [f["path"] for f in result.get("filesChanged", [])]
            diff = worktree_diff(path, changed or editable)
            patch = (diff.get("patch") or "").strip()
            if ver_caller:
                user = (f"Brief:\n{brief}\n\nResult status: {result.get('status')}\n"
                        f"Files changed: {changed}\n"
                        f"Report: {result.get('reportSummary', '')}\n"
                        f"Verification: {result.get('verificationResult', '')}\n\n"
                        f"Actual diff (review THIS directly):\n{patch or '(no diff captured)'}")
                return _parse_verdict(ver_caller(_VER_SYS, user), result)
            # Deterministic fallback: require an accepted status AND a real diff.
            if result.get("status") == "passed" and patch:
                where = ", ".join(changed) or "scope"
                return {"status": "passed",
                        "summary": f"Diff applied to {where} ({len(patch)} chars); accepted.",
                        "fixPrompt": "", "testsSuggested": [], "risks": []}
            return {"status": "failed",
                    "summary": "No real in-scope diff to accept." if not patch
                               else "Change did not pass review.",
                    "fixPrompt": "Make a concrete in-scope edit that satisfies the intent.",
                    "testsSuggested": [], "risks": []}

        if sd_backend == "claude_code":
            # OpenFDE drives the local `claude` CLI as Senior Dev — no copy/paste.
            # Scope is enforced post-hoc inside the runner (out-of-scope reverted,
            # protected → needs_approval), so the Verifier still judges the real diff.
            def senior_dev(brief):
                ed = ", ".join(editable) or "(none)"
                pr = ", ".join(protected) or "(none)"
                scope_line = (f"- This is a NEW build: create files ONLY under {ed} "
                              "(a fresh workspace; new file paths beneath it are allowed). "
                              "Implement the intent graph as a small working version; add tests if feasible."
                              if generated_scope else f"- Edit ONLY these files: {ed}")
                cc_prompt = (f"{brief}\n\nConstraints:\n"
                             f"{scope_line}\n"
                             f"- Do NOT modify protected/existing files: {pr}\n"
                             f"- Keep the change minimal and correct. Do not run tests or git.")
                return run_claude_code(repo_root=path, prompt=cc_prompt,
                                       editable=editable, protected=protected,
                                       model=(sd.get("model") or "sonnet"),
                                       should_cancel=cancel_token.is_set,
                                       on_proc=cancel_token.set_proc,
                                       on_write=lambda r: emit_progress(r, "write"),
                                       on_read=lambda r: emit_progress(r, "read"))
        else:
            if sd_backend == "echo":
                # Pass the selected intent step titles so the offline echo can recognize a
                # SaaS-shaped sketch (e.g. the support inbox) and scaffold credible per-step
                # files instead of a single generic marker — the demo payoff.
                sd_transport = make_echo_transport(path, editable, steps=intent_graph.get("steps"))
                sd_model = sd.get("model") or "echo-1"
            else:  # anthropic
                sd_transport, sd_model = make_transport(sd["apiKey"], sd.get("baseUrl", "")), sd["model"]
            sd_system = build_system_prompt(scope_summary, editable, protected)

            def senior_dev(brief):
                return run_agent(sd_transport, model=sd_model, system=sd_system, user_prompt=brief,
                                 root=path, editable_files=editable, protected_files=protected,
                                 on_write=lambda r: emit_progress(r, "write"),
                                 on_read=lambda r: emit_progress(r, "read"),
                                 should_cancel=cancel_token.is_set)

        # ── Run the bounded loop off the event loop ──────────────────────────
        outcome = await loop.run_in_executor(None, lambda: run_council(
            architect=architect, senior_dev=senior_dev, verifier=verifier,
            context={"prompt": user_prompt, "scopeSummary": scope_summary,
                     "editable": editable, "protected": protected,
                     "intentBrief": intent_brief},
            max_reprompts=1,
        ))

        # ── Cancelled? Land nothing — no reconcile, no commit. ───────────────
        was_cancelled = cancel_token.is_set()
        _RUN_CONTROLS.pop(wid, None)
        if was_cancelled:
            persistence.upsert_run({
                "runId": wid, "status": "cancelled", "backend": "openfde-council",
                "kind": "council_run", "simulated": False, "startedAt": now,
                "endedAt": datetime.now(timezone.utc).isoformat(),
                "scopedBoxIds": box_ids, "scopedArrowIds": [],
                "scopedFileIds": [], "scopedFunctionIds": [],
            })
            cev = persistence.append_event({
                "type": "council_cancelled",
                "payload": {"runId": wid, "detail": "Agent Council cancelled by user."},
            })
            await manager.broadcast({"type": "event_appended", "event": cev})
            return web.json_response({
                "ok": True, "runId": wid, "status": "cancelled",
                "stages": _stages_out(outcome["stages"]),
                "commit": None, "approval": None, "verifier": None,
            })

        # ── Record each stage: project.md ledger + timeline event ────────────
        stage_event_ids = []
        for st in outcome["stages"]:
            suffix = f" — attempt {st['attempt']}" if st.get("attempt", 0) > 1 else ""
            persistence.append_project_log_entry({
                "role": st["role"],
                "title": f"Council · {st['role']} ({st['status']}){suffix}",
                "summary": st["summary"],
                "body": st.get("detail", ""),
                "boxIds": box_ids,
                "metadata": {"runId": wid, "kind": "council_stage",
                             "stageStatus": st["status"], "attempt": st.get("attempt", 0)},
            }, path)
            sev = persistence.append_event({
                "type": "council_stage",
                "payload": {"runId": wid, "role": st["role"], "status": st["status"],
                            "attempt": st.get("attempt", 0), "detail": st["summary"][:160]},
            })
            await manager.broadcast({"type": "event_appended", "event": sev})
            stage_event_ids.append(sev["id"])

        # ── Land the final result through existing gated reconciliation ──────
        _ok, _err, final = validate_result(outcome["finalResult"])
        if not _ok:
            _ok2, _e2, final = validate_result({"status": "failed",
                "reportSummary": f"Invalid council result: {_err}", "filesChanged": []})

        protected_titles = sorted({b.get("title", b["id"]) for b in sel_boxes if b.get("type") != "dotted"})
        artifact = {
            "workflowId": wid, "backend": "openfde-council", "status": "prepared", "kind": "council_run",
            "scope": {"boxIds": box_ids, "arrowIds": [], "files": editable + protected,
                      "functions": [fn.get("name") for fn in ctx.get("functions", [])]},
            "payload": {"permissions": {"protectedModules": protected_titles,
                                        "protectedFiles": protected, "editableFiles": editable}},
            "userPrompt": user_prompt, "createdAt": now, "updatedAt": now,
            "eventIds": [started["id"]] + stage_event_ids,
        }
        if intent_source:
            artifact["intentSource"] = intent_source
        persistence.save_workflow_artifact(artifact)
        persistence.upsert_run({
            "runId": wid, "status": "prepared", "backend": "openfde-council", "kind": "council_run",
            "simulated": False, "startedAt": now, "endedAt": None,
            "scopedBoxIds": box_ids, "scopedArrowIds": [], "scopedFileIds": [], "scopedFunctionIds": [],
        })

        payload = await reconcile_result(artifact, wid, final)

        # ── Link implementation back to the intent steps (Part D) ────────────
        # The whole sketch shares the run's files (heuristic, labelled — see
        # attribute_intent_files). The client writes these onto the intent boxes.
        changed_files = payload.get("sourceFilesChanged") or []
        intent_boxes = [b for b in sel_boxes if is_intent_box(b)]
        named_text = " ".join(st.get("summary", "") for st in outcome["stages"])
        intent_links = attribute_intent_files(intent_boxes, changed_files, named_text=named_text)

        # Gap 1: persist per-step file links onto the episode's intentSource (files are only
        # known now, after the run). Story then shows which files each sketch step produced;
        # episode-level files are left untouched. Preserves kind/ref/stepCount.
        episode_id = intent_episode_id or payload.get("episodeId")
        if episode_id and intent_source and intent_links:
            ep = persistence.get_episode(episode_id)
            src = ep.get("intentSource") if ep else None
            if isinstance(src, dict) and src.get("kind") == "intent-graph":
                src["steps"] = merge_step_files(src.get("steps"), intent_links)
                ep["intentSource"] = src
                persistence.upsert_episode(ep)
                await manager.broadcast({"type": "episode_updated", "episode": ep})

        # ── Close the loop, server-durable (Fixes 2/3/4) ─────────────────────
        if intent_source and intent_episode_id:
            rstatus = outcome["status"]
            landed_ok = rstatus in ("passed", "needs_approval")        # built (committed or pending)
            failed = rstatus in ("failed", "needs_human")              # blocked
            committed = bool(payload.get("committed"))
            # (2) settle the OpenPM cards: done | testing, carrying the commit + per-step files.
            steps_out = [{"boxId": b.get("id"), "title": b.get("title"),
                          "files": (intent_links.get(b["id"]) or {}).get("files") or []}
                         for b in intent_boxes if b.get("id")]
            tasks1, ch1 = sync_intent_tasks(
                persistence.load_tasks(), episode_id=intent_episode_id, run_id=wid,
                tag=intent_source.get("ref") or "", steps=steps_out, committed=committed,
                awaiting_review=bool(payload.get("awaitingReview")), failed=failed,
                commit_sha=payload.get("commitSha"))
            if ch1:
                persistence.save_tasks(tasks1)
                await manager.broadcast({"type": "tasks_updated"})
            # (3) ground the canvas server-side (source of truth): built (with files) | blocked,
            # persisted to state.json so a reload still shows ✓ BUILT + the file links.
            st_state = persistence.load_state()
            sboxes = st_state.get("boxes", [])
            g_changed = False
            for b in sboxes:
                if b.get("kind") != "intent" or b.get("id") not in box_ids:
                    continue
                link = intent_links.get(b["id"])
                if landed_ok and link:
                    # Intent → architecture in place: a step with a clear single generated file
                    # BECOMES a module box (originIntent remembers the sketch). Steps without clear
                    # per-step attribution stay built intent boxes — the honest, unchanged path.
                    if architecturize_intent_box(b, link, episode_id, wid) is None:
                        b["runState"] = "built"
                        b["implementationFiles"] = link.get("files") or []
                        b["implementationMeta"] = {"runId": wid, "attribution": link.get("attribution"),
                                                   "confidence": link.get("confidence")}
                    g_changed = True
                elif failed:
                    b["runState"] = "blocked"
                    g_changed = True
            if g_changed:
                persistence.save_state({"boxes": sboxes, "arrows": st_state.get("arrows", [])})
                await manager.broadcast({"type": "state_updated", "payload": {"reason": "intent_run"}})
            # (4) rebuild the Story graph NOW so /api/story/prompt-graph includes this episode
            # immediately — no process restart / manual rebuild. Coalesced + off-loop in the helper.
            await _refresh_story_cache_and_broadcast()

        return web.json_response({
            "ok": True, "runId": wid, "status": outcome["status"],
            "stages": _stages_out(outcome["stages"]),
            "commit": ({"sha": payload.get("commitSha"), "committed": True}
                       if payload.get("committed") else None),
            # Review Then Land: the agent's edits are in the work tree, owned by a
            # prompt episode, waiting for the user to Land them (no auto-commit).
            "episodeId": payload.get("episodeId"),
            "awaitingReview": payload.get("awaitingReview", False),
            "approval": payload.get("approval"),
            "verifier": outcome.get("verifier"),
            "filesChanged": changed_files,
            "intentLinks": intent_links,
            # Sketch-First v2: tell the UI this ran in the generated workspace (a
            # correct path for intent-only sketches, not an error/fallback).
            "generatedScope": generated_scope,
            "workspace": GENERATED_WORKSPACE if generated_scope else None,
        })

    async def post_council_cancel(request: web.Request) -> web.Response:
        """Cancel an in-flight council run (Step 33). Sets the run's cancel flag
        (polled by the in-process agent runner between turns) and terminates the
        Claude Code subprocess if one is live. The run itself short-circuits and
        returns `status: cancelled` without committing.

        Args:
            request: web.Request — match_info 'runId'.

        Returns:
            web.Response — {ok, runId, cancelled} or 404 if the run isn't in flight.
        """
        run_id = request.match_info.get("runId", "")
        token = _RUN_CONTROLS.get(run_id)
        if token is None:
            return web.json_response(
                {"ok": False, "error": "Run is not in flight (already finished or unknown)."},
                status=404)
        token.cancel()
        logger.info("Council run cancelled by user: %s", run_id)
        return web.json_response({"ok": True, "runId": run_id, "cancelled": True})

    async def get_workflows(request: web.Request) -> web.Response:
        """List workflow artifacts (newest-first).

        Args:
            request: web.Request

        Returns:
            web.Response — JSON array of artifacts.
        """
        return web.json_response(persistence.list_workflow_artifacts())

    async def get_workflow_one(request: web.Request) -> web.Response:
        """Return a single workflow artifact by id.

        Args:
            request: web.Request — match_info 'workflowId'

        Returns:
            web.Response — JSON artifact or 404.
        """
        wid = request.match_info.get("workflowId", "")
        wf = persistence.load_workflow_artifact(wid)
        if wf is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response(wf)

    def _episode_kind_for(artifact: dict) -> str:
        """Map a run artifact to a prompt-episode kind (council|workflow|agent)."""
        b = (artifact.get("backend") or "").lower()
        k = (artifact.get("kind") or "").lower()
        if "council" in b or "council" in k:
            return "council"
        if "workflow" in b or "workflow" in k:
            return "workflow"
        return "agent"

    def _link_episode_for_run(wid: str, prompt: str, kind: str, files: list,
                              event_ids: list, ledger_ids: list, summary: str,
                              status: str, intent_source: dict = None) -> dict:
        """Create or update the prompt episode that owns this run's changes.

        Episodes are the durable "prompt turn" — the user's intent plus the runs,
        events, and (after Land) commits it produced. Re-uses an episode already
        linked to ``wid``; otherwise mints a new one. Never downgrades a 'landed'
        episode. ``intent_source`` records where the prompt came from (e.g. a
        Sketch-First intent graph) so Story shows the episode's origin. Returns the
        stored episode (caller broadcasts).
        """
        now = datetime.now(timezone.utc).isoformat()
        ep = persistence.get_open_episode_for_run(wid)
        if ep is None:
            ep = {
                "episodeId": "episode_" + secrets.token_hex(6),
                "createdAt": now, "updatedAt": now,
                "prompt": prompt or "", "kind": kind, "status": status,
                "runIds": [wid], "eventIds": list(event_ids or []),
                "projectEntryIds": list(ledger_ids or []), "commitShas": [],
                "files": sorted(set(files or [])), "summary": summary or "",
            }
            if intent_source:
                ep["intentSource"] = intent_source
        else:
            ep["updatedAt"] = now
            if ep.get("status") != "landed":
                ep["status"] = status
            ep["files"] = sorted(set((ep.get("files") or []) + list(files or [])))
            ep["eventIds"] = list(dict.fromkeys((ep.get("eventIds") or []) + list(event_ids or [])))
            ep["projectEntryIds"] = list(dict.fromkeys((ep.get("projectEntryIds") or []) + list(ledger_ids or [])))
            if wid not in (ep.get("runIds") or []):
                ep.setdefault("runIds", []).append(wid)
            if not ep.get("prompt") and prompt:
                ep["prompt"] = prompt
            if summary:
                ep["summary"] = summary
            if intent_source and not ep.get("intentSource"):
                ep["intentSource"] = intent_source
        persistence.upsert_episode(ep)
        return ep

    async def reconcile_result(artifact: dict, wid: str, result: dict) -> dict:
        """Reconcile a validated result contract into OpenFDE state.

        Shared by the manual workflow-result endpoint (Step 20) and the native
        agent runner (Step 22). Appends timeline + project.md entries, updates the
        run + box specs, commits only when a real source file changed (Step 20
        gating), and opens a protected-scope approval gate on 'needs_approval'.

        Args:
            artifact: dict — the workflow/run artifact (mutated + re-saved).
            wid: str — workflow/run id.
            result: dict — a validated, normalized output contract.

        Returns:
            dict — the response payload (ok/status/committed/commitSha/…).
        """
        status = result["status"]
        now = datetime.now(timezone.utc).isoformat()
        box_ids = artifact.get("scope", {}).get("boxIds", [])
        report = result["reportSummary"]
        tsumm = tests_summary(result["testsRun"])

        # ── Ledger: sr_dev (impl) + verifier (tests) + architect (approval) ──
        files_line = ", ".join(f["path"] for f in result["filesChanged"][:8]) or "none"
        sr_entry = persistence.append_project_log_entry({
            "role": "sr_dev",
            "title": f"Implementation result — {status}",
            "summary": report[:200] or f"Workflow {status}.",
            "body": f"{report}\n\nFiles changed: {files_line}",
            "boxIds": box_ids,
            "filePaths": [f["path"] for f in result["filesChanged"]],
            "metadata": {"workflowId": wid, "status": status, "kind": "workflow_result"},
        }, path)
        ver_entry = persistence.append_project_log_entry({
            "role": "verifier",
            "title": f"Verification: {result['verificationResult'] or 'n/a'}",
            "summary": tsumm,
            "body": "\n".join(f"- `{t['command']}` → {t['result'] or '?'}" for t in result["testsRun"]) or "No tests reported.",
            "metadata": {"workflowId": wid, "verificationResult": result["verificationResult"]},
        }, path)
        ledger_ids = [sr_entry["id"], ver_entry["id"]]

        arch_entry = None
        if status == "needs_approval":
            arch_entry = persistence.append_project_log_entry({
                "role": "architect",
                "title": "Approval required — protected scope",
                "summary": "Workflow needs approval before protected files are modified.",
                "body": report or "Protected scope change requires approval.",
                "boxIds": box_ids,
                "metadata": {"workflowId": wid, "kind": "approval_request"},
            }, path)
            ledger_ids.append(arch_entry["id"])

        # ── Timeline: received + outcome ─────────────────────────────────────
        recv = persistence.append_event({
            "type": "workflow_result_received",
            "payload": {"workflowId": wid, "status": status,
                        "fileCount": len(result["filesChanged"]),
                        "detail": f"Workflow result received ({status}): {tsumm}"},
        })
        await manager.broadcast({"type": "event_appended", "event": recv})
        outcome_type = {"passed": "workflow_passed", "failed": "workflow_failed",
                        "needs_approval": "workflow_needs_approval"}[status]
        outcome = persistence.append_event({
            "type": outcome_type,
            "payload": {"workflowId": wid, "detail": report[:160] or f"Workflow {status}."},
        })
        await manager.broadcast({"type": "event_appended", "event": outcome})
        event_ids = [recv["id"], outcome["id"]]

        # ── Auto-Land on completion (Land · Watch · Review) ──────────────────
        # File saves never commit. On a clean verifier pass with NO approval gate,
        # OpenFDE auto-lands the run's files — a SCOPED commit under a durable prompt
        # EPISODE (only the run's files; unrelated dirty files are never swept in). An
        # approval gate leaves the edits as 'reviewing' for an explicit manual Land.
        committed, commit_sha, commit_reason = False, None, None
        reported_sources = source_files(result["filesChanged"])
        actually_changed = changed_paths(path, reported_sources)
        episode = None
        if status == "passed" and actually_changed:
            episode = _link_episode_for_run(
                wid, artifact.get("userPrompt"), _episode_kind_for(artifact),
                actually_changed, event_ids, ledger_ids, report[:200], "reviewing",
                intent_source=artifact.get("intentSource"))
            from openfde import autoland
            land = await asyncio.get_event_loop().run_in_executor(
                None, lambda: autoland.land_episode(path, persistence, episode, auto=True, allow_llm=True))
            episode = land.get("episode", episode)
            committed = bool(land.get("committed"))
            commit_sha = land.get("sha")
            commit_reason = "auto-landed" if committed else (land.get("reason") or "held for manual land")
            for m in land.get("broadcasts", []):
                await manager.broadcast(m)
                if m.get("type") == "event_appended" and (m.get("event") or {}).get("id"):
                    event_ids.append(m["event"]["id"])
        elif status == "needs_approval" and actually_changed:
            episode = _link_episode_for_run(
                wid, artifact.get("userPrompt"), _episode_kind_for(artifact),
                actually_changed, event_ids, ledger_ids, report[:200], "reviewing",
                intent_source=artifact.get("intentSource"))
            commit_reason = "approval required — review and Land manually"
            rp = persistence.append_event({
                "type": "review_pending",
                "payload": {"runId": wid, "episodeId": episode["episodeId"],
                            "fileCount": len(actually_changed),
                            "detail": f"{len(actually_changed)} file(s) need approval, then Land."},
            })
            await manager.broadcast({"type": "event_appended", "event": rp})
            await manager.broadcast({"type": "episode_updated", "episode": episode})
            event_ids.append(rp["id"])
        elif status in ("passed", "needs_approval"):
            episode = _link_episode_for_run(
                wid, artifact.get("userPrompt"), _episode_kind_for(artifact),
                [], event_ids, ledger_ids, report[:200], "open",
                intent_source=artifact.get("intentSource"))
            commit_reason = "no reported source files changed on disk"

        # ── Run record: prepared → outcome ───────────────────────────────────
        run = persistence.get_run(wid) or {"runId": wid, "scopedBoxIds": box_ids, "scopedArrowIds": []}
        run["status"] = status
        run["endedAt"] = now
        run["resultSummary"] = report[:200]
        run["commitSha"] = commit_sha
        persistence.upsert_run(run)

        # ── Box spec story update ────────────────────────────────────────────
        boxes_by_id = {b["id"]: b for b in persistence.load_state().get("boxes", [])}
        specs = apply_workflow_result(
            persistence.load_box_specs(), boxes_by_id, box_ids,
            workflow_id=wid, run_id=wid, ledger_ids=ledger_ids, event_ids=event_ids,
            report_summary=report, tests_run=result["testsRun"],
            verification_result=result["verificationResult"],
            files_changed=result["filesChanged"], functions_changed=result["functionsChanged"],
            suggested_canvas_updates=result["suggestedCanvasUpdates"],
        )
        persistence.save_box_specs(specs)

        # ── Approval gate (needs_approval) ───────────────────────────────────
        approval = None
        if status == "needs_approval":
            perm = artifact.get("payload", {}).get("permissions", {})
            approval = {
                "approvalId": "apr_" + secrets.token_hex(5),
                "workflowId": wid, "runId": wid, "status": "pending",
                "protectedModules": perm.get("protectedModules", []),
                "protectedFiles": perm.get("protectedFiles", []),
                "functions": [fn["name"] for fn in result["functionsChanged"]][:25],
                "requestedChange": report[:500],
                "suggestedCanvasUpdates": result["suggestedCanvasUpdates"],
                "ledgerIds": ledger_ids, "eventIds": event_ids,
                "createdAt": now, "resolvedAt": None,
            }
            persistence.upsert_approval(approval)

        # ── Re-save artifact with the result + cross-refs ────────────────────
        artifact["result"] = result
        artifact["status"] = status
        artifact["resultReceivedAt"] = now
        artifact["updatedAt"] = now
        artifact["ledgerIds"] = (artifact.get("ledgerIds", []) + ledger_ids)
        artifact["eventIds"] = (artifact.get("eventIds", []) + event_ids)
        if approval:
            artifact["approvalId"] = approval["approvalId"]
        if commit_sha:
            artifact["commitSha"] = commit_sha
        persistence.save_workflow_artifact(artifact)

        return {
            "ok": True, "status": status,
            "committed": committed, "commitSha": commit_sha, "commitReason": commit_reason,
            "sourceFilesChanged": actually_changed,
            "episodeId": episode["episodeId"] if episode else None,
            "awaitingReview": bool(actually_changed) and not committed,   # auto-landed → nothing to review
            "reportSummary": report, "verificationResult": result["verificationResult"],
            "testsSummary": tsumm, "fileCount": len(result["filesChanged"]),
            "approval": approval,
            "events": [recv, outcome],
            "ledgerEntries": [e for e in (sr_entry, ver_entry, arch_entry) if e],
        }

    async def post_workflow_result(request: web.Request) -> web.Response:
        """Ingest a Claude Code workflow result and reconcile it into OpenFDE.

        Validates the Step-19 output contract then delegates to reconcile_result.
        OpenFDE never runs Claude Code in this path — the result is reported in.

        Args:
            request: web.Request — match_info 'workflowId'; body: output contract.

        Returns:
            web.Response — reconciliation payload, or 400/404.
        """
        wid = request.match_info.get("workflowId", "")
        artifact = persistence.load_workflow_artifact(wid)
        if artifact is None:
            return web.json_response({"ok": False, "error": "unknown workflow"}, status=404)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        ok, error, result = validate_result(body)
        if not ok:
            return web.json_response({"ok": False, "error": error}, status=400)
        payload = await reconcile_result(artifact, wid, result)
        return web.json_response(payload)

    # ================================================================== #
    #  REST — /api/approvals  (protected-scope gate, Step 20)           #
    # ================================================================== #

    async def get_approvals(request: web.Request) -> web.Response:
        """Return approval requests (newest-first).

        Args:
            request: web.Request

        Returns:
            web.Response — JSON array of approvals.
        """
        return web.json_response(persistence.load_approvals())

    async def _resolve_approval(request: web.Request, decision: str) -> web.Response:
        approval_id = request.match_info.get("approvalId", "")
        approval = persistence.get_approval(approval_id)
        if approval is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        if approval.get("status") != "pending":
            return web.json_response({"ok": False, "error": "already resolved", "approval": approval}, status=409)

        now = datetime.now(timezone.utc).isoformat()
        approval["status"] = decision           # "approved" | "rejected"
        approval["resolvedAt"] = now
        persistence.upsert_approval(approval)

        wid = approval.get("workflowId", "")
        mods = ", ".join(approval.get("protectedModules", [])) or "protected scope"
        # Ledger + timeline
        entry = persistence.append_project_log_entry({
            "role": "architect",
            "title": f"Approval {decision} — {mods}",
            "summary": (f"Protected scope {decision} for workflow {wid}."),
            "body": (f"Decision: {decision}. Requested change: {approval.get('requestedChange', '')}\n\n"
                     + ("Re-run the workflow to apply the approved changes."
                        if decision == "approved" else "Workflow will not be applied.")),
            "metadata": {"workflowId": wid, "approvalId": approval_id, "decision": decision},
        }, path)
        event = persistence.append_event({
            "type": "approval_resolved",
            "payload": {"approvalId": approval_id, "workflowId": wid, "decision": decision,
                        "detail": f"Approval {decision} for {mods}."},
        })
        await manager.broadcast({"type": "event_appended", "event": event})
        return web.json_response({"ok": True, "approval": approval, "event": event, "ledgerEntry": entry})

    async def post_approval_approve(request: web.Request) -> web.Response:
        """Approve a protected-scope approval (does not auto-run the workflow).

        Args:
            request: web.Request — match_info 'approvalId'

        Returns:
            web.Response — JSON {ok, approval, event, ledgerEntry} or 404/409.
        """
        return await _resolve_approval(request, "approved")

    async def post_approval_reject(request: web.Request) -> web.Response:
        """Reject a protected-scope approval.

        Args:
            request: web.Request — match_info 'approvalId'

        Returns:
            web.Response — JSON {ok, approval, event, ledgerEntry} or 404/409.
        """
        return await _resolve_approval(request, "rejected")

    # ================================================================== #
    #  REST — /api/git  (real history + auto-commit, Step 18)           #
    # ================================================================== #

    async def get_git_status(request: web.Request) -> web.Response:
        """Return git status for the watched repo (branch, head, dirty, staged).

        Args:
            request: web.Request

        Returns:
            web.Response — JSON git status dict.
        """
        return web.json_response(git_status(path))

    async def get_git_timeline(request: web.Request) -> web.Response:
        """Return commit history newest-first.

        Args:
            request: web.Request — optional query ?limit=N

        Returns:
            web.Response — JSON array of commit dicts.
        """
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        loop = asyncio.get_event_loop()                        # git log = subprocess → off the loop
        return web.json_response(await loop.run_in_executor(None, lambda: git_timeline(path, limit)))

    async def post_git_commit(request: web.Request) -> web.Response:
        """Stage meaningful repo files and commit, then record a timeline event.

        Initializes git safely if absent. Commits only when something changed;
        `.openfde/` and build dirs are excluded via .gitignore. On a real
        commit, appends a commit_created event and broadcasts it.

        Args:
            request: web.Request — body: {summary, detail?, eventId?, runId?,
                     projectEntryId?, boxIds?, filePaths?}

        Returns:
            web.Response — JSON {ok, committed, sha?, shortSha?, files, event?}
        """
        body = await request.json()
        summary = (body.get("summary") or "openfde: update").strip()
        detail = body.get("detail", "") or ""
        trailers = {
            "OpenFDE-Event":        body.get("eventId"),
            "OpenFDE-Run":          body.get("runId"),
            "OpenFDE-Project-Entry": body.get("projectEntryId"),
        }
        result = git_commit(path, summary, detail, trailers)

        response = {"ok": True, **result}
        if result.get("committed"):
            event = persistence.append_event({
                "type": "commit_created",
                "payload": {
                    "sha":      result["sha"],
                    "shortSha": result["shortSha"],
                    "summary":  summary,
                    "fileCount": len(result.get("files", [])),
                    "runId":    body.get("runId"),
                    "detail":   f"Committed {result['shortSha']}: {summary}",
                },
            })
            await manager.broadcast({"type": "event_appended", "event": event})
            response["event"] = event
        return web.json_response(response)

    async def get_git_diff(request: web.Request) -> web.Response:
        """Return commit metadata, changed files, stat, and a capped patch.

        Args:
            request: web.Request — match_info 'sha'

        Returns:
            web.Response — JSON diff dict, or 404 when the commit is unknown.
        """
        sha = request.match_info.get("sha", "")
        diff = git_diff(path, sha)
        if diff is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response(diff)

    async def get_commit_impact(request: web.Request) -> web.Response:
        """Canvas-native commit lens: which files a commit touched + which semantic
        concepts (tethers) it affected (and which it only partially covered).

        Args:
            request: web.Request — match_info 'sha'.

        Returns:
            web.Response — {ok, sha, shortSha, summary, timestamp, files, fileCount,
                            affectedConcepts:[{identifier, partial, untouchedFiles…}]}.
        """
        sha = request.match_info.get("sha", "")
        diff = git_diff(path, sha)
        if diff is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        files = [f["path"] for f in diff.get("files", [])]
        graph = semantic_graph_mod.load_graph(path)
        concepts = semantic_graph_mod.concepts_for_files(graph, files) if graph else []
        return web.json_response({
            "ok": True, "sha": diff["sha"], "shortSha": diff["shortSha"],
            "summary": diff.get("summary", ""), "timestamp": diff.get("timestamp"),
            "files": files, "fileCount": len(files), "affectedConcepts": concepts,
        })

    async def get_worktree_impact(request: web.Request) -> web.Response:
        """Review Delta: the uncommitted working tree as an architecture delta.

        The "Review" leg of Land · Watch · Review for changes that aren't committed
        yet — whatever an external agent or the user just wrote. Mirrors
        ``get_commit_impact`` but reads the live work tree instead of a sha, via the
        **non-staging** ``worktree_impact`` helper (never runs ``git add``), so it is
        safe to poll while the user edits. Concept delta + high-signal partial-tether
        warnings come from the same semantic-graph helpers the commit lens uses.

        Args:
            request: web.Request — no params.

        Returns:
            web.Response — {ok, dirty, files:[{path,status,additions,deletions}],
                            fileCount, shownCount, stat, patch, patchTruncated,
                            untracked, affectedConcepts, partialConcepts, signature}.
        """
        def _compute():                                        # git diff + graph reads → off the loop
            imp = worktree_impact(path)
            file_paths = [f["path"] for f in imp.get("files", [])]
            graph = semantic_graph_mod.load_graph(path)
            concepts = semantic_graph_mod.concepts_for_files(graph, file_paths) if graph else []
            partial = semantic_graph_mod.tethers_partially_touched(graph, file_paths) if graph else []
            return {
                "ok": True, "dirty": imp["dirty"],
                "files": imp["files"], "fileCount": imp["fileCount"], "shownCount": imp["shownCount"],
                "stat": imp["stat"], "patch": imp["patch"], "patchTruncated": imp["patchTruncated"],
                "untracked": imp["untracked"], "affectedConcepts": concepts,
                "partialConcepts": partial, "signature": imp["signature"],
            }
        return web.json_response(await asyncio.get_event_loop().run_in_executor(None, _compute))

    # ================================================================== #
    #  REST — /api/report  (REPORT.md, Step 18)                         #
    # ================================================================== #

    async def post_report(request: web.Request) -> web.Response:
        """Generate REPORT.md, write it to the repo root, and commit it.

        Deterministic roll-up of project memory, ledger, events, commits, and
        runs. Appends a report_generated timeline event.

        Args:
            request: web.Request — body ignored.

        Returns:
            web.Response — JSON {ok, markdown, commit?}
        """
        md = generate_report(
            persistence.load_project(),
            persistence.load_project_log(),
            persistence.load_events(),
            git_timeline(path, 100),
            persistence.load_runs(),
        )
        report_path = path / "REPORT.md"
        tmp = path / ".report_md.tmp"
        try:
            tmp.write_text(md, encoding="utf-8")
            os.replace(tmp, report_path)
            logger.info("REPORT.md written")
        except OSError as exc:
            logger.error("Failed to write REPORT.md: %s", exc)

        event = persistence.append_event({
            "type": "report_generated",
            "payload": {"detail": "REPORT.md generated"},
        })
        await manager.broadcast({"type": "event_appended", "event": event})

        commit = git_commit(path, "openfde: generate REPORT.md")
        result = {"ok": True, "markdown": md, "event": event}
        if commit.get("committed"):
            ce = persistence.append_event({
                "type": "commit_created",
                "payload": {
                    "sha": commit["sha"], "shortSha": commit["shortSha"],
                    "summary": "openfde: generate REPORT.md",
                    "fileCount": len(commit.get("files", [])),
                    "detail": f"Committed {commit['shortSha']}: REPORT.md",
                },
            })
            await manager.broadcast({"type": "event_appended", "event": ce})
            result["commit"] = commit
            result["commitEvent"] = ce
        return web.json_response(result)

    # ================================================================== #
    #  REST — /api/plan                                                   #
    # ================================================================== #

    async def get_plan(request: web.Request) -> web.Response:
        """Return the generated PLAN.md as a markdown string.

        Always reads the latest persisted state/tasks/project from disk.

        Args:
            request: web.Request

        Returns:
            web.Response — text/markdown document
        """
        md = generate_plan(
            persistence.load_state(),
            persistence.load_tasks(),
            persistence.load_project(),
        )
        return web.Response(text=md, content_type="text/markdown")

    # ================================================================== #
    #  OPTIONS preflight catch-all                                        #
    # ================================================================== #

    async def handle_options(request: web.Request) -> web.Response:
        """Handle CORS preflight for all /api/* paths.

        Args:
            request: web.Request

        Returns:
            web.Response — empty 200 (CORS headers added by middleware)
        """
        return web.Response()

    async def get_session(request: web.Request) -> web.Response:
        """Authoritative watched-repo identity — runtime, NOT .openfde/project.json.name.
        The UI keys its session on repoRoot and shows repoName from the first frame; the
        CLI uses this to refuse a port already held by a different repo."""
        loop = asyncio.get_event_loop()
        payload = await loop.run_in_executor(
            None, lambda: session_mod.session_payload(path, server_started_at, _OPENFDE_VERSION))
        return web.json_response({"ok": True, **payload})

    # ── Council Chat Router (v1) — one chat, routed across the council ───────
    # Read-only Q&A: route a message to Architect / Senior Dev / Verifier (or have
    # Architect + Senior Dev DISCUSS) and answer from the generated CouncilContext.
    # The brain is PURE (openfde.council_router / council_context); the server only
    # injects the live stores + the _text_role callers. This NEVER edits files and
    # NEVER dispatches run_council / run_claude_code / hatch / workflow — run_ask can
    # only invoke the injected text callers. Senior Dev's EDIT mode may be busy; that
    # is reported, never used to block read-only Senior Dev chat.

    def _council_available() -> dict:
        s = persistence.load_agent_settings()
        return {r: _text_role(s.get(r, {})) is not None for r in agent_settings_mod.ROLES}

    def _council_agent_states() -> dict:
        return council_context_mod.derive_agent_states(
            available=_council_available(), runs=persistence.load_runs(),
            active_run_ids=set(_RUN_CONTROLS))

    def _council_context() -> dict:
        # Ground the council in the CURRENT direction: the watched repo's memory-kit lifecycle
        # (.openfde/DECISIONS.md Now/Next) + flow contract, OpenFDE's own ROADMAP (tail = latest),
        # and what just landed. Bounded reads; council_context skips template placeholders.
        def _doc(rel, *, tail=0, cap=8000):
            try:
                f = path / rel
                if not f.is_file():
                    return ""
                data = f.read_text(encoding="utf-8", errors="replace")
                return data[-tail:] if tail else data[:cap]
            except OSError:
                return ""
        commits = [c.get("summary", "") for c in git_timeline(path, limit=6)]
        return council_context_mod.build_council_context(
            active_episode=persistence.latest_active_episode(),
            recent_episodes=persistence.load_episodes(),
            repo_status=git_status(path),
            verify_latest=persistence.load_verify_latest(),
            project=persistence.load_project(),
            project_log=persistence.load_project_log(),
            agent_states=_council_agent_states(),
            decisions_md=_doc(".openfde/DECISIONS.md"),
            flow_md=_doc(".openfde/FLOW.md") + "\n\n" + _doc("FLOW.md"),
            roadmap_md=_doc("ROADMAP.md", tail=16000),
            recent_commits=commits)

    async def get_council_context(request: web.Request) -> web.Response:
        loop = asyncio.get_event_loop()
        ctx = await loop.run_in_executor(None, _council_context)
        return web.json_response({"ok": True, "context": ctx})

    async def post_council_ask(request: web.Request) -> web.Response:
        """Route ONE chat message and answer READ-ONLY. Body: {question, target?};
        target ∈ auto|architect|senior_dev|verifier|discuss (default auto). Answers
        through _text_role callers only — never dispatches an editing runner."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        question = (body.get("question") or "").strip()
        target = (body.get("target") or "auto").strip().lower()
        if not question:
            return web.json_response({"ok": False, "error": "question required"}, status=400)

        def _work():
            settings = persistence.load_agent_settings()
            agent_states = _council_agent_states()
            ctx = _council_context()
            decision = council_router_mod.route(question, target, agent_states)
            # Inject the text callers: Architect/Verifier and Senior Dev's READ-ONLY
            # chat role all build a _text_role caller the same way — none can edit the
            # repo. (Senior Dev's edit runner is never referenced on this path.)
            callers = {r: _text_role(settings.get(r, {})) for r in agent_settings_mod.ROLES}
            # Additive per-role instructions (taste only) — layered after the fixed
            # read-only contract inside build_role_prompt; never override it.
            custom_prompts = {r: settings.get(r, {}).get("customPrompt", "")
                              for r in agent_settings_mod.ROLES}
            result = council_router_mod.run_ask(
                question=question, decision=decision, context=ctx, callers=callers,
                role_human=_ROLE_HUMAN, custom_prompts=custom_prompts)
            used = result.get("usedRole")
            if used:
                prov = settings.get(used, {}).get("provider")
                if prov:
                    result["provider"] = prov     # secondary metadata, not the label
            result["routedTarget"] = target
            result["agents"] = agent_states
            # Additive (Role-led Council): a structured, one-lead-role brief. The lead's section reuses
            # the answer above; the OTHER two sections are consulted from their owning role via that
            # role's OWN read-only text caller (reusing the `callers` already built — no extra wiring),
            # each with the centralized SECTION_ROLE_PROMPTS. Unavailable roles fall back deterministically.
            def _section_filler(brief_role, role_prompt):
                srole = council_router_mod.SETTINGS_ROLE.get(brief_role, brief_role)
                caller = callers.get(srole)
                if not caller:
                    return ""
                system, user = council_router_mod.build_section_prompt(
                    brief_role, role_prompt, question, ctx,
                    custom_prompt=custom_prompts.get(srole, ""))
                try:
                    return (caller(system, user) or "").strip()
                except Exception:  # noqa: BLE001 — provider failure degrades to the deterministic default
                    return ""
            result["brief"] = council_router_mod.role_led_brief(
                question, decision=decision, answer=result.get("answer"),
                section_filler=_section_filler)
            result["brief"]["question"] = question      # self-contained: lets a restored brief start a handoff
            return result

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _work)
        except Exception as exc:  # noqa: BLE001 — read-only chat must fail soft
            logger.error("council ask failed: %s", exc)
            return web.json_response({"ok": False, "error": "council ask failed"}, status=500)
        # Persist the turn so a browser refresh restores the thread (read-only: this writes only
        # OpenFDE's own .openfde/ state, never the repo). Best-effort — never block the answer.
        ts = datetime.now(timezone.utc).isoformat()
        try:
            persistence.append_council_chat([
                {"role": "user", "text": question, "ts": ts},
                {"role": "assistant", "text": result.get("answer", ""),
                 "label": result.get("label", ""), "provider": result.get("provider"),
                 "contributorsLabel": result.get("contributorsLabel"),
                 # Persist the structured role-led brief too, so a refresh restores the
                 # lead-role card instead of falling back to plain assistant text. Older
                 # turns saved without this key simply hydrate as plain text (brief absent).
                 "brief": result.get("brief"),
                 "routedTarget": result.get("routedTarget"), "ts": ts},
            ])
        except Exception:  # noqa: BLE001
            logger.warning("could not persist council chat turn")
        return web.json_response({"ok": True, **result})

    async def get_council_history(request: web.Request) -> web.Response:
        """Recent council chat thread (oldest-first) so a refresh restores it, never an empty box."""
        return web.json_response({"ok": True, "turns": persistence.load_council_chat()})

    async def post_council_implementation(request: web.Request) -> web.Response:
        """Create a SAFE, visible implementation handoff from a role-led council brief. Body:
        {question, brief?}.

        Thin wrapper over the module-level :func:`create_council_handoff` (which holds the logic and is
        directly testable). READ-ONLY w.r.t. repo files: it re-validates the gate, persists a pending
        handoff record + a confirmation turn, and NEVER dispatches a file-editing run — that path
        (`/api/council/run`) requires a canvas scope a chat brief does not carry."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            body = None                                   # → handled as a 400 below

        def _work():
            return create_council_handoff(body, persistence=persistence,
                                          agent_states=_council_agent_states())
        try:
            loop = asyncio.get_event_loop()
            status, payload = await loop.run_in_executor(None, _work)
        except Exception as exc:  # noqa: BLE001
            logger.error("council handoff failed: %s", exc)
            return web.json_response({"ok": False, "error": "handoff failed"}, status=500)
        return web.json_response(payload, status=status)

    # ---- register routes (order matters: specific before catch-all) -----
    app.router.add_get("/ws",                          ws_handler)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", handle_options)
    app.router.add_get( "/api/session",               get_session)
    app.router.add_get( "/api/plugins",                  get_plugins)
    app.router.add_get( "/api/plugins/webxr/summary",    get_webxr_summary)
    app.router.add_get( "/api/plugins/install-plan/{id}", get_plugin_install_plan)
    app.router.add_get( "/api/plugins/treesitter-recommendation", get_treesitter_recommendation)
    app.router.add_post("/api/plugins/{id}/install",     post_plugin_install)
    app.router.add_post("/api/focus/neighborhood",       post_focus_neighborhood)
    app.router.add_post("/api/focus/verify-plan",         post_focus_verify_plan)
    app.router.add_get( "/api/boot",                  get_boot)
    app.router.add_get( "/api/boot/canvas",           get_boot_canvas)
    app.router.add_get( "/api/files",                 get_files)
    app.router.add_get( "/api/state",                 get_state)
    app.router.add_put( "/api/state",                 put_state)
    app.router.add_get( "/api/tasks",                 get_tasks)
    app.router.add_put( "/api/tasks",                 put_tasks)
    app.router.add_post("/api/dev/sketch-demo",       post_sketch_demo)
    app.router.add_post("/api/dev/saas-demo",          post_saas_demo)
    app.router.add_get( "/api/issues/github/list",    get_github_issues)
    app.router.add_post("/api/issues/github/import",  post_github_issue_import)
    app.router.add_post("/api/issues/reproduce",       post_issue_reproduce)
    app.router.add_get( "/api/verify/status",         get_verify_status)
    app.router.add_post("/api/verify/run",            post_verify_run)
    app.router.add_get( "/api/source",                get_source_slice)
    app.router.add_post("/api/source/patch",          post_source_patch)
    app.router.add_get( "/api/events",                get_events)
    app.router.add_post("/api/events",                post_event)
    app.router.add_get( "/api/project",               get_project)
    app.router.add_post("/api/project",               post_project)
    app.router.add_get( "/api/project-log",           get_project_log)
    app.router.add_post("/api/project-log",           post_project_log)
    app.router.add_get( "/api/project-md",            get_project_md)
    app.router.add_post("/api/box-specs/update-from-execute", post_box_specs_update)
    app.router.add_get( "/api/box-specs",             get_box_specs)
    app.router.add_get( "/api/box-specs/{boxId}",     get_box_spec)
    app.router.add_post("/api/runs",                  post_run)
    app.router.add_get( "/api/runs",                  get_runs)
    app.router.add_post("/api/runs/{runId}/event",    post_run_event)
    app.router.add_get( "/api/runs/{runId}",          get_run_one)
    app.router.add_get( "/api/execution/backends",        get_execution_backends)
    app.router.add_post("/api/execution/backend",         post_execution_backend)
    app.router.add_get( "/api/agent-settings",            get_agent_settings)
    app.router.add_put( "/api/agent-settings",            put_agent_settings)
    app.router.add_post("/api/agent-settings/check",      post_agent_settings_check)
    app.router.add_get( "/api/semantic-graph",            get_semantic_graph)
    app.router.add_post("/api/semantic-graph/refresh",    post_semantic_graph_refresh)
    app.router.add_post("/api/concept/ask",               post_concept_ask)
    app.router.add_post("/api/hatch/explain",             post_hatch_explain)
    app.router.add_post("/api/hatch/prompt",              post_hatch_prompt)
    app.router.add_post("/api/hatch/flow",                post_hatch_flow)
    app.router.add_post("/api/hatch/run",                 post_hatch_run)
    app.router.add_post("/api/hatch/artifacts",           post_hatch_artifacts)
    app.router.add_post("/api/feedback/github-issue",     post_feedback_issue)
    app.router.add_post("/api/feedback/draft",            post_feedback_draft)
    app.router.add_post("/api/feedback/draft-general",    post_feedback_draft_general)
    app.router.add_get( "/api/concept-cards",             get_concept_cards)
    app.router.add_post("/api/concept-cards",             post_concept_card)
    app.router.add_post("/api/execution/compile-workflow", post_compile_workflow)
    app.router.add_post("/api/execution/run",             post_execution_run)
    app.router.add_post("/api/agent/run",                 post_agent_run)
    app.router.add_post("/api/council/run",               post_council_run)
    app.router.add_post("/api/council/{runId}/cancel",     post_council_cancel)
    app.router.add_get( "/api/council/context",           get_council_context)
    app.router.add_post("/api/council/ask",               post_council_ask)
    app.router.add_get("/api/council/history",            get_council_history)
    app.router.add_post("/api/council/implementation",    post_council_implementation)
    app.router.add_get( "/api/execution/workflows",       get_workflows)
    app.router.add_post("/api/execution/workflow/{workflowId}/result", post_workflow_result)
    app.router.add_get( "/api/execution/workflow/{workflowId}", get_workflow_one)
    app.router.add_get( "/api/approvals",                  get_approvals)
    app.router.add_post("/api/approvals/{approvalId}/approve", post_approval_approve)
    app.router.add_post("/api/approvals/{approvalId}/reject",  post_approval_reject)
    app.router.add_get( "/api/git/status",            get_git_status)
    app.router.add_get( "/api/git/timeline",          get_git_timeline)
    app.router.add_post("/api/git/commit",            post_git_commit)
    app.router.add_get( "/api/git/commit/{sha}/diff", get_git_diff)
    app.router.add_get( "/api/git/commit/{sha}/impact", get_commit_impact)
    app.router.add_get( "/api/git/worktree/impact", get_worktree_impact)
    app.router.add_post("/api/review/reassimilate", post_review_reassimilate)
    app.router.add_get( "/api/review/episodes", get_review_episodes)
    app.router.add_get( "/api/review/episodes/full", get_review_episodes_full)
    app.router.add_get( "/api/story/prompt-graph", get_prompt_story_graph)
    app.router.add_get( "/api/story/boot",         get_story_boot)
    app.router.add_post("/api/review/episodes/summarize", post_summarize_episodes)
    app.router.add_post("/api/review/episodes", post_review_episode_create)
    app.router.add_post("/api/review/episodes/{episodeId}/land", post_review_episode_land)
    app.router.add_post("/api/review/episodes/{episodeId}/pr",   post_episode_pr)
    app.router.add_get( "/api/review/episodes/{episodeId}/pr/readiness", get_episode_pr_readiness)
    app.router.add_post("/api/report",                post_report)
    app.router.add_get( "/api/plan",                  get_plan)
    app.router.add_get( "/api/archgraph",             get_archgraph)
    app.router.add_post("/api/explain",               post_explain)
    app.router.add_post("/api/story",                 post_story)
    app.router.add_post("/api/state/from-archgraph",  post_state_from_archgraph)
    app.router.add_post("/api/spec",                  post_spec)

    # ================================================================== #
    #  Static / SPA fallback                                              #
    # ================================================================== #

    if dist_dir.exists():
        dist_resolved = dist_dir.resolve()

        # index.html must NEVER be cached (a stale index references purged hashed
        # chunks → users see pre-rebuild UI / ELK 404s); hashed assets under
        # /assets are immutable by construction and may cache forever.
        _NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}
        _IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

        async def serve_root(request: web.Request) -> web.FileResponse:
            """Serve index.html for the root path (never cached).

            Args:
                request: web.Request

            Returns:
                web.FileResponse — frontend/dist/index.html
            """
            return web.FileResponse(dist_dir / "index.html", headers=_NO_CACHE)

        async def serve_spa(request: web.Request) -> web.FileResponse:
            """Serve a static asset or fall back to index.html for SPA routing.

            Hashed assets are immutable-cached; the index fallback never is.

            Args:
                request: web.Request

            Returns:
                web.FileResponse — requested asset or index.html
            """
            rel = request.match_info.get("path", "")
            if rel:
                candidate = (dist_dir / rel).resolve()
                try:
                    candidate.relative_to(dist_resolved)
                    if candidate.is_file():
                        headers = _IMMUTABLE if rel.startswith("assets/") else _NO_CACHE
                        return web.FileResponse(candidate, headers=headers)
                except (ValueError, OSError):
                    pass
            return web.FileResponse(dist_dir / "index.html", headers=_NO_CACHE)

        app.router.add_get("/",          serve_root)
        app.router.add_get("/{path:.*}", serve_spa)
    else:
        async def no_dist(request: web.Request) -> web.Response:
            """Return build instructions when frontend/dist is missing.

            Args:
                request: web.Request

            Returns:
                web.Response — HTML 200 with build instructions
            """
            return web.Response(
                content_type="text/html",
                text=(
                    "<html><body>"
                    '<pre style="font-family:monospace;padding:2rem;font-size:14px">'
                    "⚠  frontend/dist not found.\n\n"
                    "Build the frontend first:\n\n"
                    "  cd frontend\n"
                    "  npm install\n"
                    "  npm run build\n\n"
                    "Then re-run:\n\n"
                    f"  openfde watch {path}\n"
                    "</pre></body></html>"
                ),
            )

        app.router.add_get("/",          no_dist)
        app.router.add_get("/{path:.*}", no_dist)

    # ---- start -----------------------------------------------------------
    url = f"http://localhost:{port}"
    print(f"\n  OpenFDE")
    print(f"  Watching: {path}")
    print(f"  Server:   {url}\n")

    if auto_open:
        loop = asyncio.get_event_loop()
        loop.call_later(0.5, lambda: webbrowser.open(url))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    try:
        await site.start()
    except OSError as exc:
        # The probe above catches an OpenFDE server on this port; this catches anything
        # else holding it (a non-OpenFDE process, or a race) — fail clearly, never silently.
        print(f"\n  ✗  Could not bind port {port}: {exc}\n"
              f"     The port is in use. Stop the other process or choose another port.\n")
        await runner.cleanup()
        return

    logger.info("Server started on port %d — bound in %dms", port,
                int((time.perf_counter() - _boot_t0) * 1000))

    # Everything below runs in the BACKGROUND, after the server is already serving — nothing here
    # may block /api/boot, the WebSocket, or first paint (VSCode-style: activate AFTER startup).

    # ── Memory kit (deferred): bootstrap the calm `.openfde/` markdown room. Markdown templating +
    # light I/O, but it used to run SYNCHRONOUSLY here and stall the first requests — now off-loop.
    async def _bootstrap_memory():
        try:
            from openfde import memory_kit
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: memory_kit.bootstrap_memory_kit(persistence, path))
        except Exception:  # noqa: BLE001 — memory is a convenience, never blocks the watcher
            logger.debug("memory_kit bootstrap failed", exc_info=True)
    memory_task = asyncio.create_task(_bootstrap_memory())

    # ── Warm the ArchGraph in the background (thread executor), then nudge the canvas with
    # state_updated. /api/boot already serves the last warm snapshot, so the user never waits on
    # this — it just refreshes once the fresh scan lands.
    async def _warm_arch():
        try:
            await asyncio.sleep(3)                          # let first paint settle before any work
            # Only pay the (possibly minute-long) full scan on a COLD start — no warm arch on disk.
            # If a snapshot exists we keep serving it (stale is fine); a 57s analyze_repo must never
            # run under the user. Reassimilation + an explicit rescan keep the arch current.
            warm = boot_cache_mod.read_warm(persistence.openfde_dir)
            if warm and warm.get("arch"):
                logger.info("arch warm: snapshot present — skipping startup scan")
                return
            t0 = time.perf_counter()
            await _archgraph_async()
            logger.info("background arch warm (cold) done in %dms", int((time.perf_counter() - t0) * 1000))
            await manager.broadcast({"type": "state_updated", "payload": {"reason": "arch_warm"}})
        except Exception:  # noqa: BLE001 — best-effort warm-up, never fatal
            logger.debug("background arch warm failed", exc_info=True)
    warm_task = asyncio.create_task(_warm_arch())

    # ── Warm the Story boot cache (thread executor) so a fresh restart serves the recent Story
    # instantly. /api/story/boot already returns the last cache (or a "Restoring…" placeholder); this
    # builds the full graph once after first paint, refreshes the cache, and broadcasts story_updated.
    async def _warm_story():
        try:
            await asyncio.sleep(4)                          # let first paint settle before any heavy build
            # Warm restart: a cache already exists → /api/story/boot serves it instantly, so skip the
            # GIL-heavy rebuild (it refreshes on the next Land or Story open). Only a COLD start (no
            # cache) pays the build — and even then boot returns "building" instantly meanwhile.
            if story_cache_mod.read_story_cache(persistence.openfde_dir):
                logger.info("story cache present — skipping startup rebuild")
                return
            await _refresh_story_cache_and_broadcast()
            logger.info("story cache warmed at startup (cold)")
        except Exception:  # noqa: BLE001 — best-effort warm-up, never fatal
            logger.debug("background story warm failed", exc_info=True)
    warm_story_task = asyncio.create_task(_warm_story())
    # Keep the tiny rail boot cache recent through capture/land/create, so first paint always reads
    # ~5 KB instead of parsing the full store (which was clogging the boot pool + starving Story boot).
    rail_cache_task = asyncio.create_task(_rail_cache_poller())

    # ── Watch Any Agent: glow the canvas live on ANY external edit (Cursor,
    # Claude Code, terminal, human) — suppressed while our own council run glows.
    async def _resolve_watch_function(rel: str):
        """Best-effort: which function did this external edit touch? A read-only ``git diff``
        (never stages — safe to run on every save) against the cached ArchGraph. Returns
        ``{"function": "<name>"}`` when a single function is implicated, else None so the glow
        falls back to the file box. Generic: derives everything from the diff + arch graph."""
        graph = _arch_mem.get("graph")
        if not graph:
            return None
        fns = [f for f in graph.get("functions", []) if f.get("path") == rel]
        if not fns:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--no-color", "HEAD", "--", rel, cwd=str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except (OSError, asyncio.TimeoutError):
            return None
        name = watch_function.infer_changed_function(
            watch_function.changed_line_numbers(out.decode("utf-8", "replace")), fns)
        return {"function": name} if name else None

    watch_task = asyncio.create_task(fs_watch.watch_loop(
        path, manager, is_run_active=lambda: bool(_RUN_CONTROLS),
        resolve_function=_resolve_watch_function))

    # ── Passive Prompt Capture: tail this repo's Claude Code transcripts and turn
    # new human prompts into prompt-story episodes (no `openfde cc` wrapper needed).
    from openfde import prompt_capture
    capture_task = asyncio.create_task(prompt_capture.watch_loop(path, persistence, manager))

    # ── LLM Story Summarizer (best-effort): upgrade one episode's title/summary/storyFacts
    # per cycle using the local Codex/Claude CLI — off the request path, cached per
    # fingerprint, deterministic fallback. No-op when no local CLI provider is available.
    summarizer_task = asyncio.create_task(_summarizer_loop(persistence, manager))

    # ── Historical backfill (best-effort, once): reconstruct prompt episodes from local
    # agent transcripts for work done BEFORE OpenFDE started watching. Idempotent (keyed
    # by captureKey); never commits. Off the startup path so a large transcript home never
    # delays the server; a quiet receipt event lands when done.
    async def _run_backfill():
        try:
            from openfde import backfill, memory_kit
            loop2 = asyncio.get_event_loop()
            res = await loop2.run_in_executor(None, lambda: backfill.backfill_historical(path, persistence))
            if res.get("imported"):
                if res.get("event"):
                    await manager.broadcast({"type": "event_appended", "event": res["event"]})
                await manager.broadcast({"type": "episodes_changed",
                                         "payload": {"reason": "backfill", "count": res["imported"]}})
                await loop2.run_in_executor(None, lambda: memory_kit.regenerate_generated(persistence, path))
        except Exception:  # noqa: BLE001 — backfill must never break the watcher
            logger.debug("backfill failed", exc_info=True)
    backfill_task = asyncio.create_task(_run_backfill())

    try:
        await asyncio.Event().wait()       # run until cancelled / KeyboardInterrupt (Ctrl-C)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Server stopping")
        for t in (watch_task, capture_task, summarizer_task, backfill_task, memory_task,
                  warm_task, warm_story_task, rail_cache_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await runner.cleanup()
        release_watch_lock(watch_lock)
