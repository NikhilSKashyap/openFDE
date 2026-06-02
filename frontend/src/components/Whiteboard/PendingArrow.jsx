import { getPortPos, bezierPath } from './arrowUtils'

export default function PendingArrow({ pendingArrow, boxes }) {
  const fromBox = boxes.find(b => b.id === pendingArrow.fromBox)
  if (!fromBox) return null

  const start = getPortPos(fromBox, pendingArrow.fromPort)
  const end   = { x: pendingArrow.curX, y: pendingArrow.curY }
  const d     = bezierPath(start, pendingArrow.fromPort, end, null)

  const color = pendingArrow.arrowType === 'solid' ? 'var(--solid)' : 'var(--dotted)'

  return (
    <path
      d={d}
      fill="none"
      stroke={color}
      strokeWidth={1.5}
      strokeDasharray="5 4"
      opacity={0.55}
      pointerEvents="none"
    />
  )
}
