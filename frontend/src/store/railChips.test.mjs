// Standalone assertions for the prompt-rail chip classifier + Program grouping.
// Run with: node src/store/railChips.test.mjs
import assert from 'node:assert'
import { isOperationalChip, groupEpisodes } from './railChips.js'

// 1. A non-Program episode that is genuinely operational stays operational (both signals).
assert.equal(isOperationalChip({ signal: 'operational' }), true)
assert.equal(isOperationalChip({ storyFacts: { operational: true } }), true)
assert.equal(isOperationalChip({ signal: 'product' }), false)

// 2. THE BUG: a Program slice with a STALE storyFacts.operational must NOT render operational —
//    the docs slice "One tiny docs note" had signal=product but storyFacts.operational=true.
const docsSlice = { programId: 'program_8ac0befc7d14', signal: 'product', storyFacts: { operational: true } }
assert.equal(isOperationalChip(docsSlice), false)

// 3. A Program slice is never operational even if its own signal somehow says so (programId wins).
assert.equal(isOperationalChip({ programId: 'p1', signal: 'operational' }), false)
assert.equal(isOperationalChip(null), false)

// 4. Grouping: consecutive same-program episodes collapse into one parent group carrying the title;
//    non-Program episodes pass through as singletons; the order is preserved.
const groups = groupEpisodes([
  { episodeId: 'a' },
  { episodeId: 'b', programId: 'P', programTitle: 'Verification Program Smoke' },
  { episodeId: 'c', programId: 'P' },
  { episodeId: 'd' },
])
assert.equal(groups.length, 3)
assert.equal(groups[0].programId, null)
assert.equal(groups[1].programId, 'P')
assert.equal(groups[1].programTitle, 'Verification Program Smoke')
assert.equal(groups[1].episodes.length, 2)              // b + c grouped
assert.equal(groups[2].programId, null)

console.log('railChips.test.mjs: all assertions passed')
