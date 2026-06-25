import { useState } from 'react'
import { getTasks, importGithubIssue, reproduceIssue } from '../../api/backend'
import { surfaceView } from '../../store/hydration'

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

// Verify Gate receipts → tiny per-check badge text ("tests ✓", "lint ✓").
const VERIFY_SHORT = { 'unit-tests': 'tests', 'frontend-lint': 'lint' }
const verifyShort = ch => VERIFY_SHORT[ch.id] || (ch.id || 'check').replace(/-/g, ' ').slice(0, 10)

// ── OpenPM ────────────────────────────────────────────────────────────
export default function OpenPM({
  tasks,
  hydration = 'live',
  pmDispatch,
  canvasState,
  canvasDispatch,
  setActiveView,
  setPanelMode,
  selectedTaskId,
  setSelectedTaskId,
  onTaskEvent,
  onSpotlightCommit,
  highlightTags = null,
  onClearHighlight,
  onFocusFile,
}) {
  // Story concept filter: when a concept is selected, dim cards whose prompt tag
  // isn't part of that concept (non-destructive — nothing is hidden or reordered).
  const filterTags = (highlightTags && highlightTags.length) ? new Set(highlightTags) : null
  const [draggingId, setDraggingId]   = useState(null)
  const [dragOverCol, setDragOverCol] = useState(null)
  const [blockedId, setBlockedId]     = useState(null)
  const [addingCol, setAddingCol]     = useState(null)
  const [addTitle, setAddTitle]       = useState('')
  // Within-column ordering: 'story' = newest prompt first (sequence desc, which
  // clusters a prompt's commits since they share its sequence); 'tag' = by prompt
  // tag oldest-first (chronological). Kanban columns + gates are unchanged.
  const [sortMode, setSortMode]       = useState('story')
  // The Reproduce button (issue cards): triage → locate → draft → run, with
  // honest refusals. Run-once by issue-body hash; ↻ regenerates explicitly.
  const [reproBusyId, setReproBusyId] = useState(null)
  async function onReproduce(task, regenerate = false) {
    if (reproBusyId) return
    setReproBusyId(task.id)
    const r = await reproduceIssue(task.id, regenerate)
    setReproBusyId(null)
    if (r?.repro) pmDispatch({ type: 'UPDATE_TASK', id: task.id, fields: { repro: r.repro } })
  }

  // GitHub issue import (durable intent v1): issue # → To Do card with intentSource.
  const [issueOpen, setIssueOpen]     = useState(false)
  const [issueVal, setIssueVal]       = useState('')
  const [issueBusy, setIssueBusy]     = useState(false)
  const [issueErr, setIssueErr]       = useState('')

  async function doImportIssue() {
    const num = parseInt((issueVal || '').replace(/[^0-9]/g, ''), 10)
    if (!num) { setIssueErr('issue # required'); return }
    setIssueBusy(true); setIssueErr('')
    const res = await importGithubIssue({ issueNumber: num })
    setIssueBusy(false)
    if (!res?.ok) { setIssueErr(res?.error || 'import failed'); return }
    // Hydrate from the backend (it persisted the upsert) so this board re-renders
    // with the card; the PUT round-trip then no-ops server-side (unchanged list).
    const saved = await getTasks()
    if (Array.isArray(saved)) pmDispatch({ type: 'HYDRATE_TASKS', tasks: saved })
    setIssueVal(''); setIssueOpen(false)
  }

  function orderTasks(list) {
    const arr = [...list]
    if (sortMode === 'tag') {
      arr.sort((a, b) => (a.episodeTag ? (a.sequence || 0) : Infinity) - (b.episodeTag ? (b.sequence || 0) : Infinity))
    } else {
      arr.sort((a, b) => (b.sequence || 0) - (a.sequence || 0))   // story: newest prompt first
    }
    return arr
  }

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
  // Never claim "0 open · 0 done" until /api/tasks confirms an empty board — show a restoring state
  // while hydration is still pending (boot memory must feel present immediately).
  const restoring = surfaceView(hydration, tasks.length > 0) === 'restoring'

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
          {restoring ? 'Restoring task board…' : `${totalOpen} open · ${totalDone} done`}
        </span>
        {filterTags && (
          <button
            onClick={() => onClearHighlight?.()}
            title="Clear the Story concept filter"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 10, fontFamily: 'inherit',
              color: 'var(--accent)', background: 'rgba(124,111,247,0.10)',
              border: '1px solid rgba(124,111,247,0.35)', borderRadius: 99, padding: '1px 8px', cursor: 'pointer',
            }}
          >
            Story: {[...filterTags].slice(0, 4).join(', ')} ✕
          </button>
        )}
        <div style={{ flex: 1 }} />
        {/* GitHub issue → durable intent: issue # becomes a To Do card (gh CLI). */}
        {issueOpen ? (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <input
              autoFocus
              placeholder="#42"
              value={issueVal}
              onChange={e => { setIssueVal(e.target.value); setIssueErr('') }}
              onKeyDown={e => {
                if (e.key === 'Enter') doImportIssue()
                if (e.key === 'Escape') { setIssueOpen(false); setIssueVal(''); setIssueErr('') }
              }}
              style={{
                width: 52, fontSize: 11, fontFamily: 'inherit', color: 'var(--text)',
                background: 'var(--surface-2)', border: '1px solid rgba(74,158,255,0.4)',
                borderRadius: 99, padding: '1px 8px', outline: 'none',
              }}
            />
            <button
              onClick={doImportIssue}
              disabled={issueBusy}
              style={{
                padding: '1px 8px', borderRadius: 99, cursor: issueBusy ? 'wait' : 'pointer',
                fontFamily: 'inherit', fontSize: 10, color: 'var(--dotted)',
                background: 'rgba(74,158,255,0.10)', border: '1px solid rgba(74,158,255,0.35)',
              }}
            >
              {issueBusy ? 'Importing…' : 'Import'}
            </button>
            {issueErr && (
              <span title={issueErr} style={{ fontSize: 10, color: 'var(--violation)', maxWidth: 180,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {issueErr}
              </span>
            )}
          </span>
        ) : (
          <button
            onClick={() => setIssueOpen(true)}
            title="Import a GitHub issue as planned work (uses the local gh CLI)"
            style={{
              padding: '1px 8px', borderRadius: 99, cursor: 'pointer', fontFamily: 'inherit',
              fontSize: 10, color: 'var(--dotted)', background: 'transparent',
              border: '1px solid var(--border)',
            }}
          >
            ⊕ Issue
          </button>
        )}
        {/* Story / Tag ordering — clusters a prompt's work items together. */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 10, color: 'var(--text-muted)' }}>
          <span style={{ textTransform: 'uppercase', letterSpacing: '0.4px' }}>Sort</span>
          {['story', 'tag'].map(m => (
            <button
              key={m}
              onClick={() => setSortMode(m)}
              title={m === 'story' ? 'Newest prompt first' : 'Group by prompt tag (chronological)'}
              style={{
                padding: '1px 7px', borderRadius: 99, cursor: 'pointer', fontFamily: 'inherit', fontSize: 10,
                textTransform: 'capitalize',
                color: sortMode === m ? 'var(--accent)' : 'var(--text-muted)',
                background: sortMode === m ? 'rgba(124,111,247,0.10)' : 'transparent',
                border: `1px solid ${sortMode === m ? 'rgba(124,111,247,0.35)' : 'var(--border)'}`,
              }}
            >
              {m}
            </button>
          ))}
        </div>
      </div>

      {/* Board */}
      <div style={{ display: 'flex', gap: 8, flex: 1, overflow: 'hidden', minHeight: 0 }}>
        {restoring && COLS.map(col => (
          <div key={`sk-${col.id}`} style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: colHeaderColor[col.id], textTransform: 'uppercase',
              letterSpacing: '0.4px', padding: '0 2px 6px', opacity: 0.55 }}>{col.label}</div>
            <div className="pm-skeleton-card" />
            <div className="pm-skeleton-card" />
          </div>
        ))}
        {!restoring && COLS.map(col => {
          const colTasks = orderTasks(tasks.filter(t => t.column === col.id))
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
                    onCommitClick={onSpotlightCommit ? (e, sha) => { e.stopPropagation(); onSpotlightCommit(sha) } : null}
                    dimmed={!!(filterTags && !filterTags.has(task.episodeTag))}
                    onVerify={status => pmDispatch({ type: 'UPDATE_TASK', id: task.id, fields: { verificationStatus: status } })}
                    reproBusy={task.id === reproBusyId}
                    onReproduce={onReproduce}
                    onFocusFile={onFocusFile}
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

// ── Reproduce strip — issue card → honest verdict chip ────────────────
// reproduced = red (a real failing test now exists); everything else is a
// refusal with its reason: feature requests have nothing to reproduce, vague
// reports list what's missing, stale issues report not-reproduced (reverted).
const REPRO_VIEW = {
  reproduced:        { color: 'var(--violation)', bg: 'rgba(227,51,51,0.10)', br: 'rgba(227,51,51,0.4)' },
  not_a_bug:         { color: 'var(--text-muted)', bg: 'rgba(255,255,255,0.04)', br: 'var(--border)' },
  insufficient:      { color: 'var(--active)', bg: 'rgba(255,179,71,0.10)', br: 'rgba(255,179,71,0.35)' },
  not_reproduced:    { color: 'var(--active)', bg: 'rgba(255,179,71,0.10)', br: 'rgba(255,179,71,0.35)' },
  no_agent:          { color: 'var(--active)', bg: 'rgba(255,179,71,0.10)', br: 'rgba(255,179,71,0.35)' },
  unsupported_runner:{ color: 'var(--active)', bg: 'rgba(255,179,71,0.10)', br: 'rgba(255,179,71,0.35)' },
  draft_failed:      { color: 'var(--active)', bg: 'rgba(255,179,71,0.10)', br: 'rgba(255,179,71,0.35)' },
  run_error:         { color: 'var(--active)', bg: 'rgba(255,179,71,0.10)', br: 'rgba(255,179,71,0.35)' },
}
const REPRO_LABEL = {
  reproduced: r => `reproduced ✕ — ${r.testName || 'failing test written'}`,
  not_a_bug: () => 'not a bug — nothing to reproduce',
  insufficient: () => 'can’t reproduce from issue text',
  not_reproduced: () => 'did not reproduce on main',
  no_agent: () => 'needs an agent (Agents → Senior Dev)',
  unsupported_runner: () => 'needs a pytest check (v1)',
  draft_failed: () => 'could not draft a valid test',
  run_error: () => 'repro run errored',
}

function ReproStrip({ task, busy, onReproduce, onFocusFile }) {
  const r = task.repro
  if (!r) {
    return (
      <div style={{ marginBottom: 4 }}>
        <button
          onClick={e => { e.stopPropagation(); onReproduce(task) }}
          disabled={busy}
          title="Read the issue, verify it against the code, draft ONE failing test, and run it — refuses honestly when there's nothing to reproduce"
          style={{
            fontSize: 9.5, fontWeight: 700, fontFamily: 'inherit', cursor: busy ? 'wait' : 'pointer',
            color: 'var(--accent-orange)', background: 'var(--accent-orange-soft)',
            border: '1px solid var(--accent-orange-border)', borderRadius: 99, padding: '1px 8px',
          }}
        >
          {busy ? 'reproducing…' : '⌖ Reproduce'}
        </button>
      </div>
    )
  }
  const v = REPRO_VIEW[r.verdict] || REPRO_VIEW.run_error
  const label = (REPRO_LABEL[r.verdict] || REPRO_LABEL.run_error)(r)
  const tip = (r.summary || '')
    + (r.missing?.length ? `\nmissing: ${r.missing.join('; ')}` : '')
    + (r.testFile ? `\ntest: ${r.testFile}::${r.testName}` : '')
    + (r.source ? `\n${r.source}` : '')
  return (
    <div title={tip} style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }}>
      <span style={{
        fontSize: 9, fontWeight: 700, color: v.color, background: v.bg,
        border: `1px solid ${v.br}`, borderRadius: 5, padding: '0 5px',
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 180,
      }}>{label}</span>
      <button
        onClick={e => { e.stopPropagation(); onReproduce(task, true) }}
        disabled={busy}
        title="Regenerate — re-triage the live issue text and try again"
        style={{
          fontSize: 9, fontFamily: 'inherit', cursor: busy ? 'wait' : 'pointer',
          color: 'var(--text-muted)', background: 'transparent',
          border: '1px solid var(--border)', borderRadius: 5, padding: '0 4px',
        }}
      >{busy ? '…' : '↻'}</button>
      {(r.links || []).slice(0, 2).map(f => (
        <button
          key={f}
          onClick={e => { e.stopPropagation(); onFocusFile?.(f) }}
          title={`open ${f} on the canvas — where this issue lives`}
          style={{
            fontSize: 9, fontFamily: 'ui-monospace, monospace', cursor: 'pointer',
            color: 'var(--accent-orange)', background: 'transparent',
            border: '1px solid var(--accent-orange-border)', borderRadius: 5, padding: '0 4px',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 110,
          }}
        >⌖ {f.split('/').pop()}</button>
      ))}
    </div>
  )
}

// ── TaskCard ──────────────────────────────────────────────────────────
function TaskCard({ task, allBoxes, selected, blocked, dragging, dimmed, onSelect, onDragStart, onDragEnd, onChipClick, onCommitClick, onVerify, reproBusy, onReproduce, onFocusFile }) {
  const vc = veriBadge[task.verificationStatus] || veriBadge.pending
  const sha = task.shortSha || (task.commitSha ? task.commitSha.slice(0, 7) : '')

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
        opacity: dragging ? 0.35 : dimmed ? 0.32 : 1,
        transition: 'border-color 0.1s, background 0.1s, opacity 0.12s',
        userSelect: 'none',
      }}
    >
      {/* Durable intent — the GitHub issue this card came from. The badge links to
          the issue; closed state and labels stay visible (intent is preserved,
          never auto-deleted). Intent precedes the episode, so it renders first. */}
      {task.intentSource?.provider === 'github' && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4, flexWrap: 'wrap' }}>
          <a
            href={task.intentSource.url || undefined}
            target="_blank" rel="noreferrer"
            onClick={e => e.stopPropagation()}
            title={`GitHub issue #${task.intentSource.issueNumber} · ${task.intentSource.state}`
                   + `${task.intentSource.labels?.length ? ' · ' + task.intentSource.labels.join(', ') : ''}`
                   + (task.intentSource.url ? ' — open on GitHub' : '')}
            style={{
              flexShrink: 0, fontSize: 9, fontWeight: 700, fontFamily: 'ui-monospace, monospace',
              color: 'var(--dotted)', background: 'rgba(74,158,255,0.10)',
              border: '1px solid rgba(74,158,255,0.3)', borderRadius: 5, padding: '0 5px',
              letterSpacing: '0.3px', textDecoration: 'none', cursor: task.intentSource.url ? 'pointer' : 'default',
            }}
          >
            #{task.intentSource.issueNumber}
          </a>
          <span style={{ fontSize: 9, color: 'var(--dotted)', fontWeight: 600, opacity: 0.9 }}>
            github issue{task.intentSource.state === 'CLOSED' ? ' · closed' : ''}
          </span>
          {(task.intentSource.labels || []).slice(0, 3).map(l => (
            <span key={l} style={{
              fontSize: 9, color: 'var(--text-muted)',
              border: '1px solid var(--border)', borderRadius: 5, padding: '0 4px',
            }}>{l}</span>
          ))}
        </div>
      )}

      {task.intentSource?.provider === 'github' && onReproduce && (
        <ReproStrip task={task} busy={reproBusy} onReproduce={onReproduce} onFocusFile={onFocusFile} />
      )}

      {/* Prompt tag — the chapter this card belongs to (e.g. "P12 · Polish prompt
          story"). Cards from one prompt repeat the same tag, which groups them. */}
      {task.programId && (
        <div title={`Program: ${task.programTitle || 'Program'} · slice: ${task.sliceTitle || task.promptTitle || ''}`}
          style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 3, fontSize: 9,
                   color: 'var(--accent)', fontWeight: 600 }}>
          <span style={{ flexShrink: 0, border: '1px solid color-mix(in srgb, var(--accent) 40%, var(--border))',
            background: 'color-mix(in srgb, var(--accent) 10%, transparent)', borderRadius: 5, padding: '0 5px' }}>
            ▣ {task.programTitle || 'Program'}</span>
          {task.sliceTitle && <span style={{ color: 'var(--text-muted)', overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>· {task.sliceTitle}</span>}
        </div>
      )}
      {(task.episodeTag || task.promptLabel) && (
        <div
          title={`Prompt ${task.episodeTag ? task.episodeTag + ' · ' : ''}${task.promptTitle || task.promptLabel}`}
          style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 4 }}
        >
          {task.episodeTag && (
            <span style={{
              flexShrink: 0, fontSize: 9, fontWeight: 700, color: 'var(--accent-orange)',
              background: 'var(--accent-orange-soft)', border: '1px solid var(--accent-orange-border)',
              borderRadius: 5, padding: '0 5px', letterSpacing: '0.3px',
            }}>{task.episodeTag}</span>
          )}
          <span style={{
            fontSize: 10, color: 'var(--accent-orange)', fontWeight: 600,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>{task.promptTitle || task.promptLabel}</span>
        </div>
      )}

      {/* Title + verification badge. Intent-step cards carry a small violet marker so it
          reads as "this is the <step> step of the sketch" — the title IS the step label. */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 6, marginBottom: 3 }}>
        <span style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.4, fontWeight: 500 }}>
          {task.source === 'intent-graph' && (
            <span style={{
              display: 'inline-block', verticalAlign: 'middle', marginRight: 5,
              fontSize: 9, fontWeight: 700, letterSpacing: '0.3px', color: 'var(--accent)',
              background: 'color-mix(in srgb, var(--accent) 12%, transparent)',
              border: '1px solid color-mix(in srgb, var(--accent) 40%, var(--border))',
              borderRadius: 5, padding: '0 5px', whiteSpace: 'nowrap',
            }}>intent step</span>
          )}
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

      {/* Why this phase is blocked/failed (e.g. a provider timeout) — the real reason, on the card. */}
      {task.blockedReason && (
        <div title={task.blockedReason} style={{ fontSize: 10, color: 'var(--violation)', marginBottom: 5, lineHeight: 1.4 }}>
          ⛔ {task.blockedReason.length > 90 ? task.blockedReason.slice(0, 90) + '…' : task.blockedReason}
        </div>
      )}

      {/* Linked box chips + the landed-commit chip (evidence). Clicking the commit
          opens its impact/diff on the canvas via the existing commit spotlight. */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, marginBottom: 5, minHeight: 16 }}>
        {sha && (
          <span
            onClick={onCommitClick ? e => onCommitClick(e, task.commitSha) : undefined}
            title={onCommitClick ? `Open commit ${sha} on canvas` : `Commit ${sha}`}
            style={{
              fontSize: 10, fontFamily: 'ui-monospace, monospace',
              color: 'var(--solid)', background: 'rgba(61,186,110,0.10)',
              border: '1px solid rgba(61,186,110,0.3)', padding: '1px 6px', borderRadius: 99,
              cursor: onCommitClick ? 'pointer' : 'default',
              display: 'inline-flex', alignItems: 'center', gap: 3,
            }}
          >
            ⎇ {sha}{task.files?.length ? ` · ${task.files.length}f` : ''}
          </span>
        )}
        {/* Verify Gate receipts — what made this safe to land (or didn't). */}
        {task.verify?.status === 'failed' && (
          <span title="Verification failed — open the episode card for the evidence" style={{
            fontSize: 9.5, color: 'var(--violation)', background: 'rgba(227,51,51,0.10)',
            border: '1px solid rgba(227,51,51,0.3)', padding: '1px 6px', borderRadius: 99,
            fontWeight: 600,
          }}>
            verify failed
          </span>
        )}
        {task.verify?.status === 'passed' && (task.verify.checks || []).map(ch => (
          <span key={ch.id} title={`${ch.label}: ${ch.status}`} style={{
            fontSize: 9.5, padding: '1px 6px', borderRadius: 99,
            color: ch.status === 'passed' ? 'var(--solid)' : 'var(--text-muted)',
            background: ch.status === 'passed' ? 'rgba(61,186,110,0.08)' : 'transparent',
            border: `1px solid ${ch.status === 'passed' ? 'rgba(61,186,110,0.25)' : 'var(--border)'}`,
          }}>
            {verifyShort(ch)} {ch.status === 'passed' ? '✓' : '✕'}
          </span>
        ))}
        {task.verify?.status === 'skipped' && (
          <span title="No verification configured when this landed" style={{
            fontSize: 9.5, color: 'var(--text-muted)', border: '1px solid var(--border)',
            padding: '1px 6px', borderRadius: 99, opacity: 0.75,
          }}>
            no verify
          </span>
        )}
        {/* Ready-for-PR (deterministic gate) — only until the PR exists. */}
        {!task.pr?.number && task.prReady && (
          <span title="Deterministic gate: landed, clean tree, checks passed, not on base — ready to ship" style={{
            fontSize: 9.5, fontWeight: 600, color: 'var(--solid)',
            border: '1px dashed rgba(61,186,110,0.45)', background: 'rgba(61,186,110,0.06)',
            padding: '1px 6px', borderRadius: 99,
          }}>
            ready for PR
          </span>
        )}
        {/* Land-as-PR evidence — the episode's pull request. */}
        {task.pr?.number && (
          <a
            href={task.pr.url || undefined}
            target="_blank" rel="noreferrer"
            onClick={e => e.stopPropagation()}
            title={`Pull request #${task.pr.number} — open on GitHub`}
            style={{
              fontSize: 9.5, fontWeight: 700, fontFamily: 'ui-monospace, monospace',
              color: 'var(--accent)', background: 'rgba(124,111,247,0.10)',
              border: '1px solid rgba(124,111,247,0.3)', padding: '1px 6px',
              borderRadius: 99, textDecoration: 'none',
            }}
          >
            PR #{task.pr.number}
          </a>
        )}
        {task.linkedBoxIds.length === 0 && !sha ? (
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
