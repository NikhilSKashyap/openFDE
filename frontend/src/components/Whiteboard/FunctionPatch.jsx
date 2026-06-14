import { useEffect, useRef, useState } from 'react'
import { composeFixPrompt, draftOpenfdeReport, getSourceSlice, hatchExplain, hatchFlow, hatchRun, hydrateHatch, patchSource, reportOpenfdeIssue } from '../../api/backend'
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
 *     on reopen and are reused until the failure meaning changes; ONLY the
 *     card's Regenerate button replaces them.
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

// Minimal line diff (LCS) for the post-repair view: ctx/del/add rows, with
// new-file line indices on kept/added rows so the gutter stays truthful.
function lineDiff(a, b) {
  const A = a.split('\n'), B = b.split('\n')
  const n = A.length, m = B.length
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const rows = []
  let i = 0, j = 0
  while (i < n && j < m) {
    if (A[i] === B[j]) { rows.push({ t: 'ctx', s: A[i], ln: j }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ t: 'del', s: A[i] }); i++ }
    else { rows.push({ t: 'add', s: B[j], ln: j }); j++ }
  }
  while (i < n) { rows.push({ t: 'del', s: A[i] }); i++ }
  while (j < m) { rows.push({ t: 'add', s: B[j], ln: j }); j++ }
  return rows
}

// Parse a unified git diff into renderable rows — meta lines (diff/index/@@ and
// file headers) become section markers so multi-file repairs read clearly.
function unifiedRows(text) {
  return (text || '').split('\n').map(ln => {
    if (ln.startsWith('+++') || ln.startsWith('---') || ln.startsWith('diff --git')
        || ln.startsWith('index ') || ln.startsWith('@@')) return { t: 'meta', s: ln }
    if (ln.startsWith('+')) return { t: 'add', s: ln.slice(1) }
    if (ln.startsWith('-')) return { t: 'del', s: ln.slice(1) }
    return { t: 'ctx', s: ln.startsWith(' ') ? ln.slice(1) : ln }
  })
}

// Split a multi-file unified diff into { path: sectionText } keyed by the b/ path,
// so the hatch keeps its OWN file's hunk and only OTHER files go to trail editors.
function splitDiffByFile(text) {
  const out = {}
  let cur = null, buf = []
  const flush = () => { if (cur) out[cur] = buf.join('\n') }
  for (const ln of (text || '').split('\n')) {
    const m = ln.match(/^diff --git a\/.+? b\/(.+)$/)
    if (m) { flush(); cur = m[1]; buf = [ln] }
    else if (cur) buf.push(ln)
  }
  flush()
  return out
}

// One file's diff rendered IN the editor's own code area (gutter + coloured +/−
// lines) — never a separate box. Shared by the hatch (own-file fix) and trail
// editors. Git headers are dropped; new-file line numbers keep the gutter honest.
function InlineDiff({ unifiedText }) {
  let nl = 0
  const rows = []
  for (const r of unifiedRows(unifiedText)) {
    if (r.t === 'meta') { const m = r.s.match(/^@@ -\d+(?:,\d+)? \+(\d+)/); if (m) nl = parseInt(m[1], 10); continue }
    if (r.t === 'del') { rows.push({ ...r, ln: null }); continue }
    rows.push({ ...r, ln: nl }); nl += 1
  }
  return (
    <div className="repair-hatch-codewrap">
      <div className="repair-hatch-gutter" aria-hidden>
        {rows.map((r, i) => (
          <div key={i} className={`repair-hatch-ln${r.t === 'add' ? ' add' : r.t === 'del' ? ' del' : ''}`}>{r.ln != null ? r.ln : ''}</div>
        ))}
      </div>
      <div className="repair-hatch-code repair-diff-view">
        {rows.map((r, i) => (
          <div key={i} className={`repair-diff-line ${r.t}`}><span className="repair-diff-sign">{r.t === 'add' ? '+' : r.t === 'del' ? '−' : ' '}</span>{r.s}</div>
        ))}
      </div>
    </div>
  )
}

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
function RepairCard({ dock, title, card, onClose, onRegenerate, onMinimize, actions, lensActive }) {
  const [pos, startDrag] = useDragPos()
  return (
    <div className={`repair-card ${pos ? '' : `${dock}${lensActive ? ' lens' : ''}`}`} data-drag-root
         style={pos ? { left: pos.x, top: pos.y, right: 'auto', transform: 'none' } : undefined}>
      <div className="repair-card-head" onPointerDown={startDrag}>
        <span className="repair-hatch-dot" />
        <span className="repair-card-title">{title}</span>
        {onMinimize && <button className="repair-hatch-min" onClick={onMinimize} title="Minimize to a chip">–</button>}
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

/** "This one's on us": a failed RUN is an OpenFDE fault — prefilled issue for
 *  OUR tracker, fully editable, posted only when the user clicks. */
function ReportIssueCard({ report, setReport, onClose, lensActive }) {
  const [pos, startDrag] = useDragPos()
  const post = async () => {
    setReport(r => ({ ...r, posting: true, error: '' }))
    const res = await reportOpenfdeIssue(report.title, report.body)
    setReport(r => ({ ...r, posting: false,
                      url: res?.ok ? res.url : '',
                      labels: res?.ok ? (res.labels || []) : [],
                      error: res?.ok ? '' : (res?.error || 'could not post — is gh logged in?') }))
  }
  return (
    <div className={`repair-card ${pos ? '' : `dock-a${lensActive ? ' lens' : ''}`}`} data-drag-root
         style={pos ? { left: pos.x, top: pos.y, right: 'auto', transform: 'none' } : undefined}>
      <div className="repair-card-head" onPointerDown={startDrag}>
        <span className="repair-hatch-dot" />
        <span className="repair-card-title">Report to OpenFDE</span>
        <button className="repair-hatch-close" onClick={onClose} title="Close">×</button>
      </div>
      <div className="repair-card-body">
        <div className="repair-card-busy" style={{ fontStyle: 'normal', marginBottom: 6 }}>
          The repair run failed inside OpenFDE — this is our bug, not your repo's.
          Review and post it to our tracker.
        </div>
        {report.busy ? (
          <span className="repair-card-busy">{report.busyLabel || 'drafting…'}</span>
        ) : (
          <>
            <input className="report-title" value={report.title}
                   onChange={e => setReport(r => ({ ...r, title: e.target.value }))} />
            <textarea className="report-body" rows={9} value={report.body}
                      onChange={e => setReport(r => ({ ...r, body: e.target.value }))} />
          </>
        )}
      </div>
      <div className="repair-card-foot">
        {!report.url && !report.busy && (
          <button className="repair-card-btn run" onClick={post} disabled={report.posting}>
            {report.posting ? 'Posting…' : 'Post issue to OpenFDE GitHub'}
          </button>
        )}
        {!report.busy && report.source && <span className="repair-card-src">{report.source}</span>}
        <span style={{ flex: 1 }} />
        {report.url && <a className="repair-card-src" href={report.url} target="_blank" rel="noreferrer">filed ✓ {report.url.split('/').slice(-2).join('/')}{(report.labels || []).length ? ` · ${report.labels.join(', ')}` : ''} ↗</a>}
        {report.error && <span className="repair-card-note">{report.error}</span>}
      </div>
    </div>
  )
}

export default function FunctionPatch({ hatch, slice, z, onClose, onShowFlow, onRepairPhase, onRepairDiff, onBackToFailure, lensActive }) {
  // Two layers. The FAILURE layer (loaded / meta / akey) is FIXED on the
  // function that actually failed — it anchors the artifacts and the on-disk
  // fingerprint, so Explain / Generate / Run never drift. The EDITOR layer can
  // NAVIGATE the failure trail: when `slice` names another function in the chain
  // the editor shows THAT slice (read it, or fix it and ⌘S) while the failure
  // layer stays put. `slice` null ⇒ the editor shows the failure itself.
  const nav = slice && slice.file ? slice : null
  const failKey = `${hatch.file}:${hatch.start}:${hatch.end}`
  const [loaded, setLoaded] = useState({ key: null, code: null, meta: null, err: '' })
  const [navLoaded, setNavLoaded] = useState({ key: null, code: null, meta: null, err: '' })
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
  const [diffState, setDiffState] = useState(null)        // {key, rows} — post-repair diff view
  const [reportState, setReport] = useState(null)          // {key, open, title, body, busy, url, error}
  const [lh, setLh] = useState(0)                         // measured editor line height
  const [scrollTop, setScrollTop] = useState(0)
  const taRef = useRef(null)
  const gutterRef = useRef(null)
  const explainCard = explainState?.key === akey ? explainState : null
  const promptCard = promptState?.key === akey ? promptState : null
  const flow = flowState?.key === akey ? flowState : null
  const run = runState?.key === akey ? runState : null
  const diff = diffState?.key === akey ? diffState : null
  const report = reportState?.key === akey ? reportState : null
  const [hatchPos, startHatchDrag] = useDragPos()
  // Minimize, don't close: a card collapses to a labelled chip in the dock and
  // restores with one click — so the failure-flow aha (bright path + arrow) is
  // never buried, yet the editor/explanation/prompt are one tap away. v1: a flag
  // per card ('hatch' | 'explain' | 'prompt'); nothing is ever lost.
  const [minimized, setMinimized] = useState({})
  const mini = (id) => setMinimized(m => ({ ...m, [id]: true }))
  const restore = (id) => setMinimized(m => ({ ...m, [id]: false }))

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

  // Load the navigated slice on demand — only while reading a non-failure link.
  // No reset when nav clears: the editor reads `loaded` (not navLoaded) in that
  // case, and dispReady gates on the key, so a stale slice can never show.
  useEffect(() => {
    if (!nav) return undefined
    let alive = true
    ;(async () => {
      const r = await getSourceSlice(nav.file, nav.start, nav.end)
      if (!alive) return
      setNavLoaded({ key: `${nav.file}:${nav.start}:${nav.end}`,
                     code: r?.ok ? r.code : null, meta: r?.ok ? r : null,
                     err: r?.ok ? '' : (r?.error || 'could not read source') })
    })()
    return () => { alive = false }
  }, [nav])

  // FAILURE layer — fixed; feeds artifacts + fingerprint.
  const ready = loaded.key === failKey && loaded.code != null
  const meta = ready ? loaded.meta : null

  // EDITOR layer — what's actually on screen: the navigated slice, else failure.
  const dispKey = nav ? `${nav.file}:${nav.start}:${nav.end}` : failKey
  const dispRaw = nav ? navLoaded : loaded
  const dispReady = dispRaw.key === dispKey && dispRaw.code != null
  const dispMeta = dispReady ? dispRaw.meta : null
  const dispCode = draft.key === dispKey ? draft.code : (dispReady ? dispRaw.code : null)
  const dispFile = nav ? nav.file : hatch.file
  const dispName = nav ? (nav.funcName || nav.function) : (hatch.funcName || hatch.func)
  const dispFailLine = nav ? null : hatch.line   // the red line only exists in the failure slice

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

  // Half-second eased scroll to the failing line on open; measures the real
  // line height so the gutter numbers and the red band stay glued to the text.
  useEffect(() => {
    if (!dispReady || nav || !taRef.current) return undefined   // only the failure slice has a red line
    const ta = taRef.current
    const measured = parseFloat(getComputedStyle(ta).lineHeight) || 18
    const rel = Number(hatch.line) - (loaded.meta?.start ?? hatch.start)
    const target = Math.max(0, (rel - 3) * measured)
    let alive = true
    let raf = 0
    const from = ta.scrollTop
    const t0 = performance.now()
    const step = (t) => {
      if (!alive) return
      const k = Math.min(1, (t - t0) / 500)
      const e = 1 - Math.pow(1 - k, 3)
      ta.scrollTop = from + (target - from) * e
      if (gutterRef.current) gutterRef.current.scrollTop = ta.scrollTop
      setScrollTop(ta.scrollTop)
      if (k < 1) raf = requestAnimationFrame(step)
      else setLh(measured)
    }
    raf = requestAnimationFrame(step)
    return () => { alive = false; cancelAnimationFrame(raf) }
  }, [dispReady, akey, hatch, loaded.meta, nav])

  function onCodeScroll(e) {
    setScrollTop(e.target.scrollTop)
    if (gutterRef.current) gutterRef.current.scrollTop = e.target.scrollTop
  }

  // ⌘S saves whatever the editor currently shows — the failure slice by default,
  // or the navigated link when reading the trail (fix the real cause where it
  // lives). The write targets the DISPLAYED file+range, never a hidden one.
  async function save() {
    if (busy || dispCode == null || !dispMeta) return
    setBusy(true)
    const r = await patchSource(dispFile, dispMeta.start, dispMeta.end, dispCode, hatch.episodeId || '')
    setBusy(false)
    if (r?.ok) {
      const patch = l => ({ ...l, code: dispCode, meta: { ...l.meta, end: r.end, total: r.total } })
      if (nav) setNavLoaded(patch); else setLoaded(patch)
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
    onRepairPhase?.('fixing')
    const r = await hatchRun({ ...artifactPayload(), prompt: promptCard.text })
    setRun({ key: akey, last: r?.run || { status: 'failed', error: r?.error || 'run failed' } })
    const recheck = r?.run?.recheck
    const fault = r?.run?.faultDomain
    onRepairPhase?.(recheck === 'passed' ? 'fixed' : 'failing')
    setNote(recheck === 'passed'
      ? (r?.ok ? 'Senior Dev finished — the failing check now PASSES ✓. Review the diff, then Run checks.'
               : 'No changes needed — the failing check already PASSES ✓. Run checks for the full receipt.')
      : fault === 'openfde'
        ? 'The run failed inside OPENFDE — our bug, not your repo. Review & report it in the card.'
      : r?.ok ? 'Senior Dev finished — but the check still fails: the trail continues (Explain · Generate prompt · Run again).'
              : (r?.run?.error || r?.run?.summary || 'Senior Dev run failed'))
    if (fault === 'openfde') {
      const a = r?.run || {}
      // LLM-FIRST: the user's senior_dev drafts the report from the ACCURATE
      // receipt (their provider, their cost) — the server scrubs every known
      // repo string from the output and falls back to the deterministic
      // template when no provider answers. The card stays fully editable and
      // posts only on the user's click.
      setReport({ key: akey, open: true, busy: true,
                  busyLabel: 'Senior Dev is drafting the report from the receipt…',
                  title: '', body: '', url: '', error: '', source: '' })
      const d = await draftOpenfdeReport(
        { status: a.status, error: a.error, summary: a.summary, recheck: a.recheck,
          scope: a.scope, source: a.source, openfdeVersion: a.openfdeVersion,
          writes: a.writes },
        { file: hatch.file, line: hatch.line, test: hatch.test || '',
          failureMsg: hatch.failureMsg || '' })
      setReport(prev => ({ ...(prev || {}), key: akey, open: true, busy: false,
                           title: d?.title || 'Repair hatch: Run with Senior Dev failed',
                           body: d?.body || 'OpenFDE repair run failed; draft unavailable (backend offline).',
                           source: d?.source || '' }))
    }
    // Show exactly what the repair changed — IN THE EDITOR FOR THE FILE IT CHANGED.
    // The fix usually lands in the OTHER contract leg (e.g. Completions.create in
    // client.py), so hand the whole unified diff up to App, which routes each
    // file's hunks into that file's editor (opening one if needed) — never the
    // test hatch.
    if (r?.run?.diff) {
      // Keep THIS hatch's own file inline (it's the function on screen); only the
      // OTHER contract leg(s) open as separate editors — so a fix in the failing
      // function shows in place, not in a surprise box.
      const byFile = splitDiffByFile(r.run.diff)
      const mine = byFile[hatch.file]
      const others = Object.keys(byFile).filter(f => f !== hatch.file)
      if (mine) setDiffState({ key: akey, unifiedText: mine })
      if (others.length) onRepairDiff?.(others.map(f => byFile[f]).join('\n'))
    }
    if (r?.ok && meta) {
      const fresh = await getSourceSlice(hatch.file, meta.start, meta.end)
      if (fresh?.ok && fresh.code !== (loaded.code || '')) {
        if (!r?.run?.diff) setDiffState({ key: akey, rows: lineDiff(loaded.code || '', fresh.code) })
        setLoaded(l => ({ ...l, code: fresh.code, meta: { ...l.meta, end: fresh.end, total: fresh.total } }))
        setDraft({ key: null, code: null })
      }
    }
  }

  const failRel = meta ? hatch.line - meta.start + 1 : null
  const runLine = run?.last && (run.last.status !== 'failed'
    ? `ran ✓ — edited ${(run.last.writes || []).slice(0, 2).join(', ') || 'nothing'}${(run.last.writes || []).length > 2 ? ` +${run.last.writes.length - 2} more` : ''}`
      + (run.last.recheck === 'passed' ? ' · failing test now passes ✓'
         : run.last.recheck === 'failed' ? ' · still failing ✕ (repo — trail continues)' : '')
    : `run ✕ — ${run.last.error || run.last.summary || 'failed'} · OpenFDE fault`)

  return (
    <>
      {!minimized.hatch && (
      <div className="repair-hatch" data-drag-root onKeyDown={onKey}
           style={hatchPos ? { left: hatchPos.x, top: hatchPos.y, right: 'auto', transform: 'none', zIndex: z }
                           : (z != null ? { zIndex: z } : undefined)}>
        <div className="repair-hatch-head" onPointerDown={startHatchDrag}>
          <span className="repair-hatch-dot" />
          <span className="repair-hatch-title">
            {dispName}() · {dispFile}
            {dispMeta && <span className="repair-hatch-lines"> · L{dispMeta.start}–{dispMeta.end}</span>}
          </span>
          <button className="repair-hatch-min" onClick={() => mini('hatch')} title="Minimize to a chip">–</button>
          <button className="repair-hatch-close" onClick={onClose} title="Close the hatch">×</button>
        </div>
        {nav ? (
          <div className="repair-hatch-fail nav">
            <span>↪ a step in the failure trail — read it, or fix here and ⌘S</span>
            <button className="repair-hatch-backfn" onClick={() => onBackToFailure?.()}
                    title="Back to the function that failed">← failing function</button>
          </div>
        ) : (
          <div className="repair-hatch-fail">
            ✕ {hatch.test ? `${hatch.test} fails` : 'failing'} at line {hatch.line}
            {failRel != null && failRel >= 1 && <> (line {failRel} below)</>}
          </div>
        )}
        {!dispReady && <div className="repair-hatch-note" style={{ padding: '0 12px 8px' }}>{dispRaw.err || 'opening the function…'}</div>}
        {dispReady && (nav || !diff) && (() => {
          const lineCount = (dispCode || '').split('\n').length
          const failIdx = dispFailLine != null ? Number(dispFailLine) - dispMeta.start : -1
          const bandTop = (lh && failIdx >= 0) ? 10 + failIdx * lh - scrollTop : null
          return (
            <div className="repair-hatch-codewrap">
              {bandTop != null && bandTop > 6 - lh && (
                <div className="repair-hatch-failband" style={{ top: bandTop, height: lh }} />
              )}
              <div className="repair-hatch-gutter" ref={gutterRef} aria-hidden>
                {Array.from({ length: lineCount }, (_, i) => (
                  <div key={i} className={`repair-hatch-ln${i === failIdx ? ' fail' : ''}`}>
                    {dispMeta.start + i}
                  </div>
                ))}
              </div>
              <textarea
                ref={taRef}
                className="repair-hatch-code"
                value={dispCode}
                onChange={e => setDraft({ key: dispKey, code: e.target.value })}
                onScroll={onCodeScroll}
                spellCheck={false}
                rows={Math.min(22, Math.max(6, lineCount + 1))}
              />
            </div>
          )
        })()}
        {/* Senior Dev's change to THIS function — rendered inline in the editor's
            own code area (not a separate box). The unified-diff path is the run;
            the rows path is the ⌘S line-diff fallback. */}
        {!nav && ready && diff && diff.unifiedText && (
          <>
            <InlineDiff unifiedText={diff.unifiedText} />
            <div className="repair-hatch-diff-foot">
              <span className="repair-card-src">✦ Senior Dev changed this function — review, then Run checks</span>
              <span style={{ flex: 1 }} />
              <button className="repair-card-btn" onClick={() => setDiffState(null)}>Back to editor</button>
            </div>
          </>
        )}
        {!nav && ready && diff && diff.rows && (
          <div className="repair-hatch-diff">
            <div className="repair-hatch-diff-rows">
              {diff.rows.map((r, idx) => (
                <div key={idx} className={`repair-diff-row ${r.t}`}>
                  <span className="repair-diff-ln">{r.ln != null ? meta.start + r.ln : ''}</span>
                  <span className="repair-diff-mark">{r.t === 'del' ? '−' : r.t === 'add' ? '+' : ''}</span>
                  <span className="repair-diff-src">{r.s}</span>
                </div>
              ))}
            </div>
            <div className="repair-hatch-diff-foot">
              <span className="repair-card-src">the repair, line by line — review, then Run checks</span>
              <span style={{ flex: 1 }} />
              <button className="repair-card-btn" onClick={() => setDiffState(null)}>Back to editor</button>
            </div>
          </div>
        )}
        <div className="repair-hatch-foot">
          <button className="repair-hatch-explain" onClick={() => explainError()} disabled={explainCard?.busy}>
            {explainCard?.busy ? 'Explaining…' : 'Explain this error'}
          </button>
          <button className="repair-hatch-prompt" onClick={() => generatePrompt()} disabled={promptCard?.busy}>
            {promptCard?.busy ? 'Generating…' : 'Generate prompt'}
          </button>
          <button className="repair-hatch-flow" onClick={showFlow} disabled={flow?.busy}
                  title="How the failure got there — highlights only the failure path on the canvas">
            {flow?.busy ? 'Tracing…' : 'Show failure flow'}
          </button>
          <span style={{ flex: 1 }} />
          <button className="repair-hatch-save" onClick={save} disabled={busy || !dispReady}>
            {busy ? 'Saving…' : 'Save ⌘S'}
          </button>
        </div>
        {note && <div className="repair-hatch-note" style={{ padding: '0 12px 10px' }}>{note}</div>}
      </div>
      )}

      {explainCard?.open && !minimized.explain && (
        <RepairCard lensActive={lensActive} dock="dock-a" title="Explanation" card={explainCard}
                    onClose={() => setExplainCard(c => ({ ...c, open: false }))}
                    onMinimize={() => mini('explain')}
                    onRegenerate={() => explainError(true)}
                    actions={
                      <button className="repair-card-btn" onClick={() => copyCard(explainCard, setExplainCard)}>
                        Copy
                      </button>
                    } />
      )}
      {report?.open && (
        <ReportIssueCard lensActive={lensActive} report={report} setReport={setReport}
                         onClose={() => setReport(r => ({ ...r, open: false }))} />
      )}
      {promptCard?.open && !minimized.prompt && (
        <RepairCard lensActive={lensActive} dock="dock-b" title="Repair prompt" card={promptCard}
                    onClose={() => setPromptCard(c => ({ ...c, open: false }))}
                    onMinimize={() => mini('prompt')}
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

      {/* Minimized dock — labelled chips for whatever is collapsed, parked in the
          bottom-right corner clear of the failure path. One click restores. The
          editor chip names the file on screen (and flags a trail step); nothing
          here is ever permanently hidden. */}
      {(minimized.hatch || (explainCard?.open && minimized.explain) || (promptCard?.open && minimized.prompt)) && (
        <div className={`hatch-min-dock${lensActive ? ' lens' : ''}`}>
          {minimized.hatch && (
            <button className="hatch-min-chip" onClick={() => restore('hatch')}
                    title="Restore the source editor">
              <span className="hatch-min-dot" />
              Editor · {(dispFile || '').split('/').pop()}{nav ? ' ↪' : ''}
            </button>
          )}
          {explainCard?.open && minimized.explain && (
            <button className="hatch-min-chip" onClick={() => restore('explain')}
                    title="Restore the explanation">
              <span className="hatch-min-dot" /> Explanation
            </button>
          )}
          {promptCard?.open && minimized.prompt && (
            <button className="hatch-min-chip" onClick={() => restore('prompt')}
                    title="Restore the repair prompt">
              <span className="hatch-min-dot" /> Repair prompt
            </button>
          )}
        </div>
      )}
    </>
  )
}

/**
 * TrailEditor — an ADDITIONAL floating function editor opened from a failure-trail
 * node (e.g. the upstream Completions.create that raised the error). It is NOT the
 * repair hatch: it carries no artifacts/fingerprint, just one function slice with
 * its trail line highlighted (gutter + row), so a dev can read the test and the
 * product function side-by-side. ⌘S still writes the slice (fix the cause where it
 * lives). Minimize → chip; close → gone. App owns the set, dedupes, and raises.
 *
 * @param {Object}   props.editor    - {id, file, funcName, start, end, line}
 * @param {string}   props.episodeId - link saves to the live episode (attribution)
 * @param {Function} props.onClose / props.onMinimize / props.onFocus
 * @param {Object}   props.style     - cascade position + zIndex from App
 */
export function TrailEditor({ editor, episodeId = '', onClose, onMinimize, onFocus, lensActive, style }) {
  const key = `${editor.file}:${editor.start}:${editor.end}`
  const [loaded, setLoaded] = useState({ key: null, code: null, meta: null, err: '' })
  const [draft, setDraft] = useState({ key: null, code: null })
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [lh, setLh] = useState(0)
  const [scrollTop, setScrollTop] = useState(0)
  const taRef = useRef(null)
  const gutterRef = useRef(null)
  const [pos, startDrag] = useDragPos()
  // When a Senior-Dev run lands a change in THIS file, App stamps editor.diff —
  // we surface it right here (this is the point of opening the editor) until the
  // user flips back to the live source. Storing the DISMISSED diff text (not a
  // bool) means a fresh diff auto-re-shows without an effect.
  const [hiddenDiff, setHiddenDiff] = useState(null)
  const diffRows = editor.diff ? unifiedRows(editor.diff) : null
  const showDiff = !!diffRows && hiddenDiff !== editor.diff

  useEffect(() => {
    let alive = true
    ;(async () => {
      const r = await getSourceSlice(editor.file, editor.start, editor.end)
      if (!alive) return
      setLoaded({ key, code: r?.ok ? r.code : null, meta: r?.ok ? r : null,
                  err: r?.ok ? '' : (r?.error || 'could not read source') })
    })()
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const ready = loaded.key === key && loaded.code != null
  const meta = ready ? loaded.meta : null
  const code = draft.key === key ? draft.code : (ready ? loaded.code : null)
  // The trail line the lens pointed at — highlighted in BOTH gutter and code row.
  const hiIdx = (meta && editor.line != null) ? Number(editor.line) - meta.start : -1

  // Eased scroll to the highlighted line on open; measures line height so the
  // amber band and gutter number stay glued to the row.
  useEffect(() => {
    if (!ready || !taRef.current) return undefined
    const ta = taRef.current
    const measured = parseFloat(getComputedStyle(ta).lineHeight) || 18
    if (hiIdx < 0) { setLh(measured); return undefined }
    const target = Math.max(0, (hiIdx - 3) * measured)
    let alive = true, raf = 0
    const from = ta.scrollTop, t0 = performance.now()
    const stepFn = (t) => {
      if (!alive) return
      const k = Math.min(1, (t - t0) / 460)
      const e = 1 - Math.pow(1 - k, 3)
      ta.scrollTop = from + (target - from) * e
      if (gutterRef.current) gutterRef.current.scrollTop = ta.scrollTop
      setScrollTop(ta.scrollTop)
      if (k < 1) raf = requestAnimationFrame(stepFn); else setLh(measured)
    }
    raf = requestAnimationFrame(stepFn)
    return () => { alive = false; cancelAnimationFrame(raf) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, key])

  function onCodeScroll(e) {
    setScrollTop(e.target.scrollTop)
    if (gutterRef.current) gutterRef.current.scrollTop = e.target.scrollTop
  }

  async function save() {
    if (busy || code == null || !meta) return
    setBusy(true)
    const r = await patchSource(editor.file, meta.start, meta.end, code, episodeId || '')
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

  const lineCount = (code || '').split('\n').length
  const bandTop = (lh && hiIdx >= 0) ? 10 + hiIdx * lh - scrollTop : null

  return (
    <div className={`repair-hatch trail-editor${lensActive ? ' lens' : ''}`} data-drag-root
         onKeyDown={onKey} onPointerDownCapture={onFocus}
         style={pos ? { ...style, left: pos.x, top: pos.y, right: 'auto', transform: 'none' } : style}>
      <div className="repair-hatch-head" onPointerDown={startDrag}>
        <span className="repair-hatch-dot trail" />
        <span className="repair-hatch-title">
          {editor.funcName}() · {editor.file}
          {meta && <span className="repair-hatch-lines"> · L{meta.start}–{meta.end}</span>}
        </span>
        <button className="repair-hatch-min" onClick={onMinimize} title="Minimize to a chip">–</button>
        <button className="repair-hatch-close" onClick={onClose} title="Close this editor">×</button>
      </div>
      <div className={`repair-hatch-fail trail${showDiff ? ' applied' : ''}`}>
        {showDiff
          ? <>✦ Senior Dev changed this file — review the diff</>
          : <>↪ in the failure trail{editor.line != null && <> · line {editor.line} highlighted</>}</>}
      </div>
      {!ready && !showDiff && <div className="repair-hatch-note" style={{ padding: '0 12px 8px' }}>{loaded.err || 'opening the function…'}</div>}
      {showDiff && (() => {
        // The diff renders IN the editor's own code area (same gutter + lines),
        // not a separate box — and the failure-line highlight is gone (this is the
        // change, not the failure). Git headers are dropped; we walk the hunks to
        // number the new-file lines so the gutter stays truthful.
        let nl = 0
        const rows = []
        for (const r of diffRows) {
          if (r.t === 'meta') { const m = r.s.match(/^@@ -\d+(?:,\d+)? \+(\d+)/); if (m) nl = parseInt(m[1], 10); continue }
          if (r.t === 'del') { rows.push({ ...r, ln: null }); continue }
          rows.push({ ...r, ln: nl }); nl += 1
        }
        return (
          <div className="repair-hatch-codewrap">
            <div className="repair-hatch-gutter" aria-hidden>
              {rows.map((r, i) => (
                <div key={i} className={`repair-hatch-ln${r.t === 'add' ? ' add' : r.t === 'del' ? ' del' : ''}`}>{r.ln != null ? r.ln : ''}</div>
              ))}
            </div>
            <div className="repair-hatch-code repair-diff-view">
              {rows.map((r, i) => (
                <div key={i} className={`repair-diff-line ${r.t}`}>
                  <span className="repair-diff-sign">{r.t === 'add' ? '+' : r.t === 'del' ? '−' : ' '}</span>{r.s}
                </div>
              ))}
            </div>
          </div>
        )
      })()}
      {!showDiff && ready && (
        <div className="repair-hatch-codewrap">
          {bandTop != null && bandTop > 6 - lh && (
            <div className="repair-hatch-failband trail" style={{ top: bandTop, height: lh }} />
          )}
          <div className="repair-hatch-gutter" ref={gutterRef} aria-hidden>
            {Array.from({ length: lineCount }, (_, i) => (
              <div key={i} className={`repair-hatch-ln${i === hiIdx ? ' hi' : ''}`}>{meta.start + i}</div>
            ))}
          </div>
          <textarea
            ref={taRef}
            className="repair-hatch-code"
            value={code}
            onChange={e => setDraft({ key, code: e.target.value })}
            onScroll={onCodeScroll}
            spellCheck={false}
            rows={Math.min(20, Math.max(6, lineCount + 1))}
          />
        </div>
      )}
      <div className="repair-hatch-foot">
        {diffRows && (
          <button className="repair-card-btn" onClick={() => setHiddenDiff(showDiff ? editor.diff : null)}>
            {showDiff ? 'Back to editor' : 'Show diff'}
          </button>
        )}
        <span style={{ flex: 1 }} />
        <button className="repair-hatch-save" onClick={save} disabled={busy || !ready}>
          {busy ? 'Saving…' : 'Save ⌘S'}
        </button>
      </div>
      {note && <div className="repair-hatch-note" style={{ padding: '0 12px 10px' }}>{note}</div>}
    </div>
  )
}
