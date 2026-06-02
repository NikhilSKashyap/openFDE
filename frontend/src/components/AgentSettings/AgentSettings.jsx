import { useState, useEffect, useRef } from 'react'
import { putAgentSettings, checkAgentSettings } from '../../api/backend'

/**
 * Agent Settings — assign a provider to each role (Architect / Senior Dev /
 * Verifier) before native agent conversations exist (Step 21).
 *
 * Config only: no LLM calls, no execution. Existing API keys are masked; a key
 * is sent to the backend only when the user types a new one. The backend keeps
 * the stored key when the field is left blank.
 *
 * @param {object}   props
 * @param {object}   props.settings  - sanitized public settings (no raw keys)
 * @param {object}   props.options   - {roles, modes, providers}
 * @param {Function} props.onClose
 * @param {Function} props.onSettingsChange - called with new public settings after save
 */
export default function AgentSettings({ settings, options, onClose, onSettingsChange }) {
  const roles     = options?.roles     ?? []
  const modes     = options?.modes     ?? []
  const providers = options?.providers ?? []
  const providerById = Object.fromEntries(providers.map(p => [p.id, p]))

  const [activeRole, setActiveRole] = useState(roles[0]?.id ?? 'architect')
  // Draft holds editable, secret-free fields per role; keyInput holds a freshly
  // typed key (empty = keep stored). clearKey marks an explicit wipe.
  const [draft, setDraft]       = useState(() => seedDraft(settings, roles))
  const [keyInput, setKeyInput] = useState({})
  const [clearKey, setClearKey] = useState({})
  const [checks, setChecks]     = useState({})
  const [busy, setBusy]         = useState(false)
  const [flash, setFlash]       = useState('')
  const flashTimer = useRef(null)

  useEffect(() => () => clearTimeout(flashTimer.current), [])

  function showFlash(msg) {
    setFlash(msg)
    clearTimeout(flashTimer.current)
    flashTimer.current = setTimeout(() => setFlash(''), 2200)
  }

  const rd = draft[activeRole] ?? {}
  const sd = settings?.[activeRole] ?? {}
  const provMeta = providerById[rd.provider] ?? {}
  const showBaseUrl = !!provMeta.supportsBaseUrl
  const isApi = rd.mode === 'api'
  const typedKey = keyInput[activeRole] ?? ''
  const wantClear = !!clearKey[activeRole]

  function patch(field, value) {
    setDraft(d => ({ ...d, [activeRole]: { ...d[activeRole], [field]: value } }))
    setChecks(c => ({ ...c, [activeRole]: null }))
  }

  // Build the config payload for the active role, only including a key when the
  // user typed one or explicitly cleared it.
  function rolePayload() {
    const cfg = {
      mode: rd.mode, provider: rd.provider,
      model: rd.model || '', baseUrl: rd.baseUrl || '',
      enabled: rd.enabled !== false,
    }
    if (typedKey) cfg.apiKey = typedKey
    else if (wantClear) cfg.clearApiKey = true
    return cfg
  }

  async function runCheck() {
    setBusy(true)
    const res = await checkAgentSettings({ role: activeRole, config: rolePayload() })
    setBusy(false)
    if (!res) { showFlash('Backend unavailable'); return }
    setChecks(c => ({ ...c, [activeRole]: res.roles?.[activeRole] ?? null }))
  }

  async function save() {
    setBusy(true)
    const res = await putAgentSettings({ [activeRole]: rolePayload() })
    setBusy(false)
    if (!res?.settings) { showFlash('Save failed'); return }
    onSettingsChange?.(res.settings)
    setKeyInput(k => ({ ...k, [activeRole]: '' }))
    setClearKey(c => ({ ...c, [activeRole]: false }))
    showFlash('Saved')
  }

  // Derived static status (independent of a Check run).
  const check = checks[activeRole]
  const status = deriveStatus(rd, sd, typedKey, wantClear, provMeta, check)

  return (
    <>
      <div className="cmd-backdrop" onPointerDown={onClose} />
      <div className="agentset" role="dialog" aria-modal="true" onPointerDown={e => e.stopPropagation()}>
        <header className="agentset-head">
          <div>
            <div className="agentset-title">Agent Settings</div>
            <div className="agentset-sub">Assign a provider to each role · config only, no calls yet</div>
          </div>
          <button className="agentset-x" onClick={onClose} aria-label="Close">✕</button>
        </header>

        {/* Role segmented control with live status dots */}
        <div className="agentset-roles">
          {roles.map(r => {
            const st = deriveStatus(draft[r.id] ?? {}, settings?.[r.id] ?? {}, keyInput[r.id] ?? '', !!clearKey[r.id], providerById[(draft[r.id] ?? {}).provider] ?? {}, checks[r.id])
            return (
              <button
                key={r.id}
                className={`agentset-role${activeRole === r.id ? ' active' : ''}`}
                onClick={() => setActiveRole(r.id)}
              >
                <span className={`agentset-dot ${st.tone}`} />
                {r.label}
              </button>
            )
          })}
        </div>

        <div className="agentset-body">
          {/* Enable + mode row */}
          <div className="agentset-row">
            <label className="agentset-toggle">
              <input
                type="checkbox"
                checked={rd.enabled !== false}
                onChange={e => patch('enabled', e.target.checked)}
              />
              <span>Enabled</span>
            </label>
            <div className="agentset-field">
              <span className="agentset-lbl">Mode</span>
              <select value={rd.mode} onChange={e => patch('mode', e.target.value)}>
                {modes.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </div>
          </div>

          {/* Provider */}
          <div className="agentset-field full">
            <span className="agentset-lbl">Provider</span>
            <select value={rd.provider} onChange={e => patch('provider', e.target.value)}>
              {providers.map(p => (
                <option key={p.id} value={p.id}>
                  {p.label}{p.available ? '' : ' — unavailable'}
                </option>
              ))}
            </select>
          </div>

          {!provMeta.available && (
            <div className="agentset-note warn">
              This provider is a local bridge placeholder — visible but not available yet.
            </div>
          )}

          {/* Keyless offline demo provider — no model/key needed */}
          {isApi && provMeta.available && provMeta.keyless && (
            <div className="agentset-note">
              Echo is an offline demo: no key or model needed. With the
              “OpenFDE native agent” backend, Execute makes the echo agent append a
              demo marker to the first editable file, then commits it through the
              normal gated path — so you can watch the whole loop with no API call.
            </div>
          )}

          {/* API fields (only meaningful in api mode) */}
          {isApi && provMeta.available && !provMeta.keyless && (
            <>
              <div className="agentset-field full">
                <span className="agentset-lbl">Model</span>
                <input
                  type="text"
                  placeholder="e.g. gpt-4o, claude-sonnet-4, llama3.1"
                  value={rd.model || ''}
                  onChange={e => patch('model', e.target.value)}
                />
              </div>
              {showBaseUrl && (
                <div className="agentset-field full">
                  <span className="agentset-lbl">Base URL <span className="agentset-opt">optional</span></span>
                  <input
                    type="text"
                    placeholder="https://api.example.com/v1"
                    value={rd.baseUrl || ''}
                    onChange={e => patch('baseUrl', e.target.value)}
                  />
                </div>
              )}
              <div className="agentset-field full">
                <span className="agentset-lbl">
                  API key
                  {sd.hasApiKey && !wantClear && (
                    <span className="agentset-opt"> · stored {sd.maskedApiKey}</span>
                  )}
                </span>
                <input
                  type="password"
                  autoComplete="new-password"
                  placeholder={sd.hasApiKey ? 'Stored — type to replace' : 'Enter API key'}
                  value={typedKey}
                  onChange={e => {
                    setKeyInput(k => ({ ...k, [activeRole]: e.target.value }))
                    setClearKey(c => ({ ...c, [activeRole]: false }))
                    setChecks(c => ({ ...c, [activeRole]: null }))
                  }}
                />
                {sd.hasApiKey && (
                  <label className="agentset-clear">
                    <input
                      type="checkbox"
                      checked={wantClear}
                      onChange={e => {
                        setClearKey(c => ({ ...c, [activeRole]: e.target.checked }))
                        if (e.target.checked) setKeyInput(k => ({ ...k, [activeRole]: '' }))
                      }}
                    />
                    Remove stored key
                  </label>
                )}
              </div>
            </>
          )}

          {/* Status line */}
          <div className={`agentset-status ${status.tone}`}>
            <span className={`agentset-dot ${status.tone}`} />
            {status.message}
          </div>
        </div>

        <footer className="agentset-foot">
          <span className="agentset-flash">{flash}</span>
          <div className="agentset-actions">
            <button className="agentset-btn" disabled={busy} onClick={runCheck}>Check</button>
            <button className="agentset-btn primary" disabled={busy} onClick={save}>Save</button>
          </div>
        </footer>
      </div>
    </>
  )
}

/* ─── Helpers ─────────────────────────────────────────────────────── */

function seedDraft(settings, roles) {
  const out = {}
  for (const r of roles) {
    const s = settings?.[r.id] ?? {}
    out[r.id] = {
      mode: s.mode ?? 'workflow',
      provider: s.provider ?? 'claude-code-workflow',
      model: s.model ?? '',
      baseUrl: s.baseUrl ?? '',
      enabled: s.enabled !== false,
    }
  }
  return out
}

// Compute a status pill without requiring a server Check. A server Check result,
// when present, takes precedence (it is authoritative for shape validity).
function deriveStatus(rd, sd, typedKey, wantClear, provMeta, check) {
  if (check) {
    const tone = check.ok ? 'ok' : (check.supported === false ? 'muted' : 'warn')
    return { tone, message: check.message }
  }
  if (provMeta && provMeta.available === false) {
    return { tone: 'muted', message: 'Local bridge — unavailable for now.' }
  }
  if (rd.mode === 'disabled' || rd.enabled === false) {
    return { tone: 'muted', message: 'Disabled.' }
  }
  if (rd.mode === 'workflow') {
    return rd.provider === 'claude-code-workflow'
      ? { tone: 'ok', message: 'Configured — Claude Code workflow.' }
      : { tone: 'warn', message: 'Workflow mode needs the claude-code-workflow provider.' }
  }
  if (rd.mode === 'api') {
    if (provMeta && provMeta.keyless) {
      return { tone: 'ok', message: 'Configured — echo (offline demo, no key needed).' }
    }
    if (!rd.model) return { tone: 'warn', message: 'Missing model.' }
    const hasKey = (!!typedKey) || (sd.hasApiKey && !wantClear)
    if (!hasKey) return { tone: 'warn', message: 'Missing API key.' }
    return { tone: 'ok', message: 'Configured.' }
  }
  return { tone: 'muted', message: '' }
}
