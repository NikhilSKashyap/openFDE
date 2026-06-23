import { useState, useEffect } from 'react'
import { MOMENT_LABEL, MOMENT_PROMPT } from '../../productFlow/deriveMoment'
import CouncilChat from '../CouncilChat/CouncilChat'
import { postFocusVerifyPlan } from '../../api/backend'

/**
 * WorkPanel — the single progressive panel from FLOW.md (Step 28 Slice 2).
 *
 * One coherent product path: each moment shows the right content and exactly one
 * obvious primary action. Additive shell — it reuses existing app data/handlers
 * and does NOT replace the tabbed RightPanel yet. The moment is driven by App's
 * "current work unit" (not a stale global spec).
 */

function scopeLabel(sel) {
  if (!sel) return ''
  if (sel.entity) return sel.entity.name || sel.entity.path || sel.entity.id || ''
  const boxes = sel.boxes || []
  if (boxes.length === 1) return boxes[0].title
  if (boxes.length > 1) return `${boxes.length} modules`
  if ((sel.arrows || []).length) return 'connection'
  return ''
}

function councilRunId(msg) {
  const m = /^council-stage-(.+)-\d+$/.exec(msg?.id || '')
  return m ? m[1] : null
}

export default function WorkPanel({
  moment = 'orient', selectionContext = null, story = null,
  specMarkdown = null, commitDiff = null, agentMessages = [], approvals = [],
  onExecute = null, onExplain = null, onOpenDiff = null, onReset = null,
  intent = '', onIntentChange = null, run = null, onStop = null,
  onOpenAgentSettings = null, councilNonce = 0,
}) {
  const setIntent = onIntentChange || (() => {})
  const scope = scopeLabel(selectionContext)
  const submit = () => { if (onExecute) onExecute() }
  const lastResult = [...agentMessages].reverse().find(m => m.role === 'result') || null
  const pendingApproval = (approvals || []).some(a => a.status === 'pending')
  // Review commit: trust THIS run's result. Only fall back to a globally-selected
  // commitDiff when there is no fresh run result (Step 29 Slice 3 — no stale sha).
  const reviewCommitSha = lastResult
    ? (lastResult.committed ? lastResult.commitSha : null)
    : (commitDiff?.sha || null)
  // Inline diff for THIS run's commit (pre-loaded after the council commits), so
  // Review shows the actual change in-place — no detour to the Diff tab.
  const reviewDiffPatch =
    reviewCommitSha && commitDiff?.data?.patch &&
    String(commitDiff.sha).slice(0, 7) === String(reviewCommitSha).slice(0, 7)
      ? commitDiff.data.patch : null
  const reviewNeedsApproval = !!(lastResult?.approval) || (!lastResult && pendingApproval)
  // L2-B: the SCOPED VERIFY PLAN for this run's touched files — advisory only (it never runs or changes
  // the verify gate), surfaced so the scoped plan is visible before it becomes the enforced default.
  // Keyed by the touched set so a stale plan is derived away (no synchronous reset in the effect).
  const [verifyPlan, setVerifyPlan] = useState(null)   // { key, data }
  const touchedKey = (lastResult?.writes || []).join('|')
  useEffect(() => {
    let alive = true
    const files = touchedKey ? touchedKey.split('|') : []
    if (moment === 'review' && files.length) {
      postFocusVerifyPlan({ touchedFiles: files })
        .then(r => { if (alive && r?.ok) setVerifyPlan({ key: touchedKey, data: r }) })
    }
    return () => { alive = false }
  }, [moment, touchedKey])
  const verifyForRender = verifyPlan?.key === touchedKey ? verifyPlan.data : null
  // Council stage story (Step 29 Slice 3 polish): the Architect → Sr Dev →
  // Verifier stages for THIS run, surfaced directly in Work Review.
  const latestCouncilRun = lastResult?.fromRun ||
    [...agentMessages].reverse().map(councilRunId).find(Boolean)
  const councilStages = latestCouncilRun
    ? agentMessages.filter(m => m.councilStage && councilRunId(m) === latestCouncilRun)
    : []

  return (
    <div className="work-panel">
      <div className="work-head">
        <span className="work-moment-dot" data-moment={moment} />
        <span className="work-moment-label">{MOMENT_LABEL[moment]}</span>
        <span className="work-moment-sub">{MOMENT_PROMPT[moment]}</span>
      </div>

      <div className="work-body">
        {/* ── Orient: the user-facing moment. Council is the brain behind it —
            type what you want and OpenFDE routes it to the right council member
            (read-only). Building happens once you scope it (Understand / Change). ── */}
        {moment === 'orient' && (
          <Section title="Ask the council">
            <CouncilChat onOpenAgentSettings={onOpenAgentSettings} councilNonce={councilNonce} />
            <p className="work-hint">OpenFDE routes your question to the right council member.
              Select a module on the canvas to dig into its architecture and make a change.</p>
          </Section>
        )}

        {/* ── Understand ───────────────────────────────────────────── */}
        {moment === 'understand' && (
          <>
            <Section title="What this is">
              <div className="work-scope">{scope || 'Selected scope'}</div>
              {story?.summary && <p className="work-sub">{story.summary}</p>}
              {onExplain && (
                <button className="work-link" onClick={() => onExplain()}>Ask / explain →</button>
              )}
            </Section>
            <Section title="Change this">
              <PromptBox value={intent} onChange={setIntent} onSubmit={submit}
                placeholder={`Change ${scope || 'this'}…`} action="Run" disabled={!onExecute} />
            </Section>
            {story?.steps?.length > 0 && (
              <Details label="How it works (story)">
                {story.steps.map(st => (
                  <div key={st.id} className="work-detail-row"><b>{st.order}. {st.label}</b> — {st.description}</div>
                ))}
              </Details>
            )}
          </>
        )}

        {/* ── Change ───────────────────────────────────────────────── */}
        {moment === 'change' && (
          <>
            <Section title="Change">
              <div className="work-scope">{scope || 'Repository-level'}</div>
              <PermissionSummary selectionContext={selectionContext} />
              <PromptBox value={intent} onChange={setIntent} onSubmit={submit}
                placeholder="Describe the change…" action="Run" disabled={!onExecute} />
            </Section>
            {specMarkdown && (
              <Details label="Compiled scope (details)">
                <pre className="work-pre">{specMarkdown.slice(0, 1200)}</pre>
              </Details>
            )}
          </>
        )}

        {/* ── Execute: live run status + Stop ──────────────────────── */}
        {moment === 'execute' && (
          <Section title="Working">
            <RunStatus run={run} onStop={onStop} />
            <RecentMessages messages={agentMessages} />
          </Section>
        )}

        {/* ── Review ───────────────────────────────────────────────── */}
        {moment === 'review' && (
          <>
            {councilStages.length > 0 && (
              <Section title="What the agents did">
                <div className="work-stages">
                  {councilStages.map(s => <CouncilStage key={s.id} stage={s} />)}
                </div>
              </Section>
            )}
            <Section title="Review">
              {lastResult?.cancelled && (
                <div className="work-cancelled">■ Cancelled by user — nothing was committed.</div>
              )}
              {lastResult?.reportSummary && !lastResult?.cancelled && (
                <p className="work-sub">{lastResult.reportSummary}</p>
              )}
              {lastResult?.generatedScope && lastResult?.workspace && !lastResult?.cancelled && (
                <div className="work-scope">
                  Generated workspace: <code>{lastResult.workspace}</code>
                </div>
              )}
              {(lastResult?.writes?.length > 0) && (
                <div className="work-files">
                  {lastResult.writes.slice(0, 8).map(f => <code key={f} className="work-file">{f}</code>)}
                </div>
              )}
              {verifyForRender && (
                <div className="work-verifyplan">
                  <div className="work-verifyplan-h">
                    Verify plan
                    <span className={`work-verify-mode ${verifyForRender.mode}`}>{verifyForRender.mode}</span>
                  </div>
                  <p className="work-sub">{verifyForRender.reason}</p>
                  {(verifyForRender.warnings || []).map((w, i) => (
                    <p key={i} className="work-verify-warn">{w}</p>
                  ))}
                </div>
              )}
              {reviewCommitSha && (
                <div className="work-scope">
                  Committed{' '}
                  {onOpenDiff
                    ? <button className="work-sha" onClick={() => onOpenDiff(reviewCommitSha)}
                        title="Open the full diff">
                        <code>{String(reviewCommitSha).slice(0, 7)}</code>
                      </button>
                    : <code>{String(reviewCommitSha).slice(0, 7)}</code>}
                </div>
              )}
              {reviewDiffPatch && (
                <div className="work-diffwrap">
                  <div className="work-diff-title">The change</div>
                  <DiffView patch={reviewDiffPatch} />
                </div>
              )}
              {reviewNeedsApproval && (
                <p className="work-sub" style={{ color: 'var(--accent)' }}>Approval required — resolve it in Technical.</p>
              )}
              {lastResult && !lastResult.committed && !reviewNeedsApproval && lastResult.status !== 'passed' && (
                <p className="work-sub">No commit — work was not accepted.</p>
              )}
              {!lastResult && !commitDiff && (
                <p className="work-sub">
                  No fresh execution result yet. Use the Agent Council backend for the full Architect → Senior Dev → Verifier story.
                </p>
              )}
            </Section>
            <div className="work-actions">
              <button className="work-primary" onClick={() => onReset && onReset()}>Done</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

const STAGE_ROLE = { architect: 'Architect', sr_dev: 'Senior Dev', verifier: 'Verifier' }
function stageTone(status) {
  if (status === 'passed') return 'var(--solid)'
  if (status === 'failed' || status === 'needs_human') return 'var(--violation)'
  if (status === 'needs_approval') return 'var(--accent)'
  return 'var(--text-muted)'
}

function CouncilStage({ stage }) {
  const tone = stageTone(stage.status)
  return (
    <div className="work-stage">
      <div className="work-stage-head">
        <span className="work-stage-role">{STAGE_ROLE[stage.role] || stage.role}</span>
        {stage.provider && <span className="work-stage-provider">· {stage.provider}</span>}
        <span className="work-stage-status" style={{ color: tone }}>
          {stage.status}{stage.attempt > 1 ? ` · try ${stage.attempt}` : ''}
        </span>
      </div>
      {stage.summary && <div className="work-stage-summary">{(stage.summary || '').slice(0, 120)}</div>}
    </div>
  )
}

function diffLineClass(ln) {
  if (ln.startsWith('+') && !ln.startsWith('+++')) return 'work-diff-add'
  if (ln.startsWith('-') && !ln.startsWith('---')) return 'work-diff-del'
  if (ln.startsWith('@@')) return 'work-diff-hunk'
  if (ln.startsWith('diff ') || ln.startsWith('index ') ||
      ln.startsWith('+++') || ln.startsWith('---')) return 'work-diff-meta'
  return 'work-diff-ctx'
}

function DiffView({ patch }) {
  const lines = String(patch || '').split('\n').slice(0, 120)
  return (
    <div className="work-diff">
      {lines.map((ln, i) => (
        <div key={i} className={diffLineClass(ln)}>{ln || ' '}</div>
      ))}
    </div>
  )
}

function RunStatus({ run, onStop }) {
  // `now` lives in state (updated by the interval) so render stays pure.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  const planned = run?.plannedFiles?.length || 0
  const written = run?.written?.length || 0
  const active = run?.activeFile || null
  const elapsed = run?.startedAt ? Math.max(0, Math.floor((now - run.startedAt) / 1000)) : null
  const fileName = p => (p ? p.split('/').pop() : null)
  return (
    <div className="work-run">
      <div className="work-running"><span className="work-spin" /> Agents are working…</div>
      {active && <div className="work-run-active">Editing <code>{fileName(active)}</code></div>}
      <div className="work-run-stats">
        {planned > 0 && <span className="work-run-stat">{written}/{planned} files</span>}
        {elapsed != null && <span className="work-run-stat">{elapsed}s</span>}
      </div>
      {onStop && <button className="work-stop" onClick={() => onStop()}>■ Stop</button>}
    </div>
  )
}

function PermissionSummary({ selectionContext }) {
  const boxes = selectionContext?.boxes || []
  if (boxes.length > 0) {
    const dotted = boxes.filter(b => b.type === 'dotted').length
    const solid = boxes.length - dotted
    return (
      <div className="work-perms">
        {dotted > 0 && <span className="work-perm dotted">● {dotted} agent-editable</span>}
        {solid > 0 && <span className="work-perm solid">● {solid} protected (approval)</span>}
      </div>
    )
  }
  // File/function selections (no boxes) carry inherited permission via moduleType.
  const mt = selectionContext?.moduleType
  if (!mt) return null
  return (
    <div className="work-perms">
      {mt === 'dotted'
        ? <span className="work-perm dotted">● agent-editable (inherited)</span>
        : <span className="work-perm solid">● protected — approval required (inherited)</span>}
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div className="work-section">
      <div className="work-section-title">{title}</div>
      {children}
    </div>
  )
}

function PromptBox({ value, onChange, onSubmit, placeholder, action, disabled }) {
  return (
    <div className="work-promptbox">
      <textarea className="work-textarea" value={value} placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); onSubmit() } }} />
      <button className="work-primary" onClick={onSubmit} disabled={disabled}>{action}</button>
    </div>
  )
}

function Details({ label, children }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="work-details">
      <button className="work-details-toggle" onClick={() => setOpen(o => !o)}>{open ? '▾' : '▸'} {label}</button>
      {open && <div className="work-details-body">{children}</div>}
    </div>
  )
}

function RecentMessages({ messages = [], role = null }) {
  const items = (role ? messages.filter(m => m.role === role) : messages).slice(-3)
  if (!items.length) return <p className="work-sub">No activity yet.</p>
  return (
    <div className="work-msgs">
      {items.map(m => (
        <div key={m.id} className="work-msg">
          <b>{m.role}</b> {m.reportSummary || m.summary || m.body || ''}
        </div>
      ))}
    </div>
  )
}
