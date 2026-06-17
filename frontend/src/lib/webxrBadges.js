// webxrBadges — index a WebXR summary's `fileBadges` by repo-relative path so the
// canvas (and file tree) can mark the files the WebXR pack flagged. Architecture
// metadata ONLY: XR API / Scene / Three / R3F / Shader / 3D asset hints. There is no
// runtime, device, or test lens behind these — just a path → { kind, label } lookup.
//
// Source: GET /api/plugins/webxr/summary, whose `fileBadges` is
//   [{ path: '<repo-relative>', kind, label }] with kind in:
//     entrypoint (XR API) | scene | framework (Three/R3F) | shader | asset (3D asset)
//
// The box-id scheme (box:file:<repo-relative-path>) is shared with watchTarget.js,
// so badges resolve to the same canvas file nodes the Watch glow targets.

import { fileNodeId } from './watchTarget.js'

// Per-file precedence for the SINGLE canvas pill: the strongest signal wins (a file that is an
// XR-API entrypoint AND uses Three reads as "XR API"). The details surface can still show all badges.
const KIND_RANK = { entrypoint: 5, scene: 4, framework: 3, shader: 2, asset: 1 }
const DEFAULT_LABEL = { entrypoint: 'XR API', scene: 'Scene', framework: 'Framework',
                        shader: 'Shader', asset: '3D asset' }

// Build a path → { kind, label } map from a summary. Unknown kinds and path-less entries are dropped;
// when a path carries several badges, the highest-ranked kind wins. Returns {} for a missing /
// malformed / empty summary so callers can fail quiet.
export function badgeMapFromSummary(summary) {
  const out = {}
  const badges = summary?.fileBadges
  if (!Array.isArray(badges)) return out
  for (const b of badges) {
    const path = b?.path
    const kind = b?.kind
    if (typeof path !== 'string' || !path || !(kind in KIND_RANK)) continue
    if (out[path] && KIND_RANK[out[path].kind] >= KIND_RANK[kind]) continue   // keep the strongest
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
