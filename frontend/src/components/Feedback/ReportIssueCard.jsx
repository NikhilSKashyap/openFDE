import { reportOpenfdeIssue } from '../../api/backend'
import { useDragPos } from '../../lib/useDragPos'

const KINDS = [['bug', 'Bug'], ['feature', 'Feature'], ['ux', 'UX'],
               ['performance', 'Performance'], ['other', 'Other']]

/** Shared frosted/draggable "raise an OpenFDE issue" card — used by the repair
 *  hatch (a failed run is OUR bug) AND the global top-bar "Raise issue". Two phases:
 *   - compose (general feedback only): a description + kind chips + "Draft issue",
 *     which calls `onDraft()` (the parent drafts via Architect and flips to review);
 *   - review (always): editable title/body (+ labels preview) + the raise button,
 *     which posts to OUR tracker — only on the user's click. The repair path arrives
 *     pre-drafted, so it skips compose (no `onDraft`, phase defaults to 'review').
 *  State lives in the parent's `report`/`setReport` so each caller owns its lifecycle. */
export default function ReportIssueCard({
  report, setReport, onClose, lensActive,
  title = 'Report to OpenFDE', blurb = '', raiseLabel = 'Raise issue',
  dockClass = 'dock-a', onDraft = null,
}) {
  const [pos, startDrag] = useDragPos()
  const phase = report.phase || 'review'

  const raise = async () => {
    setReport(r => ({ ...r, posting: true, error: '' }))
    const res = await reportOpenfdeIssue(report.title, report.body,
                                         { kind: report.kind, labels: report.labels })
    setReport(r => ({ ...r, posting: false,
                      url: res?.ok ? res.url : '',
                      labels: res?.ok ? (res.labels || r.labels || []) : (r.labels || []),
                      error: res?.ok ? '' : (res?.error || 'could not post — is gh logged in?') }))
  }

  return (
    <div className={`repair-card ${pos ? '' : `${dockClass}${lensActive ? ' lens' : ''}`}`} data-drag-root
         style={pos ? { left: pos.x, top: pos.y, right: 'auto', transform: 'none' } : undefined}>
      <div className="repair-card-head" onPointerDown={startDrag}>
        <span className="repair-hatch-dot" />
        <span className="repair-card-title">{title}</span>
        <button className="repair-hatch-close" onClick={onClose} title="Close">×</button>
      </div>
      <div className="repair-card-body">
        {blurb && (
          <div className="repair-card-busy" style={{ fontStyle: 'normal', marginBottom: 6 }}>{blurb}</div>
        )}
        {phase === 'compose' ? (
          <>
            <textarea className="report-body" rows={5} autoFocus
                      placeholder="What were you trying to do? What happened?"
                      value={report.description || ''}
                      onChange={e => setReport(r => ({ ...r, description: e.target.value }))} />
            <div className="report-kinds">
              {KINDS.map(([id, label]) => (
                <button key={id} type="button"
                        className={`report-kind${report.kind === id ? ' on' : ''}`}
                        onClick={() => setReport(r => ({ ...r, kind: id }))}>{label}</button>
              ))}
            </div>
          </>
        ) : report.busy ? (
          <span className="repair-card-busy">{report.busyLabel || 'drafting…'}</span>
        ) : (
          <>
            <input className="report-title" value={report.title || ''}
                   onChange={e => setReport(r => ({ ...r, title: e.target.value }))} />
            <textarea className="report-body" rows={9} value={report.body || ''}
                      onChange={e => setReport(r => ({ ...r, body: e.target.value }))} />
            {(report.labels || []).length > 0 && (
              <div className="report-labels">
                {report.labels.map(l => <span className="report-label" key={l}>{l}</span>)}
              </div>
            )}
          </>
        )}
      </div>
      <div className="repair-card-foot">
        {phase === 'compose' && !report.busy && (
          <button className="repair-card-btn run" onClick={onDraft}
                  disabled={!(report.description || '').trim()}>Draft issue</button>
        )}
        {phase === 'review' && !report.url && !report.busy && (
          <button className="repair-card-btn run" onClick={raise}
                  disabled={report.posting || !(report.title || '').trim()}>
            {report.posting ? 'Raising…' : raiseLabel}
          </button>
        )}
        {!report.busy && report.source && <span className="repair-card-src">{report.source}</span>}
        <span style={{ flex: 1 }} />
        {report.url && (
          <a className="repair-card-src" href={report.url} target="_blank" rel="noreferrer">
            filed ✓ {report.url.split('/').slice(-2).join('/')}
            {(report.labels || []).length ? ` · ${report.labels.join(', ')}` : ''} ↗
          </a>
        )}
        {report.error && <span className="repair-card-note">{report.error}</span>}
      </div>
    </div>
  )
}
