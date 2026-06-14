import { useState, useEffect } from 'react'
import FileNode from './FileNode'
import { getFiles } from '../../api/backend'

// ── Backend node → FileNode shape ─────────────────────────────────────────

/**
 * Normalise a backend file-tree node into the shape that FileNode expects.
 *
 * Backend shape:  { name, path, type: 'directory'|'file', children?, size? }
 * FileNode shape: { name, ext, defaultOpen, children?, status? }
 */
function normalizeNode(node, depth = 0) {
  const isDir = node.type === 'directory'
  const ext = !isDir && node.name.includes('.')
    ? node.name.slice(node.name.lastIndexOf('.') + 1)
    : null
  return {
    name: node.name,
    path: node.path,
    ext,
    defaultOpen: depth <= 1,
    children: isDir
      ? (node.children ?? []).map(c => normalizeNode(c, depth + 1))
      : undefined,
  }
}

const statusStyle = { padding: '12px 14px', fontSize: 11.5, color: 'var(--text-muted)', lineHeight: 1.6 }

// ── Component ─────────────────────────────────────────────────────────────

/**
 * Explorer — the LIVE file tree of the watched repo. Never a mock: it starts in a
 * loading state, renders the real backend tree on success, and shows a clearly-labeled
 * "offline" state only when the backend is unreachable. Demo files are never passed off
 * as live repo files (that flashed the wrong repo on startup).
 *
 * @param {{ repoName?: string }} props - the watched repo name, for the loading label.
 */
export default function FileTree({ repoName = '' }) {
  const [query, setQuery] = useState('')
  const [tree, setTree]   = useState(null)         // null until the backend answers
  const [state, setState] = useState('loading')    // 'loading' | 'live' | 'offline'

  // Load the live file tree on mount. On a fresh `openfde watch` the backend is briefly
  // saturated by startup assimilation, so the first /api/files call can be slow or fail.
  // We RETRY before declaring offline: a transient startup stall must not strand the
  // Explorer on an empty/offline state — the watched repo has to show up. Only a backend
  // that stays unreachable across all attempts (≈12 s) settles to 'offline'.
  useEffect(() => {
    let cancelled = false
    let attempt = 0
    const MAX_ATTEMPTS = 8

    const load = () => {
      getFiles().then(data => {
        if (cancelled) return
        if (data && data.name) {
          setTree([normalizeNode(data, 0)])
          setState('live')
          return
        }
        attempt += 1
        if (attempt < MAX_ATTEMPTS) {
          setTimeout(load, 1500)                    // keep showing "Loading…" and retry
        } else {
          setState('offline')                      // backend unavailable — no fake files
        }
      })
    }
    load()
    return () => { cancelled = true }
  }, [])

  const filtered = (state === 'live' && query.trim())
    ? flatFilter(tree, query.toLowerCase())
    : (tree || [])

  return (
    <>
      <div className="panel-section-header">
        Explorer
        {state === 'live' && (
          <span style={{ fontSize: 9, marginLeft: 6, color: 'var(--solid)', fontWeight: 500 }}>live</span>
        )}
        {state === 'offline' && (
          <span style={{ fontSize: 9, marginLeft: 6, color: 'var(--text-muted)', fontWeight: 500 }}>offline</span>
        )}
      </div>
      <div className="search-wrap">
        <input
          className="search-input"
          type="text"
          placeholder="Search files…"
          value={query}
          onChange={e => setQuery(e.target.value)}
          disabled={state !== 'live'}
        />
      </div>
      <div className="file-tree-scroll">
        {state === 'loading' && (
          <div style={statusStyle}>Loading files{repoName ? ` for ${repoName}` : ''}…</div>
        )}
        {state === 'offline' && (
          <div style={statusStyle}>
            Backend not running — no live files.
            <div style={{ marginTop: 4, opacity: 0.75 }}>
              Run <code>openfde watch</code> to see this repo.
            </div>
          </div>
        )}
        {state === 'live' && filtered.map(node => (
          <FileNode key={node.path || node.name} node={node} depth={0} />
        ))}
      </div>
    </>
  )
}

/**
 * Flatten the tree and return nodes whose name matches the query.
 */
function flatFilter(nodes, q) {
  const results = []
  for (const n of nodes) {
    if (n.name.toLowerCase().includes(q)) {
      results.push({ ...n, children: undefined, defaultOpen: false })
    }
    if (n.children) results.push(...flatFilter(n.children, q))
  }
  return results
}
