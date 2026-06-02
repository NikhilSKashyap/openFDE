/**
 * storyLayout.js — staged left-to-right layout for Story mode (Batch 5b).
 *
 * Lays the backend story out as readable phases across the canvas:
 *   [Inputs] → [Phase 1] → [Phase 2] → … → [Outputs]
 * Each phase is a column with a header (number + label); related functions stack
 * vertically inside it. Story flows are drawn left-to-right between the function
 * boxes so the eye can follow the path via the column headers, not by reading
 * every function name.
 *
 * Pure geometry: returns fully-positioned nodes so the canvas just draws.
 */

const ORIGIN_X = 80
const ORIGIN_Y = 80
const HEADER_H = 34
const HEADER_GAP = 14
const COL_W = 196
const COL_GAP = 86
const IO_W = 150
const BOX_H = 50
const BOX_GAP = 16
const IO_LINE = 16

const shortName = (raw) => raw.split(':').pop().split('.').pop()

/**
 * @param {Object} story - backend story (steps, inputs, outputs, title).
 * @param {Object} archGraph - { functions, flows }.
 * @returns {Object|null} { columns, arrows, nodeById, bounds, title } or null.
 */
export function computeStoryLayout(story, archGraph) {
  if (!story || !Array.isArray(story.steps) || story.steps.length === 0) return null

  const fnById = {}
  for (const fn of (archGraph?.functions || [])) fnById[fn.id] = fn
  const flowById = {}
  for (const fw of (archGraph?.flows || [])) flowById[fw.id] = fw

  const steps = [...story.steps].sort((a, b) => a.order - b.order)
  const columns = []
  let colX = ORIGIN_X

  // ── Inputs column (left) ────────────────────────────────────────────────
  const inputs = (story.inputs || []).slice(0, 6)
  if (inputs.length) {
    columns.push({
      kind: 'io', role: 'inputs', label: 'Inputs', items: inputs,
      box: { x: colX, y: ORIGIN_Y + HEADER_H + HEADER_GAP, w: IO_W, h: 22 + inputs.length * IO_LINE },
    })
    colX += IO_W + COL_GAP
  }

  // ── Phase columns ───────────────────────────────────────────────────────
  const nodeById = {}
  const placed = new Set()
  let maxBottom = ORIGIN_Y + HEADER_H
  const phaseCols = []
  for (const st of steps) {
    const nodes = []
    let y = ORIGIN_Y + HEADER_H + HEADER_GAP
    for (const nid of (st.nodeIds || [])) {
      if (placed.has(nid)) continue
      placed.add(nid)
      const raw = nid.replace('box:', '')
      const fn = fnById[raw] || null
      const node = {
        id: nid, fn,
        label: shortName(fn ? fn.name : raw) + '()',
        sub: fn ? (fn.purpose || '') : '',
        x: colX, y, w: COL_W, h: BOX_H,
      }
      nodes.push(node)
      nodeById[nid] = node
      y += BOX_H + BOX_GAP
    }
    const col = {
      kind: 'phase', order: st.order, label: st.label, x: colX, nodes,
      header: { x: colX, y: ORIGIN_Y, w: COL_W, h: HEADER_H },
    }
    columns.push(col)
    phaseCols.push(col)
    maxBottom = Math.max(maxBottom, y)
    colX += COL_W + COL_GAP
  }

  // ── Outputs column (right) ──────────────────────────────────────────────
  const outputs = (story.outputs || []).slice(0, 6)
  if (outputs.length) {
    columns.push({
      kind: 'io', role: 'outputs', label: 'Outputs', items: outputs,
      box: { x: colX, y: ORIGIN_Y + HEADER_H + HEADER_GAP, w: IO_W, h: 22 + outputs.length * IO_LINE },
    })
    colX += IO_W
  }

  // ── Story flow arrows (left-to-right between placed functions) ───────────
  const arrows = []
  const seen = new Set()
  for (const st of steps) {
    for (const fid of (st.flowIds || [])) {
      const fw = flowById[fid]
      if (!fw) continue
      const a = nodeById[`box:${fw.fromFunctionId}`]
      const b = nodeById[`box:${fw.toFunctionId}`]
      if (!a || !b || a === b) continue
      const key = `${a.id}>${b.id}`
      if (seen.has(key)) continue
      seen.add(key)
      arrows.push({ id: `sa:${key}`, from: a, to: b, intra: a.x === b.x })
    }
  }
  // Conceptual: inputs → first phase, last phase → outputs.
  const firstPhase = phaseCols[0]
  const lastPhase = phaseCols[phaseCols.length - 1]
  const inCol = columns.find(c => c.role === 'inputs')
  const outCol = columns.find(c => c.role === 'outputs')
  if (inCol && firstPhase && firstPhase.nodes[0]) {
    arrows.push({ id: 'sa:in', from: { x: inCol.box.x, y: inCol.box.y, w: inCol.box.w, h: inCol.box.h },
      to: firstPhase.nodes[0], intra: false, faint: true })
  }
  if (outCol && lastPhase && lastPhase.nodes[0]) {
    arrows.push({ id: 'sa:out', from: lastPhase.nodes[0],
      to: { x: outCol.box.x, y: outCol.box.y, w: outCol.box.w, h: outCol.box.h }, intra: false, faint: true })
  }

  const bounds = { w: colX + 120, h: maxBottom + 100 }
  return { columns, arrows, nodeById, bounds, title: story.title }
}
