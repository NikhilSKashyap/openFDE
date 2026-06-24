import { useEffect, useRef, useState, useCallback } from 'react'
import { postCouncilAsk, getCouncilContext, getCouncilHistory, postCouncilImplementation, postAutonomousCouncilRun, postProgramRun, cancelAutonomousCouncilRun, cancelProgram } from '../../api/backend'
import { runIsLive, runDisplayPhase, runBannerClass } from '../../store/councilRun'

// Turn a machine block/cancel reason into one plain-language line for the cockpit. Generic — works
// for any provider; specific provider names only ever come from state, never hardcoded here.
const REASON_PLAIN = {
  BLOCKED_PROVIDER_TIMEOUT: 'A provider timed out and was stopped',
  BLOCKED_ADAPTER_UNAVAILABLE: 'A provider was unavailable',
  BLOCKED_NO_PROVIDER_FOR_ROLE: 'A role has no provider selected',
  BLOCKED_MAX_RETRIES: 'Stopped after too many verification retries',
  BLOCKED_NEEDS_PRODUCT_CLARITY: 'Needs a clearer product direction',
  BLOCKED_BLAST_RADIUS: 'The direction was too broad to run safely',
  'cancelled by user': 'Cancelled by you',
}
function humanizeReason(reason) {
  if (!reason) return ''
  return REASON_PLAIN[reason] || (typeof reason === 'string' && reason.startsWith('BLOCKED_')
    ? reason.slice(8).replace(/_/g, ' ').toLowerCase().replace(/^./, c => c.toUpperCase())
    : reason)
}

/**
 * CouncilChat — the read-only brain of Orient, as a real chat thread (not a single answer card).
 *
 * Feels like Codex/Claude/Cursor: a scrolling conversation with the composer DOCKED AT THE BOTTOM,
 * Enter sends, Shift+Enter inserts a newline, the textbox clears the instant you send, your message
 * appears immediately, and a pending "Thinking…" bubble is replaced when the answer returns. The
 * thread persists (server-side, .openfde/council_chat.json) so a refresh never erases it, and a
 * stalled model call never leaves you staring at an infinite spinner — after a soft timeout it
 * offers Cancel, and any failure offers Retry. This NEVER edits files or dispatches a coding run.
 */
const TARGETS = [
  { id: 'auto', label: 'Auto' },
  { id: 'architect', label: 'Architect' },
  { id: 'senior_dev', label: 'Senior Dev' },
  { id: 'verifier', label: 'Verifier' },
  { id: 'discuss', label: 'Council' },        // UI "Council" → backend target "discuss"
]
const ROLE_HUMAN = { architect: 'Architect', senior_dev: 'Senior Dev', verifier: 'Verifier', sr_dev: 'Senior Dev' }
// Role-led council ritual: one lead role's brief (never "Council says…").
const LEAD_BRIEF_LABEL = { architect: 'Architect-led brief', sr_dev: 'Senior Dev-led brief',
                           verifier: 'Verifier-led brief' }
const BRIEF_SECTIONS = [['productDirection', 'Product Direction'],
                        ['implementationPlan', 'Implementation Plan'],
                        ['risksVerification', 'Risks / Verification']]
const SUGGESTIONS = [
  'What should we do next?',
  'Is this ready to ship?',
  'Architect and Senior Dev, discuss whether this should ship.',
]
const SOFT_TIMEOUT_MS = 30_000              // after this, the pending bubble offers Cancel

const busyText = (roles) => {
  const names = (roles || []).map(r => ROLE_HUMAN[r] || r)
  if (!names.length) return ''
  return `${names.join(', ')} ${names.length === 1 ? 'is' : 'are'} mid-work — read-only chat stays available.`
}

export default function CouncilChat({ onOpenAgentSettings = null,
                                      transcript = null, onRefreshTranscript = null }) {
  const [target, setTarget]       = useState('auto')
  const [question, setQuestion]   = useState('')
  const [messages, setMessages]   = useState([])   // {id, role, text, label?, contributorsLabel?, provider?, state?, retry?}
  const [asking, setAsking]       = useState(false)
  const [busyRoles, setBusyRoles] = useState([])
  const [launching, setLaunching] = useState(false)    // autonomous relay being kicked off
  const [cancelling, setCancelling] = useState(false)  // a cancel request is in flight
  const [allowEdits, setAllowEdits] = useState(false)  // let the senior dev write files (real commit)

  // The council transcript is OWNED by App — hydrated at boot, refreshed on every council websocket
  // event — and passed in, so the Orient inbox is already populated when this panel opens (never
  // waiting on a hover or a late websocket). Actions just ask App to refresh.
  const refreshTranscript = useCallback(() => { onRefreshTranscript?.() }, [onRefreshTranscript])

  // Launch the autonomous relay for the typed prompt — OpenFDE runs the full
  // architect→consult→decide→implement→verify loop; turns stream into the transcript live.
  const runAutonomous = useCallback(async (q) => {
    const prompt = (q ?? question).trim()
    if (!prompt || launching) return
    setLaunching(true)
    // Real adapters by default (Codex architect/verifier, Claude Code senior dev). allowEdits lets
    // the senior dev write files so the run produces an actual commit; default off (plan-only).
    const res = await postAutonomousCouncilRun(prompt, { allowEdits })
    setLaunching(false)
    if (res?.ok) { setQuestion(''); refreshTranscript() }
  }, [question, launching, allowEdits, refreshTranscript])

  // Run a PROGRAM — OpenFDE decomposes the product direction into ≤3 scoped slices and runs each
  // through the council loop, auto-advancing on verify. One active program at a time.
  const runProgram = useCallback(async (q) => {
    const prompt = (q ?? question).trim()
    if (!prompt || launching) return
    setLaunching(true)
    const res = await postProgramRun(prompt, { allowEdits })
    setLaunching(false)
    if (res?.ok) { setQuestion(''); refreshTranscript() }
  }, [question, launching, allowEdits, refreshTranscript])

  // Cancel the live Program / council run — hits the REAL backend cancel endpoint (kills the managed
  // subprocess + marks run/slice/program cancelled), then refreshes so the UI shows the true state.
  const cancelActiveProgram = useCallback(async (programId) => {
    if (!programId || cancelling) return
    setCancelling(true)
    await cancelProgram(programId)
    setCancelling(false)
    refreshTranscript()
    setTimeout(refreshTranscript, 900)        // catch the worker settling the run to terminal
  }, [cancelling, refreshTranscript])
  const cancelActiveRun = useCallback(async (runId) => {
    if (!runId || cancelling) return
    setCancelling(true)
    await cancelAutonomousCouncilRun(runId)
    setCancelling(false)
    refreshTranscript()
    setTimeout(refreshTranscript, 900)
  }, [cancelling, refreshTranscript])

  const idRef    = useRef(0)
  const abortRef = useRef(null)
  const softRef  = useRef(null)
  const threadRef = useRef(null)
  const taRef    = useRef(null)
  const nextId = () => (idRef.current += 1)

  // Restore the saved thread + surface any mid-work role, before the first question.
  useEffect(() => {
    let alive = true
    ;(async () => {
      const hist = await getCouncilHistory()
      if (alive && hist?.ok && Array.isArray(hist.turns) && hist.turns.length) {
        setMessages(hist.turns.map(t => ({
          id: nextId(), role: t.role, text: t.text, label: t.label,
          contributorsLabel: t.contributorsLabel, provider: t.provider,
          // Restore the structured role-led brief so the lead-role card survives a refresh.
          // Older turns saved without it have t.brief === undefined → render as plain text.
          brief: t.brief,
        })))
      }
      const ctx = await getCouncilContext()
      if (alive && ctx?.ok) {
        const agents = ctx.context?.agents || {}
        setBusyRoles(['architect', 'senior_dev', 'verifier'].filter(r => agents[r]?.workBusy))
      }
    })()
    return () => { alive = false }
  }, [])

  // Auto-scroll to the newest message.
  useEffect(() => {
    const el = threadRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  // Auto-grow the composer up to a few lines.
  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'
  }, [question])

  const patch = useCallback((id, p) =>
    setMessages(ms => ms.map(m => (m.id === id ? { ...m, ...p } : m))), [])

  const ask = useCallback(async (q) => {
    const query = (q ?? question).trim()
    if (!query || asking) return
    const pendingId = nextId()
    // Append the user message + a pending bubble immediately; clear the box on the same tick.
    setMessages(ms => [...ms,
      { id: nextId(), role: 'user', text: query },
      { id: pendingId, role: 'assistant', text: 'Thinking…', state: 'pending' },
    ])
    setQuestion('')
    setAsking(true)

    const controller = new AbortController()
    abortRef.current = controller
    if (softRef.current) clearTimeout(softRef.current)
    softRef.current = setTimeout(() => patch(pendingId, { state: 'slow' }), SOFT_TIMEOUT_MS)

    const res = await postCouncilAsk(query, target, { signal: controller.signal })

    if (softRef.current) { clearTimeout(softRef.current); softRef.current = null }
    abortRef.current = null
    setAsking(false)
    if (controller.signal.aborted) {
      patch(pendingId, { state: 'cancelled', text: 'Cancelled.', retry: query })
    } else if (res?.ok) {
      patch(pendingId, {
        state: null, text: res.answer || '(no answer)', label: res.label,
        contributorsLabel: res.contributorsLabel, provider: res.provider, brief: res.brief,
      })
      setBusyRoles(res.workBusyRoles || [])
      refreshTranscript()
    } else {
      patch(pendingId, { state: 'error', text: 'Could not reach the council — is the backend running?', retry: query })
    }
  }, [question, asking, target, patch, refreshTranscript])

  const cancel = () => abortRef.current?.abort()

  // Start implementation: hand the structured brief to the server, which re-validates the gate and
  // CREATES a scoped handoff record from the brief — it never edits files or dispatches a run from
  // here (no /api/council/run has started). On success we append a compact confirmation (also persisted
  // server-side, so it survives a refresh); on failure we show an inline error and leave the brief intact.
  const startImplementation = useCallback(async (m) => {
    if (!m?.brief || m.startState === 'creating' || m.startState === 'created') return
    const q = m.brief.question || ''
    if (!q) { patch(m.id, { startError: 'Missing the original question for this brief.' }); return }
    patch(m.id, { startState: 'creating', startError: null })
    const res = await postCouncilImplementation(q, m.brief)
    if (res?.ok) {
      patch(m.id, { startState: 'created', startError: null, startHandoffId: res.handoff?.id })
      setMessages(ms => [...ms, { id: nextId(), role: 'assistant',
        text: res.message || 'Implementation handoff created.' }])
      refreshTranscript()
    } else {
      patch(m.id, { startState: 'error',
        startError: 'Could not create the handoff — the brief is unchanged. Try again.' })
    }
  }, [patch, refreshTranscript])

  // Enter sends; Shift+Enter inserts a newline; Cmd/Ctrl+Enter also sends.
  const onKeyDown = (e) => {
    if (e.key !== 'Enter') return
    if (e.shiftKey) return                         // newline
    e.preventDefault()
    ask()
  }

  const busy = busyText(busyRoles)
  const empty = messages.length === 0

  return (
    <div className="council council-chat">
      <div className="agentset-roles council-targets">
        {TARGETS.map(t => (
          <button key={t.id} className={`agentset-role${target === t.id ? ' active' : ''}`}
            onClick={() => setTarget(t.id)}>{t.label}</button>
        ))}
      </div>
      {busy && <div className="council-busy subtle">{busy}</div>}

      {/* Persistent council inbox — the durable handoff conversation (orange-box transcript),
          above the read-only Ask thread. Refreshes on council websocket events. */}
      <CouncilTranscript data={transcript} launching={launching} cancelling={cancelling}
        onCancelRun={cancelActiveRun} onCancelProgram={cancelActiveProgram} />

      {/* Scrolling conversation — grows; composer below stays docked. */}
      <div className="council-thread" ref={threadRef}>
        {empty && (
          <div className="council-empty">
            <div className="council-empty-hint">Ask the council — read-only, it never edits files.</div>
            <div className="concept-suggest">
              {SUGGESTIONS.map(s => (
                <button key={s} className="concept-suggest-chip" onClick={() => ask(s)}>{s}</button>
              ))}
            </div>
          </div>
        )}
        {messages.map(m => (
          <div key={m.id} className={`cmsg cmsg-${m.role}${m.state ? ' cmsg-' + m.state : ''}`}>
            <div className="cmsg-body">
              {m.state === 'pending' && <span className="cmsg-thinking">Thinking<span className="cmsg-ell">…</span></span>}
              {m.state === 'slow' && <span className="cmsg-thinking">Still thinking — this one’s taking a while.</span>}
              {(!m.state || m.state === 'error' || m.state === 'cancelled') && !m.brief && m.text}
            </div>
            {/* Role-led brief: one lead role, structured sections (replaces "Answered by Council"). */}
            {m.role === 'assistant' && m.brief && !m.state && (
              <div className="cmsg-brief">
                <div className="cmsg-src">
                  <strong>{LEAD_BRIEF_LABEL[m.brief.leadRole] || 'Brief'}</strong>
                  {m.brief.consultedRoles?.length > 0 && (
                    <span className="council-contrib"> · consulting {
                      m.brief.consultedRoles.map(r => ROLE_HUMAN[r] || r).join(', ')}</span>
                  )}
                  {m.provider && <span className="council-prov"> · {m.provider}</span>}
                </div>
                {BRIEF_SECTIONS.map(([k, label]) => (
                  m.brief.sections?.[k] ? (
                    <div key={k} className="cmsg-brief-section">
                      <div className="cmsg-brief-h">{label}</div>
                      <div className="cmsg-brief-c">{m.brief.sections[k]}</div>
                    </div>
                  ) : null
                ))}
                {m.brief.humanEscalation?.needed && (
                  <div className="cmsg-brief-escalate">Needs your call — {m.brief.humanEscalation.reason}.</div>
                )}
                {/* Start implementation: enabled only for non-escalated Architect/Senior Dev briefs;
                    otherwise disabled with the reason. Creates a scoped handoff record from the brief —
                    no automatic edit or run (no /api/council/run is dispatched from here). */}
                <div className="cmsg-brief-start">
                  <button className="cmsg-action"
                    disabled={!m.brief.canStartImplementation || m.startState === 'creating' || m.startState === 'created'}
                    title={m.brief.canStartImplementation
                      ? 'Create a scoped implementation handoff from this brief — no automatic edit or run'
                      : (m.brief.humanEscalation?.needed
                          ? `Needs your decision — ${m.brief.humanEscalation.reason}`
                          : 'Readiness brief — ask a product or implementation question to plan a change')}
                    onClick={() => startImplementation(m)}>
                    {m.startState === 'creating' ? 'Creating…'
                      : m.startState === 'created' ? 'Handoff created ✓'
                      : (m.brief.startImplementationLabel || 'Start implementation')}
                  </button>
                  {m.brief.canStartImplementation && m.startState !== 'created' && (
                    <div className="cmsg-brief-hint">Creates a scoped handoff from this brief — not an automatic edit or run.</div>
                  )}
                  {m.startError && <div className="cmsg-brief-error">{m.startError}</div>}
                </div>
              </div>
            )}
            {m.role === 'assistant' && m.label && !m.brief && !m.state && (
              <div className="cmsg-src">
                Answered by <strong>{m.label}</strong>
                {m.contributorsLabel && m.contributorsLabel !== m.label && (
                  <span className="council-contrib"> · {m.contributorsLabel}</span>
                )}
                {m.provider && <span className="council-prov"> · {m.provider}</span>}
              </div>
            )}
            {m.state === 'slow' && <button className="cmsg-action" onClick={cancel}>Cancel</button>}
            {(m.state === 'error' || m.state === 'cancelled') && m.retry && (
              <button className="cmsg-action" onClick={() => ask(m.retry)}>Retry</button>
            )}
          </div>
        ))}
      </div>

      {/* Bottom-docked composer */}
      <div className="council-composer">
        <textarea
          ref={taRef} className="council-input" rows={1}
          placeholder="Ask what to do next…  (Enter to send · Shift+Enter for newline)"
          value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={onKeyDown}
        />
        <button className="council-send" disabled={!question.trim() || asking} onClick={() => ask()}>
          {asking ? '…' : 'Send'}
        </button>
      </div>
      {/* Hand the whole task to the autonomous relay — no human copy-paste between Codex and CC. */}
      <div className="council-run-auto-row">
        <button className="council-run-auto" disabled={!question.trim() || launching}
          onClick={() => runAutonomous()}
          title="One scoped task → the council loop (architect → consult → decide → implement → verify), streamed here">
          {launching ? 'Starting…' : '▶ Run council'}
        </button>
        <button className="council-run-auto council-run-program" disabled={!question.trim() || launching}
          onClick={() => runProgram()}
          title="A product direction → a managed Program of up to 3 scoped slices, each run through the council loop, auto-advancing on verify">
          {launching ? '…' : '▶▶ Run Program'}
        </button>
        <label className="council-run-edits" title="Let the senior dev write files and produce a real commit (off = plan only, no repo changes)">
          <input type="checkbox" checked={allowEdits} onChange={e => setAllowEdits(e.target.checked)} />
          allow file edits
        </label>
      </div>
      <div className="council-run-hint">
        {allowEdits
          ? 'Real run: the senior dev will write files and the relay will commit them.'
          : 'Plan-only: drives the real Codex + Claude Code loop and verifies, but writes nothing. Turn on “allow file edits” for a real implementation + commit.'}
      </div>

      {onOpenAgentSettings && (
        <button className="council-settings-link" onClick={onOpenAgentSettings}>Agent Settings →</button>
      )}
    </div>
  )
}

// ── Council inbox transcript ─────────────────────────────────────────────────
// The durable council conversation (architect → sr dev → verifier …), normalized server-side.
// Compact role rows; summary first; chips for ids/commit/checks; expand for the body. Honest about
// pending wakeups — never claims the agent was woken.
const ROLE_ACCENT = {
  user: 'var(--text-muted)', architect: 'var(--accent)',
  sr_dev: 'var(--accent-orange)', system: 'var(--accent-orange)',
}
function rowAccent(it) {
  if (it.role === 'verifier') return it.kind === 'verified' ? 'var(--solid)' : 'var(--violation)'
  return ROLE_ACCENT[it.role] || 'var(--text-muted)'
}

// Live autonomous-relay banner — who has the baton, what happened last, whether it is stuck/done.
// The baton label is DYNAMIC — derived from the role's assigned provider, never hardcoded.
const _PROVIDER_DISP = { codex: 'Codex', 'claude-code': 'Claude Code', echo: 'echo' }
const _ROLE_DISP = { architect: 'architect', sr_dev: 'sr dev', verifier: 'verifier' }
const _ROLE_KEY = { architect: 'architect', sr_dev: 'srDev', verifier: 'verifier' }
function batonLabel(run) {
  const role = run.activeRole
  const provider = (run.providers || {})[_ROLE_KEY[role]]
  const rd = _ROLE_DISP[role] || role
  const pd = _PROVIDER_DISP[provider] || provider
  return pd ? `${pd} (${rd})` : rd
}
function providersLabel(p) {
  const vals = Object.values(p || {})
  if (!vals.length) return ''
  if (vals.every(v => v === 'echo')) return 'echo (demo)'
  return 'Codex + Claude Code'
}
function RunBanner({ run, programTerminal = false, onCancel, cancelling }) {
  if (!run) return null
  // The banner reflects the run's TRUE state. A council run BELONGS to its program: once the program
  // is terminal (cancelled/blocked/complete) the run is no longer live — so we never show a stale
  // baton or Cancel even if a racing run snapshot still says "running". Standalone runs (no program)
  // keep the pure run-state behaviour.
  const live = runIsLive(run) && !programTerminal
  const cls = (programTerminal && run.running) ? 'blocked' : runBannerClass(run)
  const phase = (programTerminal && run.running) ? 'cancelled' : runDisplayPhase(run)
  const provs = providersLabel(run.providers)
  return (
    <div className={`acr-banner acr-${cls}`}>
      <div className="acr-line1">
        <span className="acr-dot" />
        <span className="acr-phase">Autonomous council — {phase}</span>
        {live && run.activeRole && <span className="acr-baton">{batonLabel(run)} has the baton</span>}
        {run.loop > 0 && <span className="acr-loop">loop {run.loop}/{run.maxLoops}</span>}
        {provs && <span className="acr-provs">{provs}</span>}
        {live && onCancel && run.runId && (
          <button className="acr-cancel" disabled={cancelling} onClick={() => onCancel(run.runId)}
            title="Stop this run and its managed provider">{cancelling ? 'Cancelling…' : '■ Cancel'}</button>
        )}
      </div>
      {run.latestTurn?.summary && <div className="acr-last">{run.latestTurn.summary}</div>}
      {run.blockedReason && <div className="acr-blockedreason">{humanizeReason(run.blockedReason)}</div>}
    </div>
  )
}

// In-flight phase → "<role> is <verb>…". Fills the gap between a phase starting and its turn landing
// so the user always sees who has the baton, never just "running".
const PHASE_VERB = {
  ARCHITECT_PLANNING: ['architect', 'is planning'], SR_DEV_CONSULTING: ['sr_dev', 'is reviewing'],
  ARCHITECT_DECIDING: ['architect', 'is deciding'], SR_DEV_IMPLEMENTING: ['sr_dev', 'is implementing'],
  CODEX_VERIFYING: ['verifier', 'is verifying'], CHANGES_REQUESTED: ['sr_dev', 'is fixing'],
}
function LivePhaseRow({ run }) {
  const pv = PHASE_VERB[run.phase]
  if (!pv) return null
  const [role, verb] = pv
  const accent = rowAccent({ role })
  // Label derives from the role's ASSIGNED provider (from state) — never a hardcoded Codex/Claude.
  const provider = (run.providers || {})[_ROLE_KEY[role]]
  const pd = _PROVIDER_DISP[provider] || provider
  const label = pd ? `${_ROLE_DISP[role] || role} (${pd})` : (_ROLE_DISP[role] || role)
  return (
    <div className="ctx-row ctx-live" style={{ borderLeftColor: accent }}>
      <div className="ctx-row-head">
        <span className="ctx-role" style={{ color: accent }}>{label}</span>
        <span className="ctx-summary">{verb}<span className="ctx-live-dots">…</span></span>
      </div>
    </div>
  )
}

// Program banner — the parent arc above the council run: title, slice N/M, per-slice status dots.
const SLICE_DOT = {
  verified: 'var(--solid)', running: 'var(--accent-orange)', blocked: 'var(--violation)',
  failed: 'var(--violation)', cancelled: 'var(--text-muted)', queued: 'var(--text-muted)',
}
function ProgramBanner({ program, onCancel, cancelling }) {
  if (!program) return null
  const terminal = ['complete', 'blocked', 'cancelled'].includes(program.status)
  const reason = humanizeReason(program.blockerReason)
  return (
    <div className={`pgm-banner pgm-${program.status}`}>
      <div className="pgm-line1">
        <span className="pgm-title">Program — {program.title}</span>
        <span className="pgm-slice">slice {program.sliceIndex}/{program.sliceCount}</span>
        <span className="pgm-status">{program.status}{reason ? ` · ${reason}` : ''}</span>
        {!terminal && onCancel && program.programId && (
          <button className="acr-cancel" disabled={cancelling} onClick={() => onCancel(program.programId)}
            title="Stop the Program and its active slice">{cancelling ? 'Cancelling…' : '■ Cancel Program'}</button>
        )}
      </div>
      {program.currentSliceTitle && !terminal && <div className="pgm-cur">▸ {program.currentSliceTitle}</div>}
      {(program.slices || []).length > 0 && (
        <div className="pgm-slices">
          {program.slices.map((s, i) => (
            <span key={s.sliceId} className="pgm-sl" title={`${s.title} — ${s.status}`}>
              <span className="pgm-sl-dot" style={{ background: SLICE_DOT[s.status] || 'var(--text-muted)' }} />{i + 1}
            </span>
          ))}
        </div>
      )}
      {program.finalReport && terminal && <div className="pgm-final">{program.finalReport}</div>}
    </div>
  )
}

function CouncilTranscript({ data, launching = false, cancelling = false, onCancelRun, onCancelProgram }) {
  const [open, setOpen] = useState({})
  const [showPrev, setShowPrev] = useState(false)
  // Boot/loading: the transcript hasn't hydrated yet → a clear skeleton, never a blank panel. Once the
  // first fetch resolves, App always sets a (possibly empty) object, so this never hangs on a blocked/
  // cancelled run — it resolves to the terminal banner + turns below.
  if (!data) {
    return (
      <div className="ctx">
        <div className="ctx-head"><span className="ctx-title">Council inbox</span></div>
        <div className="ctx-skeleton">{launching ? 'Starting autonomous council…' : 'Restoring latest council run…'}</div>
      </div>
    )
  }
  const items = data.items || []
  const run = data.run
  const program = data.program
  const programTerminal = !!program && ['cancelled', 'blocked', 'complete'].includes(program.status)
  const prev = data.previousItems || []
  const noRuns = items.length === 0 && !run && !launching
  return (
    <div className="ctx">
      <ProgramBanner program={program} onCancel={onCancelProgram} cancelling={cancelling} />
      <RunBanner run={run} programTerminal={programTerminal} onCancel={onCancelRun} cancelling={cancelling} />
      <div className="ctx-head">
        <span className="ctx-title">Council inbox</span>
        {data.activeStatus && <span className="ctx-status">{data.activeStatus}</span>}
        {!data.active && !run?.running && <span className="ctx-idle">idle</span>}
      </div>
      {launching && !run && <div className="ctx-skeleton">Starting autonomous council…</div>}
      {noRuns ? (
        <div className="ctx-empty">No council runs yet — type a task and Run autonomous council, or ask below.</div>
      ) : items.map(it => {
        const accent = rowAccent(it)
        const expandable = !!(it.body || (it.findings || []).length)
        const sha = (it.latestCommit && it.latestCommit !== '(none)') ? String(it.latestCommit).slice(0, 7) : ''
        return (
          <div key={it.id} className={'ctx-row' + (it.kind === 'pending' ? ' ctx-pending' : '')}
               style={{ borderLeftColor: accent }}>
            <div className="ctx-row-head"
                 onClick={expandable ? () => setOpen(o => ({ ...o, [it.id]: !o[it.id] })) : undefined}
                 style={{ cursor: expandable ? 'pointer' : 'default' }}>
              <span className="ctx-role" style={{ color: accent }}>{it.label}</span>
              <span className="ctx-summary">{it.summary}</span>
              {expandable && <span className="ctx-caret">{open[it.id] ? '▾' : '▸'}</span>}
            </div>
            <div className="ctx-chips">
              {sha && <span className="ctx-chip mono" title={it.latestCommit}>⎇ {sha}</span>}
              {it.checks && <span className="ctx-chip">✓ {it.checks}</span>}
              {it.episodeId && <span className="ctx-chip mono" title={it.episodeId}>{String(it.episodeId).slice(0, 16)}</span>}
              {(it.taskIds || []).length > 0 &&
                <span className="ctx-chip mono">{it.taskIds.length} task{it.taskIds.length === 1 ? '' : 's'}</span>}
              {it.kind === 'pending' && it.nativeWakeup &&
                <span className="ctx-chip">native {it.nativeWakeup === 'native_unavailable' ? 'unavailable' : it.nativeWakeup}</span>}
            </div>
            {open[it.id] && expandable && (
              <div className="ctx-body">
                {(it.findings || []).length > 0 && (
                  <ul className="ctx-findings">{it.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>
                )}
                {it.body && <div className="ctx-pre">{it.body}</div>}
              </div>
            )}
          </div>
        )
      })}
      {runIsLive(run) && !programTerminal && <LivePhaseRow run={run} />}
      {prev.length > 0 && (
        <div className="ctx-prev">
          <button className="ctx-prev-toggle" onClick={() => setShowPrev(s => !s)}>
            {showPrev ? '▾' : '▸'} previous runs ({data.previousRunCount || 1})
          </button>
          {showPrev && prev.map(it => (
            <div key={it.id} className="ctx-row ctx-prev-row" style={{ borderLeftColor: rowAccent(it) }}>
              <div className="ctx-row-head">
                <span className="ctx-role" style={{ color: rowAccent(it) }}>{it.label}</span>
                <span className="ctx-summary">{it.summary}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
