import { useState, useEffect } from 'react'
import FileNode from './FileNode'
import { getFiles } from '../../api/backend'

// ── Mock fallback (used when backend is not running) ─────────────────────
const MOCK_TREE = [
  {
    name: 'frontend', ext: null, defaultOpen: true,
    children: [
      {
        name: 'src', ext: null, defaultOpen: true,
        children: [
          {
            name: 'components', ext: null, defaultOpen: true,
            children: [
              { name: 'Toolbar.jsx',   ext: 'jsx', status: 'active' },
              {
                name: 'FileTree', ext: null,
                children: [
                  { name: 'FileTree.jsx', ext: 'jsx', status: 'modified' },
                  { name: 'FileNode.jsx', ext: 'jsx' },
                ],
              },
              {
                name: 'Whiteboard', ext: null,
                children: [
                  { name: 'Whiteboard.jsx', ext: 'jsx' },
                ],
              },
              {
                name: 'RightPanel', ext: null,
                children: [
                  { name: 'RightPanel.jsx', ext: 'jsx' },
                ],
              },
            ],
          },
          { name: 'App.jsx',   ext: 'jsx', status: 'modified' },
          { name: 'App.css',   ext: 'css' },
          { name: 'main.jsx',  ext: 'jsx' },
          { name: 'index.css', ext: 'css' },
        ],
      },
      { name: 'index.html',    ext: 'html' },
      { name: 'package.json',  ext: 'json' },
      { name: 'vite.config.js',ext: 'js' },
    ],
  },
  { name: 'OPENFDE.md', ext: 'md' },
  { name: 'plan.md',    ext: 'md', status: 'modified' },
]

// ── Backend node → FileNode shape ─────────────────────────────────────────

/**
 * Normalise a backend file-tree node into the shape that FileNode expects.
 *
 * Backend shape:  { name, path, type: 'directory'|'file', children?, size? }
 * FileNode shape: { name, ext, defaultOpen, children?, status? }
 *
 * @param {{ name: string, type: string, children?: Array, path: string }} node
 * @param {number} depth - current depth (0 = root)
 * @returns {object} FileNode-compatible tree node
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

// ── Component ─────────────────────────────────────────────────────────────

export default function FileTree() {
  const [query, setQuery]     = useState('')
  const [tree, setTree]       = useState(MOCK_TREE)
  const [fromBackend, setFromBackend] = useState(false)

  // Try to load the live file tree from the backend once on mount.
  // Falls back to MOCK_TREE silently if the backend is unavailable.
  useEffect(() => {
    let cancelled = false
    getFiles().then(data => {
      if (cancelled || !data) return
      // The root node from the backend is a single directory node.
      // Wrap it in an array the same way MOCK_TREE is structured.
      const normalized = normalizeNode(data, 0)
      setTree(normalized.children ? [normalized] : MOCK_TREE)
      setFromBackend(true)
    })
    return () => { cancelled = true }
  }, [])

  const filtered = query.trim()
    ? flatFilter(tree, query.toLowerCase())
    : tree

  return (
    <>
      <div className="panel-section-header">
        Explorer
        {fromBackend && (
          <span style={{ fontSize: 9, marginLeft: 6, color: 'var(--solid)', fontWeight: 500 }}>
            live
          </span>
        )}
      </div>
      <div className="search-wrap">
        <input
          className="search-input"
          type="text"
          placeholder="Search files…"
          value={query}
          onChange={e => setQuery(e.target.value)}
        />
      </div>
      <div className="file-tree-scroll">
        {filtered.map(node => (
          <FileNode key={node.name} node={node} depth={0} />
        ))}
      </div>
    </>
  )
}

/**
 * Flatten the tree and return nodes whose name matches the query.
 *
 * @param {Array} nodes - array of tree nodes
 * @param {string} q - lowercase query string
 * @returns {Array} matched nodes (no children, not defaultOpen)
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
