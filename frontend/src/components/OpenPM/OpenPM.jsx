import { useState } from 'react'

const COLS = [
  { id: 'todo',    label: 'To Do' },
  { id: 'doing',   label: 'Doing' },
  { id: 'testing', label: 'Testing' },
  { id: 'done',    label: 'Done' },
]

// ── Colour helpers ────────────────────────────────────────────────────
const colHeaderColor = {
  todo:    'var(--text-muted)',
  doing:   'var(--accent)',
  testing: 'var(--active)',
  done:    'var(--solid)',
}

const veriBadge = {
  pending: { color: 'var(--text-muted)', bg: 'transparent',               border: 'var(--border)' },
  passed:  { color: 'var(--solid)',      bg: 'rgba(61,186,110,0.10)',      border: 'rgba(61,186,110,0.3)' },
  failed:  { color: 'var(--violation)',  bg: 'rgba(227,51,51,0.10)',       border: 'rgba(227,51,51,0.3)' },
}

// ── OpenPM ────────────────────────────────────────────────────────────
export default function OpenPM({
  tasks,
  pmDispatch,
  canvasState,
  canvasDispatch,
  setActiveView,
  setPanelMode,
  selectedTaskId,
  setSelectedTaskId,
  onTaskEvent,
}) {
  const [draggingId, setDraggingId]   = useState(null)
  const [dragOverCol, setDragOverCol] = useState(null)
  const [blockedId, setBlockedId]     = useState(null)
  const [addingCol, setAddingCol]     = useState(null)
  const [addTitle, setAddTitle]       = useState('')

  // ── Drag handlers ──────────────────────────────────────────────────
  function startDrag(e, taskId) {
    setDraggingId(taskId)
    e.dataTransfer.effectAllowed = 'move'
    // Suppress the default ghost (works in most browsers)
    e.dataTransfer.setData('text/plain', taskId)
  }

  function onDragOver(e, colId) {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    setDragOverCol(colId)
  }

  function onDrop(e, targetCol) {
    e.preventDefault()
    setDragOverCol(null)
    const taskId = draggingId || e.dataTransfer.getData('text/plain')
    setDraggingId(null)
    if (!taskId) return

    const task = tasks.find(t => t.id === taskId)
    if (!task || task.column === targetCol) return

    // Gate: can only move to done if verificationStatus === 'passed'
    if (targetCol === 'done' && task.verificationStatus !== 'passed') {
      setBlockedId(taskId)
      const id = taskId
      setTimeout(() => setBlockedId(b => (b === id ? null : b)), 2500)
      return
    }

    const fromCol = task.column
    pmDispatch({ type: 'MOVE_TASK', id: taskId, column: targetCol })
    // Moving to testing resets verificationStatus to pending
    if (targetCol === 'testing') {
      pmDispatch({ type: 'UPDATE_TASK', id: taskId, fields: { verificationStatus: 'pending' } })
    }
    onTaskEvent({
      type: 'task_moved',
      payload: { taskId, title: task.title, from: fromCol, to: targetCol },
    })
  }

  function onDragEnd() {
    setDraggingId(null)
    setDragOverCol(null)
  }

  // ── Add task ───────────────────────────────────────────────────────
  function commitAdd(col) {
    const title = addTitle.trim()
    if (!title) { cancelAdd(); return }
    // Default link: currently selected single box
    const sel = [...canvasState.selectedIds]
    pmDispatch({
      type: 'CREATE_TASK',
      title,
      column: col,
      linkedBoxIds: sel.length === 1 ? sel : [],
    })
    setAddTitle('')
    setAddingCol(null)
  }

  function cancelAdd() {
    setAddTitle('')
    setAddingCol(null)
  }

  // ── Chip click: navigate canvas + open Inspector ───────────────────
  function handleChipClick(e, boxId) {
    e.stopPropagation()
    canvasDispatch({ type: 'SELECT', id: boxId })
    setActiveView('whiteboard')
    setPanelMode('Inspector')
  }

  // ── Select task: show in right panel Task mode ─────────────────────
  function selectTask(taskId) {
    setSelectedTaskId(taskId)
    setPanelMode('Task')
  }

  const totalOpen = tasks.filter(t => t.column !== 'done').length
  const totalDone = tasks.filter(t => t.column === 'done').length

  return (
    <div style={{
      flex: 1, display: 'flex', flexDirection: 'column',
      overflow: 'hidden', padding: '16px 14px 14px',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, flexShrink: 0 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.6px' }}>
          OpenPM
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {totalOpen} open · {totalDone} done
        </span>
      </div>

      {/* Board */}
      <div style={{ display: 'flex', gap: 8, flex: 1, overflow: 'hidden', minHeight: 0 }}>
        {COLS.map(col => {
          const colTasks = tasks.filter(t => t.column === col.id)
          const isOver   = dragOverCol === col.id

          return (
            <div
              key={col.id}
              onDragOver={e => onDragOver(e, col.id)}
              onDrop={e => onDrop(e, col.id)}
              onDragLeave={e => {
                // Only clear if leaving the column itself, not a child
                if (!e.currentTarget.contains(e.relatedTarget)) setDragOverCol(null)
              }}
              style={{
                flex: 1, minWidth: 0,
                display: 'flex', flexDirection: 'column',
                background: isOver ? 'rgba(124,111,247,0.04)' : 'var(--surface)',
                border: `1px solid ${isOver ? 'rgba(124,111,247,0.35)' : 'var(--border)'}`,
                borderRadius: 'var(--radius)',
                overflow: 'hidden',
                transition: 'border-color 0.12s, background 0.12s',
              }}
            >
              {/* Column header */}
              <div style={{
                padding: '7px 10px',
                borderBottom: '1px solid var(--border)',
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                flexShrink: 0,
              }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: colHeaderColor[col.id] }}>
                  {col.label}
                </span>
                <span style={{
                  background: 'var(--surface-2)', padding: '1px 6px',
                  borderRadius: 99, fontSize: 10, color: 'var(--text-muted)',
                }}>
                  {colTasks.length}
                </span>
              </div>

              {/* Task list */}
              <div style={{
                flex: 1, overflow: 'auto', padding: '6px',
                display: 'flex', flexDirection: 'column', gap: 5,
              }}>
                {colTasks.map(task => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    allBoxes={canvasState.boxes}
                    selected={task.id === selectedTaskId}
                    blocked={task.id === blockedId}
                    dragging={task.id === draggingId}
                    onSelect={() => selectTask(task.id)}
                    onDragStart={e => startDrag(e, task.id)}
                    onDragEnd={onDragEnd}
                    onChipClick={handleChipClick}
                    onVerify={status => pmDispatch({ type: 'UPDATE_TASK', id: task.id, fields: { verificationStatus: status } })}
                  />
                ))}

                {/* Inline add form */}
                {addingCol === col.id ? (
                  <AddForm
                    value={addTitle}
                    onChange={setAddTitle}
                    onCommit={() => commitAdd(col.id)}
                    onCancel={cancelAdd}
                  />
                ) : (
                  <button
                    onClick={() => setAddingCol(col.id)}
                    style={{
                      background: 'transparent',
                      border: '1px dashed var(--border)',
                      borderRadius: 'var(--radius-sm)',
                      color: 'var(--text-muted)',
                      fontSize: 11, padding: '5px',
                      cursor: 'pointer', fontFamily: 'inherit',
                      marginTop: 2,
                    }}
                  >
                    + Add
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── TaskCard ──────────────────────────────────────────────────────────
function TaskCard({ task, allBoxes, selected, blocked, dragging, onSelect, onDragStart, onDragEnd, onChipClick, onVerify }) {
  const vc = veriBadge[task.verificationStatus] || veriBadge.pending

  return (
    <div
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onClick={onSelect}
      style={{
        background: selected ? 'rgba(124,111,247,0.08)' : 'var(--surface-2)',
        border: `1px solid ${blocked ? 'var(--violation)' : selected ? 'rgba(124,111,247,0.4)' : 'var(--border)'}`,
        borderRadius: 'var(--radius-sm)',
        padding: '7px 9px',
        cursor: 'grab',
        opacity: dragging ? 0.35 : 1,
        transition: 'border-color 0.1s, background 0.1s',
        userSelect: 'none',
      }}
    >
      {/* Title + verification badge */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 6, marginBottom: 3 }}>
        <span style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.4, fontWeight: 500 }}>
          {task.title}
        </span>
        <span style={{
          fontSize: 9, padding: '1px 5px', borderRadius: 99, flexShrink: 0,
          color: vc.color, background: vc.bg, border: `1px solid ${vc.border}`,
          textTransform: 'uppercase', letterSpacing: '0.3px', fontWeight: 600,
        }}>
          {task.verificationStatus}
        </span>
      </div>

      {/* Description */}
      {task.description && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 5, lineHeight: 1.4 }}>
          {task.description.length > 72 ? task.description.slice(0, 72) + '…' : task.description}
        </div>
      )}

      {/* Linked box chips */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, marginBottom: 5, minHeight: 16 }}>
        {task.linkedBoxIds.length === 0 ? (
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontStyle: 'italic', opacity: 0.6 }}>
            No linked boxes
          </span>
        ) : task.linkedBoxIds.map(boxId => {
          const box = allBoxes.find(b => b.id === boxId)
          if (!box) return null
          const isDotted = box.type === 'dotted'
          return (
            <span
              key={boxId}
              onClick={e => onChipClick(e, boxId)}
              title={`Navigate to "${box.title}" on canvas`}
              style={{
                fontSize: 10,
                color: isDotted ? 'var(--dotted)' : 'var(--solid)',
                background: isDotted ? 'rgba(74,158,255,0.08)' : 'rgba(61,186,110,0.08)',
                border: `1px solid ${isDotted ? 'rgba(74,158,255,0.2)' : 'rgba(61,186,110,0.2)'}`,
                padding: '1px 6px', borderRadius: 99,
                cursor: 'pointer',
                transition: 'opacity 0.1s',
              }}
            >
              {box.title}
            </span>
          )
        })}
      </div>

      {/* Blocked feedback */}
      {blocked && (
        <div style={{ fontSize: 10, color: 'var(--violation)', marginBottom: 4, fontStyle: 'italic' }}>
          Verification required before moving to Done
        </div>
      )}

      {/* Verification controls */}
      <div style={{ display: 'flex', gap: 4 }}>
        <VerifyBtn
          label="✓ Pass"
          active={task.verificationStatus === 'passed'}
          activeColor="var(--solid)"
          activeBg="rgba(61,186,110,0.10)"
          activeBorder="rgba(61,186,110,0.35)"
          onClick={e => { e.stopPropagation(); onVerify('passed') }}
        />
        <VerifyBtn
          label="✗ Fail"
          active={task.verificationStatus === 'failed'}
          activeColor="var(--violation)"
          activeBg="rgba(227,51,51,0.10)"
          activeBorder="rgba(227,51,51,0.35)"
          onClick={e => { e.stopPropagation(); onVerify('failed') }}
        />
      </div>
    </div>
  )
}

function VerifyBtn({ label, active, activeColor, activeBg, activeBorder, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '1px 7px',
        background: active ? activeBg : 'transparent',
        border: `1px solid ${active ? activeBorder : 'var(--border)'}`,
        borderRadius: 99,
        color: active ? activeColor : 'var(--text-muted)',
        fontSize: 10,
        cursor: 'pointer',
        fontFamily: 'inherit',
        transition: 'color 0.1s, background 0.1s, border-color 0.1s',
      }}
    >
      {label}
    </button>
  )
}

// ── Inline add form ───────────────────────────────────────────────────
function AddForm({ value, onChange, onCommit, onCancel }) {
  return (
    <div style={{
      padding: '7px 9px',
      background: 'var(--surface-2)',
      border: '1px solid rgba(124,111,247,0.5)',
      borderRadius: 'var(--radius-sm)',
    }}>
      <input
        autoFocus
        placeholder="Task title… (Enter to add)"
        value={value}
        onChange={e => onChange(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter') onCommit()
          if (e.key === 'Escape') onCancel()
        }}
        style={{
          width: '100%', background: 'transparent', border: 'none',
          color: 'var(--text)', fontSize: 12, fontFamily: 'inherit',
          outline: 'none', padding: 0, marginBottom: 6, boxSizing: 'border-box',
        }}
      />
      <div style={{ display: 'flex', gap: 4 }}>
        <button
          onClick={onCommit}
          style={{
            padding: '2px 9px', background: 'var(--accent)', border: '1px solid var(--accent)',
            borderRadius: 'var(--radius-sm)', color: '#fff', fontSize: 11,
            cursor: 'pointer', fontFamily: 'inherit',
          }}
        >
          Add
        </button>
        <button
          onClick={onCancel}
          style={{
            padding: '2px 9px', background: 'transparent', border: '1px solid var(--border)',
            borderRadius: 'var(--radius-sm)', color: 'var(--text-muted)', fontSize: 11,
            cursor: 'pointer', fontFamily: 'inherit',
          }}
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
