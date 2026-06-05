import { useState } from 'react'

/**
 * Concept panel (Step 37a) — the canvas-native card for the active spotlight
 * (a concept/tether or a commit). Canvas-first: it answers "what is this / what
 * changed" without reading code, with Ask Concept and Save-as-card on demand.
 *
 * @param {object}   props
 * @param {object}   props.spotlight  - { kind, label, summary?, files, concepts? }
 * @param {Array}    props.cards       - concept cards linked to this spotlight
 * @param {Function} props.onAsk       - (question) => Promise<{answer, role, source}>
 * @param {Function} props.onSaveCard  - ({title, summary}) => Promise<void>
 * @param {Function} props.onClose
 */
export default function ConceptPanel({ spotlight, cards = [], onAsk, onSaveCard, onClose }) {
  const isCommit = spotlight.kind === 'commit'
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState(null)
  const [asking, setAsking] = useState(false)
  const [showSave, setShowSave] = useState(false)
  // App remounts this panel (via key) when the spotlight changes, so initial
  // state is always fresh — no reset effect needed.
  const [title, setTitle] = useState(() => (isCommit ? (spotlight.summary || spotlight.label) : spotlight.label))
  const [summary, setSummary] = useState('')
  const [saved, setSaved] = useState(false)

  async function ask(q) {
    const query = (q ?? question).trim()
    if (!query) return
    setQuestion(query); setAsking(true); setAnswer(null)
    const res = await onAsk?.(query)
    setAsking(false)
    setAnswer(res?.ok ? res : { answer: 'Could not get an answer.', source: '' })
  }

  async function save() {
    if (!title.trim()) return
    await onSaveCard?.({ title: title.trim(), summary: summary.trim() })
    setSaved(true); setShowSave(false)
  }

  const concepts = spotlight.concepts || []
  // Only rename-coupled (high-signal) concepts raise the "you missed some" flag;
  // shared vocabulary (status enums, action constants) legitimately spans files.
  const partial = concepts.filter(c => c.partial && c.signal === 'high')
  const suggestions = isCommit
    ? ['What happened here?', 'Why did these files change?', 'Was anything missed?']
    : ['What does this concept do?', 'Where does it live?', 'Why does it matter?']

  return (
    <div className="concept-panel" onPointerDown={e => e.stopPropagation()}>
      <header className="concept-head">
        <div className="concept-head-main">
          <span className={`concept-kind ${spotlight.kind}`}>{isCommit ? 'COMMIT' : 'CONCEPT'}</span>
          <span className="concept-title">{isCommit ? spotlight.summary || spotlight.label : spotlight.label}</span>
        </div>
        <button className="concept-x" onClick={onClose} aria-label="Close">✕</button>
      </header>

      <div className="concept-body">
        {/* What this is — answered from the canvas, no code */}
        <div className="concept-meta">
          {isCommit ? (
            <>
              <span className="concept-sha">{spotlight.label}</span>
              <span>{spotlight.count} file{spotlight.count === 1 ? '' : 's'}</span>
              {concepts.length > 0 && <span>· {concepts.length} concept{concepts.length === 1 ? '' : 's'}</span>}
            </>
          ) : (
            <span>Appears in {(spotlight.files || []).length} files · {spotlight.count} places</span>
          )}
        </div>

        {isCommit && concepts.length > 0 && (
          <div className="concept-affected">
            {concepts.slice(0, 8).map(c => (
              <span key={c.identifier} className={`concept-pill${c.partial && c.signal === 'high' ? ' partial' : ''}`}
                title={c.partial ? `${c.touched}/${c.total} files`
                                  + (c.signal === 'high' ? ' · rename-coupled' : ' · shared vocabulary')
                                 : 'fully covered'}>
                {c.identifier}{c.partial ? ` ${c.touched}/${c.total}` : ''}
              </span>
            ))}
          </div>
        )}

        {partial.length > 0 && (
          <div className="concept-warn">
            <strong>{partial[0].identifier}</strong> spans {partial[0].total} files — this change
            updated {partial[0].touched}. Worth checking the other {partial[0].total - partial[0].touched}?
          </div>
        )}

        {/* Linked concept cards */}
        {cards.length > 0 && (
          <div className="concept-cards">
            {cards.map(card => (
              <div key={card.id} className="concept-card">
                <div className="concept-card-title">{card.title}</div>
                {card.summary && <div className="concept-card-summary">{card.summary}</div>}
              </div>
            ))}
          </div>
        )}

        {/* Ask Concept */}
        <div className="concept-ask">
          <div className="concept-ask-row">
            <input
              type="text" className="concept-ask-input" placeholder="Ask about this…"
              value={question} onChange={e => setQuestion(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') ask() }}
            />
            <button className="concept-ask-btn" disabled={asking || !question.trim()} onClick={() => ask()}>
              {asking ? '…' : 'Ask'}
            </button>
          </div>
          {!answer && !asking && (
            <div className="concept-suggest">
              {suggestions.map(s => (
                <button key={s} className="concept-suggest-chip" onClick={() => ask(s)}>{s}</button>
              ))}
            </div>
          )}
          {answer && (
            <div className="concept-answer">
              <div className="concept-answer-text">{answer.answer}</div>
              {answer.source && <div className="concept-answer-src">— {answer.source}</div>}
            </div>
          )}
        </div>
      </div>

      <footer className="concept-foot">
        {saved && <span className="concept-saved">✓ saved</span>}
        {!showSave ? (
          <button className="concept-savebtn" onClick={() => setShowSave(true)}>+ Save as concept card</button>
        ) : (
          <div className="concept-saveform">
            <input type="text" className="concept-ask-input" placeholder="Card title"
              value={title} onChange={e => setTitle(e.target.value)} />
            <input type="text" className="concept-ask-input" placeholder="Short summary (optional)"
              value={summary} onChange={e => setSummary(e.target.value)} />
            <div className="concept-saveform-actions">
              <button className="concept-ask-btn" disabled={!title.trim()} onClick={save}>Save</button>
              <button className="concept-savebtn" onClick={() => setShowSave(false)}>Cancel</button>
            </div>
          </div>
        )}
      </footer>
    </div>
  )
}
