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
  const base = file.split('/').pop()
  const byBase = list.find(b => (b.linkedFiles || []).some(f => f.split('/').pop() === base))
  return byBase ? byBase.id : null
}
