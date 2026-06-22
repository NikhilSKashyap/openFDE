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
  const isIntent = box.kind === 'intent'
  const isDotted = type === 'dotted'

  // Intent steps read as plain-English sketch (violet); modules keep the
  // blue (dotted/editable) / green (solid/protected) architecture palette.
  const stroke = isIntent ? 'var(--accent)' : (isDotted ? 'var(--dotted)' : 'var(--solid)')
  const fill   = isIntent ? 'color-mix(in srgb, var(--accent) 9%, transparent)'
                          : (isDotted ? 'rgba(74,158,255,0.08)' : 'rgba(61,186,110,0.08)')
  const dash   = isIntent ? '2 4' : (isDotted ? '6 3' : undefined)

  const implFiles = isIntent && Array.isArray(box.implementationFiles) ? box.implementationFiles : []

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
        strokeDasharray={dash}
        data-box-id={id}
      />
      <foreignObject x={0} y={0} width={w} height={h} pointerEvents="none">
        <div xmlns="http://www.w3.org/1999/xhtml" style={{
          width: '100%', height: '100%',
          padding: '10px 12px',
          display: 'flex', flexDirection: 'column', gap: 4,
          overflow: 'hidden', userSelect: 'none',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            <div style={{
              fontSize: 13, fontWeight: 600,
              color: 'var(--text)',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              opacity: isEditing && editingField === 'title' ? 0.2 : 1,
              flex: 1, minWidth: 0,
            }}>
              {title}
            </div>
            {isIntent && (
              <span style={{
                flexShrink: 0, fontSize: 9, fontWeight: 600, letterSpacing: 0.3,
                textTransform: 'uppercase', color: 'var(--accent)',
                border: '1px solid color-mix(in srgb, var(--accent) 50%, var(--border))',
                borderRadius: 4, padding: '1px 4px',
              }}>
                Intent step
              </span>
            )}
          </div>
          <div style={{ width: '100%', height: 1, background: 'var(--border)', flexShrink: 0 }} />
          <div style={{
            fontSize: 11, color: 'var(--text-muted)',
            overflow: 'hidden',
            display: '-webkit-box',
            WebkitLineClamp: implFiles.length ? 2 : 3,
            WebkitBoxOrient: 'vertical',
            opacity: isEditing && editingField === 'prompt' ? 0.2 : 1,
            flex: 1,
          }}>
            {prompt}
          </div>
          {implFiles.length > 0 && (
            <div style={{
              flexShrink: 0, alignSelf: 'flex-start',
              fontSize: 10, fontWeight: 600, color: 'var(--accent)',
              background: 'color-mix(in srgb, var(--accent) 12%, transparent)',
              border: '1px solid color-mix(in srgb, var(--accent) 40%, var(--border))',
              borderRadius: 4, padding: '1px 6px',
            }}>
              {`Implemented by ${implFiles.length} file${implFiles.length === 1 ? '' : 's'}`}
            </div>
          )}
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
