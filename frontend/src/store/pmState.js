import { useReducer } from 'react'

function makeId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
}

// A title that is operational/meta and should never be an OpenPM card's primary text —
// mirrors the backend's is_bad_title semantics, kept tiny + focused. Used to (a) pick a
// new card's title and (b) migrate older cards that captured raw commit text.
const _NOISY_TITLE = new Set([
  'yes', 'ya', 'yeah', 'ok', 'okay', 'k', 'sure', 'no', 'nope', 'done', 'here', 'prompt',
  'change', 'update', 'fix', 'landed change', "here's the cc prompt", 'here is the cc prompt',
  // Code-fence language tokens (a "```text" prompt opener is not a title) — mirrors
  // the backend's is_bad_title fence rule.
  'text', 'bash', 'python', 'json', 'diff', 'shell', 'sh', 'console', 'code',
  'markdown', 'md', 'yaml', 'html', 'css', 'js', 'jsx', 'ts', 'tsx',
])
function isNoisyTitle(title) {
  // Normalize smart quotes → ASCII first so a curly apostrophe ("Here's") matches like a
  // straight one, then strip wrapping quotes/backticks and an `openfde:` prefix.
  const s = (title || '')
    .replace(/[‘’‛ʼ′]/g, "'").replace(/[“”„″]/g, '"')
    .replace(/^openfde:\s*/i, '').trim().replace(/^[`"']+|[`"']+$/g, '').trim()
  if (!s) return true
  const low = s.toLowerCase().replace(/[.!?,:]+$/, '')
  if (_NOISY_TITLE.has(low)) return true
  if (/^(here'?s|here is|you are|you'?re|read the|read first|start with|important|restart the|implementing the|first,? read|the (cc|claude|sr dev|senior dev) prompt)\b/.test(low)) return true
  if (low.includes('openfde owns version control')) return true
  if (/^(curl|git|cd|npm|npx|node|pkill|kill|nohup|chmod|python3?|pip3?|grep|rg|sed|awk|rm|cp|mv|scp|ssh|docker|make|pytest|eslint|vite|ls|cat|tail|head|find|touch|open|export|sudo|brew)\b\s+\S*[-/]/.test(s)) return true
  const toks = s.split(/\s+/).filter(Boolean)        // a bare file-list title
  if (toks.length && toks.every(tok => /^[\w.@~+/-]+$/.test(tok) && (tok.includes('/') || tok.includes('.')))) return true
  return false
}
// The clean primary title for a commit card: prefer the backend's displayTitle / the prompt
// title, else the de-`openfde:`-ed commit summary, else a neutral label. Never noisy text.
// Exported so the episode detail card (ConceptPanel) renders commit rows with the SAME clean
// fallback as OpenPM — one frontend source of truth mirroring backend `commit_display`.
export function cardTitleFor(c) {
  const dt = (c.displayTitle || c.promptTitle || '').trim()
  if (dt && !isNoisyTitle(dt)) return dt
  const cleaned = (c.summary || '').replace(/^openfde:\s*/i, '').trim()
  if (cleaned && !isNoisyTitle(cleaned)) return cleaned
  return dt || 'Landed change'
}

function pmReducer(state, action) {
  switch (action.type) {
    case 'CREATE_TASK': {
      const task = {
        id: makeId(),
        title: action.title || 'New task',
        description: action.description || '',
        linkedBoxIds: action.linkedBoxIds || [],
        column: action.column || 'todo',
        verificationStatus: action.verificationStatus || 'pending',
        // Story binding (optional): the prompt episode + landed commit this card
        // represents, so OpenPM mirrors the same prompt→commit story as the rail.
        episodeId: action.episodeId || null,
        commitSha: action.commitSha || null,
        source: action.source || 'manual',
        files: action.files || [],
        promptLabel: action.promptLabel || '',
      }
      return [...state, task]
    }
    // Mirror landed prompt→commits into OpenPM as Done cards, grouped/labeled by their
    // prompt. Card text uses the CLEANED episode display (never raw commit chatter like
    // "Here's the CC prompt"). Idempotent + self-healing: new commits are added; existing
    // cards whose title is noisy or whose tag/summary is missing are MIGRATED in place from
    // the cleaned metadata; when nothing changes we return the SAME array reference so React
    // never re-renders or re-persists. action.commits: [{commitSha, shortSha, summary,
    // displayTitle, displaySummary, files, episodeId, episodeTag, promptTitle, sequence}].
    case 'SYNC_EPISODE_COMMITS': {
      const byCommit = new Map()
      state.forEach((t, i) => { if (t.commitSha) byCommit.set(t.commitSha, i) })
      let next = null
      const clone = () => (next || (next = state.slice()))
      const additions = []
      for (const c of action.commits || []) {
        if (!c.commitSha) continue
        const title = cardTitleFor(c)
        const desc = c.displaySummary || ''
        if (!byCommit.has(c.commitSha)) {
          additions.push({
            id: makeId(), title, description: desc, linkedBoxIds: [], column: 'done',
            verificationStatus: 'passed',   // it landed — verified by Review then Land
            episodeId: c.episodeId || null, episodeTag: c.episodeTag || '',
            promptTitle: c.promptTitle || '', sequence: c.sequence || 0,
            commitSha: c.commitSha, shortSha: c.shortSha || (c.commitSha || '').slice(0, 7),
            source: 'openfde-episode', files: c.files || [], promptLabel: c.promptLabel || '',
            verify: c.verify || null,       // Verify Gate receipts (lite) → evidence badges
            pr: c.pr || null,               // Land-as-PR metadata (lite) → PR #N badge
            prReady: !!c.prReady,           // deterministic readiness → "ready for PR" badge
          })
          continue
        }
        // Migrate an existing card from cleaned metadata (heal older noisy/partial cards).
        const idx = byCommit.get(c.commitSha)
        const t = state[idx]
        const newTitle = (isNoisyTitle(t.title) && title) ? title : t.title
        const newDesc = (!t.description && desc) ? desc : t.description
        const newTag = t.episodeTag || c.episodeTag || ''
        const newPT = t.promptTitle || c.promptTitle || ''
        const newSeq = t.sequence || c.sequence || 0
        const newVerify = c.verify || t.verify || null     // receipts can arrive late
        const newPr = c.pr || t.pr || null                 // …and so can the PR
        const newReady = c.pr ? false : !!c.prReady        // readiness is live state, not sticky
        if (newTitle !== t.title || newDesc !== t.description || newTag !== t.episodeTag
            || newPT !== t.promptTitle || newSeq !== t.sequence
            || JSON.stringify(newVerify) !== JSON.stringify(t.verify || null)
            || JSON.stringify(newPr) !== JSON.stringify(t.pr || null)
            || newReady !== !!t.prReady) {
          clone()[idx] = { ...t, title: newTitle, description: newDesc,
                           episodeTag: newTag, promptTitle: newPT, sequence: newSeq,
                           verify: newVerify, pr: newPr, prReady: newReady }
        }
      }
      if (additions.length) clone().push(...additions)
      return next || state          // same reference when nothing changed → no churn
    }
    // Sketch-First v2: mirror each intent step into one OpenPM card, grouped by the
    // sketch tag and linked to its intent box + generated files. Idempotent by
    // (episode|run + boxId) so a re-run advances the SAME cards (doing → testing →
    // done) instead of duplicating. Column reflects real state — never fake "done":
    //   committed → done · files but not yet landed → testing · else → doing.
    case 'SYNC_INTENT_STEPS': {
      const key = action.episodeId || action.runId || ''
      const tag = action.tag || ''
      const column = action.committed ? 'done' : (action.awaitingReview ? 'testing' : 'doing')
      const vstatus = action.committed ? 'passed' : 'pending'
      const byIdent = new Map()
      state.forEach((t, i) => { if (t.intentKey) byIdent.set(t.intentKey, i) })
      let next = null
      const clone = () => (next || (next = state.slice()))
      const additions = []
      for (const s of action.steps || []) {
        const ident = `${key}:${s.boxId}`
        const fields = {
          title: s.title || 'intent step', files: s.files || [], linkedBoxIds: [s.boxId],
          column, verificationStatus: vstatus,
          episodeId: action.episodeId || null, commitSha: action.commitSha || null,
          episodeTag: tag, promptTitle: tag, promptLabel: tag,
          source: 'intent-graph', intentKey: ident,
        }
        if (byIdent.has(ident)) {
          const idx = byIdent.get(ident)
          clone()[idx] = { ...state[idx], ...fields }
        } else {
          additions.push({ id: makeId(), description: '', ...fields })
        }
      }
      if (additions.length) clone().push(...additions)
      return next || state
    }
    case 'UPDATE_TASK': {
      return state.map(t => t.id === action.id ? { ...t, ...action.fields } : t)
    }
    case 'MOVE_TASK': {
      return state.map(t => t.id === action.id ? { ...t, column: action.column } : t)
    }
    case 'DELETE_TASK': {
      return state.filter(t => t.id !== action.id)
    }
    case 'LINK_BOX': {
      return state.map(t => {
        if (t.id !== action.taskId) return t
        if (t.linkedBoxIds.includes(action.boxId)) return t
        return { ...t, linkedBoxIds: [...t.linkedBoxIds, action.boxId] }
      })
    }
    case 'UNLINK_BOX': {
      return state.map(t =>
        t.id !== action.taskId ? t
          : { ...t, linkedBoxIds: t.linkedBoxIds.filter(id => id !== action.boxId) }
      )
    }
    // Hydrate from backend — replaces the full task list
    case 'HYDRATE_TASKS': {
      return action.tasks ?? []
    }
    default:
      return state
  }
}

// Boards start EMPTY — never seeded with OpenFDE's own dev cards. (Observed live:
// a fresh repo's board showed OpenFDE's bootstrap cards, then the debounced PUT
// persisted them into that repo's tasks.json.) Cards come from the backend
// (HYDRATE_TASKS), episode sync, or the user.
export function usePMState() {
  return useReducer(pmReducer, [])
}
