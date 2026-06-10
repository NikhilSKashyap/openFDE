import { useReducer } from 'react'

function makeId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
}

// Demo tasks that reflect the real OpenFDE build log.
// linkedBoxIds are empty at seed time — IDs are dynamic; user links boxes
// via [+ Add] with a box selected, or by task Inspector in the right panel.
function makeDemoTasks() {
  return [
    { id: makeId(), title: 'Vite + React scaffold',        description: 'Initial project setup with CSS design system and 3-panel IDE layout.',           linkedBoxIds: [], column: 'done',    verificationStatus: 'passed'  },
    { id: makeId(), title: 'Whiteboard canvas',            description: 'SVG canvas: box creation, drag, resize, in-place editing, rubber-band select.',   linkedBoxIds: [], column: 'done',    verificationStatus: 'passed'  },
    { id: makeId(), title: 'Connection ports + arrows',    description: 'Port hover, bezier arrow drawing, arrowhead markers, pending arrow preview.',      linkedBoxIds: [], column: 'done',    verificationStatus: 'passed'  },
    { id: makeId(), title: 'Right panel + self-map',       description: 'Inspector, Agent, Plan modes wired to canvas selection context.',                  linkedBoxIds: [], column: 'done',    verificationStatus: 'passed'  },
    { id: makeId(), title: 'Arrow labels + edge inspector',description: 'Click-to-select arrows, bezier midpoint label pills, UPDATE_ARROW action.',        linkedBoxIds: [], column: 'done',    verificationStatus: 'passed'  },
    { id: makeId(), title: 'PLAN.md live preview',         description: 'generatePlanPreview — Why/What/Dataflow/Permissions/Outcome/Open Questions.',      linkedBoxIds: [], column: 'done',    verificationStatus: 'passed'  },
    { id: makeId(), title: 'Command palette ⌘K',           description: 'Global palette: commands, box search, ranked label-first filtering.',              linkedBoxIds: [], column: 'testing', verificationStatus: 'passed'  },
    { id: makeId(), title: 'OpenPM board',                 description: 'Real Kanban wired to canvas boxes: drag, verification gates, design events.',      linkedBoxIds: [], column: 'doing',   verificationStatus: 'pending' },
    { id: makeId(), title: 'Timeline + event log',         description: 'Design and code event playback from in-memory events.',                            linkedBoxIds: [], column: 'todo',    verificationStatus: 'pending' },
    { id: makeId(), title: 'Backend CLI',                  description: 'openfde watch <path> → aiohttp server + WebSocket + MCP server.',                  linkedBoxIds: [], column: 'todo',    verificationStatus: 'pending' },
  ]
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
        if (newTitle !== t.title || newDesc !== t.description || newTag !== t.episodeTag
            || newPT !== t.promptTitle || newSeq !== t.sequence
            || JSON.stringify(newVerify) !== JSON.stringify(t.verify || null)
            || JSON.stringify(newPr) !== JSON.stringify(t.pr || null)) {
          clone()[idx] = { ...t, title: newTitle, description: newDesc,
                           episodeTag: newTag, promptTitle: newPT, sequence: newSeq,
                           verify: newVerify, pr: newPr }
        }
      }
      if (additions.length) clone().push(...additions)
      return next || state          // same reference when nothing changed → no churn
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

export function usePMState() {
  return useReducer(pmReducer, undefined, makeDemoTasks)
}
