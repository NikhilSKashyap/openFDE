import { useEffect, useRef, useState } from 'react'
import { postCouncilAsk, getCouncilContext } from '../../api/backend'

/**
 * CouncilChat — the read-only brain of Orient, embedded INLINE (no floating panel).
 *
 * The user types into Orient; OpenFDE routes the question to the right council member
 * (Auto / Architect / Senior Dev / Verifier / Council) and answers read-only, with a
 * receipt ("Answered by Architect" / "Answered by Council"). UI "Council" maps to the
 * backend "discuss" route. This never edits files or dispatches a coding run.
 *
 * Reuses the existing design language (agentset segmented control + work/concept input
 * + answer styles) — one surface, no second design.
 */
const TARGETS = [
  { id: 'auto', label: 'Auto' },
  { id: 'architect', label: 'Architect' },
  { id: 'senior_dev', label: 'Senior Dev' },
  { id: 'verifier', label: 'Verifier' },
  { id: 'discuss', label: 'Council' },        // UI "Council" → backend target "discuss"
]

const ROLE_HUMAN = { architect: 'Architect', senior_dev: 'Senior Dev', verifier: 'Verifier' }
const busyText = (roles) => {
  const names = (roles || []).map(r => ROLE_HUMAN[r] || r)
  if (!names.length) return ''
  return `${names.join(', ')} ${names.length === 1 ? 'is' : 'are'} mid-work — read-only chat stays available.`
}

const SUGGESTIONS = [
  'What should we do next?',
  'Is this ready to ship?',
  'Architect and Senior Dev, discuss whether this should ship.',
]

export default function CouncilChat({ onOpenAgentSettings = null }) {
  const [target, setTarget]     = useState('auto')
  const [question, setQuestion] = useState('')
  const [asking, setAsking]     = useState(false)
  const [answer, setAnswer]     = useState(null)
  const [busyRoles, setBusyRoles] = useState([])
  const inputRef = useRef(null)

  // Light mount read: surface any mid-work role gently, before the first question.
  useEffect(() => {
    let alive = true
    ;(async () => {
      const res = await getCouncilContext()
      if (!alive || !res?.ok) return
      const agents = res.context?.agents || {}
      setBusyRoles(['architect', 'senior_dev', 'verifier'].filter(r => agents[r]?.workBusy))
    })()
    return () => { alive = false }
  }, [])

  async function ask(q) {
    const query = (q ?? question).trim()
    if (!query || asking) return
    setQuestion(query); setAsking(true); setAnswer(null)
    const res = await postCouncilAsk(query, target)
    setAsking(false)
    if (res?.ok) {
      setAnswer(res)
      setBusyRoles(res.workBusyRoles || [])
    } else {
      setAnswer({ answer: 'Could not reach the council — is the backend running?', label: '', error: true })
    }
  }

  const a = answer
  const busy = busyText(busyRoles)

  return (
    <div className="council">
      {/* Target selector — reuses the Agent Settings segmented control */}
      <div className="agentset-roles council-targets">
        {TARGETS.map(t => (
          <button
            key={t.id}
            className={`agentset-role${target === t.id ? ' active' : ''}`}
            onClick={() => setTarget(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {busy && !a && <div className="council-busy">{busy}</div>}

      <div className="work-promptbox">
        <textarea
          ref={inputRef} className="work-textarea" placeholder="Ask what to do next…"
          value={question} onChange={e => setQuestion(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ask() } }}
        />
        <button className="work-primary" disabled={asking || !question.trim()} onClick={() => ask()}>
          {asking ? '…' : 'Ask'}
        </button>
      </div>

      {!a && !asking && (
        <div className="concept-suggest">
          {SUGGESTIONS.map(s => (
            <button key={s} className="concept-suggest-chip" onClick={() => ask(s)}>{s}</button>
          ))}
        </div>
      )}

      {asking && <div className="concept-answer"><div className="concept-answer-text">Thinking…</div></div>}

      {a && (
        <div className="concept-answer">
          <div className="concept-answer-text">{a.answer}</div>
          {a.label && (
            <div className="concept-answer-src">
              Answered by <strong>{a.label}</strong>
              {a.contributorsLabel && a.contributorsLabel !== a.label && (
                <span className="council-contrib"> · {a.contributorsLabel}</span>
              )}
              {a.provider && <span className="council-prov"> · {a.provider}</span>}
            </div>
          )}
          {(a.routedReason || Number.isFinite(a.confidence)) && !a.error && (
            <div className="council-meta">
              {a.routedReason}
              {Number.isFinite(a.confidence) && <span> · confidence {Math.round(a.confidence * 100)}%</span>}
            </div>
          )}
          {busy && !a.error && <div className="council-busy subtle">{busy}</div>}
        </div>
      )}

      {onOpenAgentSettings && (
        <button className="council-settings-link" onClick={onOpenAgentSettings}>Agent Settings →</button>
      )}
    </div>
  )
}
