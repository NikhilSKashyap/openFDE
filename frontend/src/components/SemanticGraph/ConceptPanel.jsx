import { useEffect, useRef, useState } from 'react'
import { conceptMeaning, fileRole, whyCheck, nextActions } from './conceptMeta'
import { cardTitleFor as commitDisplayTitle } from '../../store/pmState'
import { createEpisodePr, getPrReadiness, runVerify } from '../../api/backend'

// Each shipping blocker carries its next action — the panel must answer "so what
// do I click?" without the user asking (user-feel defect #2).
const BLOCKER_HINTS = [
  [/no landed commits/i, 'Land these changes first (⤓ above)'],
  [/uncommitted files/i, 'Land these changes to commit them'],
  [/verification has not run/i, 'Run checks (Verification, above)'],
  [/verification failed/i, 'fix the failures, then Run checks again'],
  [/already on .+ no PR needed/i, 'already merged — nothing to ship'],
  [/gh CLI/i, 'install GitHub CLI: brew install gh'],
  [/operational\/meta/i, 'operational episodes never ship'],
]
const blockerHint = (row) => (BLOCKER_HINTS.find(([re]) => re.test(row)) || [])[1]

// Friendly labels for the normalized episode lifecycle states (Auto-Land).
const EP_STATUS_LABEL = {
  open: 'open', reviewing: 'reviewing', auto_landing: 'auto-landing…',
  landed: 'landed', failed: 'failed', needs_manual_land: 'needs manual land',
  complete_no_changes: 'no changes',
}

// Minimal unified-diff line tinting for the patch summary preview.
function patchLineClass(ln) {
  if (ln.startsWith('+') && !ln.startsWith('+++')) return 'cpatch-add'
  if (ln.startsWith('-') && !ln.startsWith('---')) return 'cpatch-del'
  if (ln.startsWith('@@')) return 'cpatch-hunk'
  if (ln.startsWith('diff ') || ln.startsWith('index ') || ln.startsWith('+++') || ln.startsWith('---')) return 'cpatch-meta'
  return 'cpatch-ctx'
}

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
export default function ConceptPanel({ spotlight, cards = [], onAsk, onSaveCard, onFocusConcept, onClose, onLand, landing = false, onSpotlightCommit }) {
  const isCommit = spotlight.kind === 'commit'
  const isWorktree = spotlight.kind === 'worktree'
  const isEpisode = spotlight.kind === 'episode'
  const isOutside = spotlight.kind === 'outside'
  const isStoryConcept = spotlight.kind === 'storyConcept'   // a Story-view concept
  // Commit, worktree, prompt-episode, Outside bucket, and a Story concept are all
  // "a change to review" — same card shell (files + concept checks + commits),
  // differing in label, source, and which sections show.
  const isChange = isCommit || isWorktree || isEpisode || isOutside || isStoryConcept
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState(null)
  const [asking, setAsking] = useState(false)
  const [showSave, setShowSave] = useState(false)
  const [showTouched, setShowTouched] = useState(false)
  const [showAllCheck, setShowAllCheck] = useState(false)
  const [expandedId, setExpandedId] = useState(null)
  const [title, setTitle] = useState(() => (isChange ? (spotlight.summary || spotlight.label) : spotlight.label))
  const [summary, setSummary] = useState('')
  const [saved, setSaved] = useState(false)
  // Verify Gate receipts: fresh local run wins over the evidence the episode arrived with.
  const [verifyLocal, setVerifyLocal] = useState(null)
  const [verifyBusy, setVerifyBusy] = useState(false)
  const [verifyErr, setVerifyErr] = useState('')
  const verify = verifyLocal || spotlight.verify || null

  async function runChecks() {
    setVerifyBusy(true); setVerifyErr('')
    const res = await runVerify(spotlight.episodeId ? { episodeId: spotlight.episodeId } : {})
    setVerifyBusy(false)
    if (res?.ok && res.verify) setVerifyLocal(res.verify)
    else setVerifyErr('checks did not return — server may still be running them; retry shortly')
    // The shipping verdict consumes this evidence — refresh it so a "verification
    // has not run" blocker clears the moment the checks finish, not on card reopen.
    if (spotlight.episodeId) {
      const r = await getPrReadiness(spotlight.episodeId)
      if (r?.ok && r.readiness) setReadyLocal({ eid: spotlight.episodeId, data: r.readiness })
    }
  }

  // Land as PR (manual, v1): branch + push + gh pr create with the story as the body.
  const [prLocal, setPrLocal] = useState(null)
  const [prBusy, setPrBusy] = useState(false)
  const [prErr, setPrErr] = useState('')
  const pr = (prLocal?.eid === spotlight.episodeId ? prLocal.data : null) || spotlight.pr || null

  async function landAsPr() {
    setPrBusy(true); setPrErr('')
    const res = await createEpisodePr(spotlight.episodeId)
    setPrBusy(false)
    if (res?.ok && res.pr) setPrLocal({ eid: spotlight.episodeId, data: res.pr })
    else setPrErr(res?.error || 'PR failed')
  }

  // Ready-for-PR (v1.1): deterministic verdict. The embedded payload paints first;
  // a fresh read-only check replaces it when the card opens (worktree state moves).
  // Results are keyed by episode so a stale fetch never leaks across spotlights.
  const [readyLocal, setReadyLocal] = useState(null)
  const readiness = (readyLocal?.eid === spotlight.episodeId ? readyLocal.data : null)
    || spotlight.prReadiness || null

  useEffect(() => {
    if (!isEpisode || !spotlight.episodeId) return undefined
    let alive = true
    ;(async () => {
      const r = await getPrReadiness(spotlight.episodeId)
      if (alive && r?.ok && r.readiness) {
        setReadyLocal({ eid: spotlight.episodeId, data: r.readiness })
      }
    })()
    return () => { alive = false }
  }, [isEpisode, spotlight.episodeId])

  // A Land finishing (`landing` true → false) changes everything the verdict reads
  // (commits exist, tree is clean) — and the re-spotlight that follows carries no
  // readiness and won't re-fire the mount effect for the same episode. Re-verdict here.
  const prevLandingRef = useRef(false)
  useEffect(() => {
    const justFinished = prevLandingRef.current && !landing
    prevLandingRef.current = landing
    if (!justFinished || !isEpisode || !spotlight.episodeId) return undefined
    let alive = true
    ;(async () => {
      const r = await getPrReadiness(spotlight.episodeId)
      if (alive && r?.ok && r.readiness) {
        setReadyLocal({ eid: spotlight.episodeId, data: r.readiness })
      }
    })()
    return () => { alive = false }
  }, [landing, isEpisode, spotlight.episodeId])

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

  const suggestions = isChange
    ? ['What happened here?', 'Why did these files change?', 'Was anything missed?']
    : ['What does this concept do?', 'Where does it live?', 'Why does it matter?']

  const kindLabel = isEpisode ? (spotlight.tag || 'PROMPT') : isOutside ? 'OUTSIDE'
    : isStoryConcept ? 'CONCEPT' : isWorktree ? 'CHANGES' : isCommit ? 'COMMIT' : 'CONCEPT'
  // Compact "+adds −dels" line for a change spotlight (from worktree/commit stat).
  const stat = spotlight.stat || null
  const [showPatch, setShowPatch] = useState(false)
  const [showFiles, setShowFiles] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const episodeCommits = spotlight.commits || []
  // Auto-Land: OpenFDE commits on completion. The manual Land button stays only as a
  // fallback while the episode is still reviewing or was held back (needs_manual_land).
  const canLand = onLand && (isWorktree
    || (isEpisode && (spotlight.status === 'reviewing' || spotlight.status === 'needs_manual_land')))
  // Per-file entries (path + status). Ensures new/untracked files that have no
  // canvas box and no concept are still listed — file-level impact, never dropped.
  const fileEntries = spotlight.fileEntries
    || (spotlight.files || []).map(p => ({ path: p, status: '' }))

  return (
    <div className="concept-panel" onPointerDown={e => e.stopPropagation()}>
      <header className="concept-head">
        <div className="concept-head-main">
          <span className={`concept-kind ${spotlight.kind}`}>{kindLabel}</span>
          <span className="concept-title">{spotlight.title || (isChange ? spotlight.summary || spotlight.label : spotlight.label)}</span>
        </div>
        <button className="concept-x" onClick={onClose} aria-label="Close">✕</button>
      </header>

      <div className="concept-body">
        <div className="concept-meta">
          {(isEpisode || isOutside || isStoryConcept) ? (
            <>
              {spotlight.status && (
                <span className={`concept-ep-status st-${spotlight.status}`}>
                  {EP_STATUS_LABEL[spotlight.status] || spotlight.status}
                </span>
              )}
              {isEpisode && spotlight.summarySource && spotlight.summarySource !== 'deterministic' && (
                <span className="concept-ai-badge" title={`Title & summary by ${spotlight.summarySource}`}>✦ ai</span>
              )}
              {episodeCommits.length > 0 && <span>{episodeCommits.length} commit{episodeCommits.length === 1 ? '' : 's'}</span>}
              {(isEpisode || isStoryConcept) && <span>· {spotlight.count} file{spotlight.count === 1 ? '' : 's'}</span>}
              {isStoryConcept && (spotlight.tags || []).length > 0 && <span>· {spotlight.tags.join(', ')}</span>}
              {isEpisode && episodeCommits.length === 0 && spotlight.status === 'reviewing' && (
                <span className="concept-meta-flag">· awaiting Land</span>
              )}
            </>
          ) : isChange ? (
            <>
              <span className="concept-sha">{isWorktree ? 'uncommitted' : spotlight.label}</span>
              <span>{spotlight.count} file{spotlight.count === 1 ? '' : 's'}</span>
              {stat && <span>· +{stat.additions} −{stat.deletions}</span>}
              <span className={review.length ? 'concept-meta-flag' : ''}>
                · {review.length ? `${review.length} may need review` : 'looks consistent'}
              </span>
            </>
          ) : (
            <span>Appears in {(spotlight.files || []).length} files · {spotlight.count} places</span>
          )}
        </div>

        {/* Episode / concept summary. (The full original prompt — below — is shown
            for episodes only; the prompt is preserved verbatim, summary is the gloss.) */}
        {(isEpisode || isStoryConcept) && spotlight.summary && (
          <div className="concept-ep-summary">{spotlight.summary}</div>
        )}
        {isEpisode && spotlight.prompt && (
          <div className="concept-ep-prompt">
            <button className="concept-files-toggle" onClick={() => setShowPrompt(s => !s)}>
              {showPrompt ? '▾' : '▸'} Full prompt
            </button>
            {showPrompt
              ? <pre className="concept-ep-prompt-full">{spotlight.prompt}</pre>
              : <div className="concept-ep-prompt-peek">{spotlight.prompt.split('\n')[0].slice(0, 120)}{spotlight.prompt.length > 120 ? '…' : ''}</div>}
          </div>
        )}

        {/* Land — OpenFDE commits the reviewed worktree changes (the only
            user-facing commit path). Shown for the dirty worktree and for a prompt
            episode still under review. Calm, obvious, one click. */}
        {canLand && (
          <div className="concept-land">
            <button className="concept-land-btn" disabled={landing} onClick={() => onLand()}>
              {landing ? 'Landing…' : '⤓ Land these changes'}
            </button>
            <span className="concept-land-hint">OpenFDE creates the commit and links it to this prompt.</span>
          </div>
        )}

        {/* Commits — evidence inside the prompt (or the Outside bucket). Each row is
            clickable → the existing single-commit spotlight (impact/diff). */}
        {(isEpisode || isOutside) && episodeCommits.length > 0 && (
          <div className="concept-commits">
            <div className="concept-commits-head">
              {episodeCommits.length} commit{episodeCommits.length === 1 ? '' : 's'}{isEpisode ? ' landed' : ''}
            </div>
            {episodeCommits.map(c => (
              <button
                key={c.sha} className="concept-commit-row"
                title={`${commitDisplayTitle(c)}\n${(c.summary || '').replace(/^openfde:\s*/, '')}\nopen impact / diff`}
                onClick={() => onSpotlightCommit?.(c.sha)} disabled={!onSpotlightCommit}>
                <span className="concept-commit-sha">{c.shortSha}</span>
                {/* Clean display title (backend `commit_display` via displayTitle), never the
                    noisy raw subject ("openfde: Here's the CC prompt"). Raw stays in the tooltip. */}
                <span className="concept-commit-msg">{commitDisplayTitle(c)}</span>
                <span className="concept-commit-meta">
                  {c.fileCount ? `${c.fileCount}f` : ''}{c.fileCount && c.timestamp ? ' · ' : ''}{relTime(c.timestamp)}
                </span>
              </button>
            ))}
          </div>
        )}
        {isEpisode && episodeCommits.length === 0 && (
          <div className="concept-commits">
            <div className="concept-commits-head">
              {spotlight.status === 'reviewing' ? 'Edited — not yet landed (review & Land)' : 'No commits yet'}
            </div>
          </div>
        )}

        {/* Verification — the Verify Gate's receipts for this episode: each local check
            with status + one-line summary (output tail in the tooltip). Run checks
            re-verifies now and attaches fresh evidence to the episode. */}
        {isEpisode && (
          <div className="concept-commits" onClick={e => e.stopPropagation()}>
            <div className="concept-commits-head" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span>
                Verification
                {verify ? (
                  <span style={{
                    marginLeft: 6, fontWeight: 700,
                    color: verify.status === 'passed' ? 'var(--solid)'
                      : verify.status === 'failed' ? 'var(--violation)' : 'var(--text-muted)',
                  }}>{verify.status}</span>
                ) : (
                  <span style={{ marginLeft: 6, color: 'var(--text-muted)' }}>not run</span>
                )}
              </span>
              <span style={{ flex: 1 }} />
              <button
                onClick={runChecks} disabled={verifyBusy}
                title="Run the repo's local checks now and attach the evidence to this episode"
                style={{
                  padding: '1px 8px', borderRadius: 99, cursor: verifyBusy ? 'wait' : 'pointer',
                  fontFamily: 'inherit', fontSize: 10, color: 'var(--accent)',
                  background: 'rgba(124,111,247,0.10)', border: '1px solid rgba(124,111,247,0.35)',
                }}
              >
                {verifyBusy ? 'Running…' : 'Run checks'}
              </button>
            </div>
            {verifyErr && (
              <div style={{ padding: '2px 6px', fontSize: 10, color: 'var(--violation)' }}>
                {verifyErr}
              </div>
            )}
            {verify && (verify.checks || []).map(ch => (
              <div key={ch.id} title={ch.outputTail || ch.summary || ch.label} style={{
                display: 'flex', alignItems: 'center', gap: 6, padding: '3px 6px',
                fontSize: 11, color: 'var(--text)',
              }}>
                <span style={{
                  width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                  background: ch.status === 'passed' ? 'var(--solid)' : 'var(--violation)',
                }} />
                <span style={{ fontWeight: 600, flexShrink: 0 }}>{ch.label}</span>
                <span style={{
                  color: 'var(--text-muted)', overflow: 'hidden',
                  textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{ch.summary}</span>
                {Number.isFinite(ch.durationMs) && (
                  <span style={{ marginLeft: 'auto', flexShrink: 0, fontSize: 9.5, color: 'var(--text-muted)' }}>
                    {(ch.durationMs / 1000).toFixed(1)}s
                  </span>
                )}
              </div>
            ))}
            {verify && verify.status === 'skipped' && (
              <div style={{ padding: '3px 6px', fontSize: 10.5, color: 'var(--text-muted)', fontStyle: 'italic' }}>
                {verify.note || 'verification not configured'}
              </div>
            )}

            {/* Shipping panel (v1.1) — the deterministic ready-for-PR verdict, with
                its receipts (✓) or blockers (✕). Evidence + policy decide; the
                Create button is enabled ONLY when the gate says ready. An existing
                PR replaces the whole panel with its link. */}
            <div style={{ margin: '6px 6px 2px', padding: '7px 9px', borderRadius: 6,
                          border: '1px solid var(--border)', background: 'var(--surface-2)' }}>
              {pr?.url ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--solid)' }}>
                    Pull request created
                  </span>
                  <a
                    href={pr.url} target="_blank" rel="noreferrer"
                    onClick={e => e.stopPropagation()}
                    title={`${pr.title || 'Pull request'} — ${pr.branch || ''}`}
                    style={{
                      fontSize: 10.5, fontWeight: 700, fontFamily: 'ui-monospace, monospace',
                      color: 'var(--accent)', background: 'rgba(124,111,247,0.10)',
                      border: '1px solid rgba(124,111,247,0.35)', padding: '2px 9px',
                      borderRadius: 99, textDecoration: 'none',
                    }}
                  >
                    PR #{pr.number} ↗
                  </a>
                </div>
              ) : (
                <>
                  <div style={{
                    fontSize: 11, fontWeight: 600, marginBottom: 4,
                    color: readiness?.status === 'ready' ? 'var(--solid)'
                      : readiness?.status === 'blocked' ? 'var(--violation)' : 'var(--text-muted)',
                  }}>
                    {readiness?.status === 'ready' ? 'Ready to ship'
                      : readiness?.status === 'blocked' ? 'Not ready for PR'
                        : 'Checking readiness…'}
                  </div>
                  {(readiness?.status === 'ready' ? readiness.reasons : readiness?.blockedBy || [])
                    .map(row => {
                      const hint = readiness?.status === 'blocked' ? blockerHint(row) : null
                      return (
                        <div key={row} style={{ display: 'flex', alignItems: 'baseline', gap: 6,
                                                fontSize: 10.5, lineHeight: 1.6,
                                                color: 'var(--text-muted)' }}>
                          <span style={{ flexShrink: 0, fontWeight: 700,
                                         color: readiness?.status === 'ready' ? 'var(--solid)' : 'var(--violation)' }}>
                            {readiness?.status === 'ready' ? '✓' : '✕'}
                          </span>
                          <span>
                            {row}
                            {hint && (
                              <span style={{ opacity: 0.7, fontStyle: 'italic' }}> — {hint}</span>
                            )}
                          </span>
                        </div>
                      )
                    })}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 6 }}>
                    <button
                      onClick={landAsPr}
                      disabled={prBusy || readiness?.status !== 'ready'}
                      title={readiness?.status === 'ready'
                        ? 'Create a branch from this episode’s commits and open a GitHub PR (gh)'
                        : 'Blocked — resolve the items above first'}
                      style={{
                        padding: '2px 10px', borderRadius: 99, fontFamily: 'inherit', fontSize: 10.5,
                        cursor: prBusy ? 'wait' : readiness?.status === 'ready' ? 'pointer' : 'not-allowed',
                        color: 'var(--accent)', background: 'rgba(124,111,247,0.10)',
                        border: '1px solid rgba(124,111,247,0.35)',
                        opacity: readiness?.status === 'ready' ? 1 : 0.5,
                      }}
                    >
                      {prBusy ? 'Opening PR…' : 'Create Pull Request'}
                    </button>
                    {prErr && (
                      <span title={prErr} style={{ fontSize: 10, color: 'var(--violation)', maxWidth: 200,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {prErr}
                      </span>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        )}

        {/* Changed files — collapsible list so new / untracked / off-canvas files
            stay visible even when they have no concept and no box to light. */}
        {isChange && fileEntries.length > 0 && (
          <div className="concept-files">
            <button className="concept-files-toggle" onClick={() => setShowFiles(s => !s)}>
              {showFiles ? '▾' : '▸'} {spotlight.count || fileEntries.length} changed file{(spotlight.count || fileEntries.length) === 1 ? '' : 's'}
            </button>
            {showFiles && (
              <div className="concept-files-list">
                {fileEntries.map(f => (
                  <button key={f.path} className="concept-file-row" title="copy path" onClick={() => copyPath(f.path)}>
                    {f.status && <span className={`concept-file-badge st-${(f.status || '').toLowerCase().replace(/[^a-z]/g, '') || 'm'}`}>{f.status === '?' ? 'NEW' : f.status}</span>}
                    <span className="concept-file-path">{f.path}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Patch summary — collapsible diff for a change spotlight (commit/worktree). */}
        {isChange && spotlight.patch && (
          <div className="concept-patch">
            <button className="concept-patch-toggle" onClick={() => setShowPatch(s => !s)}>
              {showPatch ? '▾' : '▸'} What changed (diff{spotlight.patchTruncated ? ', truncated' : ''})
            </button>
            {showPatch && (
              <pre className="concept-patch-body">
                {String(spotlight.patch).split('\n').slice(0, 200).map((ln, i) => (
                  <div key={i} className={patchLineClass(ln)}>{ln || ' '}</div>
                ))}
              </pre>
            )}
          </div>
        )}

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
        {isChange && touched.length > 0 && (
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

// Compact relative time for commit rows ("3h ago"). Empty string for missing/bad dates.
function relTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const secs = Math.max(0, (Date.now() - d.getTime()) / 1000)
  if (secs < 60) return 'just now'
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}
