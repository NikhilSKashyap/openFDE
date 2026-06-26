// Run: node src/store/orientRoute.test.mjs   (standalone node:assert — no test runner)
import assert from 'node:assert/strict'
import { resolveRoute, routeRunsImplementation, ROUTER_UNAVAILABLE } from './orientRoute.js'

// 1. A missing / malformed / unknown-mode route FAILS CLOSED — error, no edits, never council.
for (const bad of [null, undefined, {}, { mode: 'bogus' }, { mode: '' }, { mode: 'councilish' }]) {
  const r = resolveRoute(bad)
  assert.equal(r.mode, 'error', `bad route ${JSON.stringify(bad)} must fail closed (got ${r.mode})`)
  assert.equal(r.allowEdits, false, 'fail-closed route must not allow edits')
  assert.equal(r.reason, ROUTER_UNAVAILABLE)
  assert.ok(!routeRunsImplementation(r), 'fail-closed route must not run')
}

// 2. Every valid mode passes through UNCHANGED (no rewriting to council).
for (const mode of ['program', 'council', 'ask', 'issue', 'clarify']) {
  const route = { mode, allowEdits: mode === 'program' || mode === 'council', reason: 'x' }
  assert.equal(resolveRoute(route), route, `${mode} must pass through unchanged`)
}

// 3. Only program/council dispatch a run; everything else (incl. the error route) does not.
assert.ok(routeRunsImplementation({ mode: 'program' }))
assert.ok(routeRunsImplementation({ mode: 'council' }))
for (const mode of ['ask', 'issue', 'clarify', 'error']) {
  assert.ok(!routeRunsImplementation({ mode }), `${mode} must not dispatch a run`)
}

console.log('orientRoute.test.mjs: all assertions passed')
