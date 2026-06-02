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

function pmReducer(state, action) {
  switch (action.type) {
    case 'CREATE_TASK': {
      const task = {
        id: makeId(),
        title: action.title || 'New task',
        description: action.description || '',
        linkedBoxIds: action.linkedBoxIds || [],
        column: action.column || 'todo',
        verificationStatus: 'pending',
      }
      return [...state, task]
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
