import { useEffect, useState } from 'react'
import { composeFixPrompt, getSourceSlice, hatchExplain, hatchFlow, hatchRun, hydrateHatch, patchSource } from '../../api/backend'
import { tryCopy } from '../../lib/clipboard'
import Md from '../../lib/markdown'

/**
 * FunctionPatch — the repair hatch (v3). Summoned ONLY by a failure receipt
 * ("Show →" on a failed check), it shows ONE function's code, marks the
 * failing line, and offers four exits:
 *   • fix it right here and ⌘S — the splice hits the worktree like any edit;
 *   • "Explain this error" / "Generate prompt" — artifacts composed through
 *     the Agent Council senior_dev role, each in its own movable markdown
 *     card. The LLM runs ONCE per failure fingerprint: saved artifacts hydrate
 *     on reopen and are reused until the failure meaning changes; Regenerate
 *     (or clicking the button while the card is open) explicitly replaces.
 *   • "Show failure flow" — how the failure got there, drawn on the canvas as
 *     a temporary lens (deterministic AST evidence; humanized labels).
 *   • "Run with Senior Dev" — through OpenFDE's own runner, scoped to the
 *     failing file. Never injects into a terminal; never a new episode.
 * It is deliberately not an editor: no tree, no tabs, one function, fix, gone.
 *
 * @param {Object}   props.hatch  - {file, line, func, funcName, test, start,
 *                                   end, episodeId, checkId, failureMsg}
 * @param {Function} props.onClose
 * @param {Function} props.onShowFlow - (flowArtifact) → App enters the lens
 */

// Failure-location identity for card state; the failure MEANING fingerprint
// (incl. message + code hashes) is computed server-side and rides artifacts.
const hatchKey = (h) => `${h.file}:${h.line}:${h.test || 'check'}`

// Drag-anywhere position: null until the first drag (CSS dock applies), then
// {x,y} relative to the offsetParent. Window-bound listeners so the card
// follows even when the cursor outruns the header.
function useDragPos() {
  const [pos, setPos] = useState(null)
  function startDrag(e) {
    if (e.target.closest('button, textarea, a')) return
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

/** One movable artifact card (Explanation / Repair prompt) beside the hatch. */
function RepairCard({ dock, title, card, onClose, onRegenerate, actions }) {
  const [pos, startDrag] = useDragPos()
  return (
    <div className={`repair-card ${pos ? '' : dock}`} data-drag-root
         style={pos ? { left: pos.x, top: pos.y, right: 'auto', transform: 'none' } : undefined}>
      <div className="repair-card-head" onPointerDown={startDrag}>
        <span className="repair-hatch-dot" />
        <span className="repair-card-title">{title}</span>
        <button className="repair-hatch-close" onClick={onClose} title="Close (kept on the episode)">×</button>
      </div>
      <div className="repair-card-body">
        {card.busy ? <span className="repair-card-busy">{card.busyLabel}</span> : <Md text={card.text} />}
      </div>
      {!card.busy && (
        <div className="repair-card-foot">
          {actions}
          {onRegenerate && (
            <button className="repair-card-btn" onClick={onRegenerate}
                    title="Replace this artifact — runs the model again for this failure">
              Regenerate
            </button>
          )}
          <span style={{ flex: 1 }} />
          {card.note && <span className="repair-card-note">{card.note}</span>}
          {!card.note && card.source && <span className="repair-card-src">{card.source}</span>}
        </div>
      )}
    </div>
  )
}

export default function FunctionPatch({ hatch, onClose, onShowFlow }) {
  const key = `${hatch.file}:${hatch.start}:${hatch.end}`
  const [loaded, setLoaded] = useState({ key: null, code: null, meta: null, err: '' })
  const [draft, setDraft] = useState({ key: null, code: null })
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  // Artifact cards are keyed by the failure identity — switching failures simply
  // stops the old key from rendering (no sync resets inside effects).
  const akey = hatchKey(hatch)
  const [explainState, setExplainCard] = useState(null) // {key, busy, busyLabel, open, text, source, note}
  const [promptState, setPromptCard] = useState(null)
  const [flowState, setFlowArt] = useState(null)         // {key, busy, art}
  const [runState, setRun] = useState(null)              // {key, busy, last:{status, writes, error, …}}
  const explainCard = explainState?.key === akey ? explainState : null
  const promptCard = promptState?.key === akey ? promptState : null
  const flow = flowState?.key === akey ? flowState : null
  const run = runState?.key === akey ? runState : null
  const [hatchPos, startHatchDrag] = useDragPos()

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

  // The fingerprint hashes the ON-DISK function (artifacts describe the repo's
  // failure, not a half-typed draft) — so hydration waits for the source slice,
  // and a ⌘S that changes the function correctly invalidates old artifacts.
  const artifactPayload = () => ({
    episodeId: hatch.episodeId || '', checkId: hatch.checkId || '',
    failureMsg: hatch.failureMsg || '', file: hatch.file, line: hatch.line,
    test: hatch.test || '', funcName: hatch.funcName || hatch.func,
    start: meta?.start ?? hatch.start, end: meta?.end ?? hatch.end,
    code: (ready ? loaded.code : '') || '',
  })

  // Hydrate saved artifacts for this failure meaning — reopen costs zero LLM.
  useEffect(() => {
    if (!hatch.episodeId) return undefined
    if (!(loaded.key === `${hatch.file}:${hatch.start}:${hatch.end}` && loaded.code != null)) return undefined
    let alive = true
    const k = hatchKey(hatch)
    ;(async () => {
      const r = await hydrateHatch({
        episodeId: hatch.episodeId, checkId: hatch.checkId || '',
        failureMsg: hatch.failureMsg || '', file: hatch.file, line: hatch.line,
        test: hatch.test || '', funcName: hatch.funcName || hatch.func,
        start: loaded.meta?.start ?? hatch.start, end: loaded.meta?.end ?? hatch.end,
        code: loaded.code || '',
      })
      if (!alive || !r?.artifacts) return
      const a = r.artifacts
      if (a.failure_explanation?.text) setExplainCard({ key: k, open: true, text: a.failure_explanation.text, source: a.failure_explanation.source })
      if (a.repair_prompt?.text) setPromptCard({ key: k, open: true, text: a.repair_prompt.text, source: a.repair_prompt.source })
      if (a.failure_flow) setFlowArt({ key: k, art: a.failure_flow })
      if (a.repair_run) setRun({ key: k, last: a.repair_run })
    })()
    return () => { alive = false }
  }, [hatch, loaded])

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

  // Reuse-first: a saved artifact for this failure meaning is shown as-is
  // (the server enforces run-LLM-once); regenerate is the explicit replace.
  async function explainError(regenerate = false) {
    if (explainCard?.busy) return
    if (!regenerate && explainCard?.text) {
      setExplainCard(c => ({ ...c, open: true }))
      return
    }
    setExplainCard({ key: akey, busy: true, busyLabel: 'Senior Dev is reading the failure…', open: true })
    const r = await hatchExplain({ ...artifactPayload(), regenerate })
    const a = r?.artifact
    setExplainCard({ key: akey, open: true, text: a?.text || r?.error || 'could not compose the explanation', source: a?.source || '' })
  }

  async function generatePrompt(regenerate = false) {
    if (promptCard?.busy) return
    if (!regenerate && promptCard?.text) {
      setPromptCard(c => ({ ...c, open: true }))
      return
    }
    setPromptCard({ key: akey, busy: true, busyLabel: 'Senior Dev is composing the repair prompt…', open: true })
    const r = await composeFixPrompt({ ...artifactPayload(), regenerate })
    const a = r?.artifact
    setPromptCard({ key: akey, open: true, text: a?.text || r?.error || 'could not compose the prompt', source: a?.source || '' })
  }

  // The failure FLOW — how the failure got there. Saved flows re-enter the
  // lens instantly; generation is deterministic-first (fast) + label pass.
  async function showFlow() {
    if (flow?.busy) return
    if (flow?.art) { onShowFlow?.(flow.art); return }
    setFlowArt({ key: akey, busy: true })
    const r = await hatchFlow(artifactPayload())
    const a = r?.artifact
    setFlowArt({ key: akey, art: a || null })
    if (a) onShowFlow?.(a)
    else setNote(r?.error || 'could not derive the failure flow')
  }

  async function copyCard(card, setCard) {
    const ok = await tryCopy(card.text || '')
    setCard(c => ({ ...c, note: ok ? 'Copied ✓' : 'Clipboard blocked — select the text and ⌘C' }))
    setTimeout(() => setCard(c => (c ? { ...c, note: '' } : c)), 3500)
  }

  // The scoped repair run — through OpenFDE's configured senior_dev, never the
  // user's terminal. Edits hit the worktree; the watcher records them into the
  // live episode; the receipt lands on this episode's repair artifacts.
  async function runWithSeniorDev() {
    if (run?.busy || !promptCard?.text) return
    setRun({ key: akey, busy: true })
    const r = await hatchRun({ ...artifactPayload(), prompt: promptCard.text })
    setRun({ key: akey, last: r?.run || { status: 'failed', error: r?.error || 'run failed' } })
    setNote(r?.ok ? 'Senior Dev finished — Run checks in the episode panel to verify'
                  : (r?.run?.error || 'Senior Dev run failed'))
  }

  const failRel = meta ? hatch.line - meta.start + 1 : null
  const runLine = run?.last && (run.last.status !== 'failed'
    ? `ran ✓ — ${(run.last.writes || []).length} file${(run.last.writes || []).length === 1 ? '' : 's'} edited`
    : `run ✕ — ${run.last.error || 'failed'}`)

  return (
    <>
      <div className="repair-hatch" data-drag-root onKeyDown={onKey}
           style={hatchPos ? { left: hatchPos.x, top: hatchPos.y, right: 'auto', transform: 'none' } : undefined}>
        <div className="repair-hatch-head" onPointerDown={startHatchDrag}>
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
          <button className="repair-hatch-explain" onClick={() => explainError(explainCard?.open && !!explainCard?.text)} disabled={explainCard?.busy}>
            {explainCard?.busy ? 'Explaining…' : 'Explain this error'}
          </button>
          <button className="repair-hatch-prompt" onClick={() => generatePrompt(promptCard?.open && !!promptCard?.text)} disabled={promptCard?.busy}>
            {promptCard?.busy ? 'Generating…' : 'Generate prompt'}
          </button>
          <button className="repair-hatch-flow" onClick={showFlow} disabled={flow?.busy}
                  title="How the failure got there — highlights only the failure path on the canvas">
            {flow?.busy ? 'Tracing…' : 'Show failure flow'}
          </button>
          <span style={{ flex: 1 }} />
          <button className="repair-hatch-save" onClick={save} disabled={busy || !ready}>
            {busy ? 'Saving…' : 'Save ⌘S'}
          </button>
        </div>
        {note && <div className="repair-hatch-note" style={{ padding: '0 12px 10px' }}>{note}</div>}
      </div>

      {explainCard?.open && (
        <RepairCard dock="dock-a" title="Explanation" card={explainCard}
                    onClose={() => setExplainCard(c => ({ ...c, open: false }))}
                    onRegenerate={() => explainError(true)}
                    actions={
                      <button className="repair-card-btn" onClick={() => copyCard(explainCard, setExplainCard)}>
                        Copy
                      </button>
                    } />
      )}
      {promptCard?.open && (
        <RepairCard dock="dock-b" title="Repair prompt" card={promptCard}
                    onClose={() => setPromptCard(c => ({ ...c, open: false }))}
                    onRegenerate={() => generatePrompt(true)}
                    actions={
                      <>
                        <button className="repair-card-btn" onClick={() => copyCard(promptCard, setPromptCard)}>
                          Copy
                        </button>
                        <button className="repair-card-btn run" onClick={runWithSeniorDev}
                                disabled={run?.busy}
                                title="Run this repair through OpenFDE's Senior Dev, scoped to the failing file — not your terminal">
                          {run?.busy ? 'Senior Dev running…' : 'Run with Senior Dev'}
                        </button>
                        {runLine && <span className="repair-card-runline">{runLine}</span>}
                      </>
                    } />
      )}
    </>
  )
}
