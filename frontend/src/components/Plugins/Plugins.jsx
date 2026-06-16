import { useState, useEffect } from 'react'
import { getPlugins, getWebxrSummary } from '../../api/backend'

/**
 * Plugins — a read-only window onto OpenFDE's capability registry
 * (Plugin Registry v1-A/B/C). It calls GET /api/plugins and shows each provider as a
 * card grouped by kind: displayName, kind, status, source, whether it's active for the
 * watched repo, the markers that activate it, and the capabilities it provides.
 *
 * Visibility only — there is no install/enable/load action here (and the backend
 * exposes none). Three sources, one shape: built-ins (Python, JS/TS) show as `builtin`;
 * deterministic suggestions (e.g. WebXR) show as `suggested` when the repo's markers
 * match, else `missing`; and repo-local manifests (`.openfde/plugins/*.json`) carry a
 * `local` tag and show as `available` or `disabled` — metadata only, nothing loaded.
 *
 * @param {object}   props
 * @param {Function} props.onClose
 */
export default function Plugins({ onClose }) {
  const [data, setData]       = useState(null)   // { kinds:[], plugins:[] } | null
  const [error, setError]     = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    getPlugins().then(res => {
      if (!alive) return
      setLoading(false)
      if (!res?.ok) { setError('Could not load the plugin registry.'); return }
      setData({ kinds: res.kinds ?? [], plugins: res.plugins ?? [] })
    })
    return () => { alive = false }
  }, [])

  const groups = groupByKind(data)
  const activeCount = (data?.plugins ?? []).filter(p => p.active).length

  return (
    <>
      <div className="cmd-backdrop" onPointerDown={onClose} />
      <div className="plugins" role="dialog" aria-modal="true" onPointerDown={e => e.stopPropagation()}>
        <header className="plugins-head">
          <div>
            <div className="plugins-title">Plugins</div>
            <div className="plugins-sub">
              Capability providers · built-in, suggested &amp; repo-local · activation reflects the watched repo
            </div>
          </div>
          <button className="plugins-x" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="plugins-body">
          {loading && <div className="plugins-empty">Loading providers…</div>}
          {!loading && error && <div className="plugins-empty warn">{error}</div>}
          {!loading && !error && groups.length === 0 && (
            <div className="plugins-empty">No providers registered.</div>
          )}
          {!loading && !error && groups.map(g => (
            <section key={g.kind} className="plugins-group">
              <div className="plugins-group-label">{KIND_LABELS[g.kind] ?? g.kind}</div>
              {g.items.map(p => <PluginCard key={p.id} p={p} />)}
            </section>
          ))}
        </div>

        <footer className="plugins-foot">
          <span className="plugins-note">
            {activeCount} active for this repo · read-only — metadata only, nothing installed
          </span>
        </footer>
      </div>
    </>
  )
}

function PluginCard({ p }) {
  const tone = STATUS_TONE[p.status] ?? 'muted'
  // Active means "providing capabilities for this repo now" (built-in + matched).
  // A suggested pack is `detected` but never active (nothing is installed in Lite).
  const stateLabel = p.active ? '● active' : (p.detected ? 'detected' : 'inactive')
  const stateTone  = p.active ? 'on'       : (p.detected ? 'det'      : 'off')
  return (
    <div className={`plugin-card${p.active ? ' is-active' : ''}`}>
      <div className="plugin-card-top">
        <span className="plugin-name">{p.displayName}</span>
        {p.source === 'local' && (
          <span className="plugin-source" title="Declared in .openfde/plugins">local</span>
        )}
        <span className={`plugin-status ${tone}`}>{p.status}</span>
        <span className={`plugin-state ${stateTone}`}>{stateLabel}</span>
      </div>
      {p.activatesOn && (
        <div className="plugin-activates">
          <span className="plugin-activates-lbl">Activates on</span> {p.activatesOn}
        </div>
      )}
      {Array.isArray(p.provides) && p.provides.length > 0 && (
        <div className="plugin-provides">
          {p.provides.map(c => <span key={c} className="plugin-chip">{c}</span>)}
        </div>
      )}
      {p.id === 'webxr' && p.detected && <WebxrDetails />}
    </div>
  )
}

// WebXR domain-pack details (v1-E): a compact, lazy-loaded affordance on the detected WebXR card.
// Architecture hints ONLY — frameworks / assets / entrypoints / markers — with the honest boundary
// from the backend's `warnings` ("no test lens installed"). No install/run action.
function WebxrDetails() {
  const [open, setOpen]       = useState(false)
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(false)

  function toggle() {
    const next = !open
    setOpen(next)
    if (next && !data && !loading) {
      setLoading(true)
      getWebxrSummary().then(r => { setLoading(false); if (r?.ok) setData(r) })
    }
  }

  return (
    <div className="plugin-webxr">
      <button className="plugin-webxr-toggle" onClick={toggle} aria-expanded={open}>
        {open ? '▾' : '▸'} WebXR details
      </button>
      {open && (
        <div className="plugin-webxr-body">
          {loading && <div className="plugin-webxr-loading">Scanning the repo…</div>}
          {data && (
            <>
              <WebxrRow label="Frameworks" items={data.frameworks} />
              <WebxrRow label="Entrypoints" items={data.entrypoints} mono />
              <WebxrRow label="Assets" items={data.assets} mono />
              <WebxrRow label="Markers" items={data.markers} mono />
              {(data.warnings || []).map(w => (
                <div key={w} className="plugin-webxr-warn">{w}</div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function WebxrRow({ label, items, mono = false }) {
  if (!items?.length) return null
  const shown = items.slice(0, 8)
  return (
    <div className="plugin-webxr-row">
      <span className="plugin-webxr-lbl">{label}</span>
      <span className="plugin-webxr-vals">
        {shown.map(x => <span key={x} className={`plugin-chip${mono ? ' mono' : ''}`}>{x}</span>)}
        {items.length > shown.length && (
          <span className="plugin-webxr-more">+{items.length - shown.length}</span>
        )}
      </span>
    </div>
  )
}

/* ─── Helpers ─────────────────────────────────────────────────────── */

const KIND_LABELS = {
  language_pack:  'Language packs',
  domain_pack:    'Domain packs',
  verify_adapter: 'Verify adapters',
  agent_provider: 'Agent providers',
  layout_engine:  'Layout engines',
  integration:    'Integrations',
}

// Status → status-dot/pill tone (reuses the app's ok/warn/muted palette).
const STATUS_TONE = {
  builtin:   'ok',
  available: 'ok',
  suggested: 'warn',
  missing:   'muted',
  disabled:  'muted',
}

// Group providers by kind, following the registry's kind order, then any extras.
function groupByKind(data) {
  if (!data) return []
  const order = data.kinds?.length ? data.kinds : Object.keys(KIND_LABELS)
  const byKind = {}
  for (const p of data.plugins ?? []) (byKind[p.kind] ??= []).push(p)
  const groups = order.filter(k => byKind[k]?.length).map(k => ({ kind: k, items: byKind[k] }))
  for (const k of Object.keys(byKind)) {
    if (!order.includes(k)) groups.push({ kind: k, items: byKind[k] })
  }
  return groups
}
