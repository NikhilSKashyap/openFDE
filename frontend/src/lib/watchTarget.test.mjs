// Standalone assertions for the Watch-glow target helpers — run with:
//   node src/lib/watchTarget.test.mjs
import assert from 'node:assert'
import { fileNodeId, functionNodeId, watchTargetId, moduleBoxIdForFile } from './watchTarget.js'

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

// the most specific known target: function id WINS over file id when a function name is known…
assert.equal(watchTargetId('openfde/server.py', 'start'),
             'box:function:openfde/server.py:start')
// …and falls back to the file id when no function is known.
assert.equal(watchTargetId('openfde/server.py', null), 'box:file:openfde/server.py')
assert.equal(watchTargetId('openfde/server.py', ''), 'box:file:openfde/server.py')
assert.equal(watchTargetId('openfde/server.py', undefined), 'box:file:openfde/server.py')

console.log('watchTarget.test.mjs: all assertions passed')
