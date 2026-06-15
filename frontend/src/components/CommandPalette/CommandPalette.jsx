import { useState, useEffect, useRef } from 'react'

export default function CommandPalette({
  isOpen, onClose,
  canvasState, dispatch,
  theme, toggleTheme,
  activeView, setActiveView,
  setPanelMode,
  pmDispatch,
  backendAvailable,
  onGenerateFromRepo,
  onGenerateSpec,
  onExecute,
  onInjectFailure,
  onGenerateReport,
  backends = [],
  activeBackend = 'openfde-native',
  onSetBackend,
  onOpenAgentSettings,
  onOpenSemanticGraph,
  onOpenPlugins,
}) {
  const [query, setQuery]           = useState('')
  const [highlighted, setHighlighted] = useState(0)
  const inputRef = useRef(null)

  // Reset + autofocus on open — all state changes are deferred to avoid
  // synchronous setState-in-effect (react-hooks/set-state-in-effect rule)
  useEffect(() => {
    if (!isOpen) return
    const t = setTimeout(() => {
      setQuery('')
      setHighlighted(0)
      inputRef.current?.focus()
    }, 0)
    return () => clearTimeout(t)
  }, [isOpen])

  const isEmpty = canvasState.boxes.length === 0

  function createBox(boxType) {
    // Place near the visual center of the canvas panel
    const canvasW = Math.max(400, window.innerWidth - 520)
    const canvasH = Math.max(300, window.innerHeight - 42)
    const x = Math.round(canvasW / 2 - 100)
    const y = Math.round(canvasH / 2 - 65)
    dispatch({ type: 'CREATE_BOX', x, y, boxType })
    setActiveView('whiteboard')
    onClose()
  }

  // ── Command definitions ────────────────────────────────────────────
  const commands = [
    {
      id: 'create-task', group: 'Create', label: 'Create task',
      hint: 'add to To Do',
      icon: <IconTask />,
      action: () => {
        const sel = [...canvasState.selectedIds]
        pmDispatch?.({
          type: 'CREATE_TASK',
          title: 'New task',
          column: 'todo',
          linkedBoxIds: sel.length === 1 ? sel : [],
        })
        setActiveView('pm')
        onClose()
      },
    },
    {
      id: 'new-dotted', group: 'Create', label: 'New dotted box',
      hint: 'agent-editable',
      icon: <IconDotted />,
      action: () => createBox('dotted'),
    },
    {
      id: 'new-solid', group: 'Create', label: 'New solid box',
      hint: 'protected',
      icon: <IconSolid />,
      action: () => createBox('solid'),
    },
    {
      id: 'load-self-map', group: 'Create', label: 'Load OpenFDE self-map',
      hint: isEmpty ? '6 boxes + 6 arrows' : 'canvas not empty',
      icon: <IconSelfMap />,
      disabled: !isEmpty,
      action: () => {
        dispatch({ type: 'LOAD_SELF_MAP' })
        setActiveView('whiteboard')
        onClose()
      },
    },
    {
      id: 'scan-repo', group: 'Create', label: 'Generate canvas from repo',
      hint: backendAvailable ? 'scan repo → modules as boxes' : 'backend unavailable',
      icon: <IconScanRepo />,
      disabled: !backendAvailable,
      action: () => {
        onGenerateFromRepo?.()
        onClose()
      },
    },
    {
      id: 'execute', group: 'Compile', label: 'Execute selected architecture',
      hint: backendAvailable ? 'compile → Agent chat' : 'backend unavailable',
      icon: <IconExecute />,
      disabled: !backendAvailable,
      action: () => {
        onExecute?.()
        setActiveView('whiteboard')
        setPanelMode?.('Agent')
        onClose()
      },
    },
    {
      id: 'gen-spec', group: 'Compile', label: 'Generate implementation spec',
      hint: backendAvailable ? 'canvas selection → markdown spec (debug)' : 'backend unavailable',
      icon: <IconSpec />,
      disabled: !backendAvailable,
      action: () => {
        onGenerateSpec?.()
        onClose()
      },
    },
    {
      id: 'gen-report', group: 'Compile', label: 'Generate REPORT.md',
      hint: backendAvailable ? 'roll up memory + commits → REPORT.md' : 'backend unavailable',
      icon: <IconReport />,
      disabled: !backendAvailable,
      action: () => {
        onGenerateReport?.()
        onClose()
      },
    },
    {
      id: 'inject-failure', group: 'Debug', label: 'Inject failure trace (dev)',
      hint: backendAvailable ? 'mark selected node/edge as failed' : 'backend unavailable',
      icon: <IconFailure />,
      disabled: !backendAvailable,
      action: () => {
        onInjectFailure?.()
        setActiveView('whiteboard')
        onClose()
      },
    },
    {
      id: 'clear-selection', group: 'Canvas', label: 'Clear selection',
      hint: '',
      icon: <IconClear />,
      action: () => { dispatch({ type: 'CLEAR_SELECTION' }); onClose() },
    },
    {
      id: 'view-canvas', group: 'View', label: 'Switch to Canvas',
      hint: activeView === 'whiteboard' ? 'active' : '',
      icon: <IconCanvas />,
      action: () => { setActiveView('whiteboard'); onClose() },
    },
    {
      id: 'view-timeline', group: 'View', label: 'Switch to Timeline',
      hint: activeView === 'timeline' ? 'active' : '',
      icon: <IconTimeline />,
      action: () => { setActiveView('timeline'); onClose() },
    },
    {
      id: 'view-pm', group: 'View', label: 'OpenPM',
      hint: activeView === 'pm' ? 'active' : 'work items by prompt',
      icon: <IconPM />,
      action: () => { setActiveView('pm'); onClose() },
    },
    {
      id: 'open-ledger', group: 'View', label: 'Open project ledger',
      hint: 'project.md',
      icon: <IconLedger />,
      action: () => { setPanelMode?.('Ledger'); onClose() },
    },
    {
      id: 'toggle-theme', group: 'Settings', label: 'Toggle theme',
      hint: theme === 'dark' ? 'switch to light' : 'switch to dark',
      icon: <IconTheme dark={theme === 'dark'} />,
      action: () => { toggleTheme(); onClose() },
    },
    {
      id: 'agent-settings', group: 'Settings', label: 'Open Agent Settings',
      hint: 'assign providers to Architect / Senior Dev / Verifier',
      icon: <IconAgentSettings />,
      action: () => { onOpenAgentSettings?.(); onClose() },
    },
    {
      id: 'semantic-graph', group: 'Settings', label: 'Architecture evidence',
      hint: 'semantic graph: tethers, counts, provider runs',
      icon: <IconAgentSettings />,
      action: () => { onOpenSemanticGraph?.(); onClose() },
    },
    {
      id: 'plugins', group: 'Settings', label: 'Plugins',
      hint: 'capability providers + activation for this repo',
      icon: <IconPlugin />,
      action: () => { onOpenPlugins?.(); onClose() },
    },
    // Execution backend selector (Step 19)
    ...(backends || []).map(b => ({
      id: `backend-${b.id}`, group: 'Backend',
      label: `${b.id === activeBackend ? '✓ ' : ''}${b.label}`,
      hint: b.id === activeBackend ? 'active execution backend' : (b.description || '').slice(0, 44),
      icon: <IconBackend active={b.id === activeBackend} />,
      disabled: !backendAvailable || b.id === activeBackend,
      action: () => { onSetBackend?.(b.id); onClose() },
    })),
  ]

  // ── Filtering + ranking ────────────────────────────────────────────
  // Label matches outrank group matches so e.g. "canvas" surfaces
  // "Switch to Canvas" before "Clear selection" (group=Canvas).
  function rankScore(c, q) {
    const lbl = c.label.toLowerCase()
    if (lbl === q)               return 0   // exact label
    if (lbl.startsWith(q))       return 1   // label prefix
    if (lbl.includes(q))         return 2   // label contains
    if ((c.hint  || '').toLowerCase().includes(q)) return 3  // hint
    if (c.group.toLowerCase().includes(q))         return 4  // group
    return Infinity
  }

  const q = query.toLowerCase().trim()

  const filteredCmds = q
    ? commands
        .map(c => ({ c, score: rankScore(c, q) }))
        .filter(({ score }) => score < Infinity)
        .sort((a, b) => a.score - b.score)
        .map(({ c }) => c)
    : commands

  // Boxes only appear when the user types a query
  const filteredBoxes = q
    ? canvasState.boxes.filter(b => b.title.toLowerCase().includes(q))
    : []

  // Flat navigable list (disabled commands skipped from keyboard nav)
  const activatableCmds  = filteredCmds.filter(c => !c.disabled)
  const activatableCount = activatableCmds.length

  // Build index map: command id → activatable index
  const cmdIndexMap = new Map()
  activatableCmds.forEach((c, i) => cmdIndexMap.set(c.id, i))

  const totalResults = activatableCount + filteredBoxes.length

  // ── Keyboard handling ──────────────────────────────────────────────
  function onKeyDown(e) {
    if (e.key === 'Escape') { onClose(); return }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlighted(Math.min(effectiveHL + 1, totalResults - 1))
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlighted(Math.max(effectiveHL - 1, 0))
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      if (effectiveHL < activatableCount) {
        activatableCmds[effectiveHL]?.action()
      } else {
        const box = filteredBoxes[effectiveHL - activatableCount]
        if (box) {
          dispatch({ type: 'SELECT', id: box.id })
          setActiveView('whiteboard')
          setPanelMode?.('Inspector')
          onClose()
        }
      }
    }
  }

  // Clamp highlight to valid range (derived at render time — avoids setState-in-effect)
  const effectiveHL = Math.min(highlighted, Math.max(0, totalResults - 1))

  // Collect ordered groups from filtered commands
  const groups = []
  const seenGroups = new Set()
  filteredCmds.forEach(c => {
    if (!seenGroups.has(c.group)) { seenGroups.add(c.group); groups.push(c.group) }
  })

  if (!isOpen) return null

  const noResults = filteredCmds.length === 0 && filteredBoxes.length === 0

  return (
    <>
      {/* Backdrop */}
      <div className="cmd-backdrop" onPointerDown={onClose} />

      {/* Palette */}
      <div
        className="cmd-palette"
        role="dialog"
        aria-modal="true"
        onPointerDown={e => e.stopPropagation()}
      >
        {/* Input row */}
        <div className="cmd-input-wrap">
          <IconSearch />
          <input
            ref={inputRef}
            className="cmd-input"
            placeholder="Type a command or search boxes…"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            spellCheck={false}
            autoComplete="off"
          />
          <kbd className="cmd-esc-hint">esc</kbd>
        </div>

        {/* Results */}
        <div className="cmd-results">

          {noResults && (
            <div className="cmd-no-results">No results for &ldquo;{query}&rdquo;</div>
          )}

          {/* Commands by group */}
          {groups.map(group => {
            const cmds = filteredCmds.filter(c => c.group === group)
            return (
              <div key={group}>
                <div className="cmd-group-label">{group}</div>
                {cmds.map(cmd => {
                  const aidx = cmdIndexMap.get(cmd.id) ?? -1
                  const isHl = !cmd.disabled && aidx === effectiveHL
                  return (
                    <div
                      key={cmd.id}
                      className={`cmd-item${isHl ? ' hl' : ''}${cmd.disabled ? ' disabled' : ''}`}
                      onPointerEnter={() => !cmd.disabled && setHighlighted(aidx)}
                      onPointerDown={() => !cmd.disabled && cmd.action()}
                    >
                      <span className="cmd-icon">{cmd.icon}</span>
                      <span className="cmd-label">{cmd.label}</span>
                      {cmd.hint && <span className="cmd-hint">{cmd.hint}</span>}
                    </div>
                  )
                })}
              </div>
            )
          })}

          {/* Box search results */}
          {filteredBoxes.length > 0 && (
            <div>
              <div className="cmd-group-label">Boxes</div>
              {filteredBoxes.map((b, i) => {
                const aidx = activatableCount + i
                const isHl = aidx === effectiveHL
                return (
                  <div
                    key={b.id}
                    className={`cmd-item${isHl ? ' hl' : ''}`}
                    onPointerEnter={() => setHighlighted(aidx)}
                    onPointerDown={() => {
                      dispatch({ type: 'SELECT', id: b.id })
                      setActiveView('whiteboard')
                      setPanelMode?.('Inspector')
                      onClose()
                    }}
                  >
                    <span className="cmd-icon"><IconBoxType type={b.type} /></span>
                    <span className="cmd-label">{b.title}</span>
                    <span className="cmd-hint">{b.type}</span>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </>
  )
}

/* ─── Inline icons ────────────────────────────────────────────────── */
function IconSearch() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" style={{ flexShrink: 0, opacity: 0.4 }}>
      <circle cx="5.5" cy="5.5" r="3.5" stroke="currentColor" strokeWidth="1.3"/>
      <line x1="8.5" y1="8.5" x2="11.5" y2="11.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  )
}

function IconDotted() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="10" height="10" rx="1.5"
        stroke="var(--dotted)" strokeWidth="1.3" strokeDasharray="2.5 1.8"/>
    </svg>
  )
}

function IconSolid() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="10" height="10" rx="1.5"
        stroke="var(--solid)" strokeWidth="1.3"/>
    </svg>
  )
}

function IconSelfMap() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1" y="1" width="4" height="4" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="8" y="1" width="4" height="4" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="4.5" y="8" width="4" height="4" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <line x1="5" y1="5" x2="6.5" y2="8" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="10" y1="5" x2="8.5" y2="8" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
    </svg>
  )
}

function IconClear() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <line x1="2" y1="2" x2="11" y2="11" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="11" y1="2" x2="2" y2="11" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  )
}

function IconCanvas() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="2.5" width="10" height="8" rx="1" stroke="currentColor" strokeWidth="1.3"/>
      <line x1="1.5" y1="5" x2="11.5" y2="5" stroke="currentColor" strokeWidth="1"/>
    </svg>
  )
}

function IconTimeline() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <line x1="1" y1="6.5" x2="12" y2="6.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <circle cx="3.5" cy="6.5" r="1.3" fill="currentColor"/>
      <circle cx="6.5" cy="6.5" r="1.3" fill="currentColor"/>
      <circle cx="9.5" cy="6.5" r="1.3" fill="currentColor"/>
    </svg>
  )
}

function IconPM() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="2" width="2.5" height="9" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="5.25" y="2" width="2.5" height="6" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="9" y="2" width="2.5" height="7.5" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
    </svg>
  )
}

function IconTheme({ dark }) {
  if (dark) {
    return (
      <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
        <circle cx="6.5" cy="6.5" r="2.2" stroke="currentColor" strokeWidth="1.2"/>
        <line x1="6.5" y1="1" x2="6.5" y2="2.2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
        <line x1="6.5" y1="10.8" x2="6.5" y2="12" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
        <line x1="1" y1="6.5" x2="2.2" y2="6.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
        <line x1="10.8" y1="6.5" x2="12" y2="6.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
      </svg>
    )
  }
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M10.5 8A4.5 4.5 0 015 2.5a4.5 4.5 0 100 9 4.5 4.5 0 005.5-3.5z"
        stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}

function IconTask() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="10" height="10" rx="1" stroke="currentColor" strokeWidth="1.2"/>
      <line x1="4" y1="5" x2="9" y2="5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
      <line x1="4" y1="7.5" x2="7.5" y2="7.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
    </svg>
  )
}

function IconScanRepo() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1" y="1" width="5" height="5" rx="0.8" stroke="var(--dotted)" strokeWidth="1.2"/>
      <rect x="7" y="1" width="5" height="5" rx="0.8" stroke="var(--dotted)" strokeWidth="1.2"/>
      <rect x="4" y="7" width="5" height="5" rx="0.8" stroke="var(--dotted)" strokeWidth="1.2"/>
      <line x1="6.5" y1="6" x2="6.5" y2="7" stroke="var(--dotted)" strokeWidth="1" strokeLinecap="round"/>
    </svg>
  )
}

function IconSpec() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="10" height="10" rx="1" stroke="var(--accent)" strokeWidth="1.2"/>
      <line x1="3.5" y1="4.5" x2="9.5" y2="4.5" stroke="var(--accent)" strokeWidth="1" strokeLinecap="round"/>
      <line x1="3.5" y1="6.5" x2="9.5" y2="6.5" stroke="var(--accent)" strokeWidth="1" strokeLinecap="round"/>
      <line x1="3.5" y1="8.5" x2="7" y2="8.5" stroke="var(--accent)" strokeWidth="1" strokeLinecap="round"/>
    </svg>
  )
}

function IconExecute() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <circle cx="6.5" cy="6.5" r="5.3" stroke="var(--accent)" strokeWidth="1.2"/>
      <path d="M5.2 4.3 L9 6.5 L5.2 8.7 Z" fill="var(--accent)" stroke="none"/>
    </svg>
  )
}

function IconFailure() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <circle cx="6.5" cy="6.5" r="5.3" stroke="var(--violation)" strokeWidth="1.2"/>
      <line x1="4.6" y1="4.6" x2="8.4" y2="8.4" stroke="var(--violation)" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="8.4" y1="4.6" x2="4.6" y2="8.4" stroke="var(--violation)" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  )
}

function IconReport() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M3 1.5h5l2.5 2.5v7.5H3z" stroke="var(--solid)" strokeWidth="1.2" strokeLinejoin="round"/>
      <line x1="4.5" y1="6" x2="9" y2="6" stroke="var(--solid)" strokeWidth="1"/>
      <line x1="4.5" y1="8" x2="9" y2="8" stroke="var(--solid)" strokeWidth="1"/>
      <line x1="4.5" y1="10" x2="7" y2="10" stroke="var(--solid)" strokeWidth="1"/>
    </svg>
  )
}

function IconBackend({ active }) {
  const c = active ? 'var(--accent)' : 'currentColor'
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="2" width="10" height="3.2" rx="1" stroke={c} strokeWidth="1.2"/>
      <rect x="1.5" y="7" width="10" height="3.2" rx="1" stroke={c} strokeWidth="1.2"/>
      <circle cx="3.6" cy="3.6" r="0.7" fill={c}/>
      <circle cx="3.6" cy="8.6" r="0.7" fill={c}/>
    </svg>
  )
}

function IconAgentSettings() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <circle cx="6.5" cy="6.5" r="2" stroke="var(--accent)" strokeWidth="1.2"/>
      <path d="M6.5 1v1.6M6.5 10.4V12M1 6.5h1.6M10.4 6.5H12M2.6 2.6l1.1 1.1M9.3 9.3l1.1 1.1M10.4 2.6 9.3 3.7M3.7 9.3 2.6 10.4"
        stroke="var(--accent)" strokeWidth="1.1" strokeLinecap="round"/>
    </svg>
  )
}

function IconPlugin() {
  // A puzzle-piece nub — the "pluggable capability" mark.
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M2 4.2h2.1a1.1 1.1 0 1 1 2.1 0H8.3V6.3a1.1 1.1 0 1 1 0 2.1v2.2H6.2a1.1 1.1 0 1 0-2.1 0H2V8.4a1.1 1.1 0 1 0 0-2.1z"
        stroke="var(--accent)" strokeWidth="1.1" strokeLinejoin="round"/>
    </svg>
  )
}

function IconLedger() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M3 1.5 h6 a1 1 0 0 1 1 1 v8 a1 1 0 0 1 -1 1 h-6 a1 1 0 0 1 -1 -1 v-8 a1 1 0 0 1 1 -1 z"
        stroke="currentColor" strokeWidth="1.1"/>
      <line x1="4" y1="4" x2="8" y2="4" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="4" y1="6" x2="8" y2="6" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
      <line x1="4" y1="8" x2="6.5" y2="8" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
    </svg>
  )
}

function IconBoxType({ type }) {
  if (type === 'solid') {
    return (
      <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
        <rect x="1.5" y="1.5" width="10" height="10" rx="1.5"
          stroke="var(--solid)" strokeWidth="1.3"/>
      </svg>
    )
  }
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="10" height="10" rx="1.5"
        stroke="var(--dotted)" strokeWidth="1.3" strokeDasharray="2.5 1.8"/>
    </svg>
  )
}
