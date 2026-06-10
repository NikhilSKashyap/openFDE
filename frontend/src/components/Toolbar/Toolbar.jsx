export default function Toolbar({ activeTool, setActiveTool, activeView, setActiveView, theme, toggleTheme, hasDottedSelected, onLockSelected, onExpandAll, onCollapseAll, onOpenCommandPalette, onHome }) {
  const tools = [
    { id: 'select', label: 'Select', icon: <IconCursor /> },
    { id: 'dotted', label: 'Dotted box', icon: <IconDottedBox /> },
    { id: 'solid',  label: 'Solid box',  icon: <IconSolidBox /> },
    { id: 'arrow',  label: 'Arrow',      icon: <IconArrow /> },
    { id: 'sarrow', label: 'Solid arrow', icon: <IconSolidArrow /> },
  ]

  return (
    <div className="toolbar">
      <button className="toolbar-brand" onClick={() => onHome?.()} title="Back to the architecture canvas">
        <img src="/logo.svg" className="brand-logo" alt="" />
        openfde
      </button>

      {/* Drawing tools */}
      <div className="toolbar-group">
        {tools.map(t => (
          <button
            key={t.id}
            className={`btn${activeTool === t.id ? ' active' : ''}`}
            onClick={() => { setActiveTool(t.id); setActiveView('whiteboard') }}
            title={t.label}
          >
            {t.icon}
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      <div className="toolbar-sep" />

      {/* Lock selected */}
      <button
        className="btn"
        title="Lock selected (freeze dotted → solid)"
        disabled={!hasDottedSelected}
        onClick={onLockSelected}
        style={{ opacity: hasDottedSelected ? 1 : 0.35 }}
      >
        <IconLock />
        <span>Lock</span>
      </button>

      {/* Expand / collapse all modules */}
      <button className="btn" title="Expand all modules"
        onClick={() => { onExpandAll?.(); setActiveView('whiteboard') }}>
        <IconExpandAll />
        <span>Expand</span>
      </button>
      <button className="btn" title="Collapse all modules"
        onClick={() => { onCollapseAll?.(); setActiveView('whiteboard') }}>
        <IconCollapseAll />
        <span>Collapse</span>
      </button>

      <div className="toolbar-spacer" />

      {/* View switchers */}
      <div className="toolbar-group">
        <button
          className={`btn${activeView === 'whiteboard' ? ' view-active' : ''}`}
          onClick={() => setActiveView('whiteboard')}
          title="Whiteboard"
        >
          <IconWhiteboard />
          <span>Canvas</span>
        </button>
        <button
          className={`btn view-record${activeView === 'pm' ? ' view-active' : ''}`}
          onClick={() => setActiveView('pm')}
          title="OpenPM — work items grouped by prompt"
        >
          <IconOpenPM />
          <span>openpm</span>
        </button>
        <button
          className={`btn view-record${activeView === 'story' ? ' view-active' : ''}`}
          onClick={() => setActiveView('story')}
          title="Story — the conceptual narrative built from prompts"
        >
          <IconStory />
          <span>Story</span>
        </button>
        <button
          className={`btn view-record${activeView === 'timeline' ? ' view-active' : ''}`}
          onClick={() => setActiveView('timeline')}
          title="Timeline"
        >
          <IconTimeline />
          <span>Timeline</span>
        </button>
      </div>

      <div className="toolbar-sep" />

      {/* Utilities */}
      <div className="toolbar-group">
        <button className="btn" title="Command palette (⌘K)" onClick={onOpenCommandPalette}>
          <IconPalette />
          <span>⌘K</span>
        </button>
        <button className="btn" onClick={toggleTheme} title="Toggle theme">
          {theme === 'dark' ? <IconSun /> : <IconMoon />}
        </button>
      </div>
    </div>
  )
}

/* ─── Icons ──────────────────────────────────────────────────────── */
function IconCursor() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M2 2l8 4.5-4 1-1.5 4L2 2z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
    </svg>
  )
}

function IconDottedBox() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="10" height="10" rx="1.5"
        stroke="currentColor" strokeWidth="1.3" strokeDasharray="2.5 1.8"/>
    </svg>
  )
}

function IconSolidBox() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="10" height="10" rx="1.5"
        stroke="currentColor" strokeWidth="1.3"/>
    </svg>
  )
}

function IconArrow() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <line x1="2" y1="11" x2="10" y2="3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <polyline points="5,3 10,3 10,8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
    </svg>
  )
}

function IconSolidArrow() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <line x1="2" y1="11" x2="8" y2="5" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
      <path d="M5,2.2 L10.8,2.2 L10.8,8 z" fill="currentColor"/>
    </svg>
  )
}

function IconLock() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="2.5" y="5.5" width="8" height="6" rx="1" stroke="currentColor" strokeWidth="1.3"/>
      <path d="M4.5 5.5V4a2 2 0 014 0v1.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  )
}

function IconExpandAll() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M6.5 2v9M2 6.5h9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <path d="M4.2 3.6L6.5 2l2.3 1.6M4.2 9.4L6.5 11l2.3-1.6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}

function IconCollapseAll() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M2 6.5h9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <path d="M4.4 3.1L6.5 4.9 8.6 3.1M4.4 9.9L6.5 8.1 8.6 9.9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}

function IconWhiteboard() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="2.5" width="10" height="8" rx="1" stroke="currentColor" strokeWidth="1.3"/>
      <line x1="1.5" y1="5" x2="11.5" y2="5" stroke="currentColor" strokeWidth="1"/>
    </svg>
  )
}

function IconOpenPM() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="3" height="10" rx="1" stroke="currentColor" strokeWidth="1.3"/>
      <rect x="5.5" y="1.5" width="3" height="7" rx="1" stroke="currentColor" strokeWidth="1.3"/>
      <rect x="9.5" y="1.5" width="2" height="4" rx="1" stroke="currentColor" strokeWidth="1.3"/>
    </svg>
  )
}

function IconStory() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <circle cx="3" cy="3.2" r="1.6" stroke="currentColor" strokeWidth="1.2"/>
      <circle cx="10" cy="4.2" r="1.6" stroke="currentColor" strokeWidth="1.2"/>
      <circle cx="5.5" cy="10" r="1.6" stroke="currentColor" strokeWidth="1.2"/>
      <path d="M4.3 4.1l4.4 0.8M4.1 4.6l1.1 4M8.9 5.6l-2.9 3.4" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round"/>
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

function IconPalette() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <rect x="1.5" y="1.5" width="4" height="4" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="7.5" y="1.5" width="4" height="4" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="1.5" y="7.5" width="4" height="4" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="7.5" y="7.5" width="4" height="4" rx="0.8" stroke="currentColor" strokeWidth="1.2"/>
    </svg>
  )
}

function IconSun() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <circle cx="7" cy="7" r="2.5" stroke="currentColor" strokeWidth="1.3"/>
      <line x1="7" y1="1" x2="7" y2="2.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="7" y1="11.5" x2="7" y2="13" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="1" y1="7" x2="2.5" y2="7" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="11.5" y1="7" x2="13" y2="7" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="2.93" y1="2.93" x2="3.99" y2="3.99" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="10.01" y1="10.01" x2="11.07" y2="11.07" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="11.07" y1="2.93" x2="10.01" y2="3.99" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      <line x1="3.99" y1="10.01" x2="2.93" y2="11.07" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  )
}

function IconMoon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M11.5 8.5A5 5 0 015.5 2.5a5 5 0 100 9 5 5 0 006-3z"
        stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}
