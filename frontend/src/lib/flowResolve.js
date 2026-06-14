/**
 * Pure resolution for the failure-flow lens: map a primaryPath node
 * ({file, function, line}) to the arch-graph function that CONTAINS it.
 *
 * The traceback gives a BARE frame name ('create') and a line; the arch graph
 * keys methods by their qualified name ('Completions.create'). We resolve by
 * file + line FIRST — the function whose start ≤ line and whose successor
 * starts after the line (line containment) — then fall back to qualified/base
 * name as a tiebreak. This picks Completions.create over Transcriptions.create
 * for a failure at aisuite/client.py:364.
 *
 * Kept dependency-free so it is unit-testable without a browser.
 */
export function pickPrimaryFn(fns, node) {
  if (!node || !node.file) return null
  const inFile = (fns || [])
    .filter((f) => f && f.path === node.file)
    .sort((a, b) => (a.line || 0) - (b.line || 0))
  if (!inFile.length) return null
  const base = node.function ? String(node.function).split('.').pop() : null

  // 1) Line containment — the greatest start ≤ line is the enclosing function
  //    (the next function in sorted order, if any, starts after it).
  let byLine = null
  if (node.line != null) {
    for (const f of inFile) if ((f.line || 0) <= node.line) byLine = f
  }
  if (byLine && (!base || String(byLine.name).split('.').pop() === base)) return byLine

  // 2) Name match — an EXACT qualified name ('Other.run') wins over a bare
  //    last-segment match ('run'), so a qualified node with no line still
  //    resolves precisely. Within the chosen set, prefer greatest start ≤ line.
  if (base) {
    const exact = inFile.filter((f) => f.name === node.function)
    const named = exact.length ? exact
      : inFile.filter((f) => String(f.name).split('.').pop() === base)
    let np = null
    if (node.line != null) for (const f of named) if ((f.line || 0) <= node.line) np = f
    if (np) return np
    if (named.length) return named[0]
  }
  return byLine || null
}
