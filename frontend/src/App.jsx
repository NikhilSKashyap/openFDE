import { useState, useEffect, useRef, useCallback } from 'react'
import './App.css'
import Toolbar from './components/Toolbar/Toolbar'
import FileTree from './components/FileTree/FileTree'
import Whiteboard from './components/Whiteboard/Whiteboard'
import RightPanel from './components/RightPanel/RightPanel'
import WorkPanel from './components/WorkPanel/WorkPanel'
import FocusLens from './components/Focus/FocusLens'
import { deriveMoment } from './productFlow/deriveMoment'
import CommandPalette from './components/CommandPalette/CommandPalette'
import AgentSettings from './components/AgentSettings/AgentSettings'
import Plugins from './components/Plugins/Plugins'
import SemanticGraphCard from './components/SemanticGraph/SemanticGraphCard'
import ConceptPanel from './components/SemanticGraph/ConceptPanel'
import FunctionPatch, { TrailEditor } from './components/Whiteboard/FunctionPatch'
import RaiseIssue from './components/Feedback/RaiseIssue'
import { pickPrimaryFn } from './lib/flowResolve'
import { watchActivityTargets } from './lib/watchTarget'
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
  getSession,
  getBoot,
  getBootCanvas,
  postAgentRun,
  postCouncilRun,
  cancelCouncilRun,
  postStory,
  getCommitImpact,
  getWorktreeImpact,
  reassimilateReview,
  getReviewEpisodes,
  getRailBoot,
  getReviewEpisodesFull,
  landEpisode,
  askConcept,
  getConceptCards,
  saveConceptCard, hatchFlow,
  getPlugins, getWebxrSummary, postFocusNeighborhood } from './api/backend'
import { connectWS, closeWS } from './api/ws'
import { badgeMapFromSummary } from './lib/webxrBadges'

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

// Split a multi-file unified diff into { path: sectionText } keyed by the b/ path,
// so each file's hunks can be routed to that file's editor.
function splitDiffByFile(text) {
  const out = {}
  let cur = null, buf = []
  const flush = () => { if (cur) out[cur] = buf.join('\n') }
  for (const ln of (text || '').split('\n')) {
    const m = ln.match(/^diff --git a\/.+? b\/(.+)$/)
    if (m) { flush(); cur = m[1]; buf = [ln] }
    else if (cur) buf.push(ln)
  }
  flush()
  return out
}

// The new-file start line of a diff section's first hunk (`@@ -a,b +c,d @@` → c),
// used to open the editor at the function the change landed in.
function firstHunkNewLine(section) {
  const m = (section || '').match(/^@@ -\d+(?:,\d+)? \+(\d+)/m)
  return m ? parseInt(m[1], 10) : null
}

export default function App() {
  const [theme, setTheme] = useState(() => localStorage.getItem('openfde-theme') || 'dark')
  const [activeTool, setActiveTool] = useState('select')
  const [activeView, setActiveView] = useState('whiteboard')
  const [canvasState, _rawCanvasDispatch] = useCanvasState()
  // Live mirror of boxes so WS handlers (stable closures) can map file→module.
  const boxesRef = useRef(canvasState.boxes)
  // Live mirror of the ArchGraph so the file_activity WS handler can tell whether it's loaded
  // (a module can only EXPAND to show files/functions once the arch is present).
  const archGraphRef = useRef(null)
  const [tasks, pmDispatch] = usePMState()
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [raiseOpen, setRaiseOpen] = useState(false)   // top-bar "Raise OpenFDE issue"
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
  // WebXR canvas badges (Plugin Registry payoff): path → { kind, label } for the
  // files the WebXR pack flagged (XR entrypoints / 3D assets). null until a
  // detected/enabled WebXR pack resolves; stays null on every other repo.
  const [webxrBadges, setWebxrBadges] = useState(null)
  // The repair hatch — function-scoped fix surface, summoned ONLY by a failure
  // receipt's "Show →" ({file, line, func, funcName, test, start, end} | null).
  const [hatch, setHatch] = useState(null)
  // The repair hatch stays pinned to the FAILING function. Clicking another node
  // in the failure trail opens it in its OWN floating editor (so test + product
  // functions read side-by-side) — never replacing the hatch. Each entry:
  // {id, file, funcName, start, end, line, minimized, seq}. seq = z/raise order.
  const [flowEditors, setFlowEditors] = useState([])
  const editorSeq = useRef(60)
  const [hatchZ, setHatchZ] = useState(60)         // hatch window z (raised on "Open in editor")
  const [flowLens, setFlowLens] = useState(null)   // {artifact, busy} — failure-flow lens
  const [repairPhase, setRepairPhase] = useState(null) // failing | fixing | fixed
  // L2-B Focus lens: a small focused subgraph overlay (seeds + neighbors) over the full canvas.
  // {busy} | {result, seedLabel, hops} | {error}. Null = no focus (full canvas). Never mutates canvas.
  const [focusLens, setFocusLens] = useState(null)
  const flowExpandRef = useRef('')                 // last fingerprint auto-expanded (once per failure)
  // ── Execution run / live trace (Step 17) ─────────────────────────────────
  // run: { runId, status, scopedBoxIds, scopedArrowIds, nodeStates, edgeStates,
  //        trace: {id:[events]}, failures: {id:{...}} } | null
  const [run, setRun] = useState(null)
  // Ambient "watch any agent" activity: boxId -> last-touched timestamp. Boxes
  // HOLD their glow while the agent keeps working and settle only after it goes
  // quiet (no edits anywhere for SETTLE_MS). watchTick refreshes active→trail
  // tiers over time without new events.
  const [watchActivity, setWatchActivity] = useState({})  // fileNodeId -> last-touch ts
  const [watchTiers, setWatchTiers] = useState({})        // fileNodeId -> 'active'|'trail'
  const [watchFocus, setWatchFocus] = useState(null)      // { file, fnName, moduleId, ts } → center camera
  const watchAutoExpandedRef = useRef(new Set())          // modules WE auto-expanded
  const runRef = useRef(null)
  // ── Git timeline + diff inspection (Step 18) ─────────────────────────────
  const [gitCommits, setGitCommits] = useState([])
  const [commitDiff, setCommitDiff] = useState(null)   // { loading, sha, data }
  // Review Delta (Land·Watch·Review): uncommitted worktree as a calm "Review
  // changes" affordance. { dirty, count, signature } — null until first probe.
  const [worktree, setWorktree] = useState(null)
  const worktreeSigRef = useRef(null)                  // last signature we fetched/spotlit
  // Prompt Story Rail (OpenFDE owns commits): prompt episodes + the "Outside
  // OpenFDE" bucket of commits not linked to a prompt.
  const [episodes, setEpisodes] = useState([])
  const [outsideBucket, setOutsideBucket] = useState(null)
  // Bumped on a story_updated broadcast (a Land / background rebuild) so Story re-hydrates the
  // full graph even when episodes.length is unchanged (a land adds commits, not episodes).
  const [storyNonce, setStoryNonce] = useState(0)
  const [landing, setLanding] = useState(false)
  // Why the last Land was a no-op ("no dirty files attributed…") — shown on the card.
  const [landNote, setLandNote] = useState(null)
  // Live follow — when ON, the canvas camera centers the file an agent is editing
  // and follows as it moves on. Watch glow is ALWAYS on; this only controls the
  // camera. Persisted in localStorage (theme pattern); default ON.
  const [liveFollow, setLiveFollow] = useState(() => {
    try { return localStorage.getItem('openfde-live-follow') !== 'off' } catch { return true }
  })
  useEffect(() => {
    try { localStorage.setItem('openfde-live-follow', liveFollow ? 'on' : 'off') } catch { /* ignore */ }
  }, [liveFollow])
  // Incremental Re-assimilation: changed paths from Watch, debounced into a single
  // understanding-refresh (ArchGraph + semantic graph) after edits settle.
  const pendingReassimRef = useRef(new Set())
  const reassimTimerRef = useRef(null)
  const executingRef = useRef(false)                   // mirror of `executing` for timers
  // ── Execution backend (Step 19) ──────────────────────────────────────────
  const [backends, setBackends] = useState([])
  const [activeBackend, setActiveBackend] = useState('openfde-native')
  // ── Workflow result intake + approvals (Step 20) ─────────────────────────
  const [approvals, setApprovals] = useState([])
  // ── Watched-repo identity (authoritative, from /api/session) ──────────────
  const [session, setSession] = useState(null)
  const [boot, setBoot] = useState(null)        // restore-confidence: counts from .openfde on boot
  const [bootFileTree, setBootFileTree] = useState(null)  // cached Explorer tree for instant first paint
  const [canvasHydrated, setCanvasHydrated] = useState(false)  // gate: non-canvas hydration waits for first paint
  // ── Agent role settings (Step 21) ────────────────────────────────────────
  const [agentSettings, setAgentSettings] = useState(null)
  const [agentOptions, setAgentOptions]   = useState(null)
  const [agentSettingsOpen, setAgentSettingsOpen] = useState(false)
  const [pluginsOpen, setPluginsOpen] = useState(false)
  const [semanticGraphOpen, setSemanticGraphOpen] = useState(false)
  // Canvas spotlight — a concept (tether) or a commit to light up on the canvas.
  // { kind:'tether'|'commit', label, count, files, amberFiles?, concepts?, summary? }
  const [canvasSpotlight, setCanvasSpotlight] = useState(null)

  // Concept cards — short saved notes about a concept/commit (persisted in .openfde).
  const [conceptCards, setConceptCards] = useState([])
  const reloadConceptCards = useCallback(async () => {
    const res = await getConceptCards()
    if (res?.ok) setConceptCards(res.cards || [])
  }, [])
  useEffect(() => {
    let alive = true
    ;(async () => {
      const res = await getConceptCards()
      if (alive && res?.ok) setConceptCards(res.cards || [])
    })()
    return () => { alive = false }
  }, [])

  // Probe the worktree non-destructively; only re-render when the porcelain
  // signature changed (cheap dirty-state key) so typing never thrashes the chip.
  // Plain function — it closes over only stable imports + state setters.
  // Single-flight de-dup: collapse overlapping calls to the same endpoint (focus + interval +
  // idle probe, WS bursts) into ONE in-flight request, so a slow poll never stacks up behind
  // itself and we never have N copies of /api/review/episodes racing.
  const inflightRef = useRef({})
  const single = (key, fn) => {
    if (inflightRef.current[key]) return inflightRef.current[key]
    const p = Promise.resolve().then(fn).finally(() => { delete inflightRef.current[key] })
    inflightRef.current[key] = p
    return p
  }

  const refreshWorktree = () => single('worktree', async () => {
    if (document.hidden) return
    const imp = await getWorktreeImpact()
    if (!imp?.ok) return
    // Tree went clean (e.g. the change got committed) → drop any stale worktree
    // review so the canvas never shows an uncommitted delta that no longer exists.
    if (!imp.dirty) setCanvasSpotlight(s => (s?.kind === 'worktree' ? null : s))
    setWorktree(prev => {
      if (prev && prev.signature === imp.signature && prev.dirty === imp.dirty) return prev
      return { dirty: imp.dirty, count: imp.fileCount, signature: imp.signature }
    })
  })

  // Prompt Story Rail: load episodes (prompt turns) + the Outside-OpenFDE bucket.
  // Also mirror the prompt→commit story into OpenPM: every landed commit becomes a
  // Done card grouped/labeled by its prompt. Idempotent (SYNC_EPISODE_COMMITS only
  // adds commits not already represented), so frequent polls don't churn the board.
  const refreshEpisodes = () => single('episodes', async () => {
    if (document.hidden) return null
    const res = await getReviewEpisodes()      // CHEAP rail: persisted episodes + cached commit titles
    if (!res?.ok) return null
    const eps = Array.isArray(res.episodes) ? res.episodes : []
    // The cheap rail omits PR readiness + the Outside bucket (those come from refreshEpisodesFull).
    // Merge so a frequent rail poll never wipes the richer fields the full fetch already loaded.
    setEpisodes(prev => eps.map(e => {
      const old = (prev || []).find(p => p.episodeId === e.episodeId)
      return old?.prReadiness ? { ...e, prReadiness: old.prReadiness } : e
    }))
    setOutsideBucket(prev => {
      const next = res.outside || null
      return (next?.commits?.length || 0) === 0 && (prev?.commits?.length || 0) > 0 ? prev : next
    })
    // Operational/meta episodes (chatter, file-lists, "Here's the CC prompt") never
    // become OpenPM cards — only product/build prompts with a clean title.
    const productEps = eps.filter(ep => ep.signal !== 'operational' && !ep.storyFacts?.operational)
    const commits = productEps.flatMap(ep => (ep.commits || []).map(c => ({
      commitSha: c.sha, shortSha: c.shortSha, summary: c.summary,
      // Clean card display text from the (cleaned) episode — never the raw commit subject.
      displayTitle: c.displayTitle || ep.title || '',
      displaySummary: c.displaySummary || ep.summary || '',
      files: c.files || [], episodeId: ep.episodeId,
      episodeTag: ep.tag || '', sequence: ep.sequence || 0,
      promptTitle: ep.title || '',
      promptLabel: ep.title || (ep.prompt || ep.summary || '').split('\n')[0].slice(0, 48),
      // Verify Gate receipts (lite) — drives the evidence badges on OpenPM cards.
      verify: ep.verify ? {
        status: ep.verify.status,
        checks: (ep.verify.checks || []).map(ch => ({ id: ch.id, label: ch.label, status: ch.status })),
      } : null,
      // Land-as-PR metadata (lite) — drives the PR #N badge on OpenPM cards.
      pr: ep.pr ? { number: ep.pr.number, url: ep.pr.url } : null,
      // Deterministic readiness verdict (server-embedded) → "ready for PR" badge.
      prReady: ep.prReadiness?.status === 'ready',
    })))
    if (commits.length) pmDispatch({ type: 'SYNC_EPISODE_COMMITS', commits })
    return eps     // enriched list — onLandChanges re-spotlights the landed episode from it
  })

  // Heavy, SPARSE enrichment: the full episodes view (reconciled commits + files, PR readiness,
  // Outside bucket). Fetched once after first paint and on window focus — NEVER on the frequent
  // rail poll — so the machine is never saturated by a 49s git-heavy call on a tight interval.
  const refreshEpisodesFull = () => single('episodesFull', async () => {
    if (document.hidden) return
    const res = await getReviewEpisodesFull()
    if (!res?.ok) return
    const eps = Array.isArray(res.episodes) ? res.episodes : []
    setEpisodes(eps)                         // full view supersedes the cheap rail (carries readiness)
    setOutsideBucket(res.outside || null)
  })

  // Pull the SERVER's reconciled task list and adopt it as local truth. GET
  // /api/tasks heals each card to its episode's current verification, so this is
  // how OpenPM stops showing a stale FAILED next to a passed episode. We set the
  // skip flag so adopting the server's list doesn't immediately echo it back via
  // the debounced save (that would be a pointless round-trip / churn loop).
  const refetchTasks = async () => {
    if (document.hidden) return
    const savedTasks = await getTasks()
    if (Array.isArray(savedTasks) && savedTasks.length > 0) {
      skipTasksSaveRef.current = true
      pmDispatch({ type: 'HYDRATE_TASKS', tasks: savedTasks })
    }
  }

  // Click a prompt chip → spotlight that episode: its edited files turn AMBER on
  // the canvas (intent-level highlight, distinct from a single commit's green),
  // and the panel lists the prompt's commits + files.
  function onSpotlightEpisode(ep) {
    if (!ep) return
    // A failed episode spotlights ONLY where it hurts: the failing file(s)
    // from its receipts, not the whole touched set.
    const failFiles = [...new Set(((ep.verify?.checks) || [])
      .flatMap(ch => (ch.failures || []).map(f => f.file)).filter(Boolean))]
    const files = (ep.verify?.status === 'failed' && failFiles.length)
      ? failFiles : (ep.files || [])
    const title = (ep.title || (ep.prompt || ep.summary || 'Prompt').split('\n')[0].slice(0, 40)) || 'Prompt'
    setCanvasSpotlight({
      kind: 'episode', episodeId: ep.episodeId,
      tag: ep.tag || '', label: title, title,
      summary: ep.summary || '', prompt: ep.prompt || '',
      status: ep.status, summarySource: ep.summarySource || null,
      count: files.length, files: [], amberFiles: files,
      fileEntries: files.map(p => ({ path: p, status: '' })),
      commits: ep.commits || [], epKind: ep.kind || 'agent',
      verify: ep.verify || null,
      pr: ep.pr || null,
      prReadiness: ep.prReadiness || null,
    })
    setActiveView('whiteboard')
  }

  // Story view: clicking a concept ambers its related files on the canvas and dims
  // OpenPM cards whose prompt tag isn't part of the concept. Stays in Story (the
  // detail is inline there); the canvas amber is ready when the user switches.
  const [highlightTags, setHighlightTags] = useState(null)
  function onSelectConcept(concept) {
    if (!concept) {
      setHighlightTags(null)
      setCanvasSpotlight(s => (s?.kind === 'storyConcept' ? null : s))
      return
    }
    setHighlightTags(concept.episodeTags || [])
    const files = concept.files || []
    setCanvasSpotlight({
      kind: 'storyConcept', label: concept.title, title: concept.title,
      summary: concept.summary || '', status: concept.status,
      count: files.length, files: [], amberFiles: files,
      fileEntries: files.map(p => ({ path: p, status: '' })),
      tags: concept.episodeTags || [],
    })
  }

  // Story drawer: clicking an intent step ambers the files it produced — the SAME canvas
  // highlight as clicking the intent box (kind:'intent'). Stays in Story; the amber is ready
  // when the user switches to the canvas (mirrors onSelectConcept).
  function onSpotlightFiles(files, label) {
    const fs = Array.isArray(files) ? files.filter(Boolean) : []
    if (!fs.length) { setCanvasSpotlight(s => (s?.kind === 'intent' ? null : s)); return }
    setCanvasSpotlight({ kind: 'intent', label: label || 'intent step',
      count: fs.length, files: [], amberFiles: fs, summary: '' })
  }

  // Click the "Outside OpenFDE" chapter chip → a detail card listing the commits
  // that weren't made through an OpenFDE prompt (manual / foreign). Same card shell
  // as an episode, minus prompt/summary/Land; its commits are clickable.
  function onSpotlightOutside(bucket) {
    if (!bucket) return
    setCanvasSpotlight({
      kind: 'outside', label: 'Outside OpenFDE', title: 'Outside OpenFDE',
      summary: bucket.summary || 'Commits not linked to an OpenFDE prompt (manual / foreign).',
      commits: bucket.commits || [], count: 0, files: [], amberFiles: [],
    })
    setActiveView('whiteboard')
  }

  // Land: OpenFDE creates the commit for the reviewed worktree changes and links
  // it to the active prompt episode (or a fresh "Manual changes" one). The only
  // user-facing commit path.
  async function onLandChanges(preferredEpisodeId) {
    if (landing) return
    setLanding(true)
    setLandNote(null)
    try {
      // Land THE episode whose card was clicked when given (the button says "these
      // changes"); else the first awaiting review (worktree-card path); else manual.
      const preferred = typeof preferredEpisodeId === 'string'
        ? episodes.find(e => e.episodeId === preferredEpisodeId) : null
      const reviewing = preferred
        || episodes.find(e => (e.status === 'reviewing' || e.status === 'needs_manual_land') && (e.files || []).length)
      const episodeId = reviewing?.episodeId || 'manual'
      const res = await landEpisode(episodeId, {})
      const [freshEps] = await Promise.all([refreshEpisodes(), (async () => {
        const commits = await getGitTimeline(); if (Array.isArray(commits)) setGitCommits(commits)
      })(), refreshWorktree()])
      if (res?.committed && res.episode) {
        // Spotlight the ENRICHED episode (carries .commits) so the Land button
        // becomes the clickable commit chips in place — the raw land response has
        // commitShas but no enriched commit rows, which read as "nothing happened".
        const enriched = (Array.isArray(freshEps) ? freshEps : [])
          .find(e => e.episodeId === res.episode.episodeId)
        onSpotlightEpisode(enriched || res.episode)
      } else if (res && !res.committed) {
        // Honest no-op: say WHY nothing landed instead of silently doing nothing.
        setLandNote(res.reason || res.status || 'nothing to commit for this episode')
        setCanvasSpotlight(s => (s?.kind === 'worktree' ? null : s))
      } else {
        setLandNote('land did not return — the server may still be working; the rail will refresh')
      }
    } finally {
      setLanding(false)
    }
  }

  // Incremental Re-assimilation (Land·Watch·Review): when external edits settle,
  // refresh OpenFDE's *understanding* (ArchGraph + semantic graph) so Review reflects
  // new files/functions/modules — WITHOUT touching the user's canvas arrangement
  // (top-level boxes come from persisted state, not the ArchGraph; only expanded
  // module internals + flows re-derive). Full-recompute in v1, triggered by changes.
  const runReassimilation = async () => {
    // Don't re-assimilate while a run is actively writing — requeue and wait for quiet.
    if (executingRef.current) { scheduleReassimilation(900); return }
    // Single-flight: never run two reassimilations at once (a burst of file_activity was issuing
    // duplicate /api/review/reassimilate calls). Requeue so the new files still get picked up.
    if (inflightRef.current.reassim) { scheduleReassimilation(900); return }
    const files = [...pendingReassimRef.current]
    if (!files.length) return
    pendingReassimRef.current = new Set()
    inflightRef.current.reassim = true
    try {
      const res = await reassimilateReview(files, 'file_activity')
      if (res?.ok && res.archGraph && Array.isArray(res.archGraph.files)) {
        setArchGraph(res.archGraph)        // refresh understanding; canvas boxes unaffected
      }
      refreshWorktree()                    // concepts may have changed → refresh the Review affordance
    } finally {
      delete inflightRef.current.reassim
    }
  }

  // Debounce: collapse a burst of edits into one re-assimilation ~1.8s after the
  // last activity. Re-armed on every file_activity (see handleFileActivity).
  function scheduleReassimilation(delay = 1800) {
    if (reassimTimerRef.current) clearTimeout(reassimTimerRef.current)
    reassimTimerRef.current = setTimeout(() => { reassimTimerRef.current = null; runReassimilation() }, delay)
  }

  // Commits can land from the terminal or the council — keep the commit rail
  // current by refetching the git timeline on window focus + a light poll while
  // the tab is visible (not just once at startup).
  useEffect(() => {
    if (!canvasHydrated) return undefined   // gate: non-canvas endpoints wait for first paint
    const refetch = async () => {
      if (document.hidden) return
      single('timeline', () => getGitTimeline().then(c => { if (Array.isArray(c)) setGitCommits(c) }))
      refreshWorktree()   // single-flighted; keeps the "Review changes" affordance current
      refreshEpisodes()   // single-flighted CHEAP rail; keeps the prompt story rail current
    }
    // First paint matters more than freshness: defer the initial probe past it, and fetch the
    // HEAVY full episodes (reconciled commits + files + PR readiness + Outside bucket) only ONCE
    // here and on focus — never on the 15s interval — so the rail poll can never saturate the box.
    const initial = () => { refetch(); refreshEpisodesFull() }
    const onFocus = () => { refetch(); refreshEpisodesFull() }
    const ric = window.requestIdleCallback
      ? requestIdleCallback(initial, { timeout: 2500 })
      : setTimeout(initial, 800)
    window.addEventListener('focus', onFocus)
    const id = setInterval(refetch, 15000)   // cheap rail only
    return () => {
      window.removeEventListener('focus', onFocus); clearInterval(id)
      if (window.requestIdleCallback) cancelIdleCallback(ric); else clearTimeout(ric)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canvasHydrated])

  // Click a commit chip → fetch its impact → spotlight touched boxes + concepts,
  // mark partially-touched concepts' untouched boxes amber.
  async function onSpotlightCommit(sha) {
    const imp = await getCommitImpact(sha)
    if (!imp?.ok) return
    // Amber "you missed some" only for high-signal (rename-coupled) concepts —
    // shared vocabulary (status enums, action constants) legitimately spans files.
    const amber = new Set()
    for (const c of (imp.affectedConcepts || [])) {
      if (c.partial && c.signal === 'high') for (const f of c.untouchedFiles) amber.add(f)
    }
    setCanvasSpotlight({
      kind: 'commit', label: imp.shortSha, summary: imp.summary,
      count: imp.fileCount, files: imp.files,
      amberFiles: [...amber], concepts: imp.affectedConcepts || [], sha: imp.sha,
    })
    setActiveView('whiteboard')
  }

  // Click the "Review changes" chip → fetch worktree impact → spotlight the
  // touched boxes + affected concepts, exactly like a commit (same dim/light/amber
  // + ConceptPanel), but for the *uncommitted* tree (no sha). Non-mutating.
  async function onSpotlightWorktree() {
    const imp = await getWorktreeImpact()
    if (!imp?.ok || !imp.dirty) { setCanvasSpotlight(null); return }
    worktreeSigRef.current = imp.signature
    const filePaths = (imp.files || []).map(f => (typeof f === 'string' ? f : f.path))
    const amber = new Set()
    for (const c of (imp.affectedConcepts || [])) {
      if (c.partial && c.signal === 'high') for (const f of c.untouchedFiles) amber.add(f)
    }
    const adds = imp.stat?.additions || 0, dels = imp.stat?.deletions || 0
    setCanvasSpotlight({
      kind: 'worktree', label: 'Uncommitted',
      summary: `Uncommitted changes · +${adds} −${dels}`,
      count: imp.fileCount, files: filePaths,
      // Per-file entries (path + status) so the panel can list new/off-canvas files
      // explicitly — a brand-new module has no box to light, but must not vanish.
      fileEntries: imp.files || [],
      amberFiles: [...amber], concepts: imp.affectedConcepts || [],
      patch: imp.patch || '', patchTruncated: !!imp.patchTruncated,
      stat: imp.stat || null, untracked: imp.untracked || [], sha: null,
    })
    setActiveView('whiteboard')
  }

  // Ask Concept — question about the active spotlight (optionally focused on one
  // concept), routed Architect/Sr Dev.
  async function onAskConcept(question, concept) {
    const s = canvasSpotlight
    if (!s) return null
    return askConcept(question, {
      kind: s.kind, label: s.label, summary: s.summary || '', sha: s.sha || null,
      // Episode spotlights keep their files in amberFiles (files: [] drives the
      // canvas dim) — sending the empty list told the model "appears in 0 files"
      // and it concluded the work was never implemented. Send the real list.
      files: (s.files && s.files.length ? s.files : s.amberFiles) || [],
      concepts: s.concepts || [],
      // Episode grounding: the record of what actually happened.
      tag: s.tag || null, status: s.status || null,
      prompt: s.kind === 'episode' ? (s.prompt || '').slice(0, 1500) : null,
      commits: s.kind === 'episode'
        ? (s.commits || []).slice(0, 8).map(c => ({ sha: c.shortSha || c.sha, title: c.displayTitle || '' }))
        : null,
      focusConcept: concept ? concept.identifier : null,
    })
  }

  // Highlight one concept's changed (green) + related (amber) files on the canvas.
  function onFocusConcept(concept) {
    setCanvasSpotlight(s => (s ? {
      ...s,
      focus: concept ? {
        identifier: concept.identifier,
        changedFiles: concept.touchedFiles || [],
        relatedFiles: concept.untouchedFiles || [],
        touched: concept.touched, total: concept.total,
      } : null,
    } : s))
  }

  // Save a short Concept Card — linked to a focused concept when present, else
  // the whole concept/commit.
  async function onSaveConceptCard({ title, summary, concept, meaning, whyCheck }) {
    const s = canvasSpotlight
    if (!s || !title?.trim()) return
    const c = concept || null
    await saveConceptCard({
      title, summary, meaning: meaning || '',
      tetherId: c ? c.identifier : (s.kind === 'tether' ? s.label : null),
      commitSha: s.kind === 'commit' ? s.sha : null,
      files: c ? c.touchedFiles : (s.files || []),
      relatedFiles: c ? c.untouchedFiles : [],
      whyCheck: whyCheck || '',
    })
    await reloadConceptCards()
  }

  // Cards relevant to the active spotlight (same concept, same commit, or an
  // affected concept of the commit).
  const spotlightCards = (() => {
    const s = canvasSpotlight
    if (!s) return []
    const conceptIds = new Set([s.kind === 'tether' ? s.label : null,
      ...((s.concepts || []).map(c => c.identifier))].filter(Boolean))
    return conceptCards.filter(c =>
      (s.kind === 'commit' && c.commitSha === s.sha) ||
      (c.tetherId && conceptIds.has(c.tetherId)))
  })()
  // Side panels are hidden by default — the canvas is the room. Cursor at a
  // screen edge rolls them out; Orient (right) pins itself open the moment the
  // user starts working in it and stays until they roll it back in.
  const [leftOpen, setLeftOpen]   = useState(false)
  const [rightOpen, setRightOpen] = useState(false)
  const [rightPinned, setRightPinned] = useState(false)
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

  // ── Session identity (Priority A): learn WHICH repo the backend is watching and key
  // the UI on repoRoot. If it changed since this browser last loaded (the same port now
  // serving a different repo), hard-reload so no stale canvas/story/task/filetree data
  // from another repo lingers. Runs first; cheap; never blocks the rest of hydration.
  useEffect(() => {
    let cancelled = false
    getSession().then(s => {
      if (cancelled || !s?.ok || !s.repoRoot) return
      try {
        const prior = localStorage.getItem('openfde-repo-root')
        localStorage.setItem('openfde-repo-root', s.repoRoot)
        if (prior && prior !== s.repoRoot) { window.location.reload(); return }
      } catch { /* localStorage unavailable — the identity label still works */ }
      setSession(s)
    })
    // FIRST PAINT — ONE cache-only call (/api/boot/canvas) hydrates the canvas (the persisted boxes
    // ARE the architecture modules — the canvas is empty without them), the warm ArchGraph (box
    // detail), and the Explorer file tree. Nothing else fires until this lands (the probe below is
    // gated on canvasHydrated), so it never queues behind Story/git and the cockpit shows its
    // modules + Explorer instantly instead of a blank "Scan repo → canvas" state. Retries while the
    // backend is still warming; only gives up (and lets the live probe try) after ~3.5s.
    let attempt = 0
    const hydrateFirstPaint = () => {
      getBootCanvas().then(bc => {
        if (cancelled) return
        if (bc?.ok) {
          backendRef.current = true
          setBackendAvailable(true)
          if (Array.isArray(bc.boxes) && bc.boxes.length) {
            skipStateSaveRef.current = true
            _rawCanvasDispatch({ type: 'HYDRATE', boxes: bc.boxes, arrows: bc.arrows ?? [] })
          }
          if (bc.fileTree) setBootFileTree(bc.fileTree)   // arch (box-internal detail) loads later, gated
          setCanvasHydrated(true)          // unleash the rest of hydration — AFTER first paint
          return
        }
        attempt += 1
        if (attempt < 6) setTimeout(hydrateFirstPaint, 600)   // backend still warming — retry
        else setCanvasHydrated(true)        // give up on cache; let the gated probe try live
      })
    }
    hydrateFirstPaint()
    getBoot().then(b => { if (!cancelled && b?.ok) setBoot(b) })  // tiny: restored-counts label
    // Rail BOOT — the latest ~10 prompt chips immediately (NOT gated on canvasHydrated), so the
    // prompt rail shows recent memory at first paint instead of waiting on the full cheap rail
    // (all ~115) behind the post-paint idle callback. The full rail hydrates later and supersedes
    // these; the guard never lets the boot-10 shrink an already-larger list (no backward jump).
    getRailBoot().then(r => {
      if (cancelled || !r?.ok) return
      const eps = Array.isArray(r.episodes) ? r.episodes : []
      if (eps.length) setEpisodes(prev => ((prev?.length || 0) > eps.length ? prev : eps))
    })
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ------------------------------------------------------------------ //
  //  Backend probe + hydration (runs once on mount)                     //
  // ------------------------------------------------------------------ //
  // GATED on canvasHydrated: the live/secondary hydration runs only AFTER first paint (the cached
  // canvas + Explorer are already on screen), so this pile of endpoints never competes with the
  // first-paint call and never starves it.
  useEffect(() => {
    if (!canvasHydrated) return undefined
    let cancelled = false

    async function probe() {
      // boot/canvas already confirmed the backend is up (backendRef). Only probe when it didn't
      // (the give-up path), so we still settle to an offline state if the backend never came up.
      if (!backendRef.current) {
        const available = await isBackendAvailable()
        if (cancelled || !available) return
        backendRef.current = true
        setBackendAvailable(true)
      }

      // Boxes + Explorer + arch already painted from /api/boot/canvas. Refine the architecture with
      // the LIVE ArchGraph in the background (the snapshot may be a few edits stale); re-fetch the
      // canvas state live too (cheap) in case it changed since the cache was written.
      getState().then(savedState => {
        if (!cancelled && savedState?.boxes?.length > 0) {
          skipStateSaveRef.current = true
          _rawCanvasDispatch({ type: 'HYDRATE', boxes: savedState.boxes, arrows: savedState.arrows ?? [] })
        }
      })
      getArchgraph().then(graph => {
        if (!cancelled && graph && Array.isArray(graph.files)) setArchGraph(graph)
      })

      // ── DEFERRED: not needed for the first canvas paint ───────────────────────
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

      // (Git history for the commit rail is loaded by the DEFERRED probe in the focus/poll
      // effect above — see requestIdleCallback(refetch) — so it no longer competes with
      // first-paint hydration here.)

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
  }, [canvasHydrated])

  // ── Live activity glow (adaptive) ─────────────────────────────────────────
  // Keep boxesRef synced so the WS handlers can map a file path → its module.
  useEffect(() => { boxesRef.current = canvasState.boxes }, [canvasState.boxes])
  useEffect(() => { archGraphRef.current = archGraph }, [archGraph])

  // WebXR canvas badges: gate on the plugin registry, then pull the summary ONCE.
  // We only fetch the (bounded) WebXR summary when the registry says the pack is
  // detected or enabled (a local manifest) for this repo — every other repo skips
  // the scan entirely. The result indexes XR entrypoints / 3D assets by path so the
  // canvas can mark matching file nodes. Architecture metadata only — no runtime or
  // test lens. Fail quiet: a badge-less canvas is the correct fallback. setState
  // lives only in the async callbacks (never synchronously in the effect), so it
  // can't trigger cascading renders.
  useEffect(() => {
    let alive = true
    getPlugins().then(res => {
      if (!alive || !res?.ok) return
      const webxr = (res.plugins || []).find(p => p.id === 'webxr')
      if (!webxr || !(webxr.detected || webxr.source === 'local')) return
      getWebxrSummary().then(sum => {
        if (!alive || !sum?.ok) return
        const map = badgeMapFromSummary(sum)
        if (Object.keys(map).length) setWebxrBadges(map)
      })
    })
    return () => { alive = false }
  }, [])

  const fileNodeId = (p) => `box:file:${p}`
  function moduleBoxForFile(path) {
    return (boxesRef.current || []).find(b => (b.linkedFiles || []).includes(path)) || null
  }

  // ── Watch Any Agent (Step 38): ambient glow when ANY editor (Cursor, Claude
  // Code, terminal, human) touches a repo file — no council run. The file→module/file/function
  // mapping lives in watchActivityTargets (lib/watchTarget, unit-tested); a file that maps to no
  // module on the canvas gets no glow (Watch presupposes Land). Entries fade after ~2.6s.
  function handleFileActivity({ file, functionId, function: fnName }) {
    if (!file) return
    // Re-assimilation collects EVERY changed path — including brand-new files that
    // aren't on the canvas yet — so the understanding catches up. Done before the
    // on-canvas check below (which gates only the ambient glow).
    pendingReassimRef.current.add(file)
    scheduleReassimilation()
    // ONE source of truth for the file→canvas plan (module id, file id, what to expand, what to
    // pulse) — shared with watchTarget's tests so the live path can't silently drift.
    const targets = watchActivityTargets(file, fnName, boxesRef.current)
    if (!targets) return  // not on the canvas — glow presupposes Land (re-assim still runs)
    // A module can only EXPAND to show its files/functions once the ArchGraph is loaded. If it
    // isn't yet (the gated startup load may still be in flight or have been starved), fetch it NOW
    // so the edit actually expands the module instead of leaving a ring on a collapsed box.
    if (!archGraphRef.current) {
      getArchgraph().then(g => { if (g && Array.isArray(g.files)) setArchGraph(g) })
    }
    // Expand the module AND the file box. Track what WE expanded so settle can auto-collapse;
    // anything the user already had open isn't in our set and stays expanded.
    setExpandedIds(prev => {
      const next = new Set(prev)
      let changed = false
      for (const id of targets.expandIds) {
        if (!next.has(id)) { next.add(id); watchAutoExpandedRef.current.add(id); changed = true }
      }
      return changed ? next : prev
    })
    // Pulse the MOST SPECIFIC known target: an explicit functionId, else the inferred function
    // (box:function:<path>:<name>), else the file (box:file:<path>). computeRunRings rolls it up to
    // whatever node is currently visible.
    const key = functionId || targets.watchKey
    setWatchActivity(prev => ({ ...prev, [key]: Date.now() }))
    setWatchTiers(prev => (prev[key] === 'active' ? prev : { ...prev, [key]: 'active' }))
    // Center the camera on the TOUCHED target (function/file), not the module container — the canvas
    // resolves the most specific laid-out node and re-centers as the expansion settles.
    setWatchFocus({ file, fnName, moduleId: targets.moduleId, ts: Date.now() })
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
      // Watch Any Agent: an external editor touched a repo file — ambient glow.
      else if (msg?.type === 'file_activity') { handleFileActivity(msg.payload || {}) }
      // Prompt captured / landed: refresh the Prompt Story Rail live (a captured
      // Claude Code prompt, a wrapper run, or a Land all push this). The episode's
      // verify/landed state is the source of truth for OpenPM, so refetch tasks
      // too — GET /api/tasks reconciles each card to its episode (no split-brain).
      else if (msg?.type === 'episode_updated') { refreshEpisodes(); refetchTasks() }
      // The server rebuilt the Story (a Land, a background warm) — nudge Story to re-hydrate the
      // full graph; the boot cache it just refreshed makes the refetch instant.
      else if (msg?.type === 'story_updated') { setStoryNonce(n => n + 1) }
      // The SERVER moved/relabelled a card (verify result, lifecycle, land) — pull
      // the reconciled list so OpenPM never shows a stale FAILED beside a passed
      // episode. (This used to be a no-op; the server is now a task writer too.)
      else if (msg?.type === 'tasks_updated') { refetchTasks() }
      // A commit landed (the only commit path) — refresh the rail's nested beats,
      // the OpenPM commit cards, and the git timeline so all three stay in sync.
      else if (msg?.type === 'commit_created') {
        refreshEpisodes()
        refetchTasks()
        getGitTimeline().then(c => { if (Array.isArray(c)) setGitCommits(c) })
      }
      // state_updated: no-op; this client's own canvas writes are local truth.
    })
    return () => closeWS()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Hold the watch glow while the agent works; settle the whole set only after a
  // global quiet period (no edits anywhere for ~9s). Each tick re-derives tiers:
  // a box touched in the last ~3.5s is the live focus (bright), earlier ones this
  // session are a calmer trail. Date.now() lives here (effect), never in render.
  useEffect(() => {
    if (Object.keys(watchActivity).length === 0) return undefined
    const SETTLE_MS = 9000, ACTIVE_MS = 3500
    const id = setInterval(() => {
      const now = Date.now()
      if (now - Math.max(...Object.values(watchActivity)) > SETTLE_MS) {
        setWatchActivity({}); setWatchTiers({})
        // Work finished → collapse only the modules WE auto-expanded (leave the
        // user's own expansions open).
        const auto = watchAutoExpandedRef.current
        if (auto.size) {
          setExpandedIds(prev => {
            const s = new Set(prev)
            auto.forEach(mid => s.delete(mid))
            return s
          })
          auto.clear()
        }
        // Watch → Review handoff: edits have settled, so surface the calm
        // "Review changes" affordance (signature-gated; never steals focus).
        refreshWorktree()
      } else {
        setWatchTiers(Object.fromEntries(
          Object.entries(watchActivity).map(([id, t]) => [id, now - t < ACTIVE_MS ? 'active' : 'trail'])))
      }
    }, 700)
    return () => clearInterval(id)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchActivity])

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

  // L2-B: request a focused neighborhood (a small subgraph), bounded by the backend (cached graph,
  // capped). Reuses /api/focus/neighborhood. Never mutates the canvas — it opens an overlay lens.
  const requestFocus = useCallback(async ({ seeds, primaryPath = null, label = '', hops = 1 }) => {
    const clean = [...new Set((seeds || []).filter(Boolean))]
    if (!clean.length) return
    setFocusLens({ busy: true, seedLabel: label, hops })
    const res = await postFocusNeighborhood(clean, { hops, primaryPath })
    setFocusLens(res?.ok
      ? { result: res, seedLabel: label, hops, busy: false }
      : { error: true, seedLabel: label, hops, busy: false })
  }, [])

  // Show failure flow → enter the lens. Expand exactly the files on the distilled
  // causal path (so their FUNCTION boxes render — the lens rings functions, not
  // files), ONCE per failure fingerprint. Mirrors onShowFailure's archGraph
  // fallback so the expansion lands even when the graph isn't preloaded yet.
  function onShowFlow(art) {
    const fp = art?.fingerprint || (hatch ? `${hatch.file}:${hatch.line}` : '')
    const pathNodes = art?.primaryPath?.length ? art.primaryPath : (art?.nodes || [])
    const files = [...new Set(pathNodes.map(n => n.file).filter(Boolean))]
    const expand = (graph) => {
      if (!files.length || flowExpandRef.current === fp) return
      flowExpandRef.current = fp
      setExpandedIds(prev => {
        const next = new Set(prev)
        for (const f of files) {
          const meta = (graph?.files || []).find(x => x.path === f)
          const modBox = meta ? canvasState.boxes.find(b => b.moduleId === meta.moduleId) : null
          if (modBox) next.add(modBox.id)   // reveal the module…
          next.add(`box:file:${f}`)         // …and the file box inside it (renders its functions)
        }
        return next
      })
    }
    if (archGraph) expand(archGraph)
    else getArchgraph().then(g => { if (g && Array.isArray(g.files)) { setArchGraph(g); expand(g) } })
    setFlowLens({ artifact: art })
  }

  // Tie an issue card to the canvas: expand the file's module + the file box
  // and go there — the issue gets a HOME in the architecture even without a repro.
  function onFocusFile(file) {
    const open = (graph) => {
      const f = (graph?.files || []).find(x => x.path === file)
      const modBox = f ? canvasState.boxes.find(b => b.moduleId === f.moduleId) : null
      setActiveView('whiteboard')
      setExpandedIds(prev => {
        const next = new Set(prev)
        if (modBox) next.add(modBox.id)
        next.add(`box:file:${file}`)
        return next
      })
    }
    if (archGraph) open(archGraph)
    else getArchgraph().then(g => { if (g && Array.isArray(g.files)) { setArchGraph(g); open(g) } })
  }

  // "Show →" on a failed check: resolve the failing line to its FUNCTION via the
  // ArchGraph (containing function = greatest start-line ≤ failure line; range
  // ends where the next function begins), focus the canvas there (expand the
  // module + file), and open the repair hatch on exactly that slice.
  function onShowFailure(failure, _check, episodeId) {
    const open = (graph) => {
      const fns = (graph?.functions || []).filter(f => f.path === failure.file)
        .sort((a, b) => (a.line || 0) - (b.line || 0))
      let fn = null
      let end = failure.line + 22
      for (let i = 0; i < fns.length; i++) {
        const isLast = i === fns.length - 1
        if ((fns[i].line || 0) <= failure.line && (isLast || (fns[i + 1].line || 0) > failure.line)) {
          fn = fns[i]
          end = isLast ? failure.line + 30 : Math.max(failure.line, (fns[i + 1].line || 0) - 1)
          break
        }
      }
      const start = fn ? fn.line : Math.max(1, failure.line - 8)
      const file = (graph?.files || []).find(f => f.path === failure.file)
      const modBox = file ? canvasState.boxes.find(b => b.moduleId === file.moduleId) : null
      setActiveView('whiteboard')
      setExpandedIds(prev => {
        const next = new Set(prev)
        if (modBox) next.add(modBox.id)
        next.add(`box:file:${failure.file}`)
        return next
      })
      setRepairPhase(null)
      setFlowEditors([])          // a fresh hatch starts with no extra trail editors
      setHatchZ(60)               // reset hatch window z
      setHatch({ ...failure, start, end, funcName: fn?.name || failure.func,
                 episodeId: episodeId || '', checkId: _check?.id || '',
                 failureMsg: (_check?.outputTail || _check?.summary || '').slice(0, 2000) })
    }
    if (archGraph) open(archGraph)
    else getArchgraph().then(g => { if (g && Array.isArray(g.files)) { setArchGraph(g); open(g) } })
  }

  // Resolve a failure-trail node {file, function, line} to its function SLICE,
  // using the SAME line+name resolver the lens uses to place the ring
  // (pickPrimaryFn) so the editor shows exactly the box you clicked; the span ends
  // where the next function begins. Used to open trail editors.
  function sliceForNode(graph, node) {
    const fns = (graph?.functions || []).filter(f => f.path === node.file)
      .sort((a, b) => (a.line || 0) - (b.line || 0))
    const fn = pickPrimaryFn(graph?.functions || [], node)
    if (!fn) {                       // no arch function → a small window around the line
      const line = node.line || 1
      return { file: node.file, funcName: node.function, start: Math.max(1, line - 8), end: line + 22, line }
    }
    const idx = fns.indexOf(fn)
    const isLast = idx === fns.length - 1
    const start = fn.line || 1
    const end = isLast ? start + 30 : Math.max(node.line || start, (fns[idx + 1].line || 0) - 1)
    return { file: node.file, funcName: fn.name, start, end, line: node.line || start }
  }

  // Open a function in its own floating editor — the explicit "Open in editor"
  // action (RIGHT-click; never a left-click, which only focuses arrows). Works
  // with or without a hatch (right-click any function on the canvas, or a failure
  // trail node). The failing function's editor IS the hatch, so right-clicking it
  // just brings the hatch forward (no duplicate). Any other function opens/raises
  // its own editor; a repeat open raises the existing one (and un-minimizes it).
  function onOpenEditor(node) {
    if (!node || !node.file) return
    const base = (s) => String(s || '').split('.').pop()
    if (hatch && node.file === hatch.file && base(node.function) === base(hatch.funcName || hatch.func)) {
      raiseHatch(); return            // the hatch already IS this function's editor
    }
    const open = (graph) => {
      const slice = sliceForNode(graph, node)
      if (!slice || !slice.funcName) return
      const id = `${slice.file}::${slice.funcName}`
      setFlowEditors(prev => {
        const seq = (editorSeq.current += 1)            // newest/raised → highest z
        const i = prev.findIndex(e => e.id === id)
        if (i >= 0) {                                    // already open → raise + un-minimize
          const next = prev.slice()
          next[i] = { ...next[i], ...slice, line: node.line ?? next[i].line, minimized: false, seq }
          return next
        }
        return [...prev, { ...slice, id, line: node.line ?? slice.line, minimized: false, seq }]
      })
    }
    if (archGraph) open(archGraph)
    else getArchgraph().then(g => { if (g && Array.isArray(g.files)) { setArchGraph(g); open(g) } })
  }
  // Bring the repair hatch forward (shared z counter with the trail editors).
  const raiseHatch = () => setHatchZ(editorSeq.current += 1)

  // Trail-editor lifecycle: close removes it, minimize parks it as a dock chip,
  // raise bumps its z above the others (and the hatch) on focus/repeat-click.
  const closeFlowEditor = (id) => setFlowEditors(prev => prev.filter(e => e.id !== id))
  const minFlowEditor = (id, val) => setFlowEditors(prev => prev.map(e => e.id === id ? { ...e, minimized: val } : e))
  const raiseFlowEditor = (id) => setFlowEditors(prev => prev.map(e => e.id === id ? { ...e, seq: (editorSeq.current += 1) } : e))

  // A Senior-Dev run finished and wrote files. Route EACH file's hunks into that
  // file's editor — the change shows where it landed (e.g. Completions.create in
  // client.py), not the test hatch. An editor already open for the file gets the
  // diff and is raised; a file with no editor opens one at the changed function.
  function onRepairDiff(diffText) {
    if (!diffText) return
    const byFile = splitDiffByFile(diffText)
    const files = Object.keys(byFile)
    if (!files.length) return
    const apply = (graph) => {
      setFlowEditors(prev => {
        let next = prev.slice()
        for (const file of files) {
          const section = byFile[file]
          const line = firstHunkNewLine(section) || 1
          const i = next.findIndex(e => e.file === file)
          const seq = (editorSeq.current += 1)
          if (i >= 0) {                       // already open → stamp the diff, raise
            next[i] = { ...next[i], minimized: false, seq, diff: section }
            continue
          }
          const slice = sliceForNode(graph, { file, function: null, line })
          if (!slice || !slice.funcName) continue
          next = [...next, { ...slice, id: `${slice.file}::${slice.funcName}`, line, minimized: false, seq, diff: section }]
        }
        return next
      })
    }
    if (archGraph) apply(archGraph)
    else getArchgraph().then(g => { if (g && Array.isArray(g.files)) { setArchGraph(g); apply(g) } })
  }

  // Which trail functions are open in a (trail) editor, keyed by file::baseName so
  // the lens can show them in a subtle 'selected' state. The failing function is
  // excluded — it keeps its red failure ring (it lives in the hatch, not a trail
  // editor).
  const openFlowFns = (() => {
    const s = new Set()
    const base = (x) => String(x || '').split('.').pop()
    flowEditors.forEach(e => s.add(`${e.file}::${base(e.funcName)}`))
    return s
  })()

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

  // Sketch-First: selecting a single intent box spotlights the files it produced —
  // amber on the canvas, unrelated dimmed — using the existing spotlight language.
  // Only ever sets/clears the 'intent' spotlight, so commit/episode spotlights stand.
  const intentSelKey = [...(canvasState.selectedIds ?? [])].join(',')
  useEffect(() => {
    const ids = [...(canvasState.selectedIds ?? [])]
    const box = ids.length === 1 ? canvasState.boxes.find(b => b.id === ids[0]) : null
    const files = (box?.kind === 'intent' && Array.isArray(box.implementationFiles))
      ? box.implementationFiles : []
    // Syncing shared spotlight state from selection — only ever touches the 'intent'
    // spotlight (returns the same ref otherwise, so commit/episode spotlights stand).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCanvasSpotlight(s => (
      files.length
        ? { kind: 'intent', label: box.title || 'intent step',
            count: files.length, files: [], amberFiles: files, summary: '' }
        : (s?.kind === 'intent' ? null : s)
    ))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intentSelKey])

  // ("Explain this" retired — replaced by canvas-native Ask Concept, Step 37a.)

  // Select (or clear) the inspected file/function entity. Passing a null kind
  // clears it — e.g. when a module box itself is selected.
  function selectArchEntity(kind, data) {
    if (!kind || !data) { setArchSel(null); return }
    // Click-toggle: selecting the already-selected box clears it (its flow
    // arrows disappear with the selection).
    if (archSel?.data?.id != null && archSel.data.id === data.id) {
      setArchSel(null)
      return
    }
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
      // Sketch-First v2: the generated workspace is the correct path for an
      // intent-only sketch — surfaced calmly in the result, not as an error.
      generatedScope: !!res.generatedScope,
      workspace: res.workspace || null,
    }
    setAgentMessages(prev => [...prev, ...stageMsgs, resultMsg])
    if (res.commit && res.commit.committed) {
      refreshGitTimeline()
      // Pre-load this run's diff so Work Review can show the change inline (and
      // the Diff tab is ready) without a detour through the Timeline.
      const sha = res.commit.sha
      getGitDiff(sha).then(data => setCommitDiff({ loading: false, sha, data: data || null }))
    }
    // Review Then Land: the run left edits in the work tree under a prompt episode.
    // Surface the prompt chip + Review Changes affordance (no auto-commit).
    if (!cancelled) { refreshEpisodes(); refreshWorktree() }
    if (res.approval) setApprovals(prev => [res.approval, ...prev])
    // Sketch-First Intent: link the run's files back onto the intent steps it
    // implemented (persists via the debounced canvas save → shows a count badge).
    if (res.intentLinks && Object.keys(res.intentLinks).length) {
      const links = res.intentLinks
      _rawCanvasDispatch({ type: 'SET_IMPL_FILES', links, runId: res.runId })
      // Mirror the intent steps into OpenPM — one grouped card per step, linked to
      // its box + generated files (column reflects committed / pending-land / doing).
      const steps = canvasState.boxes
        .filter(b => b.kind === 'intent' && links[b.id])
        .map(b => ({ boxId: b.id, title: b.title || 'intent step', files: links[b.id].files || [] }))
      if (steps.length) {
        pmDispatch({
          type: 'SYNC_INTENT_STEPS', steps,
          episodeId: res.episodeId || null, runId: res.runId,
          tag: steps.map(s => s.title).join(' → '),
          committed: !!(res.commit && res.commit.committed),
          commitSha: res.commit ? res.commit.sha : null,
          awaitingReview: !!res.awaitingReview,
        })
      }
    }
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

  // Mirror `executing` into a ref so the re-assimilation debounce timer can read the
  // current run state without re-arming on every render.
  useEffect(() => { executingRef.current = executing }, [executing])

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
          onExpandAll={expandAll}
          onCollapseAll={collapseAll}
          onOpenCommandPalette={() => setPaletteOpen(true)}
          onHome={goHome}
          onRaiseIssue={() => setRaiseOpen(true)}
          repoName={session?.repoName || ''}
        />
        {/* Global "Raise OpenFDE issue": product feedback from anywhere. Mounted
            only while open so each open starts a fresh compose. The Architect drafts
            from the user's words + light context (the backend scrubs repo data) and
            nothing posts until the user clicks Raise. */}
        {raiseOpen && (
          <RaiseIssue
            onClose={() => setRaiseOpen(false)}
            context={() => ({
              view: activeView,
              episode: (() => {
                const ep = episodes.find(e => e.status === 'reviewing'
                  || e.status === 'needs_manual_land') || episodes[0]
                return ep ? { title: ep.displayTitle || ep.title || '', status: ep.status || '' } : null
              })(),
              recentEvents: (designEvents || []).slice(-5).map(e => e?.type).filter(Boolean),
            })}
          />
        )}
        {/* Restore-confidence banner: shows the counts recovered from .openfde the instant boot
            returns, so a slow hydration never reads as "my work vanished". Auto-hides once the
            architecture canvas has hydrated (archGraph loaded). Non-blocking. */}
        {boot && !archGraph && (boot.episodeCount > 0 || boot.taskCount > 0 || boot.candidateCount > 0) && (
          <div role="status" style={{
            position: 'fixed', top: 46, left: '50%', transform: 'translateX(-50%)', zIndex: 50,
            fontSize: 11.5, fontWeight: 500, color: 'var(--text)', padding: '5px 12px',
            borderRadius: 99, background: 'var(--surface-2)', border: '1px solid var(--border)',
            boxShadow: '0 2px 10px rgba(0,0,0,0.18)', pointerEvents: 'none', whiteSpace: 'nowrap',
          }}>
            {(boot.episodeCount > 0 || boot.taskCount > 0)
              ? `Restored ${boot.episodeCount} episode${boot.episodeCount === 1 ? '' : 's'} · `
                + `${boot.taskCount} task${boot.taskCount === 1 ? '' : 's'}`
                + `${boot.restoredFrom ? ` · from ${boot.restoredFrom}` : ''} · refreshing…`
              : 'Restoring…'}
            {boot.candidateCount > 0 && (
              <span style={{ color: 'var(--text-muted)' }}>
                {'  ·  '}{boot.candidateCount} transcript candidate{boot.candidateCount === 1 ? '' : 's'} found
              </span>
            )}
          </div>
        )}
        <div className="panels">
          <div className="edge-zone left" onMouseEnter={() => setLeftOpen(true)} />
          <div className="edge-zone right" onMouseEnter={() => setRightOpen(true)} />
          <aside className={`panel-left${leftOpen ? '' : ' collapsed'}`}
                 onMouseLeave={() => setLeftOpen(false)}>
            <button className="panel-collapse-btn left" onClick={() => setLeftOpen(o => !o)}
              title={leftOpen ? 'Collapse file tree' : 'Expand file tree'}>
              {leftOpen ? '‹' : '›'}
            </button>
            {leftOpen && <FileTree repoName={session?.repoName || ''} cachedTree={bootFileTree} webxrBadges={webxrBadges} />}
          </aside>
          <main className="panel-middle">
            <Whiteboard
              activeTool={activeTool}
              setActiveTool={setActiveTool}
              activeView={activeView}
              setActiveView={setActiveView}
              storyNonce={storyNonce}
              canvasState={canvasState}
              canvasDispatch={canvasDispatch}
              onLoadSelfMap={() => canvasDispatch({ type: 'LOAD_SELF_MAP' })}
              onGenerateFromRepo={backendAvailable ? onGenerateFromRepo : null}
              onExecute={backendAvailable ? onWorkExecute : null}
              executing={executing}
              repoName={session?.repoName || ''}
              archGraph={archGraph}
              webxrBadges={webxrBadges}
              expandedIds={expandedIds}
              onToggleExpand={toggleExpand}
              onSelectArchEntity={selectArchEntity}
              archSel={archSel}
              onExpandModule={expandModule}
              flowMode={flowMode}
              failFocus={hatch ? { fnId: `box:function:${hatch.file}:${hatch.funcName || hatch.func}`, fileId: `box:file:${hatch.file}` } : null}
              onFocusFile={onFocusFile}
              flowLens={flowLens}
              onOpenEditor={onOpenEditor}
              openFlowFns={openFlowFns}
              repairPhase={hatch ? repairPhase : null}
              onExitFlowLens={() => { setFlowLens(null); flowExpandRef.current = '' }}
              onRegenFlowLens={async () => {
                if (!hatch || !flowLens?.artifact || flowLens.busy) return
                setFlowLens(l => ({ ...l, busy: true }))
                const r = await hatchFlow({
                  episodeId: hatch.episodeId || '', checkId: hatch.checkId || '',
                  failureMsg: hatch.failureMsg || '', file: hatch.file, line: hatch.line,
                  test: hatch.test || '', funcName: hatch.funcName || hatch.func,
                  start: hatch.start, end: hatch.end,
                  fingerprint: flowLens.artifact.fingerprint, regenerate: true,
                })
                setFlowLens(r?.artifact ? { artifact: r.artifact } : (l => ({ ...l, busy: false })))
              }}
              story={story}
              runNodeStates={run?.nodeStates}
              runEdgeStates={run?.edgeStates}
              watchBoxIds={watchTiers}
              watchConnected={backendAvailable}
              hydrating={!canvasHydrated}
              watchFocus={watchFocus}
              liveFollow={liveFollow}
              onToggleLiveFollow={() => setLiveFollow(v => !v)}
              spotlight={canvasSpotlight}
              onClearSpotlight={() => setCanvasSpotlight(null)}
              onSpotlightCommit={onSpotlightCommit}
              gitCommits={gitCommits}
              onSelectCommit={onSelectCommit}
              worktreeDirty={!!worktree?.dirty}
              worktreeCount={worktree?.count || 0}
              onReviewChanges={onSpotlightWorktree}
              reviewActive={canvasSpotlight?.kind === 'worktree'}
              episodes={episodes}
              outsideBucket={outsideBucket}
              onSpotlightEpisode={onSpotlightEpisode}
              onSpotlightFiles={onSpotlightFiles}
              activeEpisodeId={canvasSpotlight?.kind === 'episode' ? canvasSpotlight.episodeId : null}
              onSpotlightOutside={onSpotlightOutside}
              outsideActive={canvasSpotlight?.kind === 'outside'}
              onSelectConcept={onSelectConcept}
              highlightTags={highlightTags}
              tasks={tasks}
              pmDispatch={pmDispatch}
              designEvents={designEvents}
              onTaskEvent={onTaskEvent}
              selectedTaskId={selectedTaskId}
              setSelectedTaskId={setSelectedTaskId}
              setPanelMode={openTechnicalMode}
            />
            {canvasSpotlight && activeView === 'whiteboard' && (
              <ConceptPanel
                key={`${canvasSpotlight.kind}:${canvasSpotlight.sha || canvasSpotlight.label}`}
                spotlight={canvasSpotlight}
                cards={spotlightCards}
                onAsk={onAskConcept}
                onSaveCard={onSaveConceptCard}
                onFocusConcept={onFocusConcept}
                onClose={() => setCanvasSpotlight(null)}
                onLand={onLandChanges}
                landing={landing}
                landNote={landNote}
                onSpotlightCommit={onSpotlightCommit}
                onShowFailure={onShowFailure}
              />
            )}
            {hatch && <FunctionPatch hatch={hatch} z={hatchZ}
                                     onClose={() => { setHatch(null); setFlowEditors([]); setFlowLens(null); setRepairPhase(null); flowExpandRef.current = '' }}
                                     onShowFlow={onShowFlow}
                                     onRepairPhase={setRepairPhase}
                                     onRepairDiff={onRepairDiff}
                                     lensActive={!!flowLens?.artifact} />}
            {/* Floating editors opened via "Open in editor" — coexist with the
                hatch; each independently movable, minimizable, closable. Rendered
                whether or not a hatch is open (right-click any function on canvas). */}
            {flowEditors.map((ed, i) => ed.minimized ? null : (
              <TrailEditor key={ed.id} editor={ed} episodeId={hatch?.episodeId || ''}
                           lensActive={!!flowLens?.artifact}
                           onClose={() => closeFlowEditor(ed.id)}
                           onMinimize={() => minFlowEditor(ed.id, true)}
                           onFocus={() => raiseFlowEditor(ed.id)}
                           style={{ left: 24 + i * 34, top: 96 + i * 34, right: 'auto',
                                    transform: 'none', zIndex: ed.seq }} />
            ))}
            {/* Minimized editors → labelled chips, docked above the hatch's own
                card dock so nothing overlaps. One click restores. */}
            {flowEditors.some(e => e.minimized) && (
              <div className={`hatch-min-dock trail${flowLens?.artifact ? ' lens' : ''}`}>
                {flowEditors.filter(e => e.minimized).map(e => (
                  <button key={e.id} className="hatch-min-chip" onClick={() => minFlowEditor(e.id, false)}
                          title="Restore editor">
                    <span className="hatch-min-dot trail" /> {e.funcName} · {e.file.split('/').pop()}
                  </button>
                ))}
              </div>
            )}
            {/* L2-B Focus: a small focused subgraph for the selected file/function or the active
                failure path. Overlays the full canvas (which stays intact); Exit returns to it. */}
            {!focusLens && activeView === 'whiteboard' && (() => {
              let target = null
              if (flowLens?.artifact) {
                const files = [...new Set((flowLens.artifact.primaryPath || []).map(n => n.file).filter(Boolean))]
                if (files.length) target = { seeds: files, primaryPath: files, label: 'failure path', kind: 'failure' }
              } else if (archSel?.data?.path) {
                target = { seeds: [archSel.data.path], label: archSel.data.path, kind: 'file' }
              }
              if (!target) return null
              return (
                <button className="focus-trigger" onClick={() => requestFocus(target)}
                        title="Show a small focused neighborhood for this target — the full canvas stays intact">
                  ⊙ Focus {target.kind === 'failure' ? 'the failure' : 'this file'}
                </button>
              )
            })()}
            {focusLens && activeView === 'whiteboard' && (
              <FocusLens lens={focusLens} onExit={() => setFocusLens(null)} />
            )}
          </main>
          <aside className={`panel-right${rightOpen ? '' : ' collapsed'}`}
                 onMouseLeave={() => { if (!rightPinned) setRightOpen(false) }}
                 onFocusCapture={() => setRightPinned(true)}
                 onPointerDownCapture={() => setRightPinned(true)}>
            <button className="panel-collapse-btn right" onClick={() => { setRightPinned(false); setRightOpen(o => !o) }}
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
                onExplain={null}
                onOpenDiff={onSelectCommit}
                onReset={onWorkReset}
                intent={workIntent}
                onIntentChange={handleWorkIntent}
                run={run}
                onStop={onStopRun}
                onOpenAgentSettings={() => setAgentSettingsOpen(true)}
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
              onExplain={null}
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
        onOpenSemanticGraph={() => { setPaletteOpen(false); setSemanticGraphOpen(true) }}
        onOpenPlugins={() => { setPaletteOpen(false); setPluginsOpen(true) }}
      />
      {agentSettingsOpen && agentSettings && agentOptions && (
        <AgentSettings
          settings={agentSettings}
          options={agentOptions}
          onClose={() => setAgentSettingsOpen(false)}
          onSettingsChange={setAgentSettings}
        />
      )}
      {pluginsOpen && <Plugins onClose={() => setPluginsOpen(false)} />}
      {semanticGraphOpen && (
        <SemanticGraphCard
          onClose={() => setSemanticGraphOpen(false)}
          onSpotlightTether={(t) => {
            setCanvasSpotlight({ kind: 'tether', label: t.identifier, count: t.fileCount, files: t.files })
            setActiveView('whiteboard')
            setSemanticGraphOpen(false)
          }}
        />
      )}
    </>
  )
}
