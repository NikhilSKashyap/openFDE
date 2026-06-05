import { useState, useEffect } from 'react'
import { getSemanticGraph, refreshSemanticGraph } from '../../api/backend'

/**
 * Architecture Evidence — a calm read-only view over the semantic graph
 * (Step 37a). Shows last-generated time, node/edge/tether/risk counts, the top
 * tethers, and provider warnings, with a Refresh button. No canvas clutter.
 *
 * Product rule: provider output is evidence, not truth — every artifact in the
 * underlying graph carries provenance.
 *
 * @param {object}   props
 * @param {Function} props.onClose
 */
export default function SemanticGraphCard({ onClose, onSpotlightTether }) {
  const [summary, setSummary] = useState(null)
  const [exists, setExists] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    ;(async () => {
      const res = await getSemanticGraph()
      if (!alive) return
      if (!res) { setErr('Backend unavailable'); return }
      setExists(res.exists)
      setSummary(res.summary?.exists ? res.summary : null)
    })()
    return () => { alive = false }
  }, [])

  async function refresh() {
    setBusy(true); setErr('')
    const res = await refreshSemanticGraph()
    setBusy(false)
    if (!res?.ok) { setErr(res?.error || 'Refresh failed'); return }
    setExists(true)
    setSummary(res.summary)
  }

  const counts = summary?.counts ?? {}
  const when = summary?.generatedAt ? new Date(summary.generatedAt).toLocaleString() : '—'

  return (
    <>
      <div className="cmd-backdrop" onPointerDown={onClose} />
      <div className="agentset semgraph" role="dialog" aria-modal="true" onPointerDown={e => e.stopPropagation()}>
        <header className="agentset-head">
          <div>
            <div className="agentset-title">Architecture Evidence</div>
            <div className="agentset-sub">Semantic graph · provider output is evidence, not truth</div>
          </div>
          <button className="agentset-x" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="agentset-body">
          {!exists && !summary && (
            <div className="agentset-note">
              No semantic graph yet. Click <strong>Refresh</strong> to scan the repo
              (ast structure, cross-file tethers, risk scan).
            </div>
          )}

          {summary && (
            <>
              <div className="semgraph-meta">
                <span>Generated <strong>{when}</strong></span>
                {summary.commitSha && <span className="semgraph-sha">@ {summary.commitSha.slice(0, 8)}</span>}
              </div>

              <div className="semgraph-counts">
                <Count n={counts.nodes} label="nodes" />
                <Count n={counts.edges} label="edges" />
                <Count n={counts.tethers} label="tethers" />
                <Count n={counts.risks} label="risks" />
              </div>

              <div className="semgraph-section-title">Top tethers — click to spotlight on canvas</div>
              <div className="semgraph-tethers">
                {(summary.topTethers ?? []).map(t => (
                  <button key={t.identifier} type="button" className="semgraph-tether"
                    onClick={() => onSpotlightTether?.(t)} title="Spotlight on canvas">
                    <span className="semgraph-tether-id">{t.identifier}</span>
                    <span className={`semgraph-tether-kind ${t.kind}`}>{t.kind}</span>
                    <span className="semgraph-tether-files">{t.fileCount} files</span>
                    <span className="semgraph-tether-go">spotlight →</span>
                    <div className="semgraph-tether-paths">{(t.files ?? []).join(' · ')}</div>
                  </button>
                ))}
                {(summary.topTethers ?? []).length === 0 && (
                  <div className="agentset-note">No cross-file tethers found.</div>
                )}
              </div>

              <div className="semgraph-section-title">Providers</div>
              <div className="semgraph-providers">
                {(summary.providerRuns ?? []).map(r => (
                  <span key={r.provider} className={`semgraph-prov ${r.ok ? 'ok' : 'skip'}`}>
                    {r.provider} {r.ok ? `· ${r.durationMs}ms` : '· n/a'}
                  </span>
                ))}
              </div>

              {(summary.providerWarnings ?? []).length > 0 && (
                <div className="semgraph-warnings">
                  {summary.providerWarnings.map((w, i) => (
                    <div key={i} className="semgraph-warn">[{w.provider}] {w.warning}</div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        <footer className="agentset-foot">
          <span className="agentset-flash">{err}</span>
          <div className="agentset-actions">
            <button className="agentset-btn primary" disabled={busy} onClick={refresh}>
              {busy ? 'Scanning…' : 'Refresh'}
            </button>
          </div>
        </footer>
      </div>
    </>
  )
}

function Count({ n, label }) {
  return (
    <div className="semgraph-count">
      <div className="semgraph-count-n">{n ?? 0}</div>
      <div className="semgraph-count-l">{label}</div>
    </div>
  )
}
