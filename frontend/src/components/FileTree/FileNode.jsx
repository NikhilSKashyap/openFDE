import { useState } from 'react'
import { badgeForPath } from '../../lib/webxrBadges'

export default function FileNode({ node, depth = 0, webxrBadges = null }) {
  const [open, setOpen] = useState(node.defaultOpen ?? false)
  const isDir = !!node.children
  const indent = depth * 14

  const statusClass = node.status ? `status-${node.status}` : null
  // WebXR architecture tag (Plugin Registry payoff): a tiny calm chip on files the
  // WebXR pack flagged — XR entrypoint / 3D asset. Read-only metadata; null elsewhere.
  const xr = !isDir ? badgeForPath(webxrBadges, node.path) : null

  return (
    <div>
      <div
        className="file-node-row"
        style={{ paddingLeft: `${6 + indent}px` }}
        onClick={() => isDir && setOpen(o => !o)}
      >
        {/* Chevron */}
        <span className={`file-node-chevron${isDir ? (open ? ' open' : '') : ' hidden'}`}>
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
            <path d="M3 2l4 3-4 3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </span>

        {/* Icon */}
        <span className="file-node-icon">
          {isDir ? <IconFolder open={open} /> : <IconFile ext={node.ext} />}
        </span>

        {/* Name */}
        <span className="file-node-name">{node.name}</span>

        {/* WebXR architecture tag (XR entrypoint / 3D asset) */}
        {xr && (
          <span className={`file-node-xr ${xr.kind}`} title={xr.label}>
            {xr.kind === 'entrypoint' ? 'XR' : '3D'}
          </span>
        )}

        {/* Status dot */}
        {statusClass && <span className={`file-node-status ${statusClass}`} />}
      </div>

      {/* Children */}
      {isDir && open && (
        <div className="file-node-children">
          {node.children.map(child => (
            <FileNode key={child.name} node={child} depth={depth + 1} webxrBadges={webxrBadges} />
          ))}
        </div>
      )}
    </div>
  )
}

function IconFolder({ open }) {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      {open
        ? <path d="M1.5 4.5h10l-1 6h-8l-1-6z M1.5 4.5V3a1 1 0 011-1h2.5l1 1.5h4" stroke="currentColor" strokeWidth="1.1" strokeLinejoin="round"/>
        : <path d="M1.5 4.5h10v5.5a1 1 0 01-1 1h-8a1 1 0 01-1-1V4.5z M1.5 4.5V3a1 1 0 011-1h2.5l1 1.5" stroke="currentColor" strokeWidth="1.1" strokeLinejoin="round"/>
      }
    </svg>
  )
}

function IconFile({ ext }) {
  const color = ext === 'jsx' || ext === 'js' ? '#4a9eff'
    : ext === 'css' ? '#c084fc'
    : ext === 'md'  ? '#3dba6e'
    : ext === 'json'? '#ffcc00'
    : ext === 'py'  ? '#4a9eff'
    : 'currentColor'

  return (
    <svg width="11" height="13" viewBox="0 0 11 13" fill="none">
      <path d="M1.5 1.5h5l3 3v7.5a.5.5 0 01-.5.5h-7.5a.5.5 0 01-.5-.5v-11a.5.5 0 01.5-.5z"
        stroke={color} strokeWidth="1.1"/>
      <path d="M6.5 1.5V4.5H9.5" stroke={color} strokeWidth="1.1" strokeLinejoin="round"/>
    </svg>
  )
}
