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
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from openfde import agent_settings as agent_settings_mod
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
from openfde.box_spec import apply_workflow_result, update_box_specs_from_execute
from openfde.execution import ACTIVE_DEFAULT, compile_workflow, is_valid_backend, list_backends
from openfde.git_timeline import changed_paths, ensure_baseline, git_commit, git_diff, git_status, git_timeline, worktree_diff
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
        """Return the persisted OpenPM task list.

        Args:
            request: web.Request

        Returns:
            web.Response — JSON array of task objects
        """
        return web.json_response(persistence.load_tasks())

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
                 "the editable files. No code fences, no preamble — just the brief.")
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

        # ── Git reconciliation ───────────────────────────────────────────────
        # Commit only when a *reported source file* (not OpenFDE bookkeeping like
        # project.md / PLAN.md / .openfde/) is actually dirty in the work tree.
        # This prevents no-op commits where ledger writes are the only diff.
        committed, commit_sha, commit_reason = False, None, None
        reported_sources = source_files(result["filesChanged"])
        actually_changed = changed_paths(path, reported_sources)
        if status == "passed" and not actually_changed:
            commit_reason = "no reported source files changed on disk"
        if status == "passed" and actually_changed:
            commit = git_commit(path, commit_message(report),
                                detail=report, trailers={"OpenFDE-Workflow": wid})
            if commit.get("committed"):
                committed, commit_sha = True, commit["sha"]
                ce = persistence.append_event({
                    "type": "commit_created",
                    "payload": {"sha": commit["sha"], "shortSha": commit["shortSha"],
                                "summary": commit["summary"], "fileCount": len(commit.get("files", [])),
                                "workflowId": wid, "detail": f"Committed {commit['shortSha']}: {commit['summary']}"},
                })
                await manager.broadcast({"type": "event_appended", "event": ce})
                event_ids.append(ce["id"])
            else:
                commit_reason = commit.get("reason") or "commit produced no changes"

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

    try:
        await asyncio.Event().wait()       # run until cancelled / KeyboardInterrupt
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Server stopping")
        await runner.cleanup()
