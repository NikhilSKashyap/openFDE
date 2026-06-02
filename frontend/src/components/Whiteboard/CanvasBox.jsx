const PORTS = ['n', 'e', 's', 'w']

function portCoords(w, h, port) {
  switch (port) {
    case 'n': return { cx: w / 2, cy: 0 }
    case 'e': return { cx: w,     cy: h / 2 }
    case 's': return { cx: w / 2, cy: h }
    case 'w': return { cx: 0,     cy: h / 2 }
  }
}

export default function CanvasBox({ box, selected, isEditing, editingField, showPorts }) {
  const { id, x, y, w, h, type, title, prompt } = box
  const isDotted = type === 'dotted'

  const fill   = isDotted ? 'rgba(74,158,255,0.08)' : 'rgba(61,186,110,0.08)'
  const stroke = isDotted ? 'var(--dotted)' : 'var(--solid)'

  return (
    <g
      className={`canvas-box${showPorts ? ' show-ports' : ''}`}
      transform={`translate(${x},${y})`}
      data-box-id={id}
    >
      {selected && (
        <rect
          x={-3} y={-3} width={w + 6} height={h + 6} rx={9}
          fill="none" stroke="var(--accent)" strokeWidth={1.5}
          pointerEvents="none"
        />
      )}
      <rect
        width={w} height={h} rx={6}
        fill={fill}
        stroke={stroke}
        strokeWidth={1.5}
        strokeDasharray={isDotted ? '6 3' : undefined}
        data-box-id={id}
      />
      <foreignObject x={0} y={0} width={w} height={h} pointerEvents="none">
        <div xmlns="http://www.w3.org/1999/xhtml" style={{
          width: '100%', height: '100%',
          padding: '10px 12px',
          display: 'flex', flexDirection: 'column', gap: 4,
          overflow: 'hidden', userSelect: 'none',
        }}>
          <div style={{
            fontSize: 13, fontWeight: 600,
            color: 'var(--text)',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            opacity: isEditing && editingField === 'title' ? 0.2 : 1,
          }}>
            {title}
          </div>
          <div style={{ width: '100%', height: 1, background: 'var(--border)', flexShrink: 0 }} />
          <div style={{
            fontSize: 11, color: 'var(--text-muted)',
            overflow: 'hidden',
            display: '-webkit-box',
            WebkitLineClamp: 3,
            WebkitBoxOrient: 'vertical',
            opacity: isEditing && editingField === 'prompt' ? 0.2 : 1,
          }}>
            {prompt}
          </div>
        </div>
      </foreignObject>

      {/* Resize handle */}
      <rect
        x={w - 12} y={h - 12} width={12} height={12} rx={2}
        fill={stroke} opacity={0.5}
        style={{ cursor: 'nwse-resize' }}
        data-resize-id={id}
      />

      {/* Connection ports — hidden by default, shown on hover or when showPorts */}
      <g className="box-ports">
        {PORTS.map(port => {
          const { cx, cy } = portCoords(w, h, port)
          return (
            <circle
              key={port}
              cx={cx} cy={cy} r={5}
              fill="var(--surface)"
              stroke={stroke}
              strokeWidth={1.5}
              style={{ cursor: 'crosshair' }}
              data-port={port}
              data-port-box-id={id}
            />
          )
        })}
      </g>
    </g>
  )
}
