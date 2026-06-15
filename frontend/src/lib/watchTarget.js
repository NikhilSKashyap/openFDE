// watchTarget — pure helpers mapping a changed file (and, when known, its function) to the
// canvas box id that the Watch glow should pulse. Generic: no repo/file names are hardcoded.
//
// Box id scheme (shared with the canvas layout + computeRunRings):
//   module:   the persisted box id (box:module:… or a user box id)
//   file:     box:file:<repo-relative-path>
//   function: box:function:<repo-relative-path>:<function-name>

export const fileNodeId = (file) => `box:file:${file}`
export const functionNodeId = (file, name) => `box:function:${file}:${name}`

/**
 * The MOST SPECIFIC known target for a file edit: the function node when a function name is known
 * (the backend inferred it from the diff), otherwise the file node. This is what makes the glow
 * land on the function when we can, and fall back to the file when we can't.
 */
export const watchTargetId = (file, functionName) =>
  (functionName ? functionNodeId(file, functionName) : fileNodeId(file))

/**
 * The id of the canvas module box that owns `file`: an exact linkedFiles match first, then a
 * basename match (a Landed module that lists the file under a different relative path). Returns
 * null when the file maps to nothing on the canvas (Watch presupposes Land — no box, no glow).
 * Mirrors what computeRunRings uses to roll a file path up to its module.
 */
export const moduleBoxIdForFile = (file, boxes) => {
  if (!file) return null
  const list = boxes || []
  const exact = list.find(b => (b.linkedFiles || []).includes(file))
  if (exact) return exact.id
  // linkedPath DIR-PREFIX: module boxes cap linkedFiles (~25), so a deep file like
  // openfde/watch_function.py only maps to its module via the box's linkedPath.
  const byPath = list.find(b => b.linkedPath && (file === b.linkedPath || file.startsWith(`${b.linkedPath}/`)))
  if (byPath) return byPath.id
  const base = file.split('/').pop()
  const byBase = list.find(b => (b.linkedFiles || []).some(f => f.split('/').pop() === base))
  return byBase ? byBase.id : null
}

/**
 * The full file_activity → canvas plan for a changed file, in ONE place (so the live handler and
 * its test agree). Given the changed `file`, an optional inferred function name, and the persisted
 * boxes, returns what to expand and what to pulse — or null when the file maps to no module on the
 * canvas (no Land → no glow). Generic: no repo/file/function names hardcoded.
 *
 *   { moduleId, fileId, expandIds:[module, file], watchKey }
 *
 * expandIds drives setExpandedIds (module must open to lay out files; file must open to lay out
 * functions). watchKey is the MOST SPECIFIC pulse target: the function node when a name is known,
 * else the file node (computeRunRings rolls it up to whatever is visible).
 */
export const watchActivityTargets = (file, functionName, boxes) => {
  if (!file) return null
  const moduleId = moduleBoxIdForFile(file, boxes)
  if (!moduleId) return null
  const fileId = fileNodeId(file)
  return { moduleId, fileId, expandIds: [moduleId, fileId], watchKey: watchTargetId(file, functionName) }
}
