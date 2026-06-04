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

// ── Layout constants (canvas units) ─────────────────────────────────────────
const M_HEADER = 32
const M_PAD = 16
const M_GAP = 22
const M_COLS = 1            // single vertical file stack — readability over compactness (Batch 4)
const M_LEFT_LANE = 96     // left lane inside a module for cross-file rollup arcs

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
  const files = (archGraph && archGraph.files) || []
  const functions = (archGraph && archGraph.functions) || []

  const filesByModule = groupBy(files, f => f.moduleId)
  const fnsByPath = groupBy(functions, fn => fn.path)

  const nodes = []
  const fileById = {}
  const fnById = {}

  for (const box of boxes) {
    const isModule = !!box.moduleId
    const moduleExpanded = isModule && expanded.has(box.id) && !!archGraph

    if (!moduleExpanded) {
      // Collapsed (or non-drillable) module: render as-is.
      const node = {
        id: box.id, kind: 'module', expanded: false, drillable: isModule,
        x: box.x, y: box.y, w: box.w, h: box.h, type: box.type, title: box.title, box,
        files: [],
      }
      nodes.push(node)
      continue
    }

    // ── Expanded module: lay out file children ────────────────────────────
    const modFiles = (filesByModule[box.moduleId] || [])
      .slice()
      .sort((a, b) => a.path.localeCompare(b.path))

    // First pass: size every file box.
    const fileNodes = modFiles.map(f => {
      const fid = fileId(f.path)
      const fExpanded = expanded.has(fid)
      if (!fExpanded) {
        return { id: fid, kind: 'file', expanded: false, file: f, type: box.type,
                 w: F_MIN_W, h: F_HEADER + 18, functions: [], _rx: 0, _ry: 0 }
      }
      const fns = (fnsByPath[f.path] || []).slice().sort((a, b) => (a.line || 0) - (b.line || 0))
      if (fns.length === 0) {
        return { id: fid, kind: 'file', expanded: true, file: f, type: box.type,
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
          id: fnId(fn.path, fn.name), kind: 'function', fn, type: box.type,
          w: FN_W, h: FN_H,
          _rx: F_PAD + col * (FN_W + FN_GAP),
          _ry: F_HEADER + F_PAD + row * (FN_H + FN_GAP),
        }
      })
      return {
        id: fid, kind: 'file', expanded: true, file: f, type: box.type,
        // + F_LANE on the right hosts the nested call-arc lane (Step 26).
        w: Math.max(F_MIN_W, gridW + F_PAD + F_LANE),
        h: F_HEADER + F_PAD + gridH + F_PAD,
        functions: fnNodes, _rx: 0, _ry: 0,
      }
    })

    // Pack file boxes into a uniform-column grid (avoids overlap).
    const colW = fileNodes.length ? Math.max(...fileNodes.map(n => n.w)) : F_MIN_W
    const rowCount = Math.ceil(fileNodes.length / M_COLS) || 0
    const rowHeights = []
    for (let r = 0; r < rowCount; r++) {
      const inRow = fileNodes.slice(r * M_COLS, r * M_COLS + M_COLS)
      rowHeights.push(inRow.length ? Math.max(...inRow.map(n => n.h)) : 0)
    }
    const rowY = []
    let acc = M_HEADER + M_PAD
    for (let r = 0; r < rowCount; r++) { rowY.push(acc); acc += rowHeights[r] + M_GAP }
    const bodyH = rowCount ? (acc - M_GAP) : (M_HEADER + M_PAD)
    const usedCols = Math.min(M_COLS, fileNodes.length || 1)
    const bodyW = usedCols * colW + (usedCols - 1) * M_GAP

    // Module width reserves a left lane (cross-file rollup arcs) + the file
    // column (each file already carries its own right lane for same-file arcs).
    const modW = Math.max(box.w, M_PAD + M_LEFT_LANE + bodyW + M_PAD)
    const modH = Math.max(box.h, bodyH + M_PAD)

    fileNodes.forEach((fn, i) => {
      const col = i % M_COLS
      const row = Math.floor(i / M_COLS)
      fn._rx = M_PAD + M_LEFT_LANE + col * (colW + M_GAP)
      fn._ry = rowY[row]
    })

    // Resolve absolute coordinates (module x/y + relative offsets).
    const fileAbs = fileNodes.map(fn => {
      const fx = box.x + fn._rx
      const fy = box.y + fn._ry
      const functionsAbs = (fn.functions || []).map(g => {
        const gx = fx + g._rx
        const gy = fy + g._ry
        const node = { ...g, x: gx, y: gy }
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
    nodes.push(modNode)
  }

  // ── Effective top-level layout pass ─────────────────────────────────────
  // Expanded modules grow in place. Rather than re-flowing everything (which
  // scrambles the persisted flow order and tangles arrows), keep every box where
  // the user put it and only shove the boxes an expansion now overlaps further
  // right, cascading down the chain. Render-only — persisted box x/y untouched.
  const anyExpanded = nodes.some(n => n.expanded)
  if (anyExpanded) separateNodes(nodes)

  // Effective boxes carry the (possibly repacked) geometry for arrows + ports.
  const effectiveBoxes = nodes.map(n => ({ ...n.box, x: n.x, y: n.y, w: n.w, h: n.h }))

  // Content bounds for the scrollable / zoomable viewport.
  let maxX = 800, maxY = 600
  for (const n of nodes) {
    maxX = Math.max(maxX, n.x + n.w)
    maxY = Math.max(maxY, n.y + n.h)
  }
  const bounds = { w: maxX + 160, h: maxY + 160 }

  return { nodes, effectiveBoxes, bounds, fileById, fnById }
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
  const { mode = 'focused', focusId = null, storyFlowIds = null, flowIdToStep = null } = opts
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
    const crossFile = fw.fromFile !== fw.toFile

    // Story mode: keep only flows on the story path (function-level, human label).
    // Other modes: focused rolls unrelated cross-file flows up to file level.
    let a, b, related, storyFlow = false, stepLabel = null, stepOrder = null
    if (story) {
      if (!storyFlowIds.has(fw.id)) continue
      a = fa; b = fb; related = true; storyFlow = true
      const step = flowIdToStep && flowIdToStep[fw.id]
      if (step) { stepLabel = step.label; stepOrder = step.order }
    } else {
      related = focusId ? flowRelatesToFocus(fw, focusId) : false
      a = fa; b = fb
      if (mode !== 'all' && crossFile && !related) {
        a = resolveAt(fw.fromFunctionId, fw.fromFile, fw.fromModuleId, 1)
        b = resolveAt(fw.toFunctionId, fw.toFile, fw.toModuleId, 1)
      }
    }
    if (!a || !b || a.id === b.id) continue
    const route = sameFile ? 'arc-right'
      : (fw.fromModuleId === fw.toModuleId ? 'arc-left' : 'bezier')
    if (a.level === 0 && b.level === 0) continue   // module↔module = persisted arrows
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

function groupBy(arr, keyFn) {
  const out = {}
  for (const item of arr) {
    const k = keyFn(item)
    ;(out[k] = out[k] || []).push(item)
  }
  return out
}
