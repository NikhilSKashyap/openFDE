// Fail-closed routing for the unified Orient Run button.
//
// The backend intent router decides the mode; this guards the CLIENT against a stale/down router. An
// absent response or an unknown mode resolves to an explicit `error` route that runs NOTHING — never a
// silent fallback to council, which would turn a multi-slice product prompt into a standalone
// programId:null run (the P267 failure). Pure + framework-free so it is unit-testable.

export const ROUTE_MODES = new Set(['program', 'council', 'ask', 'issue', 'clarify'])

export const ROUTER_UNAVAILABLE =
  'Router unavailable — OpenFDE did not run this. Refresh/restart and try again.'

// Resolve a router response (or null) into the route the UI should act on. A missing or unknown-mode
// route becomes the fail-closed `error` route (no edits, runs nothing); a valid route passes through.
export function resolveRoute(route) {
  if (!route || !ROUTE_MODES.has(route.mode)) {
    return { mode: 'error', allowEdits: false, reason: ROUTER_UNAVAILABLE }
  }
  return route
}

// True only for the modes that dispatch a real run endpoint (program/council). ask/issue/clarify and
// the fail-closed error route never run anything.
export function routeRunsImplementation(route) {
  return route.mode === 'program' || route.mode === 'council'
}
