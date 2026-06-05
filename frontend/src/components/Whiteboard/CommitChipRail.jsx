import { useState, useRef, useEffect, useCallback } from 'react'

/**
 * Commit chip rail (Step 37a) — a calm, canvas-native lens over recent commits
 * across the full top-canvas width. Hover shows what happened; click spotlights
 * the touched boxes + affected concepts. Left/right arrows scroll when chips
 * overflow. Never replaces the Timeline tab.
 *
 * @param {object}   props
 * @param {Array}    props.commits          - git timeline commits (newest first)
 * @param {string}   props.activeSha        - currently spotlighted commit sha
 * @param {Function} props.onSpotlightCommit - (sha) => void
 * @param {number}   [props.max=24]
 */
export default function CommitChipRail({ commits = [], activeSha = null, onSpotlightCommit, max = 24 }) {
  const [busySha, setBusySha] = useState(null)
  const [overflow, setOverflow] = useState({ left: false, right: false })
  const scrollRef = useRef(null)
  const recent = (commits || []).slice(0, max)

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
  }, [refreshOverflow, recent.length])

  if (!recent.length) return null

  async function pick(sha) {
    setBusySha(sha)
    try { await onSpotlightCommit?.(sha) } finally { setBusySha(null) }
  }

  function scrollBy(dir) {
    const el = scrollRef.current
    if (el) el.scrollBy({ left: dir * Math.max(180, el.clientWidth * 0.6), behavior: 'smooth' })
  }

  return (
    <div className="commit-rail">
      <button className={`commit-rail-arrow${overflow.left ? '' : ' hidden'}`}
        onClick={() => scrollBy(-1)} aria-label="Scroll commits left" tabIndex={overflow.left ? 0 : -1}>‹</button>
      <div className="commit-rail-track" ref={scrollRef} onScroll={refreshOverflow} role="list" aria-label="Recent commits">
        {recent.map(c => {
          const active = c.sha === activeSha
          return (
            <button
              key={c.sha}
              type="button"
              role="listitem"
              className={`commit-chip${active ? ' active' : ''}${busySha === c.sha ? ' busy' : ''}`}
              onClick={() => pick(c.sha)}
              title={`${c.summary}\n${c.shortSha} · ${relTime(c.timestamp)}`}
            >
              <span className="commit-chip-sha">{c.shortSha}</span>
              <span className="commit-chip-msg">{shortMsg(c.summary)}</span>
            </button>
          )
        })}
      </div>
      <button className={`commit-rail-arrow${overflow.right ? '' : ' hidden'}`}
        onClick={() => scrollBy(1)} aria-label="Scroll commits right" tabIndex={overflow.right ? 0 : -1}>›</button>
    </div>
  )
}

function shortMsg(s, n = 30) {
  const first = (s || '').split('\n')[0]
  return first.length > n ? first.slice(0, n - 1) + '…' : first
}

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
