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
import secrets
import shutil
import subprocess
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from openfde import agent_settings as agent_settings_mod
from openfde import fs_watch
from openfde import semantic_graph as semantic_graph_mod
from openfde.agent_runner import build_system_prompt, run_agent
from openfde.anthropic_transport import make_transport
from openfde.claude_code_runner import cli_available as claude_cli_available, run_claude_code, run_claude_code_text
from openfde.codex_local_runner import cli_available as codex_cli_available, run_codex_local_text
from openfde.echo_transport import make_echo_transport
from openfde.openai_transport import complete as llm_complete, make_transport as make_openai_transport
from openfde.council import run_council
from openfde.architect import analyze_repo, generate_canvas_state
from openfde.explain import explain_selection
from openfde.story import build_story
from openfde.prompt_story import build_prompt_graph
from openfde.episode_summary import commit_display, is_bad_title, repair_episode_tasks
from openfde.issue_intents import gh_issue_list, gh_issue_view, normalize_issue, upsert_intent_task
from openfde import verify as verify_mod
from openfde import prs as prs_mod
from openfde.prs import create_episode_pr, pr_readiness
from openfde import episode_llm_summary
from openfde.box_spec import apply_workflow_result, update_box_specs_from_execute
from openfde.execution import ACTIVE_DEFAULT, compile_workflow, is_valid_backend, list_backends
from openfde.git_timeline import changed_paths, commit_files, ensure_baseline, git_commit, git_diff, git_status, git_timeline, worktree_diff, worktree_impact
from openfde.report import generate_report
from openfde.spec import compile_spec
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
    """Generate and atomically write PLAN.md to the watched repo root.

    Reads current state, tasks, and project from disk, generates markdown
    via generate_plan(), and writes atomically (tmp → rename).

    Args:
        persistence: Persistence — the active persistence instance
        repo_root: Path — repository root where PLAN.md is written

    Returns:
        None
    """
    md = generate_plan(
        persistence.load_state(),
        persistence.load_tasks(),
        persistence.load_project(),
    )
    plan_path = repo_root / "PLAN.md"
    tmp_path  = repo_root / ".plan_md.tmp"
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
    path.mkdir(parents=True, exist_ok=True)

    openfde_dir = path / ".openfde"
    openfde_dir.mkdir(exist_ok=True)

    persistence = Persistence(openfde_dir)
    manager     = ConnectionManager()

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

    async def get_archgraph(request: web.Request) -> web.Response:
        """Return the ArchGraph for the watched repository.

        Runs the OpenArchitect read-only analyzer on the watched repo path.
        Results include modules, files, functions, edges, and warnings.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON ArchGraph dict.
        """
        graph = analyze_repo(path)
        return web.json_response(graph)

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

    async def get_review_episodes(request: web.Request) -> web.Response:
        """List prompt episodes newest-first with their landed commits, plus an
        "Outside OpenFDE" bucket for commits not linked to any episode.

        Returns:
            web.Response — {ok, episodes:[{…episode, commits, commitCount,
                            fileCount}], outside:{…synthetic bucket}}.
        """
        persistence.backfill_episode_meta()                    # title/tag/seq/signal
        episodes = episode_llm_summary.ensure_facts(persistence)  # storyFacts (deterministic; no subprocess)
        commits = git_timeline(path, limit=200)
        by_ep: dict = {}
        outside: list = []
        for c in commits:
            eid = c.get("episodeId")
            (by_ep.setdefault(eid, []) if eid else outside).append(c)
        known = {e.get("episodeId") for e in episodes}
        # Enrich each episode commit with its file set (cheap name-only show) so the
        # rail's nested commit chips and OpenPM commit tasks have files without a
        # second fetch. Capped across the whole response so it stays a quick poll.
        budget = 60
        file_cache: dict = {}

        def _commit_view(c: dict, ep: dict) -> dict:
            nonlocal budget
            sha = c.get("sha")
            if sha and sha not in file_cache and budget > 0:
                budget -= 1
                file_cache[sha] = commit_files(path, sha)
            cf = file_cache.get(sha, [])
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
            return {**c, "files": cf, "fileCount": len(cf),
                    "displayTitle": dtitle, "displaySummary": dsummary}

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
                         "on_base": _on_base, "gh_ok": shutil.which("gh") is not None}

        enriched = []
        for e in episodes:
            ecs = [_commit_view(c, e) for c in by_ep.get(e["episodeId"], [])]
            enriched.append({**e, "commits": ecs, "commitCount": len(ecs),
                             "fileCount": len(e.get("files") or []),
                             "prReadiness": pr_readiness(path, e, _ctx=readiness_ctx)})
        # Commits whose episode trailer references an unknown (foreign/older) id
        # are surfaced under Outside OpenFDE too — never silently dropped.
        foreign = [c for c in commits if c.get("episodeId") and c["episodeId"] not in known]
        outside_commits = outside + foreign
        outside_bucket = {
            "episodeId": "outside", "kind": "manual", "status": "landed",
            "prompt": "Outside OpenFDE",
            "summary": "Commits not linked to an OpenFDE prompt (manual / foreign).",
            "commits": outside_commits, "commitCount": len(outside_commits),
            "files": [], "fileCount": 0,
        }
        return web.json_response({"ok": True, "episodes": enriched, "outside": outside_bucket})

    async def get_prompt_story_graph(request: web.Request) -> web.Response:
        """Prompt Story Graph — the conceptual narrative built from prompt episodes.

        Deterministic (no LLM, no git mutation): active concepts from episode titles,
        deferred/abandoned concepts from strong signals in episode text, each linked
        to its episodes/commits/files. Distinct from the Timeline (events) and the
        architecture story (code flow).

        Returns:
            web.Response — {ok, concepts[], episodes[], edges[], counts}.
        """
        persistence.backfill_episode_meta()
        episodes = episode_llm_summary.ensure_facts(persistence)  # storyFacts (deterministic; no subprocess)
        return web.json_response(build_prompt_graph(episodes))

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
                          "question about this concept/commit at the architecture level — concise, plain "
                          "language, 2-5 sentences, NO code dumps.")
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
        # Claude Code (local CLI) text role — no key, runs on the user's login.
        if prov == "claude-code-local":
            if not claude_cli_available():
                return None
            model = cfg.get("model") or "sonnet"
            return lambda system, user: run_claude_code_text(
                system=system, user=user, model=model, cwd=path)
        # Codex (local CLI) text role — Day 3B. Drives `codex exec -s read-only`,
        # keyless (uses the local Codex login); never mutates the repo.
        if prov == "codex-local":
            if not codex_cli_available():
                return None
            model = cfg.get("model") or None
            return lambda system, user: run_codex_local_text(
                system=system, user=user, model=model, cwd=path)
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
        if not editable:
            return web.json_response({"ok": False, "error":
                "No editable (dotted) in-scope files. Select a dotted box with linked files."}, status=400)

        scope_summary = f"{len(box_ids)} module(s), {len(editable)} editable file(s)"
        user_prompt = (body.get("prompt") or "").strip() or "Implement the selected architecture scope."
        wid = "council_" + secrets.token_hex(5)
        now = datetime.now(timezone.utc).isoformat()

        started = persistence.append_event({
            "type": "council_started",
            "payload": {"runId": wid, "detail": f"Agent Council started: {scope_summary}"},
        })
        await manager.broadcast({"type": "event_appended", "event": started})

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
            if arch_caller:
                user = (f"Intent: {c['prompt']}\nScope: {c['scopeSummary']}\n"
                        f"Editable files: {c['editable']}\nProtected (approval needed): {c['protected']}")
                return arch_caller(_ARCH_SYS, user)
            return (f"Implement: {c['prompt']} within {c['scopeSummary']}. "
                    f"Edit only: {', '.join(c['editable'])}. Keep changes minimal and verify the intent.")

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
                cc_prompt = (f"{brief}\n\nConstraints:\n"
                             f"- Edit ONLY these files: {ed}\n"
                             f"- Do NOT modify these protected files: {pr}\n"
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
                sd_transport, sd_model = make_echo_transport(path, editable), (sd.get("model") or "echo-1")
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
                     "editable": editable, "protected": protected},
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
        persistence.save_workflow_artifact(artifact)
        persistence.upsert_run({
            "runId": wid, "status": "prepared", "backend": "openfde-council", "kind": "council_run",
            "simulated": False, "startedAt": now, "endedAt": None,
            "scopedBoxIds": box_ids, "scopedArrowIds": [], "scopedFileIds": [], "scopedFunctionIds": [],
        })

        payload = await reconcile_result(artifact, wid, final)
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
                              status: str) -> dict:
        """Create or update the prompt episode that owns this run's changes.

        Episodes are the durable "prompt turn" — the user's intent plus the runs,
        events, and (after Land) commits it produced. Re-uses an episode already
        linked to ``wid``; otherwise mints a new one. Never downgrades a 'landed'
        episode. Returns the stored episode (caller broadcasts).
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
                actually_changed, event_ids, ledger_ids, report[:200], "reviewing")
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
                actually_changed, event_ids, ledger_ids, report[:200], "reviewing")
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
                [], event_ids, ledger_ids, report[:200], "open")
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
        return web.json_response(git_timeline(path, limit))

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
        imp = worktree_impact(path)
        file_paths = [f["path"] for f in imp.get("files", [])]
        graph = semantic_graph_mod.load_graph(path)
        concepts = semantic_graph_mod.concepts_for_files(graph, file_paths) if graph else []
        partial = semantic_graph_mod.tethers_partially_touched(graph, file_paths) if graph else []
        return web.json_response({
            "ok": True, "dirty": imp["dirty"],
            "files": imp["files"], "fileCount": imp["fileCount"], "shownCount": imp["shownCount"],
            "stat": imp["stat"], "patch": imp["patch"], "patchTruncated": imp["patchTruncated"],
            "untracked": imp["untracked"], "affectedConcepts": concepts,
            "partialConcepts": partial, "signature": imp["signature"],
        })

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

    # ---- register routes (order matters: specific before catch-all) -----
    app.router.add_get("/ws",                          ws_handler)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", handle_options)
    app.router.add_get( "/api/files",                 get_files)
    app.router.add_get( "/api/state",                 get_state)
    app.router.add_put( "/api/state",                 put_state)
    app.router.add_get( "/api/tasks",                 get_tasks)
    app.router.add_put( "/api/tasks",                 put_tasks)
    app.router.add_get( "/api/issues/github/list",    get_github_issues)
    app.router.add_post("/api/issues/github/import",  post_github_issue_import)
    app.router.add_get( "/api/verify/status",         get_verify_status)
    app.router.add_post("/api/verify/run",            post_verify_run)
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
    app.router.add_get( "/api/concept-cards",             get_concept_cards)
    app.router.add_post("/api/concept-cards",             post_concept_card)
    app.router.add_post("/api/execution/compile-workflow", post_compile_workflow)
    app.router.add_post("/api/execution/run",             post_execution_run)
    app.router.add_post("/api/agent/run",                 post_agent_run)
    app.router.add_post("/api/council/run",               post_council_run)
    app.router.add_post("/api/council/{runId}/cancel",     post_council_cancel)
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
    app.router.add_get( "/api/story/prompt-graph", get_prompt_story_graph)
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

        async def serve_root(request: web.Request) -> web.FileResponse:
            """Serve index.html for the root path.

            Args:
                request: web.Request

            Returns:
                web.FileResponse — frontend/dist/index.html
            """
            return web.FileResponse(dist_dir / "index.html")

        async def serve_spa(request: web.Request) -> web.FileResponse:
            """Serve a static asset or fall back to index.html for SPA routing.

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
                        return web.FileResponse(candidate)
                except (ValueError, OSError):
                    pass
            return web.FileResponse(dist_dir / "index.html")

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
    await site.start()

    logger.info("Server started on port %d", port)

    # ── Watch Any Agent: glow the canvas live on ANY external edit (Cursor,
    # Claude Code, terminal, human) — suppressed while our own council run glows.
    watch_task = asyncio.create_task(fs_watch.watch_loop(
        path, manager, is_run_active=lambda: bool(_RUN_CONTROLS)))

    # ── Passive Prompt Capture: tail this repo's Claude Code transcripts and turn
    # new human prompts into prompt-story episodes (no `openfde cc` wrapper needed).
    from openfde import prompt_capture
    capture_task = asyncio.create_task(prompt_capture.watch_loop(path, persistence, manager))

    # ── LLM Story Summarizer (best-effort): upgrade one episode's title/summary/storyFacts
    # per cycle using the local Codex/Claude CLI — off the request path, cached per
    # fingerprint, deterministic fallback. No-op when no local CLI provider is available.
    summarizer_task = asyncio.create_task(_summarizer_loop(persistence, manager))

    try:
        await asyncio.Event().wait()       # run until cancelled / KeyboardInterrupt
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Server stopping")
        for t in (watch_task, capture_task, summarizer_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await runner.cleanup()
