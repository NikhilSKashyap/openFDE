import { useEffect, useState } from 'react'
import { postFocusVerifyPlan } from '../../api/backend'

const base = (p) => (p || '').split('/').pop() || p

/**
 * FocusLens — L2-B focused subgraph overlay.
 *
 * Renders a SMALL, readable neighborhood (seeds + import / function-flow neighbors from
 * /api/focus/neighborhood) as an overlay over the full canvas, so a large repo shows the few relevant
 * files/functions first instead of the whole hairball. The full canvas underneath is NEVER mutated —
 * Exit just closes this overlay. It tells the truth: "Focused view · N files · K hop", any backend
 * warnings shown quietly, plus the advisory scoped-verify plan (scoped / fallback + reason) for the
 * focused scope. No graph layout engine, no parsing — it just lays out the bounded result the server
 * already computed.
 */
export default function FocusLens({ lens, onExit }) {
  const [verify, setVerify] = useState(null)   // { key, data }
  const result = lens?.result
  const files = result?.files || []
  const filesKey = files.join('|')

  // Advisory scoped-verify plan for the focused files (read-only — never runs or changes the gate).
  // Keyed by the focused set so a stale plan is derived away (no synchronous reset in the effect).
  useEffect(() => {
    let alive = true
    if (filesKey) {
      postFocusVerifyPlan({ touchedFiles: filesKey.split('|') })
        .then(r => { if (alive && r?.ok) setVerify({ key: filesKey, data: r }) })
    }
    return () => { alive = false }
  }, [filesKey])
  const verifyForRender = verify?.key === filesKey ? verify.data : null

  const seeds = new Set(result?.seeds || [])
  const funcs = result?.functions || []
  const funcName = (id) => { const f = funcs.find(x => x.id === id); return f ? `${f.name}()` : id }
  const flowEdges = (result?.edges || []).filter(e => e.type === 'flow').slice(0, 24)
  const warnings = result?.warnings || []
  const hops = lens?.hops ?? 1
  const title = lens?.busy ? 'Focusing…'
    : result ? `Focused view · ${files.length} file${files.length === 1 ? '' : 's'} · ${hops} hop${hops === 1 ? '' : 's'}`
    : 'Focus'

  return (
    <div className="focus-lens" role="dialog" aria-label="Focused view">
      <div className="focus-lens-head">
        <span className="focus-lens-title">{title}</span>
        {lens?.seedLabel && <span className="focus-lens-seed" title={lens.seedLabel}>{base(lens.seedLabel)}</span>}
        <button className="focus-lens-exit" onClick={onExit} title="Exit focus — back to the full canvas">Exit ✕</button>
      </div>

      {lens?.error && (
        <div className="focus-lens-empty">Couldn’t load the focused view. The full canvas is unchanged.</div>
      )}
      {lens?.busy && <div className="focus-lens-empty">Building the focused neighborhood…</div>}

      {result && !lens?.busy && (
        <div className="focus-lens-body">
          <div className="focus-lens-files">
            {files.map(f => (
              <span key={f} className={`focus-node${seeds.has(f) ? ' seed' : ''}`} title={f}>
                {seeds.has(f) && <span className="focus-node-badge">seed</span>}
                {base(f)}
              </span>
            ))}
          </div>

          {flowEdges.length > 0 && (
            <div className="focus-lens-edges">
              <div className="focus-lens-sub">Connected functions</div>
              {flowEdges.map((e, i) => (
                <div key={i} className="focus-edge">
                  {funcName(e.from)} <span className="focus-arrow">→</span> {funcName(e.to)}
                </div>
              ))}
            </div>
          )}

          {verifyForRender && (
            <div className="focus-lens-verify">
              <div className="focus-lens-sub">Scoped verify plan
                <span className={`focus-verify-mode ${verifyForRender.mode}`}>{verifyForRender.mode}</span>
              </div>
              <div className="focus-verify-reason">{verifyForRender.reason}</div>
              {(verifyForRender.warnings || []).map((w, i) => <div key={i} className="focus-warn">{w}</div>)}
            </div>
          )}

          {warnings.length > 0 && (
            <div className="focus-lens-warnings">
              {warnings.map((w, i) => <div key={i} className="focus-warn">{w}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
