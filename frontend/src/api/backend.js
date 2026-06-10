/**
 * Thin client for the OpenFDE backend REST API.
 *
 * All exported functions return null on network error, timeout, or non-2xx
 * response — callers treat null as "backend unavailable" and fall back to
 * in-memory / demo state.
 *
 * BASE is always an empty string so that requests go to the same origin.
 * In Vite dev mode the vite.config.js proxy forwards /api/* to port 7373.
 * In production the backend itself serves the frontend from the same origin.
 */

const TIMEOUT_MS = 4000
const PROBE_TIMEOUT_MS = 1500

/**
 * Make a fetch request with a timeout and JSON/text auto-parse.
 *
 * @param {string} path - API path starting with /
 * @param {RequestInit} [opts={}] - fetch options (method, body, headers, …)
 * @returns {Promise<any|null>} Parsed JSON, plain text, or null on error
 */
async function apiFetch(path, opts = {}) {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), opts._timeout ?? TIMEOUT_MS)
    const res = await fetch(path, {
      ...opts,
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(opts.headers ?? {}) },
    })
    clearTimeout(timer)
    if (!res.ok) return null
    const ct = res.headers.get('content-type') ?? ''
    return ct.includes('application/json') ? res.json() : res.text()
  } catch {
    return null
  }
}

/**
 * Probe whether the backend is reachable.
 * Uses a short 1.5 s timeout so the app degrades quickly when offline.
 *
 * @returns {Promise<boolean>}
 */
export async function isBackendAvailable() {
  const result = await apiFetch('/api/project', { _timeout: PROBE_TIMEOUT_MS })
  return result !== null
}

// ── File tree ─────────────────────────────────────────────────────────────

/**
 * Fetch the recursive file tree for the watched repository.
 *
 * @returns {Promise<Object|null>} Root directory tree node, or null
 */
export const getFiles = () => apiFetch('/api/files')

// ── Canvas state ──────────────────────────────────────────────────────────

/**
 * Fetch the persisted canvas state.
 *
 * @returns {Promise<{boxes: Array, arrows: Array}|null>}
 */
export const getState = () => apiFetch('/api/state')

/**
 * Persist the canvas state (boxes and arrows only; Sets are not serialisable).
 *
 * @param {{ boxes: Array, arrows: Array }} state
 * @returns {Promise<{ok: boolean}|null>}
 */
export const putState = (state) =>
  apiFetch('/api/state', {
    method: 'PUT',
    body: JSON.stringify({ boxes: state.boxes, arrows: state.arrows }),
  })

// ── Tasks ─────────────────────────────────────────────────────────────────

/**
 * Fetch the persisted OpenPM task list.
 *
 * @returns {Promise<Array|null>}
 */
export const getTasks = () => apiFetch('/api/tasks')

/**
 * Persist the full OpenPM task list.
 *
 * @param {Array} tasks
 * @returns {Promise<{ok: boolean}|null>}
 */
export const putTasks = (tasks) =>
  apiFetch('/api/tasks', { method: 'PUT', body: JSON.stringify(tasks) })

// ── GitHub issue intents (durable intent v1 — local gh CLI) ───────────────

/**
 * List open GitHub issues for this repo (via `gh` on the backend).
 *
 * @returns {Promise<{ok: boolean, issues?: Array, error?: string}|null>}
 */
export const listGithubIssues = () => apiFetch('/api/issues/github/list')

/**
 * Import one GitHub issue as a durable-intent OpenPM card (idempotent).
 *
 * @param {{issueNumber?: number, issue?: Object}} payload
 * @returns {Promise<{ok: boolean, task?: Object, created?: boolean, error?: string}|null>}
 */
export const importGithubIssue = (payload) =>
  // Shells the local gh CLI (~1–5s); headroom over the 4s default.
  apiFetch('/api/issues/github/import',
    { method: 'POST', body: JSON.stringify(payload), _timeout: 30_000 })

// ── Verify Gate (local receipts) ──────────────────────────────────────────

/**
 * Discovered checks + latest worktree-level verification evidence (no run).
 *
 * @returns {Promise<{ok: boolean, checks: Array, latest: Object}|null>}
 */
export const getVerifyStatus = () => apiFetch('/api/verify/status')

/**
 * Run the repo's local checks now; optionally attach evidence to an episode.
 *
 * @param {{episodeId?: string}} [payload]
 * @returns {Promise<{ok: boolean, verify?: Object}|null>}
 */
export const runVerify = (payload = {}) =>
  // The checks ARE the latency (suite + lint ≈ 20s here; each check may take up
  // to 300s server-side) — wait for the receipts instead of aborting at 4s.
  apiFetch('/api/verify/run', { method: 'POST', body: JSON.stringify(payload), _timeout: 300_000 })

// ── Land as PR (episode → branch → GitHub PR via local gh) ───────────────

/**
 * Open a GitHub PR for a landed episode (manual action; idempotent).
 *
 * @param {string} episodeId
 * @returns {Promise<{ok: boolean, pr?: Object, existing?: boolean, error?: string}|null>}
 */
export const createEpisodePr = (episodeId) =>
  // Branch push + `gh pr create` are network ops (5–20s) — don't abort at 4s.
  apiFetch(`/api/review/episodes/${encodeURIComponent(episodeId)}/pr`,
    { method: 'POST', body: '{}', _timeout: 120_000 })

/**
 * Fresh, read-only ready-for-PR verdict (deterministic evidence + policy).
 *
 * @param {string} episodeId
 * @returns {Promise<{ok: boolean, readiness?: Object}|null>}
 */
export const getPrReadiness = (episodeId) =>
  apiFetch(`/api/review/episodes/${encodeURIComponent(episodeId)}/pr/readiness`)

// ── Events ────────────────────────────────────────────────────────────────

/**
 * Fetch all persisted design/code events (oldest-first).
 *
 * @returns {Promise<Array|null>}
 */
export const getEvents = () => apiFetch('/api/events')

/**
 * Append a single design/code event.  Fire-and-forget — callers do not
 * need to await the result.
 *
 * @param {Object} evt - Event object (must have at least a 'type' field)
 * @returns {Promise<{ok: boolean}|null>}
 */
export const postEvent = (evt) =>
  apiFetch('/api/events', { method: 'POST', body: JSON.stringify(evt) })

// ── Project ───────────────────────────────────────────────────────────────

/**
 * Fetch project metadata (name, description, entries).
 *
 * @returns {Promise<Object|null>}
 */
export const getProject = () => apiFetch('/api/project')

/**
 * Persist project metadata (also regenerates PROJECT.md on the server).
 *
 * @param {Object} data - { name, description, entries }
 * @returns {Promise<{ok: boolean}|null>}
 */
export const postProject = (data) =>
  apiFetch('/api/project', { method: 'POST', body: JSON.stringify(data) })

// ── Project log (conversation ledger) ───────────────────────────────────────

/**
 * Fetch all conversation-ledger entries (oldest-first).
 *
 * @returns {Promise<Array|null>}
 */
export const getProjectLog = () => apiFetch('/api/project-log')

/**
 * Append one ledger entry. The server regenerates repo-root project.md.
 *
 * @param {{ role: string, title?: string, summary?: string, body?: string,
 *           eventId?: string, boxIds?: string[], arrowIds?: string[],
 *           filePaths?: string[], metadata?: Object }} entry
 * @returns {Promise<{ok: boolean, entry: Object}|null>}
 */
export const postProjectLog = (entry) =>
  apiFetch('/api/project-log', { method: 'POST', body: JSON.stringify(entry) })

/**
 * Fetch the freshly-generated project.md ledger as a markdown string.
 *
 * @returns {Promise<string|null>}
 */
export const getProjectMd = () => apiFetch('/api/project-md')

// ── Box specs (prompt provenance) ───────────────────────────────────────────

/**
 * Fetch the full box-specs map (boxId → spec).
 *
 * @returns {Promise<Object|null>}
 */
export const getBoxSpecs = () => apiFetch('/api/box-specs')

/**
 * Deterministically update box specs for the scoped boxes of an Execute run.
 *
 * @param {{ boxIds: string[], userPrompt?: string, ledgerEntryId?: string,
 *           eventId?: string, filePaths?: string[], summary?: string,
 *           outcome?: string }} payload
 * @returns {Promise<{ok: boolean, updated: string[], specs: Object}|null>}
 */
export const postBoxSpecsUpdate = (payload) =>
  apiFetch('/api/box-specs/update-from-execute', {
    method: 'POST',
    body: JSON.stringify(payload),
  })

// ── Execution runs + trace (Step 17) ────────────────────────────────────────

/**
 * Start an execution run (visualization for the current placeholder flow).
 *
 * @param {{ scopedBoxIds: string[], scopedArrowIds?: string[],
 *           scopedFileIds?: string[], scopedFunctionIds?: string[] }} payload
 * @returns {Promise<{ok: boolean, run: Object, event: Object}|null>}
 */
export const postRunStart = (payload) =>
  apiFetch('/api/runs', { method: 'POST', body: JSON.stringify(payload) })

/**
 * Append a trace event to a run. Payloads are summarized + redacted server-side.
 *
 * @param {string} runId
 * @param {{ type: string, nodeId?: string, edgeId?: string, status?: string,
 *           input?: any, output?: any, error?: any, errorSummary?: string }} event
 * @returns {Promise<{ok: boolean, event: Object, run?: Object, timelineEvent?: Object}|null>}
 */
export const postRunEvent = (runId, event) =>
  apiFetch(`/api/runs/${encodeURIComponent(runId)}/event`, {
    method: 'POST',
    body: JSON.stringify(event),
  })

/** Fetch all run records (latest-first). @returns {Promise<Array|null>} */
export const getRuns = () => apiFetch('/api/runs')

/** Fetch a run + its trace events. @returns {Promise<{run, events}|null>} */
export const getRun = (runId) => apiFetch(`/api/runs/${encodeURIComponent(runId)}`)

// ── Git timeline + report (Step 18) ─────────────────────────────────────────

/** Repo git status (branch, head, dirty, staged). @returns {Promise<Object|null>} */
export const getGitStatus = () => apiFetch('/api/git/status')

/** Commit history newest-first. @returns {Promise<Array|null>} */
export const getGitTimeline = () => apiFetch('/api/git/timeline')

/**
 * Stage meaningful repo files and commit (no-op when nothing changed).
 *
 * @param {{ summary: string, detail?: string, eventId?: string, runId?: string,
 *           projectEntryId?: string, boxIds?: string[], filePaths?: string[] }} payload
 * @returns {Promise<{ok, committed, sha?, shortSha?, files, event?}|null>}
 */
export const postGitCommit = (payload) =>
  apiFetch('/api/git/commit', { method: 'POST', body: JSON.stringify(payload) })

/** Commit diff (metadata, files, stat, capped patch). @returns {Promise<Object|null>} */
export const getGitDiff = (sha) => apiFetch(`/api/git/commit/${encodeURIComponent(sha)}/diff`)

/** Generate + write + commit REPORT.md (LLM-backed — minutes, not 4s). @returns {Promise<{ok, markdown, commit?}|null>} */
export const postReport = () =>
  apiFetch('/api/report', { method: 'POST', body: JSON.stringify({}), _timeout: 300_000 })

// ── Execution backends + Claude Code workflow bridge (Step 19) ──────────────

/** List execution backends + active. @returns {Promise<{backends, active}|null>} */
export const getExecutionBackends = () => apiFetch('/api/execution/backends')

/** Set the active execution backend. @returns {Promise<{ok, active}|null>} */
export const setExecutionBackend = (backend) =>
  apiFetch('/api/execution/backend', { method: 'POST', body: JSON.stringify({ backend }) })

/**
 * Prepare a workflow run for the active backend (no auto-execution).
 *
 * @param {{ selectedBoxIds: string[], selectedArrowIds: string[], prompt: string }} payload
 * @returns {Promise<{ok, status, workflow, run, event, architectEntry, srDevEntry}|null>}
 */
export const postExecutionRun = (payload) =>
  apiFetch('/api/execution/run', { method: 'POST', body: JSON.stringify(payload) })

/** List prepared workflow artifacts. @returns {Promise<Array|null>} */
export const getWorkflows = () => apiFetch('/api/execution/workflows')

// ── Workflow result intake + approvals (Step 20) ────────────────────────────

/**
 * Submit a Claude Code workflow result for reconciliation.
 *
 * @param {string} workflowId
 * @param {object} result - the Step-19 output contract.
 * @returns {Promise<{ok, status, committed, commitSha?, approval?, events, ledgerEntries}|null>}
 */
export const postWorkflowResult = (workflowId, result) =>
  apiFetch(`/api/execution/workflow/${encodeURIComponent(workflowId)}/result`, {
    method: 'POST', body: JSON.stringify(result),
  })

/** List protected-scope approval requests. @returns {Promise<Array|null>} */
export const getApprovals = () => apiFetch('/api/approvals')

/** Approve a protected-scope gate. @returns {Promise<{ok, approval, event, ledgerEntry}|null>} */
export const approveApproval = (approvalId) =>
  apiFetch(`/api/approvals/${encodeURIComponent(approvalId)}/approve`, { method: 'POST', body: '{}' })

/** Reject a protected-scope gate. @returns {Promise<{ok, approval, event, ledgerEntry}|null>} */
export const rejectApproval = (approvalId) =>
  apiFetch(`/api/approvals/${encodeURIComponent(approvalId)}/reject`, { method: 'POST', body: '{}' })

/**
 * Run the native Senior Dev agent over the selected scope (Step 22a).
 * Makes a real (or echo) provider call server-side, edits in-scope files, and
 * reconciles through the gated commit path. Unlike apiFetch, this preserves the
 * server's JSON error body on non-2xx so the UI can show the real reason.
 *
 * @param {{selectedBoxIds:string[], selectedArrowIds:string[], prompt:string}} payload
 * @returns {Promise<object>} the reconciliation payload, or {ok:false,error}.
 */
export async function postAgentRun(payload) {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 180000)
    const res = await fetch('/api/agent/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    clearTimeout(timer)
    const ct = res.headers.get('content-type') ?? ''
    const data = ct.includes('application/json') ? await res.json() : null
    if (!res.ok) {
      return { ok: false, error: (data && data.error) || `Request failed (HTTP ${res.status}).` }
    }
    return data ?? { ok: false, error: 'Empty response from agent run.' }
  } catch {
    return { ok: false, error: 'Native agent request failed (network or timeout).' }
  }
}

/**
 * Run one bounded Agent Council loop (Step 29 Slice 2): Architect → Senior Dev →
 * Verifier → reprompt-or-advance, landed through the gated reconciliation.
 * Backend orchestration; no new UI. Returns {ok, runId, status, stages, ...}.
 *
 * @param {{selectedBoxIds:string[], selectedArrowIds:string[], prompt:string}} payload
 * @returns {Promise<object|null>}
 */
export async function postCouncilRun(payload) {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 300000)
    const res = await fetch('/api/council/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    clearTimeout(timer)
    const ct = res.headers.get('content-type') ?? ''
    const data = ct.includes('application/json') ? await res.json() : null
    if (!res.ok) return { ok: false, error: (data && data.error) || `Request failed (HTTP ${res.status}).` }
    return data ?? { ok: false, error: 'Empty response from council run.' }
  } catch {
    return { ok: false, error: 'Council request failed (network or timeout).' }
  }
}

// Cancel an in-flight council run (Step 33). Best-effort; never throws.
export async function cancelCouncilRun(runId) {
  if (!runId) return { ok: false, error: 'No run id.' }
  try {
    const res = await fetch(`/api/council/${encodeURIComponent(runId)}/cancel`, { method: 'POST' })
    const ct = res.headers.get('content-type') ?? ''
    const data = ct.includes('application/json') ? await res.json() : null
    if (!res.ok) return { ok: false, error: (data && data.error) || `Cancel failed (HTTP ${res.status}).` }
    return data ?? { ok: true }
  } catch {
    return { ok: false, error: 'Cancel request failed.' }
  }
}

// ── Agent settings (role → provider config, Step 21) ───────────────────────

/**
 * Fetch sanitized agent role settings plus UI options.
 * Never returns raw API keys — only hasApiKey + maskedApiKey per role.
 *
 * @returns {Promise<{ok, settings, options}|null>}
 */
export const getAgentSettings = () => apiFetch('/api/agent-settings')

/**
 * Apply a full/partial agent-settings update; returns sanitized settings.
 * Omit apiKey to keep the stored key; send clearApiKey:true to wipe it.
 *
 * @param {object} payload - {settings:{...}} or a bare {role:{...}} map
 * @returns {Promise<{ok, settings, options}|null>}
 */
export const putAgentSettings = (payload) =>
  apiFetch('/api/agent-settings', { method: 'PUT', body: JSON.stringify(payload) })

/**
 * Validate config shape only (no network). Returns per-role results.
 *
 * @param {object} payload - {role, config} | {settings} | {}
 * @returns {Promise<{ok, roles}|null>}
 */
export const checkAgentSettings = (payload = {}) =>
  apiFetch('/api/agent-settings/check', { method: 'POST', body: JSON.stringify(payload) })

// ── Semantic graph (Step 37a) ───────────────────────────────────────────────

/**
 * Fetch the stored semantic-graph summary (counts, top tethers, provider runs).
 *
 * @returns {Promise<{ok, exists, summary}|null>}
 */
export const getSemanticGraph = () => apiFetch('/api/semantic-graph')

/**
 * Regenerate .openfde/semantic_graph.json for the watched repo; returns summary.
 *
 * @returns {Promise<{ok, summary}|null>}
 */
export const refreshSemanticGraph = () =>
  apiFetch('/api/semantic-graph/refresh', { method: 'POST' })

/**
 * Canvas commit lens: files a commit touched + affected semantic concepts.
 *
 * @param {string} sha
 * @returns {Promise<{ok, sha, shortSha, summary, files, fileCount, affectedConcepts}|null>}
 */
export const getCommitImpact = (sha) =>
  apiFetch(`/api/git/commit/${encodeURIComponent(sha)}/impact`)

/**
 * Review Delta: the uncommitted working tree as an architecture delta. Non-mutating
 * (never stages) — safe to poll. Drives the "Review changes" affordance.
 *
 * @returns {Promise<{ok, dirty, files:Array, fileCount, shownCount, stat, patch,
 *   patchTruncated, untracked:string[], affectedConcepts:Array, partialConcepts:Array,
 *   signature:string}|null>}
 */
export const getWorktreeImpact = () => apiFetch('/api/git/worktree/impact')

/**
 * Incremental Re-assimilation: after external edits settle, refresh OpenFDE's
 * understanding (ArchGraph + semantic graph) so Review/Land reflect new files.
 * v1 recomputes fully internally but is triggered by the changed files. Never
 * mutates canvas state or git.
 *
 * @param {string[]} files - changed repo-relative paths (advisory in v1).
 * @param {string} reason - e.g. 'file_activity'.
 * @returns {Promise<{ok, files, reason, mode, archGraph, semanticSummary, warnings}|null>}
 */
export const reassimilateReview = (files = [], reason = 'file_activity') =>
  apiFetch('/api/review/reassimilate', { method: 'POST', body: JSON.stringify({ files, reason }) })

/**
 * Prompt episodes for the Story Rail (OpenFDE-owned commits). Returns episodes
 * newest-first with their landed commits + an "Outside OpenFDE" bucket.
 * @returns {Promise<{ok, episodes:Array, outside:Object}|null>}
 */
export const getReviewEpisodes = () => apiFetch('/api/review/episodes')

/** Create a prompt episode (e.g. a "Manual changes" bucket). @returns {Promise<Object|null>} */
export const createEpisode = (payload = {}) =>
  apiFetch('/api/review/episodes', { method: 'POST', body: JSON.stringify(payload) })

/**
 * Prompt Story Graph — the conceptual narrative derived from prompt episodes:
 * active / deferred / abandoned concepts, each linked to its episodes, commits,
 * and files. Deterministic backend (no LLM). Distinct from the Timeline.
 * @returns {Promise<{ok, concepts:Array, episodes:Array, edges:Array, counts:Object}|null>}
 */
export const getPromptGraph = () => apiFetch('/api/story/prompt-graph')

/**
 * On-demand LLM story summary — upgrade up to `limit` episodes' titles/summaries via the
 * local Codex/Claude CLI (best-effort; deterministic when no provider). The background
 * summarizer also runs automatically, pushing `episode_updated` over WS.
 * @returns {Promise<{ok, providers:Array, upgraded:number}|null>}
 */
export const summarizeEpisodes = (limit = 1) =>
  // LLM summarizer subprocess — give it the same long leash as the other model calls.
  apiFetch('/api/review/episodes/summarize',
    { method: 'POST', body: JSON.stringify({ limit }), _timeout: 300_000 })

/**
 * Land the current meaningful worktree changes through OpenFDE — the only
 * user-facing path that creates a commit (with OpenFDE-Episode trailers).
 * Pass episodeId "manual" to mint a Manual changes episode.
 * @returns {Promise<{ok, committed, sha?, shortSha?, reason?, episode}|null>}
 */
export const landEpisode = (episodeId, payload = {}) =>
  // Landing runs the Verify Gate + LLM clustering server-side — minutes, not the
  // default 4s. A short abort here LOOKS like "nothing happened" while the server
  // lands anyway (observed live in the v1.1 feel-test).
  apiFetch(`/api/review/episodes/${encodeURIComponent(episodeId)}/land`,
    { method: 'POST', body: JSON.stringify(payload), _timeout: 300_000 })

/**
 * Ask a question about the active concept/commit; routed to Architect or Senior
 * Dev (deterministic v1), else a deterministic semantic-graph answer.
 *
 * @param {string} question
 * @param {object} context - { kind, label, summary?, files, concepts? }
 * @returns {Promise<{ok, answer, role, source}|null>}
 */
export const askConcept = (question, context) =>
  // Routed through the Architect/Sr Dev LLM — answers take 10–60s, never 4s.
  apiFetch('/api/concept/ask',
    { method: 'POST', body: JSON.stringify({ question, context }), _timeout: 300_000 })

/** List saved concept cards (newest-first). @returns {Promise<{ok, cards}|null>} */
export const getConceptCards = () => apiFetch('/api/concept-cards')

/**
 * Save a short concept card.
 * @param {object} card - { title, summary?, tetherId?, commitSha?, files? }
 * @returns {Promise<{ok, card}|null>}
 */
export const saveConceptCard = (card) =>
  apiFetch('/api/concept-cards', { method: 'POST', body: JSON.stringify(card) })

// ── Plan ──────────────────────────────────────────────────────────────────

/**
 * Fetch a freshly-generated PLAN.md as a markdown string.
 *
 * @returns {Promise<string|null>}
 */
export const getPlan = () => apiFetch('/api/plan')

// ── ArchGraph ─────────────────────────────────────────────────────────────

/**
 * Fetch the full ArchGraph for the watched repository (read-only analysis).
 *
 * @returns {Promise<{modules, files, functions, edges, warnings}|null>}
 */
export const getArchgraph = () => apiFetch('/api/archgraph')

/**
 * Generate canvas state from the repo's ArchGraph and persist it server-side.
 *
 * Replaces the current canvas with detected modules as boxes and import
 * edges as arrows.  Returns the generated state plus a summary.
 *
 * @returns {Promise<{ok: boolean, state: {boxes, arrows}, summary: Object}|null>}
 */
export const postFromArchgraph = () =>
  apiFetch('/api/state/from-archgraph', {
    method: 'POST',
    body: JSON.stringify({}),
  })

// ── Spec ──────────────────────────────────────────────────────────────────────

/**
 * Compile a canvas selection into a structured implementation spec.
 *
 * Sends the selected box/arrow IDs and an optional user prompt to the backend,
 * which runs compile_spec() and returns a markdown document plus context.
 * This is a read-only compiler — it does not edit any files.
 *
 * @param {{ selectedBoxIds: string[], selectedArrowIds: string[], prompt: string }} payload
 * @returns {Promise<{ok: boolean, markdown: string, context: Object, event: Object}|null>}
 */
export const postSpec = (payload) =>
  apiFetch('/api/spec', {
    method: 'POST',
    body: JSON.stringify(payload),
  })

/**
 * Explain a canvas selection deterministically (Step 26) using the read model.
 * @param {string[]} selectedBoxIds
 * @returns {Promise<{ok:boolean, markdown:string, summary:string}|null>}
 */
export const postExplain = (selectedBoxIds) =>
  apiFetch('/api/explain', { method: 'POST', body: JSON.stringify({ selectedBoxIds }) })

/**
 * Build a deterministic Story-mode summary for the current selection (Batch 5).
 * @param {string[]} selectedBoxIds
 * @param {{kind:string,id:string,path:string,name:string}|null} selectedEntity
 * @returns {Promise<object|null>}
 */
export const postStory = (selectedBoxIds, selectedEntity) =>
  apiFetch('/api/story', { method: 'POST', body: JSON.stringify({ selectedBoxIds, selectedEntity }) })
