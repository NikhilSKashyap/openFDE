const DEFAULT_PROMPT = 'Describe what this module does...'

/**
 * Generate a structured plan preview from the current canvas state.
 * Returns a plain JS object — the caller renders it.
 */
export function generatePlanPreview({ boxes, arrows }) {
  if (!boxes || boxes.length === 0) {
    return { empty: true }
  }

  const dottedBoxes = boxes.filter(b => b.type === 'dotted')
  const solidBoxes  = boxes.filter(b => b.type === 'solid')

  // ── Why ─────────────────────────────────────────────────────────────
  // Use box prompts as intent material. Skip default/empty prompts.
  const why = boxes
    .filter(b => b.prompt && b.prompt !== DEFAULT_PROMPT)
    .map(b => ({ id: b.id, title: b.title, prompt: b.prompt }))

  // ── What ─────────────────────────────────────────────────────────────
  const what = {
    dotted: dottedBoxes.map(b => ({
      id: b.id, title: b.title,
      files: b.linkedFiles ?? [],
    })),
    solid: solidBoxes.map(b => ({
      id: b.id, title: b.title,
      files: b.linkedFiles ?? [],
    })),
  }

  // ── Dataflow ──────────────────────────────────────────────────────────
  const dataflow = arrows.map(a => ({
    id: a.id,
    fromTitle: boxes.find(b => b.id === a.fromBox)?.title ?? '(deleted)',
    toTitle:   boxes.find(b => b.id === a.toBox)?.title   ?? '(deleted)',
    label:  a.label?.trim() || null,
    type:   a.type,
  }))

  // ── Permission Boundaries ─────────────────────────────────────────────
  const permissions = {
    agentFree:          dottedBoxes.map(b => b.title),
    requiresPermission: solidBoxes.map(b => b.title),
    dottedArrows:       dataflow.filter(a => a.type === 'dotted'),
    solidArrows:        dataflow.filter(a => a.type === 'solid'),
  }

  // ── How ──────────────────────────────────────────────────────────────
  const how = []
  if (dottedBoxes.length > 0) {
    how.push(`Agent can freely work in: ${dottedBoxes.map(b => b.title).join(', ')}.`)
  }
  if (solidBoxes.length > 0) {
    how.push(`Agent must request permission before modifying: ${solidBoxes.map(b => b.title).join(', ')}.`)
  }
  if (arrows.length > 0) {
    const labeled = arrows.filter(a => a.label?.trim())
    how.push(
      `${arrows.length} data flow${arrows.length > 1 ? 's' : ''} defined` +
      (labeled.length > 0 ? `, ${labeled.length} with labels.` : ' (none labeled yet).')
    )
  }
  if (how.length === 0) {
    how.push('Connect boxes with arrows to generate implementation direction.')
  }

  // ── Outcome ───────────────────────────────────────────────────────────
  const labeledArrowCount = arrows.filter(a => a.label?.trim()).length
  const outcome = {
    boxCount:         boxes.length,
    dottedCount:      dottedBoxes.length,
    solidCount:       solidBoxes.length,
    arrowCount:       arrows.length,
    labeledArrowCount,
  }

  // ── Open Questions ────────────────────────────────────────────────────
  const openQuestions = []

  boxes.forEach(b => {
    if (!b.prompt || b.prompt === DEFAULT_PROMPT) {
      openQuestions.push(`"${b.title}" has no prompt — what does this module do?`)
    }
  })

  const noFiles = boxes.filter(b => !b.linkedFiles || b.linkedFiles.length === 0)
  if (noFiles.length > 0) {
    openQuestions.push(
      `${noFiles.length} module${noFiles.length > 1 ? 's' : ''} have no linked files: ${noFiles.map(b => b.title).join(', ')}.`
    )
  }

  const unlabeled = arrows.filter(a => !a.label?.trim())
  if (unlabeled.length > 0) {
    openQuestions.push(
      `${unlabeled.length} arrow${unlabeled.length > 1 ? 's' : ''} have no label — what data flows through them?`
    )
  }

  const isolated = boxes.filter(b => {
    const connected = arrows.some(a => a.fromBox === b.id || a.toBox === b.id)
    return !connected
  })
  if (isolated.length > 0) {
    openQuestions.push(
      `${isolated.length} module${isolated.length > 1 ? 's' : ''} not connected to any flow: ${isolated.map(b => b.title).join(', ')}.`
    )
  }

  return { empty: false, why, what, dataflow, permissions, how, outcome, openQuestions }
}
