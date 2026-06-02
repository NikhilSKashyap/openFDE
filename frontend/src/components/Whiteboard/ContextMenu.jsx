import { useEffect, useRef } from 'react'

export default function ContextMenu({ x, y, targetIds, boxes, onClose, onToggleType, onDuplicate, onDelete, onExpandModule = null }) {
  const ref = useRef(null)

  useEffect(() => {
    function onDown(e) {
      if (ref.current && !ref.current.contains(e.target)) onClose()
    }
    function onKey(e) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const count = targetIds.length
  const label = count === 1 ? '1 box' : `${count} boxes`
  const allSolid = targetIds.every(id => boxes.find(b => b.id === id)?.type === 'solid')
  const toggleLabel = allSolid ? 'Make dotted' : 'Make solid'

  // Single module box → offer in-place expand actions.
  const singleBox = count === 1 ? boxes.find(b => b.id === targetIds[0]) : null
  const isModule = !!(singleBox && singleBox.moduleId)

  function item(text, onClick, danger = false) {
    return (
      <button
        key={text}
        onPointerDown={e => { e.stopPropagation(); onClick(); onClose() }}
        style={{
          display: 'block', width: '100%', textAlign: 'left',
          padding: '6px 12px', background: 'none', border: 'none',
          color: danger ? 'var(--violation)' : 'var(--text)',
          fontSize: 12, cursor: 'pointer',
        }}
        onMouseEnter={e => e.currentTarget.style.background = 'var(--surface-2)'}
        onMouseLeave={e => e.currentTarget.style.background = 'none'}
      >
        {text}
      </button>
    )
  }

  return (
    <div ref={ref} style={{
      position: 'fixed', left: x, top: y, zIndex: 1000,
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 6,
      boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
      minWidth: 160,
      padding: '4px 0',
    }}>
      <div style={{
        padding: '4px 12px 6px',
        fontSize: 11, color: 'var(--text-muted)',
        borderBottom: '1px solid var(--border)',
        marginBottom: 4,
      }}>
        {label}
      </div>
      {isModule && onExpandModule && item('Expand module', () => onExpandModule(targetIds[0], false))}
      {isModule && onExpandModule && item('Expand all in module', () => onExpandModule(targetIds[0], true))}
      {isModule && onExpandModule && <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />}
      {item(toggleLabel, () => onToggleType(targetIds))}
      {item('Duplicate', () => onDuplicate(targetIds))}
      <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />
      {item('Delete', () => onDelete(targetIds), true)}
    </div>
  )
}
