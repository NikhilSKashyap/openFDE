import { useState, useEffect, useRef } from 'react'
import './App.css'
import Toolbar from './components/Toolbar/Toolbar'
import FileTree from './components/FileTree/FileTree'
import Whiteboard from './components/Whiteboard/Whiteboard'
import RightPanel from './components/RightPanel/RightPanel'
import WorkPanel from './components/WorkPanel/WorkPanel'
import { deriveMoment } from './productFlow/deriveMoment'
import CommandPalette from './components/CommandPalette/CommandPalette'
import AgentSettings from './components/AgentSettings/AgentSettings'
import { useCanvasState } from './store/canvasState'
import { usePMState } from './store/pmState'
import {
  isBackendAvailable,
  getState, putState,
  getTasks, putTasks,
  postEvent,
  getEvents,
  postFromArchgraph,
  postSpec,
  postProjectLog,
  getBoxSpecs,
  postBoxSpecsUpdate,
  getArchgraph,
  postRunStart,
  postRunEvent,
  getGitTimeline,
  postGitCommit,
  getGitDiff,
  postReport,
  getExecutionBackends,
  setExecutionBackend,
  postExecutionRun,
  postWorkflowResult,
  getApprovals,
  approveApproval,
  rejectApproval,
  getAgentSettings,
  postAgentRun,
  postCouncilRun,
  cancelCouncilRun,
  postExplain,
  postStory,
} from './api/backend'
import { connectWS, closeWS } from './api/ws'

// Merge event lists, dedup by id, newest-first, capped. Existing (live) entries
// win over incoming so locally-enriched fields (live, projectEntryId) survive.
function mergeEvents(existing, incoming) {
  const byId = new Map()
  for (const e of [...(existing || []), ...(incoming || [])]) {
    if (e && e.id && !byId.has(e.id)) byId.set(e.id, e)
  }
  return [...byId.values()]
    .sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''))
    .slice(0, 200)
}

export default function App() {
  const [theme, setTheme] = useState(() => localStorage.getItem('openfde-theme') || 'dark')
  const [activeTool, setActiveTool] = useState('select')
  const [activeView, setActiveView] = useState('whiteboard')
  const [canvasState, _rawCanvasDispatch] = useCanvasState()
  // Live mirror of boxes so WS handlers (stable closures) can map file→module.
  const boxesRef = useRef(canvasState.boxes)
  const [tasks, pmDispatch] = usePMState()
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [panelMode, setPanelMode] = useState('Agent')
  const [selectedTaskId, setSelectedTaskId] = useState(null)
  const [designEvents, setDesignEvents] = useState([])
  const [specMarkdown, setSpecMarkdown] = useState(null)
  const [specLoading, setSpecLoading]   = useState(false)
  // Agent chat: generated messages (architect / sr_dev). Empty → seed demo shown.
  const [agentMessages, setAgentMessages] = useState([])
  const [executing, setExecuting]         = useState(false)
  // Box prompt-provenance map (boxId → spec); hydrated from backend, updated on Execute.
  const [boxSpecs, setBoxSpecs]           = useState({})
  // ── In-place nested architecture (Step 16) ───────────────────────────────
  // archGraph:   read-only ArchGraph from the backend (files + functions).
  // expandedIds: set of expanded node ids (module box ids + "box:file:<path>").
  // archSel:     currently-inspected file/function entity ({kind, data}) or null.
  const [archGraph, setArchGraph]     = useState(null)
  const [expandedIds, setExpandedIds] = useState(() => new Set())
  const [archSel, setArchSel]         = useState(null)
  // ── Execution run / live trace (Step 17) ─────────────────────────────────
  // run: { runId, status, scopedBoxIds, scopedArrowIds, nodeStates, edgeStates,
  //        trace: {id:[events]}, failures: {id:{...}} } | null
  const [run, setRun] = useState(null)
  const runRef = useRef(null)
  // ── Git timeline + diff inspection (Step 18) ─────────────────────────────
  const [gitCommits, setGitCommits] = useState([])
  const [commitDiff, setCommitDiff] = useState(null)   // { loading, sha, data }
  // ── Execution backend (Step 19) ──────────────────────────────────────────
  const [backends, setBackends] = useState([])
  const [activeBackend, setActiveBackend] = useState('openfde-native')
  // ── Workflow result intake + approvals (Step 20) ─────────────────────────
  const [approvals, setApprovals] = useState([])
  // ── Agent role settings (Step 21) ────────────────────────────────────────
  const [agentSettings, setAgentSettings] = useState(null)
  const [agentOptions, setAgentOptions]   = useState(null)
  const [agentSettingsOpen, setAgentSettingsOpen] = useState(false)
  const [leftOpen, setLeftOpen]   = useState(true)
  const [rightOpen, setRightOpen] = useState(true)
  const [flowMode, setFlowMode]   = useState('focused') // Story | Focused | All (Batch 5)
  const [story, setStory]         = useState(null)
  const [rightView, setRightView] = useState('work')    // 'work' (primary) | 'technical' (old tabbed panel)
  const [workIntent, setWorkIntent] = useState('')      // lifted Work-panel intent text
  // Current work unit (Step 28 Slice 2): the active Change→Execute→Review unit.
  // null = no active unit. status drives the Work moment (not a stale global spec).
  const [workUnit, setWorkUnit] = useState(null)        // { intent, status: 'change'|'execute'|'review' } | null
  // Reactive flag for UI — drives CTA button and command palette entry.
  const [backendAvailable, setBackendAvailable] = useState(false)

  // Whether the backend has been confirmed reachable this session.
  // Stored as a ref (not state) so debounced save effects read the latest
  // value without re-subscribing.
  const backendRef = useRef(false)

  // Debounce timers for canvas and task persistence
  const stateDebounce = useRef(null)
  const tasksDebounce = useRef(null)
  // Suppress the one debounced PUT triggered by hydrating from the backend,
  // so reloading the app does not re-persist (and dirty PLAN.md).
  const skipStateSaveRef = useRef(false)
  const skipTasksSaveRef = useRef(false)

  // ------------------------------------------------------------------ //
  //  Backend probe + hydration (runs once on mount)                     //
  // ------------------------------------------------------------------ //
  useEffect(() => {
    let cancelled = false

    async function probe() {
      const available = await isBackendAvailable()
      if (cancelled || !available) return

      backendRef.current = true
      setBackendAvailable(true)

      // Hydrate canvas state — only when backend has data (non-empty boxes).
      // Mark the resulting state change as a hydrate so the debounced PUT that
      // it triggers is skipped (otherwise reload re-persists and dirties PLAN.md).
      const savedState = await getState()
      if (!cancelled && savedState?.boxes?.length > 0) {
        skipStateSaveRef.current = true
        _rawCanvasDispatch({ type: 'HYDRATE', boxes: savedState.boxes, arrows: savedState.arrows ?? [] })
      }

      // Hydrate tasks — only when backend has data (non-empty task list)
      const savedTasks = await getTasks()
      if (!cancelled && savedTasks?.length > 0) {
        skipTasksSaveRef.current = true
        pmDispatch({ type: 'HYDRATE_TASKS', tasks: savedTasks })
      }

      // Hydrate persisted OpenFDE events into the Timeline (oldest-first from
      // the backend; merged + deduped, not marked live).
      const savedEvents = await getEvents()
      if (!cancelled && Array.isArray(savedEvents) && savedEvents.length > 0) {
        setDesignEvents(prev => mergeEvents(prev, savedEvents))
      }

      // Hydrate box specs (prompt provenance)
      const savedSpecs = await getBoxSpecs()
      if (!cancelled && savedSpecs && typeof savedSpecs === 'object') {
        setBoxSpecs(savedSpecs)
      }

      // Read-only ArchGraph for drilldown (files + functions)
      const graph = await getArchgraph()
      if (!cancelled && graph && Array.isArray(graph.files)) {
        setArchGraph(graph)
      }

      // Real git history for the Timeline code rail
      const commits = await getGitTimeline()
      if (!cancelled && Array.isArray(commits)) {
        setGitCommits(commits)
      }

      // Execution backends + active backend (Step 19)
      const be = await getExecutionBackends()
      if (!cancelled && be?.backends) {
        setBackends(be.backends)
        setActiveBackend(be.active || 'openfde-native')
      }

      // Pending approval gates (Step 20)
      const apr = await getApprovals()
      if (!cancelled && Array.isArray(apr)) setApprovals(apr)

      // Agent role settings (Step 21)
      const as = await getAgentSettings()
      if (!cancelled && as?.settings) {
        setAgentSettings(as.settings)
        setAgentOptions(as.options)
      }
    }

    probe()
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Live activity glow (adaptive) ─────────────────────────────────────────
  // Keep boxesRef synced so the WS handlers can map a file path → its module.
  useEffect(() => { boxesRef.current = canvasState.boxes }, [canvasState.boxes])

  const fileNodeId = (p) => `box:file:${p}`
  function moduleBoxForFile(path) {
    return (boxesRef.current || []).find(b => (b.linkedFiles || []).includes(path)) || null
  }

  // Architect plan arrived: pre-pulse the planned files and drill into their
  // modules so the work is visible. First file is queued as `next`.
  function handleAgentPlan({ runId, files }) {
    if (!Array.isArray(files) || files.length === 0) return
    const moduleIds = new Set()
    const nodeStates = {}
    files.forEach((f, i) => {
      nodeStates[fileNodeId(f)] = i === 0 ? 'next' : 'queued'
      const m = moduleBoxForFile(f)
      if (m) moduleIds.add(m.id)
    })
    if (moduleIds.size) {
      setExpandedIds(prev => { const s = new Set(prev); moduleIds.forEach(id => s.add(id)); return s })
    }
    setRun(prev => ({
      ...(prev || { runId: runRef.current, scopedBoxIds: [], scopedArrowIds: [], trace: {}, failures: {} }),
      status: 'running', councilRunId: runId || prev?.councilRunId,
      plannedFiles: files, written: [], read: [], nodeStates, edgeStates: {},
    }))
  }

  // The agent touched a file — `action` is 'read' or 'write'. The touched file
  // glows `active` (the live focus follows the agent). Written files settle to
  // `done` (green), read-only files to `read` (a soft "looked here" mark that
  // persists so the trail is visible, not a flash). The next untouched target is
  // `next`; the rest stay `queued`.
  function handleAgentProgress({ file, action }) {
    if (!file) return
    const m = moduleBoxForFile(file)
    if (m) setExpandedIds(prev => (prev.has(m.id) ? prev : new Set(prev).add(m.id)))
    setRun(prev => {
      if (!prev) return prev
      const base = (prev.plannedFiles && prev.plannedFiles.length) ? prev.plannedFiles : []
      const written = (action === 'write' && !prev.written?.includes(file))
        ? [...(prev.written || []), file] : (prev.written || [])
      const read = prev.read?.includes(file) ? prev.read : [...(prev.read || []), file]
      // Include any out-of-plan file the agent actually touched.
      const targets = base.includes(file) ? base : [...base, file]
      const nodeStates = { ...prev.nodeStates }
      targets.forEach(f => {
        nodeStates[fileNodeId(f)] =
          f === file ? 'active'
            : written.includes(f) ? 'done'
              : read.includes(f) ? 'read'
                : 'queued'
      })
      const idx = targets.indexOf(file)
      const next = targets.slice(idx + 1).find(f => !written.includes(f) && !read.includes(f))
      if (next) nodeStates[fileNodeId(next)] = 'next'
      return { ...prev, written, read, activeFile: file, nodeStates }
    })
  }

  // ------------------------------------------------------------------ //
  //  WebSocket connection (runs once on mount)                           //
  // ------------------------------------------------------------------ //
  useEffect(() => {
    connectWS((msg) => {
      // event_appended: merge the raw event in (existing wins, so a locally
      // enriched copy with projectEntryId / payload.via survives the echo).
      if (msg?.type === 'event_appended' && msg.event?.id) {
        setDesignEvents(prev => mergeEvents(prev, [{ ...msg.event, live: true }]))
      }
      // Live activity stream (adaptive glow): the agent's plan + per-file writes.
      else if (msg?.type === 'agent_plan') { handleAgentPlan(msg.payload || {}) }
      else if (msg?.type === 'agent_progress') { handleAgentProgress(msg.payload || {}) }
      // state_updated / tasks_updated: no-op; this client's own writes are the
      // source of truth for its local state.
    })
    return () => closeWS()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ------------------------------------------------------------------ //
  //  Debounced canvas persistence                                        //
  // ------------------------------------------------------------------ //
  useEffect(() => {
    if (!backendRef.current) return
    if (skipStateSaveRef.current) { skipStateSaveRef.current = false; return }
    clearTimeout(stateDebounce.current)
    stateDebounce.current = setTimeout(() => {
      putState({ boxes: canvasState.boxes, arrows: canvasState.arrows })
    }, 1500)
    return () => clearTimeout(stateDebounce.current)
  }, [canvasState.boxes, canvasState.arrows])

  // ------------------------------------------------------------------ //
  //  Debounced task persistence                                          //
  // ------------------------------------------------------------------ //
  useEffect(() => {
    if (!backendRef.current) return
    if (skipTasksSaveRef.current) { skipTasksSaveRef.current = false; return }
    clearTimeout(tasksDebounce.current)
    tasksDebounce.current = setTimeout(() => {
      putTasks(tasks)
    }, 1500)
    return () => clearTimeout(tasksDebounce.current)
  }, [tasks])

  // ------------------------------------------------------------------ //
  //  Shared event recorder — newest first, capped at 200                //
  // ------------------------------------------------------------------ //
  function addDesignEvent(evt) {
    const enriched = { id: Date.now().toString(36), timestamp: new Date().toISOString(), ...evt }
    setDesignEvents(prev => mergeEvents([enriched], prev))
    // Fire-and-forget persist to backend (the websocket echo is deduped by id)
    if (backendRef.current) postEvent(enriched)
  }

  // ------------------------------------------------------------------ //
  //  Instrumented canvas dispatch — records design events                //
  // ------------------------------------------------------------------ //
  function canvasDispatch(action) {
    _rawCanvasDispatch(action)
    switch (action.type) {
      case 'CREATE_BOX':
        addDesignEvent({ type: 'box_created',        payload: { boxType: action.boxType || 'dotted', detail: `new ${action.boxType || 'dotted'} box` } })
        break
      case 'CREATE_ARROW':
        addDesignEvent({ type: 'arrow_created',      payload: { detail: 'new arrow connection' } })
        break
      case 'DELETE_ARROW':
        addDesignEvent({ type: 'arrow_deleted',      payload: { detail: 'arrow removed' } })
        break
      case 'FREEZE_SELECTED':
        addDesignEvent({ type: 'permission_changed', payload: { detail: 'selected boxes locked → solid' } })
        break
      case 'MAKE_SELECTED_DOTTED':
        addDesignEvent({ type: 'permission_changed', payload: { detail: 'selected boxes unlocked → dotted' } })
        break
      case 'LOAD_SELF_MAP':
        addDesignEvent({ type: 'box_created',        payload: { detail: 'self-map loaded: 6 boxes + 6 arrows' } })
        break
      default: break
    }
  }

  // ------------------------------------------------------------------ //
  //  Generate canvas from repo (OpenArchitect read)                    //
  // ------------------------------------------------------------------ //
  async function onGenerateFromRepo() {
    if (!backendRef.current) return
    const result = await postFromArchgraph()
    if (!result?.state) return
    _rawCanvasDispatch({
      type: 'HYDRATE',
      boxes: result.state.boxes,
      arrows: result.state.arrows ?? [],
    })
    setActiveView('whiteboard')
    // Collapse any expansion and refresh the read-only ArchGraph so nested
    // file/function views reflect the new structure.
    setExpandedIds(new Set())
    setArchSel(null)
    getArchgraph().then(g => { if (g && Array.isArray(g.files)) setArchGraph(g) })
    // Backend is the source of truth for scan events — use the normalized event
    // it already persisted.  We add it to the live feed without addDesignEvent()
    // (which would fire an extra postEvent() and double-write).
    if (result.event) addLiveEvent(result.event)
  }

  // ------------------------------------------------------------------ //
  //  In-place nested expansion (Step 16)                                //
  // ------------------------------------------------------------------ //
  // Toggle a module or file open/closed, in place on the canvas. Module ids
  // are the persisted box ids; file ids are "box:file:<path>".
  function toggleExpand(id, kind) {
    if (!id) return
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
        // Collapsing a module also collapses its files (they only render inside).
        if (kind === 'module') {
          for (const fid of [...next]) if (fid.startsWith('box:file:')) next.delete(fid)
        }
      } else {
        next.add(id)
      }
      return next
    })
    if (!archGraph) getArchgraph().then(g => { if (g && Array.isArray(g.files)) setArchGraph(g) })
  }

  // Expand every module + every file at once.
  function expandAll() {
    if (!archGraph) {
      getArchgraph().then(g => { if (g && Array.isArray(g.files)) { setArchGraph(g); applyExpandAll(g) } })
      return
    }
    applyExpandAll(archGraph)
  }
  function applyExpandAll(graph) {
    const ids = new Set()
    canvasState.boxes.forEach(b => { if (b.moduleId) ids.add(b.id) })
    ;(graph.files || []).forEach(f => ids.add(`box:file:${f.path}`))
    setExpandedIds(ids)
  }
  function collapseAll() { setExpandedIds(new Set()); setArchSel(null) }

  // Expand a single module; with deep=true also expand every file inside it
  // (so all its functions become visible) — wired to the module right-click menu.
  function expandModule(boxId, deep = false) {
    const apply = (graph) => setExpandedIds(prev => {
      const next = new Set(prev)
      next.add(boxId)
      if (deep && graph) {
        const modId = canvasState.boxes.find(b => b.id === boxId)?.moduleId
        ;(graph.files || []).forEach(f => {
          if (!modId || f.moduleId === modId) next.add(`box:file:${f.path}`)
        })
      }
      return next
    })
    if (!archGraph) getArchgraph().then(g => { if (g && Array.isArray(g.files)) { setArchGraph(g); apply(g) } })
    else apply(archGraph)
  }

  // Story mode (Batch 5): fetch a deterministic story for the current selection
  // (module / file / function). Keyed on the selection so it refetches on change.
  const storySelKey = [...(canvasState.selectedIds ?? [])].join(',')
  const storyArchKey = archSel?.data?.id ?? ''
  useEffect(() => {
    if (!backendRef.current || flowMode !== 'story') return
    const boxIds = [...(canvasState.selectedIds ?? [])]
    const entity = archSel?.data
      ? { kind: archSel.kind, id: archSel.data.id, path: archSel.data.path, name: archSel.data.name }
      : null
    let cancelled = false
    const load = async () => {
      const s = (!entity && boxIds.length === 0) ? null : await postStory(boxIds, entity)
      if (!cancelled) setStory(s && s.ok ? s : null)
    }
    load()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowMode, storySelKey, storyArchKey])

  // "Explain this" (Step 26): deterministic explanation of the selected boxes,
  // grounded in the function-flow read model, shown in the Agent panel.
  async function onExplain(boxIds) {
    const ids = (boxIds && boxIds.length) ? boxIds : [...(canvasState.selectedIds ?? [])]
    if (!backendRef.current || !ids.length) return
    openTechnicalMode('Agent')   // surface the explanation in Technical/Agent
    const titles = ids.map(id => canvasState.boxes.find(b => b.id === id)?.title || id)
    const stamp = new Date().toISOString()
    setAgentMessages(prev => [...prev, {
      id: `explain-q-${stamp}`, role: 'user', timestamp: stamp,
      body: `Explain: ${titles.join(', ')}`,
    }])
    const res = await postExplain(ids)
    const done = new Date().toISOString()
    setAgentMessages(prev => [...prev, {
      id: `explain-a-${done}`, role: 'explanation', timestamp: done,
      markdown: res?.markdown || '_Could not generate an explanation._',
      summary: res?.summary || '',
    }])
  }

  // Select (or clear) the inspected file/function entity. Passing a null kind
  // clears it — e.g. when a module box itself is selected.
  function selectArchEntity(kind, data) {
    if (!kind || !data) { setArchSel(null); return }
    setArchSel({ kind, data })
    setPanelMode('Inspector')
  }

  // ------------------------------------------------------------------ //
  //  Compile canvas selection → implementation spec (read-only)         //
  // ------------------------------------------------------------------ //
  async function onGenerateSpec(userPrompt = '') {
    if (!backendRef.current) return
    setSpecLoading(true)
    setRightView('technical')   // Spec is a Technical surface; surface it.
    setPanelMode('Spec')
    const selectedBoxIds   = [...(canvasState.selectedIds ?? [])]
    const selectedArrowIds = [...(canvasState.selectedArrowIds ?? [])]
    const result = await postSpec({ selectedBoxIds, selectedArrowIds, prompt: userPrompt })
    setSpecLoading(false)
    if (!result?.markdown) return
    setSpecMarkdown(result.markdown)
    // Backend is source of truth for spec events — merge the normalized event.
    if (result.event) addLiveEvent(result.event)
  }

  // ------------------------------------------------------------------ //
  //  Execute — primary flow: compile selection → Agent chat             //
  // ------------------------------------------------------------------ //
  // Calls the same /api/spec compiler as the Spec tab, but routes the
  // result into the Agent chat as an Architect message (compact summary +
  // collapsible full spec) followed by a Senior Dev placeholder message, and
  // records both into the project.md conversation ledger (Step 14).
  // No real agent/LLM call yet — this is the product orchestration layer.
  async function onExecute(userPrompt = '') {
    if (!backendRef.current) return
    // Step 19: route to the active execution backend. claude-code-workflow
    // prepares a workflow; openfde-native keeps the existing local flow.
    if (activeBackend === 'claude-code-workflow') return onExecuteWorkflow(userPrompt)
    if (activeBackend === 'openfde-agent') return onExecuteAgent(userPrompt)
    if (activeBackend === 'openfde-council') return onExecuteCouncil(userPrompt)
    setExecuting(true)
    setPanelMode('Agent')
    const selectedBoxIds   = [...(canvasState.selectedIds ?? [])]
    const selectedArrowIds = [...(canvasState.selectedArrowIds ?? [])]
    const result = await postSpec({ selectedBoxIds, selectedArrowIds, prompt: userPrompt })

    if (!result?.markdown) { setExecuting(false); return }

    const ctx        = result.context ?? {}
    const ctxBoxes   = ctx.boxes ?? []
    const ctxArrows  = ctx.arrows ?? []
    const ctxFiles   = ctx.files ?? []
    const dotted     = ctxBoxes.filter(b => b.type === 'dotted')
    const solid      = ctxBoxes.filter(b => b.type !== 'dotted')
    const isGlobal   = selectedBoxIds.length === 0 && selectedArrowIds.length === 0
    const scope      = isGlobal ? 'Repository-level' : `${ctxBoxes.length} module${ctxBoxes.length !== 1 ? 's' : ''} in scope`
    const fileCount  = ctxFiles.length
    const fnCount    = (ctx.functions ?? []).length
    const dottedNames = dotted.map(b => b.title)
    const solidNames  = solid.map(b => b.title)
    const prompt      = (userPrompt ?? '').trim()
    const eventId     = result.event?.id ?? null
    const stamp       = new Date().toISOString()
    const seq         = eventId || stamp   // stable, unique message id suffix

    const permLine = solidNames.length
      ? `Approval required: ${solidNames.join(', ')}${dottedNames.length ? ` · Direct edit: ${dottedNames.join(', ')}` : ''}`
      : (dottedNames.length ? `Direct edit (all dotted): ${dottedNames.join(', ')}` : 'No modules in scope (repo-level prompt)')

    // ── Record the exchange into the project.md ledger (Step 14) ────────────
    // Backend appends each entry and regenerates project.md. Posted before the
    // chat renders so we can link projectEntryId into the chat + timeline.
    const archBody = [
      `Scope: ${scope}`,
      `Selected: ${ctxBoxes.length} module(s), ${fileCount} file(s), ${fnCount} function(s), ${ctxArrows.length} edge(s).`,
      `Permissions: ${permLine}`,
      prompt ? `Requested change: ${prompt}` : 'Requested change: (none specified)',
      '',
      '--- Compiled implementation prompt ---',
      '',
      result.markdown,
    ].join('\n')

    const archRes = await postProjectLog({
      role:      'architect',
      title:     isGlobal ? 'Compiled execution prompt (repo-level)' : `Compiled execution prompt — ${scope}`,
      summary:   `${ctxBoxes.length} module(s), ${fileCount} file(s), ${fnCount} function(s) compiled into an execution prompt.`,
      body:      archBody,
      eventId,
      boxIds:    ctxBoxes.map(b => b.id),
      arrowIds:  ctxArrows.map(a => a.id),
      filePaths: ctxFiles.map(f => f.path),
      metadata:  { via: 'execute', dotted: dottedNames, solid: solidNames, requestedPrompt: prompt },
    })
    const archEntryId = archRes?.entry?.id ?? null

    const srBody = solidNames.length
      ? `Execution prepared. This scope includes protected (solid) module(s): ${solidNames.join(', ')}. Approval will be requested before modifying those files.${dottedNames.length ? ` Direct edits are allowed for: ${dottedNames.join(', ')}.` : ''}\n\nPlaceholder only — the real agent execution backend is not wired up yet. No files have been modified.`
      : `Execution prepared. All scoped modules are dotted (agent-editable); direct edits to the linked files are allowed.\n\nPlaceholder only — the real agent execution backend is not wired up yet. No files have been modified.`

    const srRes = await postProjectLog({
      role:     'sr_dev',
      title:    'Execution queued (placeholder)',
      summary:  'Placeholder execution — real agent backend not wired yet; no files modified.',
      body:     srBody,
      eventId,
      boxIds:   ctxBoxes.map(b => b.id),
      metadata: { placeholder: true },
    })
    const srEntryId = srRes?.entry?.id ?? null

    // ── Update per-box provenance for every scoped box (Step 15) ────────────
    // Deterministic; references the architect ledger entry + event, never the
    // full compiled spec. Repo-level Execute updates all scoped boxes.
    const bsRes = await postBoxSpecsUpdate({
      boxIds:        ctxBoxes.map(b => b.id),
      userPrompt:    prompt,
      ledgerEntryId: archEntryId,
      eventId,
      filePaths:     ctxFiles.map(f => f.path),
      summary:       `${ctxBoxes.length} module(s), ${fileCount} file(s), ${fnCount} function(s)`,
      outcome:       solidNames.length ? 'Execution queued — approval required for protected modules' : 'Execution queued (placeholder)',
    })
    if (bsRes?.specs) setBoxSpecs(bsRes.specs)

    setExecuting(false)

    const architectMsg = {
      id:            `arch-${seq}`,
      role:          'architect',
      timestamp:     stamp,
      scope,
      moduleCount:   ctxBoxes.length,
      fileCount,
      functionCount: fnCount,
      warningCount:  (ctx.warnings ?? []).length,
      dottedNames,
      solidNames,
      prompt,
      markdown:      result.markdown,
      projectEntryId: archEntryId,
    }

    const srDevMsg = {
      id:              `sr-${seq}`,
      role:            'sr_dev',
      timestamp:       stamp,
      requiresApproval: solid.length > 0,
      dottedNames,
      solidNames,
      projectEntryId: srEntryId,
    }

    setAgentMessages(prev => [...prev, architectMsg, srDevMsg])

    // Backend persists exactly one spec_generated event. Merge it into the
    // timeline tagged via:'execute' + projectEntryId; these locally-enriched
    // fields win over the raw websocket echo (same id) via mergeEvents.
    if (result.event) {
      addLiveEvent(result.event, {
        payload: { ...(result.event.payload ?? {}), via: 'execute' },
        projectEntryId: archEntryId,
      })
    }

    // Kick off the live execution-visualization run (Step 17 — placeholder).
    // Scope arrows that touch any scoped box (repo-level → all), plus any the
    // user explicitly selected. Uses existing arrows only — never invents one.
    const scopedBoxIdList = ctxBoxes.map(b => b.id)
    const scopedBoxIdSet = new Set(scopedBoxIdList)
    const scopedArrowIds = canvasState.arrows
      .filter(a => scopedBoxIdSet.has(a.fromBox) || scopedBoxIdSet.has(a.toBox)
                || (canvasState.selectedArrowIds?.has?.(a.id)))
      .map(a => a.id)
    startSimulatedRun(scopedBoxIdList, scopedArrowIds)

    // Auto-commit OpenFDE's own writes (project.md, etc.) if the repo changed.
    // Step 19 will reuse this same endpoint after real code changes.
    const scopeLabel = isGlobal ? 'repo scope' : ctxBoxes.map(b => b.title).slice(0, 4).join(', ')
    autoCommit(`openfde: execute ${scopeLabel}`, { eventId })
  }

  // ------------------------------------------------------------------ //
  //  Execution backend selection + Claude workflow bridge (Step 19)     //
  // ------------------------------------------------------------------ //
  async function onSetBackend(backend) {
    if (!backendRef.current) return
    const res = await setExecutionBackend(backend)
    if (res?.active) {
      setActiveBackend(res.active)
      setBackends(prev => prev.map(b => ({ ...b, active: b.id === res.active })))
    }
  }

  // Execute via the Claude Code workflow backend: compile the scope into a
  // prepared workflow and surface it in the Agent chat. No code is run.
  async function onExecuteWorkflow(userPrompt = '') {
    if (!backendRef.current) return
    setExecuting(true)
    setPanelMode('Agent')
    const selectedBoxIds   = [...(canvasState.selectedIds ?? [])]
    const selectedArrowIds = [...(canvasState.selectedArrowIds ?? [])]
    const res = await postExecutionRun({ selectedBoxIds, selectedArrowIds, prompt: userPrompt })
    setExecuting(false)
    if (!res?.workflow) return

    const wf = res.workflow
    const stamp = new Date().toISOString()
    const archMsg = {
      id:            `wf-arch-${res.architectEntry?.id || stamp}`,
      role:          'architect',
      timestamp:     stamp,
      workflow:      true,
      backend:       wf.backend,
      workflowId:    wf.workflowId,
      status:        wf.status,
      scope:         wf.scopeSummary,
      moduleCount:   wf.moduleCount,
      fileCount:     wf.fileCount,
      functionCount: wf.functionCount,
      warningCount:  0,
      dottedNames:   wf.editableModules || [],
      solidNames:    wf.protectedModules || [],
      prompt:        (userPrompt ?? '').trim(),
      markdown:      wf.script,
      projectEntryId: res.architectEntry?.id,
    }
    const srMsg = {
      id:               `wf-sr-${res.srDevEntry?.id || stamp}`,
      role:             'sr_dev',
      timestamp:        stamp,
      workflowPrepared: true,
      backend:          wf.backend,
      status:           wf.status,
      requiresApproval: (wf.protectedModules || []).length > 0,
      dottedNames:      wf.editableModules || [],
      solidNames:       wf.protectedModules || [],
      projectEntryId:   res.srDevEntry?.id,
    }
    setAgentMessages(prev => [...prev, archMsg, srMsg])
    if (res.event) addLiveEvent(res.event)
  }

  // Native Senior Dev agent run (Step 22a): real model call → in-scope edits →
  // gated reconciliation. Surfaces a result message and refreshes git/box specs.
  async function onExecuteAgent(userPrompt = '') {
    if (!backendRef.current) return
    setExecuting(true)
    setPanelMode('Agent')
    const selectedBoxIds   = [...(canvasState.selectedIds ?? [])]
    const selectedArrowIds = [...(canvasState.selectedArrowIds ?? [])]
    startSimulatedRun(architectureModuleIds(selectedBoxIds), selectedArrowIds, { hold: true })
    const stamp = new Date().toISOString()
    setAgentMessages(prev => [...prev, {
      id: `agent-start-${stamp}`, role: 'sr_dev', timestamp: stamp,
      nativeAgent: true,
      summary: 'Native agent running — Senior Dev editing in-scope files…',
    }])
    const res = await postAgentRun({ selectedBoxIds, selectedArrowIds, prompt: userPrompt })
    setExecuting(false)
    if (!res || res.ok === false) {
      finishRun('failed')
      const reason = (res && res.error) ||
        'Senior Dev must be in API mode — either Anthropic (key + model) or the keyless Echo provider. Open Agent Settings.'
      setAgentMessages(prev => [...prev, {
        id: `agent-err-${Date.now()}`, role: 'result', timestamp: new Date().toISOString(),
        status: 'failed',
        reportSummary: reason,
      }])
      return
    }
    finishRun(res.status === 'failed' ? 'failed' : 'passed')
    const done = new Date().toISOString()
    setAgentMessages(prev => [...prev, {
      id: `agent-result-${res.runId || done}`, role: 'result', timestamp: done,
      status: res.status,
      reportSummary: res.reportSummary,
      verificationResult: res.verificationResult,
      testsSummary: res.testsSummary,
      committed: res.committed,
      commitSha: res.commitSha,
      approval: res.approval || null,
      writes: res.writes,
    }])
    ;(res.events || []).forEach(addLiveEvent)
    if (res.committed) refreshGitTimeline()
    if (res.approval) setApprovals(prev => [res.approval, ...prev])
    getBoxSpecs().then(s => { if (s && typeof s === 'object') setBoxSpecs(s) })
  }

  // Agent Council run (Step 29 Slice 3): Architect → Sr Dev → Verifier, streamed
  // into the existing story surfaces. Stage timeline events arrive live over the
  // WebSocket; here we surface the concise stage story + a fresh result for Review.
  async function onExecuteCouncil(userPrompt = '') {
    if (!backendRef.current) return
    setExecuting(true)
    setPanelMode('Agent')
    const selectedBoxIds   = [...(canvasState.selectedIds ?? [])]
    const selectedArrowIds = [...(canvasState.selectedArrowIds ?? [])]
    await startSimulatedRun(architectureModuleIds(selectedBoxIds), selectedArrowIds, { hold: true, live: true })
    const stamp = new Date().toISOString()
    setAgentMessages(prev => [...prev, {
      id: `council-start-${stamp}`, role: 'sr_dev', timestamp: stamp, nativeAgent: true,
      summary: 'Agent Council running — Architect → Senior Dev → Verifier…',
    }])
    const res = await postCouncilRun({ selectedBoxIds, selectedArrowIds, prompt: userPrompt })
    setExecuting(false)
    if (!res || res.ok === false) {
      finishRun('failed')
      const reason = (res && res.error) ||
        'Agent Council needs Senior Dev in API mode — Anthropic (key + model) or the keyless Echo provider. Open Agent Settings.'
      setAgentMessages(prev => [...prev, {
        id: `council-err-${Date.now()}`, role: 'result', timestamp: new Date().toISOString(),
        status: 'failed', reportSummary: reason,
      }])
      return
    }
    const cancelled = res.status === 'cancelled'
    finishRun(cancelled ? 'cancelled' : res.status === 'failed' ? 'failed' : 'passed')
    const done = new Date().toISOString()
    // Each council stage → a concise story message (Architect/Sr Dev/Verifier).
    const stageMsgs = (res.stages || []).map((s, i) => ({
      id: `council-stage-${res.runId}-${i}`, role: s.role, timestamp: done,
      councilStage: true, status: s.status, attempt: s.attempt, summary: s.summary,
      provider: s.provider || null,
    }))
    // Fresh outcome (drives Review accuracy — this run's commit/approval, not stale).
    const resultMsg = {
      id: `council-result-${res.runId || done}`, role: 'result', timestamp: done,
      status: res.status,
      reportSummary: cancelled ? 'Cancelled by user — nothing was committed.'
        : (res.verifier && res.verifier.summary) || `Council ${res.status}.`,
      cancelled,
      committed: !!(res.commit && res.commit.committed),
      commitSha: res.commit ? res.commit.sha : null,
      approval: res.approval || null,
      fromRun: res.runId,
    }
    setAgentMessages(prev => [...prev, ...stageMsgs, resultMsg])
    if (res.commit && res.commit.committed) {
      refreshGitTimeline()
      // Pre-load this run's diff so Work Review can show the change inline (and
      // the Diff tab is ready) without a detour through the Timeline.
      const sha = res.commit.sha
      getGitDiff(sha).then(data => setCommitDiff({ loading: false, sha, data: data || null }))
    }
    if (res.approval) setApprovals(prev => [res.approval, ...prev])
    getBoxSpecs().then(s => { if (s && typeof s === 'object') setBoxSpecs(s) })
  }

  // Stop button (Step 33): cancel the in-flight council run. The council POST
  // (still awaiting in onExecuteCouncil) returns status:cancelled and finishes
  // the visual; nothing commits.
  async function onStopRun() {
    const id = run?.councilRunId
    if (!id) return
    setAgentMessages(prev => [...prev, {
      id: `council-stopping-${Date.now()}`, role: 'sr_dev', timestamp: new Date().toISOString(),
      nativeAgent: true, summary: 'Stopping… cancelling the run.',
    }])
    await cancelCouncilRun(id)
  }

  // Submit a Claude workflow result (pasted JSON) for reconciliation.
  // Returns an error string on failure, or null on success.
  async function onSubmitWorkflowResult(workflowId, resultObj) {
    if (!backendRef.current) return 'Backend unavailable'
    const res = await postWorkflowResult(workflowId, resultObj)
    if (!res?.ok) return res?.error || 'Result rejected (check the JSON shape).'

    // Outcome message in the Agent chat.
    const stamp = new Date().toISOString()
    setAgentMessages(prev => [...prev, {
      id: `wf-result-${workflowId}-${stamp}`,
      role: 'result',
      timestamp: stamp,
      status: res.status,
      reportSummary: res.reportSummary,
      verificationResult: res.verificationResult,
      testsSummary: res.testsSummary,
      committed: res.committed,
      commitSha: res.commitSha,
      approval: res.approval || null,
    }])

    ;(res.events || []).forEach(addLiveEvent)
    if (res.committed) refreshGitTimeline()
    if (res.approval) setApprovals(prev => [res.approval, ...prev])
    // Refresh box specs (workflow result attached provenance)
    getBoxSpecs().then(s => { if (s && typeof s === 'object') setBoxSpecs(s) })
    return null
  }

  async function onResolveApproval(approvalId, decision) {
    if (!backendRef.current) return
    const fn = decision === 'approved' ? approveApproval : rejectApproval
    const res = await fn(approvalId)
    if (!res?.approval) return
    setApprovals(prev => prev.map(a => a.approvalId === approvalId ? res.approval : a))
    if (res.event) addLiveEvent(res.event)
  }

  // ------------------------------------------------------------------ //
  //  Git timeline + auto-commit + diff inspection (Step 18)             //
  // ------------------------------------------------------------------ //
  async function refreshGitTimeline() {
    const commits = await getGitTimeline()
    if (Array.isArray(commits)) setGitCommits(commits)
  }

  async function autoCommit(summary, extra = {}) {
    if (!backendRef.current) return
    const res = await postGitCommit({ summary, ...extra })
    if (res?.committed) {
      if (res.event) addLiveEvent(res.event)
      refreshGitTimeline()
    }
  }

  // Open a commit in the right-panel Diff inspector.
  async function onSelectCommit(sha) {
    if (!sha || !backendRef.current) return
    setCommitDiff({ loading: true, sha, data: null })
    openTechnicalMode('Diff')   // reveal the diff (Work is now default)
    const data = await getGitDiff(sha)
    setCommitDiff({ loading: false, sha, data: data || null })
  }

  // Generate + commit REPORT.md.
  async function onGenerateReport() {
    if (!backendRef.current) return
    const res = await postReport()
    if (!res) return
    if (res.event) addLiveEvent(res.event)
    if (res.commitEvent) addLiveEvent(res.commitEvent)
    refreshGitTimeline()
  }

  // ------------------------------------------------------------------ //
  //  Live execution run — visualization only (Step 17)                  //
  // ------------------------------------------------------------------ //
  // Drives a real run record + visual lifecycle (planning → running →
  // passed). It does NOT modify repo files — Senior Dev execution is Step 19.
  // Insert a backend event into the live timeline. The locally-enriched copy
  // wins over any raw websocket copy of the same id (it is merged first).
  function addLiveEvent(evt, extra = {}) {
    if (!evt?.id) return
    const enriched = { ...evt, live: true, ...extra }
    setDesignEvents(prev => mergeEvents([enriched], prev))
  }
  const stateMap = (ids, status) => Object.fromEntries(ids.map(id => [id, status]))
  function appendTrace(trace, ids, evt) {
    const next = { ...trace }
    ids.forEach(id => { next[id] = [...(next[id] || []), { ...evt, timestamp: evt.timestamp || new Date().toISOString() }] })
    return next
  }
  // Map a selected ArchGraph entity to its stable *canvas* node id so run
  // states/trace key off the same id the canvas renders geometry under.
  const archCanvasId = (kind, data) => {
    if (!data) return null
    if (kind === 'file')     return `box:file:${data.path}`
    if (kind === 'function') return `box:function:${data.path}:${data.name}`
    return data.id   // module boxes are already canvas ids (box:module:...)
  }

  function architectureModuleIds(ids = []) {
    const wanted = new Set(ids)
    return canvasState.boxes
      .filter(b => wanted.has(b.id))
      .map(b => b.id)
  }

  async function startSimulatedRun(scopedBoxIds, scopedArrowIds, opts = {}) {
    if (!backendRef.current || scopedBoxIds.length === 0) return
    const res = await postRunStart({ scopedBoxIds, scopedArrowIds, scopedFileIds: [], scopedFunctionIds: [] })
    const runId = res?.run?.runId
    if (!runId) return
    runRef.current = runId
    if (res?.event) addLiveEvent(res.event)

    setRun({
      runId, status: opts.live ? 'running' : 'planning', scopedBoxIds, scopedArrowIds,
      nodeStates: stateMap(scopedBoxIds, opts.live ? 'running' : 'planning'), edgeStates: {},
      trace: appendTrace({}, scopedBoxIds, { type: 'node_planning', status: 'planning' }),
      failures: {}, plannedFiles: [], written: [], startedAt: Date.now(),
    })

    // Live runs let agent_plan / agent_progress events drive node states (and
    // finishRun settles them), so skip the coarse simulated timers that would
    // otherwise clobber the file-level activity glow.
    if (opts.live) return

    // → running
    setTimeout(() => {
      if (runRef.current !== runId) return
      postRunEvent(runId, { type: 'run_running' })
      setRun(r => (r && r.runId === runId) ? {
        ...r, status: 'running',
        nodeStates: stateMap(scopedBoxIds, 'running'),
        edgeStates: Object.fromEntries(scopedArrowIds.map(id => [id, 'active'])),
        trace: appendTrace(r.trace, scopedBoxIds, { type: 'node_running', status: 'running' }),
      } : r)
    }, 700)

    // → passed (only for fast/simulated runs). Real runs (opts.hold) keep
    // pulsing at 'running' until the actual result lands; the caller then calls
    // finishRun(...) so the canvas stays alive for the whole 40–50s loop.
    if (!opts.hold) {
      setTimeout(async () => {
        if (runRef.current !== runId) return
        const ev = await postRunEvent(runId, { type: 'run_passed' })
        if (ev?.timelineEvent) addLiveEvent(ev.timelineEvent)
        setRun(r => (r && r.runId === runId) ? {
          ...r, status: 'passed', endedAt: new Date().toISOString(),
          nodeStates: stateMap(scopedBoxIds, 'passed'), edgeStates: {},
          trace: appendTrace(r.trace, scopedBoxIds, { type: 'node_passed', status: 'passed' }),
        } : r)
        // fade the visual back to idle (keeps trace data for the Inspector)
        setTimeout(() => {
          setRun(r => (r && r.runId === runId && r.status === 'passed')
            ? { ...r, status: 'idle', nodeStates: {}, edgeStates: {} } : r)
        }, 2600)
      }, 2100)
    }
  }

  // Finish a held run (real agent/council) once the actual result lands: settle
  // the pulse to passed/failed, then fade to idle. Acts on the current run.
  function finishRun(status) {
    const runId = runRef.current
    if (!runId) return
    const ring = status === 'failed' ? 'failed' : status === 'cancelled' ? 'cancelled' : 'passed'
    if (status !== 'cancelled') {
      postRunEvent(runId, { type: ring === 'passed' ? 'run_passed' : 'run_failed' })
        .then(ev => { if (ev?.timelineEvent) addLiveEvent(ev.timelineEvent) })
    }
    setRun(r => {
      if (!(r && r.runId === runId)) return r
      // Settle every node we touched — scoped modules AND any file-level activity
      // nodes from the live stream — to the final ring, then fade.
      const ns = {}
      Object.keys(r.nodeStates || {}).forEach(k => { ns[k] = ring })
      ;(r.scopedBoxIds || []).forEach(id => { ns[id] = ring })
      return {
        ...r, status: ring, endedAt: new Date().toISOString(),
        nodeStates: ns, edgeStates: {},
        trace: appendTrace(r.trace, r.scopedBoxIds, { type: `node_${ring}`, status: ring }),
      }
    })
    setTimeout(() => {
      setRun(r => (r && r.runId === runId && r.status === ring)
        ? { ...r, status: 'idle', nodeStates: {}, edgeStates: {} } : r)
    }, status === 'cancelled' ? 1500 : 2600)
  }

  // Dev-only: inject a failure trace so red failure states can be verified
  // before real Senior Dev / Verifier execution exists.
  async function injectFailureTrace() {
    if (!backendRef.current) return
    let nodeId = null, edgeId = null, archNodeId = null
    if (archSel?.data) {
      nodeId = archCanvasId(archSel.kind, archSel.data)   // canvas id (box:function:… / box:file:…)
      archNodeId = archSel.data.id || null                // raw ArchGraph id, for traceability
    }
    else if (selectedArrows.length) edgeId = selectedArrows[0].id
    else if (selectedBoxes.length) nodeId = selectedBoxes[0].id
    else if (run?.scopedBoxIds?.length) nodeId = run.scopedBoxIds[0]
    else if (canvasState.boxes.length) nodeId = canvasState.boxes[0].id
    if (!nodeId && !edgeId) return

    let runId = runRef.current
    const scopedBoxIds = run?.scopedBoxIds?.length
      ? run.scopedBoxIds
      : (nodeId && nodeId.startsWith('box:module:') ? [nodeId] : selectedBoxes.map(b => b.id))
    if (!runId) {
      const res = await postRunStart({ scopedBoxIds, scopedArrowIds: [], scopedFileIds: [], scopedFunctionIds: [] })
      runId = res?.run?.runId
      if (res?.event) addLiveEvent(res.event)
    }
    if (!runId) return
    runRef.current = runId

    const failEvt = {
      type: edgeId ? 'edge_failed' : 'node_failed',
      nodeId: nodeId || undefined,
      edgeId: edgeId || undefined,
      archNodeId: archNodeId || undefined,
      status: 'failed',
      error: 'TypeError: cannot read property "id" of undefined (simulated dev failure)',
      input: { request: { authorization: 'Bearer sk-DEMO-should-be-redacted', userId: 42 }, query: 'SELECT * FROM users WHERE id = ?' },
      output: { rowsFetched: 0, stack: 'at handler (server.py:312)' },
    }
    const ev = await postRunEvent(runId, failEvt)
    const stored = ev?.event || failEvt
    const rf = await postRunEvent(runId, { type: 'run_failed', errorSummary: 'simulated failure' })
    if (rf?.timelineEvent) addLiveEvent(rf.timelineEvent)

    const tid = edgeId || nodeId
    setRun(r => {
      const base = (r && r.runId === runId)
        ? r
        : { runId, status: 'failed', scopedBoxIds, scopedArrowIds: [], nodeStates: {}, edgeStates: {}, trace: {}, failures: {} }
      return {
        ...base, status: 'failed',
        nodeStates: nodeId ? { ...base.nodeStates, [nodeId]: 'failed' } : base.nodeStates,
        edgeStates: edgeId ? { ...base.edgeStates, [edgeId]: 'failed' } : base.edgeStates,
        trace: appendTrace(base.trace, [tid], { type: failEvt.type, status: 'failed', error: stored.error, input: stored.input, output: stored.output, timestamp: stored.timestamp }),
        failures: { ...base.failures, [tid]: { errorSummary: stored.error, inputSummary: stored.input, outputSummary: stored.output, timestamp: stored.timestamp } },
      }
    })
    setPanelMode('Inspector')
  }

  // Called from OpenPM for task_moved events
  function onTaskEvent(evt) {
    addDesignEvent(evt)
  }

  // ------------------------------------------------------------------ //
  //  Theme                                                               //
  // ------------------------------------------------------------------ //
  useEffect(() => {
    document.documentElement.className = theme === 'light' ? 'light' : ''
    localStorage.setItem('openfde-theme', theme)
  }, [theme])

  // ------------------------------------------------------------------ //
  //  Global ⌘K / Ctrl+K shortcut                                        //
  // ------------------------------------------------------------------ //
  useEffect(() => {
    function onKeyDown(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        setPaletteOpen(p => !p)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const toggleTheme = () => setTheme(t => t === 'dark' ? 'light' : 'dark')

  const hasDottedSelected = canvasState.boxes.some(
    b => canvasState.selectedIds.has(b.id) && b.type === 'dotted'
  )

  const selectedBoxes  = canvasState.boxes.filter(b => canvasState.selectedIds.has(b.id))
  const selectedArrows = canvasState.arrows.filter(a => canvasState.selectedArrowIds?.has(a.id))

  // Module type per moduleId — lets drilldown file/function views inherit
  // the parent module's permission (dotted → editable, solid → protected).
  const moduleTypeById = {}
  canvasState.boxes.forEach(b => { if (b.moduleId) moduleTypeById[b.moduleId] = b.type })

  // When a nested file/function is inspected, the Inspector shows that entity;
  // otherwise it reflects the canvas box / arrow selection (Execute scope).
  const selectionContext = archSel
    ? {
        boxes: [], arrows: [], allBoxes: canvasState.boxes, files: [],
        mode: archSel.kind,                 // 'file' | 'function'
        entity: archSel.data,
        moduleType: moduleTypeById[archSel.data?.moduleId] || 'dotted',
      }
    : {
        boxes: selectedBoxes,
        arrows: selectedArrows,
        allBoxes: canvasState.boxes,
        files: [],
        mode: selectedBoxes.length === 0 && selectedArrows.length === 0 ? 'none'
            : selectedArrows.length > 0  && selectedBoxes.length === 0 ? 'arrow'
            : selectedBoxes.length === 1 && selectedArrows.length === 0 ? 'box'
            : selectedBoxes.length > 1   && selectedArrows.length === 0 ? 'multi-box'
            : 'mixed',
      }

  // Moment engine (FLOW.md, Step 28 Slice 2): the Work moment is driven by the
  // current work unit's status — not a stale global spec — so clearing intent
  // never leaves the panel stuck in Change.
  const runActive = run?.status === 'planning' || run?.status === 'running'
  const currentMoment = deriveMoment({
    // A live run keeps us in Execute even if the work unit already moved to review.
    executing: executing || runActive || workUnit?.status === 'execute',
    reviewSignal: workUnit?.status === 'review',
    changeSignal: workUnit?.status === 'change',
    hasSelection: selectionContext.mode !== 'none',
  })

  // Work-unit lifecycle (Step 28 Slice 2). Typing intent opens a 'change' unit;
  // clearing it (while still in change) closes it → back to Understand/Orient.
  function handleWorkIntent(text) {
    setWorkIntent(text)
    if (text.trim()) {
      setWorkUnit(u => (u && u.status !== 'change') ? u : { intent: text, status: 'change' })
    } else {
      setWorkUnit(u => (u && u.status === 'change') ? null : u)
    }
  }
  // Execute from the Work panel: advance the unit change → execute → review,
  // reusing the existing Execute flow (no new capability).
  async function onWorkExecute() {
    const text = workIntent.trim()
    setWorkUnit({ intent: text, status: 'execute' })
    try { if (backendAvailable) await onExecute(text) } finally {
      setWorkUnit(u => (u ? { ...u, status: 'review' } : { status: 'review' }))
    }
  }
  function onWorkReset() { setWorkUnit(null); setWorkIntent('') }

  function goHome() {
    collapseAll()
    canvasDispatch({ type: 'CLEAR_SELECTION' })
    setFlowMode('focused')
    setStory(null)
    setActiveView('whiteboard')
    setRightView('work')
  }

  // Secondary "Technical" escape: open the old tabbed RightPanel at `mode`.
  function openTechnicalMode(mode) { setRightView('technical'); if (mode) setPanelMode(mode) }
  // Command-palette routing: Inspector maps to Work's Understand (box already
  // selected), so stay in Work; every other tab opens the Technical panel.
  function paletteSetPanelMode(mode) {
    if (mode === 'Inspector') { setPanelMode(mode); return }
    openTechnicalMode(mode)
  }

  return (
    <>
      <div className={`app${theme === 'light' ? ' light' : ''}`}>
        <Toolbar
          activeTool={activeTool}
          setActiveTool={setActiveTool}
          activeView={activeView}
          setActiveView={setActiveView}
          theme={theme}
          toggleTheme={toggleTheme}
          hasDottedSelected={hasDottedSelected}
          onLockSelected={() => canvasDispatch({ type: 'FREEZE_SELECTED' })}
          onOpenCommandPalette={() => setPaletteOpen(true)}
          onHome={goHome}
        />
        <div className="panels">
          <aside className={`panel-left${leftOpen ? '' : ' collapsed'}`}>
            <button className="panel-collapse-btn left" onClick={() => setLeftOpen(o => !o)}
              title={leftOpen ? 'Collapse file tree' : 'Expand file tree'}>
              {leftOpen ? '‹' : '›'}
            </button>
            {leftOpen && <FileTree />}
          </aside>
          <main className="panel-middle">
            <Whiteboard
              activeTool={activeTool}
              setActiveTool={setActiveTool}
              activeView={activeView}
              setActiveView={setActiveView}
              canvasState={canvasState}
              canvasDispatch={canvasDispatch}
              onLoadSelfMap={() => canvasDispatch({ type: 'LOAD_SELF_MAP' })}
              onGenerateFromRepo={backendAvailable ? onGenerateFromRepo : null}
              onExecute={backendAvailable ? onWorkExecute : null}
              executing={executing}
              archGraph={archGraph}
              expandedIds={expandedIds}
              onToggleExpand={toggleExpand}
              onSelectArchEntity={selectArchEntity}
              archSel={archSel}
              onExpandModule={expandModule}
              flowMode={flowMode}
              story={story}
              runNodeStates={run?.nodeStates}
              runEdgeStates={run?.edgeStates}
              gitCommits={gitCommits}
              onSelectCommit={onSelectCommit}
              tasks={tasks}
              pmDispatch={pmDispatch}
              designEvents={designEvents}
              onTaskEvent={onTaskEvent}
              selectedTaskId={selectedTaskId}
              setSelectedTaskId={setSelectedTaskId}
              setPanelMode={openTechnicalMode}
            />
          </main>
          <aside className={`panel-right${rightOpen ? '' : ' collapsed'}`}>
            <button className="panel-collapse-btn right" onClick={() => setRightOpen(o => !o)}
              title={rightOpen ? 'Collapse panel' : 'Expand panel'}>
              {rightOpen ? '›' : '‹'}
            </button>
            {rightOpen && rightView === 'work' && (
              <WorkPanel
                moment={currentMoment}
                selectionContext={selectionContext}
                story={flowMode === 'story' ? story : null}
                specMarkdown={specMarkdown}
                commitDiff={commitDiff}
                agentMessages={agentMessages}
                approvals={approvals}
                onExecute={backendAvailable ? onWorkExecute : null}
                onExplain={backendAvailable ? onExplain : null}
                onOpenDiff={onSelectCommit}
                onReset={onWorkReset}
                intent={workIntent}
                onIntentChange={handleWorkIntent}
                run={run}
                onStop={onStopRun}
              />
            )}
            {rightOpen && rightView === 'work' && (
              <div className="work-escapes">
                <button onClick={() => openTechnicalMode('Ledger')}>History</button>
                <button onClick={() => openTechnicalMode('Inspector')}>Technical</button>
              </div>
            )}
            {rightOpen && rightView === 'technical' && (
              <div className="tech-backbar">
                <button className="tech-back" onClick={() => setRightView('work')}>← Work</button>
                <span className="tech-title">Technical</span>
              </div>
            )}
            {rightOpen && rightView === 'technical' && (
              <div className="tech-canvas-controls">
                <span className="tcc-label">Canvas flow</span>
                <div className="flow-mode-toggle" role="group" title="Flow display mode">
                  {['story', 'focused', 'all'].map(fm => (
                    <button key={fm} className={`flow-mode-btn${flowMode === fm ? ' active' : ''}`}
                      onClick={() => setFlowMode(fm)}>
                      {fm === 'all' ? 'All' : fm[0].toUpperCase() + fm.slice(1)}
                    </button>
                  ))}
                </div>
                <button className="tcc-btn" onClick={expandAll} disabled={!archGraph} title="Expand every module and file">
                  Expand all
                </button>
                <button className="tcc-btn" onClick={collapseAll} title="Collapse all expanded boxes">
                  Collapse all
                </button>
              </div>
            )}
            {rightOpen && rightView === 'technical' && <RightPanel
              selectionContext={selectionContext}
              canvasState={canvasState}
              dispatch={canvasDispatch}
              panelMode={panelMode}
              setPanelMode={setPanelMode}
              setActiveView={setActiveView}
              selectedTask={tasks.find(t => t.id === selectedTaskId) || null}
              pmDispatch={pmDispatch}
              boxSpecs={boxSpecs}
              specMarkdown={specMarkdown}
              specLoading={specLoading}
              onGenerateSpec={backendAvailable ? onGenerateSpec : null}
              agentMessages={agentMessages}
              onEnterModule={(box) => { if (box?.moduleId) toggleExpand(box.id, 'module') }}
              run={run}
              commitDiff={commitDiff}
              onSubmitWorkflowResult={onSubmitWorkflowResult}
              approvals={approvals}
              onResolveApproval={onResolveApproval}
              agentSettings={agentSettings}
              onOpenAgentSettings={() => setAgentSettingsOpen(true)}
              onExplain={backendAvailable ? onExplain : null}
              story={flowMode === 'story' ? story : null}
            />}
          </aside>
        </div>
      </div>
      <CommandPalette
        isOpen={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        canvasState={canvasState}
        dispatch={canvasDispatch}
        theme={theme}
        toggleTheme={toggleTheme}
        activeView={activeView}
        setActiveView={setActiveView}
        setPanelMode={paletteSetPanelMode}
        pmDispatch={pmDispatch}
        backendAvailable={backendAvailable}
        onGenerateFromRepo={onGenerateFromRepo}
        onGenerateSpec={onGenerateSpec}
        onExecute={onExecute}
        onInjectFailure={injectFailureTrace}
        onGenerateReport={onGenerateReport}
        backends={backends}
        activeBackend={activeBackend}
        onSetBackend={onSetBackend}
        onOpenAgentSettings={() => { setPaletteOpen(false); setAgentSettingsOpen(true) }}
      />
      {agentSettingsOpen && agentSettings && agentOptions && (
        <AgentSettings
          settings={agentSettings}
          options={agentOptions}
          onClose={() => setAgentSettingsOpen(false)}
          onSettingsChange={setAgentSettings}
        />
      )}
    </>
  )
}
