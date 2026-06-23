import { useState, useEffect } from 'react'

/**
 * CouncilHandoffBubble — a small chat-style bubble for the LIVE external-council handoff.
 *
 * Appears ONLY on a handoff/verdict/status websocket event (never constant polling noise), shows the
 * route (`Codex → Claude Code` / `Claude Code → Codex` / `Codex verified`), the objective, the
 * commit, and the receiver's next action in <10s, with a "View handoff" expand for ids / acceptance /
 * the exact commit trailers CC must use. Not a panel — a dismissible toast.
 *
 * @param {object|null} handoff - the latest external_council_* event payload (or null = hidden)
 * @param {Function}    onDismiss
 */
const DIR = {
  codex_to_claude: { accent: 'var(--accent)',        label: 'Codex → Claude Code' },
  claude_to_codex: { accent: 'var(--accent-orange)', label: 'Claude Code → Codex' },
  codex_verdict:   { accent: 'var(--solid)',         label: 'Codex verified ✓' },
  claude_working:  { accent: 'var(--text-muted)',    label: 'Claude Code working…' },
  needs_human:     { accent: 'var(--violation)',     label: 'Council needs you' },
}

export default function CouncilHandoffBubble({ handoff, onDismiss }) {
  const [expanded, setExpanded] = useState(false)

  // A VERIFIED verdict is a one-shot notification — auto-fade; active handoffs persist until the
  // next event or an explicit dismiss. (The parent keys this component on episode+status, so a new
  // handoff remounts it — `expanded` resets to false naturally, no reset effect needed.)
  useEffect(() => {
    if (handoff?.direction === 'codex_verdict') {
      const t = setTimeout(() => onDismiss?.(), 8000)
      return () => clearTimeout(t)
    }
  }, [handoff, onDismiss])

  if (!handoff) return null
  const dir = DIR[handoff.direction] || DIR.claude_working
  const sha = (handoff.latestCommit || '').slice(0, 7)
  const hasDetail = (handoff.taskIds?.length || 0) > 0 || (handoff.acceptance?.length || 0) > 0
                    || !!handoff.trailers || !!handoff.episodeId

  return (
    <div className="council-bubble" style={{ borderColor: dir.accent }} role="status" aria-live="polite">
      <div className="council-bubble-head">
        <span className="council-bubble-dot" style={{ background: dir.accent }} />
        <span className="council-bubble-route" style={{ color: dir.accent }}>{dir.label}</span>
        <span className="council-bubble-pill">{handoff.status}</span>
        <span style={{ flex: 1 }} />
        <button className="council-bubble-x" onClick={() => onDismiss?.()} title="Dismiss" aria-label="Dismiss">✕</button>
      </div>

      {handoff.objective && <div className="council-bubble-obj">{handoff.objective}</div>}
      {handoff.nextAction && <div className="council-bubble-next">→ {handoff.nextAction}</div>}

      <div className="council-bubble-foot">
        {sha && <span className="council-bubble-sha" title={handoff.latestCommit}>⎇ {sha}</span>}
        <span style={{ flex: 1 }} />
        {hasDetail && (
          <button className="council-bubble-more" onClick={() => setExpanded(v => !v)}>
            {expanded ? 'Hide' : 'View handoff'}
          </button>
        )}
      </div>

      {expanded && (
        <div className="council-bubble-detail">
          {handoff.episodeId && <Row k="episode" v={handoff.episodeId} mono />}
          {handoff.taskIds?.length > 0 && <Row k="tasks" v={handoff.taskIds.join(', ')} mono />}
          {handoff.acceptance?.length > 0 && (
            <div className="council-bubble-row">
              <span className="council-bubble-k">acceptance</span>
              <span className="council-bubble-v">
                {handoff.acceptance.map((a, i) => <div key={i}>• {a}</div>)}
              </span>
            </div>
          )}
          {handoff.trailers && (
            <div className="council-bubble-row">
              <span className="council-bubble-k">commit trailers</span>
              <span className="council-bubble-v council-bubble-mono">
                {Object.entries(handoff.trailers).map(([k, v]) => <div key={k}>{k}: {v}</div>)}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Row({ k, v, mono }) {
  return (
    <div className="council-bubble-row">
      <span className="council-bubble-k">{k}</span>
      <span className={'council-bubble-v' + (mono ? ' council-bubble-mono' : '')}>{v}</span>
    </div>
  )
}
