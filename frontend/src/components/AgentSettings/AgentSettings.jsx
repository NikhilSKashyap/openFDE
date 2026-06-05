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
 * @param {object}   props.options   - {roles, providers}
 * @param {Function} props.onClose
 * @param {Function} props.onSettingsChange - called with new public settings after save
 */
export default function AgentSettings({ settings, options, onClose, onSettingsChange }) {
  const roles     = options?.roles     ?? []
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
  // Transport is determined entirely by the provider (no separate "mode" axis).
  // API providers (kind 'api') show model/key/base-URL; Echo is a keyless 'api'.
  const isApi = provMeta.kind === 'api'
  // Keyless local-CLI providers (Codex Local, Claude Code local CLI) drive a local
  // coding app on its own login — no API key, optional model override. textOnly
  // marks the read-only text role (Codex) vs the editing one (Claude Code).
  const isLocalCli = provMeta.kind === 'local' && provMeta.available !== false
  const isTextOnly = isLocalCli && !!provMeta.textOnly
  // Quick-pick model suggestions per provider family (devs can still type any id).
  const modelOptions = MODEL_SUGGESTIONS[rd.provider] ?? []
  const modelDoc = MODEL_DOCS[rd.provider]
  const modelSuggest = (
    <>
      {modelOptions.length > 0 && (
        <datalist id="agentset-model-suggestions">
          {modelOptions.map(m => <option key={m} value={m} />)}
        </datalist>
      )}
      {modelDoc && (
        <a className="agentset-doclink" href={modelDoc} target="_blank" rel="noreferrer">
          Browse all models ↗
        </a>
      )}
    </>
  )
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
      provider: rd.provider,
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
          {/* Enable row (transport comes from the provider — no mode dropdown) */}
          <div className="agentset-row">
            <label className="agentset-toggle">
              <input
                type="checkbox"
                checked={rd.enabled !== false}
                onChange={e => patch('enabled', e.target.checked)}
              />
              <span>Enabled</span>
            </label>
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

          {/* Keyless local-CLI providers (Codex Local, Claude Code) — no key, optional model */}
          {isLocalCli && (
            <>
              <div className="agentset-note">
                {isTextOnly ? (
                  <>
                    Drives your local <strong>Codex CLI</strong> as a text role
                    (Architect / Verifier). No API key — it uses your Codex login and runs
                    read-only, so it can review the repo and the diff but never edits files.
                  </>
                ) : (
                  <>
                    Drives your local <strong>Claude Code CLI</strong> headlessly on your
                    Claude login. No API key. As Senior Dev it edits in-scope files; as
                    Architect / Verifier it runs as a text role.
                  </>
                )}
              </div>
              <div className="agentset-field full">
                <span className="agentset-lbl">Model <span className="agentset-opt">optional</span></span>
                <input
                  type="text"
                  list={modelOptions.length ? 'agentset-model-suggestions' : undefined}
                  placeholder={isTextOnly ? 'Codex default (e.g. gpt-5.3-codex)'
                                          : 'Claude Code default (e.g. sonnet, opus, haiku)'}
                  value={rd.model || ''}
                  onChange={e => patch('model', e.target.value)}
                />
                {modelSuggest}
              </div>
            </>
          )}

          {/* Keyless offline demo provider — no model/key needed */}
          {isApi && provMeta.available && provMeta.keyless && !isLocalCli && (
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
                  list={modelOptions.length ? 'agentset-model-suggestions' : undefined}
                  placeholder="e.g. claude-sonnet-4-6, gpt-5.4, llama3.1"
                  value={rd.model || ''}
                  onChange={e => patch('model', e.target.value)}
                />
                {modelSuggest}
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

// Quick-pick model ids per provider family. These are suggestions in a datalist
// (the input stays free-text — devs can type any id). Keep in sync with the docs
// linked in MODEL_DOCS. Claude Code local CLI takes short aliases (sonnet/opus/
// haiku); the Anthropic API takes full pinned ids; Codex/OpenAI take gpt-5.x ids.
const MODEL_SUGGESTIONS = {
  'codex-local': ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.3-codex', 'gpt-5.3-codex-spark', 'gpt-5.2'],
  'claude-code-local': ['sonnet', 'opus', 'haiku'],
  'anthropic': ['claude-opus-4-8', 'claude-sonnet-4-6', 'claude-haiku-4-5',
                'claude-opus-4-7', 'claude-sonnet-4-5', 'claude-opus-4-5'],
  'openai-compatible': ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.3-codex', 'gpt-5.2'],
}

// Official "browse all models" reference per provider family.
const CLAUDE_DOC = 'https://platform.claude.com/docs/en/about-claude/models/overview'
const OPENAI_DOC = 'https://developers.openai.com/codex/models'
const MODEL_DOCS = {
  'codex-local': OPENAI_DOC,
  'openai-compatible': OPENAI_DOC,
  'claude-code-local': CLAUDE_DOC,
  'anthropic': CLAUDE_DOC,
}

function seedDraft(settings, roles) {
  const out = {}
  for (const r of roles) {
    const s = settings?.[r.id] ?? {}
    out[r.id] = {
      provider: s.provider ?? 'claude-code-local',
      model: s.model ?? '',
      baseUrl: s.baseUrl ?? '',
      enabled: s.enabled !== false,
    }
  }
  return out
}

// Compute a status pill without requiring a server Check. A server Check result,
// when present, takes precedence (it is authoritative for shape validity).
// Validity is a pure function of the provider — there is no separate "mode" axis.
function deriveStatus(rd, sd, typedKey, wantClear, provMeta, check) {
  if (check) {
    const tone = check.ok ? 'ok' : (check.supported === false ? 'muted' : 'warn')
    return { tone, message: check.message }
  }
  if (provMeta && provMeta.available === false) {
    return { tone: 'muted', message: 'Unavailable for now.' }
  }
  if (rd.enabled === false) {
    return { tone: 'muted', message: 'Disabled.' }
  }
  if (provMeta && provMeta.kind === 'local') {
    return {
      tone: 'ok',
      message: provMeta.textOnly
        ? 'Configured — Codex (local CLI, no key needed).'
        : 'Configured — Claude Code (local CLI, no key needed).',
    }
  }
  // Hosted API providers (Echo is keyless): need a model + key unless keyless.
  if (provMeta && provMeta.keyless) {
    return { tone: 'ok', message: 'Configured — Echo (offline demo, no key needed).' }
  }
  if (!rd.model) return { tone: 'warn', message: 'Missing model.' }
  const hasKey = (!!typedKey) || (sd.hasApiKey && !wantClear)
  if (!hasKey) return { tone: 'warn', message: 'Missing API key.' }
  return { tone: 'ok', message: 'Configured.' }
}
