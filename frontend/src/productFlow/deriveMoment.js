/**
 * deriveMoment.js — the moment engine (FLOW.md spine, Step 28 Slice 1).
 *
 * Derives the current product moment from existing app state so the UI can know
 * "what is the user doing right now" without the user choosing a panel. Pure and
 * unit-testable. Deliberately simple — refined in later slices.
 *
 * Moments (FLOW.md): orient → understand → change → execute → review.
 */

export const MOMENTS = ['orient', 'understand', 'change', 'execute', 'review']

export const MOMENT_LABEL = {
  orient: 'Orient',
  understand: 'Understand',
  change: 'Change',
  execute: 'Execute',
  review: 'Review',
}

export const MOMENT_PROMPT = {
  orient: 'What do you want to build or change?',
  understand: 'Here’s what this is — ask or change it.',
  change: 'Describe the change, then Execute.',
  execute: 'Agents are working…',
  review: 'Review what changed.',
}

/**
 * @param {Object} s - flattened signals from app state:
 *   { executing:boolean, runStatus:string, reviewSignal:boolean,
 *     changeSignal:boolean, hasSelection:boolean }
 * @returns {'orient'|'understand'|'change'|'execute'|'review'}
 */
export function deriveMoment(s = {}) {
  if (s.executing || s.runStatus === 'running') return 'execute'
  if (s.reviewSignal) return 'review'
  if (s.changeSignal) return 'change'
  if (s.hasSelection) return 'understand'
  return 'orient'
}
