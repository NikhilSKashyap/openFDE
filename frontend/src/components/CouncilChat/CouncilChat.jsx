import { useEffect, useRef, useState, useCallback } from 'react'
import { postCouncilAsk, getCouncilContext, getCouncilHistory } from '../../api/backend'

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

export default function CouncilChat({ onOpenAgentSettings = null }) {
  const [target, setTarget]       = useState('auto')
  const [question, setQuestion]   = useState('')
  const [messages, setMessages]   = useState([])   // {id, role, text, label?, contributorsLabel?, provider?, state?, retry?}
  const [asking, setAsking]       = useState(false)
  const [busyRoles, setBusyRoles] = useState([])

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
    } else {
      patch(pendingId, { state: 'error', text: 'Could not reach the council — is the backend running?', retry: query })
    }
  }, [question, asking, target, patch])

  const cancel = () => abortRef.current?.abort()

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
                {m.brief.canStartImplementation && (
                  <button className="cmsg-action" disabled title="Implementation handoff coming next">
                    {m.brief.startImplementationLabel || 'Start implementation'} — coming next
                  </button>
                )}
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

      {onOpenAgentSettings && (
        <button className="council-settings-link" onClick={onOpenAgentSettings}>Agent Settings →</button>
      )}
    </div>
  )
}
