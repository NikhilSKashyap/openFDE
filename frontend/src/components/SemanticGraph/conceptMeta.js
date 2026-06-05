/**
 * conceptMeta — deterministic, human-readable knowledge about tether concepts
 * and the files they live in. Exact-id lookup first, then pattern inference.
 * Easy to extend: add a row to CONCEPT_MEANINGS or FILE_ROLES.
 */

// Known concept ids → plain-English meaning.
const CONCEPT_MEANINGS = {
  'codex-local': 'Local Codex CLI provider used for Architect/Verifier roles.',
  'claude-code-local': 'Local Claude Code CLI provider used for Senior Dev execution.',
  'claude-code-workflow': 'Claude Code workflow execution backend id.',
  'openai-compatible': 'Hosted OpenAI-compatible provider option.',
  'anthropic': 'Hosted Anthropic API provider option.',
  'openrouter': 'Hosted OpenRouter provider option.',
  'ollama': 'Local Ollama provider option.',
  'echo': 'Offline echo provider for deterministic demo runs.',
  'openfde-agent': 'OpenFDE native single-agent execution backend.',
  'openfde-council': 'OpenFDE Architect → Senior Dev → Verifier council backend.',
  'openfde-native': 'OpenFDE built-in execution path.',
  '/ws': 'WebSocket route for live frontend/backend events.',
  'agent_progress': 'Live event emitted while an agent reads or writes files.',
  'agent_plan': 'Live event describing the files an agent plans to touch.',
}

// Basenames / patterns → the file's role in the system.
const FILE_ROLES = [
  [/AgentSettings\.jsx$/, 'settings UI'],
  [/RightPanel\.jsx$/, 'status / work display'],
  [/WorkPanel\.jsx$/, 'work display'],
  [/CommandPalette\.jsx$/, 'command palette'],
  [/Timeline\.jsx$/, 'commit timeline'],
  [/ConceptPanel\.jsx$/, 'concept panel'],
  [/App\.jsx$/, 'app orchestration / live events'],
  [/agent_settings\.py$/, 'settings schema / normalization'],
  [/server\.py$/, 'backend routing / API'],
  [/persistence\.py$/, 'saved state / logging'],
  [/semantic_graph\.py$/, 'graph / tether extraction'],
  [/claude_code_runner\.py$/, 'Claude Code local runner'],
  [/codex_local_runner\.py$/, 'Codex local runner'],
  [/execution\.py$/, 'execution backend registry'],
  [/agent_runner\.py$/, 'native agent runner'],
]

export function isFrontend(f) {
  return /\.(jsx?|tsx?|css)$/.test(f) || (f || '').includes('frontend/')
}

/** Coarse concept category: route | event | backend | provider | generic. */
export function conceptType(id = '') {
  if (id.startsWith('/')) return 'route'
  if (/^agent_(progress|plan)$|_event$/.test(id)) return 'event'
  if (id.startsWith('openfde-')) return 'backend'
  if (/-local$|-workflow$|-compatible$|^anthropic$|^echo$|^ollama$|^openrouter$/.test(id)) return 'provider'
  return 'generic'
}

/** Plain-English meaning for a concept id (known map, else type-based fallback). */
export function conceptMeaning(id = '') {
  if (CONCEPT_MEANINGS[id]) return CONCEPT_MEANINGS[id]
  switch (conceptType(id)) {
    case 'route': return 'Route shared between the backend and its frontend caller.'
    case 'event': return 'Live event id connecting a backend broadcast to a frontend handler.'
    case 'backend': return 'OpenFDE execution backend id.'
    case 'provider': return 'Agent provider option (UI option + backend routing).'
    default: return 'Identifier shared across backend and frontend files.'
  }
}

/** Short role label for a file path, or null when unknown. */
export function fileRole(path = '') {
  for (const [re, role] of FILE_ROLES) if (re.test(path)) return role
  return null
}

/** Concept-specific "why check" text (never the vague "this id"). */
export function whyCheck(id = '', files = []) {
  switch (conceptType(id)) {
    case 'provider':
      return `Provider ids connect the UI option, backend settings schema, runtime `
        + `routing, and status display. If only one side changes, the UI can save one `
        + `value while the server routes another.`
    case 'backend':
      return `Execution backend ids decide which run path OpenFDE uses. Partial updates `
        + `can make Execute display one backend while invoking another.`
    case 'route':
    case 'event':
      return `Live event ids connect server broadcasts to frontend handlers. Partial `
        + `updates can make the backend emit an event the UI no longer handles.`
    default: {
      const fe = files.filter(isFrontend).length
      const be = files.length - fe
      const span = be && fe ? 'backend and frontend' : be ? 'backend' : 'frontend'
      return `${id} is shared across ${files.length} files spanning ${span}. `
        + `Partial updates can leave layers disagreeing.`
    }
  }
}

/** Three short next-action suggestions for a flagged concept. */
export function nextActions() {
  return [
    'Ask Concept to verify whether all related files still agree.',
    'Open related files and confirm the same value is used.',
    'If intentional, save a Concept Card explaining the distinction.',
  ]
}
