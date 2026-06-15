import { useState } from 'react'
import ReportIssueCard from './ReportIssueCard'
import { draftGeneralReport } from '../../api/backend'

/** Top-bar "Raise OpenFDE issue": the GENERAL product-feedback flow. The user
 *  describes a bug / idea / rough edge / perf problem, the ARCHITECT drafts an
 *  editable issue from that + light OpenFDE context (the backend scrubs any repo
 *  detail), and nothing posts to GitHub until the user clicks Raise. Mounted only
 *  while open (App renders it conditionally), so each open starts a fresh compose. */
export default function RaiseIssue({ onClose, context }) {
  const [report, setReport] = useState({
    phase: 'compose', kind: 'bug', description: '',
    title: '', body: '', source: '', labels: [],
    busy: false, posting: false, url: '', error: '',
  })

  const onDraft = async () => {
    setReport(r => ({ ...r, busy: true, busyLabel: 'Architect is drafting the issue…', error: '' }))
    const d = await draftGeneralReport(report.description, report.kind,
                                       (typeof context === 'function' ? context() : context) || {})
    setReport(r => ({
      ...r, phase: 'review', busy: false,
      title: d?.title || 'OpenFDE feedback',
      body: d?.body || 'Draft unavailable (backend offline) — describe the issue here.',
      source: d?.source || '',
      // Preview the kind as a label; the backend confirms the full set on Raise.
      labels: r.kind && r.kind !== 'other' ? [r.kind] : [],
    }))
  }

  return (
    <ReportIssueCard
      report={report} setReport={setReport} onClose={onClose} lensActive={false}
      title="Raise OpenFDE issue" dockClass="raise-issue-card" raiseLabel="Raise issue"
      blurb="Found a bug, rough edge, or have an idea? Describe it — Architect drafts an issue you review before it's raised on OpenFDE's tracker."
      onDraft={onDraft}
    />
  )
}
