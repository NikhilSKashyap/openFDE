import { useState, useRef, useEffect, useCallback } from 'react'
import { isOperationalChip, groupEpisodes } from '../../store/railChips.js'

/**
 * Prompt Chapter Rail — canvas-native, prompt-first, **chapters only**.
 *
 * The rail is the story's table of contents: a leading "Review changes" chip when
 * the work tree is dirty, then one chip per prompt **episode** (newest-first, label
 * = the episode's short title), then a single "Outside OpenFDE" chapter chip for
 * commits with no prompt. Commits are NOT rail items — they live as evidence inside
 * the selected prompt's detail card. Clicking a prompt chip spotlights its episode
 * (its files turn amber + the episode card opens); clicking Outside opens a card
 * listing the unattributed commits.
 *
 * @param {object}   props
 * @param {boolean}  props.worktreeDirty      - uncommitted changes exist
 * @param {number}   props.worktreeCount      - changed-file count for the dirty chip
 * @param {Function} props.onReviewChanges    - () => void, spotlight the worktree delta
 * @param {boolean}  props.reviewActive       - the worktree review is currently spotlit
 * @param {Array}    props.episodes           - prompt episodes (newest-first)
 * @param {object}   props.outsideBucket      - { commits } not linked to an episode
 * @param {Function} props.onSpotlightEpisode - (episode) => void
 * @param {string}   props.activeEpisodeId    - currently spotlit episode id
 * @param {Function} props.onSpotlightOutside - (bucket) => void
 * @param {boolean}  props.outsideActive      - the Outside bucket is currently spotlit
 */
export default function CommitChipRail({
  worktreeDirty = false, worktreeCount = 0, onReviewChanges, reviewActive = false,
  episodes = [], outsideBucket = null, onSpotlightEpisode, activeEpisodeId = null,
  onSpotlightOutside, outsideActive = false,
}) {
  const [busy, setBusy] = useState(null)
  const [expandedPgm, setExpandedPgm] = useState({})
  const [overflow, setOverflow] = useState({ left: false, right: false })
  const scrollRef = useRef(null)
  const outsideCommits = (outsideBucket?.commits) || []

  const refreshOverflow = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    setOverflow({
      left: el.scrollLeft > 2,
      right: el.scrollLeft + el.clientWidth < el.scrollWidth - 2,
    })
  }, [])

  useEffect(() => {
    refreshOverflow()
    const el = scrollRef.current
    if (!el) return undefined
    const ro = new ResizeObserver(refreshOverflow)
    ro.observe(el)
    return () => ro.disconnect()
  }, [refreshOverflow, episodes.length, outsideCommits.length])

  const hasAnything = worktreeDirty || episodes.length || outsideCommits.length
  if (!hasAnything) return null

  async function review() {
    setBusy('__worktree__')
    try { await onReviewChanges?.() } finally { setBusy(null) }
  }
  async function pickEpisode(ep) {
    setBusy(ep.episodeId)
    try { await onSpotlightEpisode?.(ep) } finally { setBusy(null) }
  }
  async function pickOutside() {
    setBusy('__outside__')
    try { await onSpotlightOutside?.(outsideBucket) } finally { setBusy(null) }
  }
  function scrollBy(dir) {
    const el = scrollRef.current
    if (el) el.scrollBy({ left: dir * Math.max(180, el.clientWidth * 0.6), behavior: 'smooth' })
  }

  // One prompt-episode chip (also used as a Program's child slice chip when `child`).
  const renderChip = (ep, child = false) => {
    const active = ep.episodeId === activeEpisodeId
    const landed = (ep.commitShas || []).length || (ep.commits || []).length
    // A Program slice is never operational — isOperationalChip ignores noisy-title / stale storyFacts
    // heuristics when ep.programId is set, so a docs-y slice still renders as a product Program slice.
    const operational = isOperationalChip(ep)
    return (
      <button key={ep.episodeId} type="button" role="listitem"
        className={`commit-chip prompt-chip st-${ep.status || 'open'}${active ? ' active' : ''}${operational ? ' ops' : ''}${child ? ' slice-child' : ''}${busy === ep.episodeId ? ' busy' : ''}`}
        onClick={() => pickEpisode(ep)}
        title={`${operational ? 'operational · ' : ep.tag ? ep.tag + ' · ' : ''}${ep.sliceTitle || ep.title || promptLabel(ep)}\n${promptSub(ep)}`}>
        <span className={`prompt-chip-dot st-${ep.status || 'open'}`} aria-hidden="true" />
        {operational
          ? <span className="prompt-chip-ops" title="Operational / meta — not a Story concept">ops</span>
          : ep.tag && <span className="prompt-chip-tag">{ep.tag}</span>}
        <span className="commit-chip-msg">{child ? (ep.sliceTitle || promptLabel(ep)) : promptLabel(ep)}</span>
        {landed > 0
          ? <span className="prompt-chip-commits" title={`${landed} commit${landed === 1 ? '' : 's'} landed`}>✓{landed}</span>
          : ep.status === 'reviewing' ? <span className="prompt-chip-reviewing">review</span> : null}
      </button>
    )
  }

  return (
    <div className="commit-rail">
      <button className={`commit-rail-arrow${overflow.left ? '' : ' hidden'}`}
        onClick={() => scrollBy(-1)} aria-label="Scroll left" tabIndex={overflow.left ? 0 : -1}>‹</button>

      <div className="commit-rail-track" ref={scrollRef} onScroll={refreshOverflow} role="list" aria-label="Prompt story">
        {worktreeDirty && (
          <button
            type="button" role="listitem"
            className={`commit-chip worktree-chip${reviewActive ? ' active' : ''}${busy === '__worktree__' ? ' busy' : ''}`}
            onClick={review}
            title={`${worktreeCount} uncommitted file${worktreeCount === 1 ? '' : 's'} — review changes`}>
            <span className="worktree-chip-dot" aria-hidden="true" />
            <span className="commit-chip-msg">Review changes</span>
            {worktreeCount > 0 && <span className="worktree-chip-count">{worktreeCount}</span>}
          </button>
        )}

        {/* Prompt episode chips. A Program shows ONE parent chip with collapsible child slice chips,
            so its slices read as one journey instead of separate beats. */}
        {groupEpisodes(episodes).map(grp => {
          if (!grp.programId) return renderChip(grp.episodes[0], false)
          const verified = grp.episodes.filter(e => e.status === 'landed').length
          const commits = grp.episodes.reduce((n, e) => n + ((e.commitShas || e.commits || []).length), 0)
          const exp = !!expandedPgm[grp.programId]
          return (
            <span key={grp.programId} className="pgm-group" role="listitem">
              <button type="button"
                className={`commit-chip program-parent${exp ? ' expanded' : ''}`}
                onClick={() => setExpandedPgm(s => ({ ...s, [grp.programId]: !exp }))}
                title={`Program: ${grp.programTitle || 'Program'} — ${verified}/${grp.episodes.length} verified · ${commits} commit${commits === 1 ? '' : 's'}`}>
                <span className="program-parent-dot" aria-hidden="true" />
                <span className="commit-chip-msg">▣ {grp.programTitle || 'Program'}</span>
                <span className="program-parent-meta">{verified}/{grp.episodes.length} · ✓{commits}</span>
                <span className="program-parent-caret">{exp ? '▾' : '▸'}</span>
              </button>
              {exp && grp.episodes.map(e => renderChip(e, true))}
            </span>
          )
        })}

        {/* Outside OpenFDE — ONE chapter chip; its commits show in the detail card. */}
        {outsideCommits.length > 0 && (
          <>
            <span className="rail-divider" aria-hidden="true" />
            <button
              type="button" role="listitem"
              className={`commit-chip outside-chip${outsideActive ? ' active' : ''}${busy === '__outside__' ? ' busy' : ''}`}
              onClick={pickOutside}
              title={`${outsideCommits.length} commit${outsideCommits.length === 1 ? '' : 's'} not made through an OpenFDE prompt`}>
              <span className="outside-chip-dot" aria-hidden="true" />
              <span className="commit-chip-msg">Outside OpenFDE</span>
              <span className="prompt-chip-commits">{outsideCommits.length}</span>
            </button>
          </>
        )}
      </div>

      <button className={`commit-rail-arrow${overflow.right ? '' : ' hidden'}`}
        onClick={() => scrollBy(1)} aria-label="Scroll right" tabIndex={overflow.right ? 0 : -1}>›</button>
    </div>
  )
}

// Rail chip label: the short story title, falling back to the prompt's first line.
function promptLabel(ep) {
  const t = (ep.title || '').trim()
  if (t) return t.length > 34 ? t.slice(0, 33) + '…' : t
  const p = (ep.prompt || ep.summary || '').split('\n')[0].trim()
  if (p) return p.length > 34 ? p.slice(0, 33) + '…' : p
  return ep.kind === 'manual' ? 'Manual changes' : 'Prompt'
}
function promptSub(ep) {
  const bits = []
  if (ep.summary) return ep.summary
  if (ep.kind) bits.push(ep.kind)
  if (ep.status) bits.push(ep.status)
  const n = (ep.commitShas || []).length || (ep.commits || []).length
  if (n) bits.push(`${n} commit${n === 1 ? '' : 's'}`)
  return bits.join(' · ')
}
