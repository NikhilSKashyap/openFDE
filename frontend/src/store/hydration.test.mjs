// Standalone assertions for the hydration readiness model — run with: node src/store/hydration.test.mjs
import assert from 'node:assert'
import { HYDRATION, resolveHydration, isRestoring, isConfirmedEmpty, surfaceView } from './hydration.js'

// 1. Loading until a response arrives — an empty array while loading is NOT confirmed empty.
assert.equal(isRestoring(HYDRATION.LOADING), true)
assert.equal(surfaceView(HYDRATION.LOADING, false), 'restoring')   // boot: skeleton, never "0 done"

// 2. A confirmed empty response IS the empty state.
let s = resolveHydration(HYDRATION.LOADING, { responded: true, hasData: false })
assert.equal(s, HYDRATION.EMPTY)
assert.equal(isConfirmedEmpty(s), true)
assert.equal(surfaceView(s, false), 'empty')

// 3. A response with data is live.
s = resolveHydration(HYDRATION.LOADING, { responded: true, hasData: true })
assert.equal(s, HYDRATION.LIVE)
assert.equal(surfaceView(s, true), 'data')

// 4. STALE/NULL PRESERVATION: a transient (no response) never downgrades a known status to empty.
assert.equal(resolveHydration(HYDRATION.LIVE, { responded: false, hasData: false }), HYDRATION.LIVE)
assert.equal(resolveHydration(HYDRATION.EMPTY, { responded: false, hasData: false }), HYDRATION.EMPTY)
assert.equal(resolveHydration(HYDRATION.RESTORED, { responded: false, hasData: false }), HYDRATION.RESTORED)
// only a still-loading surface degrades to error on a transient (and error still shows "restoring").
assert.equal(resolveHydration(HYDRATION.LOADING, { responded: false, hasData: false }), HYDRATION.ERROR)
assert.equal(surfaceView(HYDRATION.ERROR, false), 'restoring')

// 5. A populated surface that gets a transient-empty response keeps showing its data (cache-first).
assert.equal(surfaceView(resolveHydration(HYDRATION.LIVE, { responded: false, hasData: false }), true), 'data')

// 6. A cached/boot empty (NOT authoritative) is "restored", not "confirmed_empty" — still restoring.
s = resolveHydration(HYDRATION.LOADING, { responded: true, hasData: false, authoritative: false })
assert.equal(s, HYDRATION.RESTORED)
assert.equal(surfaceView(s, false), 'restoring')
// once a live status exists, a non-authoritative empty must not wipe it to empty.
assert.equal(resolveHydration(HYDRATION.LIVE, { responded: true, hasData: false, authoritative: false }), HYDRATION.LIVE)

console.log('hydration.test.mjs: all assertions passed')
