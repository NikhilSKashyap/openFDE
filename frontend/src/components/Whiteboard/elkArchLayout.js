/**
 * elkArchLayout.js — ELK is the canvas layout: ELK `layered` placement
 * (direction RIGHT) plus orthogonal edge routing for intra-module file→file
 * flows. Dagre (computeArchLayout) remains only as the synchronous first-paint
 * seed and the crash fallback — see WhiteboardCanvas.
 *
 * Mirrors computeArchLayout EXACTLY except for *positioning file boxes inside an
 * expanded module*: dagre's layered LR is swapped for ELK, with spacing tuned to
 * dagre's nodesep/ranksep so the seed→ELK transition doesn't jump. Everything
 * else (file/function sizing, module assembly, top-level separation, return
 * shape) is SHARED code from archLayout.js.
 *
 * Async (ELK has no sync API). WhiteboardCanvas renders the dagre seed until a
 * fresh ELK result lands, and stays on the seed on any ELK failure.
 *
 * ELK is dynamically imported the first time it's needed, so it code-splits into
 * its own chunk and only loads once the canvas actually has flows to route.
 */
import {
  sizeModuleFiles, assembleModule, finalizeLayout, moduleCollapsedNode,
  gridLayoutFiles, FILE_SPACING,
} from './archLayout'

// Lazy singleton — the ELK bundle loads only when the ELK engine is first used.
let _elkPromise = null
function getElk() {
  if (!_elkPromise) {
    _elkPromise = import('elkjs/lib/elk.bundled.js').then(mod => new mod.default())
  }
  return _elkPromise
}

/**
 * ELK counterpart to computeArchLayout. Same args, same return shape:
 *   { nodes, effectiveBoxes, bounds, fileById, fnById }
 */
export async function computeArchLayoutElk(boxes, archGraph, expanded) {
  const nodes = []
  const fileById = {}
  const fnById = {}

  for (const box of boxes) {
    const isModule = !!box.moduleId
    const moduleExpanded = isModule && expanded.has(box.id) && !!archGraph

    if (!moduleExpanded) {
      nodes.push(moduleCollapsedNode(box, isModule))
      continue
    }

    const { fileNodes, fEdges } = sizeModuleFiles(box, archGraph, expanded)
    const fl = await layoutFilesElk(fileNodes, fEdges)
    nodes.push(assembleModule(box, fileNodes, fl, fileById, fnById))
  }

  return finalizeLayout(nodes, fileById, fnById)
}

/**
 * Position file boxes inside one expanded module with ELK's layered algorithm
 * AND capture ELK's orthogonal routing for the file→file edges. No intra-module
 * dataflow → reuse the shared grid (parity with dagre's fallback, no routes) so
 * the comparison isolates exactly the layered+routed-vs-dagre case.
 *
 * Returns relative node positions normalized to (0,0), the body bounding box,
 * and `routes` — ELK's orthogonal bend-point polylines per file→file edge, in
 * the same normalized space (so assembleModule offsets them like the nodes).
 *
 * @param {Array} fileNodes - sized file render nodes (id/w/h).
 * @param {Array<[string,string]>} fEdges - [fromFileId, toFileId] edges.
 * @returns {Promise<{ pos: Object, w: number, h: number, routes: Array }>}
 */
async function layoutFilesElk(fileNodes, fEdges) {
  if (fileNodes.length === 0) return { pos: {}, w: 0, h: 0, routes: [] }
  if (fEdges.length === 0) return gridLayoutFiles(fileNodes)

  const elk = await getElk()
  const graph = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.edgeRouting': 'ORTHOGONAL',
      // Match dagre nodesep (within-layer) and ranksep (between-layer).
      'elk.spacing.nodeNode': String(FILE_SPACING.nodesep),
      'elk.layered.spacing.nodeNodeBetweenLayers': String(FILE_SPACING.ranksep),
      'elk.spacing.edgeNode': '16',
      'elk.layered.spacing.edgeNodeBetweenLayers': '16',
      'elk.padding': '[top=0,left=0,bottom=0,right=0]',
    },
    children: fileNodes.map(n => ({ id: n.id, width: n.w, height: n.h })),
    edges: fEdges.map(([a, b], i) => ({ id: `fe${i}`, sources: [a], targets: [b], _a: a, _b: b })),
  }

  const res = await elk.layout(graph)

  // ELK returns top-left coordinates already (no centre→corner conversion).
  const pos = {}
  for (const c of (res.children || [])) pos[c.id] = { x: c.x || 0, y: c.y || 0 }

  // Normalize to 0,0 (any node ELK somehow omitted defaults to the origin).
  let minX = Infinity, minY = Infinity
  for (const n of fileNodes) {
    const p = pos[n.id] || (pos[n.id] = { x: 0, y: 0 })
    minX = Math.min(minX, p.x); minY = Math.min(minY, p.y)
  }
  if (!isFinite(minX)) { minX = 0; minY = 0 }

  let w = 0, h = 0
  for (const n of fileNodes) {
    const p = pos[n.id]
    p.x -= minX; p.y -= minY
    w = Math.max(w, p.x + n.w); h = Math.max(h, p.y + n.h)
  }

  // Pull ELK's routed bend-points for each edge, normalized to match the nodes.
  const routes = []
  const edgeMeta = {}
  for (const e of graph.edges) edgeMeta[e.id] = { fromId: e._a, toId: e._b }
  for (const e of (res.edges || [])) {
    const s = e.sections && e.sections[0]
    const meta = edgeMeta[e.id]
    if (!s || !meta) continue
    const raw = [s.startPoint, ...(s.bendPoints || []), s.endPoint].filter(Boolean)
    if (raw.length < 2) continue
    routes.push({
      fromId: meta.fromId, toId: meta.toId,
      points: raw.map(pt => ({ x: pt.x - minX, y: pt.y - minY })),
    })
  }

  return { pos, w, h, routes }
}
