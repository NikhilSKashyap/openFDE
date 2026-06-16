// webxrBadges — index a WebXR summary's `fileBadges` by repo-relative path so the
// canvas (and file tree) can mark the files the WebXR pack flagged. Architecture
// metadata ONLY: which files are XR entrypoints / 3D assets. There is no runtime,
// device, or test lens behind these — just a path → { kind, label } lookup.
//
// Source: GET /api/plugins/webxr/summary, whose `fileBadges` is
//   [{ path: '<repo-relative>', kind: 'entrypoint' | 'asset', label }]
//
// The box-id scheme (box:file:<repo-relative-path>) is shared with watchTarget.js,
// so badges resolve to the same canvas file nodes the Watch glow targets.

import { fileNodeId } from './watchTarget.js'

const DEFAULT_LABEL = { entrypoint: 'XR entrypoint', asset: '3D asset' }

// Build a path → { kind, label } map from a summary. Unknown kinds and path-less
// entries are dropped; an entrypoint outranks an asset when one path is claimed by
// both (a file that is an entrypoint AND an asset reads as the entrypoint). Returns
// {} for a missing / malformed / empty summary so callers can fail quiet.
export function badgeMapFromSummary(summary) {
  const out = {}
  const badges = summary?.fileBadges
  if (!Array.isArray(badges)) return out
  for (const b of badges) {
    const path = b?.path
    const kind = b?.kind === 'entrypoint' ? 'entrypoint' : b?.kind === 'asset' ? 'asset' : null
    if (typeof path !== 'string' || !path || !kind) continue
    if (out[path]?.kind === 'entrypoint' && kind === 'asset') continue   // entrypoint wins
    out[path] = { kind, label: (typeof b.label === 'string' && b.label) || DEFAULT_LABEL[kind] }
  }
  return out
}

// Re-key a path map onto canvas file-node ids: { 'box:file:<path>': { kind, label } }.
// Mirrors watchTarget's id scheme so the canvas can resolve geometry by node id.
export function badgeNodesById(badgeMap) {
  const out = {}
  for (const [path, badge] of Object.entries(badgeMap || {})) out[fileNodeId(path)] = badge
  return out
}

// Look up the badge for one repo-relative file path (exact match), or null.
export function badgeForPath(badgeMap, path) {
  return (badgeMap && typeof path === 'string' && badgeMap[path]) || null
}
