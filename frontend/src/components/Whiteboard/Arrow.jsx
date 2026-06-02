import { getPortPos, bezierPath, getBezierMidpoint } from './arrowUtils'

export default function Arrow({ arrow, boxes, selected, runStatus }) {
  const fromBox = boxes.find(b => b.id === arrow.fromBox)
  const toBox   = boxes.find(b => b.id === arrow.toBox)
  if (!fromBox || !toBox) return null

  const start = getPortPos(fromBox, arrow.fromPort)
  const end   = getPortPos(toBox,   arrow.toPort)
  const d     = bezierPath(start, arrow.fromPort, end, arrow.toPort)
  const mid   = getBezierMidpoint(start, arrow.fromPort, end, arrow.toPort)

  const isDotted = arrow.type === 'dotted'
  const failed   = runStatus === 'failed'
  const active   = runStatus === 'active'
  // Failure recolours to violation; dotted/solid styling otherwise preserved.
  const color    = failed ? 'var(--violation)' : (isDotted ? 'var(--dotted)' : 'var(--solid)')
  const markerId = failed ? 'arrowhead-failed' : (isDotted ? 'arrowhead-dotted' : 'arrowhead-solid')
  const hasLabel = arrow.label && arrow.label.trim().length > 0
  const labelText = hasLabel
    ? (arrow.label.length > 16 ? arrow.label.slice(0, 15) + '…' : arrow.label)
    : ''
  const labelW = Math.max(44, labelText.length * 6.5 + 16)

  // Step 23: hover tooltip summarising the underlying function-level flows.
  const flows = arrow.flows || []
  const tip = arrow.edgeType ? [
    (arrow.edgeType === 'dataflow' ? 'Dataflow' : arrow.edgeType === 'import' ? 'Import (fallback)' : 'Manual')
      + (arrow.confidence ? ` · ${arrow.confidence} confidence` : ''),
    ...(arrow.flowCount ? [`${arrow.flowCount} function flow${arrow.flowCount !== 1 ? 's' : ''}:`] : []),
    ...flows.map(f => `  ${f.label}  [${(f.fromFile || '').split('/').pop()} → ${(f.toFile || '').split('/').pop()}]`),
    ...(arrow.flowCount > flows.length ? [`  +${arrow.flowCount - flows.length} more…`] : []),
    ...(arrow.edgeType === 'import' && !flows.length ? ['No function flows resolved — import dependency.'] : []),
  ].join('\n') : ''

  return (
    <g data-arrow-id={arrow.id}>
      {/* Selection glow — rendered first so it's behind the visible path */}
      {selected && (
        <path
          d={d}
          fill="none"
          stroke="var(--accent)"
          strokeWidth={6}
          opacity={0.3}
          pointerEvents="none"
        />
      )}

      {/* Visible path */}
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth={failed ? 2 : 1.5}
        strokeDasharray={isDotted ? '6 3' : undefined}
        markerEnd={`url(#${markerId})`}
        pointerEvents="none"
      />

      {/* Active-flow animation — marching dashes along the edge while running */}
      {active && (
        <path
          className="arrow-flow"
          d={d}
          fill="none"
          stroke={color}
          strokeWidth={2.5}
          strokeLinecap="round"
          pointerEvents="none"
        />
      )}

      {/* Transparent wide hit target — data-arrow-id read by handlePointerDown */}
      <path
        d={d}
        data-arrow-id={arrow.id}
        fill="none"
        stroke="transparent"
        strokeWidth={12}
        pointerEvents="stroke"
        style={{ cursor: 'pointer' }}
      >
        {tip && <title>{tip}</title>}
      </path>

      {/* Label pill at bezier midpoint */}
      {hasLabel && (
        <g pointerEvents="none">
          <rect
            x={mid.x - labelW / 2}
            y={mid.y - 9}
            width={labelW}
            height={18}
            rx={9}
            fill="var(--surface)"
            stroke={color}
            strokeWidth={1}
          />
          <text
            x={mid.x}
            y={mid.y + 4}
            textAnchor="middle"
            fill={color}
            fontSize={10}
            fontFamily="inherit"
          >
            {labelText}
          </text>
        </g>
      )}
    </g>
  )
}
