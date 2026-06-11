import { useEffect, useState } from 'react'
import { getSourceSlice, patchSource } from '../../api/backend'

/**
 * FunctionPatch — the repair hatch. Summoned ONLY by a failure receipt
 * ("Show →" on a failed check), it shows ONE function's code, marks the
 * failing line, and offers two exits:
 *   • fix it right here and ⌘S — the splice hits the worktree like any edit,
 *     so the watcher records the repair into the live episode;
 *   • copy a SPECIFIC prompt (file · function · line · failing test) — prompt
 *     quality improves for free because you're standing at the exact spot.
 * It is deliberately not an editor: no tree, no tabs, one function, fix, gone.
 *
 * @param {Object}   props.hatch  - {file, line, func, funcName, test, start, end}
 * @param {Function} props.onClose
 */
export default function FunctionPatch({ hatch, onClose }) {
  const key = `${hatch.file}:${hatch.start}:${hatch.end}`
  const [loaded, setLoaded] = useState({ key: null, code: null, meta: null, err: '' })
  const [draft, setDraft] = useState({ key: null, code: null })
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  // Async IIFE + alive guard; every setState happens after the await.
  useEffect(() => {
    let alive = true
    ;(async () => {
      const r = await getSourceSlice(hatch.file, hatch.start, hatch.end)
      if (!alive) return
      setLoaded({ key: `${hatch.file}:${hatch.start}:${hatch.end}`,
                  code: r?.ok ? r.code : null, meta: r?.ok ? r : null,
                  err: r?.ok ? '' : (r?.error || 'could not read source') })
    })()
    return () => { alive = false }
  }, [hatch])

  const ready = loaded.key === key && loaded.code != null
  const code = draft.key === key ? draft.code : (ready ? loaded.code : null)
  const meta = ready ? loaded.meta : null

  async function save() {
    if (busy || code == null || !meta) return
    setBusy(true)
    const r = await patchSource(hatch.file, meta.start, meta.end, code)
    setBusy(false)
    if (r?.ok) {
      setLoaded(l => ({ ...l, code, meta: { ...l.meta, end: r.end, total: r.total } }))
      setDraft({ key: null, code: null })
      setNote('Saved ✓ — Run checks in the episode panel to verify the fix')
    } else {
      setNote(r?.error || 'save failed')
    }
  }

  function onKey(e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save() }
  }

  async function copyPrompt() {
    const p = `In ${hatch.file}, function ${hatch.funcName || hatch.func}() ` +
      `(lines ${meta?.start ?? hatch.start}–${meta?.end ?? hatch.end}): ` +
      `${hatch.test ? `test ${hatch.test} fails` : 'a check fails'} at line ${hatch.line}. ` +
      `Fix the function so the check passes, without changing the test.`
    try { await navigator.clipboard.writeText(p) } catch { /* clipboard may be blocked */ }
    setNote('Specific prompt copied — paste into Claude Code or the Work panel')
  }

  const failRel = meta ? hatch.line - meta.start + 1 : null

  return (
    <div className="repair-hatch" onKeyDown={onKey}>
      <div className="repair-hatch-head">
        <span className="repair-hatch-dot" />
        <span className="repair-hatch-title">
          {hatch.funcName || hatch.func}() · {hatch.file}
          {meta && <span className="repair-hatch-lines"> · L{meta.start}–{meta.end}</span>}
        </span>
        <button className="repair-hatch-close" onClick={onClose} title="Close the hatch">×</button>
      </div>
      <div className="repair-hatch-fail">
        ✕ {hatch.test ? `${hatch.test} fails` : 'failing'} at line {hatch.line}
        {failRel != null && failRel >= 1 && <> (line {failRel} below)</>}
      </div>
      {!ready && <div className="repair-hatch-note" style={{ padding: '0 12px 8px' }}>{loaded.err || 'opening the function…'}</div>}
      {ready && (
        <textarea
          className="repair-hatch-code"
          value={code}
          onChange={e => setDraft({ key, code: e.target.value })}
          spellCheck={false}
          rows={Math.min(22, Math.max(6, (code || '').split('\n').length + 1))}
        />
      )}
      <div className="repair-hatch-foot">
        <button className="repair-hatch-save" onClick={save} disabled={busy || !ready}>
          {busy ? 'Saving…' : 'Save ⌘S'}
        </button>
        <button className="repair-hatch-prompt" onClick={copyPrompt}>
          Copy specific prompt
        </button>
        {note && <span className="repair-hatch-note">{note}</span>}
      </div>
    </div>
  )
}
