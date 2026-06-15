import { useState } from 'react'

/** Pointer-drag a frosted card by its `[data-drag-root]` ancestor. Returns
 *  `[pos, startDrag]`: `pos` is null until the first drag (the card keeps its CSS
 *  dock), then `{x, y}` relative to the offset parent. Shared by the repair-hatch
 *  cards and the global Raise-issue card. */
export function useDragPos() {
  const [pos, setPos] = useState(null)
  function startDrag(e) {
    if (e.target.closest('button, textarea, a, input')) return
    const el = e.currentTarget.closest('[data-drag-root]')
    const parent = el?.offsetParent?.getBoundingClientRect()
    if (!parent) return
    const r = el.getBoundingClientRect()
    const d = { px: e.clientX, py: e.clientY, x: r.left - parent.left, y: r.top - parent.top }
    const mv = (ev) => setPos({ x: d.x + ev.clientX - d.px, y: d.y + ev.clientY - d.py })
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up) }
    window.addEventListener('pointermove', mv)
    window.addEventListener('pointerup', up)
    e.preventDefault()
  }
  return [pos, startDrag]
}
