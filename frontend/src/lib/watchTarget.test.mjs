// Standalone assertions for the Watch-glow target helpers — run with:
//   node src/lib/watchTarget.test.mjs
import assert from 'node:assert'
import { fileNodeId, functionNodeId, watchTargetId, moduleBoxIdForFile, watchActivityTargets, watchFocusTargetId } from './watchTarget.js'

const boxes = [
  { id: 'box:module:api', linkedFiles: ['frontend/src/api/backend.js'] },
  { id: 'box:module:web', linkedFiles: ['openfde/server.py', 'openfde/cli.py'] },
]

// file path → file node id
assert.equal(fileNodeId('openfde/server.py'), 'box:file:openfde/server.py')

// (file, function) → function node id
assert.equal(functionNodeId('openfde/server.py', 'start'),
             'box:function:openfde/server.py:start')

// file path → module id (exact link, then basename fallback)
assert.equal(moduleBoxIdForFile('openfde/server.py', boxes), 'box:module:web')
assert.equal(moduleBoxIdForFile('vendor/server.py', boxes), 'box:module:web')   // basename match
assert.equal(moduleBoxIdForFile('totally/unrelated.ts', boxes), null)           // not landed → no glow
assert.equal(moduleBoxIdForFile('', boxes), null)
assert.equal(moduleBoxIdForFile('x.py', null), null)                            // tolerant of no boxes

// linkedPath DIR-PREFIX: a deep file NOT in the capped linkedFiles still maps to its module — the
// fix for the watch glow silently no-op'ing on most files (e.g. openfde/watch_function.py).
const lpBoxes = [{ id: 'box:module:openfde', linkedPath: 'openfde', linkedFiles: ['openfde/__init__.py'] }]
assert.equal(moduleBoxIdForFile('openfde/watch_function.py', lpBoxes), 'box:module:openfde')
assert.equal(moduleBoxIdForFile('frontend/src/App.jsx', lpBoxes), null)         // different tree → no match

// the most specific known target: function id WINS over file id when a function name is known…
assert.equal(watchTargetId('openfde/server.py', 'start'),
             'box:function:openfde/server.py:start')
// …and falls back to the file id when no function is known.
assert.equal(watchTargetId('openfde/server.py', null), 'box:file:openfde/server.py')
assert.equal(watchTargetId('openfde/server.py', ''), 'box:file:openfde/server.py')
assert.equal(watchTargetId('openfde/server.py', undefined), 'box:file:openfde/server.py')

// ── watchActivityTargets: the full file_activity → expand + pulse plan ────────────────────────
// A real module box whose linkedFiles is capped (does NOT list the deep file) — mirrors the live
// state.json where box:module:openfde lists ~25 files but owns hundreds via linkedPath.
const repoBoxes = [{ id: 'box:module:openfde', linkedPath: 'openfde', linkedFiles: ['openfde/__init__.py'] }]
const F = 'openfde/watch_function.py'

// (a) file activity expands the owning module AND the file node.
const withFn = watchActivityTargets(F, 'changed_line_numbers', repoBoxes)
assert.equal(withFn.moduleId, 'box:module:openfde')
assert.deepEqual(withFn.expandIds, ['box:module:openfde', 'box:file:openfde/watch_function.py'])

// (b) the inferred function name WINS over the file fallback for the pulse target.
assert.equal(withFn.watchKey, 'box:function:openfde/watch_function.py:changed_line_numbers')

// (c) fallback: when the function can't be resolved, the pulse target is the FILE node.
const noFn = watchActivityTargets(F, null, repoBoxes)
assert.deepEqual(noFn.expandIds, ['box:module:openfde', 'box:file:openfde/watch_function.py'])
assert.equal(noFn.watchKey, 'box:file:openfde/watch_function.py')

// (d) a file that maps to no module on the canvas → null (no Land → no glow).
assert.equal(watchActivityTargets('vendor/zzz/unrelated.ts', 'f', repoBoxes), null)
assert.equal(watchActivityTargets('', 'f', repoBoxes), null)

// ── watchFocusTargetId: the node the camera centers on, by VISIBILITY precedence ──────────────
const FN = 'box:function:openfde/watch_function.py:changed_line_numbers'
const FILE = 'box:file:openfde/watch_function.py'
const MOD = 'box:module:openfde'
// function laid out (module+file expanded) → center on the FUNCTION (the touched target).
assert.equal(watchFocusTargetId(F, 'changed_line_numbers', { [FN]: {} }, { [FILE]: {} }, MOD), FN)
// function NOT laid out yet, file is → center on the FILE (not the module/container).
assert.equal(watchFocusTargetId(F, 'changed_line_numbers', {}, { [FILE]: {} }, MOD), FILE)
// nothing expanded yet → fall back to the owning MODULE so the edit is at least on-screen.
assert.equal(watchFocusTargetId(F, 'changed_line_numbers', {}, {}, MOD), MOD)
// no function inferred → file when laid out, never a function node.
assert.equal(watchFocusTargetId(F, null, { [FN]: {} }, { [FILE]: {} }, MOD), FILE)

console.log('watchTarget.test.mjs: all assertions passed')
