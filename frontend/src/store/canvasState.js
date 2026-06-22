import { useReducer } from 'react'

export const MIN_W = 160
export const MIN_H = 110
export const DEFAULT_W = 200
export const DEFAULT_H = 130

function makeId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
}

const initialState = {
  boxes: [],
  arrows: [],
  selectedIds: new Set(),
  selectedArrowIds: new Set(),
  editingBoxId: null,
  editingField: null,
}

function reducer(state, action) {
  switch (action.type) {
    case 'CREATE_BOX': {
      const box = {
        id: makeId(),
        x: action.x,
        y: action.y,
        w: DEFAULT_W,
        h: DEFAULT_H,
        type: action.boxType || 'dotted',
        title: 'Module',
        prompt: 'Describe what this module does...',
        linkedFiles: [],
        status: 'draft',
      }
      return { ...state, boxes: [...state.boxes, box], selectedIds: new Set([box.id]) }
    }
    case 'UPDATE_BOX': {
      return {
        ...state,
        boxes: state.boxes.map(b => b.id === action.id ? { ...b, ...action.fields } : b),
      }
    }
    case 'SET_POSITIONS': {
      return {
        ...state,
        boxes: state.boxes.map(b => {
          const p = action.positions[b.id]
          if (!p) return b
          return { ...b, x: Math.max(0, p.x), y: Math.max(0, p.y) }
        }),
      }
    }
    case 'SET_SIZE': {
      return {
        ...state,
        boxes: state.boxes.map(b =>
          b.id === action.id
            ? { ...b, w: Math.max(MIN_W, action.w), h: Math.max(MIN_H, action.h) }
            : b
        ),
      }
    }
    case 'SELECT': {
      return { ...state, selectedIds: new Set([action.id]), selectedArrowIds: new Set() }
    }
    case 'TOGGLE_SELECT': {
      const next = new Set(state.selectedIds)
      if (next.has(action.id)) next.delete(action.id)
      else next.add(action.id)
      return { ...state, selectedIds: next, selectedArrowIds: new Set() }
    }
    case 'SELECT_MANY': {
      return { ...state, selectedIds: new Set(action.ids), selectedArrowIds: new Set() }
    }
    case 'CLEAR_SELECTION': {
      return { ...state, selectedIds: new Set(), selectedArrowIds: new Set() }
    }
    case 'SELECT_ARROW': {
      return { ...state, selectedArrowIds: new Set([action.id]), selectedIds: new Set() }
    }
    case 'SET_EDITING': {
      return { ...state, editingBoxId: action.id, editingField: action.field }
    }
    case 'STOP_EDITING': {
      return { ...state, editingBoxId: null, editingField: null }
    }
    case 'DELETE_BOXES': {
      const ids = new Set(action.ids)
      const next = new Set(state.selectedIds)
      ids.forEach(id => next.delete(id))
      // Remove selected arrow IDs for arrows that will be cascade-deleted
      const survivingArrowIds = new Set(
        state.arrows.filter(a => !ids.has(a.fromBox) && !ids.has(a.toBox)).map(a => a.id)
      )
      const nextArrowIds = new Set([...state.selectedArrowIds].filter(id => survivingArrowIds.has(id)))
      return {
        ...state,
        boxes: state.boxes.filter(b => !ids.has(b.id)),
        // cascade: remove any arrow touching a deleted box
        arrows: state.arrows.filter(a => !ids.has(a.fromBox) && !ids.has(a.toBox)),
        selectedIds: next,
        selectedArrowIds: nextArrowIds,
        editingBoxId: ids.has(state.editingBoxId) ? null : state.editingBoxId,
        editingField: ids.has(state.editingBoxId) ? null : state.editingField,
      }
    }
    case 'DUPLICATE_BOXES': {
      const ids = new Set(action.ids)
      const copies = state.boxes
        .filter(b => ids.has(b.id))
        .map(b => ({ ...b, id: makeId(), x: b.x + 24, y: b.y + 24 }))
      return {
        ...state,
        boxes: [...state.boxes, ...copies],
        selectedIds: new Set(copies.map(c => c.id)),
      }
    }
    case 'TOGGLE_TYPE': {
      const ids = new Set(action.ids)
      return {
        ...state,
        boxes: state.boxes.map(b =>
          ids.has(b.id) ? { ...b, type: b.type === 'dotted' ? 'solid' : 'dotted' } : b
        ),
      }
    }
    case 'TOGGLE_INTENT': {
      // Mark/unmark boxes as plain-English intent steps (Sketch-First Intent).
      // Intent boxes carry no linked files; `kind:undefined` reverts to a module.
      // A fresh intent box starts its lifecycle at `planned`.
      const ids = new Set(action.ids)
      return {
        ...state,
        boxes: state.boxes.map(b =>
          ids.has(b.id)
            ? { ...b, kind: b.kind === 'intent' ? undefined : 'intent',
                runState: b.kind === 'intent' ? undefined : 'planned' }
            : b
        ),
      }
    }
    case 'SET_BOX_RUN_STATE': {
      // Move planned boxes through the run lifecycle IN PLACE — planned → running →
      // built | blocked. The same box stays on the canvas; it is never replaced.
      const ids = new Set(action.ids)
      return {
        ...state,
        boxes: state.boxes.map(b => ids.has(b.id) ? { ...b, runState: action.runState } : b),
      }
    }
    case 'SET_IMPL_FILES': {
      // Post-run link-back: attach a council run's changed files to the intent
      // boxes it implemented, and mark them `built`. action.links: { boxId:
      // {files, attribution, confidence} }.
      const links = action.links || {}
      return {
        ...state,
        boxes: state.boxes.map(b =>
          links[b.id]
            ? {
                ...b,
                runState: 'built',
                implementationFiles: links[b.id].files || [],
                implementationMeta: {
                  attribution: links[b.id].attribution || 'graph',
                  confidence: links[b.id].confidence ?? null,
                  runId: action.runId || null,
                },
              }
            : b
        ),
      }
    }
    case 'FREEZE_SELECTED': {
      return {
        ...state,
        boxes: state.boxes.map(b =>
          state.selectedIds.has(b.id) && b.type === 'dotted' ? { ...b, type: 'solid' } : b
        ),
      }
    }
    case 'MAKE_SELECTED_DOTTED': {
      return {
        ...state,
        boxes: state.boxes.map(b =>
          state.selectedIds.has(b.id) && b.type === 'solid' ? { ...b, type: 'dotted' } : b
        ),
      }
    }
    case 'LOAD_SELF_MAP': {
      if (state.boxes.length > 0) return state
      const fe = { id: makeId(), x: 340, y: 50,  w: DEFAULT_W, h: DEFAULT_H, type: 'solid',  status: 'draft', title: 'OpenFDE frontend',  linkedFiles: ['frontend/src/App.jsx', 'frontend/src/App.css'],                                                                                                                             prompt: 'IDE shell: toolbar, file tree, whiteboard, right panel, theme, and views.' }
      const wb = { id: makeId(), x: 60,  y: 240, w: DEFAULT_W, h: DEFAULT_H, type: 'dotted', status: 'draft', title: 'Whiteboard canvas', linkedFiles: ['frontend/src/components/Whiteboard/WhiteboardCanvas.jsx', 'frontend/src/components/Whiteboard/CanvasBox.jsx', 'frontend/src/store/canvasState.js'], prompt: 'Executable architecture surface: boxes, ports, arrows, selection, editing, and permission boundaries.' }
      const rp = { id: makeId(), x: 300, y: 240, w: DEFAULT_W, h: DEFAULT_H, type: 'dotted', status: 'draft', title: 'Right panel',        linkedFiles: ['frontend/src/components/RightPanel/RightPanel.jsx'],                                                                                                                    prompt: 'Agent, Inspector, Task, and Plan modes that react to selected architecture context.' }
      const pm = { id: makeId(), x: 540, y: 240, w: DEFAULT_W, h: DEFAULT_H, type: 'dotted', status: 'draft', title: 'OpenPM board',       linkedFiles: [],                                                                                                                                                                       prompt: 'Task board for todo, doing, testing, and done work tied back to architecture boxes.' }
      const tl = { id: makeId(), x: 780, y: 240, w: DEFAULT_W, h: DEFAULT_H, type: 'dotted', status: 'draft', title: 'Timeline',           linkedFiles: [],                                                                                                                                                                       prompt: 'Design and code evolution playback across boxes, tasks, commits, and verification events.' }
      const cs = { id: makeId(), x: 300, y: 450, w: DEFAULT_W, h: DEFAULT_H, type: 'solid',  status: 'draft', title: 'Canvas state',       linkedFiles: ['frontend/src/store/canvasState.js'],                                                                                                                                    prompt: 'Reducer-backed local model for boxes, arrows, selection, editing, and interaction state.' }
      const boxes = [fe, wb, rp, pm, tl, cs]
      const arrows = [
        { id: makeId(), fromBox: fe.id, fromPort: 'w', toBox: wb.id, toPort: 'n', type: fe.type, label: '' },
        { id: makeId(), fromBox: fe.id, fromPort: 's', toBox: rp.id, toPort: 'n', type: fe.type, label: '' },
        { id: makeId(), fromBox: wb.id, fromPort: 's', toBox: cs.id, toPort: 'n', type: wb.type, label: '' },
        { id: makeId(), fromBox: rp.id, fromPort: 's', toBox: cs.id, toPort: 'n', type: rp.type, label: '' },
        { id: makeId(), fromBox: pm.id, fromPort: 's', toBox: cs.id, toPort: 'n', type: pm.type, label: '' },
        { id: makeId(), fromBox: tl.id, fromPort: 's', toBox: cs.id, toPort: 'n', type: tl.type, label: '' },
      ]
      return { ...state, boxes, arrows, selectedIds: new Set([wb.id]), selectedArrowIds: new Set() }
    }
    case 'CREATE_ARROW': {
      // No self-loops
      if (action.fromBox === action.toBox) return state
      // No duplicates on the same port pair
      const exists = state.arrows.some(a =>
        a.fromBox === action.fromBox && a.fromPort === action.fromPort &&
        a.toBox === action.toBox && a.toPort === action.toPort
      )
      if (exists) return state
      const arrow = {
        id: makeId(),
        fromBox: action.fromBox,
        fromPort: action.fromPort,
        toBox: action.toBox,
        toPort: action.toPort,
        type: action.arrowType || 'dotted',
        label: '',
      }
      return { ...state, arrows: [...state.arrows, arrow] }
    }
    case 'UPDATE_ARROW': {
      return {
        ...state,
        arrows: state.arrows.map(a => a.id === action.id ? { ...a, ...action.fields } : a),
      }
    }
    case 'DELETE_ARROW': {
      const nextArrowIds = new Set(state.selectedArrowIds)
      nextArrowIds.delete(action.id)
      return { ...state, arrows: state.arrows.filter(a => a.id !== action.id), selectedArrowIds: nextArrowIds }
    }
    // Hydrate from backend — replaces boxes and arrows, clears ephemeral selection state.
    // `running` is transient: a box persisted mid-run (then reloaded) is no longer
    // executing, so it falls back to `planned` rather than spinning forever.
    case 'HYDRATE': {
      return {
        ...state,
        boxes: (action.boxes ?? []).map(b => b.runState === 'running' ? { ...b, runState: 'planned' } : b),
        arrows: action.arrows ?? [],
        selectedIds: new Set(),
        selectedArrowIds: new Set(),
        editingBoxId: null,
        editingField: null,
      }
    }
    default:
      return state
  }
}

export function useCanvasState() {
  return useReducer(reducer, initialState)
}
