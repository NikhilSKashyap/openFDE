import { useState, useEffect, useRef } from 'react'

// ── Seed events ───────────────────────────────────────────────────────
// Timestamps anchor to a fixed project start so the rail always looks
// populated even before the user generates any live events.
const BASE = new Date('2026-05-20T09:00:00.000Z').getTime()
const ts   = (mins) => new Date(BASE + mins * 60000).toISOString()

const SEED_EVENTS = [
  {
    id: 'sd01', timestamp: ts(0),   rail: 'design', type: 'box_created',
    detail: 'frontend/ (dotted)',
    payload: { boxType: 'dotted' },
  },
  {
    id: 'sd02', timestamp: ts(4),   rail: 'design', type: 'box_created',
    detail: 'backend/ (dotted)',
    payload: { boxType: 'dotted' },
  },
  {
    id: 'sd03', timestamp: ts(11),  rail: 'code',   type: 'commit',
    detail: 'scaffold: vite react app',
    payload: {},
  },
  {
    id: 'sd04', timestamp: ts(18),  rail: 'code',   type: 'file_changed',
    detail: 'App.jsx',
    payload: {},
  },
  {
    id: 'sd05', timestamp: ts(22),  rail: 'design', type: 'permission_changed',
    detail: 'dotted → solid',
    payload: { direction: 'lock', module: 'frontend/' },
  },
  {
    id: 'sd06', timestamp: ts(30),  rail: 'design', type: 'arrow_created',
    detail: 'frontend/ → whiteboard canvas',
    payload: {},
  },
  {
    id: 'sd07', timestamp: ts(42),  rail: 'code',   type: 'commit',
    detail: 'feat: SVG canvas + box interactions',
    payload: {},
  },
  {
    id: 'sd08', timestamp: ts(51),  rail: 'design', type: 'task_moved',
    detail: 'Whiteboard canvas  todo → doing',
    payload: { title: 'Whiteboard canvas', from: 'todo', to: 'doing' },
  },
  {
    id: 'sd09', timestamp: ts(68),  rail: 'code',   type: 'commit',
    detail: 'feat: arrow drawing + connection ports',
    payload: {},
  },
  {
    id: 'sd10', timestamp: ts(75),  rail: 'design', type: 'task_moved',
    detail: 'Connection ports + arrows  doing → done',
    payload: { title: 'Connection ports + arrows', from: 'doing', to: 'done' },
  },
  {
    id: 'sd11', timestamp: ts(80),  rail: 'design', type: 'permission_changed',
    detail: 'dotted → solid',
    payload: { direction: 'lock', module: 'whiteboard canvas' },
  },
  {
    id: 'sd12', timestamp: ts(90),  rail: 'code',   type: 'commit',
    detail: 'feat: right panel + self-map',
    payload: {},
  },
  {
    id: 'sd13', timestamp: ts(104), rail: 'design', type: 'arrow_created',
    detail: 'canvas state → right panel',
    payload: {},
  },
  {
    id: 'sd14', timestamp: ts(115), rail: 'code',   type: 'commit',
    detail: 'feat: command palette ⌘K',
    payload: {},
  },
  {
    id: 'sd15', timestamp: ts(120), rail: 'design', type: 'task_moved',
    detail: 'Command palette ⌘K  testing → done',
    payload: { title: 'Command palette ⌘K', from: 'testing', to: 'done' },
  },
  {
    id: 'sd16', timestamp: ts(130), rail: 'code',   type: 'commit',
    detail: 'feat: OpenPM board + kanban drag',
    payload: {},
  },
  {
    id: 'sd17', timestamp: ts(135), rail: 'design', type: 'task_moved',
    detail: 'OpenPM board  todo → doing',
    payload: { title: 'OpenPM board', from: 'todo', to: 'doing' },
  },
  {
    id: 'sd18', timestamp: ts(140), rail: 'code',   type: 'commit',
    detail: 'feat: Timeline + event log',
    payload: {},
  },
]

// ── Story summary generator ───────────────────────────────────────────
// Turns raw event data into a one-sentence human-readable build story.
function makeSummary(evt) {
  const { type, detail, payload = {} } = evt
  switch (type) {
    case 'box_created': {
      if (detail?.includes('self-map'))
        return 'Self-map loaded — 6 architecture boxes and data-flow arrows seeded from the project template.'
      const mod = detail?.split('(')[0]?.trim() || 'new module'
      const kind = payload.boxType || (detail?.includes('dotted') ? 'dotted' : detail?.includes('solid') ? 'solid' : '')
      if (kind === 'dotted')
        return `Agent-editable module "${mod}" placed on the canvas — free zone for active iteration.`
      if (kind === 'solid')
        return `Protected module "${mod}" added — agent must request permission before editing.`
      return `Module "${mod}" added to the architecture canvas.`
    }
    case 'arrow_created':
      return `Data flow drawn: ${detail || 'new connection'}. Architecture dependency recorded.`
    case 'arrow_deleted':
      return `Data flow removed — ${detail || 'connection'} deleted from the architecture graph.`
    case 'permission_changed': {
      const { direction, module: mod } = payload
      if (direction === 'lock' || detail?.includes('→ solid') || detail?.includes('locked'))
        return `${mod ? `"${mod}"` : 'Module'} locked to solid — agent will request permission before any further edits.`
      if (direction === 'unlock' || detail?.includes('→ dotted') || detail?.includes('unlocked'))
        return `${mod ? `"${mod}"` : 'Module'} unlocked to dotted — agent can now freely modify linked files.`
      return `Permission boundary changed: ${detail || 'box type toggled'}.`
    }
    case 'task_moved': {
      const { title = 'Task', from, to } = payload
      if (to === 'testing') return `"${title}" moved to Testing — awaiting verification before it can ship.`
      if (to === 'done')    return `"${title}" shipped — verification passed and task marked Done.`
      if (to === 'doing')   return `"${title}" is now in active development.`
      if (to === 'todo')    return `"${title}" moved back to To Do.`
      return `"${title}" moved ${from ?? '?'} → ${to ?? '?'}.`
    }
    case 'commit':
      return `Code committed to repo: ${detail || 'changes saved'}.`
    case 'file_changed':
      return `${detail || 'File'} modified during active development.`
    case 'box_edited':
      return `Module content edited: ${detail || 'title or prompt updated'}.`
    case 'archgraph_generated': {
      const n = payload.moduleCount ?? 0
      const e = payload.edgeCount   ?? 0
      return `Repo scanned by OpenArchitect — ${n} module${n !== 1 ? 's' : ''} and ${e} import edge${e !== 1 ? 's' : ''} discovered.`
    }
    case 'spec_generated': {
      const n = payload.boxCount  ?? 0
      const f = payload.fileCount ?? 0
      if (payload.via === 'execute')
        return `Execution prompt compiled for ${n} module${n !== 1 ? 's' : ''} and ${f} file${f !== 1 ? 's' : ''}.`
      return `Implementation spec compiled — ${n} module${n !== 1 ? 's' : ''} and ${f} file${f !== 1 ? 's' : ''} included.`
    }
    case 'run_started':
      return payload.detail || `Execution run started — ${payload.boxCount ?? 0} module(s) in scope.`
    case 'run_passed':
      return 'Execution run passed — scoped modules completed (placeholder execution visualization).'
    case 'run_failed':
      return payload.detail || 'Execution run failed — see the trace for the failing node.'
    case 'commit_created':
      return payload.detail || `Committed ${payload.shortSha || ''}: ${payload.summary || 'changes'}`.trim()
    case 'report_generated':
      return payload.detail || 'REPORT.md generated from project memory, commits, and runs.'
    case 'workflow_prepared':
      return payload.detail || `Workflow prepared (${payload.backend || 'claude-code-workflow'}) — handed to Claude Code, not auto-run.`
    case 'workflow_result_received':
      return payload.detail || `Workflow result received (${payload.status || '?'}).`
    case 'workflow_passed':
      return payload.detail || 'Workflow passed — changes reconciled into OpenFDE.'
    case 'workflow_failed':
      return payload.detail || 'Workflow failed — no commit made.'
    case 'workflow_needs_approval':
      return payload.detail || 'Workflow needs approval — protected scope gated.'
    case 'approval_resolved':
      return payload.detail || `Approval ${payload.decision || 'resolved'}.`
    case 'council_started':
      return payload.detail || 'Agent Council started.'
    case 'council_stage': {
      const role = { architect: 'Architect', sr_dev: 'Senior Dev', verifier: 'Verifier' }[payload.role] || payload.role
      const att = payload.attempt > 1 ? ` (attempt ${payload.attempt})` : ''
      return `${role}${att} — ${payload.status}${payload.detail ? `: ${payload.detail}` : ''}`
    }
    default:
      return detail || type.replace(/_/g, ' ')
  }
}

// ── Event metadata ────────────────────────────────────────────────────
const TYPE_PILL = {
  box_created:        'box+',
  box_edited:         'edit',
  arrow_created:      'flow+',
  arrow_deleted:      'flow−',
  permission_changed: 'lock',
  task_moved:         'task',
  commit:             'commit',
  file_changed:       'file',
  archgraph_generated: 'scan',
  spec_generated:      'spec',
  run_started:         'run',
  run_passed:          'pass',
  run_failed:          'fail',
  commit_created:      'commit',
  report_generated:    'report',
  workflow_prepared:   'workflow',
  workflow_result_received: 'result',
  workflow_passed:     'pass',
  workflow_failed:     'fail',
  workflow_needs_approval: 'approval',
  approval_resolved:   'approval',
  council_started:     'council',
  council_stage:       'council',
}

const RAIL_COLOR = {
  design: 'var(--dotted)',
  code:   'var(--solid)',
}

function eventColor(evt) {
  if (evt.live) return 'var(--accent)'
  return RAIL_COLOR[evt.rail] ?? 'var(--text-muted)'
}

// ── Normalise raw App designEvent → display event ─────────────────────
function normalise(raw) {
  const base = {
    id:             raw.id,
    timestamp:      raw.timestamp,
    rail:           'design',
    live:           true,
    payload:        raw.payload ?? {},
    projectEntryId: raw.projectEntryId ?? null,
  }
  switch (raw.type) {
    case 'task_moved':
      return { ...base, type: 'task_moved',         detail: `${raw.payload?.title}  ${raw.payload?.from} → ${raw.payload?.to}` }
    case 'box_created':
      return { ...base, type: 'box_created',         detail: raw.payload?.detail ?? `new ${raw.payload?.boxType ?? 'dotted'} box` }
    case 'arrow_created':
      return { ...base, type: 'arrow_created',       detail: raw.payload?.detail ?? 'new connection' }
    case 'arrow_deleted':
      return { ...base, type: 'arrow_deleted',       detail: raw.payload?.detail ?? 'connection removed' }
    case 'permission_changed':
      return { ...base, type: 'permission_changed',  detail: raw.payload?.detail ?? 'box type toggled', payload: { ...base.payload, direction: raw.payload?.detail?.includes('→ solid') ? 'lock' : 'unlock' } }
    default:
      return { ...base, type: raw.type, detail: raw.payload?.detail ?? '' }
  }
}

// ── Timeline ──────────────────────────────────────────────────────────
function commitToEvent(c) {
  return {
    id: `commit:${c.sha}`, timestamp: c.timestamp, rail: 'code', type: 'commit',
    detail: c.summary, sha: c.sha, shortSha: c.shortSha, author: c.author, isGit: true,
  }
}

export default function Timeline({
  designEvents = [],
  canvasState,
  canvasDispatch,
  setActiveView,
  setPanelMode,
  tasks = [],
  setSelectedTaskId,
  gitCommits = [],
  onSelectCommit,
}) {
  // Real data: live design/run/spec events + real git commits on the code rail.
  // The commit_created notification is dropped — the commit itself is the dot.
  const liveEvents   = [...designEvents].filter(e => e.type !== 'commit_created').reverse().map(normalise)
  const commitEvents = (gitCommits || []).map(commitToEvent)
  const realEvents   = [...liveEvents, ...commitEvents]
    .sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''))
  // Fall back to the seeded demo story only when there is no real data at all.
  const allEvents = realEvents.length > 0 ? realEvents : SEED_EVENTS
  const total     = allEvents.length

  const [playhead, setPlayhead] = useState(total - 1)
  const [playing,  setPlaying]  = useState(false)
  const lenRef = useRef(total)
  const allEventsRef = useRef(allEvents)
  useEffect(() => { lenRef.current = total })
  useEffect(() => { allEventsRef.current = allEvents })

  // Clamp whenever total grows
  const ph = Math.min(Math.max(0, playhead), total - 1)

  // Playback — 1.5 s per step; stops at end. When the playhead lands on a
  // commit, surface that commit's diff in the right panel (story playback).
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => {
      setPlayhead(p => {
        const next = p + 1
        if (next >= lenRef.current) { setPlaying(false); return lenRef.current - 1 }
        const ev = allEventsRef.current[next]
        if (ev?.type === 'commit' && ev.sha) onSelectCommit?.(ev.sha)
        return next
      })
    }, 1500)
    return () => clearInterval(id)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing])

  function togglePlay() {
    if (playing) { setPlaying(false); return }
    if (ph >= total - 1) setPlayhead(0)
    setPlaying(true)
  }

  function handleEventClick(idx, evt) {
    setPlayhead(idx)
    // Commit → open the diff inspector in the right panel.
    if (evt.type === 'commit' && evt.sha) { onSelectCommit?.(evt.sha); return }
    // Live task events with a taskId → open OpenPM + Task mode
    if (evt.type === 'task_moved' && evt.live && evt.payload?.taskId) {
      const task = tasks.find(t => t.id === evt.payload.taskId)
      if (task) {
        setSelectedTaskId?.(evt.payload.taskId)
        setPanelMode?.('Task')
        setActiveView('pm')
      }
    }
  }

  function handleBoxChip(boxId) {
    canvasDispatch?.({ type: 'SELECT', id: boxId })
    setActiveView('whiteboard')
    setPanelMode?.('Inspector')
  }

  function handleTaskChip(e, taskId) {
    e.stopPropagation()
    setSelectedTaskId?.(taskId)
    setPanelMode?.('Task')
    setActiveView('pm')
  }

  // Step 14: events linked to a project.md ledger entry can open the ledger.
  function handleLedgerChip(e) {
    e.stopPropagation()
    setPanelMode?.('Ledger')
  }

  const designRail = allEvents.filter(e => e.rail === 'design')
  const codeRail   = allEvents.filter(e => e.rail === 'code')
  const liveCount  = allEvents.filter(e => e.live).length

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: '16px 20px' }}>

      {/* ── Header ────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexShrink: 0 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.6px' }}>
          Timeline
        </span>

        <button
          onClick={togglePlay}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            padding: '3px 10px',
            background: playing ? 'rgba(124,111,247,0.12)' : 'var(--accent)',
            color: playing ? 'var(--accent)' : '#fff',
            border: `1px solid ${playing ? 'rgba(124,111,247,0.4)' : 'transparent'}`,
            borderRadius: 'var(--radius-sm)', fontSize: 11,
            cursor: 'pointer', fontFamily: 'inherit',
            transition: 'background 0.12s, color 0.12s',
          }}
        >
          {playing ? '⏸ Pause' : '▶ Play'}
        </button>

        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {ph + 1} / {total}
          {liveCount > 0 && <span style={{ color: 'var(--accent)', marginLeft: 8 }}>· {liveCount} live</span>}
        </span>

        <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--text-muted)' }}>
          {designRail.length} design · {codeRail.length} code
        </span>
      </div>

      {/* ── Dual rail ─────────────────────────────────────────────── */}
      <div style={{
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius)', padding: '10px 12px', marginBottom: 14, flexShrink: 0,
      }}>
        <RailRow label="Design" events={designRail} allEvents={allEvents} playhead={ph} onDotClick={setPlayhead} />
        <RailRow label="Code"   events={codeRail}   allEvents={allEvents} playhead={ph} onDotClick={setPlayhead} noMargin />
      </div>

      {/* ── Event list ────────────────────────────────────────────── */}
      <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 3 }}>
        {allEvents.map((evt, idx) => (
          <EventCard
            key={evt.id}
            evt={evt}
            isCurrent={idx === ph}
            canvasState={canvasState}
            tasks={tasks}
            onClick={() => handleEventClick(idx, evt)}
            onBoxChip={handleBoxChip}
            onTaskChip={handleTaskChip}
            onLedgerChip={handleLedgerChip}
          />
        ))}
      </div>
    </div>
  )
}

// ── Rail row ──────────────────────────────────────────────────────────
function RailRow({ label, events, allEvents, playhead, onDotClick, noMargin }) {
  const total     = allEvents.length
  const currentId = allEvents[playhead]?.id

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: noMargin ? 0 : 8 }}>
      <span style={{
        fontSize: 9, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px',
        color: 'var(--text-muted)', width: 36, flexShrink: 0, textAlign: 'right',
      }}>
        {label}
      </span>

      <div style={{ flex: 1, position: 'relative', height: 22 }}>
        {/* Track */}
        <div style={{
          position: 'absolute', left: 0, right: 0, top: '50%',
          height: 1, background: 'var(--border)', transform: 'translateY(-50%)',
        }} />

        {/* Event dots */}
        {events.map(evt => {
          const idx   = allEvents.findIndex(e => e.id === evt.id)
          const pct   = total > 1 ? (idx / (total - 1)) * 100 : 50
          const isCur = evt.id === currentId
          const col   = eventColor(evt)
          return (
            <div
              key={evt.id}
              title={makeSummary(evt)}
              onClick={() => onDotClick(idx)}
              style={{
                position: 'absolute',
                left: `${pct}%`, top: '50%',
                transform: 'translate(-50%, -50%)',
                width:      isCur ? 13 : 7,
                height:     isCur ? 13 : 7,
                borderRadius: '50%',
                background: col,
                border:     isCur ? '2px solid var(--bg)' : '1px solid var(--bg)',
                boxShadow:  isCur ? `0 0 0 2px ${col}` : 'none',
                cursor:     'pointer',
                transition: 'width 0.15s, height 0.15s, box-shadow 0.15s',
                zIndex:     isCur ? 2 : 1,
              }}
            />
          )
        })}
      </div>
    </div>
  )
}

// ── Event card ────────────────────────────────────────────────────────
function EventCard({ evt, isCurrent, canvasState, tasks, onClick, onBoxChip, onTaskChip, onLedgerChip }) {
  const col     = eventColor(evt)
  const time    = new Date(evt.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const pill    = TYPE_PILL[evt.type] ?? evt.type.replace(/_/g, ' ')
  const summary = makeSummary(evt)

  // Box chip — live events that carry a boxId
  const box = evt.payload?.boxId
    ? canvasState?.boxes?.find(b => b.id === evt.payload.boxId)
    : null

  // Task chip — live task_moved events with a taskId
  const task = evt.live && evt.type === 'task_moved' && evt.payload?.taskId
    ? tasks.find(t => t.id === evt.payload.taskId)
    : null

  return (
    <div
      onClick={onClick}
      style={{
        display: 'grid',
        gridTemplateColumns: '7px 38px auto 1fr auto',
        gridTemplateRows: 'auto auto',
        columnGap: 8,
        alignItems: 'start',
        padding: '7px 10px',
        background: isCurrent ? 'rgba(124,111,247,0.07)' : 'var(--surface)',
        border: `1px solid ${isCurrent ? 'rgba(124,111,247,0.35)' : 'var(--border)'}`,
        borderRadius: 'var(--radius)',
        cursor: 'pointer',
        transition: 'background 0.1s, border-color 0.1s',
      }}
    >
      {/* Col 1: rail dot — spans both rows */}
      <span style={{
        gridColumn: 1, gridRow: '1 / 3',
        alignSelf: 'center',
        width: 7, height: 7, borderRadius: '50%',
        background: col, flexShrink: 0, display: 'block', marginTop: 3,
      }} />

      {/* Col 2: time */}
      <span style={{ gridColumn: 2, gridRow: 1, color: 'var(--text-muted)', fontSize: 10, paddingTop: 1, whiteSpace: 'nowrap' }}>
        {time}
      </span>

      {/* Col 3: type pill */}
      <span style={{
        gridColumn: 3, gridRow: 1,
        fontSize: 9, padding: '1px 5px', borderRadius: 99,
        background: `${col}22`, border: `1px solid ${col}55`,
        color: col, textTransform: 'uppercase', letterSpacing: '0.3px',
        fontWeight: 700, whiteSpace: 'nowrap', alignSelf: 'center',
      }}>
        {pill}
      </span>

      {/* Col 4: summary (primary story text) */}
      <span style={{
        gridColumn: 4, gridRow: 1,
        color: 'var(--text)',
        fontWeight: isCurrent ? 500 : 400,
        fontSize: 12,
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        paddingTop: 1,
      }}>
        {summary}
      </span>

      {/* Col 5: live badge */}
      {evt.live ? (
        <span style={{
          gridColumn: 5, gridRow: 1,
          fontSize: 8, color: 'var(--accent)', textTransform: 'uppercase',
          letterSpacing: '0.5px', fontWeight: 700, whiteSpace: 'nowrap',
          paddingTop: 2,
        }}>
          live
        </span>
      ) : <span style={{ gridColumn: 5, gridRow: 1 }} />}

      {/* Row 2: chips (commit sha + box + task + ledger) */}
      {(evt.shortSha || box || task || evt.projectEntryId) && (
        <div style={{
          gridColumn: '2 / 6', gridRow: 2,
          display: 'flex', gap: 4, marginTop: 4,
        }}>
          {evt.shortSha && (
            <Chip
              label={`⎇ ${evt.shortSha}`}
              color="var(--solid)"
              alpha="rgba(61,186,110,"
              onClick={e => { e.stopPropagation(); onClick?.() }}
              title={`Open diff for commit ${evt.shortSha}`}
            />
          )}
          {box && (
            <Chip
              label={box.title}
              color={box.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)'}
              alpha={box.type === 'dotted' ? 'rgba(74,158,255,' : 'rgba(61,186,110,'}
              onClick={e => { e.stopPropagation(); onBoxChip(evt.payload.boxId) }}
              title={`Navigate to "${box.title}" on canvas`}
            />
          )}
          {task && (
            <Chip
              label={task.title}
              color="var(--accent)"
              alpha="rgba(124,111,247,"
              onClick={e => onTaskChip(e, task.id)}
              title={`Open task "${task.title}" in OpenPM`}
            />
          )}
          {evt.projectEntryId && (
            <Chip
              label="📒 ledger"
              color="var(--text-muted)"
              alpha="rgba(150,150,150,"
              onClick={e => onLedgerChip?.(e)}
              title={`Open project.md ledger (entry ${evt.projectEntryId})`}
            />
          )}
        </div>
      )}
    </div>
  )
}

function Chip({ label, color, alpha, onClick, title }) {
  return (
    <span
      onClick={onClick}
      title={title}
      style={{
        fontSize: 9, padding: '1px 6px', borderRadius: 99, cursor: 'pointer',
        color, background: `${alpha}0.10)`, border: `1px solid ${alpha}0.25)`,
        fontWeight: 500,
      }}
    >
      {label}
    </span>
  )
}
