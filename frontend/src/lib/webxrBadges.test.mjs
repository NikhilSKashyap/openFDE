// Standalone assertions for the WebXR canvas-badge helpers — run with:
//   node src/lib/webxrBadges.test.mjs
import assert from 'node:assert'
import { badgeMapFromSummary, badgeNodesById, badgeForPath } from './webxrBadges.js'

// Missing / malformed summaries → empty map (fail quiet, no badges).
assert.deepEqual(badgeMapFromSummary(null), {})
assert.deepEqual(badgeMapFromSummary({}), {})
assert.deepEqual(badgeMapFromSummary({ fileBadges: 'nope' }), {})
assert.deepEqual(badgeMapFromSummary({ fileBadges: [] }), {})

// Entrypoints + assets → keyed by repo-relative path with kind + label.
const summary = {
  ok: true,
  fileBadges: [
    { path: 'src/xr/scene.jsx', kind: 'entrypoint', label: 'XR entrypoint' },
    { path: 'public/duck.glb',  kind: 'asset',      label: '3D asset' },
    { path: 'bad', kind: 'mystery' },   // unknown kind → dropped
    { kind: 'asset' },                  // no path → dropped
  ],
}
const map = badgeMapFromSummary(summary)
assert.deepEqual(map['src/xr/scene.jsx'], { kind: 'entrypoint', label: 'XR entrypoint' })
assert.deepEqual(map['public/duck.glb'], { kind: 'asset', label: '3D asset' })
assert.equal(Object.keys(map).length, 2)   // the two malformed entries are dropped

// A label-less badge falls back to the canonical label for its kind.
const noLabel = badgeMapFromSummary({ fileBadges: [{ path: 'a.glb', kind: 'asset' }] })
assert.equal(noLabel['a.glb'].label, '3D asset')

// Entrypoint outranks asset when one path is claimed by both, regardless of order.
const both = badgeMapFromSummary({ fileBadges: [
  { path: 'dup.jsx', kind: 'asset',      label: '3D asset' },
  { path: 'dup.jsx', kind: 'entrypoint', label: 'XR entrypoint' },
] })
assert.deepEqual(both['dup.jsx'], { kind: 'entrypoint', label: 'XR entrypoint' })

// Re-keying onto canvas file-node ids matches the shared box-id scheme.
const byId = badgeNodesById(map)
assert.deepEqual(byId['box:file:src/xr/scene.jsx'], { kind: 'entrypoint', label: 'XR entrypoint' })
assert.deepEqual(byId['box:file:public/duck.glb'], { kind: 'asset', label: '3D asset' })
assert.equal(Object.keys(badgeNodesById(null)).length, 0)

// Exact path lookup; misses and bad input return null (never throws).
assert.deepEqual(badgeForPath(map, 'src/xr/scene.jsx'), { kind: 'entrypoint', label: 'XR entrypoint' })
assert.equal(badgeForPath(map, 'nope.js'), null)
assert.equal(badgeForPath(null, 'x'), null)
assert.equal(badgeForPath(map, null), null)

console.log('webxrBadges: all assertions passed')
