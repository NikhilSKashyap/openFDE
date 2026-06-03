export function getPortPos(box, port) {
  switch (String(port || '').toLowerCase()) {
    case 'n': return { x: box.x + box.w / 2, y: box.y }
    case 'e': return { x: box.x + box.w,     y: box.y + box.h / 2 }
    case 's': return { x: box.x + box.w / 2, y: box.y + box.h }
    case 'w': return { x: box.x,             y: box.y + box.h / 2 }
    default:  return { x: box.x + box.w / 2, y: box.y + box.h / 2 }
  }
}

function ctrlOffset(port, x, y, d) {
  switch (String(port || '').toLowerCase()) {
    case 'n': return { x,     y: y - d }
    case 's': return { x,     y: y + d }
    case 'e': return { x: x + d, y }
    case 'w': return { x: x - d, y }
    default:  return { x, y }
  }
}

// toPort may be null when rendering a pending arrow to cursor position
export function bezierPath(start, fromPort, end, toPort) {
  const dist = Math.hypot(end.x - start.x, end.y - start.y)
  const d = Math.max(50, dist * 0.4)
  const c1 = ctrlOffset(fromPort, start.x, start.y, d)
  const c2 = toPort ? ctrlOffset(toPort, end.x, end.y, d) : { x: end.x, y: end.y }
  return `M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`
}

// Cubic bezier midpoint at t=0.5 via De Casteljau:
// point = (1-t)³·p0 + 3(1-t)²t·c1 + 3(1-t)t²·c2 + t³·p1  at t=0.5
// → 0.125·p0 + 0.375·c1 + 0.375·c2 + 0.125·p1
export function getBezierMidpoint(start, fromPort, end, toPort) {
  const dist = Math.hypot(end.x - start.x, end.y - start.y)
  const d = Math.max(50, dist * 0.4)
  const c1 = ctrlOffset(fromPort, start.x, start.y, d)
  const c2 = toPort ? ctrlOffset(toPort, end.x, end.y, d) : { x: end.x, y: end.y }
  return {
    x: 0.125 * start.x + 0.375 * c1.x + 0.375 * c2.x + 0.125 * end.x,
    y: 0.125 * start.y + 0.375 * c1.y + 0.375 * c2.y + 0.125 * end.y,
  }
}
