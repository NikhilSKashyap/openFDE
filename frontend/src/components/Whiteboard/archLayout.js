/**
 * archLayout.js — pure layout for in-place nested module → file → function
 * expansion on the architecture canvas.
 *
 * Given the persisted module boxes, the read-only ArchGraph (files +
 * functions), and the set of expanded node ids, this computes absolute
 * geometry for every visible node so the SVG canvas can render nested boxes
 * and re-anchor arrows. Parents grow to fit their expanded children.
 *
 * Stable ids:
 *   module   → the persisted box id (e.g. "box:module:openfde")
 *   file     → "box:file:<path>"
 *   function → "box:function:<path>:<name>"
 */

import dagre from '@dagrejs/dagre'

// ── Layout constants (canvas units) ─────────────────────────────────────────
const M_HEADER = 32
const M_PAD = 16
const M_GAP = 22           // gap between file boxes (dagre nodesep / grid fallback)

const F_MIN_W = 180
const F_HEADER = 30
const F_PAD = 10
const F_COLS = 1            // functions stack in a single column (readable call order)
const F_EMPTY_H = 30
const F_LANE = 104         // right-side lane inside each file for same-file call arcs

const FN_W = 200
const FN_H = 42
const FN_GAP = 13

// When a module expands it grows in place (anchored at its persisted top-left);
// only the boxes it now overlaps are shoved further right so the flow structure
// is preserved and the (infinite) canvas simply grows to fit.
const SEP_GAP = 56

export const fileId = (path) => `box:file:${path}`
export const fnId = (path, name) => `box:function:${path}:${name}`

// A GROUNDED box drills into the specific files the Council wrote (box.implementationFiles), in
// place, exactly like a module (chevron + nested file/function children) — even before those files
// are reassimilated into the ArchGraph. This covers BOTH a built intent step AND an intent step that
// has been transformed into an architecture module (which keeps its implementationFiles + drops its
// `kind`). Distinct from an ArchGraph-backed module, whose files live in the graph keyed by moduleId.
export const isIntentDrillBox = (box) =>
  !!box && Array.isArray(box.implementationFiles) && box.implementationFiles.length > 0
  && (box.runState === 'built' || !box.runState)

const _LANG = { py: 'python', js: 'javascript', jsx: 'javascript', ts: 'typescript',
  tsx: 'typescript', json: 'json', md: 'markdown', css: 'css', scss: 'scss', html: 'html',
  go: 'go', rs: 'rust', java: 'java', rb: 'ruby', sh: 'shell', sql: 'sql', yml: 'yaml', yaml: 'yaml' }
const langOf = (path) => {
  const ext = String(path || '').split('.').pop().toLowerCase()
  return _LANG[ext] || ext || 'file'
}

/**
 * Compute the full render layout.
 *
 * @param {Array} boxes - persisted module boxes (each may carry moduleId).
 * @param {Object} archGraph - { files, functions } or null.
 * @param {Set<string>} expanded - expanded node ids (modules + files).
 * @returns {{ nodes: Array, effectiveBoxes: Array, bounds: {w:number,h:number},
 *             fileById: Object, fnById: Object }}
 */
export function computeArchLayout(boxes, archGraph, expanded) {
  const nodes = []
  const fileById = {}
  const fnById = {}

  for (const box of boxes) {
    const isModule = !!box.moduleId
    const isIntent = isIntentDrillBox(box)
    // A module needs the ArchGraph to drill (its files live there); a built intent box carries
    // its own file list, so it expands even before the generated files are reassimilated.
    const intentExpanded = isIntent && expanded.has(box.id)
    const moduleExpanded = isModule && expanded.has(box.id) && !!archGraph

    if (!intentExpanded && !moduleExpanded) {
      nodes.push(moduleCollapsedNode(box, isModule || isIntent))
      continue
    }

    // Expanded: size file children, then position them with dagre.
    const { fileNodes, fEdges } = intentExpanded
      ? sizeIntentFiles(box, archGraph, expanded)
      : sizeModuleFiles(box, archGraph, expanded)
    const fl = layoutFiles(fileNodes, fEdges)
    nodes.push(assembleModule(box, fileNodes, fl, fileById, fnById))
  }

  return finalizeLayout(nodes, fileById, fnById)
}

// ── Engine-agnostic building blocks ─────────────────────────────────────────
// computeArchLayout (dagre, sync) and computeArchLayoutElk (elk, async) share
// everything except *positioning file boxes within a module*. These helpers are
// the shared parts so the two engines stay behaviourally identical elsewhere.

/** Collapsed / non-drillable module render node. */
export function moduleCollapsedNode(box, isModule) {
  return {
    id: box.id, kind: 'module', expanded: false, drillable: isModule,
    x: box.x, y: box.y, w: box.w, h: box.h, type: box.type, title: box.title, box,
    files: [],
  }
}

/**
 * Size ONE file render node: a collapsed header strip, an "empty" strip (file expanded but no
 * parsed functions), or a header + stacked function boxes. Shared by the module drill-in and the
 * built-intent drill-in so a generated file renders identically to a module's file.
 *
 * @param {Object} file - file descriptor (needs `.path`; `.language` used for the sub-label).
 * @param {Array} fns - the file's functions (already filtered + sorted), or [] when collapsed.
 * @param {string} type - 'solid' | 'dotted' (inherited from the parent box, for stroke palette).
 * @param {boolean} fExpanded - whether this file node is itself expanded.
 */
export function sizeFileNode(file, fns, type, fExpanded) {
  const fid = fileId(file.path)
  if (!fExpanded) {
    return { id: fid, kind: 'file', expanded: false, file, type,
             w: F_MIN_W, h: F_HEADER + 18, functions: [], _rx: 0, _ry: 0 }
  }
  if (!fns || fns.length === 0) {
    return { id: fid, kind: 'file', expanded: true, file, type,
             w: F_MIN_W, h: F_HEADER + F_EMPTY_H, functions: [], empty: true, _rx: 0, _ry: 0 }
  }
  const cols = Math.min(F_COLS, fns.length)
  const rows = Math.ceil(fns.length / F_COLS)
  const gridW = cols * FN_W + (cols - 1) * FN_GAP
  const gridH = rows * FN_H + (rows - 1) * FN_GAP
  const fnNodes = fns.map((fn, i) => {
    const col = i % F_COLS
    const row = Math.floor(i / F_COLS)
    return {
      id: fnId(fn.path, fn.name), kind: 'function', fn, type,
      w: FN_W, h: FN_H,
      _rx: F_PAD + col * (FN_W + FN_GAP),
      _ry: F_HEADER + F_PAD + row * (FN_H + FN_GAP),
    }
  })
  return {
    id: fid, kind: 'file', expanded: true, file, type,
    // + F_LANE on the right hosts the nested call-arc lane (Step 26).
    w: Math.max(F_MIN_W, gridW + F_PAD + F_LANE),
    h: F_HEADER + F_PAD + gridH + F_PAD,
    functions: fnNodes, _rx: 0, _ry: 0,
  }
}

/**
 * Size the file children of an expanded BUILT intent box. The files come from the persisted
 * `box.implementationFiles` (no new source of truth); their functions come from the live
 * ArchGraph when the generated file has been reassimilated, else the file degrades to a
 * function-less child ("no parsed functions"). No intra-file flow edges → grid layout.
 *
 * @returns {{ fileNodes: Array, fEdges: Array }}
 */
export function sizeIntentFiles(box, archGraph, expanded) {
  const functions = (archGraph && archGraph.functions) || []
  const fnsByPath = groupBy(functions, fn => fn.path)
  const paths = [...new Set(box.implementationFiles || [])].sort((a, b) => a.localeCompare(b))
  const fileNodes = paths.map(p => {
    const fExpanded = expanded.has(fileId(p))
    const fns = fExpanded
      ? (fnsByPath[p] || []).slice().sort((a, b) => (a.line || 0) - (b.line || 0))
      : []
    return sizeFileNode({ path: p, language: langOf(p), moduleId: box.moduleId }, fns, box.type, fExpanded)
  })
  return { fileNodes, fEdges: [] }
}

/**
 * Size every file box (and its stacked function boxes) inside an expanded
 * module, and derive the intra-module file→file dataflow edges. Pure sizing —
 * no positioning yet, so both layout engines consume the same input.
 *
 * @returns {{ fileNodes: Array, fEdges: Array<[string,string]> }}
 */
export function sizeModuleFiles(box, archGraph, expanded) {
  const files = (archGraph && archGraph.files) || []
  const functions = (archGraph && archGraph.functions) || []
  const filesByModule = groupBy(files, f => f.moduleId)
  const fnsByPath = groupBy(functions, fn => fn.path)

  const modFiles = (filesByModule[box.moduleId] || [])
    .slice()
    .sort((a, b) => a.path.localeCompare(b.path))

  const fileNodes = modFiles.map(f => {
    const fExpanded = expanded.has(fileId(f.path))
    const fns = fExpanded
      ? (fnsByPath[f.path] || []).slice().sort((a, b) => (a.line || 0) - (b.line || 0))
      : []
    return sizeFileNode(f, fns, box.type, fExpanded)
  })

  // Intra-module file→file dataflow edges (drive layered LR positioning).
  const fileSet = new Set(fileNodes.map(n => n.id))
  const seen = new Set()
  const fEdges = []
  for (const fw of ((archGraph && archGraph.flows) || [])) {
    if (!fw.fromFile || !fw.toFile || fw.fromFile === fw.toFile) continue
    const a = fileId(fw.fromFile), b = fileId(fw.toFile)
    if (fileSet.has(a) && fileSet.has(b)) {
      const k = `${a}>${b}`
      if (!seen.has(k)) { seen.add(k); fEdges.push([a, b]) }
    }
  }
  return { fileNodes, fEdges }
}

/**
 * Given sized file nodes and their relative positions `fl = {pos,w,h}` (from
 * either engine, normalized to 0,0), assemble the expanded-module render node
 * and resolve absolute geometry for files + functions. Mutates fileById/fnById.
 */
export function assembleModule(box, fileNodes, fl, fileById, fnById) {
  const modW = Math.max(box.w, M_PAD + fl.w + M_PAD)
  const modH = Math.max(box.h, M_HEADER + M_PAD + fl.h + M_PAD)
  fileNodes.forEach(fn => {
    const p = fl.pos[fn.id] || { x: 0, y: 0 }
    fn._rx = M_PAD + p.x
    fn._ry = M_HEADER + M_PAD + p.y
  })

  const fileAbs = fileNodes.map(fn => {
    const fx = box.x + fn._rx
    const fy = box.y + fn._ry
    const functionsAbs = (fn.functions || []).map(g => {
      const node = { ...g, x: fx + g._rx, y: fy + g._ry }
      fnById[g.id] = node
      return node
    })
    const node = { ...fn, x: fx, y: fy, functions: functionsAbs }
    fileById[fn.id] = node
    return node
  })

  const modNode = {
    id: box.id, kind: 'module', expanded: true, drillable: true,
    x: box.x, y: box.y, w: modW, h: modH, type: box.type, title: box.title, box,
    files: fileAbs,
  }

  // Routed edges (ELK engine only): file→file orthogonal paths in module-content
  // space → absolute, using the same offset as the file boxes. They ride along
  // with the module in separateNodes (see shiftNode), then surface in
  // finalizeLayout so the canvas can draw them in place of bezier flow arrows.
  if (fl.routes && fl.routes.length) {
    const ox = box.x + M_PAD
    const oy = box.y + M_HEADER + M_PAD
    modNode.routedEdges = fl.routes.map(r => ({
      fromId: r.fromId, toId: r.toId,
      points: r.points.map(p => ({ x: ox + p.x, y: oy + p.y })),
    }))
  }

  return modNode
}

/**
 * Final top-level pass shared by both engines: structure-preserving separation
 * of overlapping modules, effective box geometry for arrows/ports, and bounds.
 */
export function finalizeLayout(nodes, fileById, fnById) {
  // Expanded modules grow in place; only neighbours they now overlap are shoved
  // right (persisted box x/y untouched — render-only).
  const anyExpanded = nodes.some(n => n.expanded)
  if (anyExpanded) separateNodes(nodes)

  const effectiveBoxes = nodes.map(n => ({ ...n.box, x: n.x, y: n.y, w: n.w, h: n.h }))

  let maxX = 800, maxY = 600
  for (const n of nodes) {
    maxX = Math.max(maxX, n.x + n.w)
    maxY = Math.max(maxY, n.y + n.h)
  }
  const bounds = { w: maxX + 160, h: maxY + 160 }

  // Collect any routed edges (ELK engine) into a flat list keyed by endpoints;
  // empty for the dagre engine.
  const routedEdges = []
  for (const n of nodes) if (n.routedEdges) routedEdges.push(...n.routedEdges)

  return { nodes, effectiveBoxes, bounds, fileById, fnById, routedEdges }
}

/**
 * Structure-preserving separation. Every box stays at its persisted position;
 * when an expanded module grows into a neighbour, the neighbour (and only the
 * boxes that genuinely overlap, cascading) is shoved just far enough to the
 * right to clear it. Reading order is preserved and the canvas grows to fit —
 * no reshuffle, no row-wrapping. Render-only: shifts the node's x (and its file/
 * function children) in place; persisted box x/y are never mutated.
 *
 * @param {Array} nodes - module render nodes (with computed w/h).
 */
function separateNodes(nodes) {
  if (nodes.length < 2) return
  // Anchor by persisted reading order (left→right, then top→bottom).
  const ordered = [...nodes].sort((a, b) => (a.x - b.x) || (a.y - b.y))
  // A few passes let a shove cascade down a chain of boxes.
  for (let pass = 0; pass < 6; pass++) {
    let moved = false
    for (let i = 1; i < ordered.length; i++) {
      const b = ordered[i]
      for (let k = 0; k < i; k++) {
        const a = ordered[k]
        if (!boxesOverlap(a, b, SEP_GAP)) continue
        const targetX = a.x + a.w + SEP_GAP   // place b just right of a
        if (targetX - b.x > 0.5) { shiftNode(b, targetX - b.x, 0); moved = true }
      }
    }
    if (!moved) break
  }
}

// AABB overlap test with a breathing gap on every side.
function boxesOverlap(a, b, gap) {
  return a.x < b.x + b.w + gap && b.x < a.x + a.w + gap &&
         a.y < b.y + b.h + gap && b.y < a.y + a.h + gap
}

function shiftNode(n, dx, dy) {
  if (dx === 0 && dy === 0) return
  n.x += dx
  n.y += dy
  for (const f of (n.files || [])) {
    f.x += dx
    f.y += dy
    for (const g of (f.functions || [])) {
      g.x += dx
      g.y += dy
    }
  }
  // Routed edges (ELK) ride along rigidly with their module.
  for (const e of (n.routedEdges || [])) {
    for (const p of e.points) { p.x += dx; p.y += dy }
  }
}

const _CONF_RANK = { high: 3, medium: 2, low: 1 }
const _confMax = (a, b) => ((_CONF_RANK[a] || 0) >= (_CONF_RANK[b] || 0) ? a : b)

// Does a raw flow touch the focused entity? Accepts a box: or raw id.
function flowRelatesToFocus(fw, focusId) {
  let id = focusId
  if (id.startsWith('box:')) id = id.slice(4)
  if (id.startsWith('function:')) return fw.fromFunctionId === id || fw.toFunctionId === id
  if (id.startsWith('file:')) { const p = id.slice(5); return fw.fromFile === p || fw.toFile === p }
  if (id.startsWith('module:')) return fw.fromModuleId === id || fw.toModuleId === id
  return false
}

/**
 * Resolve function-level dataflow into drawable arrows against the current
 * layout. Each flow's endpoints are resolved to the nearest *visible* box:
 * function box (file expanded) → file box (module expanded) → module box.
 * Flows whose endpoints collapse to the same box are hidden; module↔module
 * flows are left to the persisted module arrows (avoids double-drawing).
 *
 * @param {Object} archGraph - ArchGraph with a `flows` array, or null.
 * @param {Object} layout - result of computeArchLayout (fnById, fileById, nodes).
 * @returns {Array} drawable flow arrows: { id, from, to, fromId, toId, level,
 *                  count, label, confidence }. `from`/`to` are node geometries.
 */
export function computeFlowArrows(archGraph, layout, opts = {}) {
  const { mode = 'focused', focusId = null, storyFlowIds = null, flowIdToStep = null,
          altitude = 2 } = opts
  const story = mode === 'story' && storyFlowIds && storyFlowIds.size > 0
  const flows = (archGraph && archGraph.flows) || []
  if (!flows.length || !layout) return []
  const { fnById = {}, fileById = {}, nodes = [] } = layout

  const moduleById = {}
  for (const n of nodes) if (n.kind === 'module') moduleById[n.id] = n

  // Nearest visible box at or below maxLevel (2 = function, 1 = file, 0 = module).
  function resolveAt(rawFnId, file, moduleId, maxLevel) {
    if (maxLevel >= 2) { const c = `box:${rawFnId}`; if (fnById[c]) return { id: c, node: fnById[c], level: 2 } }
    if (maxLevel >= 1) { const c = fileId(file); if (fileById[c]) return { id: c, node: fileById[c], level: 1 } }
    const m = `box:${moduleId}`
    if (moduleById[m]) return { id: m, node: moduleById[m], level: 0 }
    return null
  }

  const agg = new Map()
  for (const fw of flows) {
    const fa = resolveAt(fw.fromFunctionId, fw.fromFile, fw.fromModuleId, 2)
    const fb = resolveAt(fw.toFunctionId, fw.toFile, fw.toModuleId, 2)
    if (!fa || !fb) continue
    const sameFile = fa.level === 2 && fb.level === 2 && fw.fromFile === fw.toFile

    // Story mode: keep only flows on the story path (function-level, human label).
    // Other modes — ATTENTION decides altitude ("altitude is the filter"):
    //   • unrelated to the focus → both endpoints collapse to the MODULE trunk
    //     (weighted aggregate). No selection ⇒ the whole board is calm trunks.
    //   • related to the focus → full detail, clamped by the zoom altitude.
    //   • mode 'all' keeps the legacy everything-on render (the art mode).
    let a, b, related, storyFlow = false, stepLabel = null, stepOrder = null
    if (story) {
      if (!storyFlowIds.has(fw.id)) continue
      a = fa; b = fb; related = true; storyFlow = true
      const step = flowIdToStep && flowIdToStep[fw.id]
      if (step) { stepLabel = step.label; stepOrder = step.order }
    } else {
      related = focusId ? flowRelatesToFocus(fw, focusId) : false
      if (mode === 'all') {
        a = fa; b = fb
      } else if (related) {
        a = resolveAt(fw.fromFunctionId, fw.fromFile, fw.fromModuleId, altitude)
        b = resolveAt(fw.toFunctionId, fw.toFile, fw.toModuleId, altitude)
      } else {
        a = resolveAt(fw.fromFunctionId, fw.fromFile, fw.fromModuleId, 0)
        b = resolveAt(fw.toFunctionId, fw.toFile, fw.toModuleId, 0)
      }
    }
    if (!a || !b || a.id === b.id) continue
    const route = sameFile ? 'arc-right'
      : (fw.fromModuleId === fw.toModuleId ? 'arc-left' : 'bezier')
    const key = `${a.id} ${b.id}`
    let e = agg.get(key)
    if (!e) {
      e = { id: `flowarrow:${key}`, from: a.node, to: b.node, fromId: a.id, toId: b.id,
            level: Math.min(a.level, b.level), sameFile, route, count: 0,
            sample: storyFlow ? (stepLabel || fw.label) : fw.label,
            confidence: 'low', related: false, story: storyFlow, stepOrder }
      agg.set(key, e)
    }
    e.count += 1
    e.related = e.related || related
    e.confidence = _confMax(e.confidence, fw.confidence)
  }

  const out = []
  for (const e of agg.values()) {
    if (e.story) {
      e.label = e.sample          // human step label, even when bundled
      e.highlight = true
      e.dim = false
    } else {
      e.label = e.count === 1 ? e.sample : `${e.count} flows`
      e.highlight = !!focusId && e.related
      e.dim = !!focusId && !e.related
    }
    out.push(e)
  }
  return out
}

/**
 * Position file boxes inside an expanded module. With intra-module dataflow
 * edges, use dagre for a clean layered left→right layout; with none, fall back
 * to a compact square-ish grid (never a tall single column). Returns relative
 * positions (top-left, normalized to 0,0) and the body bounding box.
 *
 * @param {Array} fileNodes - file render nodes (with computed w/h).
 * @param {Array<[string,string]>} fEdges - [fromFileId, toFileId] edges.
 * @returns {{ pos: Object, w: number, h: number }}
 */
// Spacing for file-box positioning, shared by the Dagre seed and the ELK layout
// so the seed→ELK transition doesn't jump (dagre nodesep/ranksep ↔ elk
// nodeNode / betweenLayers).
export const FILE_SPACING = { nodesep: 22, ranksep: 56 }

/**
 * Compact square-ish grid for file boxes with no intra-module dataflow — never a
 * tall single column. Both the Dagre seed and the ELK layout use this for the
 * edge-less case (ELK adds nothing when there are no edges to route). Returns
 * relative positions (top-left, from 0,0) and the body bounding box.
 */
export function gridLayoutFiles(fileNodes) {
  if (fileNodes.length === 0) return { pos: {}, w: 0, h: 0 }
  const pos = {}
  let w = 0, h = 0
  const cols = Math.max(1, Math.round(Math.sqrt(fileNodes.length)))
  const colW = Math.max(...fileNodes.map(n => n.w))
  const rowH = []
  fileNodes.forEach((n, i) => { const r = Math.floor(i / cols); rowH[r] = Math.max(rowH[r] || 0, n.h) })
  const rowY = []; let acc = 0
  rowH.forEach((hh, r) => { rowY[r] = acc; acc += hh + M_GAP })
  fileNodes.forEach((n, i) => {
    const c = i % cols, r = Math.floor(i / cols)
    const x = c * (colW + M_GAP), y = rowY[r]
    pos[n.id] = { x, y }
    w = Math.max(w, x + n.w); h = Math.max(h, y + n.h)
  })
  return { pos, w, h }
}

function layoutFiles(fileNodes, fEdges) {
  if (fileNodes.length === 0) return { pos: {}, w: 0, h: 0 }
  if (fEdges.length === 0) return gridLayoutFiles(fileNodes)

  const pos = {}
  let w = 0, h = 0

  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: FILE_SPACING.nodesep, ranksep: FILE_SPACING.ranksep, marginx: 0, marginy: 0 })
  g.setDefaultEdgeLabel(() => ({}))
  fileNodes.forEach(n => g.setNode(n.id, { width: n.w, height: n.h }))
  fEdges.forEach(([a, b]) => { if (g.hasNode(a) && g.hasNode(b)) g.setEdge(a, b) })
  dagre.layout(g)
  // dagre gives centres; convert to top-left and normalize so the body starts at 0,0.
  fileNodes.forEach(n => { const d = g.node(n.id); pos[n.id] = { x: d.x - n.w / 2, y: d.y - n.h / 2 } })
  const minX = Math.min(...fileNodes.map(n => pos[n.id].x))
  const minY = Math.min(...fileNodes.map(n => pos[n.id].y))
  fileNodes.forEach(n => {
    pos[n.id].x -= minX; pos[n.id].y -= minY
    w = Math.max(w, pos[n.id].x + n.w); h = Math.max(h, pos[n.id].y + n.h)
  })
  return { pos, w, h }
}

function groupBy(arr, keyFn) {
  const out = {}
  for (const item of arr) {
    const k = keyFn(item)
    ;(out[k] = out[k] || []).push(item)
  }
  return out
}
