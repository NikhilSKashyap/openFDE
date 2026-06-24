// Standalone assertions for the council run live-vs-terminal classifier.
// Run with: node src/store/councilRun.test.mjs
import assert from 'node:assert'
import { runIsLive, runIsTerminal, runDisplayPhase, runBannerClass } from './councilRun.js'

// 1. A genuinely in-flight run is live and shows its phase.
const consulting = { running: true, phase: 'SR_DEV_CONSULTING', status: 'running' }
assert.equal(runIsLive(consulting), true)
assert.equal(runIsTerminal(consulting), false)
assert.equal(runDisplayPhase(consulting), 'sr dev consulting')
assert.equal(runBannerClass(consulting), 'running')

// 2. A FINISHED run is never live, even with a non-terminal phase still in the snapshot (the bug):
//    running:false must win → terminal, no "sr dev consulting".
const finishedStalePhase = { running: false, phase: 'SR_DEV_CONSULTING', status: 'ready_to_push' }
assert.equal(runIsLive(finishedStalePhase), false)
assert.equal(runIsTerminal(finishedStalePhase), true)
assert.equal(runDisplayPhase(finishedStalePhase), 'ready to push')   // from STATUS, not the stale phase
assert.equal(runBannerClass(finishedStalePhase), 'done')

// 3. Terminal status alone (even if running flag is stale-true) is terminal — belt-and-suspenders.
const staleRunningTrue = { running: true, phase: 'SR_DEV_CONSULTING', status: 'ready_to_push' }
assert.equal(runIsLive(staleRunningTrue), false)
assert.equal(runDisplayPhase(staleRunningTrue), 'ready to push')

// 4. Terminal phase classifies as terminal.
for (const phase of ['VERIFIED', 'READY_TO_PUSH', 'BLOCKED', 'FAILED']) {
  assert.equal(runIsLive({ running: true, phase, status: 'running' }), false, phase)
  assert.equal(runIsTerminal({ running: true, phase, status: 'running' }), true, phase)
}

// 5. Blocked → blocked class + label.
const blocked = { running: false, phase: 'BLOCKED', status: 'blocked_needs_human' }
assert.equal(runBannerClass(blocked), 'blocked')
assert.equal(runDisplayPhase(blocked), 'blocked — needs human')

// 6. No run → not live, not terminal, empty label.
assert.equal(runIsLive(null), false)
assert.equal(runIsTerminal(null), false)
assert.equal(runDisplayPhase(undefined), '')

console.log('councilRun.test.mjs: all assertions passed')
