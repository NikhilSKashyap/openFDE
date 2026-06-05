import { useState } from 'react'

/**
 * Commit chip rail (Step 37a Slice 3) — a calm, canvas-native lens over recent
 * commits at the top of OpenArchitect. Hover shows what happened; click spotlights
 * the touched boxes + affected concepts on the canvas. Never replaces the
 * Timeline tab.
 *
 * @param {object}   props
 * @param {Array}    props.commits          - git timeline commits (newest first)
 * @param {string}   props.activeSha        - currently spotlighted commit sha
 * @param {Function} props.onSpotlightCommit - (sha) => void
 * @param {number}   [props.max=6]
 */
export default function CommitChipRail({ commits = [], activeSha = null, onSpotlightCommit, max = 6 }) {
  const [busySha, setBusySha] = useState(null)
  const recent = (commits || []).slice(0, max)
  if (!recent.length) return null

  async function pick(sha) {
    setBusySha(sha)
    try { await onSpotlightCommit?.(sha) } finally { setBusySha(null) }
  }

  return (
    <div className="commit-rail" role="list" aria-label="Recent commits">
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
  )
}

function shortMsg(s, n = 28) {
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
