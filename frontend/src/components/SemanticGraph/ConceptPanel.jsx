import { useState } from 'react'
import { conceptMeaning, fileRole, whyCheck, nextActions } from './conceptMeta'

/**
 * Concept panel (Step 37a) — canvas-native card for the active concept/commit.
 * Partial-tether warnings become **Concept Check cards**: what changed, which
 * related files did NOT, why the concept matters, and a next action (Ask Concept
 * / inspect on canvas). Canvas-first, no code dump.
 *
 * @param {object}   props
 * @param {object}   props.spotlight  - { kind, label, summary?, files, concepts?, sha? }
 * @param {Array}    props.cards       - concept cards linked to this spotlight
 * @param {Function} props.onAsk       - (question, concept?) => Promise<{answer, role, source}>
 * @param {Function} props.onSaveCard  - ({title, summary, concept?}) => Promise<void>
 * @param {Function} props.onFocusConcept - (concept|null) => void  (highlight on canvas)
 * @param {Function} props.onClose
 */
export default function ConceptPanel({ spotlight, cards = [], onAsk, onSaveCard, onFocusConcept, onClose }) {
  const isCommit = spotlight.kind === 'commit'
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState(null)
  const [asking, setAsking] = useState(false)
  const [showSave, setShowSave] = useState(false)
  const [showTouched, setShowTouched] = useState(false)
  const [showAllCheck, setShowAllCheck] = useState(false)
  const [expandedId, setExpandedId] = useState(null)
  const [title, setTitle] = useState(() => (isCommit ? (spotlight.summary || spotlight.label) : spotlight.label))
  const [summary, setSummary] = useState('')
  const [saved, setSaved] = useState(false)

  const concepts = spotlight.concepts || []
  // Only cross-layer rename-coupled (high-signal) concepts may need review.
  const review = concepts.filter(c => c.partial && c.signal === 'high')
  const touched = concepts.filter(c => !(c.partial && c.signal === 'high'))
  const shownReview = showAllCheck ? review : review.slice(0, 3)
  const expanded = review.find(c => c.identifier === expandedId) || null

  async function ask(q, concept) {
    const query = (q ?? question).trim()
    if (!query) return
    setQuestion(query); setAsking(true); setAnswer(null)
    const res = await onAsk?.(query, concept)
    setAsking(false)
    setAnswer(res?.ok ? res : { answer: 'Could not get an answer.', source: '' })
  }

  function toggleConcept(c) {
    const open = expandedId === c.identifier
    setExpandedId(open ? null : c.identifier)
    onFocusConcept?.(open ? null : c)   // highlight changed/related files on canvas
  }

  function askAboutConcept(c) {
    ask(`Explain the concept "${c.identifier}" (${conceptMeaning(c.identifier)}). `
        + `This commit changed it in ${c.touchedFiles.join(', ')}, but related files `
        + `${c.untouchedFiles.join(', ')} were not changed. Was this complete, or should `
        + `anything else be updated?`, c)
  }

  async function save() {
    if (!title.trim()) return
    await onSaveCard?.({
      title: title.trim(), summary: summary.trim(), concept: expanded,
      meaning: expanded ? conceptMeaning(expanded.identifier) : '',
      whyCheck: expanded ? whyCheck(expanded.identifier, expanded.files) : '',
    })
    setSaved(true); setShowSave(false)
  }

  function copyPath(p) { try { navigator.clipboard?.writeText(p) } catch { /* ignore */ } }

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
        <div className="concept-meta">
          {isCommit ? (
            <>
              <span className="concept-sha">{spotlight.label}</span>
              <span>{spotlight.count} file{spotlight.count === 1 ? '' : 's'}</span>
              <span className={review.length ? 'concept-meta-flag' : ''}>
                · {review.length ? `${review.length} may need review` : 'looks consistent'}
              </span>
            </>
          ) : (
            <span>Appears in {(spotlight.files || []).length} files · {spotlight.count} places</span>
          )}
        </div>

        {/* Concept Checks — actionable cards for partially-updated concepts */}
        {review.length > 0 && (
          <div className="concept-checks">
            <div className="concept-checks-head">
              {review.length} architecture concept{review.length === 1 ? '' : 's'} may need review
            </div>
            {shownReview.map(c => {
              const open = expandedId === c.identifier
              const related = c.total - c.touched
              return (
                <div key={c.identifier} className={`ccard${open ? ' open' : ''}`}>
                  <button className="ccard-head" onClick={() => toggleConcept(c)} aria-expanded={open}>
                    <span className="ccard-name">{c.identifier}</span>
                    <span className="ccard-line">
                      Changed {c.touched} place{c.touched === 1 ? '' : 's'} · review {related} related place{related === 1 ? '' : 's'}
                    </span>
                    <span className="ccard-chev">{open ? '▾' : '▸'}</span>
                  </button>
                  {open && (
                    <div className="ccard-body">
                      <div className="ccard-meaning">{conceptMeaning(c.identifier)}</div>
                      <div className="ccard-group">
                        <div className="ccard-group-h changed">Changed</div>
                        {c.touchedFiles.map(f => (
                          <button key={f} className="ccard-file ok" title="copy path" onClick={() => copyPath(f)}>
                            <span className="ccard-mark">✓</span>
                            <span className="ccard-path">{f}</span>
                            {fileRole(f) && <span className="ccard-role">— {fileRole(f)}</span>}
                          </button>
                        ))}
                      </div>
                      <div className="ccard-group">
                        <div className="ccard-group-h related">Related to review</div>
                        {c.untouchedFiles.map(f => (
                          <button key={f} className="ccard-file warn" title="copy path" onClick={() => copyPath(f)}>
                            <span className="ccard-mark">!</span>
                            <span className="ccard-path">{f}</span>
                            {fileRole(f) && <span className="ccard-role">— {fileRole(f)}</span>}
                          </button>
                        ))}
                      </div>
                      <div className="ccard-why"><strong>Why check:</strong> {whyCheck(c.identifier, c.files)}</div>
                      <div className="ccard-next">
                        <div className="ccard-next-h">Next</div>
                        {nextActions().map(a => <div key={a} className="ccard-next-item">• {a}</div>)}
                      </div>
                      <div className="ccard-actions">
                        <button className="ccard-action primary" onClick={() => askAboutConcept(c)}>Ask Concept</button>
                        <span className="ccard-hint">files highlighted on canvas</span>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
            {review.length > 3 && (
              <button className="concept-check-more" onClick={() => setShowAllCheck(s => !s)}>
                {showAllCheck ? '▾ show fewer' : `▸ show all ${review.length}`}
              </button>
            )}
          </div>
        )}

        {/* Touched, no action — collapsed shared vocabulary */}
        {isCommit && touched.length > 0 && (
          <div className="concept-touched">
            <button className="concept-touched-toggle" onClick={() => setShowTouched(s => !s)}>
              {showTouched ? '▾' : '▸'} {touched.length} value{touched.length === 1 ? '' : 's'} touched (no action)
            </button>
            {showTouched && (
              <div className="concept-touched-pills">
                {touched.slice(0, 16).map(c => (
                  <span key={c.identifier} className="concept-pill" title={`${c.touched}/${c.total} files`}>
                    {c.identifier}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Notes — saved concept cards */}
        {cards.length > 0 && (
          <div className="concept-cards">
            <div className="concept-cards-head">Notes</div>
            {cards.map(card => (
              <div key={card.id} className="concept-card">
                <div className="concept-card-title">{card.title}</div>
                {card.meaning && <div className="concept-card-summary">{card.meaning}</div>}
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
          {asking && <div className="concept-answer"><div className="concept-answer-text">Thinking…</div></div>}
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
          <button className="concept-savebtn" onClick={() => setShowSave(true)}>
            + Save as concept card{expanded ? ` (${expanded.identifier})` : ''}
          </button>
        ) : (
          <div className="concept-saveform">
            <input type="text" className="concept-ask-input" placeholder="Card title"
              value={title} onChange={e => setTitle(e.target.value)} />
            <input type="text" className="concept-ask-input" placeholder="Short note (optional)"
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
