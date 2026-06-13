// Standalone assertions for pickPrimaryFn — run with: node src/lib/flowResolve.test.mjs
import assert from 'node:assert'
import { pickPrimaryFn } from './flowResolve.js'

// The aisuite case: multiple create-like functions; a failure at client.py:364
// must resolve to Completions.create (start 357), NOT Transcriptions.create (462)
// and NOT a fallback.
const fns = [
  { name: 'Completions.__init__', path: 'aisuite/client.py', line: 320 },
  { name: 'Completions._tool_runner', path: 'aisuite/client.py', line: 340 },
  { name: 'Completions.create', path: 'aisuite/client.py', line: 357 },
  { name: 'Audio', path: 'aisuite/client.py', line: 440 },
  { name: 'Transcriptions.create', path: 'aisuite/client.py', line: 462 },
  { name: 'test_x', path: 'tests/client/test_client.py', line: 600 },
]

assert.equal(
  pickPrimaryFn(fns, { file: 'aisuite/client.py', function: 'create', line: 364 }).name,
  'Completions.create',
  'line-containment resolves the bare name to the enclosing qualified method',
)

// A failure deeper than create's body but before the next def still lands on create.
assert.equal(
  pickPrimaryFn(fns, { file: 'aisuite/client.py', function: 'create', line: 400 }).name,
  'Completions.create',
)

// Inside Transcriptions.create (line 470) resolves to it, not Completions.create.
assert.equal(
  pickPrimaryFn(fns, { file: 'aisuite/client.py', function: 'create', line: 470 }).name,
  'Transcriptions.create',
)

// No line → name match still works.
assert.equal(
  pickPrimaryFn(fns, { file: 'aisuite/client.py', function: 'Completions.create' }).name,
  'Completions.create',
)

// Unknown file → null (caller falls back to a chip).
assert.equal(pickPrimaryFn(fns, { file: 'nope.py', function: 'x', line: 1 }), null)

// Generic (non-aisuite) shape: two classes in one file, each with a `run`
// method. Proves the resolver is line-driven, not hardcoded to aisuite names.
const generic = [
  { name: 'helper', path: 'pkg/service.py', line: 4 },
  { name: 'Service', path: 'pkg/service.py', line: 20 },
  { name: 'Service.__init__', path: 'pkg/service.py', line: 23 },
  { name: 'Service.run', path: 'pkg/service.py', line: 30 },
  { name: 'Other', path: 'pkg/service.py', line: 80 },
  { name: 'Other.run', path: 'pkg/service.py', line: 92 },
]

assert.equal(
  pickPrimaryFn(generic, { file: 'pkg/service.py', function: 'run', line: 45 }).name,
  'Service.run',
  'a bare run() inside Service.run resolves to Service.run',
)
assert.equal(
  pickPrimaryFn(generic, { file: 'pkg/service.py', function: 'run', line: 100 }).name,
  'Other.run',
  'a bare run() inside Other.run resolves to Other.run',
)
assert.equal(
  pickPrimaryFn(generic, { file: 'pkg/service.py', function: 'Other.run' }).name,
  'Other.run',
  'qualified name with no line still resolves',
)

console.log('flowResolve: all assertions passed')
