// Authoritative live-vs-terminal classification for an autonomous-council run snapshot. The Orient
// banner and the live phase row must reflect the run's TRUE state — so once a run is finished
// (verified / ready-to-push / blocked / failed / cancelled), they can never keep showing an older
// in-flight phase like "sr dev consulting…". A run is "live" only while it is genuinely running on a
// non-terminal phase; the terminal display label comes from STATUS (authoritative), never a phase
// field that may lag.

export const TERMINAL_PHASES = new Set(['VERIFIED', 'READY_TO_PUSH', 'BLOCKED', 'FAILED'])
export const TERMINAL_STATUSES = new Set([
  'verified', 'ready_to_push', 'landed', 'blocked', 'blocked_needs_human',
  'blocked_adapter_unavailable', 'blocked_provider_timeout', 'blocked_provider_error',
  'failed', 'cancelled',
])

export function runIsTerminal(run) {
  if (!run) return false
  if (run.running === false) return true
  return TERMINAL_PHASES.has(run.phase) || TERMINAL_STATUSES.has(run.status)
}

// True ONLY while the run is actually in flight — drives the banner "running" state and whether the
// live "<role> is …" row renders. Requires running === true AND a non-terminal phase/status.
export function runIsLive(run) {
  return !!run && run.running === true
    && !TERMINAL_PHASES.has(run.phase) && !TERMINAL_STATUSES.has(run.status)
}

const STATUS_LABEL = {
  verified: 'verified', ready_to_push: 'ready to push', landed: 'landed',
  blocked: 'blocked', blocked_needs_human: 'blocked — needs human',
  blocked_adapter_unavailable: 'blocked — adapter unavailable',
  blocked_provider_timeout: 'blocked — provider timeout', blocked_provider_error: 'blocked — provider error',
  failed: 'failed', cancelled: 'cancelled',
}
const PHASE_LABEL = {
  USER_PROMPT: 'queued', ARCHITECT_PLANNING: 'architect planning', SR_DEV_CONSULTING: 'sr dev consulting',
  ARCHITECT_DECIDING: 'architect deciding', SR_DEV_IMPLEMENTING: 'sr dev implementing',
  CODEX_VERIFYING: 'verifier verifying', CHANGES_REQUESTED: 'changes requested',
  VERIFIED: 'verified', READY_TO_PUSH: 'ready to push', BLOCKED: 'blocked',
}

// The banner's phase label. When terminal, it comes from STATUS first so a stale phase field can
// never render "sr dev consulting" on a finished run; while live, it follows the phase.
export function runDisplayPhase(run) {
  if (!run) return ''
  if (runIsTerminal(run)) return STATUS_LABEL[run.status] || PHASE_LABEL[run.phase] || run.phase || ''
  return PHASE_LABEL[run.phase] || run.phase || ''
}

// Banner severity class for styling: running (orange) | blocked (red) | done (green).
export function runBannerClass(run) {
  if (runIsLive(run)) return 'running'
  return String(run?.status || '').startsWith('blocked') || run?.phase === 'BLOCKED' ? 'blocked' : 'done'
}
