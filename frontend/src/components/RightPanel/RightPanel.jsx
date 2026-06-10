import { useEffect, useRef, useState } from 'react'
import { generatePlanPreview } from '../../utils/planPreview'
import { getProjectMd } from '../../api/backend'

// Plan parked from the tab row (Step 28 Slice 5) — PlanMode + generatePlanPreview
// remain; the 'Plan' mode still renders if set, just no longer a primary tab.
const MODES = ['Agent', 'Inspector', 'Task', 'Spec', 'Ledger', 'Diff']

const TAB_TOOLTIPS = {
  Agent:     'Run work from selected architecture',
  Inspector: 'Edit selected box/arrow metadata',
  Task:      'Inspect or update selected OpenPM task',
  Spec:      'Inspect compiled prompt / debug output',
  Ledger:    'project.md conversation ledger',
  Diff:      'Inspect the selected git commit diff',
}

const MOCK_MESSAGES = [
  {
    id: 1, role: 'user',
    body: 'Build the OpenFDE frontend shell — IDE layout with toolbar, file tree, whiteboard, and agent panel.',
  },
  {
    id: 2, role: 'agent',
    structured: true,
    why:     'Establish the product surface before wiring real backend or agent execution.',
    what:    'frontend/ module — App.jsx, Toolbar, FileTree, Whiteboard, RightPanel components.',
    how:     'Vite + React scaffold → CSS design system → layout shell → mock data in each panel.',
    outcome: 'IDE opens at localhost, dark/light toggle works, layout ready for Step 2 interactions.',
  },
  {
    id: 3, role: 'user',
    body: 'What\'s next after the shell?',
  },
  {
    id: 4, role: 'agent',
    body: 'Step 2: whiteboard canvas — draw dotted/solid boxes, drag to move, in-place editing. Step 3: connection ports + arrow drawing. Step 4: right panel wired to selection.',
  },
]

export default function RightPanel({ selectionContext = { boxes: [], arrows: [], files: [], mode: 'none' }, canvasState = { boxes: [], arrows: [] }, dispatch, panelMode = 'Agent', setPanelMode, setActiveView, selectedTask = null, pmDispatch, specMarkdown = null, specLoading = false, onGenerateSpec = null, agentMessages = [], boxSpecs = {}, onEnterModule = null, run = null, commitDiff = null, onSubmitWorkflowResult = null, approvals = [], onResolveApproval = null, agentSettings = null, onOpenAgentSettings = null, onExplain = null, story = null }) {
  // panelMode is lifted to App so CommandPalette can force Inspector on box-search select.
  // Fallback setter for safety (should always be provided by App).
  const setMode = setPanelMode ?? (() => {})
  const mode = panelMode
  const [input, setInput] = useState('')

  // Auto-switch mode when selection changes between none ↔ something
  const prevSelMode = useRef(selectionContext.mode)
  useEffect(() => {
    const prev = prevSelMode.current
    const curr = selectionContext.mode
    prevSelMode.current = curr
    if (prev === curr) return
    if (prev === 'none' && curr !== 'none') setMode('Inspector')
    if (prev !== 'none' && curr === 'none') setMode('Agent')
    // arrow ↔ box transitions also land in Inspector
    if ((prev === 'box' || prev === 'multi-box') && curr === 'arrow') setMode('Inspector')
    if (prev === 'arrow' && (curr === 'box' || curr === 'multi-box')) setMode('Inspector')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectionContext.mode])

  return (
    <>
      <div className="right-mode-tabs">
        {MODES.map(m => (
          <button
            key={m}
            className={`mode-tab${mode === m ? ' active' : ''}`}
            onClick={() => setMode(m)}
            title={TAB_TOOLTIPS[m]}
          >
            {m}
          </button>
        ))}
      </div>

      <ContextBar selectionContext={selectionContext} />

      {mode === 'Agent'     && <AgentMode input={input} setInput={setInput} selectionContext={selectionContext} agentMessages={agentMessages} onSubmitWorkflowResult={onSubmitWorkflowResult} approvals={approvals} onResolveApproval={onResolveApproval} agentSettings={agentSettings} onOpenAgentSettings={onOpenAgentSettings} />}
      {mode === 'Inspector' && (
        <>
          {story && <StoryCard story={story} />}
          <InspectorMode selectionContext={selectionContext} dispatch={dispatch} boxSpecs={boxSpecs} onEnterModule={onEnterModule} run={run} onExplain={onExplain} />
        </>
      )}
      {mode === 'Task'      && <TaskMode selectedTask={selectedTask} pmDispatch={pmDispatch} canvasState={canvasState} dispatch={dispatch} setActiveView={setActiveView} setPanelMode={setMode} />}
      {mode === 'Plan'      && <PlanMode selectionContext={selectionContext} canvasState={canvasState} />}
      {mode === 'Spec'      && <SpecMode specMarkdown={specMarkdown} specLoading={specLoading} onGenerateSpec={onGenerateSpec} selectionContext={selectionContext} />}
      {mode === 'Ledger'    && <LedgerMode />}
      {mode === 'Diff'      && <DiffMode commitDiff={commitDiff} />}
    </>
  )
}

/* ─── Context bar ────────────────────────────────────────────────── */
function ContextBar({ selectionContext }) {
  const { mode, boxes, arrows, entity } = selectionContext

  if (mode === 'none') {
    return (
      <div className="context-bar">
        <span className="context-none">No selection — global context</span>
      </div>
    )
  }

  // Drilldown entities (module / file / function)
  if (mode === 'module' || mode === 'file' || mode === 'function') {
    const label = mode === 'function'
      ? (entity?.name ?? 'function')
      : (entity?.path ? entity.path.split('/').pop() : (entity?.name ?? mode))
    return (
      <div className="context-bar">
        <span className="context-label">{mode[0].toUpperCase() + mode.slice(1)}:</span>
        <span className="context-chip"><span style={{ fontSize: 10 }}>{label}</span></span>
      </div>
    )
  }

  if (mode === 'arrow') {
    const arrow = arrows[0]
    return (
      <div className="context-bar">
        <span className="context-label">Edge:</span>
        <span className="context-chip">
          <ArrowDot type={arrow.type} />
          {arrow.label ? arrow.label : `→ flow`}
        </span>
      </div>
    )
  }

  if (mode === 'box') {
    const box = boxes[0]
    return (
      <div className="context-bar">
        <span className="context-label">Context:</span>
        <span className="context-chip">
          <TypeDot type={box.type} />
          {box.title}
        </span>
      </div>
    )
  }

  return (
    <div className="context-bar">
      <span className="context-label">Context:</span>
      {boxes.slice(0, 3).map(b => (
        <span key={b.id} className="context-chip">
          <TypeDot type={b.type} />
          <span style={{ fontSize: 10 }}>{b.title}</span>
        </span>
      ))}
      {boxes.length > 3 && (
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>+{boxes.length - 3}</span>
      )}
    </div>
  )
}

/* ─── Inspector mode ─────────────────────────────────────────────── */
function InspectorMode({ selectionContext, dispatch, boxSpecs = {}, onEnterModule = null, run = null, onExplain = null }) {
  const { mode, boxes, arrows } = selectionContext

  if (mode === 'none') {
    return (
      <div className="panel-placeholder">
        <span>Select a box or arrow on the canvas to inspect it</span>
      </div>
    )
  }

  // Drilldown entities (Step 16) — read-only metadata panels.
  if (mode === 'module')   return <InspectorArchModule entity={selectionContext.entity} moduleType={selectionContext.moduleType} run={run} />
  if (mode === 'file')     return <InspectorArchFile key={selectionContext.entity?.id} entity={selectionContext.entity} moduleType={selectionContext.moduleType} run={run} />
  if (mode === 'function') return <InspectorArchFunction key={selectionContext.entity?.id} entity={selectionContext.entity} moduleType={selectionContext.moduleType} run={run} />

  if (mode === 'arrow') {
    return <InspectorArrow key={arrows[0].id} arrow={arrows[0]} allBoxes={selectionContext.allBoxes || []} dispatch={dispatch} run={run} />
  }

  if (mode === 'box') {
    return <InspectorSingleBox key={boxes[0].id} box={boxes[0]} dispatch={dispatch} boxSpec={boxSpecs[boxes[0].id] || null} onEnterModule={onEnterModule} run={run} onExplain={onExplain} />
  }

  return <InspectorMultiBox boxes={boxes} dispatch={dispatch} onExplain={onExplain} />
}

function InspectorSingleBox({ box, dispatch, boxSpec = null, onEnterModule = null, run = null, onExplain = null }) {
  const [title, setTitle] = useState(box.title)
  const [prompt, setPrompt] = useState(box.prompt)

  function updateTitle(val) {
    setTitle(val)
    dispatch({ type: 'UPDATE_BOX', id: box.id, fields: { title: val } })
  }

  function updatePrompt(val) {
    setPrompt(val)
    dispatch({ type: 'UPDATE_BOX', id: box.id, fields: { prompt: val } })
  }

  function toggleType() {
    dispatch({ type: 'UPDATE_BOX', id: box.id, fields: { type: box.type === 'dotted' ? 'solid' : 'dotted' } })
  }

  const fieldLabel = {
    fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
    letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: 4,
  }

  const inputBase = {
    width: '100%', background: 'var(--surface-2)', border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 12,
    fontFamily: 'inherit', padding: '5px 8px', outline: 'none', boxSizing: 'border-box',
  }

  return (
    <>
      <div className="chat-area" style={{ gap: 14 }}>
        {/* Open module interior (drilldown) — only for archgraph-derived boxes */}
        {onEnterModule && box.moduleId && (
          <button
            onClick={() => onEnterModule(box)}
            style={{
              alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 6,
              background: 'transparent', border: '1px solid var(--accent)',
              color: 'var(--accent)', borderRadius: 'var(--radius-sm)',
              padding: '4px 10px', fontSize: 11, fontFamily: 'inherit', cursor: 'pointer',
            }}
            title="Expand this module on the canvas to reveal its files"
          >
            ⤢ Expand on canvas
          </button>
        )}

        {/* Explain this module */}
        {onExplain && (
          <button className="explain-btn" onClick={() => onExplain([box.id])}>
            ✦ Explain this module
          </button>
        )}

        {/* Type toggle */}
        <div>
          <div style={fieldLabel}>Type</div>
          <button
            onClick={toggleType}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              background: 'transparent',
              border: `1px solid ${box.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)'}`,
              color: box.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)',
              borderRadius: 99, padding: '2px 9px', fontSize: 11,
              cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            <TypeDot type={box.type} />
            {box.type === 'dotted' ? 'Dotted · agent-editable' : 'Solid · requires permission'}
          </button>
        </div>

        {/* Title */}
        <div>
          <div style={fieldLabel}>Title</div>
          <input
            style={{ ...inputBase, lineHeight: 1.4 }}
            value={title}
            onChange={e => updateTitle(e.target.value)}
          />
        </div>

        {/* Prompt */}
        <div>
          <div style={fieldLabel}>Prompt</div>
          <textarea
            style={{ ...inputBase, resize: 'vertical', minHeight: 72, lineHeight: 1.5 }}
            value={prompt}
            onChange={e => updatePrompt(e.target.value)}
          />
        </div>

        {/* Linked files */}
        <div>
          <div style={fieldLabel}>Linked files</div>
          {box.linkedFiles && box.linkedFiles.length > 0 ? (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 2 }}>
              {box.linkedFiles.map(f => (
                <span key={f} className="context-chip" style={{ fontSize: 10 }}>
                  {f.split('/').pop()}
                </span>
              ))}
            </div>
          ) : (
            <span style={{ fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic' }}>None linked</span>
          )}
        </div>

        {/* Permission */}
        <div>
          <div style={fieldLabel}>Permission</div>
          <div style={{ fontSize: 12, color: box.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)', lineHeight: 1.5 }}>
            {box.type === 'dotted'
              ? 'Agent can modify linked files without asking'
              : 'Agent must request permission before edits'}
          </div>
        </div>

        {/* Status */}
        <div>
          <div style={fieldLabel}>Status</div>
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{box.status || 'draft'}</span>
        </div>

        {/* Acceptance criteria placeholder */}
        <div>
          <div style={fieldLabel}>Acceptance criteria</div>
          <span style={{ fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic' }}>None defined</span>
        </div>

        {/* Story / prompt provenance */}
        <BoxStory boxSpec={boxSpec} fieldLabel={fieldLabel} />

        {/* Live-run trace (Step 17) */}
        <TraceSection id={box.id} run={run} />
      </div>
      <div className="input-area">
        <textarea className="chat-input" placeholder="Describe a change to this module…" rows={1} />
        <button className="send-btn">↑</button>
      </div>
    </>
  )
}

/* ─── Box story (Step 15 prompt provenance) ──────────────────────── */
function BoxStory({ boxSpec, fieldLabel }) {
  const history = boxSpec?.promptHistory ?? []

  if (!boxSpec || history.length === 0) {
    return (
      <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12, marginTop: 2 }}>
        <div style={fieldLabel}>Story</div>
        <span style={{ fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic', lineHeight: 1.5 }}>
          No story yet. Execute against this box to start its history.
        </span>
      </div>
    )
  }

  const recent = history.slice(0, 3)
  const entryCount = boxSpec.linkedEntryIds?.length ?? 0
  const eventCount = boxSpec.linkedEventIds?.length ?? 0

  const sub = {
    fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
    letterSpacing: '0.4px', color: 'var(--text-muted)', marginBottom: 2, marginTop: 8,
  }

  return (
    <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12, marginTop: 2 }}>
      <div style={fieldLabel}>Story</div>

      {boxSpec.currentIntent && (
        <>
          <div style={sub}>Current intent</div>
          <div style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.5 }}>{boxSpec.currentIntent}</div>
        </>
      )}

      {boxSpec.latestPromptFragment && (
        <>
          <div style={sub}>Latest prompt</div>
          <div style={{
            fontSize: 12, color: 'var(--text)', lineHeight: 1.5,
            paddingLeft: 8, borderLeft: '2px solid var(--accent)',
          }}>
            {boxSpec.latestPromptFragment}
          </div>
        </>
      )}

      <div style={sub}>History (last {recent.length})</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {recent.map(h => (
          <div key={h.id} style={{
            fontSize: 11, lineHeight: 1.45, color: 'var(--text)',
            background: 'var(--surface-2)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius-sm)', padding: '5px 7px',
          }}>
            <div>{h.promptFragment || '(no fragment)'}</div>
            <div style={{ fontSize: 9, color: 'var(--text-muted)', marginTop: 3, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {typeof h.confidence === 'number' && <span>conf {h.confidence.toFixed(2)}</span>}
              {h.outcome && <span>· {h.outcome}</span>}
              {h.fullPromptRef && <span title="project.md ledger entry">· {h.fullPromptRef}</span>}
            </div>
          </div>
        ))}
      </div>

      {(entryCount > 0 || eventCount > 0) && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 8 }}>
          {entryCount > 0 && <span>{entryCount} ledger entry(ies)</span>}
          {entryCount > 0 && eventCount > 0 && <span> · </span>}
          {eventCount > 0 && <span>{eventCount} event(s)</span>}
        </div>
      )}
    </div>
  )
}

function InspectorMultiBox({ boxes, dispatch, onExplain = null }) {
  const dottedCount = boxes.filter(b => b.type === 'dotted').length
  const solidCount  = boxes.length - dottedCount

  const fieldLabel = {
    fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
    letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: 4,
  }

  const bulkBtn = {
    flex: 1, padding: '5px 8px', background: 'var(--surface-2)',
    border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
    color: 'var(--text-muted)', fontSize: 11, fontFamily: 'inherit',
    cursor: 'pointer',
  }

  return (
    <div className="chat-area" style={{ gap: 14 }}>
      {/* Count summary */}
      <div>
        <div style={fieldLabel}>Selected</div>
        <div style={{ fontSize: 12, color: 'var(--text)' }}>
          {boxes.length} boxes
          {dottedCount > 0 && <span style={{ color: 'var(--dotted)' }}> · {dottedCount} dotted</span>}
          {solidCount  > 0 && <span style={{ color: 'var(--solid)'  }}> · {solidCount} solid</span>}
        </div>
      </div>

      {/* Module chips */}
      <div>
        <div style={fieldLabel}>Modules</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 2 }}>
          {boxes.map(b => (
            <span key={b.id} className="context-chip" style={{ fontSize: 11 }}>
              <TypeDot type={b.type} />
              {b.title}
            </span>
          ))}
        </div>
      </div>

      {/* Explain */}
      {onExplain && (
        <button className="explain-btn" onClick={() => onExplain(boxes.map(b => b.id))}>
          ✦ Explain these {boxes.length} modules
        </button>
      )}

      {/* Bulk actions */}
      <div>
        <div style={fieldLabel}>Bulk actions</div>
        <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
          <button style={{ ...bulkBtn, color: 'var(--solid)', borderColor: 'rgba(61,186,110,0.3)' }}
            onClick={() => dispatch({ type: 'FREEZE_SELECTED' })}>
            Make solid
          </button>
          <button style={{ ...bulkBtn, color: 'var(--dotted)', borderColor: 'rgba(74,158,255,0.3)' }}
            onClick={() => dispatch({ type: 'MAKE_SELECTED_DOTTED' })}>
            Make dotted
          </button>
        </div>
      </div>
    </div>
  )
}

/* Function-level dataflow read-out for a selected arrow (Step 23). */
function ArrowDataflow({ arrow, fieldLabel }) {
  const flows = arrow.flows || []
  const edgeType = arrow.edgeType || (arrow.flowCount > 0 ? 'dataflow' : 'manual')
  // Manual arrows (drawn by hand) carry none of this metadata — show nothing.
  if (!arrow.edgeType && !arrow.flowCount && flows.length === 0) return null

  const typeLabel = edgeType === 'dataflow' ? 'dataflow'
    : edgeType === 'import' ? 'import (fallback)' : 'manual'
  const typeColor = edgeType === 'dataflow' ? 'var(--accent)' : 'var(--text-muted)'

  return (
    <div>
      <div style={fieldLabel}>Dataflow</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 11 }}>
        <span style={{
          border: `1px solid ${typeColor}`, color: typeColor,
          borderRadius: 99, padding: '1px 8px', fontWeight: 600,
        }}>{typeLabel}</span>
        {arrow.flowCount > 0 && (
          <span style={{ color: 'var(--text-muted)' }}>
            {arrow.flowCount} function flow{arrow.flowCount !== 1 ? 's' : ''}
          </span>
        )}
        {arrow.confidence && <span style={{ color: 'var(--text-muted)' }}>· {arrow.confidence} confidence</span>}
      </div>

      {flows.length > 0 ? (
        <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {flows.map((fw, i) => (
            <div key={fw.id || i} style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.45 }}>
              <span style={{ fontFamily: 'monospace', color: 'var(--text)' }}>{fw.label}</span>
              <span style={{ marginLeft: 6 }}>
                {(fw.fromFile || '').split('/').pop()} → {(fw.toFile || '').split('/').pop()} · {fw.confidence}
              </span>
            </div>
          ))}
          {arrow.flowCount > flows.length && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
              +{arrow.flowCount - flows.length} more…
            </div>
          )}
        </div>
      ) : edgeType === 'import' ? (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4, lineHeight: 1.5 }}>
          No function-level flows resolved — falling back to an import dependency.
        </div>
      ) : null}
    </div>
  )
}

function InspectorArrow({ arrow, allBoxes, dispatch, run = null }) {
  const [label, setLabel] = useState(arrow.label || '')

  function updateLabel(val) {
    setLabel(val)
    dispatch({ type: 'UPDATE_ARROW', id: arrow.id, fields: { label: val } })
  }

  function toggleType() {
    dispatch({ type: 'UPDATE_ARROW', id: arrow.id, fields: { type: arrow.type === 'dotted' ? 'solid' : 'dotted' } })
  }

  const fromBox = allBoxes.find(b => b.id === arrow.fromBox)
  const toBox   = allBoxes.find(b => b.id === arrow.toBox)

  const fieldLabel = {
    fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
    letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: 4,
  }

  const inputBase = {
    width: '100%', background: 'var(--surface-2)', border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 12,
    fontFamily: 'inherit', padding: '5px 8px', outline: 'none', boxSizing: 'border-box',
  }

  const arrowColor = arrow.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)'

  return (
    <>
      <div className="chat-area" style={{ gap: 14 }}>
        {/* From → To */}
        <div>
          <div style={fieldLabel}>Connection</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span style={{ color: 'var(--text)', fontWeight: 500 }}>
              {fromBox?.title ?? '(deleted)'}
            </span>
            <svg width="20" height="10" viewBox="0 0 20 10" style={{ flexShrink: 0 }}>
              <path d="M 0 5 L 16 5" stroke={arrowColor} strokeWidth={1.5}
                strokeDasharray={arrow.type === 'dotted' ? '4 2' : undefined} />
              <path d="M 13 2 L 19 5 L 13 8" fill={arrowColor} stroke="none" />
            </svg>
            <span style={{ color: 'var(--text)', fontWeight: 500 }}>
              {toBox?.title ?? '(deleted)'}
            </span>
          </div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 3 }}>
            {arrow.fromPort?.toUpperCase()} port → {arrow.toPort?.toUpperCase()} port
          </div>
        </div>

        {/* Function-level dataflow (Step 23) */}
        <ArrowDataflow arrow={arrow} fieldLabel={fieldLabel} />

        {/* Type toggle */}
        <div>
          <div style={fieldLabel}>Type</div>
          <button
            onClick={toggleType}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              background: 'transparent',
              border: `1px solid ${arrowColor}`,
              color: arrowColor,
              borderRadius: 99, padding: '2px 9px', fontSize: 11,
              cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            <ArrowDot type={arrow.type} />
            {arrow.type === 'dotted' ? 'Dotted · agent-editable flow' : 'Solid · requires permission'}
          </button>
        </div>

        {/* Label */}
        <div>
          <div style={fieldLabel}>Label</div>
          <input
            style={{ ...inputBase, lineHeight: 1.4 }}
            value={label}
            placeholder="e.g. if true:, returns data, triggers…"
            onChange={e => updateLabel(e.target.value)}
          />
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4, lineHeight: 1.5 }}>
            Shown on the arrow at its midpoint.
            Use <span style={{ fontFamily: 'monospace', color: 'var(--text)' }}>if &lt;condition&gt;:</span> to annotate conditional flows.
          </div>
        </div>

        {/* Permission */}
        <div>
          <div style={fieldLabel}>Permission</div>
          <div style={{ fontSize: 12, color: arrowColor, lineHeight: 1.5 }}>
            {arrow.type === 'dotted'
              ? 'Agent can freely modify data flowing through this edge'
              : 'Agent must request permission before modifying this flow'}
          </div>
        </div>

        {/* Live-run trace (Step 17) */}
        <TraceSection id={arrow.id} run={run} />
      </div>
      <div className="input-area">
        <textarea className="chat-input" placeholder="Describe a change to this edge…" rows={1} />
        <button className="send-btn">↑</button>
      </div>
    </>
  )
}

/* ─── Drilldown inspectors (Step 16) ─────────────────────────────── */
const _ddLabel = {
  fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
  letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: 4,
}
const _ddValue = { fontSize: 12, color: 'var(--text)', lineHeight: 1.5, wordBreak: 'break-all' }

function PermBadge({ type }) {
  const dotted = type !== 'solid'
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      border: `1px solid ${dotted ? 'var(--dotted)' : 'var(--solid)'}`,
      color: dotted ? 'var(--dotted)' : 'var(--solid)',
      borderRadius: 99, padding: '2px 9px', fontSize: 11,
    }}>
      <TypeDot type={dotted ? 'dotted' : 'solid'} />
      {dotted ? 'Inherited: dotted · editable' : 'Inherited: solid · protected'}
    </span>
  )
}

function InspectorArchModule({ entity, moduleType, run = null }) {
  if (!entity) return <div className="panel-placeholder"><span>No module selected</span></div>
  return (
    <div className="chat-area" style={{ gap: 14 }}>
      <div><div style={_ddLabel}>Module</div><div style={{ ..._ddValue, fontWeight: 600 }}>{entity.name}</div></div>
      <div><div style={_ddLabel}>Path</div><div style={_ddValue}>{entity.path || '—'}</div></div>
      <div><div style={_ddLabel}>Permission</div><PermBadge type={moduleType || entity.type} /></div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.5 }}>
        Open the module on the canvas to browse its files, then a file to see its functions.
      </div>
      <TraceSection id={entity.id} run={run} />
    </div>
  )
}

function InspectorArchFile({ entity, moduleType, run = null }) {
  if (!entity) return <div className="panel-placeholder"><span>No file selected</span></div>
  const name = entity.path ? entity.path.split('/').pop() : entity.id
  const size = typeof entity.size === 'number'
    ? (entity.size >= 1024 ? `${(entity.size / 1024).toFixed(1)} KB` : `${entity.size} B`)
    : '—'
  return (
    <div className="chat-area" style={{ gap: 14 }}>
      <div><div style={_ddLabel}>File</div><div style={{ ..._ddValue, fontWeight: 600 }}>{name}</div></div>
      <div><div style={_ddLabel}>Path</div><div style={_ddValue}>{entity.path}</div></div>
      <div style={{ display: 'flex', gap: 18 }}>
        <div><div style={_ddLabel}>Language</div><div style={_ddValue}>{entity.language || '—'}</div></div>
        <div><div style={_ddLabel}>Size</div><div style={_ddValue}>{size}</div></div>
      </div>
      <div><div style={_ddLabel}>Permission</div><PermBadge type={moduleType} /></div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.5 }}>
        Double-click the file box to open it and inspect its functions.
      </div>
      <TraceSection id={`box:file:${entity.path}`} run={run} />
    </div>
  )
}

function InspectorArchFunction({ entity, moduleType, run = null }) {
  if (!entity) return <div className="panel-placeholder"><span>No function selected</span></div>
  const args = entity.args || []
  const sigArgs = args.map(a => (a.type ? `${a.name}: ${a.type}` : a.name)).join(', ')
  const mono = { fontFamily: 'ui-monospace, monospace', fontSize: 11 }
  return (
    <div className="chat-area" style={{ gap: 14 }}>
      <div>
        <div style={_ddLabel}>Function</div>
        <div style={{ ...mono, color: 'var(--text)', lineHeight: 1.5 }}>
          {entity.name}({sigArgs}){entity.returns ? ` → ${entity.returns}` : ''}
        </div>
      </div>

      <div>
        <div style={_ddLabel}>Purpose</div>
        {entity.purpose
          ? <div style={_ddValue}>{entity.purpose}</div>
          : <div style={{ fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic' }}>No docstring summary.</div>}
      </div>

      <div>
        <div style={_ddLabel}>Arguments</div>
        {args.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {args.map((a, i) => (
              <div key={i} style={{ ...mono, color: 'var(--text)' }}>
                {a.name}{a.type ? <span style={{ color: 'var(--text-muted)' }}>: {a.type}</span> : null}
              </div>
            ))}
          </div>
        ) : <div style={{ fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic' }}>none</div>}
      </div>

      <div style={{ display: 'flex', gap: 18 }}>
        <div><div style={_ddLabel}>Returns</div><div style={{ ...mono, color: 'var(--text)' }}>{entity.returns || '—'}</div></div>
        <div><div style={_ddLabel}>Location</div><div style={_ddValue}>{entity.path}{typeof entity.line === 'number' ? `:${entity.line}` : ''}</div></div>
      </div>

      {entity.warnings && entity.warnings.length > 0 && (
        <div>
          <div style={_ddLabel}>Warnings</div>
          {entity.warnings.map((w, i) => (
            <div key={i} style={{ fontSize: 11, color: 'var(--violation)', lineHeight: 1.5 }}>⚠ {w}</div>
          ))}
        </div>
      )}

      <div><div style={_ddLabel}>Permission</div><PermBadge type={moduleType} /></div>
      <TraceSection id={`box:function:${entity.path}:${entity.name}`} run={run} />
    </div>
  )
}

/* ─── Live-run trace (Step 17) ───────────────────────────────────── */
function TraceSection({ id, run }) {
  if (!run || !id) return null
  const status = run.nodeStates?.[id] || run.edgeStates?.[id] || null
  const failure = run.failures?.[id] || null
  const events = run.trace?.[id] || []
  if (!status && !failure && events.length === 0) return null

  const statusColor = {
    planning: 'var(--text-muted)', running: 'var(--active)',
    passed: 'var(--solid)', failed: 'var(--violation)', active: 'var(--active)',
  }[status] || 'var(--text-muted)'

  const lbl = {
    fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
    letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: 4, marginTop: 8,
  }

  return (
    <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12, marginTop: 2 }}>
      <div style={{ ...lbl, marginTop: 0, color: status === 'failed' ? 'var(--violation)' : 'var(--text-muted)' }}>
        Trace{run.runId ? ` · ${run.runId}` : ''}
      </div>

      {status && (
        <div style={{ marginBottom: 6 }}>
          <span style={{
            display: 'inline-block', fontSize: 11, fontWeight: 600,
            color: statusColor, border: `1px solid ${statusColor}`,
            borderRadius: 99, padding: '1px 9px', textTransform: 'capitalize',
          }}>{status}</span>
        </div>
      )}

      {failure && (
        <>
          <div style={lbl}>Error</div>
          <div style={{ fontSize: 11, color: 'var(--violation)', lineHeight: 1.5, wordBreak: 'break-word' }}>
            {typeof failure.errorSummary === 'string' ? failure.errorSummary : <PayloadView value={failure.errorSummary} />}
          </div>
          <div style={lbl}>Input summary</div>
          <PayloadView value={failure.inputSummary} />
          <div style={lbl}>Output / intermediate summary</div>
          <PayloadView value={failure.outputSummary} />
        </>
      )}

      <div style={lbl}>Events ({events.length})</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {events.slice(-8).map((e, i) => (
          <div key={i} style={{ fontSize: 10, color: 'var(--text-muted)', display: 'flex', gap: 6 }}>
            <span style={{ fontFamily: 'ui-monospace, monospace' }}>{(e.timestamp || '').slice(11, 19)}</span>
            <span style={{ color: e.status === 'failed' ? 'var(--violation)' : 'var(--text)' }}>{e.type}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/* Compact, safe renderer for a summarized + redacted trace payload. */
function PayloadView({ value }) {
  if (value === undefined || value === null) {
    return <div style={{ fontSize: 11, color: 'var(--text-muted)', fontStyle: 'italic' }}>—</div>
  }
  let text
  try { text = typeof value === 'string' ? value : JSON.stringify(value, null, 1) }
  catch { text = String(value) }
  if (text.length > 1200) text = text.slice(0, 1200) + '\n… (capped)'
  return (
    <pre style={{
      fontFamily: 'ui-monospace, monospace', fontSize: 10, lineHeight: 1.45,
      color: 'var(--text)', background: 'var(--surface-2)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-sm)', padding: '6px 8px', margin: '2px 0 0',
      maxHeight: 140, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
    }}>{text}</pre>
  )
}

/* ─── Agent mode ─────────────────────────────────────────────────── */
/* ─── Role status row (Step 21) ──────────────────────────────────── */
const ROLE_ROW = [
  { id: 'architect',  short: 'Arch' },
  { id: 'senior_dev', short: 'Dev'  },
  { id: 'verifier',   short: 'Verify' },
]

// Provider-driven status chip (no "mode" axis — transport is the provider).
function roleStatusTone(cfg) {
  if (!cfg) return { tone: 'muted', label: '—' }
  if (cfg.enabled === false) return { tone: 'muted', label: 'off' }
  if (cfg.provider === 'codex-local') return { tone: 'ok', label: 'codex' }
  if (cfg.provider === 'claude-code-local') return { tone: 'ok', label: 'claude code' }
  if (cfg.provider === 'echo') return { tone: 'ok', label: 'echo' }
  // Hosted API providers: need a model + key.
  if (!cfg.model) return { tone: 'warn', label: 'no model' }
  if (!cfg.hasApiKey) return { tone: 'warn', label: 'no key' }
  return { tone: 'ok', label: cfg.provider }
}

function RoleStatusRow({ agentSettings, onOpenAgentSettings }) {
  return (
    <div className="role-status-row">
      <div className="role-status-chips">
        {ROLE_ROW.map(r => {
          const st = roleStatusTone(agentSettings?.[r.id])
          return (
            <span key={r.id} className="role-chip" title={`${r.short}: ${st.label}`}>
              <span className={`role-dot ${st.tone}`} />
              <span className="role-chip-name">{r.short}</span>
              <span className="role-chip-val">{st.label}</span>
            </span>
          )
        })}
      </div>
      <button className="role-settings-btn" onClick={() => onOpenAgentSettings?.()}>
        Agent Settings
      </button>
    </div>
  )
}

function AgentMode({ input, setInput, selectionContext, agentMessages = [], onSubmitWorkflowResult = null, approvals = [], onResolveApproval = null, agentSettings = null, onOpenAgentSettings = null }) {
  const { mode, boxes, arrows, allBoxes = [] } = selectionContext
  const pendingApprovals = (approvals || []).filter(a => a.status === 'pending')
  const hasBoxCtx   = mode !== 'none' && boxes.length > 0
  const hasArrowCtx = mode === 'arrow' && arrows.length > 0

  // Generated messages take over once the user has executed; otherwise the
  // seed demo conversation is shown so the panel is never empty.
  const messages = agentMessages.length > 0 ? agentMessages : MOCK_MESSAGES

  // Chat composer is a mock for now: submit (arrow / Ctrl+Enter) clears the
  // input. Execution itself lives on the canvas Execute button, not here.
  function submit() {
    if (!input.trim()) return
    setInput('')
  }

  function handleKey(e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      submit()
    }
  }

  let placeholder = 'Ask the agent or describe a change…'
  if (hasBoxCtx)   placeholder = `Ask about ${boxes.map(b => b.title).join(', ')}…`
  if (hasArrowCtx) {
    const arrow = arrows[0]
    const fromBox = allBoxes.find(b => b.id === arrow.fromBox)
    const toBox   = allBoxes.find(b => b.id === arrow.toBox)
    placeholder = `Ask about the ${fromBox?.title ?? '?'} → ${toBox?.title ?? '?'} flow…`
  }

  return (
    <>
      <RoleStatusRow agentSettings={agentSettings} onOpenAgentSettings={onOpenAgentSettings} />
      {(hasBoxCtx || hasArrowCtx) && (
        <div style={{
          padding: '6px 10px', background: 'rgba(124,111,247,0.06)',
          borderBottom: '1px solid var(--border)',
          fontSize: 11, color: 'var(--text-muted)',
          display: 'flex', alignItems: 'center', gap: 5, flexWrap: 'wrap',
          flexShrink: 0,
        }}>
          <span style={{ color: 'var(--accent)', fontWeight: 500 }}>Active context:</span>
          {hasBoxCtx && boxes.map(b => (
            <span key={b.id} className="context-chip" style={{ fontSize: 10 }}>
              <TypeDot type={b.type} />
              <span>{b.title}</span>
            </span>
          ))}
          {hasArrowCtx && (() => {
            const arrow = arrows[0]
            const fromBox = allBoxes.find(b => b.id === arrow.fromBox)
            const toBox   = allBoxes.find(b => b.id === arrow.toBox)
            return (
              <span className="context-chip" style={{ fontSize: 10 }}>
                <ArrowDot type={arrow.type} />
                <span>{fromBox?.title ?? '?'} → {toBox?.title ?? '?'}</span>
              </span>
            )
          })()}
        </div>
      )}
      {pendingApprovals.length > 0 && (
        <div className="approvals-bar">
          {pendingApprovals.map(a => (
            <ApprovalCard key={a.approvalId} approval={a} onResolve={onResolveApproval} />
          ))}
        </div>
      )}
      <div className="chat-area">
        {messages.map(msg => <AgentMessage key={msg.id} msg={msg} onSubmitWorkflowResult={onSubmitWorkflowResult} />)}
      </div>
      <div className="input-hint">Ctrl+Enter to send · Execute lives on the canvas</div>
      <div className="input-area">
        <textarea
          className="chat-input"
          placeholder={placeholder}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKey}
          rows={1}
        />
        <button className="send-btn" onClick={submit}>↑</button>
      </div>
    </>
  )
}

/* ─── Agent message dispatcher ───────────────────────────────────── */
function AgentMessage({ msg, onSubmitWorkflowResult = null }) {
  if (msg.councilStage) return <CouncilStageMessage msg={msg} />
  if (msg.role === 'architect') return <ArchitectMessage msg={msg} onSubmitWorkflowResult={onSubmitWorkflowResult} />
  if (msg.role === 'sr_dev')    return <SrDevMessage msg={msg} />
  if (msg.role === 'result')    return <ResultMessage msg={msg} />
  if (msg.role === 'explanation') return <ExplanationMessage msg={msg} />

  // Legacy / seed messages (user, agent)
  const roleLabel = msg.role === 'user' ? 'You' : 'Agent'
  return (
    <div className={`chat-msg ${msg.role}`}>
      <div className="chat-msg-role">{roleLabel}</div>
      <div className="chat-msg-body">
        {msg.structured ? (
          <div className="structured-response">
            {[
              { label: 'Why',     body: msg.why },
              { label: 'What',    body: msg.what },
              { label: 'How',     body: msg.how },
              { label: 'Outcome', body: msg.outcome },
            ].map(s => (
              <div key={s.label}>
                <div className="sr-section-label">{s.label}</div>
                <div className="sr-section-body">{s.body}</div>
              </div>
            ))}
          </div>
        ) : msg.body}
      </div>
    </div>
  )
}

/* ─── Architect message (compiled execution prompt / prepared workflow) ── */
function ArchitectMessage({ msg, onSubmitWorkflowResult = null }) {
  const [open, setOpen] = useState(false)
  const wf = !!msg.workflow
  return (
    <div className="chat-msg architect">
      <div className="chat-msg-role">{wf ? 'OpenArchitect → Claude Code' : 'OpenArchitect'}</div>
      <div className="chat-msg-body">
        {wf && (
          <div className="workflow-badge">
            <span className="workflow-badge-dot" />
            Workflow prepared · {msg.backend} · {msg.status}
          </div>
        )}
        <div className="architect-scope">{msg.scope}</div>
        <div className="architect-stats">
          {msg.moduleCount} module{msg.moduleCount !== 1 ? 's' : ''} ·{' '}
          {msg.fileCount} file{msg.fileCount !== 1 ? 's' : ''} ·{' '}
          {msg.functionCount} function{msg.functionCount !== 1 ? 's' : ''}
          {msg.warningCount > 0 && ` · ${msg.warningCount} warning${msg.warningCount !== 1 ? 's' : ''}`}
        </div>

        <div className="architect-perm">
          {msg.dottedNames.length > 0 && (
            <div className="perm-line">
              <span className="perm-dot perm-dotted" />
              <span><strong>Direct edit:</strong> {msg.dottedNames.join(', ')}</span>
            </div>
          )}
          {msg.solidNames.length > 0 && (
            <div className="perm-line">
              <span className="perm-dot perm-solid" />
              <span><strong>Approval required:</strong> {msg.solidNames.join(', ')}</span>
            </div>
          )}
          {msg.dottedNames.length === 0 && msg.solidNames.length === 0 && (
            <div className="perm-line" style={{ color: 'var(--text-muted)' }}>
              No modules in scope — repo-level prompt.
            </div>
          )}
        </div>

        {msg.prompt && (
          <div className="architect-prompt">
            <strong>Requested:</strong> {msg.prompt}
          </div>
        )}

        <button className="spec-disclosure" onClick={() => setOpen(o => !o)}>
          {wf
            ? (open ? '▾ Hide workflow script' : '▸ View workflow script')
            : (open ? '▾ Hide compiled spec' : '▸ View compiled spec')}
        </button>
        {open && (
          <div className="architect-spec">
            <SpecRenderer markdown={msg.markdown} />
          </div>
        )}

        {wf && onSubmitWorkflowResult && (
          <SubmitResultForm workflowId={msg.workflowId} onSubmit={onSubmitWorkflowResult} />
        )}
      </div>
    </div>
  )
}

/* ─── Submit-result form (paste workflow JSON, Step 20) ──────────────── */
function SubmitResultForm({ workflowId, onSubmit }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  const sample = JSON.stringify({
    status: 'passed',
    filesChanged: [{ path: 'src/app.py', status: 'M' }],
    functionsChanged: [{ name: 'main', path: 'src/app.py' }],
    testsRun: [{ command: 'pytest', result: 'pass' }],
    verificationResult: 'pass',
    errors: [],
    suggestedCanvasUpdates: [],
    reportSummary: 'Implemented the change; tests pass.',
  }, null, 2)

  async function submit() {
    let parsed
    try { parsed = JSON.parse(text) }
    catch { setErr('Invalid JSON — paste the workflow result object.'); return }
    setBusy(true); setErr(null)
    const error = await onSubmit(workflowId, parsed)
    setBusy(false)
    if (error) { setErr(error); return }
    setOpen(false); setText('')
  }

  if (!open) {
    return (
      <button className="submit-result-btn" onClick={() => setOpen(true)}>
        ↩ Submit result
      </button>
    )
  }
  return (
    <div className="submit-result-form">
      <div className="submit-result-label">Paste the workflow JSON result</div>
      <textarea
        className="submit-result-textarea"
        value={text}
        onChange={e => setText(e.target.value)}
        placeholder={sample}
        rows={6}
        spellCheck={false}
      />
      {err && <div className="submit-result-err">{err}</div>}
      <div className="submit-result-actions">
        <button className="submit-result-go" onClick={submit} disabled={busy || !text.trim()}>
          {busy ? 'Submitting…' : 'Submit'}
        </button>
        <button className="submit-result-cancel" onClick={() => { setOpen(false); setErr(null) }}>Cancel</button>
        <button className="submit-result-fill" onClick={() => setText(sample)}>Use sample</button>
      </div>
    </div>
  )
}

/* ─── Council stage message (Architect / Sr Dev / Verifier, Step 29) ─── */
function CouncilStageMessage({ msg }) {
  const roleLabel = { architect: 'Architect', sr_dev: 'Senior Dev', verifier: 'Verifier' }[msg.role] || msg.role
  const tone = msg.status === 'passed' ? 'var(--solid)'
    : msg.status === 'failed' ? 'var(--violation)'
    : msg.status === 'needs_approval' ? 'var(--accent)'
    : msg.status === 'needs_human' ? 'var(--active)' : 'var(--text-muted)'
  return (
    <div className="chat-msg council-stage">
      <div className="chat-msg-role" style={{ color: tone }}>
        {roleLabel}{msg.attempt > 1 ? ` · attempt ${msg.attempt}` : ''} · {msg.status}
      </div>
      <div className="chat-msg-body">{msg.summary}</div>
    </div>
  )
}

/* ─── Result message (reconciled workflow outcome, Step 20) ──────────── */
function ResultMessage({ msg }) {
  const tone = msg.status === 'passed' ? 'var(--solid)'
    : msg.status === 'failed' ? 'var(--violation)' : 'var(--active)'
  const label = msg.status === 'needs_approval' ? 'Needs approval' : msg.status
  return (
    <div className="chat-msg result">
      <div className="chat-msg-role" style={{ color: tone }}>Workflow result</div>
      <div className="chat-msg-body" style={{ borderColor: tone }}>
        <div className="result-status" style={{ color: tone }}>
          <span className="result-status-dot" style={{ background: tone }} />
          {label}
        </div>
        {msg.reportSummary && <div className="result-summary">{msg.reportSummary}</div>}
        <div className="result-meta">
          {msg.verificationResult && <span>Verification: <strong>{msg.verificationResult}</strong></span>}
          {msg.testsSummary && <span>· {msg.testsSummary}</span>}
        </div>
        {msg.committed && (
          <div className="result-commit">Committed <code>{(msg.commitSha || '').slice(0, 7)}</code></div>
        )}
        {msg.status === 'failed' && (
          <div className="result-note">No commit — work was not accepted.</div>
        )}
        {msg.approval && (
          <div className="result-note" style={{ color: 'var(--active)' }}>
            Protected scope — approval requested. Resolve it above.
          </div>
        )}
      </div>
    </div>
  )
}

/* ─── Story card (Step 26 Batch 5 "Story mode") ──────────────────────── */
function StoryCard({ story }) {
  const [showTech, setShowTech] = useState(false)
  if (!story) return null
  const steps = story.steps || []
  return (
    <div className="story-card">
      <div className="story-title">{story.title}</div>
      {story.summary && <div className="story-summary">{story.summary}</div>}

      <ol className="story-steps">
        {steps.map(st => (
          <li key={st.id} className="story-step">
            <span className="story-step-badge">{st.order}</span>
            <div>
              <div className="story-step-label">{st.label}</div>
              <div className="story-step-desc">{st.description}</div>
            </div>
          </li>
        ))}
      </ol>

      <div className="story-io">
        {story.inputs?.length > 0 && (
          <div><span className="story-io-label">Inputs</span> {story.inputs.join(', ')}</div>
        )}
        {story.outputs?.length > 0 && (
          <div><span className="story-io-label">Outputs</span> {story.outputs.join(', ')}</div>
        )}
        <div className="story-confidence">Confidence: {story.confidence || 'heuristic'}</div>
      </div>

      <button className="story-tech-toggle" onClick={() => setShowTech(s => !s)}>
        {showTech ? '▾ Hide technical detail' : '▸ View technical detail'}
      </button>
      {showTech && (
        <div className="story-tech">
          {steps.map(st => (
            <div key={`tech-${st.id}`} className="story-tech-step">
              <div className="story-tech-head">{st.order}. {st.label}</div>
              {(st.nodeIds || []).map(nid => (
                <code key={nid} className="story-tech-fn">{shortFnFromId(nid)}</code>
              ))}
              <div className="story-tech-meta">{(st.flowIds || []).length} flow(s) · {(st.filePaths || []).join(', ')}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// box:function:path:name → name();  box:file:path → path
function shortFnFromId(id) {
  if (id.startsWith('box:function:')) return id.split(':').pop() + '()'
  if (id.startsWith('box:file:')) return id.slice('box:file:'.length)
  return id.replace('box:', '')
}

/* ─── Explanation message (Step 26 "Explain this") ───────────────────── */
function ExplanationMessage({ msg }) {
  return (
    <div className="chat-msg explanation">
      <div className="chat-msg-role" style={{ color: 'var(--accent)' }}>OpenArchitect · explanation</div>
      <div className="chat-msg-body">
        <SpecRenderer markdown={msg.markdown} />
        <div style={{ marginTop: 6, fontSize: 10, color: 'var(--text-muted)' }}>
          Grounded in the function-flow read model (deterministic).
        </div>
      </div>
    </div>
  )
}

/* ─── Approval card (protected-scope gate, Step 20) ──────────────────── */
function ApprovalCard({ approval, onResolve }) {
  const mods = (approval.protectedModules || []).join(', ')
  return (
    <div className="approval-card">
      <div className="approval-head">
        <span className="approval-dot" />
        Approval required {mods ? `· ${mods}` : ''}
      </div>
      {approval.requestedChange && <div className="approval-change">{approval.requestedChange}</div>}
      {(approval.protectedFiles || []).length > 0 && (
        <div className="approval-files">
          {approval.protectedFiles.slice(0, 6).map(f => <code key={f}>{f}</code>)}
        </div>
      )}
      <div className="approval-actions">
        <button className="approval-approve" onClick={() => onResolve?.(approval.approvalId, 'approved')}>Approve</button>
        <button className="approval-reject" onClick={() => onResolve?.(approval.approvalId, 'rejected')}>Reject</button>
      </div>
    </div>
  )
}

/* ─── Senior Dev message (execution placeholder / prepared workflow) ─── */
function SrDevMessage({ msg }) {
  if (msg.nativeAgent) {
    return (
      <div className="chat-msg sr-dev">
        <div className="chat-msg-role">Senior Dev · native</div>
        <div className="chat-msg-body">
          <div className="workflow-badge">
            <span className="workflow-badge-dot" />
            OpenFDE native agent
          </div>
          <div style={{ marginTop: 6 }}>{msg.summary}</div>
        </div>
      </div>
    )
  }
  if (msg.workflowPrepared) {
    return (
      <div className="chat-msg sr-dev">
        <div className="chat-msg-role">Senior Dev</div>
        <div className="chat-msg-body">
          <div>
            Workflow prepared for <code>{msg.backend}</code> — it will run
            Architect → Senior Dev → Verifier → Report. Nothing has executed yet.
          </div>
          {msg.requiresApproval ? (
            <div style={{ marginTop: 6, color: 'var(--violation)' }}>
              Protected scope requires approval before any edits: {msg.solidNames.join(', ')}.
            </div>
          ) : (
            <div style={{ marginTop: 6, color: 'var(--text-muted)' }}>
              All scoped modules are editable — no protected scope in this run.
            </div>
          )}
          <div className="sr-dev-placeholder">
            Hand the workflow script to Claude Code; results report back through OpenFDE.
          </div>
        </div>
      </div>
    )
  }
  return (
    <div className="chat-msg sr-dev">
      <div className="chat-msg-role">Senior Dev</div>
      <div className="chat-msg-body">
        {msg.requiresApproval ? (
          <>
            <div>
              Execution prepared. This scope includes protected (solid)
              module{msg.solidNames.length !== 1 ? 's' : ''}
              {msg.solidNames.length ? `: ${msg.solidNames.join(', ')}` : ''}.
              I&apos;ll request approval before modifying those files.
            </div>
            {msg.dottedNames.length > 0 && (
              <div style={{ marginTop: 6, color: 'var(--text-muted)' }}>
                Direct edits are allowed for: {msg.dottedNames.join(', ')}.
              </div>
            )}
          </>
        ) : (
          <div>
            Execution prepared. All scoped modules are dotted (agent-editable) —
            direct edits to the linked files are allowed once real execution is wired up.
          </div>
        )}
        <div className="sr-dev-placeholder">
          Awaiting agent execution backend — placeholder only, no files will be modified yet.
        </div>
      </div>
    </div>
  )
}

/* ─── Task mode ──────────────────────────────────────────────────── */
function TaskMode({ selectedTask, pmDispatch, canvasState, dispatch, setActiveView, setPanelMode }) {
  const fl = {
    fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
    letterSpacing: '0.5px', color: 'var(--text-muted)', marginBottom: 4,
  }

  if (!selectedTask) {
    return (
      <div className="panel-placeholder">
        <span>Click a task card on the OpenPM board to inspect it</span>
      </div>
    )
  }

  const colColors = {
    todo:    'var(--text-muted)',
    doing:   'var(--accent)',
    testing: 'var(--active)',
    done:    'var(--solid)',
  }
  const veriColors = {
    pending: 'var(--text-muted)',
    passed:  'var(--solid)',
    failed:  'var(--violation)',
  }

  const inputBase = {
    width: '100%', background: 'var(--surface-2)', border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 12,
    fontFamily: 'inherit', padding: '5px 8px', outline: 'none', boxSizing: 'border-box',
  }

  return (
    <div className="chat-area" style={{ gap: 14 }}>
      {/* Title */}
      <div>
        <div style={fl}>Title</div>
        <input
          style={inputBase}
          value={selectedTask.title}
          onChange={e => pmDispatch({ type: 'UPDATE_TASK', id: selectedTask.id, fields: { title: e.target.value } })}
        />
      </div>

      {/* Description */}
      <div>
        <div style={fl}>Description</div>
        <textarea
          style={{ ...inputBase, resize: 'vertical', minHeight: 60, lineHeight: 1.5 }}
          value={selectedTask.description}
          onChange={e => pmDispatch({ type: 'UPDATE_TASK', id: selectedTask.id, fields: { description: e.target.value } })}
        />
      </div>

      {/* Column */}
      <div>
        <div style={fl}>Column</div>
        <span style={{ fontSize: 12, color: colColors[selectedTask.column] || 'var(--text)', fontWeight: 500 }}>
          {selectedTask.column.charAt(0).toUpperCase() + selectedTask.column.slice(1)}
        </span>
      </div>

      {/* Verification */}
      <div>
        <div style={fl}>Verification</div>
        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
          {['pending', 'passed', 'failed'].map(s => (
            <button
              key={s}
              onClick={() => pmDispatch({ type: 'UPDATE_TASK', id: selectedTask.id, fields: { verificationStatus: s } })}
              style={{
                padding: '2px 9px', borderRadius: 99, fontSize: 11, fontFamily: 'inherit', cursor: 'pointer',
                background: selectedTask.verificationStatus === s ? 'rgba(124,111,247,0.12)' : 'transparent',
                border: `1px solid ${selectedTask.verificationStatus === s ? 'var(--accent)' : 'var(--border)'}`,
                color: selectedTask.verificationStatus === s ? veriColors[s] : 'var(--text-muted)',
              }}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {/* Linked boxes */}
      <div>
        <div style={fl}>Linked boxes</div>
        {selectedTask.linkedBoxIds.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic' }}>None linked</div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {selectedTask.linkedBoxIds.map(boxId => {
              const box = canvasState.boxes.find(b => b.id === boxId)
              if (!box) return null
              return (
                <span
                  key={boxId}
                  onClick={() => {
                    dispatch({ type: 'SELECT', id: boxId })
                    setActiveView?.('whiteboard')
                    setPanelMode?.('Inspector')
                  }}
                  title={`Navigate to "${box.title}"`}
                  style={{
                    fontSize: 11, cursor: 'pointer',
                    color: box.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)',
                    background: box.type === 'dotted' ? 'rgba(74,158,255,0.08)' : 'rgba(61,186,110,0.08)',
                    border: `1px solid ${box.type === 'dotted' ? 'rgba(74,158,255,0.2)' : 'rgba(61,186,110,0.2)'}`,
                    padding: '2px 8px', borderRadius: 99,
                  }}
                >
                  <TypeDot type={box.type} /> {box.title}
                </span>
              )
            })}
          </div>
        )}
        {/* Link currently selected box */}
        {canvasState.boxes.some(b => canvasState.selectedIds?.has(b.id) && !selectedTask.linkedBoxIds.includes(b.id)) && (
          <button
            onClick={() => {
              const [boxId] = [...canvasState.selectedIds]
              if (boxId) pmDispatch({ type: 'LINK_BOX', taskId: selectedTask.id, boxId })
            }}
            style={{
              marginTop: 6, padding: '2px 9px', background: 'transparent',
              border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
              color: 'var(--text-muted)', fontSize: 11, cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            + Link selected box
          </button>
        )}
      </div>
    </div>
  )
}

/* ─── Plan mode ──────────────────────────────────────────────────── */
function PlanMode({ selectionContext, canvasState }) {
  const { mode, boxes: selectedBoxes, arrows: selectedArrows, allBoxes = [] } = selectionContext
  const plan = generatePlanPreview({ boxes: canvasState.boxes, arrows: canvasState.arrows })

  const sectionLabel = {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
    letterSpacing: '0.6px', color: 'var(--text-muted)', marginBottom: 5,
    display: 'block',
  }
  const bullet = { fontSize: 12, color: 'var(--text)', lineHeight: 1.55, marginBottom: 3 }
  const muted  = { fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.55 }
  const mono   = { fontFamily: 'ui-monospace, monospace', fontSize: 11, color: 'var(--text)' }

  return (
    <div className="chat-area" style={{ gap: 0, padding: '8px 10px' }}>

      {/* ── Selected context (top, only when selection active) ── */}
      {(mode === 'box' || mode === 'multi-box') && selectedBoxes.length > 0 && (
        <div style={{ marginBottom: 14, paddingBottom: 12, borderBottom: '1px solid var(--border)' }}>
          <span style={sectionLabel}>Selected</span>
          {selectedBoxes.map(b => (
            <div key={b.id} style={{ marginBottom: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 2 }}>
                <TypeDot type={b.type} />
                <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--text)' }}>{b.title}</span>
                <span style={{
                  fontSize: 9, color: b.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)',
                  textTransform: 'uppercase', letterSpacing: '0.4px',
                }}>{b.type}</span>
              </div>
              {b.prompt && b.prompt !== 'Describe what this module does...' && (
                <div style={{ ...muted, fontSize: 11, paddingLeft: 13, lineHeight: 1.5 }}>
                  {b.prompt}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {mode === 'arrow' && selectedArrows.length > 0 && (
        <div style={{ marginBottom: 14, paddingBottom: 12, borderBottom: '1px solid var(--border)' }}>
          <span style={sectionLabel}>Selected edge</span>
          {selectedArrows.map(a => {
            const fromBox = allBoxes.find(b => b.id === a.fromBox)
            const toBox   = allBoxes.find(b => b.id === a.toBox)
            const color   = a.type === 'dotted' ? 'var(--dotted)' : 'var(--solid)'
            return (
              <div key={a.id} style={{ marginBottom: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 2 }}>
                  <ArrowDot type={a.type} />
                  <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--text)' }}>
                    {fromBox?.title ?? '?'} → {toBox?.title ?? '?'}
                  </span>
                  <span style={{ fontSize: 9, color, textTransform: 'uppercase', letterSpacing: '0.4px' }}>{a.type}</span>
                </div>
                {a.label && (
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', paddingLeft: 13 }}>
                    <span style={mono}>{a.label}</span>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* ── Empty state ── */}
      {plan.empty && (
        <div style={{ paddingTop: 8 }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 14, fontStyle: 'italic' }}>
            Canvas is empty — draw architecture or load the self-map.
          </div>
          <span style={sectionLabel}>Why</span>
          <div style={muted}>No modules defined yet.</div>
          <div style={{ marginTop: 14 }}>
            <span style={sectionLabel}>What</span>
            <div style={muted}>Add dotted or solid boxes to define your architecture.</div>
          </div>
          <div style={{ marginTop: 14 }}>
            <span style={sectionLabel}>Dataflow</span>
            <div style={muted}>Connect boxes with arrows to define data flows.</div>
          </div>
          <div style={{ marginTop: 14 }}>
            <span style={sectionLabel}>Outcome</span>
            <div style={muted}>0 boxes · 0 arrows</div>
          </div>
        </div>
      )}

      {/* ── Generated plan ── */}
      {!plan.empty && (
        <>
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 12, fontStyle: 'italic' }}>
            Auto-generated from canvas state
          </div>

          {/* Why */}
          <div style={{ marginBottom: 14 }}>
            <span style={sectionLabel}>Why</span>
            {plan.why.length === 0 ? (
              <div style={muted}>No module prompts defined yet.</div>
            ) : (
              plan.why.map(w => (
                <div key={w.id} style={{ marginBottom: 6 }}>
                  <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--text-muted)', marginBottom: 1 }}>{w.title}</div>
                  <div style={{ ...bullet, paddingLeft: 10, borderLeft: '2px solid var(--border)' }}>{w.prompt}</div>
                </div>
              ))
            )}
          </div>

          {/* What */}
          <div style={{ marginBottom: 14 }}>
            <span style={sectionLabel}>What</span>
            {plan.what.dotted.length > 0 && (
              <div style={{ marginBottom: 6 }}>
                <div style={{ fontSize: 10, color: 'var(--dotted)', fontWeight: 600, marginBottom: 3 }}>
                  Agent-editable
                </div>
                {plan.what.dotted.map(b => (
                  <div key={b.id} style={{ ...bullet, paddingLeft: 8, marginBottom: 2 }}>
                    · {b.title}
                    {b.files.length > 0 && (
                      <span style={{ ...mono, color: 'var(--text-muted)', marginLeft: 6 }}>
                        {b.files.map(f => f.split('/').pop()).join(', ')}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
            {plan.what.solid.length > 0 && (
              <div>
                <div style={{ fontSize: 10, color: 'var(--solid)', fontWeight: 600, marginBottom: 3 }}>
                  Protected
                </div>
                {plan.what.solid.map(b => (
                  <div key={b.id} style={{ ...bullet, paddingLeft: 8, marginBottom: 2 }}>
                    · {b.title}
                    {b.files.length > 0 && (
                      <span style={{ ...mono, color: 'var(--text-muted)', marginLeft: 6 }}>
                        {b.files.map(f => f.split('/').pop()).join(', ')}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Dataflow */}
          <div style={{ marginBottom: 14 }}>
            <span style={sectionLabel}>Dataflow</span>
            {plan.dataflow.length === 0 ? (
              <div style={muted}>No arrows defined yet.</div>
            ) : (
              plan.dataflow.map(a => (
                <div key={a.id} style={{ marginBottom: 3, display: 'flex', alignItems: 'center', gap: 5 }}>
                  <ArrowDot type={a.type} />
                  <span style={{ fontSize: 12, color: 'var(--text)' }}>
                    {a.fromTitle}
                    <span style={{ color: 'var(--text-muted)' }}> → </span>
                    {a.toTitle}
                  </span>
                  {a.label ? (
                    <span style={{ ...mono, color: 'var(--text-muted)' }}>{a.label}</span>
                  ) : (
                    <span style={{ fontSize: 10, color: 'var(--text-dim)', fontStyle: 'italic' }}>(unlabeled)</span>
                  )}
                </div>
              ))
            )}
          </div>

          {/* Permission Boundaries */}
          <div style={{ marginBottom: 14 }}>
            <span style={sectionLabel}>Permissions</span>
            {plan.permissions.agentFree.length > 0 && (
              <div style={{ marginBottom: 4 }}>
                <span style={{ fontSize: 11, color: 'var(--dotted)' }}>Agent-free: </span>
                <span style={{ fontSize: 11, color: 'var(--text)' }}>{plan.permissions.agentFree.join(', ')}</span>
              </div>
            )}
            {plan.permissions.requiresPermission.length > 0 && (
              <div>
                <span style={{ fontSize: 11, color: 'var(--solid)' }}>Requires confirmation: </span>
                <span style={{ fontSize: 11, color: 'var(--text)' }}>{plan.permissions.requiresPermission.join(', ')}</span>
              </div>
            )}
            {plan.permissions.agentFree.length === 0 && plan.permissions.requiresPermission.length === 0 && (
              <div style={muted}>No permission boundaries set.</div>
            )}
          </div>

          {/* How */}
          <div style={{ marginBottom: 14 }}>
            <span style={sectionLabel}>How</span>
            {plan.how.map((line, i) => (
              <div key={i} style={{ ...bullet, marginBottom: 3 }}>· {line}</div>
            ))}
          </div>

          {/* Outcome */}
          <div style={{ marginBottom: 14 }}>
            <span style={sectionLabel}>Outcome</span>
            <div style={muted}>
              {plan.outcome.boxCount} box{plan.outcome.boxCount !== 1 ? 'es' : ''}
              {plan.outcome.dottedCount > 0 && <span style={{ color: 'var(--dotted)' }}> · {plan.outcome.dottedCount} dotted</span>}
              {plan.outcome.solidCount  > 0 && <span style={{ color: 'var(--solid)'  }}> · {plan.outcome.solidCount} solid</span>}
              {plan.outcome.arrowCount  > 0 && (
                <span>
                  {' · '}{plan.outcome.arrowCount} arrow{plan.outcome.arrowCount !== 1 ? 's' : ''}
                  {plan.outcome.labeledArrowCount > 0 && ` (${plan.outcome.labeledArrowCount} labeled)`}
                </span>
              )}
            </div>
          </div>

          {/* Open Questions */}
          {plan.openQuestions.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <span style={{ ...sectionLabel, color: 'var(--violation)', opacity: 0.75 }}>Open Questions</span>
              {plan.openQuestions.map((q, i) => (
                <div key={i} style={{ ...muted, marginBottom: 3 }}>· {q}</div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

/* ─── Spec mode ──────────────────────────────────────────────────── */
function SpecMode({ specMarkdown, specLoading, onGenerateSpec, selectionContext }) {
  const [promptInput, setPromptInput] = useState('')
  const hasSelection = selectionContext.mode !== 'none'
  const isAvailable  = typeof onGenerateSpec === 'function'

  function handleGenerate() {
    if (isAvailable) onGenerateSpec(promptInput)
  }

  function handleKey(e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleGenerate()
    }
  }

  const inputBase = {
    width: '100%', background: 'var(--surface-2)', border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 12,
    fontFamily: 'inherit', padding: '5px 8px', outline: 'none', boxSizing: 'border-box',
  }

  return (
    <>
      {/* Prompt + generate button */}
      <div style={{
        padding: '8px 10px', borderBottom: '1px solid var(--border)',
        flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 6,
      }}>
        <div style={{
          fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
          letterSpacing: '0.5px', color: 'var(--text-muted)',
        }}>
          Requested change (optional)
        </div>
        <textarea
          style={{ ...inputBase, resize: 'none', minHeight: 48, lineHeight: 1.5 }}
          placeholder={
            hasSelection
              ? 'Describe what you want the agent to do with the selected modules…'
              : 'No selection — spec will cover the whole repo…'
          }
          value={promptInput}
          onChange={e => setPromptInput(e.target.value)}
          onKeyDown={handleKey}
          rows={2}
        />
        <button
          onClick={handleGenerate}
          disabled={!isAvailable || specLoading}
          style={{
            alignSelf: 'flex-start', padding: '4px 11px',
            background: 'transparent',
            border: `1px solid ${isAvailable ? 'var(--accent)' : 'var(--border)'}`,
            borderRadius: 'var(--radius-sm)', color: isAvailable ? 'var(--accent)' : 'var(--text-muted)',
            fontSize: 11, fontFamily: 'inherit', cursor: isAvailable ? 'pointer' : 'default',
            opacity: specLoading ? 0.6 : 1,
          }}
        >
          {specLoading ? 'Generating…' : specMarkdown ? 'Regenerate spec' : 'Generate spec'}
        </button>
        {!isAvailable && (
          <div style={{ fontSize: 10, color: 'var(--text-muted)', fontStyle: 'italic' }}>
            Backend required — run <code style={{ fontSize: 10 }}>openfde watch .</code>
          </div>
        )}
        {isAvailable && !specMarkdown && !specLoading && (
          <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
            Ctrl+Enter · compiles selection → markdown spec
          </div>
        )}
      </div>

      {/* Spec output */}
      <div className="spec-output">
        {specLoading && (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontStyle: 'italic', paddingTop: 8 }}>
            Scanning repo and compiling spec…
          </div>
        )}
        {!specLoading && !specMarkdown && (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, paddingTop: 8 }}>
            <div style={{ marginBottom: 8, fontWeight: 500 }}>
              {hasSelection ? 'Spec ready to compile.' : 'No selection — will compile repo-level spec.'}
            </div>
            <div style={{ opacity: 0.7, lineHeight: 1.6 }}>
              Select boxes or arrows on the canvas then click <strong>Generate spec</strong>.
              The output is a structured markdown document covering architecture,
              files, functions, dataflow, permission boundaries, and tasks.
            </div>
          </div>
        )}
        {!specLoading && specMarkdown && <SpecRenderer markdown={specMarkdown} />}
      </div>
    </>
  )
}

/* ─── Diff mode (git commit inspector, Step 18) ──────────────────── */
function DiffMode({ commitDiff }) {
  if (!commitDiff) {
    return (
      <div className="panel-placeholder">
        <span>Click a commit on the Timeline to inspect its diff</span>
      </div>
    )
  }
  if (commitDiff.loading) {
    return <div className="panel-placeholder"><span>Loading diff for {commitDiff.sha?.slice(0, 7)}…</span></div>
  }
  const d = commitDiff.data
  if (!d) {
    return <div className="panel-placeholder"><span>Diff unavailable for {commitDiff.sha?.slice(0, 7)}</span></div>
  }

  const when = d.timestamp ? new Date(d.timestamp).toLocaleString() : ''
  const statusColor = { A: 'var(--solid)', M: 'var(--dotted)', D: 'var(--violation)', R: 'var(--accent)' }

  return (
    <div className="diff-output">
      <div className="diff-summary">{d.summary}</div>
      <div className="diff-meta">
        <span className="diff-sha" title={d.sha}>⎇ {d.shortSha}</span>
        <span>·</span>
        <span>{d.author}</span>
        {when && <><span>·</span><span>{when}</span></>}
      </div>

      <div className="diff-stat">
        {d.stat.files} file{d.stat.files !== 1 ? 's' : ''} changed
        {d.stat.additions > 0 && <span style={{ color: 'var(--solid)' }}> · +{d.stat.additions}</span>}
        {d.stat.deletions > 0 && <span style={{ color: 'var(--violation)' }}> · −{d.stat.deletions}</span>}
      </div>

      <div className="diff-files">
        {d.files.map(f => (
          <div key={f.path} className="diff-file-row">
            <span className="diff-file-status" style={{ color: statusColor[f.status] || 'var(--text-muted)' }}>{f.status}</span>
            <span className="diff-file-path">{f.path}</span>
            <span className="diff-file-counts">
              {f.additions > 0 && <span style={{ color: 'var(--solid)' }}>+{f.additions}</span>}
              {f.deletions > 0 && <span style={{ color: 'var(--violation)', marginLeft: 4 }}>−{f.deletions}</span>}
            </span>
          </div>
        ))}
      </div>

      <div className="diff-patch-label">Patch{d.patchTruncated ? ' (truncated)' : ''}</div>
      <pre className="diff-patch">{renderPatchLines(d.patch)}</pre>
    </div>
  )
}

// Colorize a unified-diff string by line prefix.
function renderPatchLines(patch) {
  if (!patch) return null
  return patch.split('\n').map((line, i) => {
    let color = 'var(--text-muted)'
    if (line.startsWith('+') && !line.startsWith('+++')) color = 'var(--solid)'
    else if (line.startsWith('-') && !line.startsWith('---')) color = 'var(--violation)'
    else if (line.startsWith('@@')) color = 'var(--accent)'
    else if (line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('+++') || line.startsWith('---')) color = 'var(--text)'
    return <div key={i} style={{ color, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{line || ' '}</div>
  })
}

/* ─── Ledger mode (project.md preview) ───────────────────────────── */
function LedgerMode() {
  const [md, setMd]           = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr]         = useState(false)

  // Fetch on mount. setState happens only after the await, so this does not
  // trip react-hooks/set-state-in-effect.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const text = await getProjectMd()
      if (cancelled) return
      setLoading(false)
      if (text == null) setErr(true)
      else setMd(typeof text === 'string' ? text : '')
    })()
    return () => { cancelled = true }
  }, [])

  function refresh() {
    setLoading(true)
    setErr(false)
    getProjectMd().then(text => {
      setLoading(false)
      if (text == null) { setErr(true); return }
      setMd(typeof text === 'string' ? text : '')
    })
  }

  const hasEntries = !!md && md.includes('---')

  return (
    <>
      <div style={{
        padding: '8px 10px', borderBottom: '1px solid var(--border)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        flexShrink: 0,
      }}>
        <span style={{
          fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
          letterSpacing: '0.5px', color: 'var(--text-muted)',
        }}>
          project.md ledger
        </span>
        <button className="spec-disclosure" onClick={refresh}>↻ Refresh</button>
      </div>
      <div className="spec-output">
        {loading && (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontStyle: 'italic', paddingTop: 8 }}>
            Loading ledger…
          </div>
        )}
        {!loading && err && (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, paddingTop: 8 }}>
            Backend required to read the ledger — run <code style={{ fontSize: 10 }}>openfde watch .</code>
          </div>
        )}
        {!loading && !err && hasEntries && <SpecRenderer markdown={md} />}
        {!loading && !err && !hasEntries && (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, paddingTop: 8, lineHeight: 1.6 }}>
            No ledger entries yet. Press <strong>Run</strong> on the canvas to record the
            first architect / sr-dev exchange into <code style={{ fontSize: 10 }}>project.md</code>.
          </div>
        )}
      </div>
    </>
  )
}

/* ─── Simple inline markdown renderer ───────────────────────────── */
function SpecRenderer({ markdown }) {
  const lines = markdown.split('\n')

  function renderInline(text) {
    // Render **bold**, `code`, and plain text inline
    const parts = []
    let remaining = text
    let key = 0
    while (remaining.length > 0) {
      const boldIdx = remaining.indexOf('**')
      const codeIdx = remaining.indexOf('`')
      const first   = Math.min(
        boldIdx === -1 ? Infinity : boldIdx,
        codeIdx === -1 ? Infinity : codeIdx,
      )
      if (first === Infinity) {
        parts.push(<span key={key}>{remaining}</span>)
        break
      }
      if (first > 0) {
        parts.push(<span key={key++}>{remaining.slice(0, first)}</span>)
        remaining = remaining.slice(first)
        continue
      }
      if (remaining.startsWith('**')) {
        const end = remaining.indexOf('**', 2)
        if (end === -1) { parts.push(<span key={key}>{remaining}</span>); break }
        parts.push(<strong key={key++} style={{ color: 'var(--text)' }}>{remaining.slice(2, end)}</strong>)
        remaining = remaining.slice(end + 2)
      } else if (remaining.startsWith('`')) {
        const end = remaining.indexOf('`', 1)
        if (end === -1) { parts.push(<span key={key}>{remaining}</span>); break }
        parts.push(<code key={key++} className="spec-code">{remaining.slice(1, end)}</code>)
        remaining = remaining.slice(end + 1)
      }
    }
    return parts
  }

  const elements = []
  lines.forEach((line, i) => {
    if (line.startsWith('# ')) {
      elements.push(<div key={i} className="spec-h1">{line.slice(2)}</div>)
    } else if (line.startsWith('## ')) {
      elements.push(<div key={i} className="spec-h2">{line.slice(3)}</div>)
    } else if (line.startsWith('### ')) {
      elements.push(<div key={i} className="spec-h3">{line.slice(4)}</div>)
    } else if (line.startsWith('> ')) {
      elements.push(<div key={i} className="spec-blockquote">{line.slice(2)}</div>)
    } else if (line.startsWith('- ')) {
      elements.push(<div key={i} className="spec-bullet">{renderInline(line.slice(2))}</div>)
    } else if (line === '') {
      elements.push(<div key={i} style={{ height: 6 }} />)
    } else {
      elements.push(<div key={i} className="spec-paragraph">{renderInline(line)}</div>)
    }
  })

  return <>{elements}</>
}

/* ─── Helpers ────────────────────────────────────────────────────── */
function TypeDot({ type }) {
  if (type === 'solid') {
    return (
      <svg width="8" height="8" viewBox="0 0 8 8" style={{ flexShrink: 0 }}>
        <rect x="0.5" y="0.5" width="7" height="7" rx="1.5"
          stroke="var(--solid)" strokeWidth="1.2" fill="none" />
      </svg>
    )
  }
  return (
    <svg width="8" height="8" viewBox="0 0 8 8" style={{ flexShrink: 0 }}>
      <rect x="0.5" y="0.5" width="7" height="7" rx="1.5"
        stroke="var(--dotted)" strokeWidth="1.2" strokeDasharray="2 1.2" fill="none" />
    </svg>
  )
}

function ArrowDot({ type }) {
  const color = type === 'dotted' ? 'var(--dotted)' : 'var(--solid)'
  return (
    <svg width="14" height="8" viewBox="0 0 14 8" style={{ flexShrink: 0 }}>
      <path
        d="M 0 4 L 10 4"
        stroke={color}
        strokeWidth="1.2"
        strokeDasharray={type === 'dotted' ? '3 1.5' : undefined}
      />
      <path d="M 8 1.5 L 13 4 L 8 6.5" fill={color} stroke="none" />
    </svg>
  )
}
