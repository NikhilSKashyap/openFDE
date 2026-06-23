import { useEffect, useRef, useState, useCallback } from 'react'
import { postCouncilAsk, getCouncilContext, getCouncilHistory, postCouncilImplementation, getCouncilTranscript, postAutonomousCouncilRun } from '../../api/backend'

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

export default function CouncilChat({ onOpenAgentSettings = null, councilNonce = 0 }) {
  const [target, setTarget]       = useState('auto')
  const [question, setQuestion]   = useState('')
  const [messages, setMessages]   = useState([])   // {id, role, text, label?, contributorsLabel?, provider?, state?, retry?}
  const [asking, setAsking]       = useState(false)
  const [busyRoles, setBusyRoles] = useState([])
  const [transcript, setTranscript] = useState(null)   // durable council conversation (Orient inbox)
  const [launching, setLaunching] = useState(false)    // autonomous relay being kicked off
  const txSeqRef = useRef(0)                            // latest-wins guard for overlapping refetches

  // The persistent council transcript — refreshed on load, on each council websocket event
  // (councilNonce bumps in App), and after an Ask. An autonomous relay fires many events in a burst,
  // so responses can resolve out of order — the seq guard keeps the LAST request the one that wins.
  const refreshTranscript = useCallback(async () => {
    const seq = ++txSeqRef.current
    const t = await getCouncilTranscript()
    if (seq === txSeqRef.current && t?.ok) setTranscript(t)
  }, [])

  // Launch the autonomous relay for the typed prompt — OpenFDE runs the full
  // architect→consult→decide→implement→verify loop; turns stream into the transcript live.
  const runAutonomous = useCallback(async (q) => {
    const prompt = (q ?? question).trim()
    if (!prompt || launching) return
    setLaunching(true)
    const res = await postAutonomousCouncilRun(prompt, { providers: { architect: 'echo', srDev: 'echo', verifier: 'echo' } })
    setLaunching(false)
    if (res?.ok) { setQuestion(''); refreshTranscript() }
  }, [question, launching, refreshTranscript])
  useEffect(() => {
    const seq = ++txSeqRef.current
    getCouncilTranscript().then(t => { if (seq === txSeqRef.current && t?.ok) setTranscript(t) })
  }, [councilNonce])

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
      <CouncilTranscript data={transcript} />

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
      <button className="council-run-auto" disabled={!question.trim() || launching}
        onClick={() => runAutonomous()}
        title="OpenFDE runs the full architect → consult → decide → implement → verify loop and streams every turn here">
        {launching ? 'Starting…' : '▶ Run autonomous council'}
      </button>

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
const PHASE_LABEL = {
  USER_PROMPT: 'queued', ARCHITECT_PLANNING: 'architect planning', SR_DEV_CONSULTING: 'sr dev consulting',
  ARCHITECT_DECIDING: 'architect deciding', SR_DEV_IMPLEMENTING: 'sr dev implementing',
  CODEX_VERIFYING: 'verifier verifying', CHANGES_REQUESTED: 'changes requested',
  VERIFIED: 'verified', READY_TO_PUSH: 'ready to push', BLOCKED: 'blocked',
}
const BATON_LABEL = { architect: 'Codex (architect)', sr_dev: 'Claude Code (sr dev)', verifier: 'Codex (verifier)' }
function RunBanner({ run }) {
  if (!run) return null
  const blocked = String(run.status || '').startsWith('blocked')
  const cls = run.running ? 'running' : (blocked ? 'blocked' : 'done')
  return (
    <div className={`acr-banner acr-${cls}`}>
      <div className="acr-line1">
        <span className="acr-dot" />
        <span className="acr-phase">Autonomous council — {PHASE_LABEL[run.phase] || run.phase}</span>
        {run.running && run.activeRole && <span className="acr-baton">{BATON_LABEL[run.activeRole] || run.activeRole} has the baton</span>}
        {run.loop > 0 && <span className="acr-loop">loop {run.loop}/{run.maxLoops}</span>}
      </div>
      {run.latestTurn?.summary && <div className="acr-last">{run.latestTurn.summary}</div>}
      {run.blockedReason && <div className="acr-blockedreason">{run.blockedReason}</div>}
    </div>
  )
}

function CouncilTranscript({ data }) {
  const [open, setOpen] = useState({})
  if (!data) return null
  const items = data.items || []
  return (
    <div className="ctx">
      <RunBanner run={data.run} />
      <div className="ctx-head">
        <span className="ctx-title">Council inbox</span>
        {data.activeStatus && <span className="ctx-status">{data.activeStatus}</span>}
        {!data.active && <span className="ctx-idle">idle</span>}
      </div>
      {items.length === 0 ? (
        <div className="ctx-empty">No active external handoff — start council work, or ask below.</div>
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
    </div>
  )
}
