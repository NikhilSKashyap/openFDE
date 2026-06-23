import { useRef, useState, useEffect, useCallback, useMemo } from 'react'
import CanvasBox from './CanvasBox'
import ContextMenu from './ContextMenu'
import Arrow from './Arrow'
import { getPortPos, bezierPath, getBezierMidpoint } from './arrowUtils'
import PendingArrow from './PendingArrow'
import { DEFAULT_W, DEFAULT_H } from '../../store/canvasState'
import { computeArchLayout, computeFlowArrows, isIntentDrillBox } from './archLayout'
import { computeArchLayoutElk } from './elkArchLayout'
import { computeStoryLayout } from './storyLayout'
import { pickPrimaryFn } from '../../lib/flowResolve'
import { watchFocusTargetId } from '../../lib/watchTarget'
import { badgeNodesById } from '../../lib/webxrBadges'

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))
const truncate = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + '…' : (s || ''))
const EMPTY_SET = new Set()

// Tether spotlight helpers: gather a node's file paths (string or {path|name})
// from its layout files + the underlying box's linkedFiles, and basename-match.
const baseName = (p) => (typeof p === 'string' ? p : '').split('/').pop()
function nodeFilePaths(node) {
  const out = []
  const push = (f) => {
    if (typeof f === 'string') out.push(f)
    else if (f && typeof f.path === 'string') out.push(f.path)
    else if (f && typeof f.name === 'string') out.push(f.name)
  }
  ;(node.files || []).forEach(push)
  ;(node.box?.linkedFiles || []).forEach(push)
  ;(node.box?.files || []).forEach(push)
  // Built intent boxes ground via implementationFiles — include them so a Story episode /
  // step spotlight (which ambers the episode's files) highlights the SAME intent box.
  ;(node.box?.implementationFiles || []).forEach(push)
  return out
}

// Sketch-First: a BUILT intent box summarizes its grounding on the collapsed card — the produced
// files and (when the archGraph has them) how many functions they hold. No new source of truth:
// files come from box.implementationFiles (persisted), symbols from the live archGraph. The full
// file/function drill-in is the standard module expansion (chevron + double-click), not a card.
function intentSymbols(box, archGraph) {
  const files = (box?.kind === 'intent' && Array.isArray(box.implementationFiles))
    ? box.implementationFiles : []
  if (!files.length) return null
  const fnByPath = {}
  for (const fn of (archGraph?.functions || [])) {
    if (fn?.path && files.includes(fn.path)) (fnByPath[fn.path] || (fnByPath[fn.path] = [])).push(fn.name)
  }
  const fileNodes = files.map(p => ({ path: p, fns: fnByPath[p] || [] }))
  const fnCount = fileNodes.reduce((s, f) => s + f.fns.length, 0)
  return { fileNodes, fileCount: files.length, fnCount }
}

// Centroid label for a spotlight: concept name + occurrence count, or a commit
// shortSha + a one-line "Changed N files · concepts: X, Y" annotation.
function spotlightLabel(spotlight) {
  if (!spotlight) return null
  if (spotlight.focus) {
    const f = spotlight.focus
    const main = f.identifier
    const sub = `changed ${f.touched} · review ${f.total - f.touched} related`
    return { main, sub, w: Math.max(main.length, sub.length) * 6.6 + 28 }
  }
  if (spotlight.kind === 'episode') {
    // Prompt episode: the user's intent leads; edited files are amber underneath.
    const msg = spotlight.label || spotlight.summary || 'Prompt'
    const main = msg.length > 44 ? msg.slice(0, 43) + '…' : msg
    const nFiles = (spotlight.amberFiles || spotlight.files || []).length
    const nCommits = (spotlight.commits || []).length
    const parts = [`${nFiles} file${nFiles === 1 ? '' : 's'}`]
    if (nCommits) parts.push(`${nCommits} commit${nCommits === 1 ? '' : 's'}`)
    if (spotlight.status) parts.unshift(spotlight.status)
    const sub = parts.join(' · ')
    const w = Math.max(main.length, sub.length) * 6.6 + 28
    return { main, sub, w }
  }
  if (spotlight.kind === 'commit' || spotlight.kind === 'worktree') {
    // Primary label answers "what happened" — the commit/change summary.
    const msg = spotlight.summary || spotlight.label
    const main = msg.length > 40 ? msg.slice(0, 39) + '…' : msg
    const nConcepts = (spotlight.concepts || []).length
    const lead = spotlight.kind === 'worktree' ? 'uncommitted' : spotlight.label
    const parts = [lead, `${spotlight.count} file${spotlight.count === 1 ? '' : 's'}`]
    if (nConcepts) parts.push(`${nConcepts} concept${nConcepts === 1 ? '' : 's'}`)
    const sub = parts.join(' · ')
    const w = Math.max(main.length, sub.length) * 6.6 + 28
    return { main, sub, w }
  }
  const main = `${spotlight.label} · ${spotlight.count} place${spotlight.count === 1 ? '' : 's'}`
  return { main, sub: null, w: main.length * 8 + 28 }
}

export default function WhiteboardCanvas({
  activeTool, setActiveTool, state, dispatch,
  onLoadSelfMap, onGenerateFromRepo, onLoadSketchDemo, onLoadSaasDemo, onExecute, executing = false, repoName = '',
  // Nesting (Step 16 in-place expansion)
  archGraph = null, expandedIds, onToggleExpand, onSelectArchEntity, archSel = null, onExpandModule = null,
  flowMode = 'focused', story = null,
  failFocus = null,   // {fnId, fileId} — red-ring + auto-scroll to the failing function
  flowLens = null,    // {artifact:{summary,nodes,edges,source}, busy} — failure-flow lens
  repairPhase = null, // 'failing' | 'fixing' | 'fixed' — the focused function's ring state
  onExitFlowLens,
  onRegenFlowLens,
  onOpenEditor = null,  // (node{file,function,line,role}) → RIGHT-CLICK "Open in editor"
  openFlowFns = null,   // Set of `file::baseName` currently open in an editor → 'selected' lens nodes
  // Live run states (Step 17)
  runNodeStates = null, runEdgeStates = null,
  watchBoxIds = null,
  watchConnected = false,          // backend/WS reachable → show the Live pill, even before activity
  hydrating = false,               // first-paint canvas still loading → show a skeleton, not the blank CTA
  watchFocus = null,               // { file, fnName, moduleId, ts } → center the camera on the touched target
  // Live follow (Step 40): center the camera on the file an agent is editing.
  liveFollow = true, onToggleLiveFollow = null,
  // Canvas spotlight (Step 37a Slice 2/3): light the boxes holding a concept, or
  // the boxes a commit touched (+ amber for partially-touched concepts).
  spotlight = null, onClearSpotlight = null,
  // WebXR architecture badges: path → { kind, label } for files the WebXR pack
  // flagged (XR entrypoints / 3D assets). null on every non-WebXR repo.
  webxrBadges = null,
}) {
  const svgRef = useRef(null)
  const scrollRef = useRef(null)          // .wb-canvas-scroll — Live-follow camera pans this
  const followRef = useRef({ id: null, timer: null })
  const focusTargetRef = useRef({ stamp: null, posKey: null })   // watchFocus camera centering
  const interaction = useRef(null)
  const lastModDownRef = useRef(null)   // { id, t } — manual module double-click timing
  const [rubberBand, setRubberBand] = useState(null)
  const [contextMenu, setContextMenu] = useState(null)
  const [demoState, setDemoState] = useState('idle')   // empty-state Sketch demo: idle | loading | error
  const [saasState, setSaasState] = useState('idle')   // empty-state SaaS example: idle | loading | error
  const [lensFocusId, setLensFocusId] = useState(null)  // left-clicked lens node → focus its arrows
  const [editOverlay, setEditOverlay] = useState(null)
  const [pendingArrow, setPendingArrow] = useState(null)
  const [scale, setScale] = useState(1)
  const failScrollRef = useRef(null)   // one auto-scroll per focused failure
  const [hoverFn, setHoverFn] = useState(null)

  const { boxes, arrows, selectedIds, selectedArrowIds, editingBoxId, editingField } = state

  // ── Compute nested layout (expanded modules → files → functions) ──────────
  // Memoised so transient state (hover, zoom, rubber-band) doesn't re-run the
  // layout — important since hover updates happen on pointer move.
  const manualExpanded = expandedIds instanceof Set ? expandedIds : EMPTY_SET

  // No semantic-zoom hiding: if a module/file is expanded, its children render at
  // ANY zoom. Structure is never hidden by altitude — we solve focus by
  // INTERACTION (left-click focuses a function's arrows), not by hiding boxes. So
  // `Show →` always shows the failed function's ring, even at 50-70% zoom. It's OK
  // if a fully-expanded repo looks busy; that's the honest structure.
  const altitude = 2
  const expanded = manualExpanded
  // Dagre lays out files/functions synchronously — this is the instant first
  // paint and the fallback if ELK hasn't resolved yet or fails. It is NOT shown
  // once ELK is ready; it only seeds the canvas so there's never a blank frame.
  const dagreLayout = useMemo(
    () => computeArchLayout(boxes, archGraph, expanded),
    [boxes, archGraph, expanded],
  )

  // ── ELK is the layout the user sees: layered placement + orthogonal edge ──
  // routing on intra-module file→file edges. ELK has no sync API, so we compute
  // it in an effect and key each result to a cheap synchronous signature of the
  // layout inputs (geometry + expansion — NOT hover/selection, so those never
  // trigger a relayout). We only *use* an ELK result whose signature still
  // matches the current inputs; otherwise (mid-compute, after an expand/drag, or
  // on failure) we render the Dagre seed. Stable: instant paint, no stale-async
  // overwrite, Dagre as the crash fallback.
  const layoutSig = useMemo(() => (
    (archGraph ? `${archGraph.files?.length || 0}:${archGraph.flows?.length || 0}` : 'none') + '|' +
    boxes.map(b => `${b.id}@${Math.round(b.x)},${Math.round(b.y)},${b.w},${b.h}`).join(';') + '|' +
    [...expanded].sort().join(',')
  ), [boxes, archGraph, expanded])

  const [elkState, setElkState] = useState(null)   // { sig, layout|null }
  useEffect(() => {
    let alive = true
    computeArchLayoutElk(boxes, archGraph, expanded)
      .then(l => { if (alive) setElkState({ sig: layoutSig, layout: l }) })
      .catch(err => {
        if (alive) {
          console.warn('[openfde] ELK layout failed — using the Dagre seed.', err)
          setElkState({ sig: layoutSig, layout: null })
        }
      })
    return () => { alive = false }
  }, [layoutSig, boxes, archGraph, expanded])

  const elkReady = elkState && elkState.sig === layoutSig && elkState.layout
  const layout = elkReady ? elkState.layout : dagreLayout
  const routedEdges = elkReady ? (layout.routedEdges || null) : null
  const { nodes, effectiveBoxes, bounds } = layout
  const archSelId = archSel?.data?.id ?? null

  // Function-level dataflow arrows (Step 23/26): resolve each flow to the nearest
  // visible box; focused mode rolls unrelated cross-file flows up and dims them.
  // Focus = a selected file/function entity, else the single selected box.
  const focusId = archSelId
    || (selectedIds.size === 1 ? [...selectedIds][0] : null)

  // Story mode (Batch 5): derive the highlighted flow ids + per-flow step label,
  // and the node→step-number badge map from the story steps.
  const storyData = useMemo(() => {
    if (flowMode !== 'story' || !story?.steps?.length) return { ids: null, map: null, badges: {} }
    const ids = new Set(), map = {}, badges = {}
    for (const st of story.steps) {
      for (const fid of (st.flowIds || [])) { ids.add(fid); if (!(fid in map)) map[fid] = { order: st.order, label: st.label } }
      for (const nid of (st.nodeIds || [])) { if (!(nid in badges)) badges[nid] = st.order }
    }
    return { ids: ids.size ? ids : null, map, badges }
  }, [flowMode, story])

  const flowArrows = useMemo(
    () => computeFlowArrows(archGraph, layout, { mode: flowMode, focusId, storyFlowIds: storyData.ids, flowIdToStep: storyData.map, altitude }),
    [archGraph, layout, flowMode, focusId, storyData, altitude],
  )

  // Routed mode: replace the bezier/arc geometry of any flow arrow ELK actually
  // routed (intra-module file→file edges) with ELK's orthogonal polyline, keeping
  // the arrow's label / focus / opacity logic. Flow arrows ELK could not route
  // (cross-module, function-level, same-file arcs) keep their existing geometry.
  const renderFlowArrows = useMemo(() => {
    if (!routedEdges || !routedEdges.length) return flowArrows
    const byPair = new Map()
    for (const e of routedEdges) byPair.set(`${e.fromId}>${e.toId}`, e.points)
    return flowArrows.map(f => {
      const pts = byPair.get(`${f.fromId}>${f.toId}`)
      return pts ? { ...f, routedPoints: pts } : f
    })
  }, [flowArrows, routedEdges])

  // Two layers + a render budget: attention edges (focused / story) draw ON TOP
  // of boxes; ambient edges draw UNDER them, heaviest trunks first, capped — and
  // the cap is never silent (the budget chip names what it dropped).
  const FLOW_BUDGET = 140
  const { flowsOver, flowsUnder, flowOverflow } = useMemo(() => {
    const over = [], under = []
    for (const f of renderFlowArrows) (f.highlight || f.story ? over : under).push(f)
    under.sort((x, y) => (y.count || 0) - (x.count || 0))
    return { flowsOver: over, flowsUnder: under.slice(0, FLOW_BUDGET),
             flowOverflow: Math.max(0, under.length - FLOW_BUDGET) }
  }, [renderFlowArrows])

  // Story mode staged layout (Batch 5b): when a story is available, render it as
  // left-to-right phases instead of the nested file stack.
  const storyStage = useMemo(
    () => (flowMode === 'story' && story ? computeStoryLayout(story, archGraph) : null),
    [flowMode, story, archGraph],
  )

  // Resolve story badge node ids to on-canvas geometry.
  const storyBadges = useMemo(() => {
    const out = []
    for (const [nid, order] of Object.entries(storyData.badges)) {
      const n = layout.fnById?.[nid] || layout.fileById?.[nid]
        || (nodes.find(nn => nn.id === nid))
      if (n) out.push({ id: nid, order, x: n.x, y: n.y })
    }
    return out
  }, [storyData, layout, nodes])

  // Resolve WebXR badges (path → kind/label) to on-canvas geometry, the same way
  // story badges resolve: each flagged file → its file node → position. A badge
  // only renders for a file that's actually visible (an expanded module), so it's
  // never orphaned in empty space. Architecture metadata only — no runtime lens.
  const webxrBadgeNodes = useMemo(() => {
    if (!webxrBadges) return []
    const out = []
    for (const [id, badge] of Object.entries(badgeNodesById(webxrBadges))) {
      const n = layout.fileById?.[id]
      if (n) out.push({ id, ...badge, x: n.x, y: n.y })
    }
    return out
  }, [webxrBadges, layout])

  // Active viewport bounds (story stage overrides the nested layout's bounds).
  const viewBounds = storyStage ? storyStage.bounds : bounds

  const selectedIdsRef = useRef(selectedIds)
  const boxesRef = useRef(effectiveBoxes)
  const layoutRef = useRef(layout)
  const boundsRef = useRef(viewBounds)
  const hoverIdRef = useRef(null)   // function id currently under the pointer
  useEffect(() => { selectedIdsRef.current = selectedIds }, [selectedIds])
  useEffect(() => { boxesRef.current = effectiveBoxes; layoutRef.current = layout; boundsRef.current = viewBounds })

  // Escape cancels pending arrow
  useEffect(() => {
    if (!pendingArrow) return
    function onEscape(e) {
      if (e.key !== 'Escape') return
      const ix = interaction.current
      interaction.current = null
      setPendingArrow(null)
      try { svgRef.current?.releasePointerCapture(ix?.pointerId) } catch { /* already released */ }
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [pendingArrow])

  // Delete / Backspace removes the current selection (arrows + boxes). Guarded so
  // it never fires while editing a label/title or typing in any input.
  useEffect(() => {
    function onDelete(e) {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      if (editingBoxId || editOverlay) return
      const el = document.activeElement
      const tag = el && el.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (el && el.isContentEditable)) return
      const arrowIds = [...(selectedArrowIds || [])]
      const boxIds = [...(selectedIds || [])]
      if (!arrowIds.length && !boxIds.length) return
      e.preventDefault()
      arrowIds.forEach(id => dispatch({ type: 'DELETE_ARROW', id }))
      if (boxIds.length) dispatch({ type: 'DELETE_BOXES', ids: boxIds })
    }
    window.addEventListener('keydown', onDelete)
    return () => window.removeEventListener('keydown', onDelete)
  }, [selectedArrowIds, selectedIds, editingBoxId, editOverlay, dispatch])

  // Pointer → canvas coords. Robust to zoom + scroll: the SVG viewBox maps
  // bounds → element box, so we scale the screen delta by bounds/elementSize.
  function getSVGPos(e) {
    const r = svgRef.current.getBoundingClientRect()
    const b = boundsRef.current
    return {
      x: (e.clientX - r.left) * b.w / r.width,
      y: (e.clientY - r.top) * b.h / r.height,
    }
  }

  function findPortAtPos(pos, excludeBoxId) {
    const HIT_R = 12
    for (const box of boxesRef.current) {
      if (box.id === excludeBoxId) continue
      for (const port of ['n', 'e', 's', 'w']) {
        const pp = getPortPos(box, port)
        if (Math.hypot(pos.x - pp.x, pos.y - pp.y) <= HIT_R) {
          return { boxId: box.id, port }
        }
      }
    }
    return null
  }

  const openEdit = useCallback((boxId, field) => {
    const box = boxes.find(b => b.id === boxId)
    if (!box || !svgRef.current) return
    const r = svgRef.current.getBoundingClientRect()
    const sx = r.width / boundsRef.current.w
    const sy = r.height / boundsRef.current.h
    dispatch({ type: 'SET_EDITING', id: boxId, field })
    setEditOverlay({ boxId, field, clientX: r.left + box.x * sx, clientY: r.top + box.y * sy, w: box.w * sx, h: box.h * sy })
  }, [boxes, dispatch])

  // Inline arrow relationship editor (Step 26): double-click an arrow → textbox
  // at its midpoint to name the relationship between the two boxes.
  const [arrowEdit, setArrowEdit] = useState(null)
  const openArrowEdit = useCallback((arrowId) => {
    const arrow = arrows.find(a => a.id === arrowId)
    if (!arrow || !svgRef.current) return
    const from = boxesRef.current.find(b => b.id === arrow.fromBox)
    const to   = boxesRef.current.find(b => b.id === arrow.toBox)
    if (!from || !to) return
    const start = getPortPos(from, arrow.fromPort)
    const end   = getPortPos(to,   arrow.toPort)
    const mid   = getBezierMidpoint(start, arrow.fromPort, end, arrow.toPort)
    const r = svgRef.current.getBoundingClientRect()
    const sx = r.width / boundsRef.current.w
    const sy = r.height / boundsRef.current.h
    setArrowEdit({ arrowId, value: arrow.label || '', clientX: r.left + mid.x * sx, clientY: r.top + mid.y * sy })
  }, [arrows])

  function saveArrowEdit(arrowId, value) {
    if (value !== null) dispatch({ type: 'UPDATE_ARROW', id: arrowId, fields: { label: value.trim() } })
    setArrowEdit(null)
  }

  function stopEdit(boxId, field, value) {
    if (value !== null) dispatch({ type: 'UPDATE_BOX', id: boxId, fields: { [field]: value } })
    dispatch({ type: 'STOP_EDITING' })
    setEditOverlay(null)
  }

  function cancelInteraction(pointerId) {
    interaction.current = null
    setRubberBand(null)
    setPendingArrow(null)
    try { svgRef.current?.releasePointerCapture(pointerId) } catch { /* already released */ }
  }

  function handlePointerCancel(e) { cancelInteraction(e.pointerId) }
  function handleLostPointerCapture() {
    if (interaction.current) {
      interaction.current = null
      setRubberBand(null)
      setPendingArrow(null)
    }
  }

  // Resolve a nested node element (file / function) and its entity.
  function nestedEntity(el) {
    const kind = el.dataset.nodeKind
    const id = el.dataset.nodeId
    if (kind === 'file')     return ['file', layoutRef.current.fileById[id]?.file]
    if (kind === 'function') return ['function', layoutRef.current.fnById[id]?.fn]
    return [null, null]
  }

  // The module id under an event — its chevron, or its box chrome (collapsed
  // body / expanded header + background). A file/function node inside an expanded
  // module owns its own double-click, so it never counts as the module. This lets
  // double-click TOGGLE: expand a collapsed module, collapse an expanded one.
  function moduleIdAt(e) {
    const tog = e.target.closest?.('[data-expand-toggle]')
    if (tog && tog.dataset.nodeKind === 'module') return tog.dataset.nodeId
    const nodeEl = e.target.closest?.('[data-node-id]')
    if (nodeEl && (nodeEl.dataset.nodeKind === 'file' || nodeEl.dataset.nodeKind === 'function')) return null
    const boxEl = e.target.closest?.('[data-box-id]')
    if (boxEl) {
      const b = boxes.find(bx => bx.id === boxEl.dataset.boxId)
      // A module OR a built intent box (architecture-backed) drills in on double-click.
      if (b?.moduleId || isIntentDrillBox(b)) return boxEl.dataset.boxId
    }
    return null
  }

  function handlePointerDown(e) {
    if (e.button === 2) return
    if (contextMenu) { setContextMenu(null); return }
    if (editOverlay) return

    // ── Module drill-in: reliable single-vs-double click ──────────────────
    // Native dblclick is unreliable here because box pointerdown uses
    // setPointerCapture + preventDefault. So detect it ourselves: two
    // pointerdowns on the SAME module within 400ms = expand; one = select
    // (falls through to the normal selection logic below).
    if (activeTool === 'select') {
      const modId = moduleIdAt(e)
      if (modId) {
        const now = Date.now()
        const last = lastModDownRef.current
        if (last && last.id === modId && now - last.t < 400) {
          lastModDownRef.current = null
          onToggleExpand?.(modId, 'module')
          e.preventDefault()
          return
        }
        lastModDownRef.current = { id: modId, t: now }
      }
    }

    // ── Nested: expand toggle (chevron) ───────────────────────────────────
    const toggleEl = e.target.closest?.('[data-expand-toggle]')
    if (toggleEl && activeTool === 'select') {
      // Single click only selects/inspects. Double-click owns expansion so the
      // canvas does not unexpectedly drill in while the user is setting scope.
      if (toggleEl.dataset.nodeKind === 'module') {
        dispatch({ type: 'SELECT', id: toggleEl.dataset.nodeId })
        onSelectArchEntity?.(null, null)
      } else {
        const [kind, data] = nestedEntity(toggleEl)
        if (kind && data) onSelectArchEntity?.(kind, data)
      }
      e.preventDefault()
      return
    }
    // ── Nested: select a file / function box (no drag) ────────────────────
    const nodeEl = e.target.closest?.('[data-node-id]')
    if (nodeEl && activeTool === 'select') {
      const [kind, data] = nestedEntity(nodeEl)
      if (kind && data) onSelectArchEntity?.(kind, data)
      e.preventDefault()
      return
    }

    const resizeId   = e.target.dataset?.resizeId
    const portBoxId  = e.target.dataset?.portBoxId
    const portSide   = e.target.dataset?.port
    const arrowId = !resizeId && !portBoxId && e.target.dataset?.arrowId
    const boxId = !resizeId && !portBoxId && !arrowId && (
      e.target.dataset?.boxId || e.target.closest?.('[data-box-id]')?.dataset.boxId
    )
    const pos = getSVGPos(e)

    if (resizeId) {
      const box = boxes.find(b => b.id === resizeId)
      if (!box) return
      interaction.current = { mode: 'resizing', id: resizeId, startX: pos.x, startY: pos.y, origW: box.w, origH: box.h, pointerId: e.pointerId }
      svgRef.current.setPointerCapture(e.pointerId)
      e.preventDefault()
      return
    }

    if (portBoxId && portSide && (activeTool === 'arrow' || activeTool === 'sarrow')) {
      const srcBox = boxesRef.current.find(b => b.id === portBoxId)
      if (!srcBox) return
      // 'arrow' inherits the source box's style (existing behavior); 'sarrow' is
      // explicitly solid — the toolbar's Solid arrow tool.
      const arrowType = activeTool === 'sarrow' ? 'solid' : srcBox.type
      interaction.current = { mode: 'arrow-drawing', fromBox: portBoxId, fromPort: portSide, arrowType, pointerId: e.pointerId }
      setPendingArrow({ fromBox: portBoxId, fromPort: portSide, curX: pos.x, curY: pos.y, arrowType })
      svgRef.current.setPointerCapture(e.pointerId)
      e.preventDefault()
      return
    }

    if (arrowId && activeTool === 'select') {
      dispatch({ type: 'SELECT_ARROW', id: arrowId })
      onSelectArchEntity?.(null, null)
      e.preventDefault()
      return
    }

    if (boxId) {
      if (activeTool === 'select') {
        onSelectArchEntity?.(null, null) // selecting a module clears file/function inspector
        const ids = selectedIdsRef.current
        let idsToMove
        if (e.shiftKey) {
          dispatch({ type: 'TOGGLE_SELECT', id: boxId })
          idsToMove = ids.has(boxId) ? [...ids].filter(i => i !== boxId) : [...ids, boxId]
        } else if (ids.has(boxId)) {
          idsToMove = [...ids]
        } else {
          dispatch({ type: 'SELECT', id: boxId })
          idsToMove = [boxId]
        }
        // Origins use *effective* positions so grabbing a repacked (expanded)
        // box doesn't jump; when nothing is expanded these equal persisted x/y.
        const origins = {}
        effectiveBoxes.forEach(b => { origins[b.id] = { x: b.x, y: b.y } })
        interaction.current = { mode: 'dragging', startX: pos.x, startY: pos.y, origins, idsToMove, moved: false, pointerId: e.pointerId }
        svgRef.current.setPointerCapture(e.pointerId)
        e.preventDefault()
      }
      return
    }

    if (activeTool === 'dotted' || activeTool === 'solid') {
      interaction.current = { mode: 'creating', boxType: activeTool, startX: pos.x, startY: pos.y, pointerId: e.pointerId }
      svgRef.current.setPointerCapture(e.pointerId)
      e.preventDefault()
      return
    }

    if (activeTool === 'select') {
      if (!e.shiftKey) { dispatch({ type: 'CLEAR_SELECTION' }); onSelectArchEntity?.(null, null); setLensFocusId(null) }
      interaction.current = { mode: 'rubber-band', startX: pos.x, startY: pos.y, additive: e.shiftKey, pointerId: e.pointerId }
      svgRef.current.setPointerCapture(e.pointerId)
      e.preventDefault()
    }
  }

  // Centralized function-hover detection — reliable on SVG nodes (doesn't rely
  // on per-<g> mouseenter/leave, which is flaky for grouped SVG elements).
  function updateHover(e) {
    const el = e.target.closest?.('[data-node-kind="function"]')
    const id = el?.dataset?.nodeId || null
    if (id === hoverIdRef.current) return        // no transition → no re-render
    hoverIdRef.current = id
    if (!id) { setHoverFn(null); return }
    const fn = layoutRef.current.fnById?.[id]?.fn
    if (!fn) { setHoverFn(null); return }
    // Offset down-right of the pointer so the card doesn't cover the target.
    setHoverFn({ fn, left: e.clientX + 16, top: e.clientY + 18 })
  }

  function clearHover() {
    if (hoverIdRef.current !== null) { hoverIdRef.current = null; setHoverFn(null) }
  }

  function handlePointerLeave() {
    clearHover()
  }

  function handlePointerMove(e) {
    const ix = interaction.current
    if (!ix) { updateHover(e); return }
    // Suppress the hover card during any active drag / resize / arrow draw.
    clearHover()
    const pos = getSVGPos(e)

    if (ix.mode === 'dragging') {
      const dx = pos.x - ix.startX
      const dy = pos.y - ix.startY
      if (!ix.moved && Math.abs(dx) + Math.abs(dy) < 3) return
      ix.moved = true
      const positions = {}
      ix.idsToMove.forEach(id => {
        const o = ix.origins[id]
        if (o) positions[id] = { x: o.x + dx, y: o.y + dy }
      })
      dispatch({ type: 'SET_POSITIONS', positions })
      return
    }

    if (ix.mode === 'resizing') {
      dispatch({ type: 'SET_SIZE', id: ix.id, w: ix.origW + (pos.x - ix.startX), h: ix.origH + (pos.y - ix.startY) })
      return
    }

    if (ix.mode === 'rubber-band') {
      const x = Math.min(pos.x, ix.startX)
      const y = Math.min(pos.y, ix.startY)
      setRubberBand({ x, y, w: Math.abs(pos.x - ix.startX), h: Math.abs(pos.y - ix.startY) })
      return
    }

    if (ix.mode === 'arrow-drawing') {
      setPendingArrow(prev => prev ? { ...prev, curX: pos.x, curY: pos.y } : null)
    }
  }

  function handlePointerUp(e) {
    const ix = interaction.current
    interaction.current = null
    try { svgRef.current?.releasePointerCapture(e.pointerId) } catch { /* already released */ }
    if (!ix) return
    const pos = getSVGPos(e)

    if (ix.mode === 'creating') {
      const dx = pos.x - ix.startX
      const dy = pos.y - ix.startY
      const x = Math.abs(dx) < 10 ? ix.startX - DEFAULT_W / 2 : Math.min(pos.x, ix.startX)
      const y = Math.abs(dy) < 10 ? ix.startY - DEFAULT_H / 2 : Math.min(pos.y, ix.startY)
      dispatch({ type: 'CREATE_BOX', x: Math.max(0, x), y: Math.max(0, y), boxType: ix.boxType })
      setActiveTool('select')
      return
    }

    if (ix.mode === 'rubber-band') {
      setRubberBand(null)
      const rb = { x: Math.min(pos.x, ix.startX), y: Math.min(pos.y, ix.startY), w: Math.abs(pos.x - ix.startX), h: Math.abs(pos.y - ix.startY) }
      if (rb.w > 4 && rb.h > 4) {
        const inside = boxesRef.current
          .filter(b => b.x + b.w > rb.x && b.x < rb.x + rb.w && b.y + b.h > rb.y && b.y < rb.y + rb.h)
          .map(b => b.id)
        if (ix.additive) {
          dispatch({ type: 'SELECT_MANY', ids: [...selectedIdsRef.current, ...inside.filter(id => !selectedIdsRef.current.has(id))] })
        } else {
          dispatch({ type: 'SELECT_MANY', ids: inside })
        }
      }
      return
    }

    if (ix.mode === 'arrow-drawing') {
      setPendingArrow(null)
      const target = findPortAtPos(pos, ix.fromBox)
      if (target) {
        dispatch({ type: 'CREATE_ARROW', fromBox: ix.fromBox, fromPort: ix.fromPort, toBox: target.boxId, toPort: target.port, arrowType: ix.arrowType })
      }
    }
  }

  function handleDblClick(e) {
    // Double-click an arrow → edit its relationship label inline.
    const arrowId = e.target.dataset?.arrowId || e.target.closest?.('[data-arrow-id]')?.dataset.arrowId
    if (arrowId) { openArrowEdit(arrowId); return }
    // Double-click a file box → toggle its functions. (Module drill-in is owned
    // by the manual double-click detection in handlePointerDown — keeping it out
    // of here avoids a double-toggle that cancels itself out to a no-op.)
    const nodeEl = e.target.closest?.('[data-node-id]')
    if (nodeEl) {
      if (nodeEl.dataset.nodeKind === 'file') { onToggleExpand?.(nodeEl.dataset.nodeId, 'file') }
      return
    }
    const boxId = e.target.dataset?.boxId || e.target.closest?.('[data-box-id]')?.dataset.boxId
    if (!boxId) return
    const box = boxes.find(b => b.id === boxId)
    if (!box) return
    if (box.moduleId) return   // module expand handled in handlePointerDown
    const pos = getSVGPos(e)
    const field = (pos.y - box.y) < 40 ? 'title' : 'prompt'
    openEdit(boxId, field)
  }

  function handleContextMenu(e) {
    e.preventDefault()
    // Right-click a FUNCTION node → open it in an editor directly (no extra menu).
    // Functions use data-node-id, not data-box-id; lens rings handle their own.
    const fnEl = e.target.closest?.('[data-node-kind="function"]')
    if (fnEl && onOpenEditor) {
      const fn = layoutRef.current.fnById?.[fnEl.dataset.nodeId]?.fn
      if (fn) { setContextMenu(null); onOpenEditor({ file: fn.path, function: fn.name, line: fn.line }); return }
    }
    const boxId = e.target.dataset?.boxId || e.target.closest?.('[data-box-id]')?.dataset.boxId
    if (!boxId) return
    const ids = selectedIdsRef.current
    const targetIds = ids.has(boxId) && ids.size > 1 ? [...ids] : [boxId]
    if (!ids.has(boxId)) dispatch({ type: 'SELECT', id: boxId })
    setContextMenu({ x: e.clientX, y: e.clientY, targetIds })
  }

  function handleWheel(e) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault()
      setScale(s => clamp(+(s * (e.deltaY < 0 ? 1.1 : 0.9)).toFixed(3), 0.3, 2.5))
    }
    // plain wheel → native scroll (pan)
  }

  const cursor = activeTool === 'select' ? 'default' : 'crosshair'
  const isEmpty = boxes.length === 0
  const showPorts = activeTool === 'arrow' || activeTool === 'sarrow'

  // Live-run overlay: pulse the most-specific *visible* node per scoped target.
  const nodeStates = runNodeStates || {}
  const edgeStates = runEdgeStates || {}
  // Adaptive glow: ride the lowest *visible* node — a collapsed module, or a
  // file/function inside an expanded one. Only the big expanded-module container
  // is skipped (its children carry the activity instead).
  const runRings = storyStage ? [] : computeRunRings(nodeStates, nodes, layout).filter(r => {
    const n = nodes.find(nn => nn.id === r.id)
    return !(n && n.expanded)
  })

  // ── Watch Any Agent: calm ambient ring on each box an external editor just
  //    touched (file_activity). Keyed by box id; geometry straight from nodes. ──
  const watchRings = useMemo(() => {
    // While the repair ring owns a function's story, the generic file-level
    // edit glow for THAT file is suppressed — the edit is happening inside the
    // function, and the phase ring (red→orange→green) tells it truthfully.
    const ids = (watchBoxIds ? Object.keys(watchBoxIds) : [])
      .filter(id => !(failFocus && (id === failFocus.fileId || id === failFocus.fnId)))
    if (!ids.length || !nodes.length) return []
    // watchBoxIds: fileNodeId (box:file:<path>) -> 'active'|'trail'. computeRunRings
    // resolves each to the lowest VISIBLE node — the file box when its module is
    // expanded, the module box when collapsed — so the glow is file-level on auto-
    // expand and module-level otherwise.
    const states = {}
    for (const [id, tier] of ids.map(id => [id, watchBoxIds[id]])) {
      states[id] = tier === 'trail' ? 'wtrail' : 'wactive'
    }
    return computeRunRings(states, nodes, layout)
  }, [watchBoxIds, nodes, layout, failFocus])
  const watching = !!watchBoxIds

  // ── Live follow: pan the camera to center the file an agent is actively editing
  //    (the 'wactive' ring). Only re-centers when the active node CHANGES — so it
  //    follows the agent file-to-file but never fights the user's own scroll while
  //    they linger on one file. Debounced so a burst of saves doesn't jitter. The
  //    Watch glow is independent of this — turning follow off never stops the glow.
  useEffect(() => {
    const f = followRef.current
    if (!liveFollow) { f.id = null; return undefined }
    const active = watchRings.find(r => r.status === 'wactive')
    const el = scrollRef.current
    if (!active || !el) return undefined
    if (f.id === active.id) return undefined          // already centered on this file
    clearTimeout(f.timer)
    const g = active.geom
    f.timer = setTimeout(() => {
      f.id = active.id
      const cx = (g.x + g.w / 2) * scale
      const cy = (g.y + g.h / 2) * scale
      el.scrollTo({
        left: Math.max(0, cx - el.clientWidth / 2),
        top: Math.max(0, cy - el.clientHeight / 2),
        behavior: 'smooth',
      })
    }, 240)
    return () => clearTimeout(f.timer)
  }, [liveFollow, watchRings, scale])

  // ── Watch focus: center the camera on the TOUCHED file/function (not the module container) ──
  //    Resolves the most specific node that's actually laid out (function → file → module) and
  //    re-centers as the expansion settles, so the edit target is on-screen even before the
  //    function box finishes laying out. Independent of liveFollow's ring-following. Guarded so it
  //    scrolls only when the resolved target CHANGES (a new edit, or the function becoming visible).
  useEffect(() => {
    if (!watchFocus || !liveFollow) return undefined   // respect the Live · following toggle
    const ft = focusTargetRef.current
    const el = scrollRef.current
    if (!el) return undefined
    // Resolve only to a LAID-OUT file/function (moduleId=null) — never yank the camera to the
    // module's centre (a middle file) before the touched node lays out, nor on its later collapse.
    const id = watchFocusTargetId(watchFocus.file, watchFocus.fnName, layout.fnById, layout.fileById, null)
    if (!id) return undefined
    // Center on the node's ACTUAL rendered position. The React layout object lags between the
    // compact dagre seed and the final ELK pass (centering off it scrolled to the seed's y and
    // clamped to 0), but the DOM always reflects what's painted — so read the node's box from the
    // DOM and re-center as it settles. The id contains ':' and '/', which are fine inside a quoted
    // attribute selector.
    const nodeEl = el.querySelector(`[data-node-id="${id}"]`)
    if (!nodeEl) return undefined   // not in the DOM yet → wait for the next layout commit
    const nr = nodeEl.getBoundingClientRect(), er = el.getBoundingClientRect()
    const cx = (nr.left - er.left) + el.scrollLeft + nr.width / 2    // absolute content coords —
    const cy = (nr.top - er.top) + el.scrollTop + nr.height / 2      // invariant to our own scroll
    const stamp = String(watchFocus.ts)
    if (ft.stamp !== stamp) { ft.stamp = stamp; ft.posKey = null }
    const posKey = `${id}:${Math.round(cy)}`     // re-center when the node MOVES (dagre→ELK); else skip
    if (ft.posKey === posKey) return undefined
    ft.posKey = posKey
    el.scrollTo({
      left: Math.max(0, cx - el.clientWidth / 2),
      top: Math.max(0, cy - el.clientHeight / 2),
      behavior: 'smooth',
    })
    return undefined
  }, [watchFocus, layout, nodes, scale, liveFollow])

  // Show →: smooth-scroll the failing function into view once its geometry
  // exists (expansion + ELK settle re-run this via the layout dep).
  useEffect(() => {
    if (!failFocus) { failScrollRef.current = null; return }
    // Scroll ONCE per focused failure — re-running on every layout change made
    // boxes snap back mid-drag while the hatch was open.
    const key = failFocus.fnId || failFocus.fileId
    if (failScrollRef.current === key) return
    const g = layout.fnById?.[failFocus.fnId] || layout.fileById?.[failFocus.fileId]
    const el = scrollRef.current
    if (!g || !el) return
    failScrollRef.current = key
    el.scrollTo({ left: Math.max(0, (g.x + g.w / 2) * scale - el.clientWidth * 0.38),
                  top: Math.max(0, (g.y + g.h / 2) * scale - el.clientHeight * 0.5),
                  behavior: 'smooth' })
  }, [failFocus, scale, layout])

  // Resolve a primaryPath node to its on-canvas FUNCTION box — line+name aware
  // (pickPrimaryFn), then the rendered geometry from the layout. Shared by the
  // lens render and the viewport-fit effect so they always agree.
  const flowFnBox = useCallback((n) => {
    if (!n || !n.file) return null
    if (n.function) {
      const base = String(n.function).split('.').pop()
      const direct = layout.fnById?.[`box:function:${n.file}:${n.function}`]
          || layout.fnById?.[`box:function:${n.file}:${base}`]
      if (direct) return direct
    }
    const pick = pickPrimaryFn(archGraph?.functions, n)
    return pick ? layout.fnById?.[`box:function:${n.file}:${pick.name}`] : null
  }, [layout, archGraph])

  // ── Failure-flow VIEWPORT FIT. Two-phase so the scroll lands after the scale
  //    settles. Phase 1 (lens active + path function boxes exist): if BOTH path
  //    endpoints fit in the free area at a legible scale (≥ 0.45) frame the whole
  //    path; otherwise the functions are too far apart on canvas to show legibly
  //    together, so frame the FAILURE function (the aha) at a readable scale with
  //    the incoming arrow/source reading in from the left. Phase 2 scrolls once
  //    `scale` matches. Once per failure fingerprint; left-biased for the hatch. ──
  const FLOW_FIT_FOCUS = 0.7
  const flowFitRef = useRef(null)
  const pendingFitRef = useRef(null)       // {scale, cx, cy, anchorX}
  useEffect(() => {
    if (!flowLens?.artifact) { flowFitRef.current = null; pendingFitRef.current = null; return }
    const art = flowLens.artifact
    const fp = art.fingerprint || 'lens'
    if (flowFitRef.current === fp) return
    const ppath = art.primaryPath || []
    const failNode = ppath.find(n => n.role === 'failure') || ppath[ppath.length - 1]
    const failBox = failNode && flowFnBox(failNode)
    const el = scrollRef.current
    if (!failBox || !el) return            // wait for expansion + layout to settle
    flowFitRef.current = fp
    const rects = ppath.map(flowFnBox).filter(Boolean)
    const minX = Math.min(...rects.map(r => r.x)), maxX = Math.max(...rects.map(r => r.x + r.w))
    const minY = Math.min(...rects.map(r => r.y)), maxY = Math.max(...rects.map(r => r.y + r.h))
    const usableW = Math.max(360, el.clientWidth - 440)   // keep the right clear for the hatch
    const bothFit = clamp(Math.min(usableW / ((maxX - minX) + 240),
                                   el.clientHeight / ((maxY - minY) + 240)), 0.2, 1.1)
    let scl, cx, cy, anchorX
    if (rects.length >= 2 && bothFit >= 0.45) {
      // Both functions fit legibly → frame the whole path, centred in the free area.
      scl = bothFit; cx = (minX + maxX) / 2; cy = (minY + maxY) / 2; anchorX = usableW / 2
    } else {
      // Too far apart → centre the FAILURE function; the labelled arrow + source
      // read in from the left (anchor the fail box at ~58% of the free area).
      scl = FLOW_FIT_FOCUS
      cx = failBox.x + failBox.w / 2; cy = failBox.y + failBox.h / 2; anchorX = usableW * 0.58
    }
    pendingFitRef.current = { scale: scl, cx, cy, anchorX }
    if (Math.abs(scale - scl) < 0.001) {
      const p = pendingFitRef.current; pendingFitRef.current = null
      el.scrollTo({ left: Math.max(0, p.cx * scl - p.anchorX),
                    top: Math.max(0, p.cy * scl - el.clientHeight / 2), behavior: 'smooth' })
    } else {
      setScale(scl)
    }
  }, [flowLens, layout, archGraph, flowFnBox, scale])

  // Phase 2: scale settled to the pending fit — the scroll math is now exact.
  useEffect(() => {
    const p = pendingFitRef.current
    const el = scrollRef.current
    if (!p || !el || Math.abs(scale - p.scale) > 0.001) return
    pendingFitRef.current = null
    requestAnimationFrame(() => {
      el.scrollTo({ left: Math.max(0, p.cx * p.scale - p.anchorX),
                    top: Math.max(0, p.cy * p.scale - el.clientHeight / 2), behavior: 'smooth' })
    })
  }, [scale])

  // ── Spotlight geometry: which visible boxes are lit (hold the concept / were
  //    touched by the commit) and which are amber (a tethered concept the commit
  //    only partially covered — "you changed 1 of 4 places this lives"). ────────
  const spot = useMemo(() => {
    if (!spotlight || !nodes.length) return null
    // Focused on one concept → its changed files are lit, related files amber.
    const litFiles = spotlight.focus ? spotlight.focus.changedFiles : spotlight.files
    const amberFiles = spotlight.focus ? spotlight.focus.relatedFiles : spotlight.amberFiles
    if (!litFiles?.length && !amberFiles?.length) return null
    const litWant = new Set((litFiles || []).map(baseName))
    const amberWant = new Set((amberFiles || []).map(baseName))
    const geom = (n) => ({ id: n.id, x: n.x, y: n.y, w: n.w, h: n.h,
                           cx: n.x + n.w / 2, cy: n.y + n.h / 2 })
    const lit = [], amber = []
    for (const n of nodes) {
      const paths = nodeFilePaths(n)
      if (paths.some(p => litWant.has(baseName(p)))) lit.push(geom(n))
      else if (amberWant.size && paths.some(p => amberWant.has(baseName(p)))) amber.push(geom(n))
    }
    const ref = lit.length ? lit : amber
    if (!ref.length) return { lit: [], amber: [], centroid: null }
    const cx = ref.reduce((s, r) => s + r.cx, 0) / ref.length
    const cy = ref.reduce((s, r) => s + r.cy, 0) / ref.length
    return { lit, amber, centroid: { x: cx, y: cy } }
  }, [spotlight, nodes])

  // Esc clears the spotlight.
  useEffect(() => {
    if (!spotlight) return undefined
    const onKey = (e) => { if (e.key === 'Escape') onClearSpotlight?.() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [spotlight, onClearSpotlight])

  // Spotlight label lines (concept name + count, or commit + affected concepts).
  const spotLabel = spotlightLabel(spotlight)

  return (
    <div className="wb-canvas-root">
      {flowOverflow > 0 && (
        <div className="wb-flow-budget" title="Ambient edges are capped for legibility — selection always shows everything it touches">
          +{flowOverflow} ambient flows beyond budget · select a node to focus
        </div>
      )}
      {flowLens?.artifact && (
        <div className="flow-lens-strip">
          <span className="flow-lens-strip-title">Focus</span>
          <span className="flow-lens-strip-summary">{flowLens.artifact.summary}</span>
          <span className="flow-lens-strip-src">{flowLens.artifact.source}</span>
          {onRegenFlowLens && (
            <button className="flow-lens-strip-btn" onClick={onRegenFlowLens} disabled={flowLens.busy}
                    title="Re-derive the flow and re-humanize the labels">
              {flowLens.busy ? 'Re-tracing…' : '↻ Regenerate'}
            </button>
          )}
          <button className="flow-lens-strip-btn" onClick={onExitFlowLens}>Exit lens ✕</button>
        </div>
      )}
      <div className="wb-canvas-scroll" ref={scrollRef} onWheel={handleWheel}>
        <svg
          ref={svgRef}
          width={viewBounds.w * scale}
          height={viewBounds.h * scale}
          viewBox={`0 0 ${viewBounds.w} ${viewBounds.h}`}
          style={{ display: 'block', cursor }}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerLeave={handlePointerLeave}
          onPointerCancel={handlePointerCancel}
          onLostPointerCapture={handleLostPointerCapture}
          onDoubleClick={handleDblClick}
          onContextMenu={handleContextMenu}
        >
          <defs>
            <pattern id="dot-grid" x="0" y="0" width="24" height="24" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="0.8" fill="var(--border)" />
            </pattern>
            <marker id="arrowhead-dotted" viewBox="0 0 6 4" refX="6" refY="2" markerWidth="6" markerHeight="4" orient="auto">
              <path d="M 0 0 L 6 2 L 0 4 z" fill="#4a9eff" />
            </marker>
            <marker id="arrowhead-solid" viewBox="0 0 6 4" refX="6" refY="2" markerWidth="6" markerHeight="4" orient="auto">
              <path d="M 0 0 L 6 2 L 0 4 z" fill="#3dba6e" />
            </marker>
            <marker id="arrowhead-failed" viewBox="0 0 6 4" refX="6" refY="2" markerWidth="6" markerHeight="4" orient="auto">
              <path d="M 0 0 L 6 2 L 0 4 z" fill="#e33333" />
            </marker>
            <marker id="arrowhead-flow" viewBox="0 0 6 4" refX="6" refY="2" markerWidth="5" markerHeight="3.5" orient="auto">
              <path d="M 0 0 L 6 2 L 0 4 z" fill="var(--accent)" />
            </marker>
          </defs>
          <rect x="0" y="0" width={viewBounds.w} height={viewBounds.h} fill="url(#dot-grid)" />

          {/* Ambient dependency trunks — UNDER the boxes: weighted module↔module
              aggregates (and dimmed leftovers). The signal layer, not the noise.
              While the failure-flow lens is active it owns the visual field —
              every ambient arrow is hidden so the ONLY arrow is the lens edge. */}
          {!storyStage && !flowLens?.artifact && (
            <g pointerEvents="none">
              {flowsUnder.map(flow => <FlowArrow key={flow.id} flow={flow} />)}
            </g>
          )}

          {storyStage ? (
            <StoryStage stage={storyStage}
              onSelectFn={n => { if (n.fn) onSelectArchEntity?.('function', n.fn) }}
              selectedId={archSelId} />
          ) : (
            <>
              {/* Arrows behind boxes (anchored to effective/expanded geometry).
                  When a module is expanded its function-level flow (purple)
                  arrows resolve precisely into the box, so the coarse module
                  (blue) arrow touching it is a duplicate — hide it. Hidden
                  entirely while the failure lens owns the field. */}
              {!flowLens?.artifact && arrows
                .filter(arrow => !(expanded.has(arrow.fromBox) || expanded.has(arrow.toBox)))
                .map(arrow => (
                  <Arrow key={arrow.id} arrow={arrow} boxes={effectiveBoxes}
                    selected={selectedArrowIds.has(arrow.id)} runStatus={edgeStates[arrow.id]} />
                ))}

              {pendingArrow && <PendingArrow pendingArrow={pendingArrow} boxes={effectiveBoxes} />}

              {/* Module nodes — collapsed (CanvasBox) or expanded (nested) */}
              {nodes.map(node => {
                if (node.expanded) return (
                  <ExpandedModule key={node.id} node={node}
                    selected={selectedIds.has(node.id)} archSelId={archSelId} />
                )
                // A built intent box carries a fn-count summary on its collapsed badge; once
                // drillable it expands IN PLACE like a module (chevron + double-click → nested
                // files/functions), so the bespoke "▾ became" card is gone — one drill-in pattern.
                const sym = node.box?.kind === 'intent' ? intentSymbols(node.box, archGraph) : null
                return (
                  <g key={node.id}>
                    <CanvasBox
                      box={{ ...node.box, x: node.x, y: node.y, w: node.w, h: node.h }}
                      selected={selectedIds.has(node.id)}
                      isEditing={editingBoxId === node.id}
                      editingField={editingField}
                      showPorts={showPorts}
                      implFnCount={sym?.fnCount || 0}
                    />
                    {node.drillable && (
                      <Chevron x={node.x + node.w - 20} y={node.y + 8} open={false} nodeId={node.id} kind="module" />
                    )}
                  </g>
                )
              })}

              {/* Attention edges — the focused / story flows, ON TOP of boxes so
                  the selected node's 1-hop detail reads; non-interactive so they
                  never intercept port clicks. Suppressed under the failure lens. */}
              {!flowLens?.artifact && (
                <g pointerEvents="none">
                  {flowsOver.map(flow => <FlowArrow key={flow.id} flow={flow} />)}
                </g>
              )}

              {/* Story-mode step badges (Batch 5) — numbered, non-interactive. */}
              <g pointerEvents="none">
                {storyBadges.map(b => (
                  <g key={`badge-${b.id}`}>
                    <circle cx={b.x + 9} cy={b.y + 9} r={9} fill="var(--accent)" stroke="var(--bg)" strokeWidth={1.5} />
                    <text x={b.x + 9} y={b.y + 12.5} textAnchor="middle" fontSize={10} fontWeight={700}
                      fill="#fff" fontFamily="inherit">{b.order}</text>
                  </g>
                ))}
              </g>

              {/* WebXR architecture badges — a small calm tag on the top edge of any
                  file the WebXR pack flagged (XR entrypoint / 3D asset). Read-only:
                  no glow, no animation, no arrows. Auto-sized HTML chip so the label
                  never clips; sits above the box so it never covers the filename. */}
              <g pointerEvents="none">
                {webxrBadgeNodes.map(b => (
                  <foreignObject key={`xr-${b.id}`} x={b.x} y={b.y - 18} width={140} height={16}>
                    <div xmlns="http://www.w3.org/1999/xhtml" className={`xr-badge ${b.kind}`} title={b.label}>
                      {b.label}
                    </div>
                  </foreignObject>
                ))}
              </g>
            </>
          )}

          {/* Live-run activity rings (pulse / passed / failed) */}
          {runRings.map(r => (
            <rect key={`ring-${r.id}`} className={`run-ring run-${r.status}`}
              x={r.geom.x - 7} y={r.geom.y - 7} width={r.geom.w + 14} height={r.geom.h + 14}
              rx={14} fill="none" pointerEvents="none" />
          ))}

          {/* Failure-flow LENS — a causal SENTENCE, not a graph: the distilled
              primaryPath (funcA → funcB → ✕ failure) drawn function-to-function.
              Only the path FUNCTIONS get rings (red = failure, orange = path);
              their containing FILE boxes stay bright (mask holes) but unringed —
              no giant file-level red ring. A name chip appears beside a node
              ONLY when its function box can't be rendered. Everything else dims. */}
          {flowLens?.artifact && (() => {
            const art = flowLens.artifact
            let ppath = art.primaryPath || []
            let pedges = art.primaryEdges || []
            // Legacy / degenerate artifact (no primaryPath): distill one here so
            // the path view still works — distinct files, last node = failure.
            if (!ppath.length && (art.nodes || []).length) {
              const seen = new Set(); const order = []
              for (const n of (art.nodes || [])) {
                if (!n.file) continue
                const k = `${n.file}::${n.label}`
                if (seen.has(k)) continue
                seen.add(k); order.push({ id: n.id, file: n.file, function: n.label, line: n.line })
              }
              order.forEach((o, j) => { o.role = j === order.length - 1 ? 'failure' : (j === 0 ? 'source' : 'step') })
              ppath = order
              pedges = order.slice(1).map((b, i) => ({ from: order[i].id, to: b.id, label: `calls ${b.function}` }))
            }
            if (!ppath.length) return null

            const fnBoxFor = flowFnBox   // shared line+name resolver (pickPrimaryFn)
            // A node is 'selected' when its function is open in an editor (hatch or
            // a trail editor) — keyed by file::baseName to bridge bare vs qualified.
            const isOpen = (n) => !!(openFlowFns && openFlowFns.has(`${n.file}::${String(n.function || '').split('.').pop()}`))
            const fileBoxFor = (n) => n.file ? layout.fileById?.[`box:file:${n.file}`] : null
            const moduleBoxFor = (file) => file ? nodes.find(m => {
              const lp = m.box?.linkedPath
              return lp && (file === lp || file.startsWith(`${lp}/`))
            }) : null

            const placed = {}              // path node id → { rect, role, ring|chip, label, sub }
            const fileHoles = {}           // path file/module boxes → bright (mask hole), NO ring
            const chipStack = {}
            ppath.forEach((n) => {
              const fnBox = fnBoxFor(n)
              const fileBox = fileBoxFor(n)
              // Keep the file bright whenever it's on canvas; only fall through to
              // the MODULE box when the file box itself isn't rendered.
              const modBox = !fileBox ? moduleBoxFor(n.file) : null
              if (fileBox) fileHoles[fileBox.id || `${fileBox.x}:${fileBox.y}`] = fileBox
              else if (modBox) fileHoles[modBox.id || `${modBox.x}:${modBox.y}`] = modBox
              if (fnBox) {
                // The function box IS the node — ring it directly (red/orange).
                placed[n.id] = { rect: { x: fnBox.x, y: fnBox.y, w: fnBox.w, h: fnBox.h },
                                 role: n.role, label: n.function, file: n.file, line: n.line, selected: isOpen(n), ring: true }
              } else {
                // Function box not laid out → fallback chip beside its file/module
                // header (the ONLY time a detached label appears).
                const host = fileBox || modBox
                if (host) {
                  fileHoles[host.id || `${host.x}:${host.y}`] = host
                  const k = host.id || `${host.x}:${host.y}`
                  const stack = chipStack[k] || 0; chipStack[k] = stack + 1
                  const cw = Math.max(120, n.function.length * 7.2 + 24)
                  placed[n.id] = { rect: { x: host.x + 4 + stack * 18, y: host.y - 42 - stack * 40, w: cw, h: 34 },
                                   role: n.role, label: n.function, file: n.file, line: n.line, selected: isOpen(n), chip: true,
                                   sub: `${n.file || ''}${n.line ? ':' + n.line : ''}` }
                } else {
                  const anchor = layout.fnById?.[failFocus?.fnId] || layout.fileById?.[failFocus?.fileId]
                  const bx = anchor ? anchor.x + anchor.w + 110 : 240
                  const by = anchor ? anchor.y - 30 : 240
                  const idx = Object.keys(placed).length
                  placed[n.id] = { rect: { x: bx, y: by + idx * 74, w: 196, h: 44 },
                                   role: n.role, label: n.function, file: n.file, line: n.line, selected: isOpen(n), chip: true,
                                   sub: `${n.file || ''}${n.line ? ':' + n.line : ''}` }
                }
              }
            })
            const ringCls = (p) => `flow-lens-lit${p.role === 'failure' ? ' fail' : ''}${p.selected ? ' selected' : ''}`
            const pillCls = (p) => `flow-lens-pill${p.role === 'failure' ? ' fail' : ''}${p.selected ? ' selected' : ''}`
            const mid = (r) => ({ cx: r.x + r.w / 2, cy: r.y + r.h / 2 })
            const holes = [...Object.values(fileHoles), ...Object.values(placed).map(p => p.rect)]
            return (
              <g className="flow-lens-layer" pointerEvents="none">
                <defs>
                  <mask id="flow-lens-mask">
                    <rect x="0" y="0" width={viewBounds.w} height={viewBounds.h} fill="white" />
                    {holes.map((r, i) => (
                      <rect key={`fl-hole-${i}`} x={r.x - 10} y={r.y - 10}
                        width={r.w + 20} height={r.h + 20} rx={16} fill="black" />
                    ))}
                  </mask>
                </defs>
                <rect className="flow-lens-dim" x="0" y="0" width={viewBounds.w} height={viewBounds.h}
                  fill="var(--bg)" mask="url(#flow-lens-mask)" />
                {/* Function rings — the WHOLE ringed box IS the hit target (visible ==
                    clickable, no invisible overlay). LEFT-click focuses this node's
                    arrows; RIGHT-click opens its editor. Generous padding + CSS hover. */}
                {Object.entries(placed).filter(([, p]) => p.ring).map(([id, p], i) => (
                  <rect key={`fl-ring-${i}`} className={`${ringCls(p)}${lensFocusId === id ? ' focused' : ''}`}
                    x={p.rect.x - 9} y={p.rect.y - 9} width={p.rect.w + 18} height={p.rect.h + 18}
                    rx={14} fill="transparent"
                    pointerEvents={onOpenEditor ? 'all' : 'none'}
                    onPointerDown={ev => ev.stopPropagation()}
                    onClick={ev => { ev.stopPropagation(); setLensFocusId(cur => cur === id ? null : id) }}
                    onContextMenu={onOpenEditor ? (ev) => { ev.preventDefault(); ev.stopPropagation(); onOpenEditor({ file: p.file, function: p.label, line: p.line, role: p.role }) } : undefined}>
                    <title>Right-click to open editor · left-click to focus arrows</title>
                  </rect>
                ))}
                {/* Fallback chips — only when the function box can't render. The pill
                    is the hit target (labels are pointer-transparent). Same contract:
                    left-click focuses arrows, right-click opens the editor. */}
                {Object.entries(placed).filter(([, p]) => p.chip).map(([id, p], i) => (
                  <g key={`fl-chip-${i}`}>
                    <rect className={`${pillCls(p)}${lensFocusId === id ? ' focused' : ''}`} x={p.rect.x} y={p.rect.y} width={p.rect.w} height={p.rect.h} rx={10}
                      pointerEvents={onOpenEditor ? 'all' : 'none'}
                      onPointerDown={ev => ev.stopPropagation()}
                      onClick={ev => { ev.stopPropagation(); setLensFocusId(cur => cur === id ? null : id) }}
                      onContextMenu={onOpenEditor ? (ev) => { ev.preventDefault(); ev.stopPropagation(); onOpenEditor({ file: p.file, function: p.label, line: p.line, role: p.role }) } : undefined}>
                      <title>Right-click to open editor · left-click to focus arrows</title>
                    </rect>
                    <text className="flow-lens-pill-label" pointerEvents="none" x={p.rect.x + 12} y={p.rect.y + 15}>{p.role === 'failure' ? '✕ ' : ''}{p.label}</text>
                    {p.sub && <text className="flow-lens-pill-sub" pointerEvents="none" x={p.rect.x + 12} y={p.rect.y + 28}>{p.sub}</text>}
                  </g>
                ))}
                {/* One clean arrow per causal step, short label near the arrow. */}
                {pedges.map((e, i) => {
                  const a = placed[e.from], b = placed[e.to]
                  if (!a || !b) return null
                  // Left-click focus: only arrows touching the focused node stay lit.
                  const dim = lensFocusId && e.from !== lensFocusId && e.to !== lensFocusId
                  const am = mid(a.rect), bm = mid(b.rect)
                  const x1 = am.cx < bm.cx ? a.rect.x + a.rect.w + 6 : a.rect.x - 6
                  const y1 = am.cy
                  const x2 = am.cx < bm.cx ? b.rect.x - 8 : b.rect.x + b.rect.w + 8
                  const y2 = bm.cy
                  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - 14
                  const lw = Math.min(180, Math.max(36, (e.label || '').length * 5.6))
                  return (
                    <g key={`fl-edge-${i}`} opacity={dim ? 0.16 : 1}>
                      <path className="flow-lens-edge"
                        d={`M ${x1} ${y1} Q ${mx} ${my - 12} ${x2} ${y2}`}
                        fill="none" markerEnd="url(#arrowhead-flow)" />
                      <rect className="flow-lens-label-bg" x={mx - lw / 2} y={my - 22} width={lw} height={16} rx={8} />
                      <text className="flow-lens-label" x={mx} y={my - 10} textAnchor="middle">
                        {(e.label || '').slice(0, 28)}
                      </text>
                    </g>
                  )
                })}
              </g>
            )
          })()}

          {/* Repair-hatch focus: the FAILING function wears a red boundary — on
              its FUNCTION box only (never a giant file ring). While the lens is
              open it owns the failing color, so the idle 'failing' ring is
              suppressed; the repair phases (orange fixing / green fixed) still show. */}
          {failFocus && (() => {
            const g = layout.fnById?.[failFocus.fnId]
            if (!g) return null
            const phase = repairPhase || 'failing'
            if (flowLens?.artifact && phase === 'failing') return null
            const cls = phase === 'fixed' ? 'watch-ring repair-fixed'
              : phase === 'fixing' ? 'watch-ring active repair-fixing'
              : 'watch-ring active fail-focus'
            return <rect className={cls} x={g.x - 6} y={g.y - 6}
              width={g.w + 12} height={g.h + 12} rx={13} fill="none" pointerEvents="none" />
          })()}

          {/* Watch Any Agent: holds while the agent works — bright on the live box,
              calmer trail on boxes touched earlier this session. */}
          {watchRings.map(r => (
            <rect key={`watch-${r.id}`} className={`watch-ring ${r.status === 'wactive' ? 'active' : 'trail'}`}
              x={r.geom.x - 6} y={r.geom.y - 6} width={r.geom.w + 12} height={r.geom.h + 12}
              rx={13} fill="none" pointerEvents="none" />
          ))}

          {/* Canvas spotlight (Step 37a Slice 2/3): dim everything except the boxes
              that hold the concept / were touched by the commit; thread the lit
              boxes to a labeled centroid; amber-ring the partially-missed ones. */}
          {spot && !flowLens?.artifact && (spot.lit.length > 0 || spot.amber.length > 0) && (
            <g className="tether-layer" pointerEvents="none">
              <defs>
                <mask id="tether-mask">
                  <rect x="0" y="0" width={viewBounds.w} height={viewBounds.h} fill="white" />
                  {[...spot.lit, ...spot.amber].map(r => (
                    <rect key={`hole-${r.id}`} x={r.x - 10} y={r.y - 10}
                      width={r.w + 20} height={r.h + 20} rx={16} fill="black" />
                  ))}
                </mask>
              </defs>
              <rect className="tether-dim" x="0" y="0" width={viewBounds.w} height={viewBounds.h}
                fill="var(--bg)" mask="url(#tether-mask)" />
              {spot.lit.map(r => (
                <line key={`thread-${r.id}`} className="tether-thread"
                  x1={spot.centroid.x} y1={spot.centroid.y} x2={r.cx} y2={r.cy} />
              ))}
              {spot.amber.map(r => (
                <rect key={`amber-${r.id}`} className="tether-ring-amber"
                  x={r.x - 8} y={r.y - 8} width={r.w + 16} height={r.h + 16} rx={14} fill="none" />
              ))}
              {spot.lit.map(r => (
                <rect key={`lit-${r.id}`} className="tether-ring"
                  x={r.x - 8} y={r.y - 8} width={r.w + 16} height={r.h + 16} rx={14} fill="none" />
              ))}
              {spotLabel && (
                <g className="tether-label-g"
                  transform={`translate(${spot.centroid.x}, ${spot.centroid.y})`}>
                  <rect className="tether-label-bg" x={-(spotLabel.w / 2)} y={spotLabel.sub ? -22 : -13}
                    width={spotLabel.w} height={spotLabel.sub ? 40 : 26} rx={13} />
                  <text className="tether-label-tx" x="0" y={spotLabel.sub ? -4 : 5} textAnchor="middle">
                    {spotLabel.main}
                  </text>
                  {spotLabel.sub && (
                    <text className="tether-label-sub" x="0" y="13" textAnchor="middle">{spotLabel.sub}</text>
                  )}
                </g>
              )}
            </g>
          )}

          {rubberBand && (
            <rect x={rubberBand.x} y={rubberBand.y} width={rubberBand.w} height={rubberBand.h}
              fill="rgba(124,111,247,0.08)" stroke="var(--accent)" strokeWidth={1} strokeDasharray="4 3" pointerEvents="none" />
          )}
        </svg>
      </div>

      {/* Live: always-on Watch indicator (pulses when activity lands) that doubles
          as the follow-camera toggle. Shown whenever Watch is connected — not only after an
          edit — so the user trusts OpenFDE is watching. Click to start/stop follow-camera. */}
      {(watchConnected || watching) && (
        <button
          className={`wb-watching wb-live${watchRings.length ? ' active' : ''}${liveFollow ? ' following' : ''}`}
          onClick={() => onToggleLiveFollow?.()}
          title={liveFollow
            ? 'Live follow ON — the canvas centers the file being edited. Click to stop following (edits still glow).'
            : 'Live follow OFF — edits still glow, camera stays put. Click to follow the active file.'}>
          <span className="wb-watching-dot" />
          Live{liveFollow ? ' · following' : ''}
        </button>
      )}

      {/* Zoom controls */}
      <div className="wb-zoom">
        <button onClick={() => setScale(s => clamp(+(s - 0.1).toFixed(2), 0.3, 2.5))} title="Zoom out">−</button>
        <button className="wb-zoom-pct" onClick={() => setScale(1)} title="Reset zoom">{Math.round(scale * 100)}%</button>
        <button onClick={() => setScale(s => clamp(+(s + 0.1).toFixed(2), 0.3, 2.5))} title="Zoom in">+</button>
      </div>

      {/* Spotlight chip — what's lit, how many boxes / at-risk, dismiss */}
      {spotlight && (
        <div className={`tether-chip kind-${spotlight.kind}`} onClick={() => onClearSpotlight?.()} title="Clear (Esc)">
          <span className="tether-chip-dot" />
          <span className="tether-chip-id">
            {spotlight.kind === 'commit' ? `commit ${spotlight.label}`
              : spotlight.kind === 'worktree' ? 'uncommitted changes'
              : spotlight.kind === 'episode' ? `prompt · ${spotlight.label}`
              : spotlight.label}
          </span>
          <span className="tether-chip-meta">
            {spot && (spot.lit.length > 0 || spot.amber.length > 0)
              ? `${spot.lit.length} box${spot.lit.length === 1 ? '' : 'es'}` +
                (spot.amber.length ? ` · ${spot.amber.length} at risk` : '')
              : 'no boxes on this canvas'}
          </span>
          <span className="tether-chip-x">✕</span>
        </div>
      )}

      {/* While first-paint hydration is in flight, NEVER show the blank "Scan repo" manual CTA —
          a warm cache may be about to restore the modules. Show a calm skeleton instead. */}
      {isEmpty && hydrating && (
        <div className="wb-empty-cta">
          <span className="wb-restoring" style={{ fontSize: 13, color: 'var(--text-muted)' }}>
            Restoring canvas…
          </span>
        </div>
      )}
      {isEmpty && !hydrating && (
        <div className="wb-empty-cta">
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
            {repoName
              ? `${repoName}'s files are ready in Explorer — scan the repo to build its architecture canvas.`
              : 'Select a box tool above and click to place a module'}
          </span>
          {onGenerateFromRepo && (
            <button className="self-map-btn scan-repo-btn" onClick={onGenerateFromRepo}>
              {repoName ? `Scan ${repoName} → canvas` : 'Scan repo → canvas'}
            </button>
          )}
          {/* The self-map is a demo of OpenFDE's OWN architecture — only offer it when
              watching the openfde repo, never as the dominant action elsewhere. */}
          {onLoadSelfMap && repoName === 'openfde' && (
            <button className="self-map-btn" onClick={onLoadSelfMap}>Load OpenFDE self-map</button>
          )}
          {/* Sketch-First discovery: calm, subtle SAMPLES — sketch intent → see what it became in
              code. Subordinate to the scan action; never a workflow template. Two examples: a tiny
              sketch (already built), and a realistic SaaS workflow you RUN to ground in place. */}
          {onLoadSketchDemo && (
            <div className="wb-sketch-demo">
              <span className="wb-sketch-demo-hint">Sketch an intent, then run it.</span>
              <div className="wb-sketch-demo-row">
                <button
                  className="self-map-btn wb-sketch-demo-btn"
                  disabled={demoState === 'loading'}
                  onClick={async () => {
                    setDemoState('loading')
                    const res = await onLoadSketchDemo()
                    // On success the canvas hydrates and this empty-state unmounts; only a
                    // refusal/error (e.g. a 409 race) lands back here to show the note.
                    setDemoState(res && res.ok && Array.isArray(res.boxes) ? 'idle' : 'error')
                  }}>
                  {demoState === 'loading' ? 'Loading sketch demo…' : 'Load sketch demo'}
                </button>
                {onLoadSaasDemo && (
                  <button
                    className="self-map-btn wb-sketch-demo-btn"
                    disabled={saasState === 'loading'}
                    title="A realistic AI support-inbox workflow — seed it, then select the steps and Run to ground them"
                    onClick={async () => {
                      setSaasState('loading')
                      const res = await onLoadSaasDemo()
                      setSaasState(res && res.ok && Array.isArray(res.boxes) ? 'idle' : 'error')
                    }}>
                    {saasState === 'loading' ? 'Loading example…' : 'Load support inbox example'}
                  </button>
                )}
              </div>
              {(demoState === 'error' || saasState === 'error') && (
                <span className="wb-sketch-demo-err">Couldn’t load the example — the canvas may already have boxes.</span>
              )}
            </div>
          )}
        </div>
      )}

      {!isEmpty && activeTool !== 'select' && (
        <div className="wb-tool-hint-pill">
          {activeTool === 'dotted' ? 'Click to place dotted module · agent-free zone' :
           activeTool === 'solid'  ? 'Click to place solid module · agent asks before changing' :
           'Hover a box to reveal ports · drag port → port to connect'}
        </div>
      )}

      {onExecute && (
        <button className="canvas-execute-btn" onClick={() => onExecute()} disabled={executing}
          title="Compile the selected architecture into an execution prompt">
          {executing ? 'Compiling…' : (
            <>
              <span className="canvas-execute-glyph" aria-hidden="true">▶</span>
              Run
              <span className="canvas-execute-scope">{selectedIds.size > 0 ? `${selectedIds.size} selected` : 'whole repo'}</span>
            </>
          )}
        </button>
      )}

      {hoverFn && <FunctionHoverCard hover={hoverFn} />}

      {editOverlay && editingBoxId && (
        <EditingOverlay key={`${editingBoxId}-${editingField}`} overlay={editOverlay}
          box={boxes.find(b => b.id === editingBoxId)} field={editingField} onSave={stopEdit} />
      )}

      {arrowEdit && (
        <ArrowLabelOverlay key={arrowEdit.arrowId} overlay={arrowEdit} onSave={saveArrowEdit} />
      )}

      {contextMenu && (
        <ContextMenu x={contextMenu.x} y={contextMenu.y} targetIds={contextMenu.targetIds} boxes={boxes}
          onClose={() => setContextMenu(null)}
          onToggleType={ids => dispatch({ type: 'TOGGLE_TYPE', ids })}
          onToggleIntent={ids => dispatch({ type: 'TOGGLE_INTENT', ids })}
          onDuplicate={ids => dispatch({ type: 'DUPLICATE_BOXES', ids })}
          onDelete={ids => dispatch({ type: 'DELETE_BOXES', ids })}
          onExpandModule={onExpandModule} />
      )}

    </div>
  )
}

/* ─── Expand chevron (SVG) ───────────────────────────────────────────────── */
function Chevron({ x, y, open, nodeId, kind }) {
  return (
    <g data-expand-toggle data-node-id={nodeId} data-node-kind={kind} style={{ cursor: 'pointer' }}>
      <rect x={x} y={y} width={14} height={14} rx={3} fill="var(--surface-2)" stroke="var(--border)" strokeWidth={1} />
      <text x={x + 7} y={y + 10.5} textAnchor="middle" fontSize="10" fill="var(--accent)" style={{ pointerEvents: 'none', userSelect: 'none' }}>
        {open ? '−' : '+'}
      </text>
    </g>
  )
}

/* ─── Function-level dataflow arrow (Step 23) ────────────────────────────── */
function flowPorts(from, to) {
  const dx = (to.x + to.w / 2) - (from.x + from.w / 2)
  const dy = (to.y + to.h / 2) - (from.y + from.h / 2)
  if (Math.abs(dx) >= Math.abs(dy)) return dx > 0 ? ['E', 'W'] : ['W', 'E']
  return dy > 0 ? ['S', 'N'] : ['N', 'S']
}

// Same-file calls route as an arc bowing into the file's RIGHT lane; same-module
// cross-file rollups bow into the module's LEFT lane. Either way the curve stays
// out of the box stack so nothing is sliced through. Bow grows with distance so
// arcs nest concentrically without explicit lane assignment.
function arcGeometry(from, to, side) {
  const sign = side === 'left' ? -1 : 1
  const x0 = side === 'left' ? from.x : from.x + from.w
  const y0 = from.y + from.h / 2
  const x1 = side === 'left' ? to.x : to.x + to.w
  const y1 = to.y + to.h / 2
  const span = Math.abs(y1 - y0)
  const off = sign * Math.max(26, Math.min(88, 22 + span * 0.16))
  const d = `M ${x0} ${y0} C ${x0 + off} ${y0}, ${x1 + off} ${y1}, ${x1} ${y1}`
  const mid = { x: (side === 'left' ? Math.min(x0, x1) : Math.max(x0, x1)) + off, y: (y0 + y1) / 2 }
  return { d, mid }
}

// Point at half the total length of a polyline — keeps the edge label on the
// path (between corners) rather than snapping it to a bend.
function polylineMidpoint(pts) {
  let total = 0
  for (let i = 1; i < pts.length; i++) total += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
  let acc = 0
  const half = total / 2
  for (let i = 1; i < pts.length; i++) {
    const seg = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
    if (acc + seg >= half) {
      const t = seg ? (half - acc) / seg : 0
      return { x: pts[i - 1].x + (pts[i].x - pts[i - 1].x) * t, y: pts[i - 1].y + (pts[i].y - pts[i - 1].y) * t }
    }
    acc += seg
  }
  const m = pts[Math.floor(pts.length / 2)]
  return { x: m.x, y: m.y }
}

function FlowArrow({ flow }) {
  let d, mid, routed = false
  if (flow.routedPoints && flow.routedPoints.length >= 2) {
    // ELK-routed orthogonal polyline (Routed mode). Label sits at the geometric
    // mid-length point so it lands on the path, not on a corner.
    const pts = flow.routedPoints
    d = pts.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')
    mid = polylineMidpoint(pts)
    routed = true
  } else if (flow.route === 'arc-right') {
    ({ d, mid } = arcGeometry(flow.from, flow.to, 'right'))
  } else if (flow.route === 'arc-left') {
    ({ d, mid } = arcGeometry(flow.from, flow.to, 'left'))
  } else {
    const [fp, tp] = flowPorts(flow.from, flow.to)
    const start = getPortPos(flow.from, fp)
    const end   = getPortPos(flow.to,   tp)
    d   = bezierPath(start, fp, end, tp)
    mid = getBezierMidpoint(start, fp, end, tp)
  }

  // Visual hierarchy: module trunks carry WEIGHT (width grows with bundled flow
  // count), long edges fade with distance so locality reads stronger, the
  // focused flow pops, unrelated flows nearly vanish.
  const ddx = (flow.to.x + flow.to.w / 2) - (flow.from.x + flow.from.w / 2)
  const ddy = (flow.to.y + flow.to.h / 2) - (flow.from.y + flow.from.h / 2)
  const fade = Math.max(0.3, Math.min(1, 1.15 - Math.hypot(ddx, ddy) / 2600))
  const base = flow.route === 'arc-right' ? 0.22 : flow.level === 0 ? 0.45 : 0.5
  const opacity = flow.dim ? 0.07 : flow.highlight ? 0.95 : base * fade
  const width = flow.highlight ? 2
    : flow.level === 0 ? Math.min(3.2, 1.2 + Math.log2((flow.count || 1) + 1) * 0.55)
      : (flow.route === 'arc-right' ? 1.1 : 1.35)
  // Labels only when highlighted (related to focus); otherwise the canvas stays
  // calm. Bundled flows show an aggregate "N flows" pill.
  const labelText = flow.highlight && flow.label ? truncate(flow.label, 22) : ''
  const labelW = labelText ? labelText.length * 5.6 + 12 : 0

  return (
    <g className="flow-arrow">
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth={width} strokeOpacity={opacity}
        strokeDasharray={routed ? 'none' : '3 3'} strokeLinejoin="round"
        markerEnd="url(#arrowhead-flow)" />
      {labelText && (
        <g pointerEvents="none">
          <rect x={mid.x - labelW / 2} y={mid.y - 7} width={labelW} height={14} rx={7}
            fill="var(--surface)" stroke="var(--accent)" strokeWidth={0.75} strokeOpacity={0.8} />
          <text x={mid.x} y={mid.y + 3} textAnchor="middle" fill="var(--accent)" fontSize={8.5}
            fontFamily="inherit">{labelText}</text>
        </g>
      )}
    </g>
  )
}

/* ─── Story stage: staged left-to-right phase layout (Batch 5b) ──────────── */
function storyArrowPath(a) {
  const x0 = a.from.x + a.from.w
  const y0 = a.from.y + a.from.h / 2
  if (a.intra) {
    const x1 = a.to.x + a.to.w
    const y1 = a.to.y + a.to.h / 2
    const off = 28 + Math.abs(y1 - y0) * 0.12
    return `M ${x0} ${y0} C ${x0 + off} ${y0}, ${x1 + off} ${y1}, ${x1} ${y1}`
  }
  const x1 = a.to.x
  const y1 = a.to.y + a.to.h / 2
  const mx = (x0 + x1) / 2
  return `M ${x0} ${y0} C ${mx} ${y0}, ${mx} ${y1}, ${x1} ${y1}`
}

function StoryStageArrow({ a }) {
  return (
    <path d={storyArrowPath(a)} fill="none" stroke="var(--accent)"
      strokeWidth={a.faint ? 1.2 : 1.6} strokeOpacity={a.faint ? 0.35 : 0.8}
      markerEnd="url(#arrowhead-flow)" />
  )
}

function StoryFnBox({ node, selected, onSelect }) {
  return (
    <g data-story-fn-id={node.id} style={{ cursor: 'pointer' }}
      onPointerDown={e => { e.stopPropagation(); onSelect?.(node) }}>
      <rect x={node.x} y={node.y} width={node.w} height={node.h} rx={8}
        fill="var(--surface)" stroke={selected ? 'var(--accent)' : 'var(--border)'}
        strokeWidth={selected ? 2 : 1.2} />
      <text x={node.x + 12} y={node.y + (node.sub ? 19 : 28)} fontSize={12} fontWeight={600}
        fill="var(--text)" fontFamily="ui-monospace, monospace">{truncate(node.label, 22)}</text>
      {node.sub && (
        <text x={node.x + 12} y={node.y + 35} fontSize={10} fill="var(--text-muted)" fontFamily="inherit">
          {truncate(node.sub, 30)}
        </text>
      )}
      <title>{node.sub || node.label}</title>
    </g>
  )
}

function StoryIO({ col }) {
  const b = col.box
  return (
    <g pointerEvents="none">
      <text x={b.x} y={b.y - 8} fontSize={11} fontWeight={700} fill="var(--text-muted)">{col.label}</text>
      <rect x={b.x} y={b.y} width={b.w} height={b.h} rx={8} fill="var(--surface-2)"
        stroke="var(--border)" strokeWidth={1.2} strokeDasharray="4 3" />
      {col.items.map((it, i) => (
        <text key={i} x={b.x + 12} y={b.y + 18 + i * 16} fontSize={11} fill="var(--text)" fontFamily="inherit">
          • {truncate(String(it), 16)}
        </text>
      ))}
    </g>
  )
}

function StoryStage({ stage, onSelectFn, selectedId }) {
  const selCanvas = selectedId ? `box:${selectedId}` : null
  return (
    <g className="story-stage">
      <g pointerEvents="none">
        {stage.arrows.map(a => <StoryStageArrow key={a.id} a={a} />)}
      </g>
      {stage.columns.map(col => col.kind === 'phase' ? (
        <g key={`col-${col.order}`}>
          <rect x={col.header.x} y={col.header.y} width={col.header.w} height={col.header.h} rx={9}
            fill="rgba(124,111,247,0.13)" stroke="var(--accent)" strokeWidth={1.2} pointerEvents="none" />
          <circle cx={col.header.x + 18} cy={col.header.y + col.header.h / 2} r={9} fill="var(--accent)" pointerEvents="none" />
          <text x={col.header.x + 18} y={col.header.y + col.header.h / 2 + 3.5} textAnchor="middle"
            fontSize={10} fontWeight={700} fill="#fff" pointerEvents="none">{col.order}</text>
          <text x={col.header.x + 34} y={col.header.y + col.header.h / 2 + 4} fontSize={12} fontWeight={700}
            fill="var(--text)" pointerEvents="none">{truncate(col.label, 19)}</text>
          {col.nodes.map(n => (
            <StoryFnBox key={n.id} node={n} selected={selCanvas === n.id} onSelect={onSelectFn} />
          ))}
        </g>
      ) : (
        <StoryIO key={`io-${col.role}`} col={col} />
      ))}
    </g>
  )
}

/* ─── Expanded module (nested files / functions) ─────────────────────────── */
function ExpandedModule({ node, selected, archSelId }) {
  // A built intent box expanded reads as architecture-backed: solid green chrome (matching its
  // collapsed built state), with the original sketch title kept as the header.
  const isIntentBuilt = node.box?.kind === 'intent'
  const stroke = (isIntentBuilt || node.type === 'solid') ? 'var(--solid)' : 'var(--dotted)'
  const fill = (isIntentBuilt || node.type === 'solid') ? 'rgba(61,186,110,0.05)' : 'rgba(74,158,255,0.05)'
  const dashed = !isIntentBuilt && node.type !== 'solid'
  return (
    <g>
      <rect
        data-box-id={node.id}
        x={node.x} y={node.y} width={node.w} height={node.h} rx={8}
        fill={fill} stroke={stroke} strokeWidth={selected ? 2 : 1.4}
        strokeDasharray={dashed ? '5 3' : undefined}
      />
      {/* Header */}
      <text data-box-id={node.id} x={node.x + 12} y={node.y + 20} fontSize="13" fontWeight="600" fill="var(--text)" style={{ userSelect: 'none' }}>
        {truncate(node.title, isIntentBuilt ? 22 : 28)}
      </text>
      {isIntentBuilt && (
        <text x={node.x + node.w - 30} y={node.y + 20} fontSize="9" fontWeight="600" fill="var(--solid)"
          textAnchor="end" style={{ userSelect: 'none', pointerEvents: 'none' }}>✓ built</text>
      )}
      <Chevron x={node.x + node.w - 20} y={node.y + 8} open nodeId={node.id} kind="module" />

      {/* File children */}
      {node.files.map(f => (
        <FileNodeBox key={f.id} f={f} archSelId={archSelId} />
      ))}
    </g>
  )
}

function FileNodeBox({ f, archSelId }) {
  const stroke = f.type === 'solid' ? 'var(--solid)' : 'var(--dotted)'
  const sel = f.id === archSelId
  const name = f.file.path.split('/').pop()
  return (
    <g>
      <rect
        data-node-id={f.id} data-node-kind="file"
        x={f.x} y={f.y} width={f.w} height={f.h} rx={5}
        fill="var(--surface)" stroke={sel ? 'var(--accent)' : stroke} strokeWidth={sel ? 2 : 1.2}
        strokeDasharray={f.type === 'solid' ? undefined : '4 2.5'}
        style={{ cursor: 'pointer' }}
      />
      <text data-node-id={f.id} data-node-kind="file" x={f.x + 9} y={f.y + 18} fontSize="11.5" fontWeight="600" fill="var(--text)" style={{ userSelect: 'none', pointerEvents: 'none' }}>
        {truncate(name, 18)}
      </text>
      <text x={f.x + 9} y={f.y + 18 + 13} fontSize="9" fill="var(--text-muted)" style={{ userSelect: 'none', pointerEvents: 'none' }}>
        {f.file.language}{f.expanded ? '' : ' · double-click to open'}
      </text>
      <Chevron x={f.x + f.w - 19} y={f.y + 7} open={f.expanded} nodeId={f.id} kind="file" />

      {f.expanded && f.empty && (
        <text x={f.x + f.w / 2} y={f.y + f.h - 12} textAnchor="middle" fontSize="10" fill="var(--text-muted)" fontStyle="italic" style={{ pointerEvents: 'none' }}>
          no parsed functions
        </text>
      )}

      {f.expanded && (f.functions || []).map(g => (
        <FunctionNodeBox key={g.id} g={g} sel={g.id === archSelId} />
      ))}
    </g>
  )
}

function FunctionNodeBox({ g, sel }) {
  const stroke = g.type === 'solid' ? 'var(--solid)' : 'var(--dotted)'
  const sig = compactSig(g.fn)
  // Hover is handled centrally by the canvas pointer handler (data-node-kind).
  return (
    <g data-node-id={g.id} data-node-kind="function" style={{ cursor: 'pointer' }}>
      <rect x={g.x} y={g.y} width={g.w} height={g.h} rx={4}
        fill="var(--surface-2)" stroke={sel ? 'var(--accent)' : stroke} strokeWidth={sel ? 1.8 : 1}
        strokeDasharray={g.type === 'solid' ? undefined : '3 2'} pointerEvents="all" />
      <text x={g.x + 7} y={g.y + 15} fontSize="10.5" fontWeight="600" fill="var(--text)" style={{ pointerEvents: 'none', userSelect: 'none' }}>
        {truncate(g.fn.name, 16)}
      </text>
      <text x={g.x + 7} y={g.y + 28} fontSize="8.5" fill="var(--text-muted)" fontFamily="ui-monospace, monospace" style={{ pointerEvents: 'none', userSelect: 'none' }}>
        {truncate(sig, 20)}
      </text>
    </g>
  )
}

function compactSig(fn) {
  const args = (fn.args || []).map(a => (a.type ? `${a.name}: ${a.type}` : a.name))
  let inner = args.join(', ')
  if (inner.length > 22) inner = inner.slice(0, 21) + '…'
  return `(${inner})${fn.returns ? ` → ${fn.returns}` : ''}`
}

/* ─── Live-run activity rings (Step 17) ─────────────────────────────────────
 * Resolves each scoped node to a *visible* render target: itself when on
 * screen, otherwise the nearest visible parent (function → file → module).
 * When several scoped ids resolve to the same target, the highest-severity
 * status wins (failed > running > planning/active > passed). */
const _RUN_SEV = { done: 0, passed: 0, queued: 1, planning: 1, read: 2, next: 3, running: 4, active: 5, failed: 6, wtrail: 1, wactive: 5 }

function computeRunRings(nodeStates, nodes, layout) {
  const ids = Object.keys(nodeStates)
  if (ids.length === 0) return []

  const geomFor = (id) => {
    if (id.startsWith('box:function:')) { const g = layout.fnById?.[id]; return g ? { x: g.x, y: g.y, w: g.w, h: g.h } : null }
    if (id.startsWith('box:file:'))     { const f = layout.fileById?.[id]; return f ? { x: f.x, y: f.y, w: f.w, h: f.h } : null }
    const n = nodes.find(nn => nn.id === id)
    return n ? { x: n.x, y: n.y, w: n.w, h: n.h } : null
  }

  // Repo path embedded in a canvas file/function id (paths contain no ':').
  const pathOf = (id) => {
    if (id.startsWith('box:function:')) { const rest = id.slice('box:function:'.length); const li = rest.lastIndexOf(':'); return li >= 0 ? rest.slice(0, li) : rest }
    if (id.startsWith('box:file:')) return id.slice('box:file:'.length)
    return null
  }
  // Roll a file path up to its module box. Match the module's linkedPath (dir prefix) OR its
  // linkedFiles (exact, then basename) — the same way Watch resolves a file to its box — so a
  // file glow ALWAYS lands on the box that was expanded, never resolving to nothing.
  const moduleForPath = (p) => nodes.find(m => {
    const b = m.box || {}
    if (b.linkedPath && (p === b.linkedPath || p.startsWith(`${b.linkedPath}/`))) return true
    const lf = b.linkedFiles || []
    if (lf.includes(p)) return true
    const base = p.split('/').pop()
    return lf.some(f => f.split('/').pop() === base)
  }) || null

  // Resolve a run-state id to the lowest VISIBLE node, rolling up only (Step 33):
  //   function box (only if the id is a function AND it's visible)
  //     → visible file node
  //       → parent module.
  // It never descends, so a file-level state (box:file:<path>) lights the file
  // box, never the function boxes inside it — file/comment/doc tasks don't force
  // function glow.
  const resolve = (id) => {
    if (geomFor(id)) return id
    const p = pathOf(id)
    if (p == null) return null
    const fid = `box:file:${p}`
    if (geomFor(fid)) return fid
    const m = moduleForPath(p)
    return m ? m.id : null
  }

  const resolved = {}   // visible targetId → status (highest severity wins)
  ids.forEach(id => {
    const t = resolve(id)
    if (!t) return
    const s = nodeStates[id]
    if (resolved[t] === undefined || (_RUN_SEV[s] ?? 0) >= (_RUN_SEV[resolved[t]] ?? 0)) resolved[t] = s
  })

  return Object.entries(resolved)
    .map(([id, status]) => ({ id, status, geom: geomFor(id) }))
    .filter(r => r.geom)
}

/* ─── Function hover card (HTML overlay) ─────────────────────────────────── */
function FunctionHoverCard({ hover }) {
  const { fn, left, top } = hover
  const args = fn.args || []
  return (
    <div className="dd-fn-card hover-card" style={{ position: 'fixed', left, top, width: 240 }}>
      <div className="dd-fn-card-name">{fn.name}</div>
      <div className="dd-fn-card-purpose">{fn.purpose ? fn.purpose : <span className="dd-muted">No docstring summary.</span>}</div>
      <div className="dd-fn-card-row">
        <span className="dd-fn-card-label">Args</span>
        {args.length > 0 ? (
          <div className="dd-fn-card-args">
            {args.map((a, i) => (
              <div key={i} className="dd-fn-card-arg"><code>{a.name}</code>{a.type ? <span className="dd-fn-card-type">: {a.type}</span> : null}</div>
            ))}
          </div>
        ) : <span className="dd-muted">none</span>}
      </div>
      <div className="dd-fn-card-row">
        <span className="dd-fn-card-label">Returns</span>
        <span>{fn.returns ? <code>{fn.returns}</code> : <span className="dd-muted">unspecified</span>}</span>
      </div>
      <div className="dd-fn-card-loc">{fn.path}{typeof fn.line === 'number' ? `:${fn.line}` : ''}</div>
    </div>
  )
}

/* ─── Inline arrow relationship editor (Step 26) ─────────────────────────── */
function ArrowLabelOverlay({ overlay, onSave }) {
  const [value, setValue] = useState(overlay.value || '')
  const ref = useRef(null)
  useEffect(() => {
    const t = setTimeout(() => { ref.current?.focus(); ref.current?.select?.() }, 0)
    return () => clearTimeout(t)
  }, [])
  function onKey(e) {
    if (e.key === 'Enter') { e.preventDefault(); onSave(overlay.arrowId, value) }
    if (e.key === 'Escape') { e.preventDefault(); onSave(overlay.arrowId, null) }
  }
  return (
    <input
      ref={ref}
      value={value}
      placeholder="name this relationship…"
      onChange={e => setValue(e.target.value)}
      onKeyDown={onKey}
      onBlur={() => onSave(overlay.arrowId, value)}
      style={{
        position: 'fixed', left: overlay.clientX - 80, top: overlay.clientY - 12, width: 160,
        background: 'var(--surface)', border: '1px solid var(--accent)', borderRadius: 4,
        color: 'var(--text)', fontSize: 11, fontFamily: 'inherit', padding: '3px 7px',
        outline: 'none', zIndex: 500, textAlign: 'center', boxShadow: '0 2px 10px rgba(0,0,0,0.35)',
      }}
    />
  )
}

function EditingOverlay({ overlay, box, field, onSave }) {
  const [value, setValue] = useState(() => box?.[field] ?? '')
  const ref = useRef(null)

  useEffect(() => {
    const t = setTimeout(() => { ref.current?.focus(); ref.current?.select?.() }, 0)
    return () => clearTimeout(t)
  }, [])

  if (!box) return null
  const isTitle = field === 'title'
  const base = {
    position: 'fixed', left: overlay.clientX + 12, top: overlay.clientY + (isTitle ? 10 : 32), width: overlay.w - 24,
    background: 'var(--surface)', border: '1px solid var(--accent)', borderRadius: 4, color: 'var(--text)',
    fontSize: isTitle ? 13 : 11, fontWeight: isTitle ? 600 : 400, fontFamily: 'inherit', padding: '2px 6px', outline: 'none', zIndex: 500,
  }
  function onKey(e) {
    if (isTitle && e.key === 'Enter') { e.preventDefault(); onSave(box.id, field, value) }
    if (!isTitle && e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); onSave(box.id, field, value) }
    if (e.key === 'Escape') { e.preventDefault(); onSave(box.id, field, null) }
  }
  if (isTitle) {
    return <input ref={ref} style={{ ...base, height: 22 }} value={value} onChange={e => setValue(e.target.value)} onKeyDown={onKey} onBlur={() => onSave(box.id, field, value)} />
  }
  return (
    <textarea ref={ref} style={{ ...base, height: overlay.h - 46, resize: 'none' }} value={value}
      onChange={e => setValue(e.target.value)} onKeyDown={onKey} onBlur={() => onSave(box.id, field, value)}
      placeholder="Describe what this module does..." />
  )
}
