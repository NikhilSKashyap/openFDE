// Per-surface hydration readiness — the single model that decides "restoring…" vs a real empty
// state, so OpenFDE's memory (OpenPM, Story, prompt rail, Council inbox) never flashes a FALSE empty
// during boot. The STATUS, not array length, drives the copy: a surface stays "restoring" until a
// response confirms it, and a null/timeout/empty transient never wipes populated/known state.

export const HYDRATION = Object.freeze({
  LOADING: 'loading',                 // no response yet → skeleton, never "0 / empty"
  RESTORED: 'restored_from_cache',    // showing last-known-good cache; a fresh fetch is in flight
  LIVE: 'live',                       // confirmed fresh data
  EMPTY: 'confirmed_empty',           // the server authoritatively returned nothing
  ERROR: 'error',                     // fetch failed → keep what we had, never claim empty
})

/**
 * Fold a fetch outcome into the next status. CACHE-FIRST: a transient (no response) never downgrades
 * a known status to empty, and an empty response is only "confirmed_empty" when authoritative.
 *
 * @param {string} prev    current status
 * @param {object} outcome
 * @param {boolean} outcome.responded     the server answered (vs network/timeout/null)
 * @param {boolean} outcome.hasData       the response carried rows
 * @param {boolean} [outcome.authoritative=true]  this response is the source of truth for emptiness
 *                                                 (e.g. a full endpoint, not a boot/cached one)
 * @returns {string} next status
 */
export function resolveHydration(prev, { responded, hasData, authoritative = true }) {
  if (!responded) return prev === HYDRATION.LOADING ? HYDRATION.ERROR : prev   // transient → keep cache
  if (hasData) return HYDRATION.LIVE
  if (authoritative) return HYDRATION.EMPTY
  return prev === HYDRATION.LIVE ? HYDRATION.LIVE : HYDRATION.RESTORED          // cached-empty, unconfirmed
}

// True while a surface should show a "restoring…" skeleton instead of an empty state — i.e. until a
// response has CONFIRMED the data (live) or CONFIRMED there is none (confirmed_empty).
export function isRestoring(status) {
  return status === HYDRATION.LOADING || status === HYDRATION.RESTORED || status === HYDRATION.ERROR
}

export function isConfirmedEmpty(status) {
  return status === HYDRATION.EMPTY
}

/**
 * The one decision every memory surface makes for boot: given the readiness status and whether it
 * currently has rows, should it render the data, a "restoring…" skeleton, or a confirmed-empty state?
 * @returns {'data'|'restoring'|'empty'}
 */
export function surfaceView(status, hasRows) {
  if (hasRows) return 'data'
  return isConfirmedEmpty(status) ? 'empty' : 'restoring'
}
